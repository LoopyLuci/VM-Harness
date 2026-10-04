"""Reproduce the fps=30 stall through the real WebSocket path and report why.

A raw client at fps=10 gets ~140 frames in 8s. At fps=30 it gets exactly one.
This drives the actual bridge over a real socket, then reaches into the
subscriber task to retrieve the exception that kills it -- which nothing in the
bridge does, so it fails silently.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

REPO = r"C:\Projects\VM-Harness"
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.environ.setdefault("VMHARNESS_BRIDGE_TOKEN", "probe-token")

import streaming_bridge as sb  # noqa: E402

PORT = 8471
VM = "omarchy"


async def run_client(fps: int) -> int:
    import aiohttp

    url = f"http://127.0.0.1:{PORT}/ws/stream"
    frames = 0
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url) as ws:
            await ws.send_str(json.dumps({"type": "auth", "key": "probe-token"}))
            await asyncio.sleep(0.5)
            await ws.send_str(json.dumps({
                "type": "config", "vm": VM, "quality": 85,
                "fps": fps, "width": 1280, "height": 800, "input_enabled": True,
            }))
            await ws.send_str(json.dumps({"type": "subscribe", "vm": VM}))
            end = asyncio.get_running_loop().time() + 8
            while asyncio.get_running_loop().time() < end:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if msg.type == aiohttp.WSMsgType.BINARY:
                    frames += 1
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("type") == "error":
                        print("   server error:", str(data.get("message"))[:140], flush=True)
    return frames


async def main() -> int:
    bridge = sb.StreamingBridge(port=PORT)
    await bridge.start()
    try:
        for fps in (10, 30):
            got = await run_client(fps)
            await asyncio.sleep(1.0)
            source = bridge.sources.get(VM)
            print(f"fps={fps}: frames={got}", flush=True)
            if source is None:
                print("   no source", flush=True)
                continue
            print(f"   subscriber_count={source.subscriber_count} target_fps={source.target_fps()}", flush=True)
            for queue, rate in list(source._subscribers.items()):
                print(f"   queue maxsize={queue.maxsize} qsize={queue.qsize()} rate={rate}", flush=True)
            # Find the FrameSubscriber task via the session the bridge kept.
            for session in list(bridge.clients.values()):
                sub = getattr(session, "subscriber", None)
                task = getattr(sub, "_task", None) if sub else None
                if task is None:
                    continue
                print(f"   subscriber task done={task.done()} cancelled={task.cancelled()}", flush=True)
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    print(f"   TASK EXCEPTION: {exc!r}", flush=True)
                    if exc is not None:
                        import traceback
                        traceback.print_exception(
                            type(exc), exc, exc.__traceback__, limit=10
                        )
    finally:
        await bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))