"""OKBrowser 指纹浏览器本地 API：建窗口 → 启动拿 CDP 地址 → 用完关闭/删除。

文档：https://help.okbrow.com/ （「API说明」）。要点：
- 仅付费版可用；客户端必须开着并已登录，否则返回 code=-4。
- 每台设备每秒最多 1 次请求，所有调用走同一把锁排队。
- 返回统一为 {"code": 0, "msg": ..., "data": ...}，code 非 0 即失败。
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

DEFAULT_API_URL = "http://localhost:30396"
MIN_INTERVAL_S = 1.1

_ERRORS = {
    -1: "参数格式错误",
    -2: "操作失败",
    -4: "账号登录异常（请先打开 OKBrowser 客户端并登录付费账号）",
}
_PROXY_SCHEMES = {"http": "http", "https": "https", "socks5": "socks5",
                  "socks5h": "socks5", "socks": "socks5", "socks4": "socks4"}


class OkBrowserError(RuntimeError):
    pass


_DEBUG_PORT_RE = re.compile(r"--remote-debugging-port=(\d+)")
_PS_LIST_BROWSERS = (
    "Get-CimInstance Win32_Process -Filter \"CommandLine like '%remote-debugging-port%'\""
    " | ForEach-Object { $_.CommandLine }"
)


def _process_cmdlines() -> list[str]:
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_LIST_BROWSERS],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return out.stdout.splitlines()
    lines = []
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            lines.append(cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace"))
        except OSError:
            continue
    return lines


def running_ws(window_id: str) -> str:
    """窗口已在运行时返回它的 CDP 地址，否则返回空串。

    OKBrowser 的 /api/browser/start 对已打开的窗口不返回、一直挂到超时，
    所以先从本机进程的 --remote-debugging-port 找到这个窗口的调试端口。
    """
    window_id = (window_id or "").strip()
    if not window_id:
        return ""
    for cmd in _process_cmdlines():
        if window_id not in cmd or "--type=" in cmd:
            continue
        match = _DEBUG_PORT_RE.search(cmd)
        if not match:
            continue
        try:
            resp = httpx.get(f"http://127.0.0.1:{match.group(1)}/json/version",
                             timeout=3, trust_env=False)
            return str(resp.json().get("webSocketDebuggerUrl") or "")
        except (httpx.HTTPError, ValueError):
            return ""
    return ""


@dataclass(frozen=True)
class OkBrowserSettings:
    api_url: str = DEFAULT_API_URL
    group_id: str = ""
    delete_after: bool = True


def proxy_obj(url: str | None) -> dict | None:
    """代理 URL → OKBrowser 的 proxyObj；空串返回 None（不走代理）。"""
    text = (url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise OkBrowserError("代理地址格式不对") from None
    protocol = _PROXY_SCHEMES.get((parts.scheme or "").lower())
    if not protocol or not parts.hostname or not port:
        raise OkBrowserError(f"OKBrowser 不支持该代理：{parts.scheme}://{parts.hostname}")
    return {
        "protocol": protocol,
        "host": parts.hostname,
        "port": str(port),
        "username": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "apiUrl": "",
    }


class OkBrowserClient:
    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, api_url: str = DEFAULT_API_URL, *, http: httpx.Client | None = None,
                 sleep=time.sleep, clock=time.monotonic, timeout: float = 60.0):
        self.api_url = (api_url or DEFAULT_API_URL).rstrip("/")
        self._http = http or httpx.Client(timeout=timeout, trust_env=False)
        self._sleep = sleep
        self._clock = clock
        self._restarting: set[str] = set()

    def _call(self, method: str, path: str, *, params=None, json=None):
        cls = type(self)
        with cls._lock:
            wait = cls._last_call + MIN_INTERVAL_S - self._clock()
            if wait > 0:
                self._sleep(wait)
            try:
                resp = self._http.request(method, f"{self.api_url}{path}",
                                          params=params, json=json)
            except httpx.HTTPError as exc:
                raise OkBrowserError(
                    f"连不上 OKBrowser 本地 API（{self.api_url}）：{exc}。"
                    "请确认客户端已打开并登录。"
                ) from exc
            finally:
                cls._last_call = self._clock()
        try:
            body = resp.json()
        except ValueError:
            raise OkBrowserError(f"OKBrowser 返回了非 JSON（HTTP {resp.status_code}）") from None
        code = body.get("code") if isinstance(body, dict) else None
        if code != 0:
            reason = _ERRORS.get(code, "未知错误")
            raise OkBrowserError(f"OKBrowser {path} 失败：{reason}（code={code}，{body.get('msg')}）")
        return body.get("data")

    def status(self) -> None:
        self._call("GET", "/api/browser/status")

    def create(self, *, name: str, group_id: str, proxy: str | None = None) -> str:
        obj = proxy_obj(proxy)
        payload = {"name": name, "groupId": group_id, "proxyWay": obj is not None}
        if obj is not None:
            payload["proxyObj"] = obj
        data = self._call("POST", "/api/browser/create", json=payload) or {}
        window_id = str(data.get("id") or "")
        if not window_id:
            raise OkBrowserError("OKBrowser 创建窗口未返回 ID")
        return window_id

    def update_proxy(self, window_id: str, proxy: str | None = None) -> None:
        """更新已有窗口的代理配置；若窗口正在运行则先关闭，以便下次启动时生效。"""
        if running_ws(window_id):
            try:
                self.close(window_id)
            except OkBrowserError:
                pass
            self._restarting.add(window_id)
        obj = proxy_obj(proxy)
        payload: dict = {"id": window_id, "proxyWay": obj is not None}
        if obj is not None:
            payload["proxyObj"] = obj
        self._call("PUT", "/api/browser/edit", json=payload)

    def start(self, window_id: str) -> str:
        if window_id in self._restarting:
            self._restarting.discard(window_id)
        else:
            ws = running_ws(window_id)
            if ws:
                return ws
        data = self._call("GET", "/api/browser/start", params={"browserId": window_id}) or {}
        ws = str(data.get("ws") or "")
        if not ws:
            raise OkBrowserError("OKBrowser 启动窗口未返回 CDP 地址")
        return ws

    def close(self, window_id: str) -> None:
        self._call("GET", "/api/browser/close", params={"browserId": window_id})

    def delete(self, window_id: str) -> None:
        self._call("DELETE", "/api/browser/delete", params={"ids": window_id})
