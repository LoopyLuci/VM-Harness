"""Every operation in the catalog.

Most are generated from the backends' own interfaces, so the catalog grows with them:

* ``vm.*``          every ``HypervisorBackend`` method, on any hypervisor (``backend=`` picks one; otherwise the VM is
                     looked up on every hypervisor available here)
* ``container.*``, ``image.*``, ``network.*``, ``volume.*``  every ``ContainerBackend`` method, on Docker or Podman
* ``k8s.*``         every public ``KubernetesBackend`` method
* ``compose.*``     Docker Compose stacks

and some are written here because no backend method covers them: host facts, backend settings, raw QMP, qemu-img,
extra Docker features (inspect, pause, top, build, prune, ...), ISO images, and the audit log. ``gui.*`` operations
are added by the hub when a GUI is attached.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Optional

from vm_harness import _proc
from vm_harness.control import schema as S
from vm_harness.control.catalog import Catalog, OperationError
from vm_harness.control.engine import CONTAINER_ENGINES, HYPERVISORS, Engine, home, host_info

# ---- generated operations -----------------------------------------------------------------------------------------
# method name -> (operation id, mutating, destructive, summary override)
VM_METHODS: dict[str, tuple[str, bool, bool, str]] = {
    "create_vm": ("vm.create", True, False, "Create a VM from a configuration (backend= is required)"),
    "destroy_vm": ("vm.destroy", True, True, "Delete a VM and its files"),
    "start_vm": ("vm.start", True, False, ""),
    "stop_vm": ("vm.stop", True, False, "Stop a VM (force=true pulls the plug)"),
    "pause_vm": ("vm.pause", True, False, ""),
    "resume_vm": ("vm.resume", True, False, ""),
    "reset_vm": ("vm.reset", True, False, "Hard-reset a VM"),
    "reboot_vm": ("vm.reboot", True, False, ""),
    "shutdown_guest": ("vm.shutdown_guest", True, False, "Ask the guest OS to shut down"),
    "get_status": ("vm.status", False, False, "A VM's state, resources and addresses"),
    "get_config": ("vm.config", False, False, "A VM's configuration"),
    "update_config": ("vm.update_config", True, False, ""),
    "get_metrics": ("vm.metrics", False, False, "CPU, memory, disk and network counters right now"),
    "get_display": ("vm.display", False, False, "How to connect to a VM's screen (SPICE, VNC, RDP)"),
    "set_display_password": ("vm.display_password", True, False, ""),
    "screenshot": ("vm.screenshot", False, False, "A picture of a VM's screen (PNG, base64)"),
    "get_console": ("vm.console", False, False, ""),
    "send_console_data": ("vm.console_send", True, False, "Type into a VM's serial console"),
    "receive_console_data": ("vm.console_read", False, False, ""),
    "guest_exec": ("vm.exec", True, False, "Run a command inside the guest"),
    "guest_info": ("vm.guest_info", False, False, "Hostname, OS and addresses reported by the guest"),
    "guest_file_read": ("vm.file_read", False, False, "Read a file inside the guest"),
    "guest_file_write": ("vm.file_write", True, False, "Write a file inside the guest"),
    "list_snapshots": ("vm.snapshot.list", False, False, ""),
    "create_snapshot": ("vm.snapshot.create", True, False, ""),
    "restore_snapshot": ("vm.snapshot.restore", True, True, "Roll a VM back to a snapshot (its current state is lost)"),
    "delete_snapshot": ("vm.snapshot.delete", True, True, ""),
    "resize_disk": ("vm.disk.resize", True, False, ""),
    "add_disk": ("vm.disk.add", True, False, ""),
    "eject_cdrom": ("vm.cdrom.eject", True, False, ""),
    "insert_cdrom": ("vm.cdrom.insert", True, False, "Insert an ISO image into a VM's CD drive"),
    "list_network_interfaces": ("vm.nic.list", False, False, ""),
    "add_network_interface": ("vm.nic.add", True, False, ""),
    "remove_network_interface": ("vm.nic.remove", True, False, ""),
    "connect_network": ("vm.nic.connect", True, False, "Connect or disconnect a network adapter"),
    "migrate_vm": ("vm.migrate", True, False, ""),
    "export_vm": ("vm.export", True, False, ""),
    "import_vm": ("vm.import", True, False, "Import a VM from an exported file (backend= is required)"),
    "clone_vm": ("vm.clone", True, False, ""),
    "set_resource_limits": ("vm.limits", True, False, "Cap a VM's memory, CPUs, CPU weight or disk bandwidth"),
    "attach_usb": ("vm.usb.attach", True, False, "Pass a host USB device through to a VM"),
    "detach_usb": ("vm.usb.detach", True, False, ""),
}

CONTAINER_METHODS: dict[str, tuple[str, bool, bool, str]] = {
    "list_containers": ("container.list", False, False, "Containers (all=false: running only)"),
    "get_container": ("container.get", False, False, ""),
    "create_container": ("container.create", True, False, ""),
    "start_container": ("container.start", True, False, ""),
    "stop_container": ("container.stop", True, False, ""),
    "restart_container": ("container.restart", True, False, ""),
    "remove_container": ("container.remove", True, True, ""),
    "exec_command": ("container.exec", True, False, "Run a command in a running container"),
    "get_logs": ("container.logs", False, False, ""),
    "get_stats": ("container.stats", False, False, "CPU, memory and I/O of a container right now"),
    "list_images": ("image.list", False, False, ""),
    "pull_image": ("image.pull", True, False, ""),
    "remove_image": ("image.remove", True, True, ""),
    "list_networks": ("network.list", False, False, ""),
    "create_network": ("network.create", True, False, ""),
    "list_volumes": ("volume.list", False, False, ""),
    "create_volume": ("volume.create", True, False, ""),
}

K8S_SKIP = {"connect", "disconnect", "is_connected"} | set(CONTAINER_METHODS)
K8S_MUTATING = ("create", "delete", "apply", "scale", "switch", "exec", "restart")


def _wrapper(method: Callable, extra: list[inspect.Parameter], call: Callable[..., Any]) -> Callable:
    """A function with the method's parameters (minus self) plus `extra`, whose body is `call(**kwargs)`."""
    sig = inspect.signature(method)
    params = [p for n, p in sig.parameters.items() if n != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    names = {p.name for p in params}
    params += [p for p in extra if p.name not in names]
    # Required parameters first, as Python signatures demand.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    hints = S._hints(method)

    async def handler(**kwargs: Any) -> Any:
        return await call(**kwargs)

    handler.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    handler.__annotations__ = {p.name: hints.get(p.name, p.annotation) for p in params}
    handler.__doc__ = method.__doc__
    return handler


def _add_vm_ops(cat: Catalog, engine: Engine) -> None:
    from vm_harness.hypervisor.backend import HypervisorBackend

    backend_param = inspect.Parameter("backend", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str)
    for method_name, (op_id, mutating, destructive, summary) in VM_METHODS.items():
        method = getattr(HypervisorBackend, method_name)
        needs_backend = method_name in ("create_vm", "import_vm")

        def make(method_name: str = method_name, needs_backend: bool = needs_backend) -> Callable[..., Any]:
            async def call(backend: str = "", **kw: Any) -> Any:
                if needs_backend:
                    if not backend:
                        raise OperationError("backend= is required (one of: " + ", ".join(HYPERVISORS) + ")")
                    b = await engine.hypervisor(backend)
                else:
                    vm = kw.get("name")
                    backend, b = await engine.locate(vm, backend)
                try:
                    result = await getattr(b, method_name)(**kw)
                except NotImplementedError:
                    raise OperationError(f"{backend} does not support {method_name}", code="unsupported", status=501)
                if method_name == "screenshot" and isinstance(result, (bytes, bytearray)):
                    return {"format": "png" if bytes(result[:8]) == b"\x89PNG\r\n\x1a\n" else "ppm",
                            "base64": base64.b64encode(bytes(result)).decode("ascii"), "bytes": len(result)}
                return result
            return call

        handler = _wrapper(method, [backend_param], make())
        params = S.signature_schema(handler)
        params["properties"]["backend"]["description"] = (
            "Hypervisor: " + ", ".join(HYPERVISORS) + (". Required." if needs_backend else ". Optional: found from the VM name."))
        if needs_backend:
            params.setdefault("required", []).append("backend")
        cat.add(op_id, handler, group="vm", summary=summary or S.first_line(method.__doc__), params=params,
                mutating=mutating, destructive=destructive, long_running=method_name in (
                    "create_vm", "clone_vm", "export_vm", "import_vm", "migrate_vm", "stop_vm", "shutdown_guest"))

    @cat.op("vm.list", group="vm")
    async def vm_list(backend: str = "") -> list[dict]:
        """Every VM on every hypervisor available here (or on one), with its state"""
        names = [backend] if backend else await engine.available_hypervisors()

        async def one(bname: str) -> list[dict]:
            b = await engine.hypervisor(bname)
            out = []
            for vm in await b.list_vms():
                try:
                    st = await b.get_status(vm)
                    out.append({"name": vm, "backend": bname, "state": S.to_json(st.state), "status": S.to_json(st)})
                except Exception as e:  # noqa: BLE001
                    out.append({"name": vm, "backend": bname, "state": "error", "error": str(e)[:300]})
            return out

        results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
        vms: list[dict] = []
        for n, r in zip(names, results):
            if isinstance(r, Exception):
                vms.append({"backend": n, "error": str(r)[:300]})
            else:
                vms.extend(r)
        return vms


def _add_container_ops(cat: Catalog, engine: Engine) -> None:
    from vm_harness.container.backend import ContainerBackend

    engine_param = inspect.Parameter("engine", inspect.Parameter.KEYWORD_ONLY, default="docker", annotation=str)
    for method_name, (op_id, mutating, destructive, summary) in CONTAINER_METHODS.items():
        method = getattr(ContainerBackend, method_name)

        def make(method_name: str = method_name) -> Callable[..., Any]:
            async def call(engine: str = "docker", **kw: Any) -> Any:
                b = await _engine_obj.container(engine)
                return await getattr(b, method_name)(**kw)
            return call

        _engine_obj = engine
        handler = _wrapper(method, [engine_param], make())
        params = S.signature_schema(handler)
        params["properties"]["engine"]["enum"] = list(CONTAINER_ENGINES)
        cat.add(op_id, handler, group=op_id.split(".")[0], summary=summary or S.first_line(method.__doc__),
                params=params, mutating=mutating, destructive=destructive,
                long_running=method_name in ("pull_image",))


def _add_k8s_ops(cat: Catalog, engine: Engine) -> None:
    from vm_harness.container.kubernetes.backend import KubernetesBackend

    for method_name, method in inspect.getmembers(KubernetesBackend, inspect.iscoroutinefunction):
        if method_name.startswith("_") or method_name in K8S_SKIP:
            continue

        def make(method_name: str = method_name) -> Callable[..., Any]:
            async def call(**kw: Any) -> Any:
                b = await engine.kubernetes()
                result = await getattr(b, method_name)(**kw)
                if method_name == "switch_context":
                    engine.forget_kubernetes()
                return result
            return call

        handler = _wrapper(method, [], make())
        mutating = method_name.startswith(K8S_MUTATING)
        cat.add(f"k8s.{method_name}", handler, group="k8s", mutating=mutating,
                destructive=method_name.startswith("delete"))


def _add_compose_ops(cat: Catalog, engine: Engine) -> None:
    from vm_harness.container.docker.compose import DockerCompose

    compose: dict[str, DockerCompose] = {}

    def get() -> DockerCompose:
        if "c" not in compose:
            compose["c"] = DockerCompose()
        return compose["c"]

    for method_name, (op_id, mutating, destructive) in {
        "deploy": ("compose.up", True, False), "remove": ("compose.down", True, True),
        "list_stacks": ("compose.list", False, False), "get_services": ("compose.services", False, False),
        "restart_service": ("compose.restart", True, False), "scale_service": ("compose.scale", True, False),
        "get_logs": ("compose.logs", False, False),
    }.items():
        method = getattr(DockerCompose, method_name)

        def make(method_name: str = method_name) -> Callable[..., Any]:
            async def call(**kw: Any) -> Any:
                return await getattr(get(), method_name)(**kw)
            return call

        cat.add(op_id, _wrapper(method, [], make()), group="compose", mutating=mutating, destructive=destructive,
                long_running=method_name == "deploy")


# ---- hand-written operations ---------------------------------------------------------------------------------------
def _add_host_ops(cat: Catalog, engine: Engine) -> None:
    @cat.op("host.info", group="host")
    async def info() -> dict:
        """This machine: OS, CPUs, memory, whether hardware virtualization is on"""
        return await asyncio.to_thread(host_info)

    @cat.op("host.capabilities", group="host")
    async def capabilities() -> dict:
        """What this machine can actually run: usable accelerators, and CPU/RAM sizing that leaves the host room"""
        from vm_harness import env

        caps = await asyncio.to_thread(env.host_capabilities)
        payload = caps.to_dict()
        payload["suggested"] = {
            "vcpus": caps.suggest_cpu_count(),
            "ram_mb": caps.suggest_ram_mb(),
        }
        return payload

    @cat.op("guest.type", group="guest", mutating=True)
    async def guest_type(qmp_uri: str, text: str, key_delay_sec: float = 0.05,
                         submit: bool = False) -> dict:
        """Type text into a running guest's console

        Reaches an interactive guest -- a login prompt, a confirmation, an
        installer question -- which is otherwise unreachable once its serial
        log goes quiet. Goes through the guest's PS/2 keyboard, so no extra
        device is needed on the VM's command line.

        Typing is character-by-character with a delay, because guests drop
        input that arrives faster than they poll the keyboard. `submit` sends
        Return afterwards. Verify with guest.screendump: there is no way to
        read what the guest drew, so assume nothing landed until you look."""
        from vm_harness.guest_input import GuestKeyboard
        from vm_harness.qmp_client import QMPClient

        client = QMPClient(qmp_uri)
        await client.connect()
        try:
            keyboard = GuestKeyboard(client, key_delay_sec=key_delay_sec)
            await keyboard.type_text(text)
            if submit:
                await keyboard.press("ret")
            return {"typed": len(text), "submitted": bool(submit)}
        finally:
            await client.disconnect()

    @cat.op("guest.press", group="guest", mutating=True)
    async def guest_press(qmp_uri: str, key: str, times: int = 1,
                          key_delay_sec: float = 0.05) -> dict:
        """Press a named key in a guest: ret, tab, esc, backspace, up, home

        For clearing a field that already has text in it, or answering a
        prompt the guest is stuck on."""
        from vm_harness.guest_input import GuestKeyboard
        from vm_harness.qmp_client import QMPClient

        client = QMPClient(qmp_uri)
        await client.connect()
        try:
            keyboard = GuestKeyboard(client, key_delay_sec=key_delay_sec)
            await keyboard.press(key, times=times)
            return {"key": key, "times": times}
        finally:
            await client.disconnect()

    @cat.op("guest.screendump", group="guest")
    async def guest_screendump(qmp_uri: str, path: str) -> dict:
        """Photograph what the guest is drawing right now

        The only way to see a guest that draws to a TTY: a graphical console
        has no serial equivalent, and an installer's progress text never
        reaches the log. Writes a PPM; convert with Pillow to view it."""
        from vm_harness.guest_input import screendump
        from vm_harness.qmp_client import QMPClient

        client = QMPClient(qmp_uri)
        await client.connect()
        try:
            written = await screendump(client, path)
            return {"path": str(written), "bytes": written.stat().st_size}
        finally:
            await client.disconnect()

    @cat.op("cloudinit.backends", group="cloudinit")
    async def seed_backends() -> dict:
        """Which NoCloud seed builders this host can use"""
        from vm_harness.cloudinit import SeedBuilder

        return SeedBuilder().describe()

    @cat.op("cloudinit.build", group="cloudinit", mutating=True)
    async def seed_build(path: str, hostname: str = "", timezone: str = "", username: str = "",
                         password_hash: str = "", full_name: str = "", email: str = "",
                         encrypt: bool = False, files: dict | None = None,
                         required_files: list | None = None, backend: str = "auto") -> dict:
        """Write a cloud-init NoCloud seed (labelled cidata) and verify it

        Attach the result to a VM as a second CD-ROM and the guest configures
        itself instead of showing a setup wizard. `files` carries
        installer-specific documents (e.g. Omarchy's user_configuration.json).
        The seed is verified before this returns, so a malformed image is an
        error rather than a silent boot to an interactive wizard."""
        from vm_harness.cloudinit import NoCloudSeed, SeedBuilder, verify_seed

        required = tuple(required_files or (sorted(files) if files else ()))
        seed = NoCloudSeed(
            hostname=hostname or None,
            timezone=timezone or None,
            username=username or None,
            password_hash=password_hash or None,
            full_name=full_name or None,
            email=email or None,
            extra_files=files or {},
            encrypt=bool(encrypt),
            required_files=required,
        )
        written = await asyncio.to_thread(SeedBuilder(backend).build, seed, path)
        return {"path": str(written), "bytes": written.stat().st_size,
                "verified": verify_seed(written, required), "label": seed.label}

    @cat.op("host.backends", group="host")
    async def backends() -> dict:
        """Which hypervisors and container engines work here, their versions, and why the others do not"""
        hv, ce = await asyncio.gather(engine.hypervisors(), engine.container_engines())
        return {"hypervisors": hv, "containers": ce}

    @cat.op("host.settings", group="host")
    async def settings() -> dict:
        """Per-backend settings (paths, guest credentials, kubeconfig...). Secret values are hidden"""
        return {b: {k: ("***" if re.search(r"pass|secret|token|key", k, re.I) and v else v) for k, v in vals.items()}
                for b, vals in engine.settings().items()}

    @cat.op("host.set_settings", group="host", mutating=True)
    async def set_settings(backend: str, values: dict) -> dict:
        """Change a backend's settings (a null value removes one). The backend restarts with them on next use"""
        allowed = set(HYPERVISORS) | set(CONTAINER_ENGINES) | {"kubernetes", "iso"}
        if backend not in allowed:
            raise OperationError(f"backend must be one of {', '.join(sorted(allowed))}")
        engine.save_settings(backend, values)
        return {"backend": backend, "saved": sorted(values)}


def _qemu_img() -> str:
    from vm_harness.hypervisor.qemu.backend import find_qemu
    tool = find_qemu("qemu-img")
    if not tool:
        raise OperationError("qemu-img not found (install QEMU, or set VMH_QEMU_DIR)", code="unavailable", status=409)
    return tool


async def _qimg(*args: str, timeout: float = 3600) -> str:
    r = await _proc.run([_qemu_img(), *args], timeout=timeout)
    if r.returncode != 0:
        raise OperationError((r.stderr or r.stdout).strip()[-1000:] or f"qemu-img exited {r.returncode}")
    return r.stdout


def _add_qemu_ops(cat: Catalog, engine: Engine) -> None:
    @cat.op("qemu.qmp", group="qemu", mutating=True)
    async def qmp(name: str, command: str, arguments: Optional[dict] = None) -> Any:
        """Send any QMP command to a running QEMU VM and return QEMU's answer"""
        b = await engine.hypervisor("qemu")
        client = await b._require_qmp_client(name)
        reply = await client.send(command, arguments or None)
        return reply.get("return", reply)

    @cat.op("qemu.events", group="qemu")
    async def qmp_events(name: str, limit: int = 50) -> list:
        """The latest QMP events (STOP, RESUME, SHUTDOWN, ...) a running QEMU VM has sent"""
        b = await engine.hypervisor("qemu")
        client = await b._require_qmp_client(name)
        return list(client.events)[-limit:]

    @cat.op("qemu.img_info", group="qemu")
    async def img_info(path: str) -> dict:
        """Format, virtual size, disk use, backing file and snapshots of a disk image"""
        return json.loads(await _qimg("info", "--output=json", "-U", path, timeout=60))

    @cat.op("qemu.img_create", group="qemu", mutating=True)
    async def img_create(path: str, size: str, format: str = "qcow2", backing_file: str = "") -> dict:
        """Create a disk image (size like 20G). With backing_file, a thin copy-on-write overlay of it"""
        if Path(path).exists():
            raise OperationError(f"{path} already exists")
        args = ["create", "-f", format]
        if backing_file:
            fmt = json.loads(await _qimg("info", "--output=json", "-U", backing_file, timeout=60)).get("format", "qcow2")
            args += ["-b", backing_file, "-F", fmt]
        await _qimg(*args, path, size)
        return {"path": path, "size": size, "format": format}

    @cat.op("qemu.img_convert", group="qemu", mutating=True, long_running=True)
    async def img_convert(source: str, destination: str, format: str = "qcow2", compress: bool = False) -> dict:
        """Convert a disk image between formats (qcow2, raw, vmdk, vdi, vhdx, vpc)"""
        if Path(destination).exists():
            raise OperationError(f"{destination} already exists")
        await _qimg("convert", "-p", "-O", format, *(["-c"] if compress else []), source, destination)
        return {"destination": destination, "format": format, "bytes": Path(destination).stat().st_size}

    @cat.op("qemu.img_resize", group="qemu", mutating=True)
    async def img_resize(path: str, size: str, shrink: bool = False) -> dict:
        """Grow (or with shrink=true, shrink) a disk image: size like 40G or +10G"""
        await _qimg("resize", *(["--shrink"] if shrink else []), path, size)
        return json.loads(await _qimg("info", "--output=json", "-U", path, timeout=60))

    @cat.op("qemu.img_check", group="qemu")
    async def img_check(path: str) -> dict:
        """Check a qcow2 image for corruption and leaks"""
        r = await _proc.run([_qemu_img(), "check", "--output=json", "-U", path], timeout=3600)
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise OperationError((r.stderr or r.stdout).strip()[-800:])

    @cat.op("qemu.img_snapshots", group="qemu")
    async def img_snapshots(path: str) -> list:
        """Internal snapshots stored in a qcow2 image"""
        return json.loads(await _qimg("info", "--output=json", "-U", path, timeout=60)).get("snapshots", [])

    @cat.op("qemu.img_snapshot", group="qemu", mutating=True)
    async def img_snapshot(path: str, action: str, snapshot: str) -> dict:
        """Create, apply or delete an internal snapshot of a qcow2 image (the VM must be off)"""
        flag = {"create": "-c", "apply": "-a", "delete": "-d"}.get(action)
        if not flag:
            raise OperationError("action must be create, apply or delete")
        await _qimg("snapshot", flag, snapshot, path)
        return {"path": path, "action": action, "snapshot": snapshot}


def _add_docker_extras(cat: Catalog, engine: Engine) -> None:
    async def client():
        b = await engine.container("docker")
        return b._ensure_connected()

    def sync(fn: Callable[[], Any]) -> Any:
        return asyncio.to_thread(fn)

    @cat.op("docker.info", group="docker")
    async def info() -> dict:
        """Docker's own report: version, storage driver, containers, images, CPUs, memory"""
        c = await client()
        info, version = await asyncio.gather(sync(c.info), sync(c.version))
        keep = ("Containers", "ContainersRunning", "ContainersPaused", "ContainersStopped", "Images", "Driver",
                "OperatingSystem", "OSType", "Architecture", "NCPU", "MemTotal", "ServerVersion", "Name")
        return {"info": {k: info.get(k) for k in keep}, "version": version}

    @cat.op("docker.disk_usage", group="docker")
    async def disk_usage() -> dict:
        """Space used by images, containers, volumes and the build cache"""
        c = await client()
        df = await sync(c.df)
        return {"images_bytes": sum(i.get("Size", 0) for i in df.get("Images") or []),
                "containers_bytes": sum(x.get("SizeRw", 0) or 0 for x in df.get("Containers") or []),
                "volumes_bytes": sum((v.get("UsageData") or {}).get("Size", 0) for v in df.get("Volumes") or []),
                "build_cache_bytes": sum(b.get("Size", 0) for b in df.get("BuildCache") or [])}

    @cat.op("docker.prune", group="docker", destructive=True)
    async def prune(what: str = "containers") -> dict:
        """Remove stopped containers, dangling images, unused networks, unused volumes or build cache"""
        c = await client()
        fn = {"containers": c.containers.prune, "images": c.images.prune, "networks": c.networks.prune,
              "volumes": c.volumes.prune, "build_cache": lambda: c.api.prune_builds()}.get(what)
        if fn is None:
            raise OperationError("what must be containers, images, networks, volumes or build_cache")
        return await sync(fn)

    @cat.op("container.inspect", group="container")
    async def c_inspect(container_id: str) -> dict:
        """Docker's full description of a container (config, mounts, network, state)"""
        c = await client()
        return (await sync(lambda: c.containers.get(container_id))).attrs

    @cat.op("container.top", group="container")
    async def c_top(container_id: str) -> dict:
        """The processes running in a container"""
        c = await client()
        return await sync(lambda: c.containers.get(container_id).top())

    for verb in ("pause", "unpause"):
        def make(verb: str = verb) -> Callable:
            async def fn(container_id: str) -> dict:
                c = await client()
                await sync(lambda: getattr(c.containers.get(container_id), verb)())
                return {"container": container_id, verb: True}
            fn.__doc__ = f"{verb.capitalize()} a running container"
            return fn
        cat.add(f"container.{verb}", make(), group="container", mutating=True)

    @cat.op("container.rename", group="container", mutating=True)
    async def c_rename(container_id: str, new_name: str) -> dict:
        """Rename a container"""
        c = await client()
        await sync(lambda: c.containers.get(container_id).rename(new_name))
        return {"container": container_id, "name": new_name}

    @cat.op("container.copy_from", group="container")
    async def c_copy_from(container_id: str, path: str) -> dict:
        """Read a file from a container (base64; up to 20 MB)"""
        import io
        import tarfile
        c = await client()

        def read() -> bytes:
            stream, stat = c.containers.get(container_id).get_archive(path)
            if stat.get("size", 0) > 20 * 2**20:
                raise OperationError("file is larger than 20 MB")
            buf = io.BytesIO(b"".join(stream))
            with tarfile.open(fileobj=buf) as tar:
                member = tar.next()
                f = tar.extractfile(member) if member else None
                if f is None:
                    raise OperationError(f"{path} is not a regular file")
                return f.read()

        data = await sync(read)
        return {"path": path, "bytes": len(data), "base64": base64.b64encode(data).decode("ascii")}

    @cat.op("container.copy_to", group="container", mutating=True)
    async def c_copy_to(container_id: str, path: str, content_base64: str, mode: int = 0o644) -> dict:
        """Write a file into a container (the parent folder must exist)"""
        import io
        import tarfile
        c = await client()
        data = base64.b64decode(content_base64)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(Path(path).name)
            info.size, info.mode, info.mtime = len(data), mode, int(time.time())
            tar.addfile(info, io.BytesIO(data))
        parent = str(Path(path).parent).replace("\\", "/")
        ok = await sync(lambda: c.containers.get(container_id).put_archive(parent, buf.getvalue()))
        if not ok:
            raise OperationError(f"could not write {path}")
        return {"path": path, "bytes": len(data)}

    @cat.op("image.inspect", group="image")
    async def i_inspect(image: str) -> dict:
        """Docker's full description of an image"""
        c = await client()
        return (await sync(lambda: c.images.get(image))).attrs

    @cat.op("image.history", group="image")
    async def i_history(image: str) -> list:
        """The layers an image was built from"""
        c = await client()
        return await sync(lambda: c.images.get(image).history())

    @cat.op("image.tag", group="image", mutating=True)
    async def i_tag(image: str, repository: str, tag: str = "latest") -> dict:
        """Give an image another name"""
        c = await client()
        await sync(lambda: c.images.get(image).tag(repository, tag))
        return {"image": image, "tagged": f"{repository}:{tag}"}

    @cat.op("image.build", group="image", mutating=True, long_running=True)
    async def i_build(path: str, tag: str, dockerfile: str = "Dockerfile", buildargs: Optional[dict] = None,
                      pull: bool = False, nocache: bool = False) -> dict:
        """Build an image from a folder with a Dockerfile"""
        c = await client()

        def build() -> dict:
            image, logs = c.images.build(path=path, tag=tag, dockerfile=dockerfile, buildargs=buildargs or {},
                                         pull=pull, nocache=nocache, rm=True)
            lines = [str(x.get("stream", "")).rstrip() for x in logs if x.get("stream")]
            return {"id": image.id, "tags": image.tags, "log_tail": [l for l in lines if l][-40:]}

        return await sync(build)

    @cat.op("network.remove", group="network", destructive=True)
    async def n_remove(network: str) -> dict:
        """Delete a Docker network"""
        c = await client()
        await sync(lambda: c.networks.get(network).remove())
        return {"network": network, "removed": True}

    for verb in ("connect", "disconnect"):
        def make_n(verb: str = verb) -> Callable:
            async def fn(network: str, container_id: str) -> dict:
                c = await client()
                await sync(lambda: getattr(c.networks.get(network), verb)(container_id))
                return {"network": network, "container": container_id, verb: True}
            fn.__doc__ = f"{verb.capitalize()} a container {'to' if verb == 'connect' else 'from'} a network"
            return fn
        cat.add(f"network.{verb}", make_n(), group="network", mutating=True)

    @cat.op("volume.remove", group="volume", destructive=True)
    async def v_remove(volume: str, force: bool = False) -> dict:
        """Delete a Docker volume and everything in it"""
        c = await client()
        await sync(lambda: c.volumes.get(volume).remove(force=force))
        return {"volume": volume, "removed": True}


# ---- ISO images ----------------------------------------------------------------------------------------------------
def iso_dir(engine: Engine) -> Path:
    d = Path((engine.settings().get("iso") or {}).get("dir") or home() / "iso")
    d.mkdir(parents=True, exist_ok=True)
    return d


_downloads: dict[str, dict[str, Any]] = {}


def _add_iso_ops(cat: Catalog, engine: Engine) -> None:
    @cat.op("iso.list", group="iso")
    async def iso_list() -> list:
        """ISO and disk images in the ISO folder (and any extra folders set in iso settings)"""
        dirs = [iso_dir(engine)] + [Path(p) for p in (engine.settings().get("iso") or {}).get("extra_dirs") or []]
        out = []
        for d in dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.suffix.lower() in (".iso", ".img", ".qcow2", ".vmdk", ".vdi", ".vhdx", ".raw") and p.is_file():
                    st = p.stat()
                    out.append({"name": p.name, "path": str(p), "bytes": st.st_size, "modified": st.st_mtime})
        return out

    @cat.op("iso.download", group="iso", mutating=True, long_running=True)
    async def iso_download(url: str, filename: str = "", sha256: str = "", wait: bool = False) -> dict:
        """Download an ISO into the ISO folder (in the background unless wait=true), optionally checking its SHA-256"""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("https", "http"):
            raise OperationError("only http(s) URLs can be downloaded")
        name = filename or Path(urllib.parse.unquote(parsed.path)).name or "download.iso"
        if "/" in name or "\\" in name or name.startswith("."):
            raise OperationError("filename must be a plain file name")
        dest = iso_dir(engine) / name
        if dest.exists():
            raise OperationError(f"{dest} already exists")
        job = {"id": name, "url": url, "path": str(dest), "state": "running", "bytes": 0, "total": 0, "error": ""}
        _downloads[name] = job

        def fetch() -> None:
            import hashlib
            part = dest.with_suffix(dest.suffix + ".part")
            h = hashlib.sha256()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "VM-Harness"})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                    job["total"] = int(r.headers.get("Content-Length") or 0)
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
                        job["bytes"] += len(chunk)
                if sha256 and h.hexdigest().lower() != sha256.lower():
                    part.unlink(missing_ok=True)
                    raise OperationError(f"SHA-256 mismatch: got {h.hexdigest()}")
                part.replace(dest)
                job.update(state="done", sha256=h.hexdigest())
            except Exception as e:  # noqa: BLE001
                part.unlink(missing_ok=True)
                job.update(state="failed", error=str(e)[:500])

        task = asyncio.create_task(asyncio.to_thread(fetch))
        if wait:
            await task
        return dict(job)

    @cat.op("iso.downloads", group="iso")
    async def iso_downloads() -> list:
        """Downloads started by iso.download, with progress"""
        return [dict(j) for j in _downloads.values()]

    @cat.op("iso.import", group="iso", mutating=True, long_running=True)
    async def iso_import(path: str, move: bool = False) -> dict:
        """Copy (or move) an image file into the ISO folder"""
        src = Path(path)
        if not src.is_file():
            raise OperationError(f"{path} is not a file")
        dest = iso_dir(engine) / src.name
        if dest.exists():
            raise OperationError(f"{dest} already exists")
        await asyncio.to_thread(shutil.move if move else shutil.copy2, src, dest)
        return {"path": str(dest), "bytes": dest.stat().st_size}

    @cat.op("iso.delete", group="iso", destructive=True)
    async def iso_delete(name: str) -> dict:
        """Delete an image from the ISO folder"""
        p = (iso_dir(engine) / name).resolve()
        if p.parent != iso_dir(engine).resolve() or not p.is_file():
            raise OperationError(f"no image {name!r} in the ISO folder")
        p.unlink()
        return {"deleted": str(p)}


# ---- topology & cluster ---------------------------------------------------------------------------------------------
# The operations here answer "what is actually there, and can I reach it?" -- and
# they are written so that a fact nobody measured never comes back as if it were
# one. Every endpoint carries a provenance, and a node nobody has heard from is
# reported as `unknown`, never as `down`.

def cluster_config_path(engine: Engine) -> Path:
    """Where the cluster config lives.

    Sits beside the other VM-Harness state so a backup of the home directory
    carries the cluster topology with it. A missing file is an empty cluster, not
    an error: single-node installations have no reason to write one.
    """
    return home() / "cluster.json"


def _registry_from(engine: Engine) -> Any:
    """Build a registry from the persisted cluster config.

    Every op that touches the cluster goes through this so there is one place
    that knows where the config is and one interpretation of it.
    """
    from vm_harness.cluster import ClusterConfig, NodeRegistry

    return NodeRegistry(ClusterConfig.load(cluster_config_path(engine)))


def _endpoint_from_vm(name: str, backend: str, status: Any) -> Any:
    """Describe one VM as an endpoint, without guessing at what is unknown.

    Provenance per field, not per endpoint. A backend that answered is reporting
    the run state; the management URI it hands back is what the *host* believes the
    forward to be, which is an inference until something connects to it -- so the
    forward is modelled as ``INFERRED`` even though the URI came from a runtime.

    Addresses stay ``unknown`` unless the status actually carries one. A stopped VM
    with no address is the common case, and filling it in from the VM's name or the
    host's own IP would be a fabrication.
    """
    import enum as _enum
    import urllib.parse

    from vm_harness.cluster import Endpoint, EndpointKind, Provenance, vm_forwards

    raw_state = getattr(status, "state", None)
    state = raw_state.value if isinstance(raw_state, _enum.Enum) else str(raw_state or "")
    state = state or "unknown"

    # The management URI is a QMP endpoint (tcp://host:port), which is the one
    # forward a harness-launched VM is certain to have. SSH is not in VMStatus, so
    # it is not invented here -- an SSH forward the backend never mentioned would be
    # a config-shaped guess presented as a fact.
    forwards: list[Any] = []
    management_uri = str(getattr(status, "management_uri", "") or "")
    if management_uri:
        parsed = urllib.parse.urlparse(management_uri)
        if parsed.port and parsed.scheme in ("tcp", "http", "https"):
            forwards.extend(vm_forwards(name, qmp_port=parsed.port,
                                        host=parsed.hostname or "127.0.0.1"))

    address = str(getattr(status, "ip_address", "") or "")
    host_ip = str(getattr(status, "host_ip", "") or "")

    return Endpoint(
        id=name,
        kind=EndpointKind.VM,
        labels={"backend": backend},
        addresses=[a for a in (address, host_ip) if a],
        # Reported by the backend, but only when it is non-empty; otherwise the VM
        # simply has not told us and "unknown" is the honest answer.
        addresses_provenance=Provenance.REPORTED if address else Provenance.UNKNOWN,
        state=state,
        state_provenance=Provenance.REPORTED if state != "unknown" else Provenance.UNKNOWN,
        ports=forwards,
    )


def _add_topology_ops(cat: Catalog, engine: Engine) -> None:
    @cat.op("topology.list", group="topology")
    async def topology_list(backend: str = "", verify: bool = False,
                            timeout: float = 2.0) -> dict:
        """Every VM here as a network endpoint: its state, its addresses, its port forwards

        Addresses come back as `unknown` unless something reported them -- a VM
        being listed does not mean anyone knows its IP, and guessing one from a
        config would be worse than saying nothing.

        With `verify=true` each forward is actually connected to, so its
        reachability is observed rather than read from the config. That is a real
        socket connect per port and runs off the event loop, so the caller's
        timeout is per port, not total."""
        from vm_harness.cluster import (Endpoint, EndpointKind, Link, LinkKind,
                                        Provenance, Topology, container_api_forward,
                                        layer2_capability)
        from vm_harness.cluster.network import ReachabilityVerifier

        names = [backend] if backend else await engine.available_hypervisors()
        topology = Topology()

        async def one(bname: str) -> list[Any]:
            try:
                b = await engine.hypervisor(bname)
                vms = await b.list_vms()
            except Exception:  # noqa: BLE001 - one absent hypervisor must not hide the rest
                return []
            out = []
            for vm in vms:
                try:
                    status = await b.get_status(vm)
                except Exception:  # noqa: BLE001 - status failure is unknown, not absent
                    status = None
                out.append(_endpoint_from_vm(vm, bname, status))
            return out

        gathered = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
        for result in gathered:
            if isinstance(result, Exception):
                continue
            topology.endpoints.extend(result)

        # The container engine's API endpoint, modelled rather than discovered:
        # Docker Desktop speaks a named pipe, so this is frequently not a TCP
        # port at all and the note says so.
        try:
            container_ok = bool((await engine.container_engines()).get("docker"))
        except Exception:  # noqa: BLE001
            container_ok = False
        if container_ok:
            topology.endpoints.append(Endpoint(
                id="docker-api", kind=EndpointKind.CONTAINER,
                labels={"engine": "docker"},
                state="unknown", state_provenance=Provenance.UNKNOWN,
                ports=[container_api_forward()],
            ))

        # Only the local host's forwards are modelled as links to it. This host
        # cannot bridge a container to a VM, so no L2 link is ever asserted here.
        host = Endpoint(id="host", kind=EndpointKind.HOST, state="local",
                        state_provenance=Provenance.OBSERVED)
        topology.endpoints.append(host)
        for ep in topology.endpoints:
            for f in ep.ports:
                topology.links.append(Link(
                    source="host", target=ep.id, kind=LinkKind.PORT_FORWARD,
                    detail=f"{f.host}:{f.port} ({f.purpose})",
                    provenance=Provenance.INFERRED,
                ))

        if verify:
            # Verification rebuilds the endpoints rather than editing them: a
            # PortForward is frozen so that "what we modelled" stays recoverable
            # after "what we measured" arrives.
            budget = max(0.1, float(timeout))
            verifier = ReachabilityVerifier(timeout=budget)
            results = await verifier.check_many_async(
                [(f.host, f.port) for ep in topology.endpoints for f in ep.ports],
                timeout=budget,
            )
            by_key = {(r.host, r.port): r for r in results}
            measured: list[Any] = []
            for ep in topology.endpoints:
                if not ep.ports:
                    measured.append(ep)
                    continue
                rebuilt = []
                for f in ep.ports:
                    result = by_key.get((f.host, f.port))
                    if result is None:
                        rebuilt.append(f)
                        continue
                    rebuilt.append(replace(
                        f,
                        provenance=result.provenance,
                        note=(f"connected in {result.latency_ms:.0f} ms" if result.reachable
                              else f"{result.reachability.value}: {result.detail}"),
                    ))
                measured.append(replace(ep, ports=rebuilt))
            topology.endpoints = measured

        can_l2, why = await asyncio.to_thread(layer2_capability)
        if not can_l2:
            topology.limitations.append(why)
        return topology.to_dict()

    @cat.op("topology.reach", group="topology")
    async def topology_reach(host: str, port: int, timeout: float = 2.0) -> dict:
        """Try to connect to one host:port and report what happened

        The three answers are kept apart on purpose: `reachable` (a connect
        completed), `refused` (the host answered no -- nothing listening) and
        `timeout` (nothing answered at all). A refused QMP port means QEMU is
        running without that forward; a timeout means the guest's network is
        wrong, and they call for different fixes."""
        from vm_harness.cluster.network import tcp_probe

        budget = max(0.1, float(timeout))
        result = await asyncio.to_thread(tcp_probe, host, int(port), budget)
        return result.to_dict()

    @cat.op("topology.limits", group="topology")
    async def topology_limits() -> dict:
        """What this host cannot do at the network layer, and why

        On Windows with WHPX there is no way to put a Docker Desktop container and
        a VM on one layer-2 segment: the two live in different stacks and the Linux
        bridge helpers do not exist here. Ask before building a topology that needs
        it, rather than discovering it from a timeout."""
        from vm_harness.cluster import layer2_capability

        can_l2, why = await asyncio.to_thread(layer2_capability)
        return {"l2_bridge": can_l2, "reason": why,
                "workarounds": ["host port forwarding (QMP and SSH forwards)",
                                "put the VM and the container on the same external network"]
                if not can_l2 else []}

    @cat.op("cluster.status", group="cluster")
    async def cluster_status() -> dict:
        """Every configured node, its health, and its capacity

        `unknown` means we have not heard from a node. That is not the same as
        `down` and is never reported as such: silence is indistinguishable from a
        paused node, a severed network and a frozen process, and only one of those
        is something to act on. A node becomes `down` only when a connection to a
        node we had heard from actually failed."""
        registry = await asyncio.to_thread(_registry_from, engine)
        return registry.to_dict()

    @cat.op("cluster.place", group="cluster")
    async def cluster_place(capabilities: Optional[list[str]] = None, slots: int = 1,
                            name: str = "", node: str = "",
                            allow_stale: bool = True) -> dict:
        """Choose which node should run a piece of work, by capability and free capacity

        Only nodes that have sent a recent heartbeat are eligible, because a node's
        capacity is taken from what it reported rather than from what someone typed
        into a config file. Among eligible nodes the one with the most free capacity
        wins, with node id as a deterministic tie-break.

        If nothing fits this fails with a per-node reason and places nothing. It
        does not fall back to a different node: work that lands on a machine you did
        not choose is work you will not find when you go looking for it."""
        from vm_harness.cluster import PlacementError, Scheduler, WorkRequest

        registry = await asyncio.to_thread(_registry_from, engine)
        scheduler = Scheduler(registry)
        request = WorkRequest(
            name=name,
            capabilities=frozenset(capabilities or ()),
            slots=int(slots),
            pin_node=node,
            allow_stale=bool(allow_stale),
        )
        try:
            placement = scheduler.place(request)
        except PlacementError as exc:
            raise OperationError(exc.describe(), code="no_capacity", status=409)
        scheduler.commit(placement)
        return placement.to_dict()

    @cat.op("cluster.failover", group="cluster")
    async def cluster_failover() -> dict:
        """What survives a node disappearing: what is retried, and what is not

        Placement decisions are pure and always safe to redo. Starting or stopping
        a VM may be retried with the understanding that the first attempt might
        have succeeded before the node went away, so the operation could run twice.
        Anything destructive -- deleting a disk, writing files into a guest, running
        a guest command -- is never retried automatically, because a node vanishing
        is exactly the moment a blind retry does the most damage."""
        from vm_harness.cluster import DEFAULT_FAILOVER

        return DEFAULT_FAILOVER.to_dict()


# ---- audit ---------------------------------------------------------------------------------------------------------
def _add_audit_ops(cat: Catalog, audit: Any) -> None:
    @cat.op("audit.query", group="audit")
    async def audit_query(limit: int = 100, operation: str = "", since: float = 0.0) -> list:
        """The record of everything that changed something, newest first"""
        return audit.query(limit=limit, operation=operation, since=since)

    @cat.op("audit.verify", group="audit")
    async def audit_verify() -> dict:
        """Check the audit log's hash chain: any edited or removed entry breaks it"""
        ok, message = audit.verify()
        return {"ok": ok, "message": message}


def build_catalog(engine: Engine, audit: Any = None) -> Catalog:
    cat = Catalog()
    _add_host_ops(cat, engine)
    _add_vm_ops(cat, engine)
    _add_qemu_ops(cat, engine)
    _add_container_ops(cat, engine)
    _add_docker_extras(cat, engine)
    _add_compose_ops(cat, engine)
    _add_k8s_ops(cat, engine)
    _add_iso_ops(cat, engine)
    _add_topology_ops(cat, engine)
    if audit is not None:
        _add_audit_ops(cat, audit)
    return cat
