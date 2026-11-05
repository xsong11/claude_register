"""POST /api/accounts/{email}/chatgpt-session：返回 /api/auth/session 完整 JSON。"""
from __future__ import annotations

import json
from unittest import mock

from fastapi.testclient import TestClient

from server import db
from server.app import create_app
from server.config_store import save_config

PAYLOAD = {"accessToken": "new-at", "expires": "2027-01-31T00:00:00Z",
           "user": {"id": "u-1", "email": "a@x.com"}, "authProvider": "openai"}


def _client(tmp_path):
    save_config(tmp_path / "config.yaml", {"panel_password": "pw"})
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                     now_fn=lambda: "2026-11-02T00:00:00Z")
    conn = app.state.cr.conn
    db.upsert_account(conn, "a@x.com", "x.com", "", "", 1, "")
    db.update_account_chatgpt(conn, "a@x.com", {
        "chatgpt_session_token": "st", "chatgpt_access_token": "old-at",
        "chatgpt_session_json": json.dumps({"accessToken": "old-at"}),
    })
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})
    return app, c


def test_chatgpt_session_returns_fresh_json_and_saves(tmp_path):
    app, c = _client(tmp_path)
    fresh = {"chatgpt_access_token": "new-at", "chatgpt_session_json": json.dumps(PAYLOAD)}
    with mock.patch("server.app.check_chatgpt_session", return_value=("alive", "有效", fresh)):
        body = c.post("/api/accounts/a@x.com/chatgpt-session").json()
    assert body["status"] == "alive"
    assert body["fresh"] is True
    assert body["session"] == PAYLOAD
    row = db.get_account(app.state.cr.conn, "a@x.com")
    assert json.loads(row["chatgpt_session_json"]) == PAYLOAD
    assert row["chatgpt_access_token"] == "new-at"


def test_account_list_omits_session_json_but_get_returns_it(tmp_path):
    _, c = _client(tmp_path)
    row = next(r for r in c.get("/api/accounts").json() if r["email"] == "a@x.com")
    assert "chatgpt_session_json" not in row
    assert row["chatgpt_session_json_len"] == len(json.dumps({"accessToken": "old-at"}))
    assert c.get("/api/accounts/a@x.com/chatgpt-session").json() == {
        "session": {"accessToken": "old-at"}}


def test_chatgpt_session_falls_back_to_saved_when_blocked(tmp_path):
    _, c = _client(tmp_path)
    with mock.patch("server.app.check_chatgpt_session",
                    return_value=("blocked", "被 Cloudflare 拦截", {})):
        body = c.post("/api/accounts/a@x.com/chatgpt-session").json()
    assert body["status"] == "blocked"
    assert body["fresh"] is False
    assert body["session"] == {"accessToken": "old-at"}
