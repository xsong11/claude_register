"""pytest 共享 fixture。"""

from __future__ import annotations

import pytest

from claude_register import anymail

BASE_URL = "https://mail.test"


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """禁止测试读取真实 .env。"""
    monkeypatch.setattr(anymail, "load_dotenv", lambda *a, **k: None)


@pytest.fixture
def client():
    return anymail.AnyMailClient(
        base_url=BASE_URL,
        api_key="ak_test",
        domain="mail.test",
    )


@pytest.fixture(autouse=True)
def _no_timezone_lookup(monkeypatch):
    """出口时区查询会真的联网（经代理），测试里一律视为查不到。"""
    from claude_register import browser

    monkeypatch.setattr(browser, "lookup_timezone", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_real_okbrowser(monkeypatch):
    """开发机上可能开着真的 OKBrowser 客户端：测试里没注入 http 的客户端一律拒绝联网。"""
    from claude_register import okbrowser

    real_init = okbrowser.OkBrowserClient.__init__

    def guarded(self, api_url=okbrowser.DEFAULT_API_URL, *, http=None, **kw):
        if http is None:
            raise RuntimeError("测试不得连接真实的 OKBrowser 本地 API")
        real_init(self, api_url, http=http, **kw)

    monkeypatch.setattr(okbrowser.OkBrowserClient, "__init__", guarded)
    monkeypatch.setattr(okbrowser, "_process_cmdlines", lambda: [])


class FakeChromiumSession:
    def __init__(self, browser="BROWSER"):
        self.browser = browser
        self.closed = False

    def close(self):
        self.closed = True


class FakeVirtualDisplay:
    instances: list = []

    def __init__(self, **kwargs):
        self.name = None
        self.stopped = False
        FakeVirtualDisplay.instances.append(self)

    def start(self):
        self.name = ":150"
        return self

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_chromium(monkeypatch):
    """替换 launch_chromium / VirtualDisplay，记录启动参数；不拉起真实浏览器。"""
    from claude_register import browser

    seen: dict = {}

    def _launch(**kwargs):
        seen.update(kwargs)
        seen["session"] = FakeChromiumSession()
        return seen["session"]

    FakeVirtualDisplay.instances = []
    seen["displays"] = FakeVirtualDisplay.instances
    monkeypatch.setattr(browser, "launch_chromium", _launch)
    monkeypatch.setattr(browser, "VirtualDisplay", FakeVirtualDisplay)
    return seen
