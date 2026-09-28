# The hub, the API, MCP, and remote control of the window

VM-Harness has one local service, **the hub**, that owns the hypervisor and container backends. Everything else is a
client of it: the desktop window, the MCP server, the command line, and other programs such as ABP. So a VM started
from one of them is the same VM every other one sees, and every change lands in one audit log.

```
 window (PyQt5) ──attach──┐
 MCP server (stdio) ──────┤
 python -m vm_harness call┼──►  hub  127.0.0.1:8765  ──►  QEMU · VirtualBox · VMware · Hyper-V · WSL · KVM
 ABP / any HTTP client ───┘      (catalog, audit)         Docker · Podman · Kubernetes · Compose
```

## Running it

```bash
python -m vm_harness serve          # the hub (the window and the MCP server start it if it is not running)
python -m vm_harness gui            # the window; it attaches to the hub
python -m vm_harness mcp            # MCP over stdio
python -m vm_harness status         # hub address, and whether the window is attached
python -m vm_harness ops snapshot   # search the operations
python -m vm_harness call vm.list
python -m vm_harness call vm.start name=Kali
python -m vm_harness call vm.create backend=qemu --json '{"config": {"name": "lab", "ram_mb": 2048, "disk_size_gb": 20}}'
python -m vm_harness stop
```

State lives in `~/.vmharness` (or `$VMH_HOME`): `control.json` (the hub's address, token and pid), `token`,
`backends.json` (per-backend settings), `audit/audit.jsonl`, `iso/`.

## Finding and authenticating

The hub writes `control.json` when it starts and removes it when it stops. Clients read it, so nobody configures a port
or copies a token. The token is generated once. `VMH_URL` + `VMH_TOKEN` point a client at another hub (for example
one on another machine reached over an SSH tunnel). Requests carry `Authorization: Bearer <token>` (or `X-VMH-Token`).
The hub listens on 127.0.0.1 unless `--host` says otherwise.

## The catalog

Every capability is an **operation** with an id, a JSON Schema for its arguments, and flags: `mutating` (changes
something: audited) and `destructive` (deletes or overwrites something unrecoverable). Most are generated from the
backends' own methods, so a backend method never lacks an operation (a test checks this):

| Group | Operations |
|---|---|
| `vm.*` | list, status, config, create, destroy, start, stop, pause, resume, reset, reboot, shutdown_guest, metrics, display, screenshot, console, exec, guest_info, file read/write, snapshots, disks, CD-ROM, NICs, export/import/clone/migrate, limits, USB passthrough, on any hypervisor (`backend=` picks one; otherwise the VM is found by name) |
| `qemu.*` | raw QMP commands and events; qemu-img info, create (with backing files), convert, resize, check, internal snapshots |
| `container.*` `image.*` `network.*` `volume.*` | Docker or Podman (`engine=`): containers (inspect, top, pause, rename, copy files in and out...), images (build, tag, history...), networks, volumes |
| `docker.*` | info, disk usage, prune |
| `compose.*` | up, down, list, services, restart, scale, logs |
| `k8s.*` | contexts, namespaces, pods, deployments, services, ingresses, config maps, secrets, manifests, exec, logs, scale |
| `iso.*` | list, download (with SHA-256 check), import, delete |
| `host.*` | this machine, which backends work here (and why the others do not), backend settings |
| `audit.*` | the log, and a check that it has not been edited |
| `gui.*` | the window (below) |
| `service.*` | the hub itself |

## HTTP

```
GET  /v1/health                      no token: {ok, version, pid, gui}
GET  /v1/operations?q=&group=
POST /v1/call/<operation>   {args}   {ok, result, duration_s} | {ok: false, error, code}
GET  /v1/openapi.json
GET  /v1/events   (WebSocket)        every call and window attach/detach, live
POST /mcp                            MCP over Streamable HTTP (JSON responses)
```

Error codes: `unauthorized`, `not_found`, `bad_arguments`, `unavailable` (the backend is not installed or not
running), `unsupported`, `gui_not_attached`, `timeout`, `internal`.

## MCP

`python -m vm_harness mcp` offers every operation as a tool (`vm_start`, `gui_click`...) plus `vmh_operations`,
`vmh_describe` and `vmh_call`. Tools carry `readOnlyHint` / `destructiveHint`, and screenshots come back as images.
`--tools compact` offers only the three meta tools; `--groups vm,gui` limits the groups. For Claude Desktop:

```json
{"mcpServers": {"vm-harness": {"command": "Z:/Projects/VM-Harness/.venv/Scripts/python.exe",
                                "args": ["-m", "vm_harness", "mcp"]}}}
```

## Driving the window

When the window is open it attaches to the hub, and the `gui.*` operations act on it. They run on the window's own
thread, exactly as clicks would.

| Operation | What it does |
|---|---|
| `gui.launch` | open the window if it is not open, and wait until it is attached |
| `gui.state`, `gui.window` | visible / minimized / size / panel on screen; show, hide, raise, minimize, maximize, resize |
| `gui.panels`, `gui.open` | the 34 panels and their sidebar labels; switch to one (by name or label) |
| `gui.inspect`, `gui.find` | the widgets on a panel with ids, labels, text, values, items and table rows; search them |
| `gui.read` | everything one widget shows (whole tables and text areas) |
| `gui.click`, `gui.set`, `gui.select`, `gui.type`, `gui.key` | act on a widget |
| `gui.wait` | until a widget exists, is enabled, or shows some text |
| `gui.messages`, `gui.dialog` | open dialogs, and answering them |
| `gui.methods`, `gui.invoke` | a panel's public methods, and calling one |
| `gui.screenshot` | a PNG of the window, a panel or a widget |

A widget is named by `{"panel": ..., "id": ...}` (its objectName, or the id `gui.inspect` gave it) or by
`{"panel": ..., "text": "Create"}` (its text, or the label beside it).

## Checked on the development machine (2026-09-28, Windows 10, RX 7900 XTX)

- QEMU 11.0 (Scoop): create, start, a second process sees and stops it, pause/resume, metrics, PNG screenshot, destroy.
- VirtualBox 7.2: lists VMs including broken registrations (by UUID); Hyper-V: the Kali VM's state, checkpoints and
  adapters; WSL: three distributions, without starting a stopped one.
- Docker Desktop: containers, images, networks, volumes; a container pulled, created, exec'd, stopped and removed.
- Kubernetes: the configured cluster was down, and the backend now reports that in 2 s instead of hanging.
- The window attached to the hub and was driven through it: panels listed, the Create VM wizard filled in and advanced.
- MCP over stdio: initialize, 161 tools listed, calls and errors.
