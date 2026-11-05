"""Outlook OAuth2 + IMAP XOAUTH2 收件客户端。

无需经过第三方接码页（如 2fa.run），直接用账号自带的 client_id + refresh_token
向微软官方 login.microsoftonline.com 换取 access_token，再经 outlook.office365.com:993
XOAUTH2 读取 INBOX 与 Junk 中的 Claude 登录魔术链接或验证码。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import email as stdlib_email
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
import imaplib
import os
import re
import time
from typing import Any

import httpx

from claude_register.anymail import (
    AnyMailAccessError,
    extract_code,
    extract_magic_link,
    magic_link_recipient,
)
from claude_register.config import FALLBACK_CODE_REGEX, resolve_code_regex
from claude_register.console import log

OUTLOOK_MAIL_URL = "outlook"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
IMAP_FOLDERS = ("INBOX", "Junk")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
EMAIL_PART_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass(frozen=True)
class OutlookCredentials:
    email: str
    password: str
    client_id: str
    refresh_token: str

    @property
    def mail_key(self) -> str:
        return format_outlook_mail_key(self.client_id, self.refresh_token)


def format_outlook_mail_key(client_id: str, refresh_token: str) -> str:
    return f"{client_id.strip()}----{refresh_token.strip()}"


def _is_refresh_token_candidate(part: str) -> bool:
    if not part or "@" in part or UUID_RE.fullmatch(part) or part.startswith("sk-ant-"):
        return False
    if part.startswith(("M.", "0.", "1.")):
        return True
    return len(part) >= 40


def _split_parts(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    if "----" in text:
        return [p.strip() for p in text.split("----") if p.strip()]
    return [p.strip() for p in re.split(r"[\s,|;]+", text) if p.strip()]


def parse_outlook_line(raw: str) -> OutlookCredentials | None:
    """解析整行 Outlook 账号文本。

    兼容常见批量格式：
      邮箱----密码----client_id----refresh_token[----辅助邮箱----辅助密码]
      邮箱----密码----refresh_token----client_id[----...]
      邮箱----client_id----refresh_token
    """
    parts = _split_parts(raw)
    if len(parts) < 3:
        return None

    email_idx: int | None = None
    email_val = ""
    for i, p in enumerate(parts):
        if EMAIL_PART_RE.fullmatch(p):
            email_idx = i
            email_val = p.lower()
            break
    if email_idx is None:
        return None

    client_idx: int | None = None
    client_id = ""
    for i, p in enumerate(parts):
        if i == email_idx:
            continue
        if UUID_RE.fullmatch(p):
            client_idx = i
            client_id = p
            break
    if client_idx is None:
        return None

    rt_idx: int | None = None
    refresh_token = ""
    for i, p in enumerate(parts):
        if i in (email_idx, client_idx):
            continue
        if _is_refresh_token_candidate(p):
            rt_idx = i
            refresh_token = p
            break
    if rt_idx is None:
        return None

    password = ""
    first_cred_idx = min(client_idx, rt_idx)
    for i in range(email_idx + 1, first_cred_idx):
        cand = parts[i]
        if cand and not EMAIL_PART_RE.fullmatch(cand):
            password = cand
            break

    return OutlookCredentials(
        email=email_val,
        password=password,
        client_id=client_id,
        refresh_token=refresh_token,
    )


def parse_outlook_mail_key(mail_key: str | None, mail_base_url: str | None = "") -> tuple[str, str] | None:
    """从 mail_key（或完整账号串）解析出 (client_id, refresh_token)。"""
    raw = (mail_key or "").strip()
    if not raw:
        return None

    full = parse_outlook_line(raw)
    if full is not None:
        return full.client_id, full.refresh_token

    parts = _split_parts(raw)
    client_id = ""
    refresh_token = ""
    for p in parts:
        if not client_id and UUID_RE.fullmatch(p):
            client_id = p
        elif not refresh_token and _is_refresh_token_candidate(p):
            refresh_token = p

    if not client_id and mail_base_url:
        url_clean = mail_base_url.strip().removeprefix("outlook://").strip("/")
        if UUID_RE.fullmatch(url_clean):
            client_id = url_clean

    if client_id and refresh_token:
        return client_id, refresh_token
    return None


def is_outlook_credentials(mail_base_url: str | None, mail_key: str | None) -> bool:
    return parse_outlook_mail_key(mail_key, mail_base_url) is not None


class OutlookAccessError(AnyMailAccessError):
    """Outlook OAuth2 refresh_token 失效或无权访问。"""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        RuntimeError.__init__(
            self,
            f"Outlook 授权失败 {status_code}: {detail}\n"
            "请确认 client_id 与 refresh_token 未过期或未被撤销。",
        )


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    out: list[str] = []
    for chunk, enc in decode_header(value):
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return "".join(out)


def _extract_bodies(msg: Message) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment":
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except LookupError:
            decoded = payload.decode("utf-8", errors="replace")
        if ctype == "text/plain":
            text_parts.append(decoded)
        else:
            html_parts.append(decoded)
    return "\n".join(text_parts), "\n".join(html_parts)


def _parse_iso_utc(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_msg_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OutlookClient:
    """对接微软官方 OAuth2 + IMAP XOAUTH2 的收件客户端。"""

    def __init__(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        code_regex: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.email = email.strip().lower()
        self.client_id = client_id.strip()
        self.refresh_token = refresh_token.strip()
        self.code_regex = (code_regex or "").strip()
        self.timeout = timeout
        self.base_url = OUTLOOK_MAIL_URL
        self.domain = self.email.partition("@")[2]
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

        if "@" not in self.email:
            raise ValueError(f"Outlook 邮箱格式无效：{email}")
        if not self.client_id or not self.refresh_token:
            raise ValueError("缺少 Outlook client_id 或 refresh_token")

    @property
    def mail_key(self) -> str:
        return format_outlook_mail_key(self.client_id, self.refresh_token)

    @classmethod
    def from_credentials(
        cls,
        *,
        email: str,
        mail_key: str,
        mail_base_url: str = "",
        code_regex: str | None = None,
        timeout: float = 30.0,
    ) -> OutlookClient:
        parsed = parse_outlook_mail_key(mail_key, mail_base_url)
        if parsed is None:
            raise ValueError("Outlook 收件凭据格式不正确，需为 client_id----refresh_token")
        client_id, refresh_token = parsed
        return cls(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            code_regex=code_regex,
            timeout=timeout,
        )

    def _get_access_token(self) -> str:
        now = time.monotonic()
        if self._access_token and now < self._token_expires_at:
            return self._access_token

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
            if resp.status_code in (400, 401, 403):
                detail = resp.text[:300]
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        err = body.get("error") or ""
                        desc = body.get("error_description") or ""
                        detail = f"{err}: {desc}".strip(": ") or detail
                except Exception:
                    pass
                raise OutlookAccessError(resp.status_code, detail)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{resp.status_code}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
            data = resp.json() if resp.content else {}

        token = str(data.get("access_token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            raise OutlookAccessError(401, "微软 OAuth2 未返回 access_token")

        expires_in = 3600
        if isinstance(data, dict):
            try:
                expires_in = int(data.get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires_in = 3600
        self._access_token = token
        self._token_expires_at = now + max(60, expires_in - 60)
        return token

    def check_email_access(self, *, to: str = "") -> None:
        """只读探测 refresh_token 能否正常换取 access_token。"""
        self._get_access_token()

    def _open_imap(self, access_token: str) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=self.timeout)
        auth_str = f"user={self.email}\x01auth=Bearer {access_token}\x01\x01"
        imap.authenticate("XOAUTH2", lambda _: auth_str.encode("utf-8"))
        return imap

    def _fetch_latest(
        self,
        *,
        to: str,
        since: str,
        code_regex: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        access_token = self._get_access_token()
        since_dt = _parse_iso_utc(since)
        # RFC 2822 邮件 Date 头精度到秒，且邮件服务器可能有轻微时钟偏差，留 60s 余量
        cutoff_dt = (since_dt - timedelta(seconds=60)) if since_dt else None

        try:
            imap = self._open_imap(access_token)
        except imaplib.IMAP4.error as exc:
            # token 可能刚失效，清缓存后当作连接错误交由上层退避重试一次
            self._access_token = ""
            self._token_expires_at = 0.0
            raise httpx.ConnectError(f"IMAP 认证或连接失败: {exc}") from exc
        except OSError as exc:
            raise httpx.ConnectError(f"IMAP 网络连接失败: {exc}") from exc

        results: list[dict[str, Any]] = []
        try:
            for folder in IMAP_FOLDERS:
                try:
                    typ, count_data = imap.select(folder, readonly=True)
                except Exception:
                    continue
                if typ != "OK" or not count_data or not count_data[0]:
                    continue
                try:
                    total = int(count_data[0])
                except (TypeError, ValueError):
                    continue
                if total <= 0:
                    continue

                start = max(1, total - max(1, limit) + 1)
                typ, fetched = imap.fetch(f"{start}:{total}", "(RFC822)")
                if typ != "OK" or not fetched:
                    continue

                for item in reversed(fetched):
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    raw_bytes = item[1]
                    if not isinstance(raw_bytes, (bytes, bytearray)):
                        continue
                    msg = stdlib_email.message_from_bytes(raw_bytes)
                    msg_dt = _parse_msg_date(msg.get("Date"))
                    if cutoff_dt and msg_dt and msg_dt < cutoff_dt:
                        continue
                    subject = _decode_mime_header(msg.get("Subject"))
                    text_body, html_body = _extract_bodies(msg)
                    entry: dict[str, Any] = {
                        "subject": subject,
                        "text_body": text_body,
                        "html_body": html_body,
                        "from": _decode_mime_header(msg.get("From")),
                        "date": msg_dt.isoformat().replace("+00:00", "Z") if msg_dt else "",
                        "folder": folder,
                    }
                    if code_regex:
                        code = extract_code(entry, code_regex)
                        if code:
                            entry["code"] = code
                    results.append(entry)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise httpx.ReadError(f"IMAP 读取邮件失败: {exc}") from exc
        finally:
            try:
                imap.logout()
            except Exception:
                pass

        return results

    def list_messages(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """INBOX 与 Junk 各取最近 limit 封，合并后按时间倒序返回最近 limit 封。"""
        items = self._fetch_latest(to=self.email, since="", limit=limit)
        items.sort(key=lambda m: m.get("date") or "", reverse=True)
        return items[:limit]

    def poll_code(
        self,
        *,
        to: str,
        since: str,
        code_regex: str | None = None,
        fallback_regex: str | None = None,
        timeout: float = 120.0,
        interval: float = 3.0,
        exclude: Collection[str] = (),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str | None:
        primary = (
            code_regex
            or self.code_regex
            or resolve_code_regex(os.getenv("ANYMAIL_CODE_REGEX"))
        )
        fallback = fallback_regex or FALLBACK_CODE_REGEX
        deadline = monotonic() + timeout
        backoff = 1.0

        log(f"开始通过 Outlook 接码：{to}（超时 {timeout:.0f}s，每 {interval:.0f}s 一次）")
        while monotonic() < deadline:
            try:
                emails = self._fetch_latest(to=to, since=since, code_regex=primary)
            except httpx.HTTPError as exc:
                log(f"Outlook 接码请求失败（{exc}），{backoff:.0f}s 后重试。")
                sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            backoff = 1.0

            for mail_item in emails:
                code = mail_item.get("code")
                if code and str(code) not in exclude:
                    return str(code)
            for mail_item in emails:
                code = extract_code(mail_item, fallback)
                if code and code not in exclude:
                    log("主正则未命中，已用兜底正则提取到验证码。")
                    return code

            sleep(interval)

        return None

    def poll_magic_link(
        self,
        *,
        to: str,
        since: str,
        timeout: float = 120.0,
        interval: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> str | None:
        target = to.strip().lower()
        deadline = monotonic() + timeout
        backoff = 1.0

        log(f"开始通过 Outlook 等待登录链接：{to}（超时 {timeout:.0f}s，每 {interval:.0f}s 一次）")
        while monotonic() < deadline:
            try:
                emails = self._fetch_latest(to=to, since=since, code_regex="")
            except httpx.HTTPError as exc:
                log(f"Outlook 查询邮件失败（{exc}），{backoff:.0f}s 后重试。")
                sleep(backoff)
                backoff = min(backoff * 2, 4.0)
                continue
            backoff = 1.0

            for mail_item in emails:
                link = extract_magic_link(mail_item)
                if not link:
                    continue
                who = magic_link_recipient(link)
                if who is None:
                    log("跳过收件人无法解析的链接（base64 尾巴解不出邮箱）。")
                    continue
                if who != target:
                    log(f"跳过收件人不匹配的链接（{who}）。")
                    continue
                return link

            sleep(interval)

        return None
