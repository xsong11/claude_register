"""CDP 指纹覆盖：新目标在放行前先拿到 Emulation 覆盖，并递归 auto-attach。"""
from __future__ import annotations

import json
import random
import threading

from claude_register.cdp_override import (
    COMMON_CPU_CORES,
    CdpOverrideSession,
    FingerprintOverrides,
    emulation_commands,
)


class _FakeWs:
    def __init__(self, incoming):
        self.sent = []
        self._incoming = list(incoming)
        self._closed = threading.Event()

    def send(self, data):
        self.sent.append(json.loads(data))

    def __iter__(self):
        for msg in self._incoming:
            yield json.dumps(msg)
        self._closed.wait(5)

    def close(self):
        self._closed.set()


def _attached(session_id, kind, waiting=True):
    return {"method": "Target.attachedToTarget", "params": {
        "sessionId": session_id, "targetInfo": {"type": kind}, "waitingForDebugger": waiting}}


def _run(incoming):
    ws = _FakeWs(incoming)
    s = CdpOverrideSession("ws://x", FingerprintOverrides("America/Chicago", 8),
                           connect_fn=lambda url, **kw: ws).start()
    for _ in range(200):
        if s.attached >= sum(1 for m in incoming if m["params"]["targetInfo"]["type"] in ("page", "iframe")):
            break
        threading.Event().wait(0.01)
    s.stop()
    return ws.sent


def test_browser_level_auto_attach_first():
    sent = _run([])
    assert sent[0]["method"] == "Target.setAutoAttach" and "sessionId" not in sent[0]
    assert sent[0]["params"]["waitForDebuggerOnStart"] is True


def test_page_and_iframe_get_overrides_before_resume():
    sent = _run([_attached("S1", "page"), _attached("S2", "iframe")])
    for sid in ("S1", "S2"):
        methods = [m["method"] for m in sent if m.get("sessionId") == sid]
        assert methods == [
            "Emulation.setTimezoneOverride",
            "Emulation.setHardwareConcurrencyOverride",
            "Target.setAutoAttach",
            "Runtime.runIfWaitingForDebugger",
        ]
    tz = next(m for m in sent if m["method"] == "Emulation.setTimezoneOverride")
    assert tz["params"] == {"timezoneId": "America/Chicago"}


def test_worker_is_only_resumed():
    sent = _run([_attached("W1", "worker"), _attached("P1", "page")])
    assert [m["method"] for m in sent if m.get("sessionId") == "W1"] == [
        "Runtime.runIfWaitingForDebugger"]


def test_randomized_picks_common_values():
    fp = FingerprintOverrides.randomized("Asia/Tokyo", random.Random(1))
    assert fp.timezone == "Asia/Tokyo" and fp.hardware_concurrency in COMMON_CPU_CORES
    assert emulation_commands(FingerprintOverrides()) == []
