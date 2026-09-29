# VM-Harness

Control virtual machines and containers from one place: a desktop window, a local API, an MCP server for AI agents,
and a command line. QEMU, VirtualBox, VMware, Hyper-V, WSL and KVM; Docker, Podman, Kubernetes and Compose.

## Quick start

```bash
pip install -e ".[dev]"
python -m vm_harness gui           # the window
python -m vm_harness mcp           # MCP server for Claude Desktop, ABP, Cursor... (stdio)
python -m vm_harness call vm.list  # any operation from the command line
```

Everything goes through one local service, the hub: see **[docs/control.md](docs/control.md)** for the API, the 160+
operations, MCP, and driving the window remotely.

## Features

- **34 GUI Panels** — Complete VM control from a polished dark-themed desktop interface
- **CLI Tool** — Command-line interface for scripting and agent access
- **REST API** — HTTP endpoints for programmatic control
- **MCP Server** — every operation as an MCP tool, including driving the window (`python -m vm_harness mcp`)
- **Self-Healing** — Atomic state, process guardian, automatic recovery
- **Multi-VM** — Control multiple QEMU instances from one interface
- **ISO Manager** — Internal storage + external folder scanning + downloads
- **Guest Integration** — SSH terminal, file browser, process manager

## Quick Start

```bash
pip install -e ".[dev]"

qemu-mcp gui              # Launch GUI
qemu-mcp status           # CLI status
qemu-mcp api start        # REST API
python -m vm_mcp          # MCP server
```

## GUI Panels (23 Total)

| # | Panel | Description |
|---|-------|-------------|
| 1 | **Dashboard** | Live VM status — state, PID, resources, activity log, quick actions |
| 2 | **VM Switcher** | Multi-VM management — add/remove/switch between VMs |
| 3 | **VM Control** | Lifecycle — start/stop/reset/suspend/resume/eject ISO |
| 4 | **Guest Terminal** | SSH command execution with history |
| 5 | **Guest Agent** | Guest processes, services, files via SSH |
| 6 | **Telemetry** | Real-time CPU/RAM/Disk/Net charts (matplotlib) |
| 7 | **QMP System Info** | Real QMP data — status, version, KVM, command reference |
| 8 | **QMP Console** | Direct QMP command input with JSON response viewer |
| 9 | **Snapshots** | Create/restore/delete VM snapshots via qemu-img |
| 10 | **ISO Manager** | Browse, import, download ISO files |
| 11 | **Create VM** | 5-step provisioning wizard |
| 12 | **Storage** | Disk create/resize/convert/delete |
| 13 | **CPU/Memory** | Hotplug, pinning, NUMA, ballooning |
| 14 | **Display** | VNC, SPICE, RDP, GPU configuration |
| 15 | **Advanced QEMU** | Machine type, boot order, SMBIOS settings |
| 16 | **USB/Devices** | USB passthrough, PCI passthrough, TPM 2.0 |
| 17 | **Network** | Virtual networks, port forwarding, firewall |
| 18 | **Automation** | Macros, schedules, event hooks |
| 19 | **Monitoring** | Real-time metrics, alerts, log explorer |
| 20 | **Settings** | Full .env editor with validation |
| 21 | **Security** | Credential vault, user management, audit log |
| 22 | **Troubleshooting** | Diagnostics, GDB debugger, debug logs |
| 23 | **Logs** | Filterable, searchable, exportable log viewer |

## Self-Healing System

- **Atomic State** — Write-ahead logging, snapshots every 30 seconds, crash recovery
- **Process Guardian** — Monitors GUI process, auto-restarts on crash with backup failover
- **Hot Reloader** — Watches source files for development-time code reloading
- **Health Checks** — Verifies window visibility every 5 seconds

## CLI Reference

```bash
qemu-mcp status                  # System status
qemu-mcp vm list                 # List VMs
qemu-mcp vm start <name>         # Start VM
qemu-mcp vm stop <name>          # Stop VM
qemu-mcp vm restart <name>       # Restart VM
qemu-mcp snapshot list           # List snapshots
qemu-mcp snapshot create <name>  # Create snapshot
qemu-mcp iso list                # List ISOs
qemu-mcp config get              # Get configuration
qemu-mcp api start --port 8080   # Start REST API
qemu-mcp gui                     # Launch GUI
```

## Phone API (the Android app)

The window serves the Android companion app on port 8443. Phones pair once, then call it over Tailscale with their key.
Every call goes through the hub, so a phone sees and controls every VM on every hypervisor by name.

| Method | Endpoint | What it does |
|--------|----------|--------------|
| POST | `/api/v1/auth/pair` | Pair a phone (with the one-time code shown in the window) |
| GET | `/api/v1/vms` | Every VM: name, hypervisor, state, RAM, vCPUs, uptime, CPU and memory in use |
| GET | `/api/v1/vms/{name}` | One VM's state, configuration and network interfaces |
| POST | `/api/v1/vms/{name}/{action}` | `start`, `stop` (force), `powerdown`, `shutdown`, `reset`, `reboot`, `pause`, `resume`, `eject` |
| GET, POST | `/api/v1/vms/{name}/snapshots` | List snapshots, or create one: `{name, description?}` |
| POST | `/api/v1/vms/{name}/snapshots/{snapshot}/restore` | Roll the VM back to a snapshot |
| POST | `/api/v1/vms/{name}/ssh/command` | Run a command in the guest over SSH |
| GET | `/api/v1/metrics` | This host's CPU, memory, disk and network, measured now |
| GET | `/api/v1/security/audit` | The tamper-evident audit log: what changed, and who changed it |
| GET | `/api/v1/logs?lines=&level=` | The newest lines of the hub's log |

Everything VM-Harness can do (about 160 operations) is also available on the hub. It serves local HTTP (`python -m
vm_harness serve`) and MCP (`python -m vm_harness mcp`). See [docs/control.md](docs/control.md).

## Testing and the local CI/CD pipeline

```bash
pytest tests -n auto               # the whole suite in parallel (about 750 tests, under 2 minutes)
python ci/pipeline.py              # everything this change needs (see below)
python ci/install_hooks.py         # once: every `git push` runs the pipeline first
```

`ci/pipeline.py` runs on this machine, with no cloud runner. Each stage runs only when the change needs it. When the
change set can't be determined, everything runs.

| Stage | What it checks |
|-------|----------------|
| `preflight` | The Python version, the venv and its dependencies (`pip check`), free disk space, the git state, and whether QEMU and a JDK are present |
| `static` | Every file compiles. ruff finds no syntax errors, undefined names or shared mutable defaults. There is no bare `except:`. The push adds no secret-shaped strings and no file over 5 MB. |
| `tests` | pytest, in parallel with a per-test time limit. A failing test is run once more on its own: a flake is reported as a flake, and a real failure blocks. A JUnit report is kept. |
| `smoke` | A real hub in a throwaway home must answer health, refuse callers without the token, list 100+ operations, run a read-only call, speak MCP over HTTP and stdio, and shut down cleanly. |
| `android` | `gradlew testDebugUnitTest assembleDebug`, when `android/` changed |
| `build` | The Windows executable, with PyInstaller (`--build`) |
| `deploy` | A hub running from this checkout is restarted onto the new code |

Options: `--full`, `--fast`, `--only STAGE`, `--build`, `--no-deploy`, `--list`.

How it stays reliable:
- Only one run happens at a time. A lock left by a crashed run is taken over.
- Every command has a timeout, and on timeout its whole process tree is killed.
- Each run writes a log to `ci/logs/`, plus `ci/reports/latest.json` and a timing history in `ci/reports/history.jsonl`.

To push without the pipeline once, use `git push --no-verify` or set `VMH_SKIP_PIPELINE=1`.

## Architecture

```
GUI (PyQt5) → QMP/SSH Bridges → QEMU/Guest
     ↓              ↓
   CLI (argparse)  REST API (HTTP)
     ↓              ↓
   MCP Server ← Tools (lifecycle, guest)
```

## Security

- Three-layer secret isolation
- Fernet encryption for credential store
- No secrets in logs
- SSH host key verification
