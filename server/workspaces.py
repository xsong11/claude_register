"""Authenticated, platform-scoped account workspace API."""
from __future__ import annotations

import asyncio
import re

from fastapi import Depends, HTTPException, Response

from claude_register.account_inspect import inspect_account
from claude_register.browser import mask_proxy, validate_proxy
from claude_register.okbrowser import OkBrowserError
from claude_register.oauth import OAuthError
from claude_register.outlook import OUTLOOK_MAIL_URL, parse_outlook_line
from server import db, export, oauth_acquire, okbrowser_ops, sk_import, workspace_resync, workspace_store as store
from server.runner import RunnerBusy

EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
CLAUDE_EDIT = ("password", "session_key")
GPT_EDIT = ("chatgpt_password", "chatgpt_session_token", "chatgpt_access_token")
SHARED_EDIT = ("mail_key", "mail_base_url")
SUMMARY_EXPORT = ("email", "group_name", "display_name", "check_status", "checked_at", "proxy", "okbrowser_window_id")
CLAUDE_EXPORT = ("session_key", "password", "access_token", "refresh_token", "org_uuid", "oauth_expires_at")
GPT_EXPORT = (*GPT_EDIT, "chatgpt_token_expires_at")
RECOVERABLE_RESULTS = ("dead", "blocked", "error")


def _platform(value, *, unassigned=False):
    if value not in store.PLATFORMS and not (unassigned and value == "unassigned"):
        raise HTTPException(400, "请选择 Claude 或 ChatGPT")
    return value


def _stage(platform, stage):
    _platform(platform, unassigned=stage == "register")
    if stage not in store.STAGES:
        raise HTTPException(404, "工作区不存在")
    return stage


def _fields(body, allowed):
    out = {}
    for key in allowed:
        if key in body:
            value = body[key]
            if not isinstance(value, str):
                raise HTTPException(400, "字段内容必须是文本")
            out[key] = value.strip() if "password" not in key else value
    return out


def _meta(body):
    data = _fields(body, ("group_name", "proxy", "note"))
    try:
        if "group_name" in data:
            data["group_name"] = db.normalize_group(data["group_name"])
        if data.get("proxy"):
            validate_proxy(data["proxy"])
        if len(data.get("note", "")) > 500:
            raise ValueError("备注最长 500 个字符")
    except ValueError as exc:
        raise HTTPException(400, str(exc) if not data.get("proxy") else "请检查代理地址或其他字段") from None
    return data


def _summary(record):
    return {**record, "metadata": store.metadata(record), "proxy": mask_proxy(record["proxy"])}


def _parse_line(line, platform, stage):
    if stage == "register":
        outlook = parse_outlook_line(line)
        if outlook:
            return outlook.email, {"password" if platform == "claude" else "chatgpt_password": outlook.password}, {
                "mail_key": outlook.mail_key, "mail_base_url": OUTLOOK_MAIL_URL,
            }
        email = line.lower()
        if EMAIL.fullmatch(email):
            return email, {}, {}
        raise ValueError("请输入邮箱地址或 Outlook 邮箱凭据")
    if platform == "claude":
        items, _ = sk_import.parse(line)
        if not items or not items[0].session_key.startswith(("sk-ant-sid01-", "sk-ant-sid02-")):
            raise ValueError("请输入 Claude sessionKey，可在同一行附上邮箱")
        item = items[0]
        return item.email or sk_import.placeholder_email(item.session_key), {"session_key": item.session_key}, {}
    parts = [s.strip() for s in line.split("----")]
    if not 2 <= len(parts) <= 3 or not EMAIL.fullmatch(parts[0]) or not any(parts[1:]):
        raise ValueError("请输入 邮箱----sessionToken，可在第三段附上 accessToken")
    return parts[0].lower(), dict(zip(("chatgpt_session_token", "chatgpt_access_token"), parts[1:])), {}


def _preview(conn, body, platform, stage):
    text = body.get("text")
    if not isinstance(text, str) or len(text) > 2_000_000:
        raise HTTPException(400, "请提供不超过 2 MB 的导入文本")
    lines = [(no, s.strip()) for no, s in enumerate(text.splitlines(), 1)
             if s.strip() and not s.strip().startswith("#")]
    if not 0 < len(lines) <= 500:
        raise HTTPException(400, "每批请输入 1–500 行")
    seen = set()
    for no, line in lines:
        try:
            email, creds, shared = _parse_line(line, platform, stage)
        except ValueError as exc:
            yield {"line": no, "result": "invalid", "detail": str(exc)}, {}, {}
            continue
        row = store.mailbox(conn, email) or {}
        existing_token = row.get("session_key") if platform == "claude" else (
            row.get("chatgpt_session_token") or row.get("chatgpt_access_token")
        )
        # Also catch a key already imported with a placeholder email.
        tokens = {k: v for k, v in creds.items() if k in (
            "session_key", "chatgpt_session_token", "chatgpt_access_token",
        ) and v}
        duplicate_key = any(conn.execute(f"SELECT 1 FROM accounts WHERE {k}=?", (v,)).fetchone()
                            for k, v in tokens.items())
        keys = {email, *tokens.items()}
        duplicate = seen.intersection(keys) or store.find(conn, email, platform) or existing_token or duplicate_key
        seen.update(keys)
        yield {"line": no, "email": email, "result": "duplicate" if duplicate else "ready"}, creds, shared


def register_routes(app, state, require_auth, start_registration):
    conn = state.conn
    busy_accounts: set[int] = set()
    busy_windows: set[str] = set()

    def find(platform, account_id):
        _platform(platform, unassigned=True)
        record = store.get(conn, account_id)
        if not record or record["platform"] != platform:
            raise HTTPException(404, "该平台下找不到此账号")
        return record

    def guard(record):
        if record["id"] in busy_accounts:
            raise HTTPException(409, "账号正在检测或同步会话，请稍后重试")
        active = db.active_run(conn)
        if active and (active.get("email") or "").strip().casefold() == record["email"].casefold():
            raise HTTPException(409, "该邮箱有注册任务在运行")

    def run_registration(body):
        try:
            return {"run_id": start_registration(body)}
        except RunnerBusy:
            raise HTTPException(409, "已有注册任务在运行") from None

    @app.get("/api/workspaces/{platform}/{stage}")
    async def list_workspace(platform: str, stage: str, _=Depends(require_auth)):
        _stage(platform, stage)
        return {"accounts": [_summary(r) for r in store.list_records(conn, platform, stage)]}

    @app.get("/api/workspaces/{platform}/record/{account_id}")
    async def account_detail(platform: str, account_id: int, response: Response, _=Depends(require_auth)):
        record = find(platform, account_id)
        row = store.projected(conn, record)
        editable = (CLAUDE_EDIT if platform == "claude" else GPT_EDIT if platform == "chatgpt" else ())
        extra = db.OAUTH_FIELDS if platform == "claude" else (
            "chatgpt_token_expires_at", "chatgpt_account_id", "chatgpt_password_set",
        ) if platform == "chatgpt" else ()
        credentials = {k: row.get(k) or "" for k in (*editable, *extra, *SHARED_EDIT)}
        response.headers["Cache-Control"] = "no-store"
        return {**record, "metadata": store.metadata(record), "window_id": row.get("okbrowser_window_id") or "",
                "run_id": row.get("last_run_id" if platform == "claude" else "chatgpt_last_run_id"),
                "claimed_at": row.get("claimed_at" if platform == "claude" else "chatgpt_claimed_at") or "",
                "credentials": credentials, "history": store.history(conn, account_id)}

    @app.patch("/api/workspaces/{platform}/record/{account_id}")
    async def edit_account(platform: str, account_id: int, body: dict, _=Depends(require_auth)):
        record = find(platform, account_id)
        guard(record)
        editable = CLAUDE_EDIT if platform == "claude" else GPT_EDIT if platform == "chatgpt" else ()
        creds = _fields(body, editable)
        if creds.get("session_key") and not creds["session_key"].startswith(("sk-ant-sid01-", "sk-ant-sid02-")):
            raise HTTPException(400, "请输入 Claude sessionKey")
        store.edit(conn, record, _meta(body), creds, _fields(body, SHARED_EDIT))
        return {"ok": True}

    @app.post("/api/workspaces/unassigned/record/{account_id}/assign")
    async def assign_account(account_id: int, body: dict, _=Depends(require_auth)):
        record = find("unassigned", account_id)
        guard(record)
        try:
            store.assign(conn, record, _platform(body.get("platform")))
        except store.DuplicateAccount:
            raise HTTPException(409, "该邮箱已存在于所选平台") from None
        return {"ok": True}

    @app.post("/api/workspaces/{platform}/{stage}/import")
    async def import_accounts(platform: str, stage: str, body: dict, _=Depends(require_auth)):
        _platform(platform)
        _stage(platform, stage)
        meta = _meta(body)
        preview = body.get("commit") is not True
        results = []
        for item, creds, shared in _preview(conn, body, platform, stage):
            if not preview and item["result"] == "ready":
                try:
                    store.add(conn, item["email"], platform, stage, group=meta.get("group_name", ""),
                              proxy=meta.get("proxy", ""), note=meta.get("note", ""), creds=creds,
                              shared=shared, now=state.now_fn())
                except store.DuplicateAccount:
                    item["result"] = "duplicate"
                else:
                    item["result"] = "added"
            results.append(item)
        return {"items": results}

    @app.post("/api/workspaces/{platform}/record/{account_id}/register")
    async def register_account(platform: str, account_id: int, _=Depends(require_auth)):
        _platform(platform)
        record = find(platform, account_id)
        guard(record)
        if record["stage"] != "register":
            raise HTTPException(409, "请在注册工作台执行注册")
        return run_registration({"email": record["email"], "platform": platform})

    @app.post("/api/workspaces/{platform}/record/{account_id}/window")
    async def open_window(platform: str, account_id: int, _=Depends(require_auth)):
        _platform(platform)
        record = find(platform, account_id)
        guard(record)
        row = store.projected(conn, record)
        window_id = row.get("okbrowser_window_id")
        if window_id in busy_windows:
            raise HTTPException(409, "该 OKBrowser 窗口正在同步会话，请稍后打开")
        if window_id:
            busy_windows.add(window_id)
        busy_accounts.add(account_id)
        try:
            out = await asyncio.to_thread(okbrowser_ops.open_window, conn, state.config(), row, platform=platform)
        except OkBrowserError as exc:
            raise HTTPException(502, str(exc)) from None
        finally:
            busy_accounts.discard(account_id)
            busy_windows.discard(window_id)
        return {"window_id": out["window_id"], "created": out["created"]}

    @app.post("/api/workspaces/claude/record/{account_id}/oauth")
    async def authorize(account_id: int, _=Depends(require_auth)):
        record = find("claude", account_id)
        guard(record)
        if record["stage"] != "library":
            raise HTTPException(409, "请先检测账号")
        try:
            await asyncio.to_thread(oauth_acquire.acquire, conn, record["email"], now=state.now_fn(),
                                    expect_email=record["email"], proxy_override=record["proxy"])
        except OAuthError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"ok": True}

    def current_record(record, row, *, include_window=False):
        latest = store.get(conn, record["id"])
        fields = ("email", "session_key", "chatgpt_session_token", "chatgpt_access_token", "proxy")
        if include_window:
            fields += ("okbrowser_window_id",)
        latest_row = store.projected(conn, latest) if latest else {}
        if (not latest or latest["platform"] != record["platform"] or latest["stage"] != record["stage"]
                or any(latest_row.get(k) != row.get(k) for k in fields)):
            raise HTTPException(409, "账号资料在操作期间已变更，请刷新后重试")
        return latest

    def detection_result(account_id):
        current = store.get(conn, account_id)
        return {"id": account_id, "email": current["email"], "status": current["check_status"],
                "detail": current["check_detail"], "stage": current["stage"]}

    async def detect(record):
        guard(record)
        account_id = record["id"]
        busy_accounts.add(account_id)
        try:
            row = store.projected(conn, record)
            result = await asyncio.to_thread(inspect_account, record["platform"], row)
            current_record(record, row)
            with conn:
                store.apply_detection(conn, account_id, result, now=state.now_fn())
            return detection_result(account_id)
        finally:
            busy_accounts.discard(account_id)

    async def recover_session(record):
        record = find(record["platform"], record["id"])
        guard(record)
        if record["stage"] == "register" or record["check_status"] not in RECOVERABLE_RESULTS:
            raise HTTPException(409, "仅同步登录失效、请求受阻或检测失败的账号")
        row = store.projected(conn, record)
        window_id = row.get("okbrowser_window_id")
        if not window_id:
            raise HTTPException(422, "未绑定 OKBrowser 窗口，请先打开并登录对应账号")
        if window_id in busy_windows:
            raise HTTPException(409, "该 OKBrowser 窗口正在使用，请稍后重试")
        email = record["email"]
        if email.endswith("@sk-import.local"):
            email = store.metadata(record).get("observed_email", "")
        account_id = record["id"]
        busy_accounts.add(account_id)
        busy_windows.add(window_id)
        try:
            creds, result = await asyncio.to_thread(
                workspace_resync.recover, state.config(), record["platform"], row, expected_email=email,
            )
            latest = current_record(record, row, include_window=True)
            store.save_recovered_session(conn, latest, creds, result, now=state.now_fn())
            return detection_result(account_id)
        except workspace_resync.SessionUnavailable as exc:
            raise HTTPException(422, str(exc)) from None
        except OkBrowserError:
            raise HTTPException(502, "无法打开已绑定的窗口，请检查 OKBrowser 客户端和窗口绑定") from None
        finally:
            busy_accounts.discard(account_id)
            busy_windows.discard(window_id)

    @app.post("/api/workspaces/{platform}/{stage}/batch")
    async def batch(platform: str, stage: str, body: dict, _=Depends(require_auth)):
        _stage(platform, stage)
        ids = body.get("ids")
        if not isinstance(ids, list) or not 0 < len(ids) <= 500 or any(type(i) is not int for i in ids):
            raise HTTPException(400, "请选中 1–500 个账号")
        records = [find(platform, i) for i in dict.fromkeys(ids)]
        if any(r["stage"] != stage for r in records):
            raise HTTPException(409, "所选账号的工作区已变化，请刷新后重试")
        action = body.get("action")
        if action in ("detect", "resync") and stage != "register":
            results = []
            operation = detect if action == "detect" else recover_session
            for record in records:
                try:
                    results.append(await operation(record))
                except HTTPException as exc:
                    results.append({"id": record["id"], "email": record["email"],
                                    "status": "skipped", "detail": exc.detail})
            return {"results": results}
        if action == "export":
            fields = body.get("fields", ["email"])
            allowed = (*SUMMARY_EXPORT, *(CLAUDE_EXPORT if platform == "claude" else GPT_EXPORT
                                         if platform == "chatgpt" else ()))
            if not isinstance(fields, list) or not fields or any(f not in allowed for f in fields):
                raise HTTPException(400, "包含不属于当前平台的导出字段")
            rows = [{**store.projected(conn, r), "check_status": r["check_status"],
                     "checked_at": r["checked_at"], "display_name": r["note"]} for r in records]
            content, _ = export.render(rows, fields, "csv")
            return Response(content, media_type="text/csv", headers={"Cache-Control": "no-store"})
        for record in records:
            guard(record)
        with conn:
            if action in ("group", "proxy"):
                column = "group_name" if action == "group" else "proxy"
                value = _meta({column: body.get(column, "")})[column]
                conn.executemany(f"UPDATE workspace_accounts SET {column}=? WHERE id=?",
                                 [(value, r["id"]) for r in records])
            elif action == "status" and platform != "unassigned":
                status = body.get("status")
                if status not in store.STATUSES:
                    raise HTTPException(400, "请选择有效的账号状态")
                now = state.now_fn()
                for record in records:
                    store.set_status(conn, record, status, now)
            elif action == "claimed" and stage == "library":
                if type(body.get("claimed")) is not bool:
                    raise HTTPException(400, "请选择领取状态")
                col = "claimed_at" if platform == "claude" else "chatgpt_claimed_at"
                now = state.now_fn() if body["claimed"] else ""
                conn.executemany(f"UPDATE accounts SET {col}=? WHERE email=?",
                                 [(now, r["email"]) for r in records])
            elif action == "delete" and len(records) == 1:
                store.delete(conn, records[0])
            else:
                raise HTTPException(400, "当前工作区不支持此操作")
        return {"updated": len(records)}
