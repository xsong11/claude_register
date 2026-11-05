"""开放 API（/api/v1/*）：供外部脚本触发自动注册、查询结果、按需导出账号信息。

与面板接口的区别：
- 鉴权用 API Key（Authorization: Bearer <key> 或 X-API-Key），不用面板 Cookie；
  不接受 query 参数传 key，避免落进访问日志。
- 默认关闭，需在面板「设置 → 开放 API」启用并生成 Key。
- 注册仍是单任务：已有任务在跑时返回 409 并带上正在运行的 run_id。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server import auth, db, export, oauth_acquire, open_api_docs
from claude_register.oauth import OAuthError
from server.runner import RunnerBusy

# 长轮询上限：容器内 Nginx 对 / 的默认读超时是 60s，留足余量。
MAX_WAIT_S = 50
LOG_TAIL_LINES = 20
CHECK_STATUSES = ("alive", "dead", "blocked", "error")


# ---- 仅用于生成 OpenAPI 文档的模型（运行时校验仍走下面的手写逻辑，保持 400 语义）----


class RegisterRequest(BaseModel):
    """触发注册的请求体；整个 body 与每个字段都可省略。"""

    email: str | None = Field(None, description="指定要注册的邮箱；不填自动创建临时邮箱",
                              examples=["alice@example.com"])
    domain: str | None = Field(None, description="自动创建邮箱时使用的域名；不填用默认域名")
    proxy_id: str | None = Field(None, description="代理池中的代理 id（见 GET /api/v1/proxies）")
    platform: Literal["claude", "chatgpt", "both"] | None = Field(
        None, description="注册平台：claude（默认）/ chatgpt / both（同一邮箱先 Claude 再 ChatGPT）")
    group_name: str | None = Field(None, description="注册产出的账号归入的分组", examples=["主力号"])


class RegisterAccepted(BaseModel):
    run_id: int = Field(description="注册任务 id，用于查询结果")
    status: Literal["running"]


class RegisterBusy(BaseModel):
    detail: str = Field(examples=["已有任务在运行"])
    active_run_id: int | None = Field(description="正在运行的任务 id")


class RegisterResult(BaseModel):
    run_id: int
    status: Literal["running", "success", "needs_manual", "phone_required", "banned",
                    "failed"] = Field(
        description="running 进行中；success 成功并拿到凭据；"
                    "needs_manual 跑完但没拿到凭据（账号已入库）；phone_required 需手机号验证；"
                    "banned ChatGPT 账号已被封禁；"
                    "failed 失败。platform=both 时两个平台都成功才是 success")
    platform: Literal["claude", "chatgpt", "both"] = Field(description="本次注册的平台")
    claude_status: str | None = Field(None, description="platform=both 时 Claude 侧的结果")
    chatgpt_status: str | None = Field(None, description="platform 含 chatgpt 时 ChatGPT 侧的结果")
    email: str | None = Field(description="注册的邮箱（开始后才确定）")
    started_at: str | None
    finished_at: str | None
    account: dict[str, Any] | None = Field(
        description="成功或 needs_manual 时的账号信息，只含 fields 选择的字段",
        examples=[{"email": "alice@example.com", "session_key": "sk-ant-sid01-..."}])
    export: str | None = Field(None, description="format 为 text/csv/line 时才返回的导出文本")
    log_tail: list[str] | None = Field(None, description="failed / needs_manual 时最近 20 行日志")


class ErrorResponse(BaseModel):
    detail: str


class FieldInfo(BaseModel):
    key: str = Field(description="fields 参数里使用的字段名")
    title: str = Field(description="中文名称")
    label: str = Field(description="text 格式里的行标签")
    default: bool = Field(description="是否属于默认字段")
    secret: bool = Field(description="是否为敏感凭据")


class FieldsInfo(BaseModel):
    fields: list[FieldInfo]
    formats: list[str]
    default_fields: list[str]
    default_line_sep: str


class ProxyItem(BaseModel):
    id: str
    name: str


class ClaimResult(BaseModel):
    email: str = Field(description="被领取的账号邮箱")
    claimed_at: str = Field(description="打上「已获取」标记的时间")
    account: dict[str, Any] = Field(
        description="账号信息，只含 fields 选择的字段",
        examples=[{"email": "alice@example.com", "session_key": "sk-ant-sid01-..."}])
    export: str | None = Field(None, description="format 为 text/csv/line 时才返回的导出文本")
    remaining: int = Field(description="同样条件下剩余可领取的账号数")


class OAuthRequest(BaseModel):
    email: str = Field(description="要授权的账号邮箱（必须已入库且有 sessionKey）",
                       examples=["alice@example.com"])


class OAuthResult(BaseModel):
    email: str
    oauth_at: str = Field(description="本次授权时间")
    oauth_expires_at: str = Field(description="access_token 过期时间（UTC）")
    account: dict[str, Any] = Field(
        description="账号信息，只含 fields 选择的字段（默认为 OAuth 令牌字段）",
        examples=[{"email": "alice@example.com", "access_token": "sk-ant-oat01-...",
                   "refresh_token": "sk-ant-ort01-..."}])
    export: str | None = Field(None, description="format 为 text/csv/line/sub2api 时的导出文本")


OAUTH_DEFAULT_FIELDS = ("email", "access_token", "refresh_token", "oauth_expires_at",
                        "org_uuid", "account_uuid")


DOC_MODELS = (RegisterRequest, RegisterAccepted, RegisterBusy, RegisterResult,
              ErrorResponse, FieldInfo, FieldsInfo, ProxyItem, ClaimResult,
              OAuthRequest, OAuthResult)
_AUTH_ERRORS = {
    401: {"model": ErrorResponse, "description": "缺少或错误的 API Key"},
    403: {"model": ErrorResponse, "description": "服务未启用开放 API"},
}
_FIELDS_DESC = ("逗号分隔的字段名，默认 email,session_key,proxy,mail_base_url,mail_key；"
                "可选值见 GET /api/v1/fields")
_GROUP_DESC = f"只要该分组的账号；{export.UNGROUPED} 表示仅未分组；不填不筛选"
_PLATFORM_DESC = ("claude 只要有 sessionKey 的账号；chatgpt 只要有 ChatGPT 凭据的账号，"
                  "且 status / check_status / claimed 改按 ChatGPT 侧判断；不填不筛选")


def run_result(state, run_id: int, fields, fmt: str, sep: str) -> dict | None:
    """把 runs 行 + 账号行整理成对外的结果；run 不存在返回 None。

    status 取值：running / success / needs_manual / phone_required / failed。
    needs_manual 表示流程跑完但没拿到 sessionKey（账号已入库，需在面板接管处理）。
    phone_required 表示 Claude 要求手机号验证（多半是代理 IP 被风控，换代理重跑）。
    """
    row = db.get_run(state.conn, run_id)
    if row is None:
        return None
    platform = row.get("platform") or "claude"
    status = row["status"] or "failed"
    acct = None
    side: dict[str, str] = {}
    if status not in ("running", "failed"):
        acct = db.get_account(state.conn, row["email"]) if row.get("email") else None
        if platform in ("claude", "both"):
            side["claude"] = _claude_status(acct)
        if platform in ("chatgpt", "both"):
            side["chatgpt"] = _chatgpt_status(acct, run_id)
        bad = [s for s in side.values() if s != "success"]
        status = bad[0] if bad else "success"
    result = {
        "run_id": row["id"],
        "status": status,
        "platform": platform,
        "email": row.get("email") or None,
        "started_at": row.get("started_at") or None,
        "finished_at": row.get("finished_at") or None,
        "account": export.pick(acct, fields) if acct else None,
    }
    if platform == "both" and "claude" in side:
        result["claude_status"] = side["claude"]
    if "chatgpt" in side:
        result["chatgpt_status"] = side["chatgpt"]
    if acct and fmt != "json":
        result["export"] = export.render([acct], fields, fmt, sep=sep)[0]
    if status in ("failed", "needs_manual", "phone_required", "banned"):
        result["log_tail"] = _log_tail(row.get("output_dir"))
    return result


def _claude_status(acct: dict | None) -> str:
    if acct and acct.get("session_key"):
        return "success"
    if acct and acct.get("status") == "phone_required":
        return "phone_required"
    return "needs_manual"


def _chatgpt_status(acct: dict | None, run_id: int) -> str:
    if not acct or acct.get("chatgpt_last_run_id") != run_id:
        return "needs_manual"
    if acct.get("chatgpt_status") == "banned":
        return "banned"
    if acct.get("chatgpt_session_token") or acct.get("chatgpt_access_token"):
        return "success"
    if acct.get("chatgpt_status") == "phone_required":
        return "phone_required"
    return "needs_manual"


def _log_tail(output_dir) -> list[str]:
    if not output_dir:
        return []
    path = Path(output_dir) / "log.txt"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-LOG_TAIL_LINES:]


def _split(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def export_response(rows, *, fields, fmt, sep, emails, status, check_status,
                    download: bool, claimed=None, group=None, platform=None) -> Response:
    try:
        keys = export.parse_fields(fields)
        fmt = export.parse_format(fmt)
        sep = export.parse_sep(sep)
        claimed_filter = export.parse_claimed(claimed)
        platform = export.parse_platform(platform)
    except export.ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    picked = export.filter_rows(
        rows, emails=_split(emails), status=status or None,
        check_status=check_status or None, claimed=claimed_filter,
        group=group or None, platform=platform,
    )
    if fmt == "sub2api":  # sub2api 只导入已授权的账号
        picked = [r for r in picked if r.get("access_token")]
    body, media = export.render(picked, keys, fmt, sep=sep)
    headers = {"X-Total-Count": str(len(picked)), "Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="accounts.{export.FILE_EXT[fmt]}"'
        )
    return Response(body, media_type=media, headers=headers)


def register_open_api(app: FastAPI, state, *, start_registration, account_rows) -> None:
    open_api_docs.register_docs(app, max_wait=MAX_WAIT_S)

    def require_api_key(request: Request):
        cfg = state.config()
        docs = open_api_docs.base_url(request) + open_api_docs.DOCS_MD
        if not cfg.api_enabled or not cfg.api_key:
            raise HTTPException(status_code=403, detail=f"开放 API 未启用（接口文档：{docs}）")
        token = ""
        authz = request.headers.get("authorization", "")
        if authz[:7].lower() == "bearer ":
            token = authz[7:].strip()
        token = token or request.headers.get("x-api-key", "").strip()
        if not auth.passwords_match(token, cfg.api_key):
            raise HTTPException(
                status_code=401, detail=f"API Key 无效（接口文档：{docs}）",
                headers={"WWW-Authenticate": "Bearer",
                         "Link": f'<{docs}>; rel="describedby"'},
            )

    @app.get("/api/v1/fields", tags=["元数据"], summary="可导出的字段与格式",
             response_model=FieldsInfo, responses=_AUTH_ERRORS)
    def v1_fields(_=Depends(require_api_key)):
        """返回 fields 参数可用的字段、默认字段和支持的导出格式。"""
        return export.describe()

    @app.get("/api/v1/proxies", tags=["元数据"], summary="可选的注册代理",
             response_model=list[ProxyItem], responses=_AUTH_ERRORS)
    def v1_proxies(_=Depends(require_api_key)):
        """面板代理池里可选的代理（只给 id 和名称，不暴露地址与凭据）。"""
        return [{"id": p["id"], "name": p["name"]} for p in state.config().saved_proxies]

    @app.post(
        "/api/v1/register", status_code=202, tags=["注册"], summary="触发一次自动注册",
        responses={
            202: {"model": RegisterAccepted, "description": "已开始，用 run_id 查询结果"},
            400: {"model": ErrorResponse, "description": "请求体不合法或代理不存在"},
            409: {"model": RegisterBusy, "description": "已有注册任务在运行"},
            **_AUTH_ERRORS,
        },
        openapi_extra={"requestBody": {"required": False, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/RegisterRequest"}}}}},
    )
    async def v1_register(request: Request, _=Depends(require_api_key)):
        """后台开始一次注册（通常 1–5 分钟），立即返回 run_id。同一时刻只运行一个任务。"""
        try:
            body = await request.json() if await request.body() else {}
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        for key in ("email", "domain", "proxy_id", "platform", "group_name"):
            if body.get(key) is not None and not isinstance(body[key], str):
                raise HTTPException(status_code=400, detail=f"{key} 必须是字符串")
        try:
            rid = start_registration(body)
        except RunnerBusy:
            active = db.active_run(state.conn)
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "已有任务在运行",
                    "active_run_id": active["id"] if active else None,
                },
            )
        return {"run_id": rid, "status": "running"}

    @app.get(
        "/api/v1/register/{run_id}", tags=["注册"], summary="查询注册结果",
        responses={
            200: {"model": RegisterResult, "description": "任务状态与结果"},
            400: {"model": ErrorResponse, "description": "fields / format / sep 不合法"},
            404: {"model": ErrorResponse, "description": "任务不存在"},
            **_AUTH_ERRORS,
        },
    )
    async def v1_register_status(
        run_id: int,
        wait: int = Query(0, ge=0, le=MAX_WAIT_S,
                          description=f"长轮询：任务仍在运行时最多等待的秒数（0–{MAX_WAIT_S}）"),
        fields: str | None = Query(None, description=_FIELDS_DESC),
        format: str = Query("json", description="设为 text/csv/line 时额外返回 export 文本",
                            json_schema_extra={"enum": list(export.FORMATS)}),
        sep: str | None = Query(None, description="format=line 的分隔符，默认 ----"),
        _=Depends(require_api_key),
    ):
        """查询注册任务；成功时 account 只含 fields 选择的字段。任务未结束且 wait>0 时会等待。"""
        try:
            keys = export.parse_fields(fields)
            fmt = export.parse_format(format)
            line_sep = export.parse_sep(sep)
        except export.ExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            result = run_result(state, run_id, keys, fmt, line_sep)
            if result is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            if result["status"] != "running" or loop.time() >= deadline:
                return result
            await asyncio.sleep(min(1.0, max(0.0, deadline - loop.time())))

    @app.get(
        "/api/v1/accounts/export", tags=["导出"], summary="批量导出账号",
        responses={
            200: {
                "description": "对应格式的导出内容；响应头 X-Total-Count 为条数",
                "content": {
                    "application/json": {"schema": {"type": "array", "items": {"type": "object"}}},
                    "text/plain": {"schema": {"type": "string"}},
                    "text/csv": {"schema": {"type": "string"}},
                },
            },
            400: {"model": ErrorResponse, "description": "参数不合法"},
            **_AUTH_ERRORS,
        },
    )
    def v1_accounts_export(
        fields: str | None = Query(None, description=_FIELDS_DESC),
        format: str = Query("json", description="json / text / csv / line",
                            json_schema_extra={"enum": list(export.FORMATS)}),
        sep: str | None = Query(None, description="format=line 的分隔符，默认 ----"),
        emails: str | None = Query(None, description="只导出这些邮箱（逗号分隔，不区分大小写）"),
        status: str | None = Query(None, description="注册状态：success / needs_manual"),
        check_status: str | None = Query(
            None, description="最近检测结果：alive / dead / blocked / error"),
        claimed: str | None = Query(
            None, description="true 只导出已获取的账号，false 只导出未获取的；不填不筛选",
            json_schema_extra={"enum": ["true", "false"]}),
        group: str | None = Query(None, description=_GROUP_DESC),
        platform: str | None = Query(None, description=_PLATFORM_DESC,
                                     json_schema_extra={"enum": list(export.PLATFORMS)}),
        _=Depends(require_api_key),
    ):
        """按创建时间倒序导出已入库账号，可选字段、格式与筛选条件。不会打「已获取」标记。"""
        return export_response(
            account_rows(), fields=fields, fmt=format, sep=sep, emails=emails,
            status=status, check_status=check_status, download=False, claimed=claimed,
            group=group, platform=platform,
        )

    @app.post(
        "/api/v1/accounts/claim", tags=["导出"], summary="获取一个账号并标记为已获取",
        responses={
            200: {"model": ClaimResult, "description": "已领取的账号"},
            400: {"model": ErrorResponse, "description": "参数不合法"},
            404: {"model": ErrorResponse, "description": "没有可获取的账号"},
            **_AUTH_ERRORS,
        },
    )
    def v1_accounts_claim(
        fields: str | None = Query(None, description=_FIELDS_DESC),
        format: str = Query("json", description="设为 text/csv/line 时额外返回 export 文本",
                            json_schema_extra={"enum": list(export.FORMATS)}),
        sep: str | None = Query(None, description="format=line 的分隔符，默认 ----"),
        check_status: str | None = Query(
            None, description="只领取该检测结果的账号（如 alive）；不填时跳过 dead / blocked"),
        group: str | None = Query(None, description=_GROUP_DESC),
        platform: str | None = Query(
            None, description="claude（默认）领取有 sessionKey 的；chatgpt 领取有 ChatGPT 凭据的，"
                              "两个平台的「已获取」标记互不影响",
            json_schema_extra={"enum": list(export.PLATFORMS)}),
        _=Depends(require_api_key),
    ):
        """每调用一次领取一个「注册成功、有凭据、未获取过」的账号（先入库先发），
        并立即打上「已获取」标记，同一账号不会被重复领取。"""
        try:
            keys = export.parse_fields(fields)
            fmt = export.parse_format(format)
            line_sep = export.parse_sep(sep)
            plat = export.parse_platform(platform) or "claude"
        except export.ExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if check_status is not None and check_status not in CHECK_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"check_status 只能是 {' / '.join(CHECK_STATUSES)}")
        opts = {"check_status": check_status or None, "platform": plat, "group": group or None}
        acct = db.claim_account(state.conn, state.now_fn(), **opts)
        if acct is None:
            raise HTTPException(status_code=404, detail="没有可获取的账号")
        result = {
            "email": acct["email"],
            "claimed_at": acct["chatgpt_claimed_at" if plat == "chatgpt" else "claimed_at"],
            "account": export.pick(acct, keys),
            "remaining": db.count_claimable(state.conn, **opts),
        }
        if fmt != "json":
            result["export"] = export.render([acct], keys, fmt, sep=line_sep)[0]
        return result

    @app.post(
        "/api/v1/accounts/oauth", tags=["OAuth"], summary="用 sessionKey 获取 Claude OAuth 令牌",
        responses={
            200: {"model": OAuthResult, "description": "授权成功，令牌已写入账号"},
            400: {"model": ErrorResponse, "description": "参数不合法或账号无 sessionKey"},
            404: {"model": ErrorResponse, "description": "账号不存在"},
            422: {"model": ErrorResponse, "description": "授权失败（sessionKey 失效、被拦截等）"},
            **_AUTH_ERRORS,
        },
        openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/OAuthRequest"}}}}},
    )
    async def v1_accounts_oauth(
        request: Request,
        fields: str | None = Query(
            None, description="逗号分隔的字段名，默认 " + ",".join(OAUTH_DEFAULT_FIELDS)),
        format: str = Query("json", description="设为 text/csv/line/sub2api 时额外返回 export 文本",
                            json_schema_extra={"enum": list(export.FORMATS)}),
        sep: str | None = Query(None, description="format=line 的分隔符，默认 ----"),
        _=Depends(require_api_key),
    ):
        """用账号保存的 sessionKey（经账号绑定的代理）自动完成完整的 Claude OAuth 授权，
        返回 access_token / refresh_token 并写入账号。每次调用都会重新授权、覆盖旧令牌。"""
        try:
            keys = export.parse_fields(fields if fields is not None else OAUTH_DEFAULT_FIELDS)
            fmt = export.parse_format(format)
            line_sep = export.parse_sep(sep)
        except export.ExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            body = await request.json() if await request.body() else {}
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
        email = body.get("email") if isinstance(body, dict) else None
        if not isinstance(email, str) or not email.strip():
            raise HTTPException(status_code=400, detail="email 必须是非空字符串")
        email = email.strip()
        row = db.get_account(state.conn, email) or db.get_account(state.conn, email.lower())
        if row is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        email = row["email"]
        if not row.get("session_key"):
            raise HTTPException(status_code=400, detail="该账号无 sessionKey")
        try:
            acct = await asyncio.to_thread(
                oauth_acquire.acquire, state.conn, email, now=state.now_fn(),
            )
        except oauth_acquire.AccountNotFound:
            raise HTTPException(status_code=404, detail="账号不存在") from None
        except OAuthError as exc:
            raise HTTPException(status_code=422, detail=f"OAuth 授权失败：{exc}") from None
        result = {
            "email": acct["email"],
            "oauth_at": acct.get("oauth_at") or "",
            "oauth_expires_at": acct.get("oauth_expires_at") or "",
            "account": export.pick(acct, keys),
        }
        if fmt != "json":
            result["export"] = export.render([acct], keys, fmt, sep=line_sep)[0]
        return result
