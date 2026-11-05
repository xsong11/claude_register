"""ChatGPT 凭据存活检测：拿 sessionToken Cookie / accessToken 请求 /api/auth/session。

纯函数，不碰数据库/FastAPI。所有异常收敛为 ("error", 原因, {})，绝不上抛。
"""
from __future__ import annotations

from claude_register.browser import mask_proxy, normalize_proxy_url
from claude_register.chatgpt_browser import SESSION_URL, parse_auth_session
from claude_register.session_check import _ImpersonatingClient, _looks_like_shield

ME_URL = "https://chatgpt.com/backend-api/me"

_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _default_client(proxy: str | None) -> _ImpersonatingClient:
    return _ImpersonatingClient(proxy)


def check_chatgpt_session(
    session_token: str,
    access_token: str = "",
    proxy: str | None = None,
    *,
    timeout: float = 15.0,
    client_factory=None,
) -> tuple[str, str, dict]:
    """返回 (状态, 说明, 刷新得到的字段)。

    状态：alive / dead / blocked / error。alive 时第三项含最新的
    chatgpt_access_token / chatgpt_token_expires_at / chatgpt_account_id，调用方落库。
    """
    if not session_token and not access_token:
        return ("error", "无 ChatGPT sessionToken / accessToken", {})
    try:
        proxy_url = normalize_proxy_url(proxy)
    except Exception:  # noqa: BLE001
        return ("error", f"代理无效：{mask_proxy(proxy or '')}", {})
    headers = dict(_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    cookies = {"__Secure-next-auth.session-token": session_token} if session_token else None

    factory = client_factory or _default_client
    try:
        client = factory(proxy_url)
    except Exception as exc:  # noqa: BLE001
        return ("error", f"发起请求失败：{exc}", {})
    try:
        with client:
            resp = client.get(SESSION_URL, headers=headers, cookies=cookies, timeout=timeout)
            status, detail, fresh = classify(resp)
            token = fresh.get("chatgpt_access_token") or access_token
            if status != "alive" or not token:
                return status, detail, fresh
            # /api/auth/session 只解开会话 Cookie，账号被封后照样签发 accessToken；
            # 账号是否还能用要以 backend-api 的结果为准
            me = client.get(ME_URL, headers={**_HEADERS, "Authorization": f"Bearer {token}"},
                            timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return ("error", f"请求失败：{type(exc).__name__}", {})
    return classify_me(me, fresh)


def _error_code(resp) -> str:
    try:
        err = resp.json().get("error")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(err, dict):
        return str(err.get("code") or err.get("message") or "")
    return str(err or "")


def classify_me(resp, fresh: dict) -> tuple[str, str, dict]:
    if resp.status_code in (401, 403):
        if _looks_like_shield(resp):
            return ("blocked", "账号状态查询被 Cloudflare 拦截", fresh)
        code = _error_code(resp)
        return ("dead", f"账号已被封禁或注销（{code or f'HTTP {resp.status_code}'}）", {})
    if resp.status_code != 200:
        return ("error", f"账号状态查询异常（HTTP {resp.status_code}）", fresh)
    return ("alive", "有效", fresh)


def classify(resp) -> tuple[str, str, dict]:
    if resp.status_code in (401, 403):
        if _looks_like_shield(resp):
            return ("blocked", "被 Cloudflare 拦截", {})
        return ("dead", f"会话已失效（HTTP {resp.status_code}）", {})
    if resp.status_code != 200:
        return ("error", f"未知响应（HTTP {resp.status_code}）", {})
    if _looks_like_shield(resp):
        return ("blocked", "被 Cloudflare 拦截", {})
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return ("error", "响应不是 JSON", {})
    fresh = parse_auth_session(payload)
    if not fresh:
        return ("dead", "会话已失效", {})
    fresh.pop("email", None)
    return ("alive", "有效", {k: v for k, v in fresh.items() if v})
