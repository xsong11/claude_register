"""经独立 CDP 连接给浏览器里的每个页面 / 跨域 iframe 打上指纹覆盖。

OKBrowser 经 API 启动的窗口不应用窗口配置里的时区、CPU 等字段（实测时区固定
"+00:00"、CPU 固定 24），只能由我们在连上后自己覆盖。

用浏览器级 Target.setAutoAttach(waitForDebuggerOnStart) 挂住新目标，在它执行
任何脚本前下发 Emulation 覆盖再放行；对 page/iframe 递归开 auto-attach，
Cloudflare Turnstile 这类跨域 iframe（独立进程）也能覆盖到。
"""
from __future__ import annotations

import itertools
import json
import random
import threading
from dataclasses import dataclass

from websockets.sync.client import connect

COMMON_CPU_CORES = (4, 8, 8, 12, 16)
_EMULATABLE = {"page", "iframe"}


@dataclass(frozen=True)
class FingerprintOverrides:
    """屏幕分辨率不在这里：OKBrowser 内核把 screen.* 硬写成窗口尺寸，CDP 覆盖不了。"""

    timezone: str | None = None
    hardware_concurrency: int | None = None

    @classmethod
    def randomized(cls, timezone: str | None, rng: random.Random | None = None):
        r = rng or random
        return cls(timezone=timezone, hardware_concurrency=r.choice(COMMON_CPU_CORES))

    def describe(self) -> str:
        parts = []
        if self.timezone:
            parts.append(f"时区 {self.timezone}")
        if self.hardware_concurrency:
            parts.append(f"CPU {self.hardware_concurrency} 核")
        return "、".join(parts) or "无"


def emulation_commands(overrides: FingerprintOverrides) -> list[tuple[str, dict]]:
    cmds: list[tuple[str, dict]] = []
    if overrides.timezone:
        cmds.append(("Emulation.setTimezoneOverride", {"timezoneId": overrides.timezone}))
    if overrides.hardware_concurrency:
        cmds.append(("Emulation.setHardwareConcurrencyOverride",
                     {"hardwareConcurrency": overrides.hardware_concurrency}))
    return cmds


class CdpOverrideSession:
    """后台线程维持一条 CDP 连接；stop() 断开后覆盖随会话一起失效。"""

    def __init__(self, ws_url: str, overrides: FingerprintOverrides, *, on_error=None,
                 connect_fn=connect):
        self._ws_url = ws_url
        self._overrides = overrides
        self._on_error = on_error or (lambda msg: None)
        self._connect = connect_fn
        self._ids = itertools.count(1)
        self._send_lock = threading.Lock()
        self._ws = None
        self._thread: threading.Thread | None = None
        self.attached = 0

    def start(self) -> "CdpOverrideSession":
        self._ws = self._connect(self._ws_url, max_size=None, open_timeout=15)
        self._send("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
        })
        self._thread = threading.Thread(target=self._loop, name="cdp-override", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _send(self, method: str, params: dict | None = None, session_id: str | None = None) -> None:
        msg = {"id": next(self._ids), "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        with self._send_lock:
            self._ws.send(json.dumps(msg))

    def _loop(self) -> None:
        try:
            for raw in self._ws:
                msg = json.loads(raw)
                if msg.get("method") == "Target.attachedToTarget":
                    self._on_attached(msg["params"])
                elif "error" in msg:
                    err = msg["error"].get("message", "")
                    # worker 等目标不支持 Emulation 域，属预期。
                    if "wasn't found" not in err and "not supported" not in err.lower():
                        self._on_error(f"CDP 覆盖出错：{err}")
        except Exception:  # noqa: BLE001
            pass  # 连接关闭（stop 或浏览器退出）

    def _on_attached(self, params: dict) -> None:
        session_id = params.get("sessionId")
        kind = (params.get("targetInfo") or {}).get("type", "")
        if not session_id:
            return
        try:
            if kind in _EMULATABLE:
                for method, p in emulation_commands(self._overrides):
                    self._send(method, p, session_id)
                self._send("Target.setAutoAttach", {
                    "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
                }, session_id)
                self.attached += 1
            if params.get("waitingForDebugger"):
                self._send("Runtime.runIfWaitingForDebugger", {}, session_id)
        except Exception as exc:  # noqa: BLE001
            self._on_error(f"CDP 覆盖下发失败：{exc}")
