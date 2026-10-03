#!/usr/bin/env python3
"""Install (or remove) the pre-push hook that runs the local CI/CD pipeline on every `git push`.

    python ci/install_hooks.py            install
    python ci/install_hooks.py --remove   remove

The hook runs `ci/pipeline.py` with the refs being pushed, so only what the push changes is checked. To push without
it once (an emergency), use `git push --no-verify`, or set VMH_SKIP_PIPELINE=1.
"""
from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "# vm-harness local pipeline"
HOOK = f"""#!/bin/sh
{MARK}
if [ "$VMH_SKIP_PIPELINE" = "1" ]; then
  echo "VMH_SKIP_PIPELINE=1: skipping the local pipeline"
  exit 0
fi
ROOT="$(git rev-parse --show-toplevel)"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python"
VMH_PIPELINE_HOOK=1 exec "$PY" "$ROOT/ci/pipeline.py"
"""


def hooks_dir() -> Path:
    out = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=ROOT, capture_output=True, text=True, check=True)
    return (ROOT / out.stdout.strip()).resolve()


def main() -> int:
    path = hooks_dir() / "pre-push"
    if "--remove" in sys.argv:
        if path.is_file() and MARK in path.read_text(encoding="utf-8", errors="replace"):
            path.unlink()
            print(f"removed {path}")
        else:
            print("no pipeline hook installed")
        return 0
    if path.is_file() and MARK not in path.read_text(encoding="utf-8", errors="replace"):
        backup = path.with_suffix(".before-pipeline")
        path.replace(backup)
        print(f"kept the existing pre-push hook as {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {path}: every `git push` now runs ci/pipeline.py first")
    return 0


if __name__ == "__main__":
    sys.exit(main())
