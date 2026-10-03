"""VM-Harness from the command line.

    python -m vm_harness serve                     run the hub (the local API every client uses)
    python -m vm_harness mcp [--tools compact]     MCP server on stdio (starts the hub if needed)
    python -m vm_harness gui                       open the window (it attaches to the hub)
    python -m vm_harness ops [query] [--group g]   list operations
    python -m vm_harness call vm.list               run one: arguments as key=value or --json '{...}'
    python -m vm_harness call vm.start name=Kali
    python -m vm_harness status                    is the hub running, is the window attached
    python -m vm_harness stop                      stop the hub
    python -m vm_harness legacy-mcp                the old single-VM QMP MCP server
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Running from a checkout without installing: make `vm_harness` and `gui` importable.
_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _call(argv: list[str]) -> int:
    from vm_harness.control.client import HubClient, HubError

    if not argv:
        print("usage: vm-harness call <operation> [key=value ...] [--json '{...}']", file=sys.stderr)
        return 2
    op, rest = argv[0], argv[1:]
    args: dict = {}
    i = 0
    while i < len(rest):
        if rest[i] == "--json" and i + 1 < len(rest):
            args.update(json.loads(rest[i + 1]))
            i += 2
            continue
        k, sep, v = rest[i].partition("=")
        if not sep:
            print(f"arguments are key=value; got {rest[i]!r}", file=sys.stderr)
            return 2
        args[k] = _value(v)
        i += 1
    try:
        result = HubClient.connect(start=True, client_name="cli").call(op, args)
    except HubError as e:
        print(f"error ({e.code}): {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


def _ops(argv: list[str]) -> int:
    from vm_harness.control.client import HubClient
    group = ""
    if "--group" in argv:
        i = argv.index("--group")
        group = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    for o in HubClient.connect(start=True, client_name="cli").operations(" ".join(argv), group):
        mark = "!" if o["destructive"] else ("*" if o["mutating"] else " ")
        print(f"{mark} {o['id']:<28} {o['summary']}")
    print("\n* changes something   ! deletes or overwrites")
    return 0


def _status() -> int:
    from vm_harness.control.hub import hub_alive
    d = hub_alive()
    if not d:
        print("hub: not running")
        return 1
    print(f"hub: {d['url']} (pid {d['pid']}, version {d['version']}), window attached: {d.get('gui', False)}")
    return 0


def _stop() -> int:
    from vm_harness.control.client import HubClient, HubError
    try:
        HubClient.connect(client_name="cli")._request("POST", "/v1/service/stop", {})
        print("hub stopping")
        return 0
    except HubError as e:
        print(str(e))
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "help"
    rest = argv[1:]
    if cmd == "serve":
        from vm_harness.control.hub import main as serve
        return serve(rest)
    if cmd == "mcp":
        from vm_harness.control.mcp_server import main as mcp
        return mcp(rest)
    if cmd == "gui":
        os.chdir(_ROOT)
        from gui.__main__ import main as gui_main
        sys.argv = [sys.argv[0], *rest]
        return gui_main() or 0
    if cmd == "call":
        return _call(rest)
    if cmd == "ops":
        return _ops(rest)
    if cmd == "status":
        return _status()
    if cmd == "stop":
        return _stop()
    if cmd == "legacy-mcp":
        import asyncio
        from vm_harness.server import main as legacy
        asyncio.run(legacy())
        return 0
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())
