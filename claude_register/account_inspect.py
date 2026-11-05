"""Read-only inspection of Claude and ChatGPT sessions on their own domains."""
from __future__ import annotations

import re
from datetime import datetime

from claude_register.browser import normalize_proxy_url
from claude_register.chatgpt_check import check_chatgpt_session
from claude_register.session_check import (
    _HEADERS, ORG_URL, _ImpersonatingClient, _looks_like_org_payload, _looks_like_shield,
)
from claude_register.subscription import expiry_date

BASE = "https://claude.ai"
PORTAL_QUERY = "statsig_hashing_algorithm=djb2&growthbook_format=sdk&cache_bust=1&include_system_prompts=false"
SUBSCRIPTION_STATES = {
    "not_required", "not_started", "pending", "completed", "verified", "approved", "failed",
    "declined", "expired", "needs_review", "in_progress", "requires_retry", "required",
    "exempt", "verification_required", "requires_action", "verification_failed", "in_review",
    "created", "submitted", "marked_for_review", "denied",
}


def _object(value):
    return value if isinstance(value, dict) else {}


def _confirmed_ban(payload):
    error = _object(_object(payload).get("error"))
    return (error.get("code") or error.get("type")) in (
        "account_banned", "account_disabled", "account_suspended", "account_deactivated",
    )


def _plan(org):
    capabilities = org.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, list) else []
    tier = org.get("rate_limit_tier") or org.get("plan_tier") or org.get("billing_tier") or ""
    tier = tier.lower() if isinstance(tier, str) else ""
    if org.get("raven_type") == "enterprise" or "claude_enterprise" in capabilities or "enterprise" in tier:
        return "enterprise"
    if org.get("raven_type") == "team" or "claude_team" in capabilities or "raven" in capabilities or "team" in tier:
        return "team"
    if "20x" in tier or "x20" in tier:
        return "max_20x"
    if "5x" in tier or "x5" in tier:
        return "max_5x"
    if "max" in tier or "claude_max" in capabilities:
        return "max"
    if "pro" in re.split(r"[^a-z0-9]+", tier) or "claude_pro" in capabilities or org.get("is_pro") is True:
        return "pro"
    if tier in ("free", "default", "default_claude_ai", "claude_free") or org.get("is_free") is True:
        return "free"
    return ""


def _matches_org(payload, org_id, expected_email):
    returned = payload.get("resolved_org_uuid") or payload.get("organization_uuid") or payload.get("org_uuid")
    account = _object(payload.get("account"))
    email = account.get("email_address") or account.get("email")
    return (not returned or returned == org_id) and (
        not email or not expected_email or (isinstance(email, str) and email.lower() == expected_email.lower())
    )


def _portal(payload, org_id):
    if "hold_notice" not in payload:
        return "unknown"
    if payload["hold_notice"] is None:
        return "not_flagged"
    hold = _object(payload["hold_notice"])
    hold_id = _object(hold.get("org")).get("uuid")
    if hold_id and hold_id != org_id:
        return "unknown"
    withheld = hold.get("withheld")
    if hold.get("error_code") == "verification_required" or (
        hold_id == org_id and isinstance(withheld, list)
        and any(_object(item).get("check") in ("kyc", "both") for item in withheld)
    ):
        return "required"
    return "unknown"


def _valid_date(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return ""


def inspect_claude(session_key, proxy, *, expected_email="", timeout=10, client_factory=None):
    def result(status, detail):
        return {"status": status, "detail": detail}

    if not session_key:
        return result("error", "缺少 Claude sessionKey")
    try:
        client = (client_factory or (lambda url: _ImpersonatingClient(url, allow_redirects=False)))(
            normalize_proxy_url(proxy),
        )
        with client:
            def get(path, limit=timeout):
                return client.get(path, headers=_HEADERS, cookies={"sessionKey": session_key}, timeout=limit)

            def optional(path, limit=timeout):
                try:
                    resp = get(path, limit)
                    if resp.status_code == 200 and not _looks_like_shield(resp):
                        return _object(resp.json())
                except Exception:  # optional metadata must not replace an authenticated result
                    pass
                return {}

            resp = get(ORG_URL)
            code = resp.status_code
            shield = _looks_like_shield(resp)
            if code in (401, 403) and not shield and _confirmed_ban(resp.json()):
                return result("banned", "平台明确返回账号已停用或封禁")
            if code == 401 and not shield:
                return result("dead", "登录凭据已失效（HTTP 401）")
            if code == 403 or code == 429 or shield and code in (200, 401, 503):
                return result("blocked", f"账号查询受阻（HTTP {code}）")
            if code != 200 or not _looks_like_org_payload(resp):
                return result("error", f"未取得有效的组织信息（HTTP {code}）")
            raw = resp.json()
            orgs = raw.get("organizations") if isinstance(raw, dict) else raw
            bootstrap = optional(f"{BASE}/api/bootstrap")
            account = _object(bootstrap.get("account"))
            if not (account.get("email_address") or account.get("email")):
                info = optional(f"{BASE}/api/account")
                account = {**account, **(_object(info.get("account")) or info)}
            email = account.get("email_address") or account.get("email")
            if isinstance(email, str) and email and expected_email and email.strip().lower() != expected_email.lower():
                return result("error", "凭据对应的邮箱与记录不符")
            if not orgs and not email:
                return result("error", "平台未返回可识别的账号或组织")
            active = (account.get("lastActiveOrgId") or bootstrap.get("active_organization_id")
                      or _object(bootstrap.get("active_organization")).get("uuid")
                      or account.get("active_organization_uuid"))
            org = next((o for o in orgs if active and (o.get("uuid") or o.get("id")) == active), {})
            if not active and len(orgs) == 1:
                org = orgs[0]
            org_id = org.get("uuid") or org.get("id") or ""
            text_fields = {"org_name": "name", "billing_type": "billing_type", "merchant": "merchant_of_record"}
            metadata = {field: org[key][:256] for field, key in text_fields.items() if isinstance(org.get(key), str)}
            capabilities = org.get("capabilities")
            if isinstance(capabilities, list):
                metadata["capabilities"] = [v[:80] for v in capabilities[:20] if isinstance(v, str)]
            if isinstance(email, str) and re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip()):
                metadata["observed_email"] = email.strip()
            out = {**result("alive", "有效"), "plan": _plan(org),
                   "registered_at": _valid_date(account.get("created_at")), "metadata": metadata}
            if isinstance(org_id, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", org_id):
                out["org_uuid"] = org_id
                if out["plan"] != "free":
                    details = optional(f"{BASE}/api/organizations/{org_id}/subscription_details", min(timeout, 8))
                    if "plan_ending_at" in details and _matches_org(details, org_id, expected_email):
                        out["subscription_expires_at"] = expiry_date(details["plan_ending_at"])
                subscription = optional(f"{BASE}/api/organizations/{org_id}/kyc_status")
                status = subscription.get("status")
                if isinstance(status, str) and status in SUBSCRIPTION_STATES and _matches_org(subscription, org_id, expected_email):
                    out["subscription_kyc"] = status
                portal = optional(f"{BASE}/edge-api/bootstrap/{org_id}/app_start?{PORTAL_QUERY}")
                if _matches_org(portal, org_id, expected_email):
                    out["portal_kyc"] = _portal(portal, org_id)
            return out
    except Exception as exc:
        return result("error", f"账号查询失败（{type(exc).__name__}）")


def inspect_account(platform, row):
    email = row["email"]
    if platform == "claude":
        return inspect_claude(row.get("session_key"), row.get("proxy"),
                              expected_email="" if email.endswith("@sk-import.local") else email)
    status, detail, fresh = check_chatgpt_session(
        row.get("chatgpt_session_token") or "", row.get("chatgpt_access_token") or "",
        row.get("proxy"), inspection=True, expected_email=email,
        expected_account_id=row.get("chatgpt_account_id") or "",
    )
    result = {"status": status, "detail": detail, "plan": fresh.pop("workspace_plan", ""), "fresh": fresh}
    if "workspace_subscription_expires_at" in fresh:
        result["subscription_expires_at"] = fresh.pop("workspace_subscription_expires_at")
    return result
