"""ChatGPT 注册：密码、验证码提取、凭证解析、存活检测、状态机推进、runner 落库。"""
from __future__ import annotations

import json
import string
import time
from datetime import date

import pytest

from claude_register import chatgpt_browser as cb
from claude_register import chatgpt_check, chatgpt_flow
from claude_register.browser import PhoneVerificationRequired
from server import db, runner
from server.config_store import Config


# ---- 密码 ----

def test_generate_password_has_every_class():
    for _ in range(50):
        pw = chatgpt_flow.generate_password()
        assert len(pw) == 16
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert any(c in chatgpt_flow._SYMBOLS for c in pw)
    assert len(chatgpt_flow.generate_password(8)) == chatgpt_flow.PASSWORD_MIN_LEN


def test_pick_password_prefers_saved_then_compliant_account_password():
    assert chatgpt_flow.pick_password("cg-pass", "x" * 20) == ("cg-pass", False)
    assert chatgpt_flow.pick_password("", "y" * 12) == ("y" * 12, False)
    pw, generated = chatgpt_flow.pick_password("", "short")
    assert generated and len(pw) == 16


# ---- 验证码 ----

def test_openai_code_from_subject_and_body():
    assert chatgpt_flow.extract_openai_code({"subject": "Your ChatGPT code is 482913"}) == "482913"
    body = "Enter this temporary verification code to continue:\n\n  730264\n"
    assert chatgpt_flow.extract_openai_code({"subject": "OpenAI", "text_body": body}) == "730264"
    # 同一邮箱里的 Claude 邮件、HTML 颜色值都不能被当成验证码
    assert chatgpt_flow.extract_openai_code(
        {"subject": "Your Claude verification code", "text_body": "code: 111222"}) is None
    assert chatgpt_flow.extract_openai_code(
        {"subject": "Welcome", "html_body": "<td style='color:#262624'>x</td>"}) is None


# ---- 凭证 ----

def test_session_token_from_cookies_plain_and_chunked():
    assert cb.session_token_from_cookies(
        [{"name": "__Secure-next-auth.session-token", "value": "abc"}]) == "abc"
    chunks = [{"name": "__Secure-next-auth.session-token.1", "value": "DEF"},
              {"name": "__Secure-next-auth.session-token.0", "value": "abc"}]
    assert cb.session_token_from_cookies(chunks) == "abcDEF"
    assert cb.session_token_from_cookies([{"name": "other", "value": "x"}]) == ""


def test_parse_auth_session():
    payload = {"accessToken": "eyJ.at", "expires": "2026-12-01T00:00:00.000Z",
               "user": {"id": "user-1", "email": "a@x.com"}, "account": {"id": "acc-9"}}
    parsed = cb.parse_auth_session(payload)
    assert json.loads(parsed.pop("chatgpt_session_json")) == payload
    assert parsed == {
        "chatgpt_access_token": "eyJ.at",
        "chatgpt_token_expires_at": "2026-12-01T00:00:00.000Z",
        "chatgpt_account_id": "acc-9",
        "email": "a@x.com",
    }
    assert cb.parse_auth_session({}) == {}
    assert cb.parse_auth_session({"user": {"id": "u"}}) == {}


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = {"content-type": "application/json", **(headers or {})}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Client:
    def __init__(self, resp, me=None):
        self.resp = resp
        self.me = me or _Resp(200, {"id": "user-1"})
        self.seen = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url, *, headers=None, cookies=None, timeout=None):
        self.seen[url] = {"headers": headers, "cookies": cookies}
        return self.me if url == chatgpt_check.ME_URL else self.resp


@pytest.mark.parametrize("resp,expected", [
    (_Resp(200, {"accessToken": "new-at", "expires": "e", "user": {"id": "u"}}), "alive"),
    (_Resp(200, {}), "dead"),
    (_Resp(401, {"detail": "x"}), "dead"),
    (_Resp(403, None, {"cf-mitigated": "challenge", "content-type": "text/html"}), "blocked"),
    (_Resp(500, {}), "error"),
])
def test_check_chatgpt_session_classifies(resp, expected):
    client = _Client(resp)
    status, _detail, fresh = chatgpt_check.check_chatgpt_session(
        "st", "at", None, client_factory=lambda proxy: client)
    assert status == expected
    session_call = client.seen[cb.SESSION_URL]
    assert session_call["cookies"] == {"__Secure-next-auth.session-token": "st"}
    assert session_call["headers"]["Authorization"] == "Bearer at"
    if expected == "alive":
        assert client.seen[chatgpt_check.ME_URL]["headers"]["Authorization"] == "Bearer new-at"
        assert json.loads(fresh.pop("chatgpt_session_json")) == resp._payload
        assert fresh == {"chatgpt_access_token": "new-at", "chatgpt_token_expires_at": "e",
                         "chatgpt_account_id": "u"}
    else:
        assert chatgpt_check.ME_URL not in client.seen


def test_check_chatgpt_session_detects_deactivated_account():
    """封号后 /api/auth/session 仍签发 accessToken，只有 backend-api 会返回 token_revoked。"""
    session = _Resp(200, {"accessToken": "new-at", "expires": "e", "user": {"id": "u"}})
    me = _Resp(401, {"error": {"message": "Encountered invalidated oauth token",
                               "code": "token_revoked"}, "status": 401})
    status, detail, fresh = chatgpt_check.check_chatgpt_session(
        "st", "at", None, client_factory=lambda proxy: _Client(session, me))
    assert status == "dead" and "token_revoked" in detail and fresh == {}


def test_check_chatgpt_session_keeps_fresh_tokens_when_me_errors():
    session = _Resp(200, {"accessToken": "new-at", "expires": "e", "user": {"id": "u"}})
    status, _detail, fresh = chatgpt_check.check_chatgpt_session(
        "st", "", None, client_factory=lambda proxy: _Client(session, _Resp(502, {})))
    assert status == "error" and fresh["chatgpt_access_token"] == "new-at"


def test_check_chatgpt_session_without_credentials():
    assert chatgpt_check.check_chatgpt_session("", "")[0] == "error"


# ---- 状态机 ----

class _FakePage:
    """按脚本切换「当前页面」；步骤函数只看 page.state。"""

    def __init__(self, url="https://auth.openai.com/create-account"):
        self.url = url
        self.state = "email"
        self.waits = 0

    def wait_for_timeout(self, ms):
        self.waits += 1

    def title(self):
        return "ChatGPT"


def _inputs(get_code=lambda: "123456"):
    return cb.SignupInputs(email="a@x.com", password="P@ssw0rd-123456", full_name="Alex Morgan",
                           birthday=date(1995, 5, 17), get_code=get_code)


def _scripted_steps(log):
    def step(name, nxt):
        def run(page, inputs, progress):
            if page.state != name:
                return False
            log.append((name, inputs.get_code() if name == "code" else None))
            page.state = nxt
            if nxt == "home":
                page.url = "https://chatgpt.com/"
            return True
        return run
    return (step("code", "about"), step("about", "home"), step("password", "code"),
            step("email", "password"))


def test_drive_signup_walks_steps_until_chat_home(monkeypatch):
    page = _FakePage()
    monkeypatch.setattr(cb, "chat_home_visible", lambda p: p.state == "home")
    monkeypatch.setattr(cb, "phone_verify_visible", lambda p: False)
    monkeypatch.setattr(cb, "cloudflare_challenge_visible", lambda p: False)
    log = []
    assert cb.drive_signup(page, _inputs(), steps=_scripted_steps(log), poll_ms=1)
    assert log == [("email", None), ("password", None), ("code", "123456"), ("about", None)]


def test_drive_signup_raises_on_phone(monkeypatch):
    page = _FakePage()
    monkeypatch.setattr(cb, "chat_home_visible", lambda p: False)
    monkeypatch.setattr(cb, "phone_verify_visible", lambda p: True)
    monkeypatch.setattr(cb, "screenshot", lambda *a: None)
    with pytest.raises(PhoneVerificationRequired):
        cb.drive_signup(page, _inputs(), steps=(), poll_ms=1)


def test_drive_signup_survives_step_exception(monkeypatch):
    """资料页填写途中页面跳到 “You're all set”，元素消失抛 Playwright 超时，不应终止整个流程。"""
    page = _FakePage()
    page.state = "about"
    monkeypatch.setattr(cb, "chat_home_visible", lambda p: p.state == "home")
    monkeypatch.setattr(cb, "phone_verify_visible", lambda p: False)
    monkeypatch.setattr(cb, "cloudflare_challenge_visible", lambda p: False)

    def flaky_about(page, inputs, progress):
        if page.state != "about":
            return False
        page.state = "all_set"
        raise TimeoutError("Locator.fill: Timeout 30000ms exceeded.\nCall log: ...")

    def all_set(page, inputs, progress):
        if page.state != "all_set":
            return False
        page.state = "home"
        page.url = "https://chatgpt.com/"
        return True

    assert cb.drive_signup(page, _inputs(), steps=(flaky_about, all_set), poll_ms=1)


def test_all_set_step_clicks_continue(monkeypatch):
    clicked = []
    monkeypatch.setattr(cb, "all_set_visible", lambda p: True)
    monkeypatch.setattr(cb, "click_continue", lambda p: clicked.append(1) or True)
    assert cb._step_all_set(_FakePage(), _inputs(), cb._Progress())
    assert clicked == [1]


def test_all_set_step_ignores_other_pages(monkeypatch):
    monkeypatch.setattr(cb, "all_set_visible", lambda p: False)
    assert not cb._step_all_set(_FakePage(), _inputs(), cb._Progress())


def test_set_password_failure_does_not_break_signup(monkeypatch):
    from claude_register import chatgpt_flow

    def boom(*a, **kw):
        raise RuntimeError("settings page changed")

    monkeypatch.setattr(chatgpt_flow, "set_account_password", boom)
    assert chatgpt_flow._try_set_password(_FakePage(), "P@ssw0rd-123456", lambda: "1") is False


class _PasswordPage(_FakePage):
    def __init__(self):
        super().__init__("https://auth.openai.com/reset-password/new-password")
        self.form = True
        self.leave_in = None

    def goto(self, url, **kw):
        pass

    def locator(self, selector):
        return selector

    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self.leave_in is not None:
            self.leave_in -= 1
            if self.leave_in <= 0:
                self.form = False
                self.url = "https://chatgpt.com/settings/security"


def _password_page(monkeypatch):
    page = _PasswordPage()
    typed = []

    def first_visible(p, builders):
        sel = builders[0]()
        return sel if p.form and "password" in str(sel) else None

    def submit(p):
        p.leave_in = 3
        return True

    monkeypatch.setattr(cb, "_first_visible", first_visible)
    monkeypatch.setattr(cb, "_code_input", lambda p: None)
    monkeypatch.setattr(cb, "_password_row", lambda p: None if p.form else "row")
    monkeypatch.setattr(cb, "_row_has_password", lambda row: True)
    monkeypatch.setattr(cb, "_type", lambda loc, value: typed.append(loc))
    monkeypatch.setattr(cb, "click_continue", submit)
    monkeypatch.setattr(cb, "cloudflare_challenge_visible", lambda p: False)
    monkeypatch.setattr(cb, "screenshot", lambda *a: None)
    return page, typed


def test_set_password_waits_for_form_to_leave_instead_of_refilling(monkeypatch):
    """提交新密码后页面要几秒才跳走：不能对着正在消失的输入框再填一遍。"""
    page, typed = _password_page(monkeypatch)
    assert cb.set_account_password(page, "P@ssw0rd-123456", lambda: "1", poll_ms=1)
    assert typed == ["input[name='new-password']", "input[name='confirm-password']"]


def test_set_password_checks_row_once_more_on_timeout(monkeypatch):
    page, _ = _password_page(monkeypatch)
    page.form = False
    page.url = "https://chatgpt.com/settings/security"
    assert cb.set_account_password(page, "P@ssw0rd-123456", lambda: "1", timeout_ms=0)


def test_code_step_waits_for_code_page_to_leave(monkeypatch):
    """提交后验证码页还要几秒才跳走：必须等它消失，不能下一轮再去等一封新邮件。"""
    boxes = iter([object(), object(), object(), None])
    monkeypatch.setattr(cb, "_code_input", lambda page: next(boxes))
    monkeypatch.setattr(cb, "_type", lambda loc, value: None)
    monkeypatch.setattr(cb, "click_continue", lambda page: True)
    calls = []
    page = _FakePage()
    assert cb._step_code(page, _inputs(get_code=lambda: calls.append(1) or "123456"), cb._Progress())
    assert calls == [1]
    assert page.waits == 2


def _stuck_code_page(monkeypatch, *, leave_after_submits=None, error=""):
    """验证码页：提交 leave_after_submits 次后才跳走（None 表示一直不跳）。"""
    state = {"submits": 0, "resent": 0}
    box = object()

    def code_input(page):
        n = leave_after_submits
        return None if n is not None and state["submits"] >= n else box

    def submit(page):
        state["submits"] += 1
        return True

    class _Btn:
        def click(self):
            state["resent"] += 1

    monkeypatch.setattr(cb, "_code_input", code_input)
    monkeypatch.setattr(cb, "_type", lambda loc, value: None)
    monkeypatch.setattr(cb, "_input_value", lambda loc: "123456")
    monkeypatch.setattr(cb, "click_continue", submit)
    monkeypatch.setattr(cb, "click_turnstile", lambda page: False)
    monkeypatch.setattr(cb, "screenshot", lambda *a: None)
    monkeypatch.setattr(cb, "_code_error_text", lambda page: error)
    monkeypatch.setattr(cb, "_resend_code_button", lambda page: _Btn())
    monkeypatch.setattr(cb, "_wait_code_page_gone", lambda page: code_input(page) is None)
    return state


def test_code_step_resubmits_same_code_when_page_does_not_move(monkeypatch):
    """提交后没跳转且页面无错误：用同一个码重新提交，而不是去等一封不会来的新邮件。"""
    state = _stuck_code_page(monkeypatch, leave_after_submits=2)
    calls = []
    assert cb._step_code(_FakePage(), _inputs(get_code=lambda: calls.append(1) or "123456"),
                         cb._Progress())
    assert calls == [1] and state["submits"] == 2 and state["resent"] == 0


def test_code_step_resends_when_code_rejected(monkeypatch):
    state = _stuck_code_page(monkeypatch, error="Incorrect code")
    calls = []
    assert cb._step_code(_FakePage(), _inputs(get_code=lambda: calls.append(1) or "123456"),
                         cb._Progress())
    assert calls == [1] and state["submits"] == 1 and state["resent"] == 1


def test_code_step_gives_up_after_repeated_submits(monkeypatch):
    state = _stuck_code_page(monkeypatch)
    calls = []
    with pytest.raises(cb.ChatGPTSignupError, match="没有跳转"):
        cb._step_code(_FakePage(), _inputs(get_code=lambda: calls.append(1) or "123456"),
                      cb._Progress())
    assert calls == [1] and state["submits"] == cb._CODE_SUBMITS


def test_code_step_without_mail_raises(monkeypatch):
    monkeypatch.setattr(cb, "_code_input", lambda page: object())
    monkeypatch.setattr(cb, "screenshot", lambda *a: None)
    with pytest.raises(cb.ChatGPTSignupError, match="验证码"):
        cb._step_code(_FakePage(), _inputs(get_code=lambda: None), cb._Progress())


def test_random_birthday_range():
    for _ in range(100):
        d = cb.random_birthday()
        assert 1990 <= d.year <= 2001 and 1 <= d.day <= 28
    assert cb._age_on(date(2000, 6, 1), today=date(2026, 5, 31)) == 25


# ---- runner：同邮箱多平台共用一条记录 ----

def _now():
    return "2026-11-02T00:00:00Z"


def _wait(conn, rid):
    for _ in range(100):
        if db.get_run(conn, rid)["status"] != "running":
            return db.get_run(conn, rid)
        time.sleep(0.05)
    raise AssertionError("run 未结束")


def test_chatgpt_run_reuses_account_and_keeps_claude_fields(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    db.upsert_account(conn, "a@x.com", "x.com", None, "mb", 1, "success",
                      session_key="sk-claude", proxy="socks5://p:1",
                      mail_key="mk", mail_base_url="https://mail")
    db.update_account_fields(conn, "a@x.com", {"okbrowser_window_id": "W9"})
    seen = {}

    def fake_chatgpt(**kw):
        seen.update(kw)
        return {"email": "a@x.com", "chatgpt_password": "Gen-Pass-1234567",
                "chatgpt_session_token": "st", "chatgpt_access_token": "at",
                "chatgpt_token_expires_at": "2026-12-01", "chatgpt_account_id": "acc",
                "password": "Gen-Pass-1234567", "proxy": "socks5://p:1",
                "mail_key": "mk", "mail_base_url": "https://mail",
                "okbrowser_window_id": "W9"}

    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), email="a@x.com", platform="chatgpt", group_name="主力",
                  chatgpt_fn=fake_chatgpt)
    assert _wait(conn, rid)["platform"] == "chatgpt"
    assert seen["okbrowser_window_id"] == "W9" and seen["mail_key"] == "mk"
    assert seen["config"].register_proxy == "socks5://p:1"
    row = db.get_account(conn, "a@x.com")
    assert row["session_key"] == "sk-claude" and row["status"] == "success"
    assert row["last_run_id"] == 1
    assert row["chatgpt_status"] == "success" and row["chatgpt_access_token"] == "at"
    assert row["chatgpt_last_run_id"] == rid and row["chatgpt_check_status"] == "alive"
    assert row["password"] == "Gen-Pass-1234567"  # 原密码为空时补上
    assert row["group_name"] == "主力" and row["okbrowser_window_id"] == "W9"


def test_both_runs_claude_then_chatgpt_on_same_mailbox(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    order = []

    def fake_claude(*, email=None, domain=None, config=None, **kw):
        order.append(("claude", email))
        return {"email": "new@x.com", "sessionKey": "sk-new", "proxy": "socks5://q:2",
                "mail_key": "child", "mail_base_url": "https://mail",
                "okbrowser_window_id": "W1"}

    def fake_chatgpt(**kw):
        order.append(("chatgpt", kw["email"], kw["mail_key"], kw["okbrowser_window_id"]))
        return {"email": "new@x.com", "phone_required": True, "chatgpt_password": "p" * 16}

    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), platform="both", flow_fn=fake_claude, chatgpt_fn=fake_chatgpt)
    _wait(conn, rid)
    assert order == [("claude", None), ("chatgpt", "new@x.com", "child", "W1")]
    row = db.get_account(conn, "new@x.com")
    assert row["session_key"] == "sk-new" and row["status"] == "success"
    assert row["chatgpt_status"] == "phone_required" and not row["chatgpt_access_token"]
    assert row["okbrowser_window_id"] == "W1"


def test_chatgpt_failure_keeps_previous_tokens(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    db.upsert_account(conn, "a@x.com", "x.com", None, "", 1, "success", session_key="sk")
    db.update_account_chatgpt(conn, "a@x.com", {"chatgpt_status": "success",
                                                "chatgpt_access_token": "old-at"})
    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), email="a@x.com", platform="chatgpt",
                  chatgpt_fn=lambda **kw: {"email": "a@x.com", "chatgpt_password": "x" * 16})
    _wait(conn, rid)
    row = db.get_account(conn, "a@x.com")
    assert row["chatgpt_status"] == "success" and row["chatgpt_access_token"] == "old-at"


@pytest.mark.parametrize("result,expected", [
    ({"email": "a@x.com", "chatgpt_password": "x" * 16}, "needs_manual"),
    ({"email": "a@x.com", "phone_required": True}, "phone_required"),
    ({"email": "a@x.com", "chatgpt_access_token": "at"}, "success"),
    (None, "needs_manual"),
])
def test_chatgpt_run_status_follows_outcome(tmp_path, result, expected):
    """流程没抛异常不等于注册成功：运行记录的状态要反映是否拿到凭证。"""
    conn = db.init_db(tmp_path / "t.db")
    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), email="a@x.com", platform="chatgpt", chatgpt_fn=lambda **kw: result)
    assert _wait(conn, rid)["status"] == expected


def test_both_run_status_reports_first_unfinished_platform(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), platform="both",
                  flow_fn=lambda **kw: {"email": "n@x.com", "sessionKey": "sk"},
                  chatgpt_fn=lambda **kw: {"email": "n@x.com"})
    assert _wait(conn, rid)["status"] == "needs_manual"


def test_chatgpt_only_new_mailbox_creates_account_without_claude_status(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    r = runner.Runner(conn, tmp_path, _now)
    rid = r.start(Config(), platform="chatgpt", chatgpt_fn=lambda **kw: {
        "email": "fresh@x.com", "chatgpt_access_token": "at", "chatgpt_password": "x" * 16,
        "password": "x" * 16, "mail_key": "child", "mail_base_url": "https://mail"})
    _wait(conn, rid)
    row = db.get_account(conn, "fresh@x.com")
    assert row["status"] == "" and not row["session_key"]
    assert row["chatgpt_status"] == "success" and row["mail_key"] == "child"
    assert db.get_run(conn, rid)["email"] == "fresh@x.com"


def test_runner_rejects_unknown_platform(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    with pytest.raises(ValueError):
        runner.Runner(conn, tmp_path, _now).start(Config(), platform="gemini")
