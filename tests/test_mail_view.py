"""账号「查询邮件」：GET /api/accounts/{email}/mails。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from claude_register.outlook import OutlookAccessError, OutlookClient
from server import db, mail_view
from server.app import create_app
from server.config_store import save_config

OUTLOOK_KEY = "9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C5_BAY.refresh-token-value-xxxxxxxxxxxx"


def _client(tmp_path, **acct):
    save_config(tmp_path / "config.yaml", {"panel_password": "pw"})
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                     now_fn=lambda: "2026-11-03T00:00:00Z")
    db.upsert_account(app.state.cr.conn, "a@outlook.com", "outlook.com", "", "", 1, "success",
                      **acct)
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})
    return c


def test_summarize_extracts_code_and_plain_text():
    out = mail_view.summarize({
        "subject": "Your temporary ChatGPT verification code",
        "html_body": "<p style='padding: 28px'>Enter this temporary verification code: 358202</p>",
        "from": "ChatGPT <noreply@tm.openai.com>",
        "date": "2026-11-03T04:00:09Z",
        "folder": "INBOX",
    })
    assert out["code"] == "358202"
    assert out["text"] == "Enter this temporary verification code: 358202"
    assert out["from"] == "ChatGPT <noreply@tm.openai.com>" and not out["truncated"]


def test_mails_endpoint_reads_outlook(tmp_path, monkeypatch):
    seen = {}

    def fake_list(self, *, limit):
        seen["email"], seen["limit"] = self.email, limit
        return [{"subject": "Hi", "text_body": "hello", "folder": "Junk", "date": "d"}]

    monkeypatch.setattr(OutlookClient, "list_messages", fake_list)
    c = _client(tmp_path, mail_key=OUTLOOK_KEY, mail_base_url="outlook")
    r = c.get("/api/accounts/a@outlook.com/mails?limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "outlook" and seen == {"email": "a@outlook.com", "limit": 5}
    assert body["mails"][0]["folder"] == "Junk" and body["mails"][0]["text"] == "hello"


def test_mails_endpoint_reports_revoked_outlook_token(tmp_path, monkeypatch):
    def boom(self, *, limit):
        raise OutlookAccessError(400, "invalid_grant")

    monkeypatch.setattr(OutlookClient, "list_messages", boom)
    c = _client(tmp_path, mail_key=OUTLOOK_KEY, mail_base_url="outlook")
    r = c.get("/api/accounts/a@outlook.com/mails")
    assert r.status_code == 422 and "invalid_grant" in r.json()["detail"]


def test_mails_endpoint_without_credentials(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/accounts/a@outlook.com/mails").status_code == 400
    assert c.get("/api/accounts/nobody@x.com/mails").status_code == 404
