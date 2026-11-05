from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from claude_register.account_inspect import inspect_claude
from claude_register.chatgpt_check import ACCOUNTS_URL, ME_URL, check_chatgpt_session
from server import db, okbrowser_ops, workspace_resync, workspace_store, workspaces
from server.app import create_app
from server.config_store import save_config

NOW = "2026-11-05T12:00:00Z"
EMAIL = "same@example.test"
SK = "sk-ant-sid02-fake-fixture-for-local-testing"
EXPIRES = "2026-12-03T13:08:13+00:00"


@pytest.fixture
def panel(tmp_path):
    config = tmp_path / "config.yaml"
    save_config(config, {"panel_password": "test-password"})
    app = create_app(data_dir=tmp_path, config_path=config, now_fn=lambda: NOW)
    with TestClient(app) as client:
        assert client.get("/api/workspaces/claude/library").status_code == 401
        assert client.post("/api/login", json={"password": "test-password"}).status_code == 200
        yield client, app.state.cr


def test_registration_detection_library_and_management_are_independent(panel, monkeypatch):
    c, state = panel
    preview_url = "/api/workspaces/claude/register/import"
    assert c.post(preview_url, json={"text": EMAIL}).json()["items"][0]["result"] == "ready"
    assert c.get("/api/workspaces/claude/register").json()["accounts"] == []
    c.post(preview_url, json={"text": EMAIL, "group_name": "batch-a", "commit": True}).raise_for_status()
    rid = state.runner.start(state.config(), email=EMAIL, platform="claude",
                             flow_fn=lambda **_: {"email": EMAIL, "sessionKey": SK})
    deadline = time.monotonic() + 5
    while state.runner.is_busy() and time.monotonic() < deadline:
        time.sleep(.01)
    assert db.get_run(state.conn, rid)["status"] == "success"
    [record] = c.get("/api/workspaces/claude/detect").json()["accounts"]
    account_id = record["id"]
    assert record["group_name"] == "batch-a"
    assert record["check_status"] == "unchecked"
    assert SK not in c.get("/api/workspaces/claude/detect").text
    assert db.count_claimable(state.conn) == 0

    def post(stage, action, **fields):
        return c.post(f"/api/workspaces/claude/{stage}/batch", json={"ids": [account_id], "action": action, **fields})

    assert post("detect", "status", status="other").status_code == 200
    assert post("detect", "status", status="other").status_code == 200
    replies = iter([
        {"status": "dead", "detail": "HTTP 401"},
        {"status": "alive", "detail": "有效", "plan": "free", "portal_kyc": "not_flagged"},
        {"status": "blocked", "detail": "HTTP 403"},
    ])
    monkeypatch.setattr(workspaces, "inspect_account", lambda *_: next(replies))
    assert post("detect", "detect").json()["results"][0]["stage"] == "detect"
    assert post("detect", "detect").json()["results"][0]["stage"] == "library"
    assert post("library", "detect").json()["results"][0]["stage"] == "library"
    detail = c.get(f"/api/workspaces/claude/record/{account_id}").json()
    assert (detail["plan"], detail["check_status"], detail["management_status"]) == ("free", "blocked", "other")
    assert detail["portal_kyc"] == "unknown"
    assert detail["history"] == [dict(old_status="normal", new_status="other", reason="",
                                      changed_at=NOW, source="manual")]
    assert db.count_claimable(state.conn, check_status="blocked") == 0
    assert not workspace_store.eligible_for_sync(state.conn, EMAIL)
    assert c.get(f"/api/workspaces/chatgpt/record/{account_id}").status_code == 404


def test_same_email_import_scopes_credentials_group_deletion_and_claim(panel, monkeypatch):
    c, state = panel
    for platform, text in (("claude", f"{EMAIL}----{SK}"), ("chatgpt", f"{EMAIL}----fake-gpt")):
        url = f"/api/workspaces/{platform}/library/import"
        response = c.post(url, json={"text": text, "commit": True, "group_name": platform})
        assert response.json()["items"][0]["result"] == "added"
        assert c.post(url, json={"text": text, "commit": True}).json()["items"][0]["result"] == "duplicate"

    def batch(platform, action, **fields):
        rec = c.get(f"/api/workspaces/{platform}/library").json()["accounts"][0]
        return c.post(f"/api/workspaces/{platform}/library/batch",
                      json={"action": action, "ids": [rec["id"]], **fields})

    assert batch("claude", "export", fields=["chatgpt_session_token"]).status_code == 400
    assert SK in batch("claude", "export", fields=["session_key"]).text
    assert batch("claude", "group", group_name="moved").status_code == 200
    assert db.count_claimable(state.conn, platform="chatgpt", group="chatgpt") == 1
    assert db.count_claimable(state.conn, platform="claude", group="moved") == 1
    monkeypatch.setattr(workspaces, "inspect_account", lambda *_: {"status": "banned", "detail": "已停用"})
    assert batch("chatgpt", "detect").json()["results"][0]["status"] == "banned"
    assert db.count_claimable(state.conn, platform="chatgpt") == 0
    assert batch("claude", "delete").status_code == 200
    [gpt] = c.get("/api/workspaces/chatgpt/library").json()["accounts"]
    detail = c.get(f"/api/workspaces/chatgpt/record/{gpt['id']}").json()
    assert detail["credentials"]["chatgpt_session_token"] == "fake-gpt"
    assert "session_key" not in detail["credentials"]
    assert detail["history"][0]["source"] == "detection"
    assert not db.get_account(state.conn, EMAIL)["session_key"]


def test_subscription_expiry_follows_detection_into_library_and_clears_on_recheck(panel, monkeypatch):
    c, state = panel
    account_id = workspace_store.add(state.conn, EMAIL, "claude", "detect", group="", proxy="", note="",
                                     creds={"session_key": SK}, shared={}, now=NOW)
    replies = iter([
        {"status": "alive", "plan": "pro", "subscription_expires_at": EXPIRES},
        {"status": "alive", "plan": "pro", "subscription_expires_at": ""},
        {"status": "alive", "plan": "pro"},
    ])
    monkeypatch.setattr(workspaces, "inspect_account", lambda *_: next(replies))

    for stage, expected in (("detect", (EXPIRES, NOW)), ("library", ("", NOW)), ("library", ("", ""))):
        c.post(f"/api/workspaces/claude/{stage}/batch",
               json={"ids": [account_id], "action": "detect"}).raise_for_status()
        [item] = c.get("/api/workspaces/claude/library").raise_for_status().json()["accounts"]
        detail = c.get(f"/api/workspaces/claude/record/{account_id}").raise_for_status().json()
        for view in (item, detail):
            assert (view["subscription_expires_at"], view["subscription_checked_at"]) == expected
            assert view["check_status"] == "alive" and view["plan"] == "pro"


def test_resync_recovers_gpt_only_and_skips_unbound_accounts(panel, monkeypatch):
    c, state = panel
    conn = state.conn

    def add(email, platform, stage, creds):
        return workspace_store.add(conn, email, platform, stage, group="测试分组",
                                   proxy="socks5://proxy.test:1080", note="", creds=creds, shared={}, now=NOW)

    claude_id = add(EMAIL, "claude", "library", {"session_key": SK})
    gpt_id = add(EMAIL, "chatgpt", "detect", {"chatgpt_access_token": "old-fixture"})
    unbound_id = add("unbound@example.test", "chatgpt", "detect", {"chatgpt_access_token": "unbound-fixture"})
    db.update_account_fields(conn, EMAIL, {"okbrowser_window_id": "bound-fixture"})
    with conn:
        workspace_store.apply_detection(conn, gpt_id, {"status": "dead"}, now=NOW)
        workspace_store.apply_detection(conn, unbound_id, {"status": "blocked"}, now=NOW)
        workspace_store.set_status(conn, workspace_store.get(conn, gpt_id), "disabled", NOW)

    calls = []
    def read_window(cfg, window_id, reader):
        calls.append(window_id)
        return {"email": EMAIL.upper(), "chatgpt_session_token": "new-cookie-fixture",
                "chatgpt_access_token": "new-token-fixture",
                "chatgpt_session_json": json.dumps({"accessToken": "new-token-fixture",
                                                    "user": {"email": EMAIL},
                                                    "account": {"planType": "prolite"}})}

    def inspect(platform, row):
        assert platform == "chatgpt"
        assert row["email"] == EMAIL and row["proxy"] == "socks5://proxy.test:1080"
        assert row["chatgpt_access_token"] == "new-token-fixture"
        assert row["chatgpt_session_token"] == ""  # Do not rotate the browser cookie a second time.
        assert "session_key" not in row
        return {"status": "alive", "detail": "有效", "fresh": {}}

    monkeypatch.setattr(okbrowser_ops, "read_bound_session", read_window)
    monkeypatch.setattr(workspace_resync, "inspect_account", inspect)
    response = c.post("/api/workspaces/chatgpt/detect/batch", json={"ids": [gpt_id, unbound_id], "action": "resync"})
    updated, skipped = response.raise_for_status().json()["results"]
    assert updated["status"] == "alive" and updated["stage"] == "library"
    assert skipped["status"] == "skipped" and "未绑定" in skipped["detail"]
    assert "new-token-fixture" not in response.text and "new-cookie-fixture" not in response.text
    assert calls == ["bound-fixture"]
    detail = c.get(f"/api/workspaces/chatgpt/record/{gpt_id}").raise_for_status().json()
    assert (detail["plan"], detail["group_name"], detail["management_status"]) == ("pro_lite", "测试分组", "disabled")
    row = db.get_account(conn, EMAIL)
    assert row["chatgpt_session_token"] == "new-cookie-fixture"
    assert row["chatgpt_access_token"] == "new-token-fixture"
    assert row["session_key"] == SK and workspace_store.get(conn, claude_id)["check_status"] == "unchecked"


@pytest.mark.parametrize(("platform", "candidate", "inspection", "reason"), [
    ("chatgpt", {}, {}, "没有可读取"),
    ("chatgpt", {"email": "elsewhere@example.test", "chatgpt_access_token": "new"}, {}, "另一个账号"),
    ("chatgpt", {"chatgpt_access_token": "new"}, {}, "没有返回邮箱"),
    ("chatgpt", {"email": EMAIL, "chatgpt_access_token": "new"}, {"status": "dead", "detail": "已失效"}, "未通过检测"),
    ("claude", {"session_key": "new"}, {"status": "alive", "metadata": {}}, "没有返回邮箱"),
])
def test_resync_keeps_existing_credentials_if_identity_or_session_is_unconfirmed(panel, monkeypatch,
                                                                                platform, candidate, inspection, reason):
    c, state = panel
    conn = state.conn
    field = "session_key" if platform == "claude" else "chatgpt_access_token"
    account_id = workspace_store.add(conn, EMAIL, platform, "library", group="", proxy="", note="",
                                     creds={field: "original-fixture"}, shared={}, now=NOW)
    db.update_account_fields(conn, EMAIL, {"okbrowser_window_id": "bound-fixture"})
    with conn:
        workspace_store.apply_detection(conn, account_id, {"status": "dead"}, now=NOW)
    monkeypatch.setattr(okbrowser_ops, "read_bound_session", lambda *_: candidate)
    monkeypatch.setattr(workspace_resync, "inspect_account", lambda *_: inspection)
    result = c.post(f"/api/workspaces/{platform}/library/batch",
                    json={"ids": [account_id], "action": "resync"}).raise_for_status().json()["results"][0]
    assert result["status"] == "skipped" and reason in result["detail"]
    assert db.get_account(conn, EMAIL)[field] == "original-fixture"
    assert workspace_store.get(conn, account_id)["check_status"] == "dead"


def test_migration_preserves_old_data_and_separates_unknown_and_banned(tmp_path):
    path = tmp_path / "legacy.db"
    conn = db.init_db(path)
    db.upsert_account(conn, EMAIL, "example.test", "", "", None, "success",
                      session_key=SK, created_at=NOW)
    db.update_account_fields(conn, EMAIL, {"group_name": "batch-a", "okbrowser_window_id": "window-1"})
    db.update_account_check(conn, EMAIL, "dead", NOW)
    db.update_account_chatgpt(conn, EMAIL, {"chatgpt_status": "banned"})
    db.create_account(conn, "unknown@example.test", {"group_name": "gs_claude"}, created_at=NOW)
    conn.execute("DELETE FROM workspace_migrations")
    conn.commit()
    conn.close()
    conn = db.init_db(path)
    claude = workspace_store.find(conn, EMAIL, "claude")
    chatgpt = workspace_store.find(conn, EMAIL, "chatgpt")
    assert (claude["management_status"], claude["check_status"], claude["stage"]) == ("normal", "dead", "library")
    assert workspace_store.history(conn, chatgpt["id"])[0]["source"] == "migration"
    assert workspace_store.find(conn, "unknown@example.test", "unassigned")["group_name"] == "gs_claude"
    assert db.get_account(conn, EMAIL)["okbrowser_window_id"] == "window-1"
    conn.close()
    conn = db.init_db(path)
    assert workspace_store.find(conn, EMAIL, "claude")["id"] == claude["id"]
    conn.close()


def test_existing_chatgpt_plans_backfill_only_from_matching_verified_sessions(tmp_path):
    path = tmp_path / "chatgpt.db"
    conn = db.init_db(path)
    expected = {}
    for label, status, token, reported_email, raw_plan, known in (
        ("lite", "alive", "active", None, "prolite", ""),
        ("free", "alive", "active", None, "free", ""),
        ("different-token", "alive", "other", None, "prolite", ""),
        ("different-email", "alive", "active", "other@example.test", "prolite", ""),
        ("expired", "dead", "active", None, "prolite", ""),
        ("unrecognized", "alive", "active", None, "unknown_vendor_tier", ""),
        ("already-known", "alive", "active", None, "free", "plus"),
    ):
        email = f"{label}@example.test"
        account_id = workspace_store.add(conn, email, "chatgpt", "library", group="", proxy="",
                                         note="", creds={"chatgpt_access_token": "active"}, shared={}, now=NOW)
        raw = json.dumps({"accessToken": token, "user": {"email": reported_email or email.upper()},
                          "account": {"planType": raw_plan}})
        db.update_account_chatgpt(conn, email, {"chatgpt_session_json": raw})
        with conn:
            workspace_store.apply_detection(conn, account_id, {"status": status, "plan": known}, now=NOW)
        expected[account_id] = {"lite": "pro_lite", "free": "free"}.get(label, known)
    conn.execute("DELETE FROM workspace_migrations WHERE name='chatgpt_saved_plan'")
    conn.commit()
    conn.close()

    conn = db.init_db(path)
    for account_id, plan in expected.items():
        row = workspace_store.get(conn, account_id)
        assert (row["plan"], row["plan_checked_at"]) == (plan, NOW if plan else "")
    lite = workspace_store.find(conn, "lite@example.test", "chatgpt")
    later = "2026-11-05T13:00:00Z"
    with conn:
        workspace_store.apply_detection(conn, lite["id"], {"status": "alive", "plan": ""}, now=later)
    refreshed = workspace_store.get(conn, lite["id"])
    assert (refreshed["checked_at"], refreshed["plan"], refreshed["plan_checked_at"]) == (later, "pro_lite", NOW)
    with conn:
        workspace_store.apply_detection(conn, lite["id"], {"status": "alive", "plan": "free"}, now=later)
    refreshed = workspace_store.get(conn, lite["id"])
    assert (refreshed["plan"], refreshed["plan_checked_at"]) == ("free", later)
    conn.close()


@pytest.mark.parametrize(("ending", "expiry"), [("2026-12-03T21:08:13+08:00", EXPIRES), (None, "")])
def test_claude_checks_active_organization_and_ignores_mismatched_kyc(ending, expiry):
    active = "00000000-0000-0000-0000-000000000123"
    other = "00000000-0000-0000-0000-000000000234"
    paths = []

    def reply(req):
        paths.append(req.url.path)
        assert req.url.host == "claude.ai" and req.method == "GET"
        if req.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": other, "raven_type": "team"},
                                            {"uuid": active, "rate_limit_tier": "claude_max_5x"}])
        if req.url.path == "/api/bootstrap":
            return httpx.Response(200, json={"account": {"lastActiveOrgId": active, "email": EMAIL, "created_at": NOW}})
        if req.url.path.endswith("/kyc_status"):
            return httpx.Response(200, json={"status": "not_required", "org_uuid": other})
        if req.url.path.endswith("/subscription_details"):
            return httpx.Response(200, json={"plan_ending_at": ending, "next_charge_at": "2026-12-01T13:08:13Z"})
        return httpx.Response(200, json={"hold_notice": None, "org_uuid": active})

    result = inspect_claude(SK, "", expected_email=EMAIL, client_factory=lambda _: httpx.Client(transport=httpx.MockTransport(reply)))
    assert (result["status"], result["org_uuid"], result["plan"]) == ("alive", active, "max_5x")
    assert result["subscription_expires_at"] == expiry
    assert "subscription_kyc" not in result and result["portal_kyc"] == "not_flagged"
    assert all(other not in path for path in paths)
    assert f"/api/organizations/{active}/kyc_status" in paths


@pytest.mark.parametrize(("code", "payload", "status"), [
    (401, {"error": {"code": "token_revoked"}}, "dead"),
    (403, {"error": {"code": "forbidden"}}, "blocked"),
    (401, {"detail": {"code": "account_deactivated"}}, "banned"),
    (200, {"email": EMAIL, "account": {"plan_type": "plus"}}, "alive"),
    (200, {"email": EMAIL, "account": {"planType": "prolite"}}, "alive"),
    (200, {"email": "other@example.test"}, "error"),
])
def test_chatgpt_inspection_only_confirms_explicit_ban(code, payload, status):
    def reply(req):
        assert req.headers["authorization"] == "Bearer fixture"
        if str(req.url) == ME_URL:
            return httpx.Response(code, json=payload)
        assert str(req.url) == ACCOUNTS_URL
        return httpx.Response(503)

    found, _, fields = check_chatgpt_session(
        "", "fixture", inspection=True, expected_email=EMAIL,
        client_factory=lambda _: httpx.Client(transport=httpx.MockTransport(reply)),
    )
    assert found == status
    if found == "alive":
        assert fields["workspace_plan"] == ("pro_lite" if "planType" in payload["account"] else "plus")
        assert "workspace_subscription_expires_at" not in fields


@pytest.mark.parametrize(("account_id", "expiry"), [("personal", EXPIRES), ("missing", None), ("", None)])
def test_chatgpt_subscription_expiry_uses_the_matching_workspace(account_id, expiry):
    def reply(req):
        assert req.method == "GET" and req.headers["authorization"] == "Bearer fixture"
        if str(req.url) == ME_URL:
            return httpx.Response(200, json={"email": EMAIL, "account": {"plan_type": "prolite"}})
        assert str(req.url) == ACCOUNTS_URL
        return httpx.Response(200, json={"accounts": {
            "default": {"account": {"account_id": "business"}, "entitlement": {"expires_at": "2027-01-01T00:00:00Z"}},
            "personal": {"account": {"account_id": "personal"},
                         "entitlement": {"expires_at": EXPIRES, "renews_at": "2026-12-03T07:08:13Z"},
                         "last_active_subscription": {"will_renew": False}},
            "business": {"account": {"account_id": "business"}, "entitlement": {"expires_at": "2027-01-01T00:00:00Z"}},
        }})

    status, _, fields = check_chatgpt_session(
        "", "fixture", inspection=True, expected_email=EMAIL, expected_account_id=account_id,
        client_factory=lambda _: httpx.Client(transport=httpx.MockTransport(reply)),
    )
    assert status == "alive"
    assert fields.get("workspace_subscription_expires_at") == expiry
