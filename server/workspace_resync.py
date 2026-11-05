"""Recover a platform session from its bound OKBrowser profile without logging in."""
from __future__ import annotations

from claude_register.account_inspect import inspect_account
from claude_register.chatgpt_check import verified_saved_plan
from claude_register.session_check import ORG_URL
from server import chatgpt_resync, okbrowser_ops


class SessionUnavailable(Exception):
    """A user-facing reason to leave the stored session untouched."""


def read_claude_session(ws: str) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ws, timeout=15_000)
        if not browser.contexts:
            return {}
        cookies = browser.contexts[0].cookies(ORG_URL)
        for name in ("sessionKey", "session_key"):
            for cookie in cookies:
                if cookie["name"] == name and cookie["value"]:
                    return {"session_key": cookie["value"]}
    return {}


def recover(cfg, platform, row, *, expected_email):
    """Return verified credentials and inspection result, without writing the database."""
    from playwright.sync_api import Error as PlaywrightError

    window_id = str(row.get("okbrowser_window_id") or "")
    if not window_id:
        raise SessionUnavailable("未绑定 OKBrowser 窗口，请先打开并登录对应账号")
    if not expected_email:
        raise SessionUnavailable("该记录尚无已确认的邮箱，请先确认账号归属")
    reader = read_claude_session if platform == "claude" else chatgpt_resync.read_window_session
    try:
        creds = okbrowser_ops.read_bound_session(cfg, window_id, reader)
    except PlaywrightError:
        raise SessionUnavailable("无法读取 OKBrowser 会话，请检查该窗口能否正常访问平台") from None
    if platform == "chatgpt":
        if not creds.get("chatgpt_access_token"):
            raise SessionUnavailable("窗口里没有可读取的 ChatGPT 登录会话，请先在该窗口登录")
        _verify_email(creds.get("email"), expected_email)
        # Verify the access token paired with the browser response. A second auth/session
        # request could rotate the cookie we just read.
        candidate = {**row, **creds, "email": expected_email, "chatgpt_session_token": ""}
    else:
        if not creds.get("session_key"):
            raise SessionUnavailable("窗口里没有 Claude 登录会话，请先在该窗口登录")
        candidate = {**row, **creds, "email": expected_email}

    result = inspect_account(platform, candidate)
    if result["status"] != "alive":
        raise SessionUnavailable(f"窗口里的会话未通过检测：{result['detail']}")
    if platform == "claude":
        _verify_email(result.get("metadata", {}).get("observed_email"), expected_email)
    elif not result.get("plan"):
        result["plan"] = verified_saved_plan(creds.get("chatgpt_session_json"),
                                             access_token=creds["chatgpt_access_token"],
                                             expected_email=expected_email)
    return creds, {**result, "detail": "已从 OKBrowser 同步并验证会话"}


def _verify_email(found, expected):
    if not isinstance(found, str) or not found.strip():
        raise SessionUnavailable("平台没有返回邮箱，无法确认窗口登录的是该账号")
    if found.strip().casefold() != expected.strip().casefold():
        raise SessionUnavailable("窗口里登录的是另一个账号，请切换后重试")
