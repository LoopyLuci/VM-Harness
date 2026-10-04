"""Instrument FrameSubscriber.run's pacing loop.

capture and publish are known-good (87/164 frames produced). Delivery collapses
to 1-2, so the fault is in the subscriber's pacing: it pulls off a latest-wins
queue via

    await asyncio.wait_for(self.queue.get(), timeout=interval - since_last_send)

This wraps asyncio.wait_for to record the timeouts actually used and how many
expire, which distinguishes "the queue is empty" from "timeout computed as ~0,
so wait_for cancels before the getter can run".
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import time

REPO = r"C:\Projects\VM-Harness"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.environ.setdefault("VMHARNESS_BRIDGE_TOKEN", "probe-token")

import streaming_bridge as sb  # noqa: E402

PORT = 8473
VM = "omarchy"

TIMEOUTS: collections.Counter = collections.Counter()
EXPIRED = collections.Counter()
SENDS = collections.Counter()

_real_wait_for = asyncio.wait_for


async def traced_wait_for(aw, timeout=None, **kw):
    try:
        result = await _real_wait_for(aw, timeout, **kw)
    except asyncio.TimeoutError:
        bucket = "0" if not timeout else f"{timeout:.3f}"
        EXPIRED[bucket] += 1
        raise
    bucket = "none" if timeout is None else f"{timeout:.3f}"
    TIMEOUTS[bucket] += 1
    return result


asyncio.wait_for = traced_wait_for


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
            end = time.monotonic() + 6
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frames += 1
    return frames


async def main() -> int:
    bridge = sb.StreamingBridge(port=PORT)
    await bridge.start()
    try:
        for fps in (10, 30):
            TIMEOUTS.clear()
            EXPIRED.clear()
            got = await run_client(fps)
            await asyncio.sleep(0.5)
            print(f"--- fps={fps}: {got} frames delivered ---", flush=True)
            print("  wait_for SUCCEEDED (item retrieved), by timeout value:", flush=True)
            for k, v in sorted(TIMEOUTS.items(), key=lambda kv: -kv[1])[:6]:
                print(f"      {k}: {v}", flush=True)
            print("  wait_for EXPIRED (no item), by timeout value:", flush=True)
            for k, v in sorted(EXPIRED.items(), key=lambda kv: -kv[1])[:6]:
                print(f"      {k}: {v}", flush=True)
            await asyncio.sleep(0.8)
    finally:
        await bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))