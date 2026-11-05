"""面板「查询邮件」：按账号保存的收件凭据读取最近的邮件，并提取验证码 / 登录链接。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from claude_register.anymail import (
    AnyMailAccessError,
    AnyMailClient,
    extract_code,
    extract_magic_link,
    html_to_text,
)
from claude_register.chatgpt_flow import CHATGPT_CODE_REGEX
from claude_register.config import DEFAULT_CODE_REGEX
from claude_register.outlook import OutlookAccessError, OutlookClient, is_outlook_credentials

MAX_LIMIT = 50
BODY_MAX_CHARS = 5_000
# AnyMail 的 /api/emails/latest 必须带 since，查看历史邮件时往回看这么多天
ANYMAIL_LOOKBACK_DAYS = 30


class MailViewError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _first(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("address") or value.get("email") or value.get("name")
        if value:
            return str(value)
    return ""


def summarize(item: dict[str, Any]) -> dict[str, Any]:
    html = str(item.get("html_body") or "")
    text = str(item.get("text_body") or "").strip() or (html_to_text(html) if html else "")
    code = (
        str(item.get("code") or "")
        or extract_code(item, CHATGPT_CODE_REGEX)
        or extract_code(item, DEFAULT_CODE_REGEX)
        or ""
    )
    return {
        "subject": str(item.get("subject") or ""),
        "from": _first(item, "from", "from_address", "sender"),
        "date": _first(item, "date", "received_at", "created_at"),
        "folder": str(item.get("folder") or ""),
        "code": code,
        "link": extract_magic_link(item) or "",
        "text": text[:BODY_MAX_CHARS],
        "truncated": len(text) > BODY_MAX_CHARS,
    }


def _fetch_raw(row: dict, cfg, limit: int) -> tuple[str, list[dict]]:
    email = str(row.get("email") or "")
    mail_key = str(row.get("mail_key") or "")
    mail_url = str(row.get("mail_base_url") or "")
    if is_outlook_credentials(mail_url, mail_key):
        client = OutlookClient.from_credentials(email=email, mail_key=mail_key,
                                                mail_base_url=mail_url, timeout=20.0)
        return "outlook", client.list_messages(limit=limit)
    if mail_key and mail_url:
        api_key, base_url = mail_key, mail_url
    elif cfg.anymail_api_key and cfg.anymail_base_url:
        api_key, base_url = cfg.anymail_api_key, cfg.anymail_base_url
    else:
        raise MailViewError(400, "该账号没有收件凭据，且未配置 AnyMail 主凭据")
    client = AnyMailClient(base_url=base_url, api_key=api_key, timeout=20.0)
    since = (datetime.now(timezone.utc) - timedelta(days=ANYMAIL_LOOKBACK_DAYS))
    items = client._fetch_latest(to=email, since=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 code_regex="", limit=limit)
    return "anymail", items


def list_account_mails(row: dict, cfg, limit: int = 20) -> dict[str, Any]:
    """返回 {"source": "outlook" | "anymail", "mails": [...]}；凭据或网络问题抛 MailViewError。"""
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        source, items = _fetch_raw(row, cfg, limit)
    except MailViewError:
        raise
    except (OutlookAccessError, AnyMailAccessError) as exc:
        raise MailViewError(422, f"收件凭据不可用：{exc}") from exc
    except ValueError as exc:
        raise MailViewError(400, f"收件凭据格式不正确：{exc}") from exc
    except (httpx.HTTPError, RuntimeError, OSError) as exc:
        raise MailViewError(502, f"读取邮件失败：{exc}") from exc
    return {"source": source, "mails": [summarize(i) for i in items if isinstance(i, dict)]}
