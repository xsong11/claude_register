"""OpenAI Admin Key 管理：多个组织的费用、剩余额度与项目 API Key。

- 鉴权用 OpenAI Platform 的 Admin Key（sk-admin-...），只调 /v1/organization/* 管理接口。
- OpenAI 没有 API Key 可用的余额接口，剩余额度 = 本地填的额度 − 起算日至今的费用。
- Admin Key 只回显掩码；新建的项目 Key 明文只在创建响应里出现一次，不落库。
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request

from claude_register.console import log
from server import db

BASE_URL = "https://api.openai.com/v1/organization"
TIMEOUT = 30.0
COSTS_LIMIT = 180  # /costs 单页最多 180 个日桶；不传 limit 默认只给 7 个
LIST_LIMIT = 100
MAX_PAGES = 50
_DETAIL_MAX = 200
TRANSPORT = None  # 测试注入 httpx.MockTransport；生产为 None（真实网络）


class OpenAIAdminError(Exception):
    """调用 OpenAI 失败；消息可直接展示（不含 Key）。"""

    def __init__(self, message: str, *, invalid_key: bool = False):
        super().__init__(message)
        self.invalid_key = invalid_key


def mask_key(key: str) -> str:
    key = key or ""
    if len(key) <= 13:
        return "…" if key else ""
    return f"{key[:9]}…{key[-4:]}"


class OpenAIAdminClient:
    def __init__(self, admin_key: str, *, transport=None, timeout=TIMEOUT):
        if not admin_key:
            raise OpenAIAdminError("Admin Key 为空")
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {admin_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport if transport is not None else TRANSPORT,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAIAdminClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise OpenAIAdminError(f"连接 api.openai.com 失败：{type(exc).__name__}") from None
        if resp.status_code == 401:
            # 原文会回显 Key 掩码，不透传
            raise OpenAIAdminError("Admin Key 无效或已被撤销（HTTP 401）", invalid_key=True)
        if not 200 <= resp.status_code < 300:
            try:
                err = resp.json().get("error")
                msg = err.get("message") if isinstance(err, dict) else err
            except (ValueError, AttributeError):
                msg = resp.text
            text = " ".join(str(msg or "").split())[:_DETAIL_MAX]
            raise OpenAIAdminError(f"OpenAI 返回 HTTP {resp.status_code}：{text}".rstrip("："))
        return resp

    def _json(self, method: str, path: str, **kw) -> dict:
        try:
            data = self._request(method, path, **kw).json()
        except ValueError:
            raise OpenAIAdminError("OpenAI 响应格式不正确") from None
        if not isinstance(data, dict):
            raise OpenAIAdminError("OpenAI 响应格式不正确")
        return data

    def _list_all(self, path: str, params: dict | None = None) -> list[dict]:
        """after / has_more / last_id 分页的列表接口，拉全。"""
        items: list[dict] = []
        after = None
        for _ in range(MAX_PAGES):
            q = {"limit": LIST_LIMIT, **(params or {})}
            if after:
                q["after"] = after
            data = self._json("GET", path, params=q)
            batch = data.get("data")
            if not isinstance(batch, list):
                raise OpenAIAdminError("OpenAI 列表格式不正确")
            items.extend(x for x in batch if isinstance(x, dict))
            after = data.get("last_id")
            if not data.get("has_more") or not after:
                break
        return items

    def verify(self) -> str:
        """校验 Key，返回组织 ID（响应头 openai-organization，个人组织为 user-...）。"""
        resp = self._request("GET", "/projects", params={"limit": 1})
        return resp.headers.get("openai-organization", "")

    def cost_buckets(self, start_ts: int) -> list[dict]:
        """start_ts 至今的日费用桶；不传 end_time（传了必须比 start 晚至少 1 天）。"""
        buckets: list[dict] = []
        page = None
        for _ in range(MAX_PAGES):
            q = {"start_time": int(start_ts), "bucket_width": "1d", "limit": COSTS_LIMIT}
            if page:
                q["page"] = page
            data = self._json("GET", "/costs", params=q)
            batch = data.get("data")
            if not isinstance(batch, list):
                raise OpenAIAdminError("OpenAI 费用数据格式不正确")
            buckets.extend(b for b in batch if isinstance(b, dict))
            page = data.get("next_page")
            if not data.get("has_more") or not page:
                break
        return buckets

    def list_projects(self) -> list[dict]:
        return self._list_all("/projects")

    def list_api_keys(self, project_id: str) -> list[dict]:
        return self._list_all(f"/projects/{project_id}/api_keys")

    def get_api_key(self, project_id: str, key_id: str) -> dict:
        return self._json("GET", f"/projects/{project_id}/api_keys/{key_id}")

    def create_service_account(self, project_id: str, name: str) -> dict:
        return self._json("POST", f"/projects/{project_id}/service_accounts",
                          json={"name": name})

    def delete_api_key(self, project_id: str, key_id: str) -> None:
        self._request("DELETE", f"/projects/{project_id}/api_keys/{key_id}")

    def delete_service_account(self, project_id: str, sa_id: str) -> None:
        self._request("DELETE", f"/projects/{project_id}/service_accounts/{sa_id}")


# ---- 费用汇总（纯函数） ----

def _utc_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def parse_now(now_iso: str) -> datetime:
    return datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_since(text) -> date | None:
    try:
        return date.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def bucket_usd(bucket: dict) -> float:
    total = 0.0
    for r in bucket.get("results") or []:
        amount = r.get("amount") if isinstance(r, dict) else None
        if isinstance(amount, dict) and str(amount.get("currency") or "usd").lower() == "usd":
            try:
                total += float(amount.get("value") or 0)
            except (TypeError, ValueError):
                pass
    return total


def cost_window(now: datetime, since: date | None) -> tuple[int, int, int]:
    """返回 (拉取起点, 本月起点, 起算日起点) 的 Unix 秒，均为 UTC 零点。"""
    month_ts = _utc_ts(now.date().replace(day=1))
    since_ts = _utc_ts(since) if since else month_ts
    return min(month_ts, since_ts), month_ts, since_ts


def sum_costs(buckets: list[dict], month_ts: int, since_ts: int) -> tuple[float, float]:
    month = since_total = 0.0
    for b in buckets:
        start = int(b.get("start_time") or 0)
        usd = bucket_usd(b)
        if start >= month_ts:
            month += usd
        if start >= since_ts:
            since_total += usd
    return round(month, 6), round(since_total, 6)


def remaining(row: dict) -> float | None:
    if row.get("credit_usd") is None:
        return None
    return round(float(row["credit_usd"]) - float(row.get("cost_since_usd") or 0), 6)


def public_row(row: dict) -> dict:
    """接口回包：Admin Key 只给掩码。"""
    out = {k: row.get(k) for k in (
        "id", "name", "org_id", "credit_usd", "credit_since", "status", "last_error",
        "cost_month_usd", "cost_since_usd", "refreshed_at", "created_at",
    )}
    out["key_masked"] = mask_key(row.get("admin_key") or "")
    out["remaining_usd"] = remaining(row)
    return out


def summarize_api_key(project: dict, key: dict) -> dict:
    owner = key.get("owner") if isinstance(key.get("owner"), dict) else {}
    owner_type = owner.get("type") or ""
    detail = owner.get(owner_type) if isinstance(owner.get(owner_type), dict) else {}
    return {
        "id": key.get("id") or "",
        "name": key.get("name") or "",
        "redacted_value": key.get("redacted_value") or "",
        "created_at": key.get("created_at"),
        "last_used_at": key.get("last_used_at"),
        "owner_type": owner_type,
        "owner_name": detail.get("name") or detail.get("email") or "",
        "project_id": project.get("id") or "",
        "project_name": project.get("name") or "",
    }


# ---- 面板路由 ----

def _parse_credit(body: dict) -> dict:
    out: dict = {}
    if "credit_usd" in body:
        raw = body.get("credit_usd")
        if raw in (None, ""):
            out["credit_usd"] = None
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="额度必须是数字") from None
            if value < 0:
                raise HTTPException(status_code=400, detail="额度不能为负数")
            out["credit_usd"] = value
    if "credit_since" in body:
        text = str(body.get("credit_since") or "").strip()
        if text and parse_since(text) is None:
            raise HTTPException(status_code=400, detail="起算日期格式应为 YYYY-MM-DD")
        out["credit_since"] = text
    return out


async def _body(request: Request) -> dict:
    try:
        body = await request.json() if await request.body() else {}
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体格式不正确")
    return body


def register_openai_routes(app: FastAPI, state, *, require_auth) -> None:
    def load(key_id: int) -> dict:
        row = db.get_openai_key(state.conn, key_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Admin Key 不存在")
        return row

    def refresh(row: dict) -> dict:
        """校验 + 拉费用，结果写库；OpenAI 出错时记 status，不抛。"""
        now_iso = state.now_fn()
        since = parse_since(row.get("credit_since"))
        try:
            with OpenAIAdminClient(row["admin_key"]) as c:
                org_id = c.verify()
                start_ts, month_ts, since_ts = cost_window(parse_now(now_iso), since)
                month, since_total = sum_costs(c.cost_buckets(start_ts), month_ts, since_ts)
        except OpenAIAdminError as exc:
            db.update_openai_key(state.conn, row["id"], {
                "status": "invalid" if exc.invalid_key else "error",
                "last_error": str(exc), "refreshed_at": now_iso,
            })
            log(f"OpenAI Admin Key 刷新失败：#{row['id']} {row.get('name')}：{exc}")
        else:
            db.update_openai_key(state.conn, row["id"], {
                "status": "ok", "last_error": "", "org_id": org_id or row.get("org_id") or "",
                "cost_month_usd": month, "cost_since_usd": since_total, "refreshed_at": now_iso,
            })
        return db.get_openai_key(state.conn, row["id"])

    def client_for(key_id: int) -> OpenAIAdminClient:
        return OpenAIAdminClient(load(key_id)["admin_key"])

    def upstream(exc: OpenAIAdminError) -> HTTPException:
        return HTTPException(status_code=502, detail=str(exc))

    @app.get("/api/openai/keys")
    def openai_keys(_=Depends(require_auth)):
        return {"keys": [public_row(r) for r in db.list_openai_keys(state.conn)]}

    @app.post("/api/openai/keys")
    async def openai_key_create(request: Request, _=Depends(require_auth)):
        body = await _body(request)
        name = str(body.get("name") or "").strip()
        admin_key = str(body.get("admin_key") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="请填写名称")
        if not admin_key:
            raise HTTPException(status_code=400, detail="请填写 Admin Key")
        fields = {"name": name, "admin_key": admin_key, **_parse_credit(body)}
        if not fields.get("credit_since"):
            fields["credit_since"] = state.now_fn()[:10]

        def check():
            with OpenAIAdminClient(admin_key) as c:
                return c.verify()

        try:
            fields["org_id"] = await asyncio.to_thread(check)
        except OpenAIAdminError as exc:
            raise HTTPException(status_code=400 if exc.invalid_key else 502,
                                detail=str(exc)) from None
        row = db.create_openai_key(state.conn, fields, created_at=state.now_fn())
        log(f"已添加 OpenAI Admin Key：#{row['id']} {name}（{row.get('org_id') or '未知组织'}）")
        row = await asyncio.to_thread(refresh, row)
        return public_row(row)

    @app.patch("/api/openai/keys/{key_id}")
    async def openai_key_update(key_id: int, request: Request, _=Depends(require_auth)):
        row = load(key_id)
        body = await _body(request)
        fields = _parse_credit(body)
        if "name" in body:
            name = str(body.get("name") or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="名称不能为空")
            fields["name"] = name
        new_key = str(body.get("admin_key") or "").strip()
        key_changed = bool(new_key) and new_key != row["admin_key"]
        if key_changed:
            fields.update({"admin_key": new_key, "status": "", "org_id": ""})
        db.update_openai_key(state.conn, key_id, fields)
        row = load(key_id)
        if key_changed or "credit_since" in fields:
            row = await asyncio.to_thread(refresh, row)
        return public_row(row)

    @app.delete("/api/openai/keys/{key_id}")
    def openai_key_delete(key_id: int, _=Depends(require_auth)):
        row = load(key_id)
        db.delete_openai_key(state.conn, key_id)
        log(f"已删除 OpenAI Admin Key：#{key_id} {row.get('name')}")
        return {"ok": True}

    @app.post("/api/openai/keys/{key_id}/refresh")
    async def openai_key_refresh(key_id: int, _=Depends(require_auth)):
        row = await asyncio.to_thread(refresh, load(key_id))
        return public_row(row)

    @app.get("/api/openai/keys/{key_id}/api-keys")
    async def openai_api_keys(key_id: int, _=Depends(require_auth)):
        def work():
            with client_for(key_id) as c:
                projects = [p for p in c.list_projects() if not p.get("archived_at")]
                keys = [summarize_api_key(p, k)
                        for p in projects for k in c.list_api_keys(p["id"])]
                return projects, keys

        try:
            projects, keys = await asyncio.to_thread(work)
        except OpenAIAdminError as exc:
            raise upstream(exc) from None
        return {
            "projects": [{"id": p.get("id"), "name": p.get("name") or ""} for p in projects],
            "api_keys": keys,
        }

    @app.post("/api/openai/keys/{key_id}/api-keys")
    async def openai_api_key_create(key_id: int, request: Request, _=Depends(require_auth)):
        body = await _body(request)
        project_id = str(body.get("project_id") or "").strip()
        name = str(body.get("name") or "").strip()
        if not project_id or not name:
            raise HTTPException(status_code=400, detail="请选择项目并填写 Key 名称")

        def work():
            with client_for(key_id) as c:
                return c.create_service_account(project_id, name)

        try:
            sa = await asyncio.to_thread(work)
        except OpenAIAdminError as exc:
            raise upstream(exc) from None
        api_key = sa.get("api_key") if isinstance(sa.get("api_key"), dict) else {}
        if not api_key.get("value"):
            raise HTTPException(status_code=502, detail="OpenAI 未返回 Key 明文")
        log(f"已新建 OpenAI 项目 Key：Admin #{key_id} {project_id} {name}（{api_key.get('id')}）")
        return {
            "id": api_key.get("id") or "",
            "name": api_key.get("name") or name,
            "value": api_key["value"],
            "project_id": project_id,
            "service_account_id": sa.get("id") or "",
        }

    @app.delete("/api/openai/keys/{key_id}/api-keys/{api_key_id}")
    async def openai_api_key_delete(key_id: int, api_key_id: str, project_id: str,
                                    _=Depends(require_auth)):
        def work():
            with client_for(key_id) as c:
                key = c.get_api_key(project_id, api_key_id)
                owner = key.get("owner") if isinstance(key.get("owner"), dict) else {}
                sa = owner.get("service_account") if isinstance(
                    owner.get("service_account"), dict) else {}
                if owner.get("type") == "service_account" and sa.get("id"):
                    c.delete_service_account(project_id, sa["id"])
                else:
                    c.delete_api_key(project_id, api_key_id)

        try:
            await asyncio.to_thread(work)
        except OpenAIAdminError as exc:
            raise upstream(exc) from None
        log(f"已删除 OpenAI 项目 Key：Admin #{key_id} {project_id} {api_key_id}")
        return {"ok": True}
