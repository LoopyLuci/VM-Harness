"""Where does the frame rate actually die: capture, publish, or send?

Instruments the existing bridge by wrapping methods rather than editing it, so
what is measured is the real code path. Compares fps=10 with fps=30 through the
real WebSocket handler, logging:

  * how many screendumps the pump actually performed
  * how many frames it published
  * how many the subscriber managed to send

The gap between those three numbers says which stage is at fault.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

REPO = r"C:\Projects\VM-Harness"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.environ.setdefault("VMHARNESS_BRIDGE_TOKEN", "probe-token")

import streaming_bridge as sb  # noqa: E402

PORT = 8472
VM = "omarchy"
STATS: dict[str, int] = {"captures": 0, "publishes": 0, "sends": 0}
CAPTURE_TIMES: list[float] = []

_orig_capture = sb.VMStreamSource.capture_frame
_orig_publish = sb.VMStreamSource._publish


async def counting_capture(self):
    t0 = time.monotonic()
    frame = await _orig_capture(self)
    STATS["captures"] += 1
    CAPTURE_TIMES.append(time.monotonic() - t0)
    return frame


def counting_publish(self, kind, payload):
    if kind == "frame":
        STATS["publishes"] += 1
    return _orig_publish(self, kind, payload)


sb.VMStreamSource.capture_frame = counting_capture
sb.VMStreamSource._publish = counting_publish


async def run_client(fps: int) -> int:
    import aiohttp

    frames = 0
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://127.0.0.1:{PORT}/ws/stream") as ws:
            await ws.send_str(json.dumps({"type": "auth", "key": "probe-token"}))
            await asyncio.sleep(0.5)
            await ws.send_str(json.dumps({
                "type": "config", "vm": VM, "quality": 85,
                "fps": fps, "width": 1280, "height": 800, "input_enabled": True,
            }))
            await ws.send_str(json.dumps({"type": "subscribe", "vm": VM}))
            end = time.monotonic() + 8
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frames += 1
                    STATS["sends"] += 1
    return frames


async def main() -> int:
    bridge = sb.StreamingBridge(port=PORT)
    await bridge.start()
    try:
        for fps in (10, 30):
            for key in STATS:
                STATS[key] = 0
            CAPTURE_TIMES.clear()
            got = await run_client(fps)
            await asyncio.sleep(0.5)
            print(f"--- requested fps={fps} ---", flush=True)
            print(f"  frames delivered to client : {got}", flush=True)
            print(f"  screendumps performed      : {STATS['captures']}", flush=True)
            print(f"  frames published           : {STATS['publishes']}", flush=True)
            if CAPTURE_TIMES:
                avg = sum(CAPTURE_TIMES) / len(CAPTURE_TIMES)
                print(f"  capture cost: avg {avg*1000:.0f} ms, max {max(CAPTURE_TIMES)*1000:.0f} ms", flush=True)
            await asyncio.sleep(1.0)
    finally:
        await bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))