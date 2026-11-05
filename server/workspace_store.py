"""Platform records and workflow, backed by the existing mailbox/credential store."""
from __future__ import annotations

import json

from claude_register.chatgpt_check import verified_saved_plan
from server import db

PLATFORMS = ("claude", "chatgpt")
STAGES = ("register", "detect", "library")
STATUSES = ("normal", "banned", "disabled", "other")
CLAUDE_FIELDS = ("password", "session_key", *db.OAUTH_FIELDS)
GPT_FIELDS = db.CHATGPT_FIELDS

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_migrations (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS workspace_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL COLLATE NOCASE,
    platform TEXT NOT NULL CHECK(platform IN ('claude','chatgpt','unassigned')),
    stage TEXT NOT NULL CHECK(stage IN ('register','detect','library')),
    management_status TEXT NOT NULL DEFAULT 'normal',
    status_reason TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '',
    proxy TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT '',
    check_status TEXT NOT NULL DEFAULT 'unchecked',
    checked_at TEXT NOT NULL DEFAULT '',
    check_detail TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    plan_checked_at TEXT NOT NULL DEFAULT '',
    subscription_expires_at TEXT NOT NULL DEFAULT '',
    subscription_checked_at TEXT NOT NULL DEFAULT '',
    org_uuid TEXT NOT NULL DEFAULT '',
    subscription_kyc TEXT NOT NULL DEFAULT 'unknown',
    portal_kyc TEXT NOT NULL DEFAULT 'unknown',
    metadata TEXT NOT NULL DEFAULT '{}',
    UNIQUE(platform, email)
);
CREATE INDEX IF NOT EXISTS workspace_stage_idx ON workspace_accounts(platform, stage);
CREATE TABLE IF NOT EXISTS workspace_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
    old_status TEXT NOT NULL, new_status TEXT NOT NULL, reason TEXT NOT NULL,
    changed_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_status_idx ON workspace_status_history(account_id, id);
CREATE TRIGGER IF NOT EXISTS workspace_mailbox_rename AFTER UPDATE OF email ON accounts
BEGIN
    UPDATE workspace_accounts SET email=NEW.email WHERE email=OLD.email COLLATE BINARY;
END;
CREATE TRIGGER IF NOT EXISTS workspace_mailbox_delete AFTER DELETE ON accounts
BEGIN
    DELETE FROM workspace_accounts WHERE email=OLD.email COLLATE BINARY;
END;
CREATE TRIGGER IF NOT EXISTS workspace_account_delete AFTER DELETE ON workspace_accounts
BEGIN
    DELETE FROM workspace_status_history WHERE account_id=OLD.id;
END;
"""


def migrate(conn) -> None:
    conn.executescript(_SCHEMA)
    db._add_missing_cols(conn, "workspace_accounts", (
        ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ("subscription_expires_at", "TEXT NOT NULL DEFAULT ''"),
        ("subscription_checked_at", "TEXT NOT NULL DEFAULT ''"),
    ))
    with conn:
        if conn.execute("INSERT OR IGNORE INTO workspace_migrations VALUES('initial')").rowcount:
            _migrate_legacy_accounts(conn)
        if conn.execute("INSERT OR IGNORE INTO workspace_migrations VALUES('chatgpt_saved_plan')").rowcount:
            _backfill_chatgpt_plans(conn)


def _migrate_legacy_accounts(conn) -> None:
    for row in db.list_accounts(conn):
        if not row["email"]:
            continue
        found = False
        for platform in PLATFORMS:
            prefix = "chatgpt_" if platform == "chatgpt" else ""
            status = row.get(prefix + "status") or ""
            has_creds = bool(row.get("session_key") or row.get("access_token")
                             or row.get("refresh_token")) if not prefix else bool(
                row.get("chatgpt_session_token") or row.get("chatgpt_access_token"))
            check = row.get(prefix + "check_status") or "unchecked"
            banned = status == "banned" or check == "banned"
            run_id = row.get(prefix + "last_run_id")
            run = db.get_run(conn, run_id) if run_id else None
            evidence = run and (run.get("platform") or "claude") in (platform, "both")
            if not (has_creds or banned or evidence or (prefix and status)):
                continue
            found = True
            if find(conn, row["email"], platform):
                continue  # Legacy rows may differ only by casing; retain the raw row.
            stage = "library" if has_creds or banned or status == "success" else "register"
            stamp = row.get(prefix + "checked_at") or ""
            account_id = _insert(conn, row, platform, stage, group=row.get("group_name"),
                                 proxy=row.get("proxy"), now=row.get("created_at") or "",
                                 note=row.get("display_name") or "")
            conn.execute(
                "UPDATE workspace_accounts SET check_status=?,checked_at=?,org_uuid=?,registered_at=? WHERE id=?",
                ("banned" if banned else check, stamp, (row.get("org_uuid") or "") if not prefix else "",
                 (run.get("finished_at") or "") if run and status == "success" else "", account_id),
            )
            if banned:
                _status(conn, account_id, "normal", "banned", "旧数据已标记封禁",
                        stamp or row.get("created_at") or "", "migration")
        if not found and not find(conn, row["email"], "unassigned"):
            _insert(conn, row, "unassigned", "register", group=row.get("group_name"),
                    proxy=row.get("proxy"), now=row.get("created_at") or "", note=row.get("display_name") or "")


def _backfill_chatgpt_plans(conn) -> None:
    rows = conn.execute(
        """SELECT w.id, w.email, w.checked_at, a.chatgpt_session_json, a.chatgpt_access_token
           FROM workspace_accounts w JOIN accounts a ON a.email=w.email COLLATE BINARY
           WHERE w.platform='chatgpt' AND w.stage='library' AND w.check_status='alive'
             AND w.checked_at<>'' AND w.plan=''""",
    )
    for row in rows:
        plan = verified_saved_plan(row["chatgpt_session_json"], access_token=row["chatgpt_access_token"],
                                   expected_email=row["email"])
        if plan:
            conn.execute("UPDATE workspace_accounts SET plan=?,plan_checked_at=? WHERE id=?",
                         (plan, row["checked_at"], row["id"]))


def _insert(conn, row, platform, stage, *, group, proxy, now, note="") -> int:
    return conn.execute(
        "INSERT INTO workspace_accounts(email,platform,stage,group_name,proxy,note,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (row["email"], platform, stage, group or "", proxy or "", note, now),
    ).lastrowid


def mailbox(conn, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE email=? COLLATE NOCASE ORDER BY rowid LIMIT 1",
                       (email,)).fetchone()
    return dict(row) if row else None


def find(conn, email: str, platform: str) -> dict | None:
    row = conn.execute("SELECT * FROM workspace_accounts WHERE platform=? AND email=?",
                       (platform, email)).fetchone()
    return dict(row) if row else None


def get(conn, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM workspace_accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def list_records(conn, platform, stage) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT w.*, COALESCE(a.okbrowser_window_id,'') AS window_id,
           CASE w.platform WHEN 'claude' THEN a.last_run_id WHEN 'chatgpt' THEN a.chatgpt_last_run_id END AS run_id,
           COALESCE(NULLIF(r.status, ''), CASE w.platform WHEN 'claude' THEN COALESCE(a.status,'')
             WHEN 'chatgpt' THEN COALESCE(a.chatgpt_status,'') ELSE '' END) AS registration_status,
           CASE w.platform WHEN 'claude' THEN COALESCE(a.claimed_at,'') WHEN 'chatgpt' THEN COALESCE(a.chatgpt_claimed_at,'') ELSE '' END AS claimed_at,
           CASE w.platform WHEN 'claude' THEN COALESCE(a.session_key,'') <> ''
             WHEN 'chatgpt' THEN COALESCE(a.chatgpt_session_token,'') <> '' OR COALESCE(a.chatgpt_access_token,'') <> '' ELSE 0 END AS has_session
           FROM workspace_accounts w JOIN accounts a ON a.email=w.email COLLATE BINARY
           LEFT JOIN runs r ON r.id=CASE w.platform WHEN 'claude' THEN a.last_run_id WHEN 'chatgpt' THEN a.chatgpt_last_run_id END
             AND COALESCE(r.platform,'claude')<>'both'
           WHERE w.platform=? AND w.stage=? ORDER BY w.id DESC""", (platform, stage),
    )]


def projected(conn, record) -> dict:
    """For existing integrations: keep shared mailbox data and only this platform's credentials."""
    row = db.get_account(conn, record["email"]) or {}
    drop = GPT_FIELDS if record["platform"] == "claude" else (
        *CLAUDE_FIELDS, "status", "last_run_id", "check_status", "checked_at", "claimed_at",
    )
    for field in drop:
        row.pop(field, None)
    row["proxy"] = record["proxy"]
    row["group_name"] = record["group_name"]
    return row


def metadata(record) -> dict:
    return json.loads(record["metadata"])


def history(conn, account_id) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT old_status,new_status,reason,changed_at,source FROM workspace_status_history "
        "WHERE account_id=? ORDER BY id DESC", (account_id,),
    )]


def _status(conn, account_id, old, status, reason, now, source) -> None:
    conn.execute("UPDATE workspace_accounts SET management_status=?,status_reason=? WHERE id=?",
                 (status, reason, account_id))
    conn.execute(
        "INSERT INTO workspace_status_history(account_id,old_status,new_status,reason,changed_at,source)"
        " VALUES(?,?,?,?,?,?)", (account_id, old, status, reason, now, source),
    )


def set_status(conn, record, status, now) -> None:
    if record["management_status"] != status:
        _status(conn, record["id"], record["management_status"], status, "", now, "manual")


def _update_fields(conn, table, where, identity, values) -> None:
    if values:
        conn.execute(f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in values)
                     + f" WHERE {where}=?", (*values.values(), identity))


def _clear_detection(conn, account_id) -> None:
    _update_fields(conn, "workspace_accounts", "id", account_id,
                   dict(check_status="unchecked", checked_at="", check_detail="",
                        subscription_expires_at="", subscription_checked_at="",
                        subscription_kyc="unknown", portal_kyc="unknown"))


def record_registration(conn, email, platform, outcome, *, group, proxy, now, got_session=False):
    row = mailbox(conn, email)
    if not row:
        return
    record = find(conn, email, platform)
    unassigned = find(conn, email, "unassigned")
    next_stage = "detect" if got_session or outcome == "banned" else "register"
    with conn:
        if not record:
            if unassigned:
                account_id = unassigned["id"]
                conn.execute("UPDATE workspace_accounts SET platform=?,stage=? WHERE id=?",
                             (platform, next_stage, account_id))
            else:
                account_id = _insert(conn, row, platform, next_stage, group=group or row.get("group_name"),
                                     proxy=proxy, now=now, note=row.get("display_name") or "")
        else:
            account_id = record["id"]
        current = get(conn, account_id)
        updates = {"proxy": proxy}
        if group:
            updates["group_name"] = group
        if next_stage == "detect" and current["stage"] == "register":
            updates["stage"] = "detect"
        if got_session:
            if not current["registered_at"]:
                updates["registered_at"] = now
            _clear_detection(conn, account_id)
        _update_fields(conn, "workspace_accounts", "id", account_id, updates)
        if outcome == "banned":
            apply_detection(conn, account_id, {"status": "banned", "detail": "注册时平台明确返回封禁"},
                            now=now, source="registration")


class DuplicateAccount(Exception):
    pass


def add(conn, email, platform, stage, *, group, proxy, note, creds, shared, now) -> int:
    with conn:
        if find(conn, email, platform):
            raise DuplicateAccount
        # Also protect credentials from earlier versions of the write API.
        row = mailbox(conn, email)
        token_field = "session_key" if platform == "claude" else "chatgpt_session_token"
        if row and (row.get(token_field) or (platform == "chatgpt" and row.get("chatgpt_access_token"))):
            raise DuplicateAccount
        orphan = find(conn, email, "unassigned")
        if not row:
            conn.execute("INSERT INTO accounts(email,domain,created_at) VALUES(?,?,?)",
                         (email, email.split("@", 1)[-1], now))
            row = db.get_account(conn, email)
        shared = {key: val for key, val in shared.items() if val and not row.get(key)}
        status_field = "status" if platform == "claude" else "chatgpt_status"
        _update_fields(conn, "accounts", "email", row["email"], {
            **shared, **creds, status_field: "success" if stage != "register" else "pending",
        })
        if orphan:
            conn.execute(
                "UPDATE workspace_accounts SET platform=?,stage=?,group_name=?,proxy=?,note=? WHERE id=?",
                (platform, stage, group or orphan["group_name"], proxy or orphan["proxy"],
                 note or orphan["note"], orphan["id"]),
            )
            return orphan["id"]
        return _insert(conn, row, platform, stage, group=group, proxy=proxy, note=note, now=now)


def assign(conn, record, platform) -> None:
    with conn:
        if find(conn, record["email"], platform):
            raise DuplicateAccount
        conn.execute("UPDATE workspace_accounts SET platform=? WHERE id=? AND platform='unassigned'",
                     (platform, record["id"]))


def edit(conn, record, fields, creds, shared) -> None:
    row = db.get_account(conn, record["email"])
    creds = {k: v for k, v in creds.items() if row.get(k) != v}
    auth_changed = any(k in creds for k in ("session_key", "chatgpt_session_token", "chatgpt_access_token"))
    if "session_key" in creds:
        creds = {**dict.fromkeys(db.OAUTH_FIELDS, ""), **creds, "check_status": "", "checked_at": ""}
    if "chatgpt_session_token" in creds:
        creds = {**dict.fromkeys(("chatgpt_access_token", "chatgpt_token_expires_at",
                                 "chatgpt_account_id", "chatgpt_session_json"), ""), **creds}
    if auth_changed and record["platform"] == "chatgpt":
        creds.update(chatgpt_check_status="", chatgpt_checked_at="")
    with conn:
        _update_fields(conn, "accounts", "email", row["email"], {**shared, **creds})
        _update_fields(conn, "workspace_accounts", "id", record["id"], fields)
        if auth_changed:
            _clear_detection(conn, record["id"])


def apply_detection(conn, account_id, result, *, now, source="detection") -> None:
    """Caller owns the transaction. A later check cannot move a library account back."""
    record = get(conn, account_id)
    if not record:
        return
    status = result["status"]
    updates = dict(check_status=status, checked_at=now, check_detail=result.get("detail", ""),
                   subscription_kyc=result.get("subscription_kyc", "unknown"),
                   portal_kyc=result.get("portal_kyc", "unknown"))
    if status in ("alive", "banned") and record["stage"] != "register":
        updates["stage"] = "library"
    if status == "alive":
        plan = result.get("plan") or ""
        if plan or record["platform"] != "chatgpt":
            updates.update(plan=plan, plan_checked_at=now)
        if plan != "free" and "subscription_expires_at" in result:
            updates.update(subscription_expires_at=result["subscription_expires_at"], subscription_checked_at=now)
        else:
            updates.update(subscription_expires_at="", subscription_checked_at="")
        updates.update(org_uuid=result.get("org_uuid", ""), metadata=json.dumps(result.get("metadata", {})))
        if result.get("registered_at"):
            updates["registered_at"] = result["registered_at"]
    _update_fields(conn, "workspace_accounts", "id", account_id, updates)
    if status == "banned" and record["management_status"] == "normal":
        _status(conn, account_id, "normal", "banned", updates["check_detail"], now, source)
    prefix = "chatgpt_" if record["platform"] == "chatgpt" else ""
    legacy = {prefix + "check_status": status, prefix + "checked_at": now}
    if status == "alive":
        if record["stage"] != "register":
            legacy[prefix + "status"] = "success"
        if prefix:
            legacy.update({k: v for k, v in result.get("fresh", {}).items() if k in GPT_FIELDS})
        elif result.get("org_uuid"):
            legacy["org_uuid"] = result["org_uuid"]
    _update_fields(conn, "accounts", "email", record["email"], legacy)


def save_recovered_session(conn, record, creds, result, *, now) -> None:
    if record["platform"] == "claude":
        updates = {"session_key": creds["session_key"]}
    else:
        fields = ("chatgpt_session_token", "chatgpt_access_token", "chatgpt_token_expires_at",
                  "chatgpt_account_id", "chatgpt_session_json")
        updates = {key: creds.get(key) or "" for key in fields}
    with conn:
        _update_fields(conn, "accounts", "email", record["email"], updates)
        apply_detection(conn, record["id"], result, now=now, source="okbrowser")


def delete(conn, record) -> None:
    conn.execute("DELETE FROM workspace_accounts WHERE id=?", (record["id"],))
    remaining = conn.execute("SELECT 1 FROM workspace_accounts WHERE email=?", (record["email"],)).fetchone()
    row = db.get_account(conn, record["email"]) or {}
    other_creds = (row.get("chatgpt_session_token") or row.get("chatgpt_access_token")
                   if record["platform"] == "claude" else row.get("session_key") or row.get("access_token"))
    if remaining is None and not other_creds:
        conn.execute("DELETE FROM accounts WHERE email=?", (record["email"],))
    elif record["platform"] != "unassigned":
        cols = GPT_FIELDS + ("chatgpt_claimed_at",) if record["platform"] == "chatgpt" else (
            *CLAUDE_FIELDS, "status", "last_run_id", "check_status", "checked_at", "claimed_at",
        )
        _update_fields(conn, "accounts", "email", record["email"], dict.fromkeys(cols, None))


def eligible_for_sync(conn, email) -> bool:
    record = find(conn, email, "claude")
    return record is None or (record["stage"] == "library" and record["management_status"] == "normal")
