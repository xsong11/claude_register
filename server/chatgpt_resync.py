"""ChatGPT 凭证失效后「重新获取」：从账号绑定的 OKBrowser 窗口读取当前会话。

窗口里的会话可能比库里新（重新登录、改密码后旧 token 会被作废），
能用就直接写回；窗口里也没有可用会话时由调用方改走重新登录。
"""
from __future__ import annotations

from claude_register import okbrowser
from claude_register.chatgpt_browser import CHATGPT_URL, extract_credentials
from claude_register.chatgpt_check import check_chatgpt_session
from claude_register.console import log
from server import db


def read_window_session(ws: str) -> dict:
    """经 CDP 在窗口里新开一个标签页读取 chatgpt.com 会话；只断开连接，不关窗口。"""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(ws)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60_000)
            return extract_credentials(page, timeout_ms=10_000)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        playwright.stop()


def resync_from_window(conn, cfg, row: dict, now: str, *, client_factory=None,
                       read_session=None, check=None) -> dict:
    """返回 {"synced": bool, "reason": str, ...}；OKBrowser 不可用时抛 OkBrowserError。"""
    email = str(row["email"])
    window_id = str(row.get("okbrowser_window_id") or "")
    if not window_id:
        return {"synced": False, "reason": "未绑定 OKBrowser 窗口"}
    client = (client_factory or okbrowser.OkBrowserClient)(cfg.okbrowser_api_url)
    was_running = bool(okbrowser.running_ws(window_id))
    if not was_running and hasattr(client, "update_proxy"):
        client.update_proxy(window_id, row.get("proxy") or None)
    ws = client.start(window_id)
    try:
        creds = (read_session or read_window_session)(ws)
    finally:
        if not was_running:
            try:
                client.close(window_id)
            except Exception as exc:  # noqa: BLE001
                log(f"关闭 OKBrowser 窗口 {window_id} 失败（{exc}），忽略。")
    found = str(creds.pop("email", "") or "").lower()
    if not creds.get("chatgpt_access_token"):
        return {"synced": False, "reason": "窗口里没有登录 ChatGPT"}
    if found and found != email.lower():
        return {"synced": False, "reason": f"窗口里登录的是 {found}，不是该账号"}
    status, detail, fresh = (check or check_chatgpt_session)(
        creds.get("chatgpt_session_token") or "", creds["chatgpt_access_token"],
        row.get("proxy") or "")
    if status != "alive":
        return {"synced": False, "reason": f"窗口里的会话也不可用：{detail}"}
    db.update_account_chatgpt(conn, email, {
        **creds, **fresh, "chatgpt_status": "success",
        "chatgpt_check_status": "alive", "chatgpt_checked_at": now,
    })
    log(f"已从 OKBrowser 窗口 {window_id} 同步 {email} 的 ChatGPT 凭证。")
    return {"synced": True, "reason": "", "window_id": window_id}
