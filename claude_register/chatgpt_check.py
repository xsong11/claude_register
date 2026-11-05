"""ChatGPT 凭据存活检测：拿 sessionToken Cookie / accessToken 请求 /api/auth/session。

纯函数，不碰数据库/FastAPI。所有异常收敛为 ("error", 原因, {})，绝不上抛。
"""
from __future__ import annotations

import json

from claude_register.browser import mask_proxy, normalize_proxy_url
from claude_register.chatgpt_browser import SESSION_URL, parse_auth_session
from claude_register.session_check import _ImpersonatingClient, _looks_like_shield
from claude_register.subscription import expiry_date

ME_URL = "https://chatgpt.com/backend-api/me"
ACCOUNTS_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"

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
    inspection: bool = False,
    expected_email: str = "",
    expected_account_id: str = "",
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

    factory = client_factory or (
        (lambda url: _ImpersonatingClient(url, allow_redirects=False)) if inspection else _default_client
    )
    try:
        client = factory(proxy_url)
    except Exception as exc:  # noqa: BLE001
        return ("error", f"发起请求失败：{type(exc).__name__}" if inspection else f"发起请求失败：{exc}", {})
    session_payload = {}
    try:
        with client:
            token, fresh = access_token, {}
            if session_token or not inspection:
                resp = client.get(SESSION_URL, headers=headers, cookies=cookies, timeout=timeout)
                status, detail, fresh = classify(resp)
                if status != "alive":
                    if inspection and _confirmed_ban(resp):
                        return ("banned", "平台明确返回账号已停用或封禁", {})
                    if inspection and resp.status_code in (403, 429):
                        return ("blocked", f"会话查询受阻（HTTP {resp.status_code}）", {})
                    return status, detail, fresh
                if inspection:
                    session_payload = resp.json()
                    if _email_mismatch(_as_dict(session_payload.get("user")), expected_email):
                        return ("error", "凭据对应的邮箱与记录不符", {})
                token = fresh.get("chatgpt_access_token") or access_token
                if not token:
                    return status, detail, fresh
            # /api/auth/session 只解开会话 Cookie，账号被封后照样签发 accessToken；
            # 账号是否还能用要以 backend-api 的结果为准
            auth_headers = {**_HEADERS, "Authorization": f"Bearer {token}"}
            me = client.get(ME_URL, headers=auth_headers, timeout=timeout)
            if inspection:
                status, detail, fresh = _inspect_me(me, fresh, session_payload, expected_email)
                if status == "alive" and fresh.get("workspace_plan") != "free":
                    account_id = fresh.get("chatgpt_account_id") or expected_account_id
                    expires = _subscription_expiry(client, auth_headers, account_id, min(timeout, 8))
                    if expires is not None:
                        fresh["workspace_subscription_expires_at"] = expires
                return status, detail, fresh
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


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _subscription_expiry(client, headers, account_id, timeout):
    """Return None for an inconclusive lookup; an empty date means the platform supplied none."""
    try:
        response = client.get(ACCOUNTS_URL, headers=headers, timeout=timeout)
        if response.status_code != 200 or _looks_like_shield(response):
            return None
        accounts = _as_dict(_as_dict(response.json()).get("accounts"))
    except Exception:  # Subscription metadata is optional once the session is verified.
        return None
    # The default entry is an alias. Use the authenticated account ID, or an
    # unambiguous workspace when checking a token imported without one.
    matches = {}
    for key, entry in accounts.items():
        entry = _as_dict(entry)
        declared_id = _as_dict(entry.get("account")).get("account_id")
        if key != "default" and declared_id and declared_id != key:
            continue
        identity = declared_id or (key if key != "default" else "")
        if isinstance(identity, str) and identity and (not account_id or identity == account_id):
            if key != "default" or identity not in matches:
                matches[identity] = entry
    if len(matches) != 1:
        return None
    entitlement = next(iter(matches.values())).get("entitlement")
    if isinstance(entitlement, dict):
        return expiry_date(entitlement.get("expires_at"))
    return None


def _email_mismatch(value, expected_email) -> bool:
    actual = _as_dict(value).get("email")
    return bool(expected_email and isinstance(actual, str) and actual.strip()
                and actual.strip().casefold() != expected_email.casefold())


def _confirmed_ban(resp) -> bool:
    if resp.status_code not in (401, 403) or _looks_like_shield(resp):
        return False
    payload = _as_dict(resp.json())
    explicit = ("account_deactivated", "account_banned", "account_suspended")
    return any(_as_dict(payload.get(key)).get("code") in explicit for key in ("error", "detail"))


def _inspect_me(resp, fresh, session, expected_email):
    if _confirmed_ban(resp):
        return ("banned", "平台明确返回账号已停用或封禁", {})
    if resp.status_code in (401, 403) and not _looks_like_shield(resp):
        if resp.status_code == 401:
            return ("dead", "登录凭据已失效（HTTP 401）", {})
        return ("blocked", "账号状态查询被拒绝（HTTP 403）", {})
    if resp.status_code == 429:
        return ("blocked", "账号状态查询触发限流（HTTP 429）", {})
    if resp.status_code == 200:
        if _looks_like_shield(resp):
            return ("blocked", "账号状态查询被 Cloudflare 拦截", {})
        me = _as_dict(resp.json())
        if not (me.get("id") or me.get("email") or _as_dict(me.get("user")).get("id")):
            return ("error", "响应不包含可识别的账号信息", {})
        if _email_mismatch(me, expected_email) or _email_mismatch(me.get("user"), expected_email):
            return ("error", "凭据对应的邮箱与记录不符", {})
        plan = reported_plan(me) or reported_plan(session)
        if plan:
            fresh["workspace_plan"] = plan
        return ("alive", "有效", fresh)
    return classify_me(resp, fresh)


def reported_plan(payload: dict) -> str:
    known = {"free", "go", "plus", "pro", "pro_lite", "team", "business", "enterprise", "edu"}
    for obj in (_as_dict(payload.get("account")), payload):
        for key in ("plan_type", "planType"):
            plan = obj.get(key)
            if not isinstance(plan, str):
                continue
            normalized = plan.strip().lower().replace("-", "_")
            if normalized == "prolite":
                normalized = "pro_lite"
            if normalized in known:
                return normalized
    return ""


def verified_saved_plan(raw: str, *, access_token: str, expected_email: str) -> str:
    """Read the plan from a saved session only when it matches the checked credentials."""
    if not raw or not access_token or not expected_email:
        return ""
    try:
        payload = _as_dict(json.loads(raw))
    except (ValueError, TypeError):
        return ""
    if payload.get("accessToken") != access_token:
        return ""
    email = _as_dict(payload.get("user")).get("email")
    if not isinstance(email, str) or email.strip().casefold() != expected_email.strip().casefold():
        return ""
    return reported_plan(payload)


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
