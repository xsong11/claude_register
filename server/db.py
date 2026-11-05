"""SQLite 数据访问：runs / accounts。纯数据层，无业务逻辑。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT, domain TEXT, status TEXT,
  started_at TEXT, finished_at TEXT, output_dir TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
  email TEXT UNIQUE, domain TEXT, created_at TEXT, expires_at TEXT,
  mailbox_id TEXT, last_run_id INTEGER, status TEXT,
  password TEXT, session_key TEXT, proxy TEXT, display_name TEXT,
  mail_key TEXT, mail_base_url TEXT,
  check_status TEXT, checked_at TEXT, claimed_at TEXT,
  access_token TEXT, refresh_token TEXT, oauth_expires_at TEXT, oauth_scope TEXT,
  org_uuid TEXT, account_uuid TEXT, oauth_at TEXT
);
CREATE TABLE IF NOT EXISTS openai_admin_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, admin_key TEXT, org_id TEXT,
  credit_usd REAL, credit_since TEXT,
  status TEXT, last_error TEXT,
  cost_month_usd REAL, cost_since_usd REAL, refreshed_at TEXT,
  created_at TEXT
);
"""

_ACCOUNT_EXTRA_COLS = (
    ("password", "TEXT"),
    ("session_key", "TEXT"),
    ("proxy", "TEXT"),
    ("display_name", "TEXT"),
    ("mail_key", "TEXT"),
    ("mail_base_url", "TEXT"),
    ("check_status", "TEXT"),
    ("checked_at", "TEXT"),
    ("claimed_at", "TEXT"),
    # Claude OAuth 令牌（sessionKey 自动授权得到）
    ("access_token", "TEXT"),
    ("refresh_token", "TEXT"),
    ("oauth_expires_at", "TEXT"),
    ("oauth_scope", "TEXT"),
    ("org_uuid", "TEXT"),
    ("account_uuid", "TEXT"),
    ("oauth_at", "TEXT"),
    # 分组（空串 = 未分组）与 OKBrowser 指纹窗口绑定
    ("group_name", "TEXT"),
    ("okbrowser_window_id", "TEXT"),
    # ChatGPT 维度；原有 status / session_key / check_status 等仍代表 Claude
    ("chatgpt_status", "TEXT"),
    ("chatgpt_password", "TEXT"),
    ("chatgpt_session_token", "TEXT"),
    ("chatgpt_access_token", "TEXT"),
    ("chatgpt_token_expires_at", "TEXT"),
    ("chatgpt_account_id", "TEXT"),
    ("chatgpt_check_status", "TEXT"),
    ("chatgpt_checked_at", "TEXT"),
    ("chatgpt_last_run_id", "INTEGER"),
    ("chatgpt_claimed_at", "TEXT"),
    # "1" = chatgpt_password 已在 ChatGPT 设置里生效，可用邮箱 + 密码登录
    ("chatgpt_password_set", "TEXT"),
    # 最近一次 /api/auth/session 的完整响应 JSON（注册或检测时刷新）
    ("chatgpt_session_json", "TEXT"),
)

_RUN_EXTRA_COLS = (
    ("platform", "TEXT"),
    ("group_name", "TEXT"),
)


def _add_missing_cols(conn: sqlite3.Connection, table: str, cols) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, typ in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _migrate_accounts(conn: sqlite3.Connection) -> None:
    """旧库补列：_ACCOUNT_EXTRA_COLS 里缺的列一律 ALTER TABLE 补上。"""
    _add_missing_cols(conn, "accounts", _ACCOUNT_EXTRA_COLS)


def _migrate_runs(conn: sqlite3.Connection) -> None:
    _add_missing_cols(conn, "runs", _RUN_EXTRA_COLS)


def init_db(path: Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_accounts(conn)
    _migrate_runs(conn)
    conn.commit()
    return conn


PLATFORMS = ("claude", "chatgpt", "both")


def create_run(conn, email, domain, output_dir, now, *, platform: str = "claude",
               group_name: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO runs(email,domain,status,started_at,output_dir,platform,group_name) "
        "VALUES(?,?,'running',?,?,?,?)",
        (email, domain, now, output_dir, platform or "claude", group_name or ""),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, now) -> None:
    conn.execute("UPDATE runs SET status=?, finished_at=? WHERE id=?",
                 (status, now, run_id))
    conn.commit()


def _row(r) -> dict | None:
    return dict(r) if r is not None else None


def list_runs(conn, limit=50, offset=0) -> list[dict]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?",
                        (limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_run(conn, run_id) -> dict | None:
    return _row(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def active_run(conn) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone())


def mark_stale_running_as_failed(conn) -> int:
    cur = conn.execute("UPDATE runs SET status='failed' WHERE status='running'")
    conn.commit()
    return cur.rowcount


def upsert_account(
    conn,
    email,
    domain,
    expires_at,
    mailbox_id,
    last_run_id,
    status,
    *,
    password: str = "",
    session_key: str = "",
    proxy: str = "",
    display_name: str = "",
    created_at: str = "",
    mail_key: str = "",
    mail_base_url: str = "",
) -> None:
    conn.execute(
        "INSERT INTO accounts(email,domain,created_at,expires_at,mailbox_id,last_run_id,status,"
        "password,session_key,proxy,display_name,mail_key,mail_base_url) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET domain=excluded.domain, "
        "expires_at=excluded.expires_at, mailbox_id=excluded.mailbox_id, "
        "last_run_id=excluded.last_run_id, status=excluded.status, "
        "password=excluded.password, session_key=excluded.session_key, "
        "proxy=excluded.proxy, display_name=excluded.display_name, "
        "mail_key=excluded.mail_key, mail_base_url=excluded.mail_base_url",
        (
            email,
            domain,
            created_at or "",
            expires_at,
            mailbox_id,
            last_run_id,
            status,
            password or "",
            session_key or "",
            proxy or "",
            display_name or "",
            mail_key or "",
            mail_base_url or "",
        ),
    )
    conn.commit()


def list_accounts(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM accounts ORDER BY rowid DESC").fetchall()
    return [dict(r) for r in rows]


def get_account(conn, email) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


# 面板可手工编辑的字段；email/status 等由注册流程维护，不开放
ACCOUNT_EDITABLE_FIELDS = (
    "password", "session_key", "proxy", "display_name", "mail_key", "mail_base_url",
    "group_name", "okbrowser_window_id",
    "chatgpt_password", "chatgpt_session_token", "chatgpt_access_token",
)


class AccountExists(Exception):
    """新建 / 改名时目标邮箱已被其他账号占用。"""


def create_account(conn, email, fields: dict, *, created_at: str,
                   status: str = "success") -> dict:
    """面板手动新增一个账号；邮箱已存在抛 AccountExists，不覆盖已有数据。"""
    vals = {k: str(fields.get(k) or "") for k in ACCOUNT_EDITABLE_FIELDS}
    cols = ("email", "domain", "created_at", "expires_at", "mailbox_id", "status",
            *vals.keys())
    params = (email, email.split("@", 1)[-1], created_at, "", "", status, *vals.values())
    try:
        conn.execute(
            f"INSERT INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            params,
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise AccountExists(email) from None
    conn.commit()
    return get_account(conn, email)


def rename_account(conn, old_email, new_email) -> bool:
    """修改账号邮箱（同时更新 domain）；目标已被占用抛 AccountExists。"""
    if old_email == new_email:
        return get_account(conn, old_email) is not None
    if get_account(conn, new_email) is not None:
        raise AccountExists(new_email)
    try:
        cur = conn.execute(
            "UPDATE accounts SET email=?, domain=? WHERE email=?",
            (new_email, new_email.split("@", 1)[-1], old_email),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        raise AccountExists(new_email) from None
    conn.commit()
    return cur.rowcount > 0


def update_account_fields(conn, email, fields: dict) -> bool:
    """仅更新 ACCOUNT_EDITABLE_FIELDS 里的字段，返回是否有行被更新。"""
    sets = {k: str(v or "") for k, v in fields.items() if k in ACCOUNT_EDITABLE_FIELDS}
    if not sets:
        return False
    sql = "UPDATE accounts SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE email=?"
    cur = conn.execute(sql, (*sets.values(), email))
    conn.commit()
    return cur.rowcount > 0


def update_account_check(conn, email, status, checked_at) -> bool:
    """更新单个账号的存活检测结果，返回是否有行被更新。"""
    cur = conn.execute(
        "UPDATE accounts SET check_status=?, checked_at=? WHERE email=?",
        (status, checked_at, email),
    )
    conn.commit()
    return cur.rowcount > 0


def update_account_org(conn, email, org_uuid: str) -> bool:
    """只写组织 UUID（检测时从组织列表里取到的），不动 OAuth 令牌字段。"""
    cur = conn.execute("UPDATE accounts SET org_uuid=? WHERE email=?", (org_uuid, email))
    conn.commit()
    return cur.rowcount > 0


OAUTH_FIELDS = (
    "access_token", "refresh_token", "oauth_expires_at", "oauth_scope",
    "org_uuid", "account_uuid", "oauth_at",
)


def update_account_oauth(conn, email, values: dict) -> bool:
    """写入 / 覆盖 OAuth 令牌字段（只认 OAUTH_FIELDS），返回是否有行被更新。"""
    sets = {k: str(values.get(k) or "") for k in OAUTH_FIELDS}
    sql = "UPDATE accounts SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE email=?"
    cur = conn.execute(sql, (*sets.values(), email))
    conn.commit()
    return cur.rowcount > 0


CHATGPT_FIELDS = (
    "chatgpt_status", "chatgpt_password", "chatgpt_session_token", "chatgpt_access_token",
    "chatgpt_token_expires_at", "chatgpt_account_id", "chatgpt_check_status",
    "chatgpt_checked_at", "chatgpt_last_run_id", "chatgpt_password_set", "chatgpt_session_json",
)


def update_account_chatgpt(conn, email, values: dict) -> bool:
    """只更新 values 里给出的 ChatGPT 字段（只认 CHATGPT_FIELDS），不动 Claude 字段。"""
    sets = {}
    for k, v in values.items():
        if k not in CHATGPT_FIELDS:
            continue
        sets[k] = v if k == "chatgpt_last_run_id" else str(v or "")
    if not sets:
        return False
    sql = "UPDATE accounts SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE email=?"
    cur = conn.execute(sql, (*sets.values(), email))
    conn.commit()
    return cur.rowcount > 0


# ---- 分组 ----

GROUP_NAME_MAX = 64
UNGROUPED = "__ungrouped__"  # 筛选参数里表示「仅未分组」


def normalize_group(name) -> str:
    """去首尾空白；超长或用了保留名抛 ValueError。"""
    text = str(name or "").strip()
    if len(text) > GROUP_NAME_MAX:
        raise ValueError(f"分组名最长 {GROUP_NAME_MAX} 个字符")
    if text == UNGROUPED:
        raise ValueError("该分组名为保留字")
    if "/" in text:
        raise ValueError("分组名不能包含 /")
    return text


def group_counts(conn) -> tuple[dict[str, int], int]:
    """返回 ({分组名: 账号数}, 未分组账号数)。"""
    rows = conn.execute(
        "SELECT COALESCE(group_name,'') AS g, COUNT(*) AS n FROM accounts GROUP BY g"
    ).fetchall()
    counts = {r["g"]: r["n"] for r in rows}
    ungrouped = counts.pop("", 0)
    return counts, ungrouped


def set_accounts_group(conn, emails, group_name: str) -> int:
    """批量设置分组（空串 = 移出分组），返回实际更新的行数。"""
    emails = list(dict.fromkeys(emails))
    updated = 0
    for i in range(0, len(emails), 500):
        chunk = emails[i:i + 500]
        cur = conn.execute(
            f"UPDATE accounts SET group_name=? WHERE email IN ({','.join('?' * len(chunk))})",
            (group_name, *chunk),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def set_accounts_proxy(conn, emails, proxy: str) -> int:
    """批量设置代理（空串 = 清除代理），返回实际更新的行数。"""
    emails = list(dict.fromkeys(emails))
    updated = 0
    for i in range(0, len(emails), 500):
        chunk = emails[i:i + 500]
        cur = conn.execute(
            f"UPDATE accounts SET proxy=? WHERE email IN ({','.join('?' * len(chunk))})",
            (proxy, *chunk),
        )
        updated += cur.rowcount
    conn.commit()
    return updated


def rename_group(conn, old: str, new: str) -> int:
    cur = conn.execute("UPDATE accounts SET group_name=? WHERE group_name=?", (new, old))
    conn.commit()
    return cur.rowcount


def _group_where(group: str | None) -> tuple[list[str], list]:
    if group is None or group == "":
        return [], []
    if group == UNGROUPED:
        return ["COALESCE(group_name,'')=''"], []
    return ["group_name=?"], [group]


# 已知不可用的检测结果：领取时默认跳过
UNUSABLE_CHECK_STATUSES = ("dead", "blocked")
_claim_lock = threading.Lock()

# 平台 → (注册状态列, 凭据条件, 检测结果列, 已获取标记列)
_CLAIM_COLUMNS = {
    "claude": ("status", "COALESCE(session_key,'')<>''", "check_status", "claimed_at"),
    "chatgpt": (
        "chatgpt_status",
        "(COALESCE(chatgpt_session_token,'')<>'' OR COALESCE(chatgpt_access_token,'')<>'')",
        "chatgpt_check_status",
        "chatgpt_claimed_at",
    ),
}


def _claimable_where(check_status: str | None, platform: str = "claude",
                     group: str | None = None) -> tuple[str, list]:
    """可领取：注册成功、有凭据、未获取；check_status 为空时跳过已知失效/被拦截的，
    指定时只取该检测结果的账号。Claude 与 ChatGPT 各有一套状态与「已获取」标记。"""
    status_col, cred_cond, check_col, claimed_col = _CLAIM_COLUMNS[platform]
    where = [f"{status_col}='success'", cred_cond, f"COALESCE({claimed_col},'')=''"]
    params: list = []
    if check_status:
        where.append(f"{check_col}=?")
        params.append(check_status)
    else:
        where.append(
            f"COALESCE({check_col},'') NOT IN ("
            + ",".join("?" * len(UNUSABLE_CHECK_STATUSES)) + ")"
        )
        params.extend(UNUSABLE_CHECK_STATUSES)
    gw, gp = _group_where(group)
    where.extend(gw)
    params.extend(gp)
    return " AND ".join(where), params


def count_claimable(conn, *, check_status: str | None = None, platform: str = "claude",
                    group: str | None = None) -> int:
    where, params = _claimable_where(check_status, platform, group)
    return conn.execute(f"SELECT COUNT(*) FROM accounts WHERE {where}", params).fetchone()[0]


def claim_account(conn, now, *, check_status: str | None = None, platform: str = "claude",
                  group: str | None = None) -> dict | None:
    """领取一个可领取的账号并打上「已获取」标记，没有返回 None。

    按入库顺序先进先出；加锁 + 条件 UPDATE，并发调用不会把同一个账号发给两个调用方。
    platform="chatgpt" 时标记 chatgpt_claimed_at，不影响该邮箱 Claude 侧能否被领取。
    """
    where, params = _claimable_where(check_status, platform, group)
    claimed_col = _CLAIM_COLUMNS[platform][3]
    sql = f"SELECT rowid, email FROM accounts WHERE {where} ORDER BY rowid ASC LIMIT 1"
    with _claim_lock:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            f"UPDATE accounts SET {claimed_col}=? "
            f"WHERE rowid=? AND COALESCE({claimed_col},'')=''",
            (now, row["rowid"]),
        )
        conn.commit()
        if cur.rowcount == 0:  # 理论上不会发生（锁内），保险起见
            return None
        return get_account(conn, row["email"])


def set_account_claimed(conn, email, claimed_at: str) -> bool:
    """面板手动标记/取消「已获取」；claimed_at 为空串表示取消。"""
    cur = conn.execute("UPDATE accounts SET claimed_at=? WHERE email=?",
                       (claimed_at or "", email))
    conn.commit()
    return cur.rowcount > 0


def delete_account(conn, email) -> bool:
    cur = conn.execute("DELETE FROM accounts WHERE email=?", (email,))
    conn.commit()
    return cur.rowcount > 0


def delete_accounts(conn, emails) -> int:
    """批量删除，返回实际删除的行数；不存在的邮箱忽略。"""
    emails = list(dict.fromkeys(emails))
    if not emails:
        return 0
    deleted = 0
    for i in range(0, len(emails), 500):  # 避开 SQLite 变量个数上限
        chunk = emails[i:i + 500]
        cur = conn.execute(
            f"DELETE FROM accounts WHERE email IN ({','.join('?' * len(chunk))})", chunk,
        )
        deleted += cur.rowcount
    conn.commit()
    return deleted


# ---- OpenAI Admin Key ----

OPENAI_KEY_FIELDS = (
    "name", "admin_key", "org_id", "credit_usd", "credit_since",
    "status", "last_error", "cost_month_usd", "cost_since_usd", "refreshed_at",
)


def list_openai_keys(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM openai_admin_keys ORDER BY id ASC").fetchall()
    return [dict(r) for r in rows]


def get_openai_key(conn, key_id) -> dict | None:
    return _row(conn.execute("SELECT * FROM openai_admin_keys WHERE id=?",
                             (key_id,)).fetchone())


def create_openai_key(conn, fields: dict, *, created_at: str) -> dict:
    vals = {k: fields[k] for k in OPENAI_KEY_FIELDS if k in fields}
    cols = (*vals.keys(), "created_at")
    cur = conn.execute(
        f"INSERT INTO openai_admin_keys({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        (*vals.values(), created_at),
    )
    conn.commit()
    return get_openai_key(conn, cur.lastrowid)


def update_openai_key(conn, key_id, fields: dict) -> bool:
    """只更新 OPENAI_KEY_FIELDS 里的字段，返回是否有行被更新。"""
    sets = {k: v for k, v in fields.items() if k in OPENAI_KEY_FIELDS}
    if not sets:
        return False
    sql = "UPDATE openai_admin_keys SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?"
    cur = conn.execute(sql, (*sets.values(), key_id))
    conn.commit()
    return cur.rowcount > 0


def delete_openai_key(conn, key_id) -> bool:
    cur = conn.execute("DELETE FROM openai_admin_keys WHERE id=?", (key_id,))
    conn.commit()
    return cur.rowcount > 0
