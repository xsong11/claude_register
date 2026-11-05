"""Outlook OAuth2 + IMAP XOAUTH2 解析、轮询、批量导入与注册流测试。"""
from __future__ import annotations

import base64
from email.message import EmailMessage

import httpx

from claude_register import flow
from claude_register.outlook import (
    OUTLOOK_MAIL_URL,
    OutlookClient,
    is_outlook_credentials,
    parse_outlook_line,
    parse_outlook_mail_key,
)
from server import db, runner, sk_import
from server.config_store import Config

SAMPLE_EMAIL = "EarlAufderharcjx@outlook.com"
SAMPLE_PW = "qlvnpcefln4993"
SAMPLE_CID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
SAMPLE_RT = "M.C548_SN1.0.U.MsaArtifacts.-CtvGK*x5v4orSrN1BIqkOdgF*GKYavJ!VX6t6C8ExhQm"

LINE_STD = f"{SAMPLE_EMAIL}----{SAMPLE_PW}----{SAMPLE_CID}----{SAMPLE_RT}----aux@evenemail.com----auxpw"
LINE_REV = f"{SAMPLE_EMAIL}----{SAMPLE_PW}----{SAMPLE_RT}----{SAMPLE_CID}"


def test_parse_outlook_line_both_orders():
    c1 = parse_outlook_line(LINE_STD)
    assert c1 is not None
    assert c1.email == SAMPLE_EMAIL.lower()
    assert c1.password == SAMPLE_PW
    assert c1.client_id == SAMPLE_CID
    assert c1.refresh_token == SAMPLE_RT
    assert c1.mail_key == f"{SAMPLE_CID}----{SAMPLE_RT}"

    c2 = parse_outlook_line(LINE_REV)
    assert c2 == c1


def test_parse_outlook_mail_key_and_is_outlook():
    mk = f"{SAMPLE_CID}----{SAMPLE_RT}"
    assert parse_outlook_mail_key(mk) == (SAMPLE_CID, SAMPLE_RT)
    assert is_outlook_credentials(OUTLOOK_MAIL_URL, mk) is True
    assert is_outlook_credentials("https://mail.example.com", "ak_12345") is False


def test_sk_import_parses_and_imports_outlook_accounts(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    res = sk_import.run_import(
        conn,
        f"{LINE_STD}\n{LINE_REV}\n",
        proxy="socks5://p:1080",
        check=True,
        skip_dead=True,
        now="2026-10-22T00:00:00Z",
        probe=lambda *a: ("alive", "有效", ""),
        outlook_probe=lambda email, mk: ("alive", "Outlook 凭据有效"),
    )
    assert res["summary"]["created"] == 1
    assert res["summary"]["duplicate"] == 1
    row = db.get_account(conn, SAMPLE_EMAIL.lower())
    assert row is not None
    assert row["status"] == "pending"
    assert row["password"] == SAMPLE_PW
    assert row["mail_base_url"] == OUTLOOK_MAIL_URL
    assert row["mail_key"] == f"{SAMPLE_CID}----{SAMPLE_RT}"
    assert row["proxy"] == "socks5://p:1080"


def test_outlook_poll_magic_link(monkeypatch):
    b64_recipient = base64.b64encode(SAMPLE_EMAIL.lower().encode()).decode()
    magic = f"https://claude.ai/magic-link#0123456789abcdef0123456789abcdef:{b64_recipient}"

    msg = EmailMessage()
    msg["Subject"] = "Secure link to log in to Claude.ai"
    msg["From"] = "noreply@mail.anthropic.com"
    msg["To"] = SAMPLE_EMAIL
    msg["Date"] = "Thu, 22 Oct 2026 05:05:00 +0000"
    msg.set_content("plain text")
    msg.add_alternative(f'<a href="{magic}">Log in</a>', subtype="html")
    raw_bytes = msg.as_bytes()

    class FakeIMAP:
        def select(self, folder, readonly=True):
            if folder == "INBOX":
                return "OK", [b"1"]
            return "OK", [b"0"]

        def fetch(self, msg_set, parts):
            return "OK", [(b"1 (RFC822)", raw_bytes)]

        def logout(self):
            return "BYE", [b""]

    client = OutlookClient(
        email=SAMPLE_EMAIL,
        client_id=SAMPLE_CID,
        refresh_token=SAMPLE_RT,
    )
    monkeypatch.setattr(client, "_get_access_token", lambda: "fake_at")
    monkeypatch.setattr(client, "_open_imap", lambda token: FakeIMAP())

    found = client.poll_magic_link(
        to=SAMPLE_EMAIL,
        since="2026-10-22T05:00:00Z",
        timeout=5.0,
        interval=0.01,
    )
    assert found == magic


def test_outlook_poll_code_skips_excluded(monkeypatch):
    """注册验证码刚用过、设置密码的新码还没到：不能把旧码再交出去。"""
    client = OutlookClient(email=SAMPLE_EMAIL, client_id=SAMPLE_CID, refresh_token=SAMPLE_RT)
    batches = iter([
        [{"subject": "Your temporary ChatGPT verification code", "code": "111111"}],
        [{"subject": "Your temporary ChatGPT login code", "code": "222222"},
         {"subject": "Your temporary ChatGPT verification code", "code": "111111"}],
    ])
    monkeypatch.setattr(client, "_fetch_latest", lambda **kw: next(batches))
    code = client.poll_code(to=SAMPLE_EMAIL, since="2026-11-02T10:00:00Z", timeout=5.0,
                            exclude={"111111"}, sleep=lambda s: None)
    assert code == "222222"


def test_runner_passes_saved_outlook_credentials_to_flow(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    db.upsert_account(
        conn,
        SAMPLE_EMAIL.lower(),
        "outlook.com",
        "",
        "",
        None,
        "pending",
        password=SAMPLE_PW,
        proxy="socks5://saved:1080",
        created_at="2026-10-22T00:00:00Z",
        mail_key=f"{SAMPLE_CID}----{SAMPLE_RT}",
        mail_base_url=OUTLOOK_MAIL_URL,
    )

    called = {}

    def fake_flow(*, email=None, domain=None, config=None, **kw):
        called["email"] = email
        called["config"] = config
        called["kw"] = kw
        return {
            "email": email,
            "password": kw.get("password", ""),
            "sessionKey": "sk-ant-sid01-new",
            "proxy": config.register_proxy,
            "mail_key": kw.get("mail_key", ""),
            "mail_base_url": kw.get("mail_base_url", ""),
        }

    r = runner.Runner(conn, tmp_path, lambda: "2026-10-22T01:00:00Z")
    # 即使全局配置了默认 register_proxy，账号自身绑定的代理也必须优先生效
    rid = r.start(
        Config(register_proxy="http://127.0.0.1:7890"),
        email=SAMPLE_EMAIL.lower(),
        flow_fn=fake_flow,
    )
    import time
    for _ in range(50):
        if db.get_run(conn, rid)["status"] != "running":
            break
        time.sleep(0.02)

    assert called["email"] == SAMPLE_EMAIL.lower()
    assert called["config"].register_proxy == "socks5://saved:1080"
    assert called["kw"]["mail_key"] == f"{SAMPLE_CID}----{SAMPLE_RT}"
    assert called["kw"]["mail_base_url"] == OUTLOOK_MAIL_URL
    acct = db.get_account(conn, SAMPLE_EMAIL.lower())
    assert acct["status"] == "success"
    assert acct["session_key"] == "sk-ant-sid01-new"
    assert acct["mail_key"] == f"{SAMPLE_CID}----{SAMPLE_RT}"
    assert acct["mail_base_url"] == OUTLOOK_MAIL_URL


def test_export_outlook_format_appends_session_key():
    from server import export

    rows = [
        {"email": "a@outlook.com", "password": "pw", "mail_base_url": "outlook",
         "mail_key": "9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C548_" + "x" * 50,
         "session_key": "sk-ant-sid01-abc"},
        {"email": "b@example.com", "mail_base_url": "https://anymail", "mail_key": "k",
         "session_key": "sk-other"},
    ]
    body, media = export.render(rows, export.DEFAULT_FIELDS, "outlook")
    assert media.startswith("text/plain")
    assert body == ("a@outlook.com----pw----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C548_"
                    + "x" * 50 + "----sk-ant-sid01-abc\n")


def test_runner_marks_phone_required(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    db.upsert_account(
        conn, SAMPLE_EMAIL.lower(), "outlook.com", "", "", None, "pending",
        password=SAMPLE_PW, created_at="2026-10-22T00:00:00Z",
        mail_key=f"{SAMPLE_CID}----{SAMPLE_RT}", mail_base_url=OUTLOOK_MAIL_URL,
    )

    def fake_flow(*, email=None, domain=None, config=None, **kw):
        return {"email": email, "phone_required": True,
                "mail_key": kw.get("mail_key", ""), "mail_base_url": OUTLOOK_MAIL_URL}

    r = runner.Runner(conn, tmp_path, lambda: "2026-10-22T01:00:00Z")
    rid = r.start(Config(), email=SAMPLE_EMAIL.lower(), flow_fn=fake_flow)
    import time
    for _ in range(50):
        if db.get_run(conn, rid)["status"] != "running":
            break
        time.sleep(0.02)

    acct = db.get_account(conn, SAMPLE_EMAIL.lower())
    assert acct["status"] == "phone_required"
    assert acct["session_key"] == ""
    assert acct["password"] == SAMPLE_PW
    assert acct["mail_key"] == f"{SAMPLE_CID}----{SAMPLE_RT}"
