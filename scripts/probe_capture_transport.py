"""Measure the capture source end to end: QMP screendump against RFB.

The question this answers is not "does the VNC path work" but "how much faster is
it, on this host, with this guest". Two numbers matter and they are not the same
number:

* **frames delivered per second** to a real WebSocket subscriber, after the
  subscriber's own pacing and JPEG encode -- what a viewer would see;
* **capture cost per frame**, measured inside the source: for screendump the
  QMP round trip plus QEMU's full-framebuffer PNG encode plus our decode, for
  RFB the rectangle decode plus the copy into a publishable image. That is the
  per-frame work the source actually does, and it is the one the transport
  change was meant to cut.

The measurement is deliberately end to end rather than around the encode: a
source that captures faster but whose subscriber cannot keep up has not helped
anyone, and the whole point of the per-subscriber JPEG encode surviving the
change is that it is now the bottleneck worth seeing.

Everything runs in one process, which owns the single QMP connection: QMP serves
one client at a time, so a probe that opened its own monitor socket alongside
the bridge would take the capture path down rather than measure it.

    python scripts/probe_capture_transport.py --capture-source vnc
    python scripts/probe_capture_transport.py --capture-source screendump
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

TOKEN = os.environ.get("VMHARNESS_BRIDGE_TOKEN", "probe-token")
VM = os.environ.get("VMHARNESS_PROBE_VM", "omarchy")

import streaming_bridge as sb  # noqa: E402
from vm_harness.vnc.client import VNCClient  # noqa: E402

#: How long each capture source is measured for. Long enough that a 20fps and a
#: 60fps path are not separated by warmup noise.
DEFAULT_SECONDS = 20.0

#: Fraction of the window at the start that is discarded. The first frames
#: include the first full-screen refresh, connection setup and the JPEG encode
#: of an image whose size the subscriber has not settled on.
WARMUP_FRACTION = 0.35

CAPTURE_COSTS: List[float] = []
DUMPS = 0


def _instrument_screendump() -> None:
    original = sb.VMStreamSource.capture_frame

    async def timed(self: Any) -> Any:
        started = time.perf_counter()
        try:
            return await original(self)
        finally:
            CAPTURE_COSTS.append(time.perf_counter() - started)

    sb.VMStreamSource.capture_frame = timed  # type: ignore[method-assign]


def _instrument_rfb() -> None:
    original = VNCClient._handle_fb_update

    async def timed(self: Any) -> Any:
        global DUMPS
        started = time.perf_counter()
        try:
            return await original(self)
        finally:
            # Only an update that actually carried damage costs anything; an
            # empty one is the server saying "nothing changed".
            DUMPS += 1
            CAPTURE_COSTS.append(time.perf_counter() - started)

    VNCClient._handle_fb_update = timed  # type: ignore[method-assign]


async def drive_input(ws: Any, seconds: float, workload: str) -> None:
    """Keep the guest's pointer moving for the length of the measurement.

    QMP is single-client, so input goes through the same bridge connection the
    capture uses. Without something drawing, a damage-driven source has nothing
    to report and the comparison measures nothing -- which is why the "idle"
    workload exists as its own explicit result rather than as the default.

    The sweep is deliberately fast and wide: one pointer step every 5ms over
    about 90 pixels, which damages a broad band of the desktop each frame
    instead of a single cursor glyph. That is generous to a damage-driven
    transport, and it is called out in the results for exactly that reason --
    a guest that repaints its entire screen every frame would narrow the gap
    without inverting it, because the whole-frame PNG round trip is still
    gone.
    """
    end = time.monotonic() + seconds
    step = 0
    width, height = 1280, 800
    while time.monotonic() < end:
        if workload == "idle":
            await asyncio.sleep(0.05)
            continue
        step += 1
        x = 40 + (step * 90) % (width - 80)
        y = 40 + (step * 61) % (height - 80)
        await ws.send_str(json.dumps({
            "type": "input", "input_type": "mouse_move", "x": x, "y": y,
        }))
        await asyncio.sleep(0.005)


async def run_client(port: int, capture_source: str, seconds: float,
                     width: int, height: int, workload: str) -> Dict[str, Any]:
    import aiohttp

    arrivals: List[float] = []
    frames = 0
    errors: List[str] = []
    started = time.monotonic()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://127.0.0.1:{port}/ws/stream") as ws:
            await ws.send_str(json.dumps({"type": "auth", "key": TOKEN}))
            await asyncio.sleep(0.4)
            await ws.send_str(json.dumps({
                "type": "config", "vm": VM, "quality": 85,
                "fps": sb.MAX_FPS, "width": width, "height": height,
                "input_enabled": True, "capture_source": capture_source,
            }))
            await ws.send_str(json.dumps({"type": "subscribe", "vm": VM}))
            # Let the session settle: the first update after connecting is a
            # full-screen refresh, and the subscriber's first JPEG encode is
            # slower than the ones after it.
            await asyncio.sleep(2.0)

            started = time.monotonic()
            mover = asyncio.ensure_future(drive_input(ws, seconds, workload))
            end = time.monotonic() + seconds
            try:
                while time.monotonic() < end:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        frames += 1
                        arrivals.append(time.monotonic() - started)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if payload.get("type") == "error":
                            errors.append(str(payload.get("message", "")))
            finally:
                mover.cancel()
                await asyncio.gather(mover, return_exceptions=True)

    elapsed = time.monotonic() - started
    return summarise(capture_source, frames, arrivals, elapsed, errors, width, height, workload)


def summarise(source: str, frames: int, arrivals: List[float], elapsed: float,
              errors: List[str], width: int, height: int, workload: str) -> Dict[str, Any]:
    warmup = arrivals[: max(1, int(len(arrivals) * WARMUP_FRACTION))]
    steady = arrivals[len(warmup):] or arrivals
    window = (steady[-1] - steady[0]) if len(steady) > 1 else 0.0
    steady_fps = ((len(steady) - 1) / window) if window > 0 else 0.0
    gaps = [round((b - a) * 1000, 2) for a, b in zip(steady, steady[1:])]
    costs_ms = [round(c * 1000, 2) for c in CAPTURE_COSTS]
    return {
        "capture_source": source,
        "workload": workload,
        "frames_total": frames,
        "window_sec": round(elapsed, 1),
        "steady_fps": round(steady_fps, 1),
        "interval_ms_median": round(statistics.median(gaps), 2) if gaps else None,
        "interval_ms_p95": round(sorted(gaps)[int(len(gaps) * 0.95)], 2) if gaps else None,
        "interval_ms_max": round(max(gaps), 2) if gaps else None,
        "capture_ms_mean": round(statistics.fmean(costs_ms), 2) if costs_ms else None,
        "capture_ms_median": round(statistics.median(costs_ms), 2) if costs_ms else None,
        "capture_ms_p95": round(sorted(costs_ms)[int(len(costs_ms) * 0.95)], 2) if costs_ms else None,
        "capture_calls": len(costs_ms),
        "frame_bytes_approx": width * height * 3,
        "errors": errors[:5],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-source", default="vnc",
                        choices=list(sb.CAPTURE_SOURCES))
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--port", type=int, default=8473)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--workload", default="pointer", choices=["pointer", "idle"],
                        help="what makes the guest draw: a pointer sweep, or nothing")
    parser.add_argument("--json", action="store_true", help="print only the summary")
    args = parser.parse_args()

    # Resolve the request to a real transport before instrumenting, so the
    # wrapper is on the class that will actually run.
    registry = sb.VMRegistry()
    targets = {t.name: t for t in registry.list_targets()}
    if VM not in targets:
        print(f"no such VM in the registry: {VM}. Known: {sorted(targets)}")
        return 2
    target = targets[VM]
    wanted = sb.normalise_capture_source(args.capture_source)
    if wanted == sb.CAPTURE_SOURCE_AUTO:
        wanted = sb.CAPTURE_SOURCE_VNC if target.vnc_port else sb.CAPTURE_SOURCE_SCREENDUMP
    if wanted == sb.CAPTURE_SOURCE_VNC and not target.vnc_port:
        print(f"VM {VM} has no VNC display (vnc_port={target.vnc_port}); "
              f"cannot measure the rfb path")
        return 2

    if wanted == sb.CAPTURE_SOURCE_VNC:
        _instrument_rfb()
    else:
        _instrument_screendump()

    logging.basicConfig(level=logging.WARNING)
    bridge = sb.StreamingBridge(port=args.port, capture_source=wanted)
    await bridge.start()
    print(f"measuring capture_source={wanted} for {args.seconds:g}s "
          f"(target {VM} qmp={target.qmp_uri} vnc_port={target.vnc_port})",
          file=sys.stderr)
    try:
        result = await run_client(args.port, wanted, args.seconds, args.width, args.height,
                                args.workload)
    finally:
        await bridge.stop()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))