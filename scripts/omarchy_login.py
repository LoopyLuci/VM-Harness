"""Sign in to the Omarchy VM using the credentials saved in the GUI.

Drives input through the streaming bridge's WebSocket, which is the same path
the GUI panel uses: auth -> config -> subscribe -> input events -> frames back.
That keeps QEMU's one-QMP-client rule intact (the bridge owns the QMP socket)
while still exercising the real input chain end to end.

Credentials come from the per-VM encrypted store written by the GUI dialog.
The password is never printed, logged, or written anywhere.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Projects\VM-Harness")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

TOKEN = os.environ.get("VMHARNESS_BRIDGE_TOKEN", "")
URL = "ws://127.0.0.1:8445/ws/stream"
VM = "omarchy"
SHOT_DIR = Path(os.environ.get("TEMP", ".")) / "opencode"


async def main() -> int:
    from gui.dialogs_vm_login import load_vm_login
    from vm_harness.guest_input import key_for

    creds = load_vm_login(VM)
    if creds is None:
        print(f"no saved credentials for {VM}")
        return 1
    username, password = creds
    print(f"loaded credentials for {VM}: user={username} password_len={len(password)}")

    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(URL) as ws:
            await ws.send_str(json.dumps({"type": "auth", "key": TOKEN}))
            await asyncio.sleep(0.6)
            await ws.send_str(json.dumps({
                "type": "config", "vm": VM, "quality": 85, "fps": 10,
                "width": 1024, "height": 768, "input_enabled": True,
            }))
            await ws.send_str(json.dumps({"type": "subscribe", "vm": VM}))
            await asyncio.sleep(1.5)

            errors: list[str] = []

            async def drain(seconds: float, tag: str) -> list[bytes]:
                got: list[bytes] = []
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        got.append(msg.data)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get("type") == "error":
                            errors.append(str(data.get("message"))[:160])
                if got:
                    from PIL import Image
                    img = Image.open(io.BytesIO(got[-1]))
                    img.save(SHOT_DIR / f"login_{tag}.png")
                    print(f"  {tag}: {len(got)} frames, last {img.size}", flush=True)
                else:
                    print(f"  {tag}: no frames", flush=True)
                return got

            await drain(4, "before")

            # The greeter shows only a password box for its single account, so
            # the username is not typed; click the field, then the password.
            await ws.send_str(json.dumps({
                "type": "input", "input_type": "mouse_move", "x": 535, "y": 491}))
            await asyncio.sleep(0.3)
            await ws.send_str(json.dumps({
                "type": "input", "input_type": "mouse_click",
                "button": "left", "pressed": True}))
            await asyncio.sleep(0.2)
            await ws.send_str(json.dumps({
                "type": "input", "input_type": "mouse_click",
                "button": "left", "pressed": False}))
            await asyncio.sleep(0.6)

            for ch in password:
                await ws.send_str(json.dumps({
                    "type": "input", "input_type": "key",
                    "key": key_for(ch), "pressed": True}))
                # guest_input documents ~50ms as the floor before a guest drops
                # input; the bridge paces internally too, but do not rely on it.
                await asyncio.sleep(0.09)
            print(f"typed {len(password)} characters", flush=True)
            await drain(2, "typed")

            await ws.send_str(json.dumps({
                "type": "input", "input_type": "key", "key": "ret", "pressed": True}))
            print("submitted", flush=True)

            for i in range(6):
                await drain(10, f"after{i}")
                if errors:
                    break

            if errors:
                print("server errors:", errors[:3], flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))