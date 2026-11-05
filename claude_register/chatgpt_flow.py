"""ChatGPT 注册编排：邮箱准备 → 代理 → 浏览器注册 → 接码 → 引导页 → 提取凭证落盘。

与 Claude 注册（flow.py）共用邮箱凭据（AnyMail 子 Key / Outlook OAuth2）、代理与
OKBrowser 指纹窗口；同一邮箱的两个平台共用一条账号记录，由 runner 分别落库。
"""
from __future__ import annotations

import json
import random
import secrets
import string
from typing import TYPE_CHECKING

import httpx

from claude_register.anymail import AnyMailAccessError, AnyMailClient, extract_code, load_dotenv
from claude_register.browser import (
    PhoneVerificationRequired,
    _output_dir,
    browser_session,
    default_display_name,
    new_page,
    validate_proxy,
)
from claude_register.chatgpt_browser import (
    AccountDeactivated,
    ChatGPTSignupError,
    SignupInputs,
    drive_signup,
    extract_credentials,
    open_signup,
    random_birthday,
    set_account_password,
)
from claude_register.console import banner, log
from claude_register.flow import WindowTracker, _build_proxy_pool, _okbrowser_settings
from claude_register.mailbox import prepare_mailbox, utc_now_iso
from claude_register.okbrowser import OkBrowserClient
from claude_register.outlook import (
    OUTLOOK_MAIL_URL,
    OutlookClient,
    is_outlook_credentials,
    parse_outlook_line,
)

if TYPE_CHECKING:
    from server.config_store import Config

# OpenAI 验证码邮件：主题「Your ChatGPT code is 123456」，正文「Enter this temporary
# verification code to continue: 123456」。要求带 ChatGPT/OpenAI 字样，同一邮箱里
# 刚收到的 Claude 邮件不会被误当成验证码。
CHATGPT_CODE_REGEX = r"(?:ChatGPT|OpenAI|temporary verification code)[^\d#]{0,80}(\d{6})"
PASSWORD_MIN_LEN = 12
PASSWORD_LEN = 16
_SYMBOLS = "!@#$%^&*-_=+?"
_LAST_NAMES = ("Morgan", "Carter", "Bennett", "Hayes", "Parker", "Brooks", "Reed",
               "Foster", "Collins", "Harper", "Sullivan", "Turner", "Ellis", "Wright")


def generate_password(length: int = PASSWORD_LEN) -> str:
    """高强度随机密码：大小写字母、数字、符号各至少一个。"""
    length = max(length, PASSWORD_MIN_LEN)
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, _SYMBOLS)
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def pick_password(chatgpt_password: str, password: str) -> tuple[str, bool]:
    """返回 (要用的密码, 是否新生成)：已有 ChatGPT 密码 > 合规的账号密码 > 新生成。"""
    if chatgpt_password:
        return chatgpt_password, False
    if password and len(password) >= PASSWORD_MIN_LEN:
        return password, False
    return generate_password(), True


def default_full_name(email: str | None = None, rng: random.Random | None = None) -> str:
    return f"{default_display_name(email)} {(rng or random).choice(_LAST_NAMES)}"


def extract_openai_code(email: dict) -> str | None:
    """从一封邮件（subject / text_body / html_body）里取 OpenAI 的 6 位验证码。"""
    return extract_code(email, CHATGPT_CODE_REGEX)


def _saved_anymail_client(config: "Config | None", email: str, mail_key: str,
                          mail_base_url: str) -> tuple[AnyMailClient, str, str]:
    """用账号保存的 AnyMail 子 Key 接码；子 Key 失效时用主 Key 重新派生。

    返回 (接码客户端, 要回写的 mail_key, 要回写的 mail_base_url)。
    """
    saved = AnyMailClient(base_url=mail_base_url, api_key=mail_key,
                          code_regex=CHATGPT_CODE_REGEX)
    try:
        saved.check_email_access(to=email)
        return saved, mail_key, mail_base_url
    except httpx.HTTPError as exc:
        log(f"AnyMail 子 Key 探测暂时失败（{exc}），继续使用原凭据。")
        return saved, mail_key, mail_base_url
    except (AnyMailAccessError, ValueError) as exc:
        if config is None or not config.anymail_api_key or not config.anymail_base_url:
            raise RuntimeError(f"账号保存的 AnyMail 子 Key 已不可用（{exc}），且未配置主凭据") from exc
        log(f"账号保存的 AnyMail 子 Key 已不可用（{exc}），用主 Key 修复。")
    main = AnyMailClient(base_url=config.anymail_base_url, api_key=config.anymail_api_key,
                         code_regex=CHATGPT_CODE_REGEX)
    child = main.create_child_key(email=email, expires_at=None,
                                  name_prefix="claude-register-chatgpt")
    if child is None:
        log("未能派生新子 Key，改用 AnyMail 主 Key 接码。")
        return main, "", ""
    log(f"已为 {email} 重新派生 AnyMail 子 Key。")
    fresh = AnyMailClient(base_url=main.base_url, api_key=child.plaintext,
                          code_regex=CHATGPT_CODE_REGEX, timeout=main.timeout)
    return fresh, child.plaintext, main.base_url


def run_browser(
    email: str,
    poll,
    *,
    password: str,
    since: str,
    code_timeout: float,
    full_name: str,
    proxy: str | None = None,
    okbrowser=None,
    okbrowser_window_id: str = "",
) -> dict:
    """跑一次 ChatGPT 浏览器注册，返回 {email, chatgpt_*..., phone_required?, okbrowser_window_id?}。"""
    window = WindowTracker(okbrowser_window_id)
    out: dict = {"email": email}
    used_codes: set[str] = set()

    def get_code(after: str = since) -> str | None:
        code = poll.poll_code(to=email, since=after, code_regex=CHATGPT_CODE_REGEX,
                              fallback_regex=CHATGPT_CODE_REGEX, timeout=code_timeout,
                              exclude=used_codes)
        if code:
            used_codes.add(code)
        return code

    inputs = SignupInputs(email=email, password=password, full_name=full_name,
                          birthday=random_birthday(), get_code=get_code)
    with browser_session(proxy=proxy, okbrowser=okbrowser, name=email,
                         window_id=okbrowser_window_id, on_window=window) as browser:
        context, page = new_page(browser)
        try:
            open_signup(page)
            ok = drive_signup(page, inputs)
            if not ok:
                log("未能自动走完 ChatGPT 注册，仍尝试读取已有的登录凭证。")
            creds = extract_credentials(page, timeout_ms=30_000 if ok else 5_000)
            creds.pop("email", None)
            out.update(creds)
            if creds.get("chatgpt_session_token") or creds.get("chatgpt_access_token"):
                out["chatgpt_password_set"] = _try_set_password(
                    page, password, lambda: get_code(utc_now_iso()))
                if out["chatgpt_password_set"]:
                    # 改密码会作废之前签发的 token，必须重新读取当前会话
                    _refresh_credentials(page, out)
        except PhoneVerificationRequired:
            out["phone_required"] = True
            banner("ChatGPT 需要手机号验证，已停止本次注册")
        except AccountDeactivated as exc:
            out["banned"] = True
            banner(f"ChatGPT 账号已被封禁：{exc}")
        except ChatGPTSignupError as exc:
            log(f"ChatGPT 注册未完成：{exc}")
        except Exception as exc:  # noqa: BLE001
            log(f"ChatGPT 注册出错（{exc}），尝试读取已有凭证。")
            try:
                creds = extract_credentials(page, timeout_ms=3_000)
                creds.pop("email", None)
                out.update(creds)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                context.close()
            except Exception as exc:  # noqa: BLE001
                log(f"关闭浏览器上下文出错（{exc}），忽略。")
    if window.reported:
        out["okbrowser_window_id"] = window.window_id
    return out


def _refresh_credentials(page, out: dict) -> None:
    try:
        creds = extract_credentials(page, timeout_ms=15_000)
    except Exception as exc:  # noqa: BLE001
        log(f"设置密码后重新读取 ChatGPT 凭证失败（{exc}），保留之前的凭证。")
        return
    creds.pop("email", None)
    if creds.get("chatgpt_access_token"):
        out.update(creds)
        log("设置密码后已重新读取 ChatGPT 凭证。")


def _try_set_password(page, password: str, get_code) -> bool:
    """设置密码失败不影响注册结果：凭证已经拿到，最多是下次只能用验证码登录。"""
    try:
        return set_account_password(page, password, get_code)
    except Exception as exc:  # noqa: BLE001
        log(f"设置 ChatGPT 密码出错（{exc}），跳过。")
        return False


def _save_result(result: dict) -> None:
    out_dir = _output_dir.get()
    if out_dir is None:
        return
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chatgpt_account.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    except OSError as exc:
        log(f"写入 chatgpt_account.json 失败（{exc}），忽略。")


def run(
    *,
    email: str | None = None,
    domain: str | None = None,
    config: "Config | None" = None,
    password: str = "",
    chatgpt_password: str = "",
    mail_key: str = "",
    mail_base_url: str = "",
    okbrowser_window_id: str = "",
    display_name: str = "",
) -> dict | None:
    """注册 ChatGPT。email 为空时新建 AnyMail 邮箱；否则复用该邮箱已保存的收件凭据。

    返回 dict（含 email 与 chatgpt_* 字段）；拿到 sessionToken 或 accessToken 即成功。
    """
    load_dotenv()
    if email:
        parsed_line = parse_outlook_line(email)
        if parsed_line is not None:
            email = parsed_line.email
            password = password or parsed_line.password
            mail_key = mail_key or parsed_line.mail_key
            mail_base_url = mail_base_url or OUTLOOK_MAIL_URL
        email = email.strip().lower()

    use_outlook = bool(email and is_outlook_credentials(mail_base_url, mail_key))
    use_saved_anymail = bool(email and not use_outlook and mail_key and mail_base_url)

    if config is not None:
        expires_hours = None if config.anymail_expires_hours <= 0 else config.anymail_expires_hours
        code_timeout = config.register_login_timeout
        proxy = config.register_proxy or None
    else:
        expires_hours = None
        code_timeout = 120.0
        proxy = None

    pool = _build_proxy_pool(config) if config is not None else None
    if pool is None:
        validate_proxy(proxy)

    okbrowser = _okbrowser_settings(config)
    if okbrowser is not None:
        OkBrowserClient(okbrowser.api_url).status()
        log(f"浏览器：OKBrowser 指纹窗口（{okbrowser.api_url}）")

    child = None
    main_client: AnyMailClient | None = None
    mailbox_id = ""
    if use_outlook:
        poll = OutlookClient.from_credentials(email=email or "", mail_key=mail_key,
                                              mail_base_url=mail_base_url,
                                              code_regex=CHATGPT_CODE_REGEX)
        mailbox_email = poll.email
        saved_key, saved_url = poll.mail_key, OUTLOOK_MAIL_URL
        log(f"使用 Outlook 邮箱（OAuth2 + IMAP）：{mailbox_email}")
    elif use_saved_anymail:
        mailbox_email = email or ""
        poll, saved_key, saved_url = _saved_anymail_client(config, mailbox_email, mail_key,
                                                           mail_base_url)
        log(f"复用账号的收件凭据：{mailbox_email}")
    else:
        main_client = AnyMailClient(
            base_url=(config.anymail_base_url or None) if config else None,
            api_key=(config.anymail_api_key or None) if config else None,
            domain=((config.anymail_domain if config else "") or domain),
            code_regex=CHATGPT_CODE_REGEX,
        )
        mailbox, _ = prepare_mailbox(main_client, email=email, domain=domain,
                                     expires_hours=expires_hours)
        mailbox_email = mailbox.email
        mailbox_id = str(mailbox.id or "")
        log(f"本次邮箱：{mailbox.email} (id={mailbox.id or 'new'})")
        child = main_client.create_child_key(email=mailbox.email, expires_at=mailbox.expires_at)
        if child:
            poll = AnyMailClient(base_url=main_client.base_url, api_key=child.plaintext,
                                 domain=main_client.domain or None,
                                 code_regex=CHATGPT_CODE_REGEX, timeout=main_client.timeout)
            saved_key, saved_url = child.plaintext, main_client.base_url
            log("已派生本邮箱专用子 key（仅 emails:read），接码轮询改用子 key。")
        else:
            poll = main_client
            saved_key, saved_url = "", ""

    provisioned = None
    if pool is not None:
        provisioned = pool.provision(mailbox_email)
        proxy = provisioned.url
        log(f"已在节点 {provisioned.node_name} 开专属 socks5（inbound={provisioned.inbound_id}）。")

    chosen_password, generated = pick_password(chatgpt_password, password)
    if generated:
        log("已生成 16 位 ChatGPT 密码。")
    full_name = display_name if " " in (display_name or "").strip() else default_full_name(
        mailbox_email)

    def _rollback(reason: str) -> None:
        if child and main_client is not None:
            main_client.delete_key(child.id)
            log(f"{reason}，已撤销本次派生的子 key。")
        if pool is not None and provisioned is not None:
            pool.revoke(provisioned)
            log(f"{reason}，已撤销本次开的专属代理。")

    since = utc_now_iso()
    try:
        result = run_browser(
            mailbox_email, poll, password=chosen_password, since=since,
            code_timeout=code_timeout, full_name=full_name, proxy=proxy,
            okbrowser=okbrowser,
            okbrowser_window_id=okbrowser_window_id if okbrowser is not None else "",
        )
    except BaseException:
        _rollback("ChatGPT 注册中断")
        raise

    success = bool(result.get("chatgpt_session_token") or result.get("chatgpt_access_token"))
    if not success:
        _rollback("ChatGPT 注册未成功")
        if child:
            saved_key, saved_url = "", ""
    result.update({
        "chatgpt_password": chosen_password,
        "password": password or chosen_password,
        "proxy": proxy or "",
        "mail_key": saved_key,
        "mail_base_url": saved_url,
        "mailbox_id": mailbox_id,
        "display_name": display_name or full_name.split(" ", 1)[0],
    })
    _save_result(result)
    banner(f"邮箱：{mailbox_email}")
    if success:
        token = str(result.get("chatgpt_access_token") or result.get("chatgpt_session_token"))
        log(f"ChatGPT 凭证已取得：{token[:16]}…（共 {len(token)} 字符）")
        if result.get("chatgpt_password_set"):
            log("ChatGPT 密码已生效，之后可用 邮箱 + chatgpt_password 登录。")
        else:
            log("ChatGPT 密码未能设置，之后仍需邮箱验证码登录（可重跑补设）。")
    elif result.get("banned"):
        log("未取得 ChatGPT 凭证：该账号已被 OpenAI 封禁。")
    elif result.get("phone_required"):
        log("未取得 ChatGPT 凭证：OpenAI 要求手机号验证，请换代理后重跑。")
    else:
        log("未取得 ChatGPT 凭证（注册可能未完成）。")
    return result
