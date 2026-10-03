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
- **Command line** — every operation from the shell, over the same hub as the window and the MCP server
- **REST API** — HTTP endpoints for programmatic control
- **MCP Server** — every operation as an MCP tool, including driving the window (`python -m vm_harness mcp`)
- **Self-Healing** — Atomic state, process guardian, automatic recovery
- **Multi-VM** — Control multiple QEMU instances from one interface
- **ISO Manager** — Internal storage + external folder scanning + downloads
- **Guest Integration** — SSH terminal, file browser, process manager

## GUI Panels (the core set)

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

The console script `vm-harness` and `python -m vm_harness` are the same program.

```bash
vm-harness status                  # is the hub running, is the window attached
vm-harness serve                   # run the hub (the local API every client uses)
vm-harness stop                    # stop the hub
vm-harness gui                     # open the window (it attaches to the hub)
vm-harness mcp                     # MCP server on stdio
vm-harness ops [query]             # list operations, or search them
vm-harness ops --group vm          # just one group
vm-harness call vm.list            # run one operation
vm-harness call vm.start name=Kali # arguments as key=value, or --json '{...}'
```

The `vm.*`, `iso.*`, `container.*` and `host.*` names are operation IDs, not subcommands: `vm.list`,
`vm.start`, `vm.stop`, `vm.snapshot.create`, `iso.list`, `container.list`, `host.capabilities`, and about 160 more.
`vm-harness ops` lists them all with a summary; `docs/control.md` documents them.

## Talking to a guest

The harness could start, stop and inspect a VM but had no way to *talk* to one, so every question a
guest could only answer by drawing to its console — a login prompt, a confirmation, an installer
dialog — was unaskable once its serial log went quiet.

```bash
vm-harness call guest.type        qmp_uri=tcp:127.0.0.1:4444 text=hello submit=true
vm-harness call guest.press       qmp_uri=tcp:127.0.0.1:4444 key=backspace times=40
vm-harness call guest.screendump  qmp_uri=tcp:127.0.0.1:4444 path=vm/console.ppm
```

Keystrokes go through QMP's `human-monitor-command` passthrough to the guest's PS/2 keyboard, so no
extra device is needed on the VM's command line. They are sent one at a time with a delay, because
guests drop input that arrives faster than they poll the keyboard.

Every unencodable character raises rather than being skipped. A password typed with one character
missing produces no error — just a login that fails for an invisible reason — so a loud failure at
type time is much cheaper. The keymap covers all 95 printable ASCII characters on a US layout and is
the place to extend for other layouts.

**Console credentials.** *VM Control → Connect to QMP → Console Login…* stores the username and
password a VM's login prompt expects. They go into the same encrypted `CredentialStore` as every
other secret (Fernet, `0600`), and the dialog will not echo a stored password back.

Signing in unattended:

```python
from vm_harness.autologin import ConsoleLogin, login
from vm_harness.qmp_client import QMPClient
from gui.dialogs_vm_login import load_vm_login

creds = load_vm_login()          # (username, password), or None
async with QMPClient("tcp:127.0.0.1:4444") as client:
    print(await login(client, ConsoleLogin(*creds)))
```

`login()` reports a before/after screendump and whether the screen changed, rather than assuming the
keystrokes landed. An unchanged screen means the login prompt simply redrew itself, i.e. it failed.

Two deliberate constraints. The store derives its key from `GUI_MASTER_PASSWORD` when set, otherwise
from a random `.master_key` file beside the credentials — the second mode is what allows unattended
use at all, but it means the key is on the same disk as the data, so it protects against the
credentials file leaking alone rather than against someone who already has the account. And
`login()` takes the credentials as arguments rather than reading the store itself, because ops are
dispatched, logged and exposed over MCP and HTTP, and a password should not be riding along in an
argument dict.

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
pytest                              # the default run; see below for what it excludes
pytest -n auto                      # the same set in parallel (needs pytest-xdist)
python ci/pipeline.py               # everything this change needs (see below)
python ci/install_hooks.py          # once: every `git push` runs the pipeline first
```

`pytest` needs no environment variables: `pyproject.toml` sets `pythonpath = ["src"]`, so a plain `pytest` in a
checkout picks up `vm_harness` without `PYTHONPATH` and without installing.

Two files are excluded from the default run, by `--ignore` in `addopts`:

| Excluded | Why | Needs |
|----------|-----|-------|
| `tests/test_gui_comprehensive.py` | drives the container and Kubernetes panels | a live Docker daemon and a reachable cluster |
| `tests/test_gui_performance.py` | boots real guests and times panel switches | a real hypervisor with usable VMs |

On a host without those services they do not fail — they block forever. Nothing else is excluded. On a host that has
them, run them by clearing the default `addopts`:

```bash
pytest tests/test_gui_comprehensive.py tests/test_gui_performance.py --override-ini addopts=
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
