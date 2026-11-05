"""ChatGPT「重新获取凭证」与封禁状态。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_register import chatgpt_browser as cb
from claude_register import okbrowser
from server import chatgpt_resync, db
from server.app import create_app
from server.config_store import Config, save_config

NOW = "2026-11-03T00:00:00Z"


class _Okb:
    def __init__(self, api_url=""):
        self.calls = []

    def start(self, wid):
        self.calls.append(("start", wid))
        return "ws://x"

    def close(self, wid):
        self.calls.append(("close", wid))


def _seed(conn, **fields):
    db.upsert_account(conn, "a@x.com", "x.com", None, "", 1, "", proxy="socks5://p:1")
    db.update_account_fields(conn, "a@x.com", {"okbrowser_window_id": "W1"})
    db.update_account_chatgpt(conn, "a@x.com", {"chatgpt_status": "success",
                                                "chatgpt_access_token": "old-at",
                                                "chatgpt_check_status": "dead", **fields})
    return db.get_account(conn, "a@x.com")


def _alive(st, at, proxy):
    return "alive", "有效", {"chatgpt_access_token": at, "chatgpt_token_expires_at": "e"}


def test_resync_saves_window_session_and_closes_window_it_opened(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    row = _seed(conn)
    okb = _Okb()
    res = chatgpt_resync.resync_from_window(
        conn, Config(), row, NOW, client_factory=lambda url: okb,
        read_session=lambda ws: {"chatgpt_session_token": "new-st",
                                 "chatgpt_access_token": "new-at", "email": "a@x.com"},
        check=_alive)
    assert res["synced"]
    assert okb.calls == [("start", "W1"), ("close", "W1")]
    acct = db.get_account(conn, "a@x.com")
    assert acct["chatgpt_session_token"] == "new-st" and acct["chatgpt_access_token"] == "new-at"
    assert acct["chatgpt_check_status"] == "alive" and acct["chatgpt_checked_at"] == NOW


def test_resync_keeps_window_open_when_it_was_already_running(tmp_path, monkeypatch):
    conn = db.init_db(tmp_path / "t.db")
    row = _seed(conn)
    monkeypatch.setattr(okbrowser, "running_ws", lambda wid: "ws://running")
    okb = _Okb()
    chatgpt_resync.resync_from_window(
        conn, Config(), row, NOW, client_factory=lambda url: okb,
        read_session=lambda ws: {"chatgpt_access_token": "new-at"}, check=_alive)
    assert ("close", "W1") not in okb.calls


@pytest.mark.parametrize("session,check,reason", [
    ({}, _alive, "没有登录"),
    ({"chatgpt_access_token": "at", "email": "b@x.com"}, _alive, "b@x.com"),
    ({"chatgpt_access_token": "at"}, lambda *a: ("dead", "token_revoked", {}), "token_revoked"),
])
def test_resync_does_not_overwrite_when_window_session_unusable(tmp_path, session, check, reason):
    conn = db.init_db(tmp_path / "t.db")
    row = _seed(conn)
    res = chatgpt_resync.resync_from_window(
        conn, Config(), row, NOW, client_factory=lambda url: _Okb(),
        read_session=lambda ws: dict(session), check=check)
    assert not res["synced"] and reason in res["reason"]
    assert db.get_account(conn, "a@x.com")["chatgpt_access_token"] == "old-at"


def _client(tmp_path):
    save_config(tmp_path / "config.yaml", {"panel_password": "pw"})
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml", now_fn=lambda: NOW)
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})
    return app, c


def test_resync_endpoint_falls_back_to_relogin(tmp_path, monkeypatch):
    app, c = _client(tmp_path)
    _seed(app.state.cr.conn)
    monkeypatch.setattr(chatgpt_resync, "resync_from_window",
                        lambda *a, **k: {"synced": False, "reason": "窗口里没有登录 ChatGPT"})
    started = {}

    def fake_start(cfg, *, email, platform, **kw):
        started.update(email=email, platform=platform)
        return 7

    monkeypatch.setattr(app.state.cr.runner, "start", fake_start)
    body = c.post("/api/accounts/a@x.com/chatgpt-resync").json()
    assert body == {"mode": "relogin", "run_id": 7, "reason": "窗口里没有登录 ChatGPT"}
    assert started == {"email": "a@x.com", "platform": "chatgpt"}


def test_check_keeps_banned_status(tmp_path, monkeypatch):
    import server.app as appmod

    app, c = _client(tmp_path)
    _seed(app.state.cr.conn, chatgpt_check_status="banned")
    monkeypatch.setattr(appmod, "check_chatgpt_session",
                        lambda *a: ("dead", "账号已被封禁或注销（token_revoked）", {}))
    assert c.post("/api/accounts/a@x.com/check-chatgpt").json()["status"] == "banned"
    assert db.get_account(app.state.cr.conn, "a@x.com")["chatgpt_check_status"] == "banned"


def test_drive_signup_raises_on_deactivated_account(monkeypatch):
    class _Page:
        url = "https://auth.openai.com/error"

        def wait_for_timeout(self, ms):
            pass

    monkeypatch.setattr(cb, "chat_home_visible", lambda p: False)
    monkeypatch.setattr(cb, "phone_verify_visible", lambda p: False)
    monkeypatch.setattr(cb, "account_deactivated_visible", lambda p: True)
    monkeypatch.setattr(cb, "screenshot", lambda *a: None)
    inputs = cb.SignupInputs(email="a@x.com", password="p" * 16, full_name="A B",
                             birthday=None, get_code=lambda: None)
    with pytest.raises(cb.AccountDeactivated):
        cb.drive_signup(_Page(), inputs, steps=(), poll_ms=1)


def test_runner_marks_banned(tmp_path):
    import time
    from server import runner

    conn = db.init_db(tmp_path / "t.db")
    r = runner.Runner(conn, tmp_path, lambda: NOW)
    rid = r.start(Config(), email="a@x.com", platform="chatgpt",
                  chatgpt_fn=lambda **kw: {"email": "a@x.com", "banned": True})
    for _ in range(100):
        if db.get_run(conn, rid)["status"] != "running":
            break
        time.sleep(0.05)
    assert db.get_run(conn, rid)["status"] == "banned"
    acct = db.get_account(conn, "a@x.com")
    assert acct["chatgpt_status"] == "banned" and acct["chatgpt_check_status"] == "banned"


def test_running_ws_reads_debug_port_of_matching_window(monkeypatch):
    monkeypatch.setattr(okbrowser, "_process_cmdlines", lambda: [
        r'"okb.exe" --type=renderer --user-data-dir=C:\cache\W1 --remote-debugging-port=1',
        r'"okb.exe" --user-data-dir=C:\cache\W2 --remote-debugging-port=2222',
        r'"okb.exe" --user-data-dir=C:\cache\W1 --remote-debugging-port=5952',
    ])
    seen = []

    class _Resp:
        def json(self):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:5952/devtools/browser/x"}

    monkeypatch.setattr(okbrowser.httpx, "get", lambda url, **kw: seen.append(url) or _Resp())
    assert okbrowser.running_ws("W1") == "ws://127.0.0.1:5952/devtools/browser/x"
    assert seen == ["http://127.0.0.1:5952/json/version"]
    assert okbrowser.running_ws("W9") == ""
