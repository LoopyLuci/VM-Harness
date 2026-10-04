"""Launch the VM-Harness window. `python -m gui` fails from some shells because
the package needs the repo root and src/ on sys.path before its first import;
this puts both there explicitly so it runs the same way every time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

os.environ.setdefault("PYTHONPATH", str(REPO / "src"))
# The window needs a real display; do not inherit an offscreen override.
os.environ.pop("QT_QPA_PLATFORM", None)

from gui.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())