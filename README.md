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

## REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/status` | System status |
| GET | `/api/v1/vms` | List VMs |
| GET | `/api/v1/config` | Get configuration |
| GET | `/api/v1/isos` | List ISOs |

## Testing

```bash
pytest tests/ -v                # All 97 tests
pytest tests/test_gui_comprehensive.py -v  # GUI tests only
```

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
