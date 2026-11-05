"""账号分组、ChatGPT 字段迁移、按平台/分组导出与领取。"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from server import db, export
from server.app import create_app
from server.config_store import load_config, save_config

KEY = "cr_test_key_0123456789"
H = {"Authorization": f"Bearer {KEY}"}


def _app(tmp_path, **cfg):
    save_config(tmp_path / "config.yaml", {"panel_password": "pw", "api_enabled": True,
                                           "api_key": KEY, **cfg})
    return create_app(data_dir=tmp_path, config_path=tmp_path / "config.yaml",
                      now_fn=lambda: "2026-11-02T00:00:00Z")


def _login(app):
    c = TestClient(app)
    assert c.post("/api/login", json={"password": "pw"}).status_code == 200
    return c


def _seed(conn):
    db.upsert_account(conn, "a@x.com", "x.com", "", "", 1, "success", session_key="sk-a")
    db.upsert_account(conn, "b@x.com", "x.com", "", "", 2, "success", session_key="sk-b")
    db.upsert_account(conn, "c@x.com", "x.com", "", "", None, "")
    db.update_account_chatgpt(conn, "c@x.com", {"chatgpt_status": "success",
                                                "chatgpt_access_token": "at-c"})
    db.update_account_chatgpt(conn, "a@x.com", {"chatgpt_status": "success",
                                                "chatgpt_session_token": "st-a"})
    db.set_accounts_group(conn, ["a@x.com", "c@x.com"], "主力")


# ---- 迁移 ----

def test_migration_adds_group_window_chatgpt_and_run_columns(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, domain TEXT,
          status TEXT, started_at TEXT, finished_at TEXT, output_dir TEXT);
        CREATE TABLE accounts (email TEXT UNIQUE, domain TEXT, created_at TEXT, expires_at TEXT,
          mailbox_id TEXT, last_run_id INTEGER, status TEXT);
        INSERT INTO accounts(email, status) VALUES ('old@x.com', 'success');
    """)
    raw.commit()
    raw.close()
    conn = db.init_db(path)
    acols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    assert {"group_name", "okbrowser_window_id", "chatgpt_status", "chatgpt_session_token",
            "chatgpt_access_token", "chatgpt_last_run_id", "chatgpt_claimed_at"} <= acols
    rcols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert {"platform", "group_name"} <= rcols
    assert db.get_account(conn, "old@x.com")["status"] == "success"
    rid = db.create_run(conn, "", "x.com", "", "t", platform="chatgpt", group_name="g")
    assert db.get_run(conn, rid)["platform"] == "chatgpt"


# ---- 分组 ----

def test_group_counts_rename_and_batch_set(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _seed(conn)
    counts, ungrouped = db.group_counts(conn)
    assert counts == {"主力": 2} and ungrouped == 1
    assert db.rename_group(conn, "主力", "备用") == 2
    assert db.set_accounts_group(conn, ["b@x.com", "nope@x.com"], "备用") == 1
    assert db.group_counts(conn) == ({"备用": 3}, 0)


def test_normalize_group_rules():
    assert db.normalize_group("  主力 ") == "主力"
    for bad in ("x" * 65, db.UNGROUPED, "a/b"):
        try:
            db.normalize_group(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_groups_api_crud_and_batch(tmp_path):
    app = _app(tmp_path)
    _seed(app.state.cr.conn)
    c = _login(app)

    assert c.post("/api/groups", json={"name": " 空分组 "}).status_code == 201
    assert load_config(tmp_path / "config.yaml").account_groups == ("空分组",)
    body = c.get("/api/groups").json()
    assert body["groups"] == [{"name": "空分组", "count": 0}, {"name": "主力", "count": 2}]
    assert body["ungrouped_count"] == 1

    r = c.post("/api/accounts/batch-group", json={"emails": ["b@x.com"], "group_name": "空分组"})
    assert r.json()["updated"] == 1

    r = c.patch("/api/groups/空分组", json={"new_name": "新号"})
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert load_config(tmp_path / "config.yaml").account_groups == ("新号",)
    assert db.get_account(app.state.cr.conn, "b@x.com")["group_name"] == "新号"
    assert c.patch("/api/groups/不存在", json={"new_name": "x"}).status_code == 404

    assert c.delete("/api/groups/新号?clear_accounts=false").status_code == 409
    r = c.delete("/api/groups/新号")
    assert r.json()["cleared"] == 1
    assert load_config(tmp_path / "config.yaml").account_groups == ()
    assert db.get_account(app.state.cr.conn, "b@x.com")["group_name"] == ""

    assert c.post("/api/accounts/batch-group", json={"emails": ["a@x.com"]}).status_code == 400
    assert c.post("/api/groups", json={"name": "a/b"}).status_code == 400


def test_accounts_batch_proxy(tmp_path):
    app = _app(
        tmp_path,
        saved_proxies=[
            {"id": "p1", "name": "美西节点", "url": "socks5://u:p@us.example:1080"},
        ],
    )
    _seed(app.state.cr.conn)
    c = _login(app)

    # 通过代理池 proxy_id 批量调整代理
    r = c.post("/api/accounts/batch-proxy", json={
        "emails": ["a@x.com", "b@x.com"], "proxy_id": "p1",
    })
    assert r.status_code == 200
    assert r.json() == {
        "ok": True, "updated": 2, "proxy": "socks5://u:p@us.example:1080",
    }
    assert db.get_account(app.state.cr.conn, "a@x.com")["proxy"] == "socks5://u:p@us.example:1080"
    assert db.get_account(app.state.cr.conn, "b@x.com")["proxy"] == "socks5://u:p@us.example:1080"

    # 通过自定义 proxy URL 批量调整代理
    r = c.post("/api/accounts/batch-proxy", json={
        "emails": ["a@x.com"], "proxy": "http://127.0.0.1:7890",
    })
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert db.get_account(app.state.cr.conn, "a@x.com")["proxy"] == "http://127.0.0.1:7890"

    # 清除代理（直连）
    r = c.post("/api/accounts/batch-proxy", json={
        "emails": ["a@x.com", "b@x.com"], "proxy": "",
    })
    assert r.status_code == 200 and r.json()["updated"] == 2
    assert db.get_account(app.state.cr.conn, "a@x.com")["proxy"] == ""

    # 校验错误
    assert c.post("/api/accounts/batch-proxy", json={"emails": []}).status_code == 400
    assert c.post("/api/accounts/batch-proxy", json={"emails": ["a@x.com"]}).status_code == 400
    assert c.post("/api/accounts/batch-proxy",
                  json={"emails": ["a@x.com"], "proxy_id": "missing"}).status_code == 400
    assert c.post("/api/accounts/batch-proxy",
                  json={"emails": ["a@x.com"], "proxy": "ftp://bad:1"}).status_code == 400


def test_account_create_and_patch_group(tmp_path):
    app = _app(tmp_path)
    c = _login(app)
    r = c.post("/api/accounts", json={"email": "n@x.com", "group_name": "  主力 ",
                                      "okbrowser_window_id": " 1829304 ",
                                      "chatgpt_password": " pw with space "})
    assert r.status_code == 201
    row = r.json()
    assert row["group_name"] == "主力" and row["okbrowser_window_id"] == "1829304"
    assert row["chatgpt_password"] == " pw with space "
    r = c.patch("/api/accounts/n@x.com", json={"group_name": "", "chatgpt_access_token": "at"})
    assert r.json()["group_name"] == "" and r.json()["chatgpt_access_token"] == "at"


def test_import_assigns_group(tmp_path):
    app = _app(tmp_path)
    c = _login(app)
    r = c.post("/api/accounts/import", json={
        "text": "u@x.com----sk-ant-sid01-aaaaaaaaaaaaaaaaaaaa", "check": False,
        "group_name": "导入批次",
    })
    assert r.json()["summary"]["created"] == 1
    assert db.get_account(app.state.cr.conn, "u@x.com")["group_name"] == "导入批次"


# ---- 导出 / 领取 ----

def test_filter_rows_by_group_and_platform(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _seed(conn)
    rows = db.list_accounts(conn)
    emails = lambda rs: sorted(r["email"] for r in rs)  # noqa: E731
    assert emails(export.filter_rows(rows, group="主力")) == ["a@x.com", "c@x.com"]
    assert emails(export.filter_rows(rows, group=export.UNGROUPED)) == ["b@x.com"]
    assert emails(export.filter_rows(rows, platform="chatgpt")) == ["a@x.com", "c@x.com"]
    assert emails(export.filter_rows(rows, platform="claude", group="主力")) == ["a@x.com"]
    assert emails(export.filter_rows(rows, platform="chatgpt", status="success")) == [
        "a@x.com", "c@x.com"]


def test_claim_by_platform_keeps_separate_marks(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _seed(conn)
    got = db.claim_account(conn, "t1", platform="chatgpt")
    assert got["email"] == "a@x.com" and got["chatgpt_claimed_at"] == "t1"
    assert not got["claimed_at"]
    # ChatGPT 侧领走后，Claude 侧仍可领取同一邮箱
    assert db.claim_account(conn, "t2", group="主力")["email"] == "a@x.com"
    assert db.claim_account(conn, "t3", platform="chatgpt")["email"] == "c@x.com"
    assert db.claim_account(conn, "t4", platform="chatgpt") is None
    assert db.count_claimable(conn, group=db.UNGROUPED) == 1


def test_open_api_export_and_claim_with_group_platform(tmp_path):
    app = _app(tmp_path)
    _seed(app.state.cr.conn)
    c = TestClient(app)
    r = c.get("/api/v1/accounts/export?format=json&platform=chatgpt&group=主力"
              "&fields=email,chatgpt_access_token,group_name", headers=H)
    assert r.status_code == 200
    assert r.json() == [
        {"email": "c@x.com", "chatgpt_access_token": "at-c", "group_name": "主力"},
        {"email": "a@x.com", "chatgpt_access_token": "", "group_name": "主力"},
    ]
    assert c.get("/api/v1/accounts/export?platform=nope", headers=H).status_code == 400

    r = c.post("/api/v1/accounts/claim?platform=chatgpt&group=主力", headers=H)
    assert r.status_code == 200
    assert r.json()["email"] == "a@x.com" and r.json()["remaining"] == 1
    r = c.post(f"/api/v1/accounts/claim?group={db.UNGROUPED}", headers=H)
    assert r.json()["email"] == "b@x.com"


def test_open_api_register_validates_platform_and_group(tmp_path, monkeypatch):
    import claude_register.flow as flow

    monkeypatch.setattr(flow, "run", lambda **kw: None)
    c = TestClient(_app(tmp_path))
    assert c.post("/api/v1/register", json={"platform": 1}, headers=H).status_code == 400
    assert c.post("/api/v1/register", json={"platform": "gemini"}, headers=H).status_code == 400
    assert c.post("/api/v1/register", json={"group_name": "a/b"}, headers=H).status_code == 400
