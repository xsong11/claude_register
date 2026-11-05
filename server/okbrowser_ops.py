"""??????????? OKBrowser ??????????????????????"""
from __future__ import annotations

from claude_register import okbrowser
from claude_register.console import log
from claude_register.okbrowser import OkBrowserError
from server import db


def _client(cfg, client_factory=None):
    return (client_factory or okbrowser.OkBrowserClient)(cfg.okbrowser_api_url)


def account_cookies(row: dict) -> list[dict]:
    """?????????? Cookie?Claude sessionKey ? ChatGPT sessionToken?????????"""
    cookies = []
    if row.get("session_key"):
        cookies.append({"name": "sessionKey", "value": str(row["session_key"]),
                        "domain": ".claude.ai", "path": "/", "secure": True,
                        "httpOnly": True, "sameSite": "Lax"})
    if row.get("chatgpt_session_token"):
        cookies.append({"name": "__Secure-next-auth.session-token",
                        "value": str(row["chatgpt_session_token"]),
                        "domain": ".chatgpt.com", "path": "/", "secure": True,
                        "httpOnly": True, "sameSite": "Lax"})
    return cookies


def _landing_url(row: dict) -> str:
    if row.get("chatgpt_session_token") and not row.get("session_key"):
        return "https://chatgpt.com/"
    return "https://claude.ai/"


def inject_cookies(ws: str, cookies: list[dict], url: str) -> None:
    """? CDP ????????? Cookie ????????? CDP???????"""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(ws)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.add_cookies(cookies)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="commit", timeout=30_000)
    finally:
        playwright.stop()


def open_window(conn, cfg, row: dict, *, create_if_missing: bool = True,
                client_factory=None, inject=None) -> dict:
    """打开账号绑定的窗口；未绑定且允许 create_if_missing 时新建、落库并注入 Cookie。"""
    client = _client(cfg, client_factory)
    window_id = str(row.get("okbrowser_window_id") or "")
    proxy = row.get("proxy") or None
    created = False
    if not window_id:
        if not create_if_missing:
            raise OkBrowserError("该账号没有绑定 OKBrowser 窗口")
        window_id = client.create(name=row["email"], group_id=(cfg.okbrowser_group_id or "").strip(),
                                  proxy=proxy)
        created = True
        db.update_account_fields(conn, row["email"], {"okbrowser_window_id": window_id})
        log(f"已为 {row['email']} 新建并绑定 OKBrowser 窗口：{window_id}")
    else:
        client.update_proxy(window_id, proxy)
    ws = client.start(window_id)
    cookies = account_cookies(row) if created else []
    if cookies:
        try:
            (inject or inject_cookies)(ws, cookies, _landing_url(row))
        except Exception as exc:  # noqa: BLE001
            log(f"? OKBrowser ?????? Cookie ???{exc}????????")
    return {"ok": True, "window_id": window_id, "ws": ws, "created": created}


def close_window(cfg, window_id: str, *, client_factory=None) -> None:
    _client(cfg, client_factory).close(window_id)


def delete_window(cfg, window_id: str, *, client_factory=None) -> None:
    """??????????????????"""
    client = _client(cfg, client_factory)
    try:
        client.close(window_id)
    except OkBrowserError:
        pass
    client.delete(window_id)


def delete_windows_for(conn, cfg, emails, *, client_factory=None) -> tuple[int, list[str]]:
    """??????????????????? (?????, ????)??????"""
    deleted, errors = 0, []
    for email in dict.fromkeys(emails):
        row = db.get_account(conn, email)
        wid = str((row or {}).get("okbrowser_window_id") or "")
        if not wid:
            continue
        try:
            delete_window(cfg, wid, client_factory=client_factory)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            log(f"?? {email} ? OKBrowser ?? {wid} ???{exc}")
            errors.append(f"{email}: {exc}")
    return deleted, errors
