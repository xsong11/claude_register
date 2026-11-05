"""后台单任务执行：全局锁 + 线程 + sink 捕获日志落库。"""
from __future__ import annotations

from dataclasses import dataclass, replace
import queue
import threading
from pathlib import Path

from claude_register import browser
from claude_register import console
from claude_register import flow as flow_module
from claude_register.outlook import OUTLOOK_MAIL_URL, parse_outlook_line
from server import db, workspace_store


class RunnerBusy(Exception):
    pass


@dataclass
class _Prepared:
    """跑注册前从已有账号里取出的可复用凭据。"""
    email: str | None
    config: object
    existing: dict | None
    password: str = ""
    mail_key: str = ""
    mail_base_url: str = ""

    @property
    def window_id(self) -> str:
        return str((self.existing or {}).get("okbrowser_window_id") or "")


def _chatgpt_run(**kw):
    from claude_register import chatgpt_flow

    return chatgpt_flow.run(**kw)


class Runner:
    def __init__(self, conn, data_dir: Path, now_fn):
        self.conn = conn
        self.data_dir = Path(data_dir)
        self.now_fn = now_fn
        self._lock = threading.Lock()
        self._active_id: int | None = None
        self._queues: dict[int, queue.Queue] = {}

    def is_busy(self) -> bool:
        with self._lock:
            return self._active_id is not None

    def subscribe(self, run_id):
        with self._lock:
            if run_id != self._active_id:
                return None
        return self._queues.get(run_id)

    def start(self, config, *, email=None, domain=None, flow_fn=None, proxy_override: bool = False,
              platform: str = "claude", group_name: str = "", chatgpt_fn=None):
        """platform：claude / chatgpt / both（先 Claude，再用同一邮箱、代理、窗口跑 ChatGPT）。"""
        if platform not in db.PLATFORMS:
            raise ValueError(f"platform 只能是 {' / '.join(db.PLATFORMS)}")
        fns = {"claude": flow_fn or flow_module.run, "chatgpt": chatgpt_fn or _chatgpt_run}
        with self._lock:
            if self._active_id is not None:
                raise RunnerBusy("已有注册任务在运行")
            run_dir = self.data_dir / "runs"
            rid = db.create_run(
                self.conn, email or "", domain or config.anymail_domain,
                "", self.now_fn(), platform=platform, group_name=group_name,
            )
            out_dir = run_dir / str(rid)
            out_dir.mkdir(parents=True, exist_ok=True)
            self.conn.execute("UPDATE runs SET output_dir=? WHERE id=?",
                              (str(out_dir), rid))
            self.conn.commit()
            q: queue.Queue = queue.Queue()
            self._queues[rid] = q
            self._active_id = rid
        t = threading.Thread(
            target=self._run,
            args=(rid, out_dir, config, email, domain, fns, q, proxy_override, platform,
                  group_name),
            daemon=True,
        )
        t.start()
        return rid

    def _auto_oauth(self, email: str, sink) -> None:
        """注册成功后自动获取 OAuth 令牌；失败只记日志，不影响注册结果。"""
        from claude_register.oauth import OAuthError
        from server import oauth_acquire

        sink("正在用 sessionKey 获取 Claude OAuth 令牌…")
        try:
            row = oauth_acquire.acquire(self.conn, email, now=self.now_fn())
        except OAuthError as exc:
            sink(f"OAuth 授权失败：{exc}（可稍后在账号页重试）")
            return
        except Exception as exc:  # noqa: BLE001
            sink(f"OAuth 授权出错：{type(exc).__name__}（可稍后在账号页重试）")
            return
        sink(f"已获取 OAuth 令牌（过期 {row.get('oauth_expires_at') or '未知'}）")

    def _prepare(self, config, email, proxy_override: bool, platform: str = "claude") -> _Prepared:
        password = mail_key = mail_base_url = ""
        if email:
            parsed_line = parse_outlook_line(email)
            if parsed_line is not None:
                email = parsed_line.email
                password = parsed_line.password
                mail_key = parsed_line.mail_key
                mail_base_url = OUTLOOK_MAIL_URL
        existing = workspace_store.mailbox(self.conn, email.strip()) if email else None
        if existing is not None:
            email = existing["email"]
            scoped = workspace_store.find(self.conn, email, platform)
            if scoped:
                existing["proxy"] = scoped["proxy"]
            password = password or str(existing.get("password") or "")
            mail_key = mail_key or str(existing.get("mail_key") or "")
            mail_base_url = mail_base_url or str(existing.get("mail_base_url") or "")
            # 只要没有在本次运行中显式指定覆盖代理，账号自身绑定的代理始终优先于全局默认代理
            if existing.get("proxy") and not proxy_override:
                config = replace(
                    config, register_proxy=str(existing["proxy"]), xui_enabled=False
                )
        return _Prepared(email, config, existing, password, mail_key, mail_base_url)

    def _deleted_meanwhile(self, prep: _Prepared, sink) -> bool:
        if prep.existing is not None and db.get_account(self.conn, prep.existing["email"]) is None:
            sink(f"账号 {prep.existing['email']} 已在运行期间被删除，本次结果不再入库。")
            return True
        return False

    def _set_run_email(self, rid, acct_email: str, domain) -> str:
        domain_part = acct_email.split("@", 1)[1] if "@" in acct_email else (domain or "")
        self.conn.execute("UPDATE runs SET email=?, domain=? WHERE id=?",
                          (acct_email, domain_part, rid))
        self.conn.commit()
        return domain_part

    def _bind_kept_window(self, acct_email: str, exc: Exception, sink) -> None:
        wid = str(getattr(exc, "okbrowser_window_id", "") or "")
        if not wid or not acct_email or db.get_account(self.conn, acct_email) is None:
            return
        db.update_account_fields(self.conn, acct_email, {"okbrowser_window_id": wid})
        sink(f"已把保留的 OKBrowser 窗口（id={wid}）绑定到 {acct_email}。")

    def _after_persist(self, acct_email: str, result: dict, group_name: str, sink) -> None:
        """两个平台共用的收尾：OKBrowser 窗口绑定与分组。"""
        if "okbrowser_window_id" in result:
            wid = str(result.get("okbrowser_window_id") or "")
            db.update_account_fields(self.conn, acct_email, {"okbrowser_window_id": wid})
            sink(f"已绑定 OKBrowser 窗口：{wid}" if wid else "已解除 OKBrowser 窗口绑定（窗口已删除）。")
        if group_name:
            db.set_accounts_group(self.conn, [acct_email], group_name)
            sink(f"账号归入分组：{group_name}")

    def _run_claude(self, rid, prep: _Prepared, domain, flow_fn, group_name,
                    sink) -> tuple[str | None, str]:
        """跑 Claude 注册并落库，返回 (账号邮箱或 None, 本平台结果状态)。"""
        config = prep.config
        extra_kw = {}
        if prep.password:
            extra_kw["password"] = prep.password
        if prep.mail_key:
            extra_kw["mail_key"] = prep.mail_key
        if prep.mail_base_url:
            extra_kw["mail_base_url"] = prep.mail_base_url
        if prep.window_id:
            extra_kw["okbrowser_window_id"] = prep.window_id

        result = flow_fn(email=prep.email, domain=domain, config=config, **extra_kw)
        existing = prep.existing
        if self._deleted_meanwhile(prep, sink):
            return None, "needs_manual"
        if isinstance(result, dict) and result.get("email"):
            acct_email = str(result["email"]).strip()
            current = workspace_store.mailbox(self.conn, acct_email)
            if current:
                acct_email = current["email"]
            domain_part = self._set_run_email(rid, acct_email, domain)
            if result.get("sessionKey"):
                acct_status = "success"
            elif result.get("phone_required"):
                acct_status = "phone_required"
            else:
                acct_status = "needs_manual"
            prev = existing or db.get_account(self.conn, acct_email.lower()) or {}
            db.upsert_account(
                self.conn,
                acct_email,
                domain_part,
                None,
                str(result.get("mailbox_id") or prev.get("mailbox_id") or ""),
                rid,
                acct_status,
                password=str(result.get("password") or prep.password or prev.get("password") or ""),
                session_key=str(result.get("sessionKey") or prev.get("session_key") or ""),
                proxy=str(
                    result.get("proxy")
                    or getattr(config, "register_proxy", "")
                    or prev.get("proxy")
                    or ""
                ),
                display_name=str(
                    result.get("display_name") or prev.get("display_name") or ""
                ),
                created_at=str(prev.get("created_at") or self.now_fn()),
                mail_key=str(result.get("mail_key") or prep.mail_key or prev.get("mail_key") or ""),
                mail_base_url=str(
                    result.get("mail_base_url")
                    or prep.mail_base_url
                    or prev.get("mail_base_url")
                    or ""
                ),
            )
            sink(
                f"已保存 Claude 注册结果：{acct_email} status={acct_status} "
                f"sessionKey={'有' if result.get('sessionKey') else '无'}"
            )
            self._after_persist(acct_email, result, group_name, sink)
            workspace_store.record_registration(
                self.conn, acct_email, "claude", acct_status, group=group_name,
                proxy=str(result.get("proxy") or getattr(config, "register_proxy", "")
                          or prev.get("proxy") or ""), now=self.now_fn(),
                got_session=bool(result.get("sessionKey")),
            )
            if result.get("sessionKey") and getattr(
                config, "oauth_auto_after_register", False,
            ):
                self._auto_oauth(acct_email, sink)
            return acct_email, acct_status
        if existing is not None:
            new_status = "success" if existing.get("session_key") else "needs_manual"
            self.conn.execute(
                "UPDATE accounts SET last_run_id=?, status=? WHERE email=?",
                (rid, new_status, existing["email"]),
            )
            self.conn.commit()
            if group_name:
                db.set_accounts_group(self.conn, [existing["email"]], group_name)
            return str(existing["email"]), new_status
        return None, "needs_manual"

    def _run_chatgpt(self, rid, prep: _Prepared, domain, chatgpt_fn, group_name, sink) -> str:
        """跑 ChatGPT 注册；只写 chatgpt_* 字段（及空缺的共用字段），不覆盖 Claude 侧数据。

        返回本平台结果状态：success / banned / phone_required / needs_manual。
        """
        existing = prep.existing or {}
        kw = {
            "email": prep.email,
            "domain": domain,
            "config": prep.config,
            "password": prep.password,
            "chatgpt_password": str(existing.get("chatgpt_password") or ""),
            "mail_key": prep.mail_key,
            "mail_base_url": prep.mail_base_url,
            "okbrowser_window_id": prep.window_id,
            "display_name": str(existing.get("display_name") or ""),
        }
        result = chatgpt_fn(**kw)
        if self._deleted_meanwhile(prep, sink):
            return "needs_manual"
        if not isinstance(result, dict) or not result.get("email"):
            if prep.existing is not None:
                db.update_account_chatgpt(self.conn, prep.existing["email"],
                                          {"chatgpt_last_run_id": rid})
            return "needs_manual"
        acct_email = str(result["email"]).strip().lower()
        current = workspace_store.mailbox(self.conn, acct_email)
        if current:
            acct_email = current["email"]
        domain_part = self._set_run_email(rid, acct_email, domain)
        prev = prep.existing or db.get_account(self.conn, acct_email)
        session_token = str(result.get("chatgpt_session_token") or "")
        access_token = str(result.get("chatgpt_access_token") or "")
        got = bool(session_token or access_token)
        had = bool(prev and (prev.get("chatgpt_session_token") or prev.get("chatgpt_access_token")))
        if result.get("banned"):
            cstatus = "banned"
        elif got:
            cstatus = "success"
        elif result.get("phone_required"):
            cstatus = "phone_required"
        elif had:
            cstatus = "success"
        else:
            cstatus = "needs_manual"

        proxy = str(result.get("proxy") or getattr(prep.config, "register_proxy", "") or "")
        if prev is None:
            db.upsert_account(
                self.conn, acct_email, domain_part, None,
                str(result.get("mailbox_id") or ""), None, "",
                password=str(result.get("password") or ""),
                proxy=proxy,
                display_name=str(result.get("display_name") or ""),
                created_at=self.now_fn(),
                mail_key=str(result.get("mail_key") or ""),
                mail_base_url=str(result.get("mail_base_url") or ""),
            )
        else:
            fill = {}
            if result.get("mail_key") and result["mail_key"] != prev.get("mail_key"):
                fill["mail_key"] = result["mail_key"]
                fill["mail_base_url"] = result.get("mail_base_url") or ""
            for key in ("password", "proxy", "display_name"):
                value = proxy if key == "proxy" else result.get(key)
                if value and not prev.get(key):
                    fill[key] = value
            if fill:
                db.update_account_fields(self.conn, acct_email, fill)

        values = {
            "chatgpt_status": cstatus,
            "chatgpt_password": result.get("chatgpt_password") or "",
            "chatgpt_last_run_id": rid,
        }
        if result.get("chatgpt_password_set"):
            values["chatgpt_password_set"] = "1"
        if cstatus == "banned":
            values.update({"chatgpt_check_status": "banned", "chatgpt_checked_at": self.now_fn()})
        elif got:
            values.update({
                "chatgpt_session_token": session_token
                or (prev or {}).get("chatgpt_session_token") or "",
                "chatgpt_access_token": access_token,
                "chatgpt_token_expires_at": result.get("chatgpt_token_expires_at") or "",
                "chatgpt_account_id": result.get("chatgpt_account_id") or "",
                "chatgpt_session_json": result.get("chatgpt_session_json")
                or (prev or {}).get("chatgpt_session_json") or "",
                "chatgpt_check_status": "alive",
                "chatgpt_checked_at": self.now_fn(),
            })
        db.update_account_chatgpt(self.conn, acct_email, values)
        sink(
            f"已保存 ChatGPT 注册结果：{acct_email} chatgpt_status={cstatus} "
            f"sessionToken={'有' if session_token else '无'} "
            f"accessToken={'有' if access_token else '无'} "
            f"密码登录={'可用' if result.get('chatgpt_password_set') else '未设置'}"
        )
        self._after_persist(acct_email, result, group_name, sink)
        workspace_store.record_registration(
            self.conn, acct_email, "chatgpt", cstatus, group=group_name,
            proxy=proxy or str((prev or {}).get("proxy") or ""), now=self.now_fn(), got_session=got,
        )
        return cstatus

    def _run(self, rid, out_dir: Path, config, email, domain, fns, q, proxy_override: bool = False,
             platform: str = "claude", group_name: str = ""):
        log_path = out_dir / "log.txt"
        fh = log_path.open("a", encoding="utf-8")

        def sink(msg: str):
            fh.write(msg + "\n")
            fh.flush()
            q.put({"type": "log", "line": msg})

        token = console.set_sink(sink)
        token2 = browser.set_output_dir(out_dir)
        status = "success"
        try:
            acct_email = email
            outcomes: list[str] = []
            if platform in ("claude", "both"):
                prep = self._prepare(config, email, proxy_override)
                acct_email, claude_status = self._run_claude(
                    rid, prep, domain, fns["claude"], group_name, sink)
                outcomes.append(claude_status)
            if platform == "both":
                if not acct_email:
                    sink("Claude 注册没有产出邮箱，跳过 ChatGPT 注册。")
                else:
                    sink(f"开始用同一邮箱注册 ChatGPT：{acct_email}")
            if platform == "chatgpt" or (platform == "both" and acct_email):
                prep = self._prepare(config, acct_email, proxy_override, "chatgpt")
                outcomes.append(
                    self._run_chatgpt(rid, prep, domain, fns["chatgpt"], group_name, sink))
            # 流程没抛异常不代表注册成功：任一平台没拿到凭据，整次运行就不算成功
            status = next((s for s in outcomes if s != "success"), "success")
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            sink(f"运行出错：{exc!r}")
            self._bind_kept_window(acct_email, exc, sink)
        finally:
            console.reset_sink(token)
            browser._output_dir.reset(token2)
            db.finish_run(self.conn, rid, status, self.now_fn())
            q.put({"type": "done", "status": status})
            fh.close()
            with self._lock:
                self._active_id = None
                self._queues.pop(rid, None)  # 清理已完成 run 的队列句柄，避免无限累积
