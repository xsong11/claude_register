"""OpenAI Admin Key 管理：校验、费用 / 剩余额度、项目 Key 增删。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from server import db, openai_admin
from server.app import create_app
from server.config_store import save_config

ADMIN_KEY = "sk-admin-REALSECRETVALUE1234abcd"
ORG = "user-7avtliwapxibfs9t86iwyx2y"
NOW = "2026-11-03T06:00:00Z"
DAY = 86400


def _ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


class FakeOpenAI:
    """按 2026-11-03 实测响应结构构造。默认每天 $1，10-25 无消费。"""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.daily = {}
        d = datetime(2026, 9, 1, tzinfo=timezone.utc)
        while d <= datetime(2026, 11, 3, tzinfo=timezone.utc):
            if d.date().isoformat() != "2026-10-25":
                self.daily[int(d.timestamp())] = 1.0
            d += timedelta(days=1)
        self.projects = [
            {"id": "proj_A", "object": "organization.project", "name": "Default project",
             "status": "active", "archived_at": None},
            {"id": "proj_OLD", "object": "organization.project", "name": "Old",
             "status": "archived", "archived_at": 1790000000},
        ]
        self.keys = {
            "proj_A": [
                {"id": "key_user", "object": "organization.project.api_key", "name": "mine",
                 "redacted_value": "sk-proj-****abcd", "created_at": 1790000000,
                 "last_used_at": 1790100000,
                 "owner": {"type": "user", "user": {"id": "user-1", "name": "xs",
                                                     "email": "a@x.com"}}},
                {"id": "key_sa", "object": "organization.project.api_key", "name": "bot",
                 "redacted_value": "sk-svca-****wxyz", "created_at": 1790000001,
                 "last_used_at": None,
                 "owner": {"type": "service_account",
                           "service_account": {"id": "svc_1", "name": "bot"}}},
            ],
        }
        self.deleted: list[tuple[str, str]] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.headers.get("authorization") != f"Bearer {ADMIN_KEY}":
            return httpx.Response(401, json={"error": {
                "message": "Incorrect API key provided: sk-admin*******d000.",
                "type": "invalid_request_error", "code": "invalid_api_key"}})
        path = req.url.path.removeprefix("/v1/organization")
        q = req.url.params
        hdr = {"openai-organization": ORG}
        if path == "/projects" and req.method == "GET":
            return httpx.Response(200, headers=hdr, json={
                "object": "list", "data": self.projects, "has_more": False,
                "first_id": "proj_A", "last_id": "proj_OLD"})
        if path == "/costs":
            assert "end_time" not in q
            start, limit = int(q["start_time"]), int(q["limit"])
            offset = int(q.get("page", "0") or 0)
            all_days = list(range(start, _ts(2026, 11, 4), DAY))
            chunk = all_days[offset:offset + limit]
            more = offset + limit < len(all_days)
            return httpx.Response(200, headers=hdr, json={
                "object": "page",
                "data": [{"object": "bucket", "start_time": t, "end_time": t + DAY,
                          "results": ([{"object": "organization.costs.result",
                                        "amount": {"value": self.daily[t], "currency": "usd"},
                                        "line_item": None, "project_id": None}]
                                      if t in self.daily else [])} for t in chunk],
                "has_more": more, "next_page": str(offset + limit) if more else None})
        if path.startswith("/projects/") and path.endswith("/api_keys"):
            pid = path.split("/")[2]
            return httpx.Response(200, json={"object": "list", "data": self.keys.get(pid, []),
                                             "has_more": False, "first_id": None, "last_id": None})
        parts = path.split("/")
        if len(parts) == 5 and parts[3] == "api_keys" and req.method == "GET":
            key = next((k for k in self.keys.get(parts[2], []) if k["id"] == parts[4]), None)
            if key is None:
                return httpx.Response(404, json={"error": {"message": "No such key"}})
            return httpx.Response(200, json=key)
        if len(parts) == 5 and req.method == "DELETE":
            self.deleted.append((parts[3], parts[4]))
            return httpx.Response(200, json={"deleted": True, "id": parts[4]})
        if path.endswith("/service_accounts") and req.method == "POST":
            name = json.loads(req.content)["name"]
            return httpx.Response(200, json={
                "object": "organization.project.service_account", "id": "svc_new",
                "name": name, "role": "member", "created_at": 1793700000,
                "api_key": {"object": "organization.project.service_account.api_key",
                            "value": "sk-svcacct-PLAINTEXT", "name": "Secret Key",
                            "created_at": 1793700000, "id": "key_new"}})
        return httpx.Response(404, json={"error": {"message": f"not found {path}"}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    save_config(tmp_path / "config.yaml", {"panel_password": "pw"})
    app = create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                     now_fn=lambda: NOW)
    fake = FakeOpenAI()
    monkeypatch.setattr(openai_admin, "TRANSPORT", httpx.MockTransport(fake.handle))
    c = TestClient(app)
    c.post("/api/login", json={"password": "pw"})
    return app, c, fake


def _add(c, **kw):
    body = {"name": "主组织", "admin_key": ADMIN_KEY, "credit_usd": 50,
            "credit_since": "2026-10-20", **kw}
    return c.post("/api/openai/keys", json=body)


def test_cost_window_and_sum():
    now = datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
    start, month, since = openai_admin.cost_window(now, datetime(2026, 10, 20).date())
    assert (start, month, since) == (_ts(2026, 10, 20), _ts(2026, 11, 1), _ts(2026, 10, 20))
    start, month, since = openai_admin.cost_window(now, None)
    assert start == month == since == _ts(2026, 11, 1)
    # 起算日晚于月初：拉取起点仍是月初
    start, _, since = openai_admin.cost_window(now, datetime(2026, 11, 2).date())
    assert start == _ts(2026, 11, 1) and since == _ts(2026, 11, 2)
    buckets = [
        {"start_time": _ts(2026, 10, 31), "results": [{"amount": {"value": 2, "currency": "usd"}}]},
        {"start_time": _ts(2026, 11, 1), "results": []},
        {"start_time": _ts(2026, 11, 2), "results": [
            {"amount": {"value": 0.25, "currency": "usd"}},
            {"amount": {"value": 9, "currency": "eur"}}]},
    ]
    assert openai_admin.sum_costs(buckets, _ts(2026, 11, 1), _ts(2026, 10, 1)) == (0.25, 2.25)


def test_mask_key():
    assert openai_admin.mask_key(ADMIN_KEY) == "sk-admin-…abcd"
    assert openai_admin.mask_key("") == ""


def test_create_verifies_and_refreshes(env, monkeypatch):
    app, c, fake = env
    monkeypatch.setattr(openai_admin, "COSTS_LIMIT", 4)  # 强制翻页
    r = _add(c)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org_id"] == ORG and body["status"] == "ok"
    assert body["key_masked"] == "sk-admin-…abcd"
    # 10-20 .. 11-03 共 15 天，10-25 无消费
    assert body["cost_since_usd"] == 14 and body["cost_month_usd"] == 3
    assert body["remaining_usd"] == 36
    assert ADMIN_KEY not in r.text
    cost_calls = [q for q in fake.calls if q.url.path.endswith("/costs")]
    assert len(cost_calls) == 4 and all(q.url.params["limit"] == "4" for q in cost_calls)
    listed = c.get("/api/openai/keys").json()["keys"]
    assert [k["name"] for k in listed] == ["主组织"]
    assert ADMIN_KEY not in json.dumps(listed)
    assert db.get_openai_key(app.state.cr.conn, body["id"])["admin_key"] == ADMIN_KEY


def test_create_defaults(env):
    _, c, _ = env
    body = _add(c, credit_usd=None, credit_since="").json()
    assert body["credit_since"] == "2026-11-03" and body["remaining_usd"] is None
    assert body["cost_month_usd"] == 3 and body["cost_since_usd"] == 1


def test_create_rejects_invalid_key(env):
    app, c, _ = env
    r = _add(c, admin_key="sk-admin-wrong0000")
    assert r.status_code == 400 and "无效" in r.json()["detail"]
    assert "d000" not in r.text
    assert db.list_openai_keys(app.state.cr.conn) == []


def test_create_validation(env):
    _, c, _ = env
    assert _add(c, name=" ").status_code == 400
    assert _add(c, admin_key="").status_code == 400
    assert _add(c, credit_usd="abc").status_code == 400
    assert _add(c, credit_usd=-1).status_code == 400
    assert _add(c, credit_since="2026/10/20").status_code == 400


def test_update_keeps_key_when_blank(env):
    app, c, _ = env
    kid = _add(c).json()["id"]
    r = c.patch(f"/api/openai/keys/{kid}", json={"name": "改名", "admin_key": "",
                                                 "credit_usd": 100})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "改名" and r.json()["remaining_usd"] == 86
    assert db.get_openai_key(app.state.cr.conn, kid)["admin_key"] == ADMIN_KEY
    r = c.patch(f"/api/openai/keys/{kid}", json={"credit_since": "2026-11-02"})
    assert r.json()["cost_since_usd"] == 2


def test_refresh_marks_invalid(env):
    app, c, _ = env
    kid = _add(c).json()["id"]
    db.update_openai_key(app.state.cr.conn, kid, {"admin_key": "sk-admin-revoked0000"})
    body = c.post(f"/api/openai/keys/{kid}/refresh").json()
    assert body["status"] == "invalid" and "无效" in body["last_error"]
    assert body["cost_since_usd"] == 14  # 保留上次成功的数据


def test_refresh_network_error(env, monkeypatch):
    _, c, _ = env
    kid = _add(c).json()["id"]

    def boom(req):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(openai_admin, "TRANSPORT", httpx.MockTransport(boom))
    body = c.post(f"/api/openai/keys/{kid}/refresh").json()
    assert body["status"] == "error" and "连接 api.openai.com 失败" in body["last_error"]


def test_delete(env):
    app, c, _ = env
    kid = _add(c).json()["id"]
    assert c.delete(f"/api/openai/keys/{kid}").json() == {"ok": True}
    assert c.delete(f"/api/openai/keys/{kid}").status_code == 404
    assert db.list_openai_keys(app.state.cr.conn) == []


def test_list_project_keys(env):
    _, c, _ = env
    kid = _add(c).json()["id"]
    body = c.get(f"/api/openai/keys/{kid}/api-keys").json()
    assert body["projects"] == [{"id": "proj_A", "name": "Default project"}]
    by_id = {k["id"]: k for k in body["api_keys"]}
    assert by_id["key_user"]["owner_type"] == "user" and by_id["key_user"]["owner_name"] == "xs"
    assert by_id["key_sa"]["owner_type"] == "service_account"
    assert by_id["key_sa"]["project_name"] == "Default project"


def test_create_project_key(env):
    _, c, fake = env
    kid = _add(c).json()["id"]
    r = c.post(f"/api/openai/keys/{kid}/api-keys", json={"project_id": "proj_A", "name": "bot2"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "sk-svcacct-PLAINTEXT" and r.json()["id"] == "key_new"
    sent = [q for q in fake.calls if q.method == "POST"][-1]
    assert json.loads(sent.content) == {"name": "bot2"}
    assert c.post(f"/api/openai/keys/{kid}/api-keys", json={"name": "x"}).status_code == 400


def test_delete_project_key_by_owner_type(env):
    _, c, fake = env
    kid = _add(c).json()["id"]
    base = f"/api/openai/keys/{kid}/api-keys"
    assert c.delete(f"{base}/key_user?project_id=proj_A").status_code == 200
    assert c.delete(f"{base}/key_sa?project_id=proj_A").status_code == 200
    assert fake.deleted == [("api_keys", "key_user"), ("service_accounts", "svc_1")]
    r = c.delete(f"{base}/key_missing?project_id=proj_A")
    assert r.status_code == 502 and "404" in r.json()["detail"]


def test_requires_panel_auth(env):
    app, _, _ = env
    anon = TestClient(app)
    assert anon.get("/api/openai/keys").status_code == 401
    assert anon.post("/api/openai/keys", json={}).status_code == 401
    assert anon.get("/api/openai/keys/1/api-keys").status_code == 401
