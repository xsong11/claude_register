"""经代理查出口时区：解析各站点格式、socks5→socks5h、全部失败返回 None。"""
from __future__ import annotations

from curl_cffi import requests as cffi_requests

from claude_register import browser

# conftest 的 autouse fixture 会把 browser.lookup_timezone 换成假实现；
# 模块导入发生在 fixture 之前，这里拿到的是真函数。
lookup_timezone = browser.lookup_timezone


class Resp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def _patch(monkeypatch, responses):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs["proxy"]))
        r = responses.get(url)
        if isinstance(r, Exception):
            raise r
        return r or Resp(500, {})

    monkeypatch.setattr(cffi_requests, "get", fake_get)
    return calls


def test_socks5_is_resolved_remotely_and_first_hit_wins(monkeypatch):
    calls = _patch(monkeypatch, {"https://ipinfo.io/json": Resp(200, {"timezone": "Asia/Tokyo"})})
    assert lookup_timezone("socks5://127.0.0.1:4000") == "Asia/Tokyo"
    assert calls == [("https://ipinfo.io/json", "socks5h://127.0.0.1:4000")]


def test_falls_through_failures_and_parses_nested_timezone(monkeypatch):
    _patch(monkeypatch, {
        "https://ipinfo.io/json": OSError("blocked"),
        "https://ipapi.co/json/": Resp(429, {}),
        "https://ipwho.is/": Resp(200, {"timezone": {"id": "America/New_York"}}),
    })
    assert lookup_timezone("http://u:p@h:8080") == "America/New_York"


def test_rejects_garbage_and_returns_none(monkeypatch):
    _patch(monkeypatch, {
        "https://ipinfo.io/json": Resp(200, {"timezone": "x; rm -rf /"}),
        "https://ipapi.co/json/": Resp(200, ValueError("not json")),
    })
    assert lookup_timezone("http://h:8080") is None


def test_collects_readable_failure_reasons(monkeypatch):
    socks_refused = RuntimeError(
        "Failed to perform, curl: (97) cannot complete SOCKS5 connection to ipinfo.io. (2)."
    )
    _patch(monkeypatch, {
        "https://ipinfo.io/json": socks_refused,
        "https://ipapi.co/json/": Resp(429, {}),
    })
    errors = []
    assert lookup_timezone("socks5://u:p@h:1", errors=errors) is None
    assert errors[0].startswith("ipinfo.io: 代理拒绝连接")
    assert errors[1] == "ipapi.co: HTTP 429"
    assert errors[2] == "ipwho.is: HTTP 500"


def test_no_proxy_no_lookup(monkeypatch):
    calls = _patch(monkeypatch, {})
    assert lookup_timezone(None) is None
    assert calls == []


def test_probe_proxy_and_api_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.config_store import save_config

    _patch(monkeypatch, {
        "https://ipinfo.io/json": Resp(200, {
            "ip": "1.2.3.4", "country": "US", "city": "Dallas", "timezone": "America/Chicago",
        }),
    })
    save_config(tmp_path / "config.yaml", {
        "panel_password": "pw",
        "saved_proxies": [{"id": "p1", "name": "美区", "url": "socks5://u:p@h:1080"}],
    })
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml")
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})

    r = c.post("/api/proxies/test", json={"proxy_id": "p1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["ip"] == "1.2.3.4"
    assert body["country"] == "US" and body["city"] == "Dallas"
    assert body["timezone"] == "America/Chicago" and body["latency_ms"] >= 1

    r = c.post("/api/proxies/test", json={"url": "http://h:8080"})
    assert r.status_code == 200 and r.json()["ok"] is True

    _patch(monkeypatch, {})
    r = c.post("/api/proxies/test", json={"url": "http://h:8080"})
    assert r.status_code == 200 and r.json()["ok"] is False and "HTTP 500" in r.json()["detail"]

    assert c.post("/api/proxies/test", json={"url": ""}).status_code == 400
    assert c.post("/api/proxies/test", json={"url": "ftp://h:1"}).status_code == 400
    assert c.post("/api/proxies/test", json={"proxy_id": "missing"}).status_code == 400
