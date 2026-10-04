"""End-to-end test for the container terminal WebSocket.

Tests the full flow:
  1. Start a Docker container (alpine:latest with sleep 300)
  2. Connect to ws://127.0.0.1:8445/terminal/{container_name}
  3. Send {"command": "echo hello"}
  4. Wait for response
  5. Assert "hello" is in the output
  6. Clean up the container
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
import sys
import threading
import time
import unittest
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from streaming_bridge import StreamingBridge  # noqa: E402

import aiohttp  # noqa: E402


def _docker_available() -> bool:
    """Return True if the Docker CLI is reachable."""
    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(_docker_available(), "Docker not available")
class TestTerminalWebSocket(unittest.TestCase):
    """E2E test for the /terminal/{container} WebSocket endpoint."""

    CONTAINER_NAME = "test-terminal-e2e"
    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = 8445
    # The bridge refuses unauthenticated clients, so the test pins the token it
    # starts the bridge with and sends the same one. Without this the socket is
    # closed before the command is ever read.
    TOKEN = "test-terminal-e2e-token"

    # ── Bridge server lifecycle ────────────────────────────────────────────

    @classmethod
    def setUpClass(cls):
        """Start the streaming bridge server in a background thread."""
        os.environ["VMHARNESS_BRIDGE_TOKEN"] = cls.TOKEN
        cls._loop = asyncio.new_event_loop()
        cls._bridge = StreamingBridge()

        async def _run_bridge():
            await cls._bridge.start()

        cls._bridge_thread = threading.Thread(
            target=lambda: cls._loop.run_until_complete(_run_bridge()),
            daemon=True,
        )
        cls._bridge_thread.start()
        # Give the server a moment to bind
        time.sleep(2)

    @classmethod
    def tearDownClass(cls):
        """Stop the streaming bridge server."""
        if hasattr(cls, "_loop") and cls._loop.is_running():
            async def _stop():
                await cls._bridge.stop()

            future = asyncio.run_coroutine_threadsafe(_stop(), cls._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
            cls._loop.call_soon_threadsafe(cls._loop.stop)
            if hasattr(cls, "_bridge_thread"):
                cls._bridge_thread.join(timeout=5)
            if not cls._loop.is_running():
                cls._loop.close()
        elif hasattr(cls, "_loop"):
            cls._loop.close()

    # ── Container lifecycle ───────────────────────────────────────────────

    def setUp(self):
        """Pull alpine and start a fresh container."""
        subprocess.run(
            ["docker", "pull", "alpine:latest"],
            capture_output=True,
            timeout=60,
        )
        # Remove any leftover container from a previous run
        subprocess.run(
            ["docker", "rm", "-f", self.CONTAINER_NAME],
            capture_output=True,
            timeout=10,
        )
        result = subprocess.run(
            [
                "docker", "run", "-d",
                "--name", self.CONTAINER_NAME,
                "alpine:latest", "sleep", "300",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"Failed to start container: {result.stderr}",
        )

    def tearDown(self):
        """Remove the container (ignore errors if already gone)."""
        try:
            subprocess.run(
                ["docker", "rm", "-f", self.CONTAINER_NAME],
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            pass

    # ── Test ──────────────────────────────────────────────────────────────

    def test_terminal_echo(self):
        """Send 'echo hello' via WebSocket and verify 'hello' comes back."""
        ws_url = (
            f"ws://{self.BRIDGE_HOST}:{self.BRIDGE_PORT}"
            f"/terminal/{self.CONTAINER_NAME}"
        )

        output_received = None
        deadline = time.time() + 10

        async def _run_client():
            nonlocal output_received
            session = aiohttp.ClientSession()
            try:
                async with session.ws_connect(ws_url) as ws:
                    # The bridge requires auth as the first message. There is no
                    # unauthenticated path by design: an open docker exec beside
                    # an authenticated frame stream would make the frame
                    # stream's auth decorative.
                    await ws.send_str(json.dumps({"type": "auth", "key": self.TOKEN}))
                    await ws.send_str(json.dumps({"command": "echo hello"}))
                    while time.time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                if "output" in data:
                                    output_received = data["output"]
                                    if "hello" in output_received:
                                        return
                            elif msg.type == aiohttp.WSMsgType.CLOSED:
                                return
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                return
                        except asyncio.TimeoutError:
                            continue
            finally:
                await session.close()

        asyncio.run(_run_client())

        self.assertIsNotNone(
            output_received,
            "No output received from container — "
            "docker exec may have failed (e.g. TTY issue with -it flag)",
        )
        self.assertIn(
            "hello",
            output_received,
            f"Expected 'hello' in output, got: {output_received!r}",
        )
