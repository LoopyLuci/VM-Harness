"""Podman container backend — real implementation via podman CLI.

Provides a ContainerBackend implementation for Podman using subprocess
calls to the `podman` CLI. Podman is a daemonless container engine that
is Docker-compatible and supports rootless containers.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from vm_harness import _proc
# Suppress CLI console windows on Windows
CREATE_NO_WINDOW = 0x08000000
from typing import Any, List, Optional, Union

from vm_harness.container.backend import (
    CommandResult,
    ContainerBackend,
    ContainerConfig,
    ContainerImage,
    ContainerNetwork,
    ContainerStats,
    ContainerVolume,
)


class PodmanError(Exception):
    """Base exception for Podman backend errors."""
    pass


class PodmanBackend(ContainerBackend):
    """Podman container backend using CLI subprocess calls."""

    def __init__(self, uri: str = "unix:///run/podman/podman.sock"):
        self._uri = uri
        self._connected = False

    async def connect(self) -> None:
        """Verify podman is available."""
        try:
            result = await _proc.run(
                ["podman", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                self._connected = True
            else:
                raise PodmanError("podman CLI not available")
        except FileNotFoundError:
            raise PodmanError("podman CLI not found in PATH")

    async def disconnect(self) -> None:
        """No-op for CLI-based backend."""
        self._connected = False

    async def is_connected(self) -> bool:
        """Check if podman is available."""
        return self._connected

    async def _run_podman(self, args: list, timeout: int = 30) -> str:
        """Run a podman command off the event loop and return stdout."""
        try:
            result = await _proc.run(
                ["podman"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                raise PodmanError(f"podman {' '.join(args)} failed: {result.stderr.strip()}")
            return result.stdout.strip()
        except FileNotFoundError:
            raise PodmanError("podman CLI not found in PATH")
        except subprocess.TimeoutExpired:
            raise PodmanError(f"podman {' '.join(args)} timed out")

    async def list_containers(self, all: bool = True) -> List[Any]:
        """List containers."""
        args = ["ps", "--format", "json"]
        if all:
            args.insert(1, "--all")
        output = await self._run_podman(args)
        if not output:
            return []
        try:
            data = json.loads(output)
            result = []
            for c in data:
                result.append({
                    "id": c.get("Id", ""),
                    "name": c.get("Names", [""])[0] if c.get("Names") else "",
                    "image": c.get("Image", ""),
                    "status": c.get("Status", ""),
                    "state": c.get("State", ""),
                    "ports": c.get("Ports", []),
                })
            return result
        except json.JSONDecodeError:
            return []

    async def get_container(self, container_id: str) -> Any:
        """Get container details."""
        output = await self._run_podman(["inspect", "--format", "json", container_id])
        if not output:
            return None
        try:
            data = json.loads(output)
            if isinstance(data, list) and data:
                c = data[0]
                return {
                    "id": c.get("Id", ""),
                    "name": c.get("Name", "").lstrip("/"),
                    "image": c.get("Image", ""),
                    "status": c.get("State", {}).get("Status", ""),
                    "state": c.get("State", {}).get("Status", ""),
                }
            return data
        except json.JSONDecodeError:
            return None

    async def create_container(self, config: ContainerConfig) -> Any:
        """Create a new container."""
        args = ["create"]
        if config.name:
            args.extend(["--name", config.name])
        for key, val in (config.environment or {}).items():
            args.extend(["--env", f"{key}={val}"])
        args.append(config.image)
        if config.command:
            if isinstance(config.command, list):
                args.extend(config.command)
            else:
                args.extend(["/bin/sh", "-c", config.command])
        output = await self._run_podman(args)
        return {"id": output, "name": config.name}

    async def start_container(self, container_id: str) -> None:
        """Start a container."""
        await self._run_podman(["start", container_id])

    async def stop_container(self, container_id: str, timeout: int = 10) -> None:
        """Stop a container."""
        await self._run_podman(["stop", "--time", str(timeout), container_id])

    async def restart_container(self, container_id: str, timeout: int = 10) -> None:
        """Restart a container."""
        await self._run_podman(["restart", "--time", str(timeout), container_id])

    async def remove_container(
        self, container_id: str, force: bool = False, volumes: bool = False
    ) -> None:
        """Remove a container."""
        args = ["rm"]
        if force:
            args.append("--force")
        if volumes:
            args.append("--volumes")
        args.append(container_id)
        await self._run_podman(args)

    async def exec_command(
        self, container_id: str, command: Union[str, List[str]], tty: bool = False, timeout: int | None = None
    ) -> CommandResult:
        """Execute a command in a container."""
        args = ["exec"]
        if tty:
            args.append("--tty")
        if isinstance(command, list):
            args.extend([container_id] + command)
        else:
            args.extend([container_id, "/bin/sh", "-c", command])
        output = await self._run_podman(args, timeout=timeout or 30)
        return CommandResult(exit_code=0, output=output, stderr="")

    async def get_logs(self, container_id: str, tail: int = 100, **kwargs: Any) -> str:
        """Get container logs."""
        output = await self._run_podman(["logs", "--tail", str(tail), container_id])
        return output

    async def get_stats(self, container_id: str) -> ContainerStats:
        """Get container stats."""
        output = await self._run_podman(["stats", "--no-stream", "--format", "json", container_id])
        try:
            data = json.loads(output)
            if isinstance(data, list) and data:
                s = data[0]
                return ContainerStats(
                    cpu_percent=float(s.get("CPUPerc", "0").replace("%", "")),
                    memory_usage_bytes=int(s.get("MemUsage", "0").split()[0]) if s.get("MemUsage") else 0,
                    memory_limit_bytes=0,
                    network_rx_bytes=0,
                    network_tx_bytes=0,
                    disk_read_bytes=0,
                    disk_write_bytes=0,
                    pids=0,
                )
        except (json.JSONDecodeError, ValueError):
            pass
        return ContainerStats(
            cpu_percent=0.0, memory_usage_bytes=0, memory_limit_bytes=0,
            network_rx_bytes=0, network_tx_bytes=0, disk_read_bytes=0,
            disk_write_bytes=0, pids=0,
        )

    async def list_images(self) -> List[ContainerImage]:
        """List images."""
        output = await self._run_podman(["images", "--format", "json"])
        if not output:
            return []
        try:
            data = json.loads(output)
            return [
                ContainerImage(
                    id=img.get("Id", ""),
                    tags=img.get("RepoTags", []),
                    size=img.get("Size", "0"),
                    created=img.get("Created", ""),
                )
                for img in data
            ]
        except json.JSONDecodeError:
            return []

    async def pull_image(self, image: str, tag: str = "latest") -> None:
        """Pull an image."""
        await self._run_podman(["pull", f"{image}:{tag}"], timeout=300)

    async def remove_image(self, image_id: str, force: bool = False) -> None:
        """Remove an image."""
        args = ["rmi"]
        if force:
            args.append("--force")
        args.append(image_id)
        await self._run_podman(args)

    async def list_networks(self) -> List[ContainerNetwork]:
        """List networks."""
        output = await self._run_podman(["network", "ls", "--format", "json"])
        if not output:
            return []
        try:
            data = json.loads(output)
            return [
                ContainerNetwork(
                    id=net.get("ID", net.get("Id", "")),
                    name=net.get("Name", ""),
                    driver=net.get("Driver", "bridge"),
                )
                for net in data
            ]
        except json.JSONDecodeError:
            return []

    async def create_network(
        self, name: str, driver: str = "bridge", internal: bool = False
    ) -> ContainerNetwork:
        """Create a network."""
        args = ["network", "create", "--driver", driver]
        if internal:
            args.append("--internal")
        args.append(name)
        await self._run_podman(args)
        return ContainerNetwork(id=name, name=name, driver=driver)

    async def list_volumes(self) -> List[ContainerVolume]:
        """List volumes."""
        output = await self._run_podman(["volume", "ls", "--format", "json"])
        if not output:
            return []
        try:
            data = json.loads(output)
            return [
                ContainerVolume(
                    name=vol.get("Name", ""),
                    driver=vol.get("Driver", "local"),
                    mountpoint=vol.get("Mountpoint", ""),
                )
                for vol in data
            ]
        except json.JSONDecodeError:
            return []

    async def create_volume(
        self, name: str, driver: str = "local", labels: dict | None = None
    ) -> ContainerVolume:
        """Create a volume."""
        args = ["volume", "create", "--driver", driver]
        for key, val in (labels or {}).items():
            args.extend(["--label", f"{key}={val}"])
        args.append(name)
        await self._run_podman(args)
        return ContainerVolume(name=name, driver=driver, mountpoint="")
