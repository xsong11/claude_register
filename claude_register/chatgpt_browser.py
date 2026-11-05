"""ChatGPT（chatgpt.com / auth.openai.com）注册页面交互与凭证提取。

浏览器底座（代理、OKBrowser、Cloudflare 勾选、截图、手机号检测）沿用 browser.py；
这里只管 ChatGPT 的页面：入口 → 邮箱 → 密码 → 邮箱验证码 → 姓名/生日 → 主界面，
最后从 Cookie 与 /api/auth/session 取 sessionToken / accessToken。
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from playwright.sync_api import Page

from claude_register.browser import (
    PhoneVerificationRequired,
    _TURNSTILE_FIRST_CLICK_MS,
    _TURNSTILE_RETRY_MS,
    click_turnstile,
    cloudflare_challenge_visible,
    phone_verify_visible,
    screenshot,
)
from claude_register.console import log

CHATGPT_URL = "https://chatgpt.com/"
LOGIN_URL = "https://chatgpt.com/auth/login"
SESSION_URL = "https://chatgpt.com/api/auth/session"
SECURITY_URL = "https://chatgpt.com/#settings/Security"
# next-auth 的会话 Cookie；值太长时会被拆成 .0 / .1 … 多段
SESSION_COOKIE_NAMES = ("__Secure-next-auth.session-token", "next-auth.session-token")


class ChatGPTSignupError(RuntimeError):
    """注册流程无法继续（收不到验证码、密码被拒等）。"""


class AccountDeactivated(ChatGPTSignupError):
    """OpenAI 登录返回 account_deactivated：账号已被封禁或删除。"""


def _visible(loc) -> bool:
    try:
        return loc.count() >= 1 and loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _first_visible(page: Page, builders):
    for build in builders:
        try:
            loc = build()
        except Exception:  # noqa: BLE001
            continue
        if _visible(loc):
            return loc.first
    return None


def _type(loc, value: str) -> None:
    """拟人输入：先清空再逐字敲，React 受控输入框才会更新状态。

    页面已跳走时元素消失，默认 30s 等待会让每一步都白等，所以都给短超时。
    """
    try:
        loc.click(timeout=5_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        loc.fill("", timeout=3_000)
    except Exception:  # noqa: BLE001
        pass
    try:
        loc.press_sequentially(value, delay=random.randint(40, 90), timeout=15_000)
    except Exception:  # noqa: BLE001
        try:
            loc.fill(value, timeout=3_000)
        except Exception:  # noqa: BLE001
            pass


def _input_value(loc) -> str:
    try:
        return loc.input_value(timeout=1_000) or ""
    except Exception:  # noqa: BLE001
        return ""


# ---- 页面识别 ----

def _signup_button(page: Page):
    return _first_visible(page, (
        lambda: page.get_by_test_id("signup-button"),
        lambda: page.get_by_role("button", name="Sign up for free"),
        lambda: page.get_by_role("button", name="Sign up", exact=True),
        lambda: page.get_by_role("link", name="Sign up", exact=True),
        lambda: page.get_by_role("button", name="Create account"),
    ))


def _email_input(page: Page):
    return _first_visible(page, (
        lambda: page.locator("input[name='email']"),
        lambda: page.locator("input[type='email']"),
        lambda: page.get_by_label("Email address"),
    ))


def _password_input(page: Page):
    return _first_visible(page, (
        lambda: page.locator("input[name='new-password']"),
        lambda: page.locator("input[name='password']"),
        lambda: page.locator("input[type='password']"),
    ))


def _code_input(page: Page):
    return _first_visible(page, (
        lambda: page.locator("input[name='code']"),
        lambda: page.locator("input[autocomplete='one-time-code']"),
        lambda: page.get_by_label("Code"),
    ))


def _name_input(page: Page):
    return _first_visible(page, (
        lambda: page.locator("input[name='name']"),
        lambda: page.get_by_label("Full name"),
        lambda: page.get_by_placeholder("Full name"),
    ))


def about_you_visible(page: Page) -> bool:
    if "about-you" in (page.url or ""):
        return True
    return _first_visible(page, (
        lambda: page.get_by_text("Tell us about you", exact=False),
        lambda: page.get_by_text("How old are you", exact=False),
        lambda: page.get_by_text("What's your name", exact=False),
    )) is not None


def chat_home_visible(page: Page) -> bool:
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001
        url = ""
    if "chatgpt.com" not in url or "/auth" in url:
        return False
    if _first_visible(page, (
        lambda: page.locator("#prompt-textarea"),
        lambda: page.locator("[data-testid='composer']"),
        lambda: page.locator(".ProseMirror[contenteditable='true']"),
        lambda: page.get_by_role("textbox", name=re.compile(r"Ask ChatGPT|Message ChatGPT", re.I)),
    )) is not None:
        return True
    # 输入框选择器随改版失效时的兜底：已拿到会话 Cookie 且没有挂着任何注册步骤页
    if about_you_visible(page) or all_set_visible(page):
        return False
    try:
        return bool(session_token_from_cookies(page.context.cookies(CHATGPT_URL)))
    except Exception:  # noqa: BLE001
        return False


def account_deactivated_visible(page: Page) -> bool:
    return _first_visible(page, (
        lambda: page.get_by_text("account_deactivated", exact=False),
        lambda: page.get_by_text(re.compile(r"has been deleted or deactivated", re.I)),
    )) is not None


def password_rejected(page: Page) -> bool:
    return _first_visible(page, (
        lambda: page.get_by_text("Incorrect email address or password", exact=False),
        lambda: page.get_by_text("Wrong email or password", exact=False),
    )) is not None


_CONTINUE_NAMES = ("Continue", "Next", "Agree", "Finish creating account", "Create account")
_WELCOME_NAMES = ("Okay, let\u2019s go", "Okay, let's go", "Let's go", "Get started",
                  "Skip", "Maybe later", "Not now", "Done", "Dismiss")


def click_continue(page: Page) -> bool:
    for name in _CONTINUE_NAMES:
        btn = _first_visible(page, (lambda n=name: page.get_by_role("button", name=n, exact=True),))
        if btn is not None:
            try:
                btn.click(timeout=10_000)
                return True
            except Exception:  # noqa: BLE001
                continue
    btn = _first_visible(page, (lambda: page.locator("button[type='submit']"),))
    if btn is not None:
        try:
            btn.click(timeout=10_000)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def dismiss_welcome(page: Page) -> bool:
    for name in _WELCOME_NAMES:
        btn = _first_visible(page, (lambda n=name: page.get_by_role("button", name=n),))
        if btn is not None:
            try:
                btn.click()
                log(f"已关闭引导弹窗：{name}")
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


# ---- 姓名与生日 ----

def random_birthday(rng: random.Random | None = None) -> date:
    """1990–2001 年之间的合法日期（日取 1–28，避开大小月与闰年）。"""
    r = rng or random
    return date(r.randint(1990, 2001), r.randint(1, 12), r.randint(1, 28))


def _age_on(birthday: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))


def fill_birthday(page: Page, birthday: date) -> bool:
    """兼容三种生日控件：分段 spinbutton（React Aria）、文本 MM/DD/YYYY、<select> 下拉；
    只要年龄的页面填年龄。返回是否填了。"""
    mm, dd, yyyy = f"{birthday.month:02d}", f"{birthday.day:02d}", str(birthday.year)

    segments = page.locator("[role='spinbutton']:visible")
    try:
        seg_count = segments.count()
    except Exception:  # noqa: BLE001
        seg_count = 0
    if seg_count >= 3:
        values = {"month": mm, "day": dd, "year": yyyy}
        order = ("month", "day", "year")
        for i in range(seg_count):
            seg = segments.nth(i)
            try:
                kind = (seg.get_attribute("data-type") or "").lower()
            except Exception:  # noqa: BLE001
                kind = ""
            value = values.get(kind) or (values[order[i]] if i < 3 else None)
            if value is None:
                continue
            try:
                seg.click()
                page.keyboard.type(value, delay=random.randint(40, 90))
            except Exception:  # noqa: BLE001
                return False
        return True

    text_box = _first_visible(page, (
        lambda: page.locator("input[name='birthday']"),
        lambda: page.get_by_label("Birthday"),
        lambda: page.get_by_placeholder("MM/DD/YYYY"),
    ))
    if text_box is not None:
        _type(text_box, f"{mm}/{dd}/{yyyy}")
        return True

    month_sel = _first_visible(page, (lambda: page.locator("select[name*='month' i]"),))
    if month_sel is not None:
        try:
            month_sel.select_option(str(birthday.month))
            page.locator("select[name*='day' i]").first.select_option(str(birthday.day))
            page.locator("select[name*='year' i]").first.select_option(yyyy)
            return True
        except Exception:  # noqa: BLE001
            return False

    age_box = _age_input(page)
    if age_box is not None:
        age = str(_age_on(birthday))
        if _input_value(age_box) != age:
            _type(age_box, age)
        return _input_value(age_box) == age
    return False


def _age_input(page: Page):
    return _first_visible(page, (
        lambda: page.locator("input[name='age']"),
        lambda: page.get_by_label("Age", exact=True),
        lambda: page.get_by_placeholder("Age", exact=True),
        lambda: page.locator("input[type='number']"),
    ))


def fill_about_you(page: Page, *, full_name: str, birthday: date) -> bool:
    name_box = _name_input(page)
    if name_box is not None and _input_value(name_box) != full_name:
        _type(name_box, full_name)
    filled = fill_birthday(page, birthday)
    if not filled:
        log("没找到生日 / 年龄输入框（或年龄未写入），直接尝试继续。")
    page.wait_for_timeout(400)
    return click_continue(page)


def all_set_visible(page: Page) -> bool:
    return _first_visible(page, (
        lambda: page.get_by_text("You\u2019re all set", exact=False),
        lambda: page.get_by_text("You're all set", exact=False),
    )) is not None


# ---- 状态机 ----

@dataclass
class SignupInputs:
    email: str
    password: str
    full_name: str
    birthday: date
    get_code: Callable[[], str | None]


class _Progress:
    def __init__(self):
        self.counts: dict[str, int] = {}

    def bump(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


def _step_email(page, inputs: SignupInputs, progress: _Progress) -> bool:
    box = _email_input(page)
    if box is None or _password_input(page) is not None:
        return False
    if progress.bump("email") > 3:
        raise ChatGPTSignupError("邮箱页提交多次仍未前进")
    log(f"填写邮箱：{inputs.email}")
    _type(box, inputs.email)
    click_continue(page)
    return True


def _step_password(page, inputs: SignupInputs, progress: _Progress) -> bool:
    box = _password_input(page)
    if box is None:
        return False
    if password_rejected(page) or progress.bump("password") > 3:
        screenshot(page, "chatgpt_password_rejected.png")
        raise ChatGPTSignupError("密码被拒绝（该邮箱可能已注册过 ChatGPT 且密码不同）")
    url = page.url or ""
    log("该邮箱已有 ChatGPT 账号，用保存的密码登录。" if "log-in" in url else "设置 ChatGPT 密码。")
    _type(box, inputs.password)
    click_continue(page)
    return True


_CODE_ERROR_RE = re.compile(
    r"\b(incorrect|invalid|expired|wrong)\b.{0,40}\bcode\b|\bcode\b.{0,40}\b(incorrect|invalid|expired)\b",
    re.I,
)
# 同一封验证码邮件最多提交几次；OpenAI 只在点 Resend email 后才会发新邮件
_CODE_SUBMITS = 3
_MAX_CODE_MAILS = 3


def _code_error_text(page) -> str:
    """验证码页上的「码错误 / 已过期」提示；没有时返回空串。"""
    loc = _first_visible(page, (lambda: page.get_by_text(_CODE_ERROR_RE),))
    if loc is None:
        return ""
    try:
        return (loc.inner_text(timeout=1_000) or "").strip() or "验证码错误"
    except Exception:  # noqa: BLE001
        return "验证码错误"


def _resend_code_button(page):
    return _first_visible(page, (
        lambda: page.get_by_role("button", name=re.compile(r"^Resend", re.I)),
        lambda: page.get_by_role("link", name=re.compile(r"^Resend", re.I)),
    ))


def _resend_code(page) -> bool:
    btn = _resend_code_button(page)
    if btn is None:
        raise ChatGPTSignupError("验证码被拒绝，且页面上没有 Resend email 按钮")
    log("验证码被拒绝，点击 Resend email 重新发送验证码。")
    btn.click()
    page.wait_for_timeout(2_000)
    return True


def _resubmit_code(page, box, code: str) -> None:
    if click_turnstile(page):
        log("验证码页有 Cloudflare 人机验证，已自动勾选验证框。")
        page.wait_for_timeout(3_000)
    if _input_value(box) != code:
        _type(box, code)
    if not click_continue(page):
        box.press("Enter")


def _step_code(page, inputs: SignupInputs, progress: _Progress) -> bool:
    box = _code_input(page)
    if box is None:
        return False
    if progress.bump("code") > _MAX_CODE_MAILS:
        screenshot(page, "chatgpt_code_rejected.png")
        raise ChatGPTSignupError("多次重新发送验证码后仍停在验证页")
    log("等待 OpenAI 邮箱验证码…")
    code = inputs.get_code()
    if not code:
        screenshot(page, "chatgpt_no_code.png")
        raise ChatGPTSignupError("未收到 OpenAI 验证码邮件")
    log(f"验证码：{code}")
    _type(box, code)
    click_continue(page)
    for attempt in range(1, _CODE_SUBMITS + 1):
        if _wait_code_page_gone(page):
            return True
        error = _code_error_text(page)
        screenshot(page, f"chatgpt_code_stuck_{progress.counts['code']}_{attempt}.png")
        log(f"验证码提交后仍停在验证页（{error or '页面没有错误提示'}），当前地址：{page.url}")
        if error:
            return _resend_code(page)
        box = _code_input(page)
        if box is None:
            return True
        if attempt < _CODE_SUBMITS:
            log(f"重新提交同一个验证码（第 {attempt + 1} 次）。")
            _resubmit_code(page, box, code)
    raise ChatGPTSignupError(f"验证码提交 {_CODE_SUBMITS} 次后页面仍没有跳转")


def _wait_code_page_gone(page, timeout_ms: int = 15_000, poll_ms: int = 500) -> bool:
    """提交验证码后等页面跳走；超时仍停在验证页、或页面提示验证码错误时返回 False。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _code_input(page) is None:
            return True
        if _code_error_text(page):
            return False
        page.wait_for_timeout(poll_ms)
    return _code_input(page) is None


def _step_about_you(page, inputs: SignupInputs, progress: _Progress) -> bool:
    if not about_you_visible(page):
        return False
    if progress.bump("about") > 3:
        screenshot(page, "chatgpt_about_you_stuck.png")
        raise ChatGPTSignupError("姓名/生日页提交多次仍未前进")
    log(f"填写姓名与生日：{inputs.full_name} / {inputs.birthday.isoformat()}")
    fill_about_you(page, full_name=inputs.full_name, birthday=inputs.birthday)
    return True


def _step_signup_entry(page, inputs: SignupInputs, progress: _Progress) -> bool:
    if "chatgpt.com" not in (page.url or ""):
        return False
    btn = _signup_button(page)
    if btn is None:
        return False
    if progress.bump("entry") > 3:
        raise ChatGPTSignupError("点击注册入口后没有跳转")
    log("点击 Sign up 进入注册页。")
    btn.click()
    return True


def _step_welcome(page, inputs: SignupInputs, progress: _Progress) -> bool:
    return dismiss_welcome(page)


def _step_all_set(page, inputs: SignupInputs, progress: _Progress) -> bool:
    """资料页提交后的 “You're all set” 确认页，只有一个 Continue。"""
    if not all_set_visible(page):
        return False
    if progress.bump("all_set") > 3:
        screenshot(page, "chatgpt_all_set_stuck.png")
        raise ChatGPTSignupError("“You're all set” 页点击 Continue 后没有前进")
    log("注册完成确认页（You're all set），点击 Continue。")
    return click_continue(page)


# 顺序有讲究：验证码页 / 资料页先于邮箱页判断（它们也可能带只读的邮箱框）
SIGNUP_STEPS = (
    _step_code,
    _step_all_set,
    _step_about_you,
    _step_password,
    _step_email,
    _step_signup_entry,
    _step_welcome,
)


def drive_signup(page: Page, inputs: SignupInputs, *, timeout_ms: int = 300_000,
                 steps=SIGNUP_STEPS, poll_ms: int = 1_500) -> bool:
    """循环识别当前页面并推进，直到进入 ChatGPT 主界面（True）或超时（False）。

    要求手机号验证时截图并抛 PhoneVerificationRequired。
    """
    progress = _Progress()
    deadline = time.monotonic() + timeout_ms / 1000
    started = time.monotonic()
    next_click_ms = _TURNSTILE_FIRST_CLICK_MS
    while time.monotonic() < deadline:
        if chat_home_visible(page):
            log("已进入 ChatGPT 主界面。")
            return True
        if phone_verify_visible(page):
            try:
                screenshot(page, "chatgpt_phone_verify.png")
                log(f"当前地址：{page.url}")
            except Exception:  # noqa: BLE001
                pass
            log("OpenAI 要求验证手机号（多半是代理 IP 被风控），请换代理后重试。")
            raise PhoneVerificationRequired("需要手机号验证")
        if account_deactivated_visible(page):
            try:
                screenshot(page, "chatgpt_account_deactivated.png")
            except Exception:  # noqa: BLE001
                pass
            raise AccountDeactivated("OpenAI 提示账号已被封禁或删除（account_deactivated）")
        if cloudflare_challenge_visible(page):
            elapsed = (time.monotonic() - started) * 1000
            if elapsed >= next_click_ms:
                next_click_ms = elapsed + _TURNSTILE_RETRY_MS
                if click_turnstile(page):
                    log("Cloudflare 要求人机验证，已自动勾选验证框。")
            page.wait_for_timeout(poll_ms)
            continue
        acted = False
        for step in steps:
            try:
                if step(page, inputs, progress):
                    acted = True
                    break
            except (ChatGPTSignupError, PhoneVerificationRequired):
                raise
            except Exception as exc:  # noqa: BLE001
                # 多半是操作途中页面跳转导致元素消失；下一轮按新页面重新识别
                log(f"页面操作中断（{str(exc).splitlines()[0]}），重新识别当前页面。")
                acted = True
                break
        page.wait_for_timeout(poll_ms * (2 if acted else 1))
    try:
        screenshot(page, "chatgpt_signup_timeout.png")
        log(f"注册超时，当前地址：{page.url}")
    except Exception:  # noqa: BLE001
        pass
    return False


# ---- 注册后添加密码 ----
# 实测流程：设置 → Security and login → Password 行（未设时按钮文案 Add）
# → auth.openai.com/email-verification 输邮箱验证码
# → auth.openai.com/reset-password/new-password 填 new-password / confirm-password
# → 回到设置页，Password 行变成 Change。

def _password_row(page: Page):
    return _first_visible(page, (
        lambda: page.get_by_role("button", name=re.compile(r"Manage the password", re.I)),
    ))


def _row_has_password(row) -> bool:
    try:
        return re.search(r"\bChange\b", row.inner_text(timeout=2_000) or "") is not None
    except Exception:  # noqa: BLE001
        return False


def _wait_new_password_gone(page, timeout_ms: int = 20_000, poll_ms: int = 500) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if _first_visible(page, (lambda: page.locator("input[name='new-password']"),)) is None:
            return True
        page.wait_for_timeout(poll_ms)
    return False


def set_account_password(page: Page, password: str, get_code: Callable[[], str | None], *,
                         timeout_ms: int = 180_000, poll_ms: int = 1_500) -> bool:
    """给已登录的 ChatGPT 账号添加密码；账号已有密码时直接返回 True。"""
    log("打开 设置 → Security，为账号添加密码。")
    page.goto(SECURITY_URL, wait_until="domcontentloaded", timeout=60_000)
    deadline = time.monotonic() + timeout_ms / 1000
    started = time.monotonic()
    next_click_ms = _TURNSTILE_FIRST_CLICK_MS
    progress = _Progress()
    while time.monotonic() < deadline:
        if cloudflare_challenge_visible(page):
            elapsed = (time.monotonic() - started) * 1000
            if elapsed >= next_click_ms:
                next_click_ms = elapsed + _TURNSTILE_RETRY_MS
                if click_turnstile(page):
                    log("Cloudflare 要求人机验证，已自动勾选验证框。")
            page.wait_for_timeout(poll_ms)
            continue
        url = page.url or ""
        try:
            new_box = _first_visible(page, (lambda: page.locator("input[name='new-password']"),))
            confirm_box = _first_visible(page, (
                lambda: page.locator("input[name='confirm-password']"),))
            code_box = _code_input(page) if "auth.openai.com" in url else None
            row = _password_row(page) if "chatgpt.com" in url else None
            if new_box is not None:
                if progress.bump("new_password") > 2:
                    screenshot(page, "chatgpt_set_password_rejected.png")
                    log("新密码提交后仍停在设置密码页，放弃。")
                    return False
                log("填写新密码。")
                _type(new_box, password)
                if confirm_box is not None:
                    _type(confirm_box, password)
                page.wait_for_timeout(400)
                click_continue(page)
                # 提交后页面要跳回 chatgpt.com；没等它跳完就再填一次会对着正在消失的输入框空等
                if not _wait_new_password_gone(page):
                    log("新密码提交后页面暂未跳转，重新检查。")
            elif code_box is not None:
                if progress.bump("code") > 2:
                    screenshot(page, "chatgpt_set_password_code_rejected.png")
                    log("验证码提交后仍停在验证页，放弃设置密码。")
                    return False
                log("设置密码需要邮箱验证码，等待邮件…")
                code = get_code()
                if not code:
                    screenshot(page, "chatgpt_set_password_no_code.png")
                    log("未收到设置密码的验证码邮件，放弃。")
                    return False
                log(f"验证码：{code}")
                _type(code_box, code)
                click_continue(page)
                _wait_code_page_gone(page)
            elif row is not None:
                if _row_has_password(row):
                    log("账号已设置密码（Security 页 Password 显示 Change）。")
                    return True
                if progress.bump("row") > 2:
                    screenshot(page, "chatgpt_set_password_row_stuck.png")
                    log("点击 Password → Add 后没有跳转，放弃设置密码。")
                    return False
                log("点击 Password → Add。")
                row.click()
            elif "chatgpt.com" in url and progress.counts.get("new_password") \
                    and "#settings" not in url and progress.bump("reopen") <= 2:
                page.goto(SECURITY_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            log(f"页面操作中断（{str(exc).splitlines()[0]}），重新识别当前页面。")
        page.wait_for_timeout(poll_ms * 2)
    row = _password_row(page) if "chatgpt.com" in (page.url or "") else None
    if row is not None and _row_has_password(row):
        log("账号已设置密码（Security 页 Password 显示 Change）。")
        return True
    screenshot(page, "chatgpt_set_password_timeout.png")
    log(f"设置密码超时，当前地址：{page.url}")
    return False


def open_signup(page: Page) -> None:
    log(f"正在打开：{LOGIN_URL}")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    log(f"当前地址：{page.url}")


# ---- 凭证提取 ----

def session_token_from_cookies(cookies) -> str:
    """取 next-auth 会话 Cookie；被拆成 name.0 / name.1 … 时按序拼回。"""
    by_name = {str(c.get("name") or ""): str(c.get("value") or "") for c in cookies or []}
    for base in SESSION_COOKIE_NAMES:
        if by_name.get(base):
            return by_name[base]
        chunks = []
        i = 0
        while f"{base}.{i}" in by_name:
            chunks.append(by_name[f"{base}.{i}"])
            i += 1
        if chunks:
            return "".join(chunks)
    return ""


def parse_auth_session(payload) -> dict:
    """/api/auth/session 的 JSON → 落库字段；没有 accessToken 时返回空 dict。"""
    if not isinstance(payload, dict) or not payload.get("accessToken"):
        return {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    return {
        "chatgpt_access_token": str(payload["accessToken"]),
        "chatgpt_token_expires_at": str(payload.get("expires") or ""),
        "chatgpt_account_id": str(account.get("id") or user.get("id") or ""),
        "chatgpt_session_json": json.dumps(payload, ensure_ascii=False),
        "email": str(user.get("email") or ""),
    }


_FETCH_SESSION_JS = """async () => {
  const r = await fetch('/api/auth/session', {credentials: 'include'});
  return await r.text();
}"""


def fetch_auth_session(page: Page) -> dict:
    """在 chatgpt.com 页面上下文里请求 /api/auth/session，失败返回空 dict。"""
    try:
        if "chatgpt.com" not in (page.url or ""):
            page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60_000)
        text = page.evaluate(_FETCH_SESSION_JS)
        return parse_auth_session(json.loads(text or "{}"))
    except Exception as exc:  # noqa: BLE001
        log(f"读取 /api/auth/session 失败（{exc}）。")
        return {}


def extract_credentials(page: Page, *, timeout_ms: int = 30_000) -> dict:
    """轮询直到拿到 sessionToken 或 accessToken；返回 chatgpt_* 字段 dict（可能为空）。"""
    deadline = time.monotonic() + timeout_ms / 1000
    result: dict = {}
    while True:
        try:
            token = session_token_from_cookies(page.context.cookies(CHATGPT_URL))
        except Exception:  # noqa: BLE001
            token = ""
        if token:
            result["chatgpt_session_token"] = token
        result.update({k: v for k, v in fetch_auth_session(page).items() if v})
        if result.get("chatgpt_access_token") or time.monotonic() >= deadline:
            return result
        page.wait_for_timeout(2_000)
