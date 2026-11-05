"""OKBrowser 本地 API 客户端、指纹窗口会话、配置读写。"""
from __future__ import annotations

import httpx
import pytest

from claude_register import browser, okbrowser
from claude_register.okbrowser import OkBrowserClient, OkBrowserError, OkBrowserSettings
from server.config_store import load_config, save_config, to_dict


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    OkBrowserClient._last_call = 0.0
    yield
    OkBrowserClient._last_call = 0.0


def _client(handler, calls=None, sleeps=None):
    clock = {"t": 100.0}

    def sleep(s):
        (sleeps if sleeps is not None else []).append(s)
        clock["t"] += s

    def wrapped(request):
        if calls is not None:
            calls.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(wrapped))
    return OkBrowserClient("http://ok.test:30396/", http=http, sleep=sleep,
                           clock=lambda: clock["t"])


def test_proxy_obj_maps_auth_socks5():
    assert okbrowser.proxy_obj("socks5h://u%40x:p%3Aw@gw.example:8001") == {
        "protocol": "socks5", "host": "gw.example", "port": "8001",
        "username": "u@x", "password": "p:w", "apiUrl": "",
    }
    assert okbrowser.proxy_obj("") is None
    with pytest.raises(OkBrowserError):
        okbrowser.proxy_obj("ftp://h:1")


def test_create_sends_group_and_proxy():
    calls = []
    client = _client(lambda r: httpx.Response(200, json={"code": 0, "data": {"id": "123456789012345"}}),
                     calls)
    wid = client.create(name="a@outlook.com", group_id="g1", proxy="http://u:p@h:8080")
    assert wid == "123456789012345"
    req = calls[0]
    assert req.method == "POST" and req.url.path == "/api/browser/create"
    body = httpx.Response(200, content=req.content).json()
    assert body["groupId"] == "g1" and body["proxyWay"] is True
    assert body["proxyObj"]["host"] == "h" and body["proxyObj"]["password"] == "p"


def test_create_without_proxy_disables_proxy_way():
    calls = []
    client = _client(lambda r: httpx.Response(200, json={"code": 0, "data": {"id": "1"}}), calls)
    client.create(name="n", group_id="g")
    body = httpx.Response(200, content=calls[0].content).json()
    assert body["proxyWay"] is False and "proxyObj" not in body


def test_update_proxy_edits_window_and_restarts_if_running(monkeypatch):
    calls = []
    client = _client(lambda r: httpx.Response(200, json={
        "code": 0, "data": {"ws": "ws://127.0.0.1:5983/devtools/browser/new"}}), calls)
    client.update_proxy("W1", "socks5://u:p@h:1080")
    req = calls[0]
    assert req.method == "PUT" and req.url.path == "/api/browser/edit"
    body = httpx.Response(200, content=req.content).json()
    assert body["id"] == "W1" and body["proxyWay"] is True
    assert body["proxyObj"]["host"] == "h" and body["proxyObj"]["port"] == "1080"

    calls.clear()
    client.update_proxy("W1", None)
    body = httpx.Response(200, content=calls[0].content).json()
    assert body == {"id": "W1", "proxyWay": False}

    # 窗口正在运行时：update_proxy 先 close，紧接着 start 走 /api/browser/start 重新拉起而非直接复用旧 ws
    calls.clear()
    monkeypatch.setattr(okbrowser, "running_ws", lambda wid: "ws://old-running")
    client.update_proxy("W1", "http://h:8080")
    ws = client.start("W1")
    assert [c.url.path for c in calls] == [
        "/api/browser/close", "/api/browser/edit", "/api/browser/start",
    ]
    assert ws == "ws://127.0.0.1:5983/devtools/browser/new"


def test_start_returns_ws_and_errors_surface():
    client = _client(lambda r: httpx.Response(200, json={
        "code": 0, "data": {"ws": "ws://127.0.0.1:5983/devtools/browser/x", "http": "127.0.0.1:5983"}}))
    assert client.start("1").startswith("ws://")

    bad = _client(lambda r: httpx.Response(200, json={"code": -4, "msg": "login"}))
    with pytest.raises(OkBrowserError, match="登录"):
        bad.status()


def test_unreachable_api_gives_clear_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(OkBrowserError, match="客户端已打开"):
        _client(boom).status()


def test_calls_are_rate_limited():
    sleeps = []
    client = _client(lambda r: httpx.Response(200, json={"code": 0}), sleeps=sleeps)
    client.status()
    client.status()
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(okbrowser.MIN_INTERVAL_S)


class _FakeBrowser:
    def __init__(self):
        self.contexts = ["DEFAULT_CTX"]
        self.closed = False

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self, log):
        self.log = log
        self.chromium = self
        self.browser = _FakeBrowser()

    def start(self):
        return self

    def connect_over_cdp(self, ws):
        self.log.append(("connect", ws))
        return self.browser

    def stop(self):
        self.log.append(("stop",))


class _FakeClient:
    def __init__(self, log, fail_start=False):
        self.log = log
        self.fail_start = fail_start

    def __call__(self, api_url):
        self.log.append(("init", api_url))
        return self

    def create(self, *, name, group_id, proxy):
        self.log.append(("create", name, group_id, proxy))
        return "W1"

    def update_proxy(self, wid, proxy=None):
        self.log.append(("update_proxy", wid, proxy))

    def start(self, wid):
        if self.fail_start:
            raise OkBrowserError("boom")
        return "ws://cdp"

    def close(self, wid):
        self.log.append(("close", wid))

    def delete(self, wid):
        self.log.append(("delete", wid))


class _FakeOverride:
    def __init__(self, log):
        self.log = log

    def __call__(self, ws, fingerprint, on_error=None):
        self.log.append(("override", ws, fingerprint.timezone))
        return self

    def start(self):
        return self

    def stop(self):
        self.log.append(("override-stop",))


def _patch_session(monkeypatch, log, timezone="America/Chicago", **kw):
    from claude_register import cdp_override

    pw = _FakePlaywright(log)
    monkeypatch.setattr(browser, "sync_playwright", lambda: pw)
    monkeypatch.setattr(okbrowser, "OkBrowserClient", _FakeClient(log, **kw))
    monkeypatch.setattr(cdp_override, "CdpOverrideSession", _FakeOverride(log))

    def fake_lookup(proxy_url, **kwargs):
        log.append(("tz-lookup", proxy_url, kwargs.get("direct")))
        return timezone

    monkeypatch.setattr(browser, "lookup_timezone", fake_lookup)
    return pw


def test_okbrowser_session_reuses_default_context_and_cleans_up(monkeypatch):
    log = []
    pw = _patch_session(monkeypatch, log)
    settings = OkBrowserSettings(api_url="http://ok", group_id="g", delete_after=True)
    with browser.browser_session(proxy="socks5://u:p@h:1", okbrowser=settings, name="a@x.com") as b:
        assert browser.new_context(b) == "DEFAULT_CTX"
    assert ("create", "a@x.com", "g", "socks5://u:p@h:1") in log
    assert ("tz-lookup", "socks5://u:p@h:1", True) in log
    # 覆盖要在 Playwright 连上之前挂好，新页面脚本执行前就生效
    assert log.index(("override", "ws://cdp", "America/Chicago")) < log.index(("connect", "ws://cdp"))
    assert pw.browser.closed
    assert log[-3:] == [("override-stop",), ("close", "W1"), ("delete", "W1")]


def test_okbrowser_session_fails_when_timezone_unknown(monkeypatch):
    log = []
    _patch_session(monkeypatch, log, timezone=None)
    with pytest.raises(RuntimeError, match=r"socks5://h:1 查不到出口 IP 的时区") as ei:
        with browser.browser_session(proxy="socks5://u:p@h:1",
                                     okbrowser=OkBrowserSettings(group_id="g")):
            pass
    assert "u:p" not in str(ei.value)
    # 代理不通时不建窗口，也就不留下空窗口
    assert not any(e[0] in ("create", "start", "close", "delete", "connect") for e in log)


def test_session_failure_carries_kept_window_id(monkeypatch):
    log = []
    _patch_session(monkeypatch, log)
    with pytest.raises(ValueError) as ei:
        with browser.browser_session(okbrowser=OkBrowserSettings(group_id="g", delete_after=False)):
            raise ValueError("flow broke")
    assert ei.value.okbrowser_window_id == "W1"

    log.clear()
    with pytest.raises(ValueError) as ei:
        with browser.browser_session(okbrowser=OkBrowserSettings(group_id="g", delete_after=True)):
            raise ValueError("flow broke")
    assert not hasattr(ei.value, "okbrowser_window_id")


def test_okbrowser_session_keeps_window_when_delete_disabled(monkeypatch):
    log = []
    _patch_session(monkeypatch, log)
    with browser.browser_session(okbrowser=OkBrowserSettings(group_id="g", delete_after=False)):
        pass
    assert ("close", "W1") in log and not any(e[0] == "delete" for e in log)


def test_okbrowser_session_closes_window_when_start_fails(monkeypatch):
    log = []
    _patch_session(monkeypatch, log, fail_start=True)
    with pytest.raises(OkBrowserError):
        with browser.browser_session(okbrowser=OkBrowserSettings(group_id="g")):
            pass
    # 没启动成功就不关闭（OKBrowser 会报 Browser not found），新建的窗口照样删掉
    assert ("close", "W1") not in log and ("delete", "W1") in log


class _WindowClient(_FakeClient):
    """start 对 dead 集合里的窗口报错（模拟窗口已在客户端被删）。"""

    def __init__(self, log, dead=()):
        super().__init__(log)
        self.dead = set(dead)

    def start(self, wid):
        self.log.append(("start", wid))
        if wid in self.dead:
            raise OkBrowserError("窗口不存在")
        return "ws://cdp"


def _patch_windows(monkeypatch, log, dead=()):
    _patch_session(monkeypatch, log)
    monkeypatch.setattr(okbrowser, "OkBrowserClient", _WindowClient(log, dead))


def test_session_reuses_bound_window_without_create_or_delete(monkeypatch):
    log, seen = [], []
    _patch_windows(monkeypatch, log)
    settings = OkBrowserSettings(group_id="g", delete_after=True)
    with browser.browser_session(okbrowser=settings, window_id="OLD", on_window=seen.append):
        pass
    assert ("start", "OLD") in log
    assert not any(e[0] in ("create", "delete") for e in log)
    assert ("close", "OLD") in log and seen == ["OLD"]


def test_session_falls_back_to_create_when_bound_window_is_gone(monkeypatch):
    log, seen = [], []
    _patch_windows(monkeypatch, log, dead={"OLD"})
    settings = OkBrowserSettings(group_id="g", delete_after=False)
    with browser.browser_session(proxy="http://h:1", okbrowser=settings, name="a@x.com",
                                 window_id="OLD", on_window=seen.append):
        pass
    assert log.index(("start", "OLD")) < log.index(("create", "a@x.com", "g", "http://h:1"))
    assert ("start", "W1") in log and not any(e[0] == "delete" for e in log)
    assert seen == ["W1"]


def test_session_reports_cleared_window_after_delete(monkeypatch):
    log, seen = [], []
    _patch_windows(monkeypatch, log)
    with browser.browser_session(okbrowser=OkBrowserSettings(group_id="g"), on_window=seen.append):
        pass
    assert ("delete", "W1") in log and seen == ["W1", ""]


def test_window_tracker_and_flow_attach_window_id():
    from claude_register.flow import WindowTracker

    t = WindowTracker("OLD")
    assert not t.reported and t.window_id == "OLD" and not t.kept
    t("W2")
    assert t.kept
    t("")
    assert t.reported and not t.kept


class _Okb:
    """面板接口用的假 OKBrowser 客户端，记录调用。"""

    calls: list = []
    fail_close = False

    def __init__(self, api_url):
        self.calls.append(("init", api_url))

    def create(self, *, name, group_id, proxy):
        self.calls.append(("create", name, group_id, proxy))
        return "NEW1"

    def update_proxy(self, wid, proxy=None):
        self.calls.append(("update_proxy", wid, proxy))

    def start(self, wid):
        self.calls.append(("start", wid))
        return f"ws://{wid}"

    def close(self, wid):
        self.calls.append(("close", wid))
        if self.fail_close:
            raise OkBrowserError("not open")

    def delete(self, wid):
        self.calls.append(("delete", wid))


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from server import db, okbrowser_ops
    from server.app import create_app

    _Okb.calls = []
    _Okb.fail_close = False
    monkeypatch.setattr(okbrowser, "OkBrowserClient", _Okb)
    injected = []
    monkeypatch.setattr(okbrowser_ops, "inject_cookies",
                        lambda ws, cookies, url: injected.append((ws, cookies, url)))
    save_config(tmp_path / "config.yaml", {"panel_password": "pw", "browser_mode": "okbrowser",
                                           "okbrowser_group_id": "G"})
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                     now_fn=lambda: "2026-11-02T00:00:00Z")
    conn = app.state.cr.conn
    db.upsert_account(conn, "a@x.com", "x.com", "", "", 1, "success",
                      session_key="sk-a", proxy="socks5://u:p@h:1")
    db.upsert_account(conn, "b@x.com", "x.com", "", "", 2, "success")
    db.update_account_fields(conn, "b@x.com", {"okbrowser_window_id": "WB"})
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})
    return c, conn, injected


def test_panel_start_creates_binds_and_injects_cookies(panel):
    from server import db

    c, conn, injected = panel
    r = c.post("/api/accounts/a@x.com/okbrowser/start")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "window_id": "NEW1", "ws": "ws://NEW1", "created": True}
    assert ("create", "a@x.com", "G", "socks5://u:p@h:1") in _Okb.calls
    assert db.get_account(conn, "a@x.com")["okbrowser_window_id"] == "NEW1"
    assert injected[0][1][0]["name"] == "sessionKey" and injected[0][2] == "https://claude.ai/"

    db.update_account_fields(conn, "b@x.com", {"proxy": "http://new-proxy:8080"})
    r = c.post("/api/accounts/b@x.com/okbrowser/start")
    assert r.json()["created"] is False
    assert ("update_proxy", "WB", "http://new-proxy:8080") in _Okb.calls
    assert ("start", "WB") in _Okb.calls
    assert _Okb.calls.index(("update_proxy", "WB", "http://new-proxy:8080")) < _Okb.calls.index(("start", "WB"))
    assert len(injected) == 1
    assert c.post("/api/accounts/a@x.com/okbrowser/start",
                  json={"create_if_missing": False}).status_code == 200


def test_panel_start_without_window_and_no_create_is_502(panel):
    c, _conn, _ = panel
    r = c.post("/api/accounts/a@x.com/okbrowser/start", json={"create_if_missing": False})
    assert r.status_code == 502


def test_scoped_open_does_not_replace_session_in_existing_window(panel, monkeypatch):
    from server import db, okbrowser_ops
    from server.config_store import Config

    _, conn, injected = panel
    db.update_account_fields(conn, "a@x.com", {"okbrowser_window_id": "bound"})
    monkeypatch.setattr(okbrowser, "running_ws", lambda _: "ws://existing")
    row = db.get_account(conn, "a@x.com")
    result = okbrowser_ops.open_window(conn, Config(), row, platform="claude")
    assert result["created"] is False and not injected
    assert ("start", "bound") in _Okb.calls
    assert not any(call[0] == "update_proxy" for call in _Okb.calls)


def test_read_bound_session_reuses_active_profile_and_closes_only_its_own(panel, monkeypatch):
    from server import okbrowser_ops
    from server.config_store import Config

    monkeypatch.setattr(okbrowser, "running_ws", lambda wid: "ws://open" if wid == "active" else "")
    assert okbrowser_ops.read_bound_session(Config(), "active", lambda ws: ws) == "ws://open"
    assert not _Okb.calls
    with pytest.raises(ValueError):
        def bad_reader(ws):
            assert ws == "ws://stopped"
            raise ValueError("no session")

        okbrowser_ops.read_bound_session(Config(), "stopped", bad_reader)
    assert [call[0] for call in _Okb.calls] == ["init", "start", "close"]


def test_panel_close_unbind_and_delete(panel):
    from server import db

    c, conn, _ = panel
    assert c.post("/api/accounts/a@x.com/okbrowser/close").status_code == 400
    assert c.post("/api/accounts/b@x.com/okbrowser/close").status_code == 200
    assert ("close", "WB") in _Okb.calls

    r = c.delete("/api/accounts/b@x.com/okbrowser?unbind_only=true")
    assert r.json()["okbrowser_window_id"] == "" and ("delete", "WB") not in _Okb.calls

    db.update_account_fields(conn, "b@x.com", {"okbrowser_window_id": "WB"})
    _Okb.fail_close = True  # 窗口没开着时 close 报错也要继续删
    r = c.delete("/api/accounts/b@x.com/okbrowser")
    assert r.status_code == 200 and ("delete", "WB") in _Okb.calls
    assert db.get_account(conn, "b@x.com")["okbrowser_window_id"] == ""


def test_delete_account_can_delete_bound_windows(panel):
    from server import db

    c, conn, _ = panel
    r = c.delete("/api/accounts/b@x.com")
    assert r.json()["okbrowser_deleted"] == 0 and ("delete", "WB") not in _Okb.calls

    db.upsert_account(conn, "b@x.com", "x.com", "", "", 2, "success")
    db.update_account_fields(conn, "b@x.com", {"okbrowser_window_id": "WB"})
    r = c.post("/api/accounts/batch-delete",
               json={"emails": ["a@x.com", "b@x.com"], "delete_okbrowser": True})
    assert r.json() == {"ok": True, "deleted": 2, "okbrowser_deleted": 1}
    assert ("delete", "WB") in _Okb.calls


def test_new_context_creates_fresh_context_for_launched_browser():
    class Launched:
        contexts = []

        def new_context(self, **kw):
            return ("NEW", kw)

    assert browser.new_context(Launched()) == ("NEW", {"no_viewport": True})


def test_config_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    assert load_config(p).browser_mode == "chromium"
    save_config(p, {"browser_mode": "okbrowser", "okbrowser_group_id": " 42 ",
                    "okbrowser_api_url": "", "okbrowser_delete_after": False})
    cfg = load_config(p)
    assert cfg.browser_mode == "okbrowser"
    assert cfg.okbrowser_group_id == "42"
    assert cfg.okbrowser_api_url == "http://localhost:30396"
    assert cfg.okbrowser_delete_after is False
    d = to_dict(cfg)
    assert d["browser_mode"] == "okbrowser" and d["okbrowser_group_id"] == "42"
    with pytest.raises(ValueError):
        save_config(p, {"browser_mode": "firefox"})


def test_flow_okbrowser_settings_from_config():
    from claude_register import flow
    from server.config_store import Config

    assert flow._okbrowser_settings(Config()) is None
    assert flow._okbrowser_settings(Config(browser_mode="okbrowser")).group_id == ""
    s = flow._okbrowser_settings(Config(browser_mode="okbrowser", okbrowser_group_id=" g "))
    assert s.group_id == "g" and s.delete_after is True
