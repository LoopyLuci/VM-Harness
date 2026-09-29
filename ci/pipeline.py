#!/usr/bin/env python3
"""VM-Harness's local, on-device CI/CD pipeline. Everything runs on this machine; no cloud runner.

    python ci/pipeline.py                 # everything this change needs, then deploy
    python ci/pipeline.py --full          # every stage regardless of what changed (+ Android if available)
    python ci/pipeline.py --fast          # static checks and tests only
    python ci/pipeline.py --only tests    # one or more stages (repeat --only)
    python ci/pipeline.py --list          # the stages and whether this change needs them
    python ci/pipeline.py --no-deploy     # verify, but leave the running hub alone
    python ci/install_hooks.py            # run it on every `git push` (pre-push hook)

Stages, in order (a failed stage stops the run; skipped stages say why):

    preflight   Python version, venv and dependencies (pip check), git state, free disk, a stale run's lock,
                optional tools (QEMU, JDK) reported
    static      every file compiles; ruff (if installed); no bare `except:`; no secret-shaped strings or big
                files in what this push adds
    tests       pytest (parallel with pytest-xdist when installed); a failure is re-run once on its own, so
                a flake is reported as a flake and a real failure still blocks; a JUnit XML report is kept
    smoke       the real thing, isolated: a hub started in a throwaway VMH_HOME on a free port must answer
                /v1/health, list 100+ operations, run a read-only call, speak MCP (initialize, tools/list) over
                HTTP, and shut down cleanly; the stdio MCP server must answer initialize
    android     ./gradlew testDebugUnitTest assembleDebug (when android/ changed and a JDK is present)
    build       the Windows executable with PyInstaller (--build or --full)
    deploy      a hub running from this checkout is restarted so it serves the new code (the window, if open,
                re-attaches by itself)

Robustness:
- One run at a time: a lock file names the running pid, and a lock left by a dead run is taken over.
- Every command has a timeout, and on timeout its whole process tree is killed, never just the parent.
- Change-aware: a push touching only docs skips the tests; unknown scope always means "run everything".
- Each run writes ci/logs/<time>.log, a JSON report (ci/reports/<time>.json and latest.json), and one line
  in ci/reports/history.jsonl with each stage's duration.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
LOGS = ROOT / "ci" / "logs"
REPORTS = ROOT / "ci" / "reports"
LOCK = ROOT / "ci" / ".pipeline.lock"
MIN_PYTHON = (3, 10)
MIN_FREE_GB = 2.0
FLAKY_RERUN_LIMIT = 10
STAGES = ["preflight", "static", "tests", "smoke", "android", "build", "deploy"]

# What each stage cares about (paths relative to the repo, forward slashes).
CODE = ("src/", "gui/", "tests/", "pyproject.toml", "requirements", "conftest.py", "setup", "ci/")
ANDROID = ("android/",)
BUILD = ("src/", "gui/", "scripts/build_pyinstaller.py", "pyproject.toml", "requirements")
DEPLOY = ("src/", "gui/", "pyproject.toml", "requirements")

SECRET_PATTERNS = {
    "api key": r"\bsk-(?:ant-|or-v1-)?[A-Za-z0-9_-]{24,}",
    "github token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    "aws key": r"\bAKIA[0-9A-Z]{16}\b",
    "slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "google key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
    "hf token": r"\bhf_[A-Za-z0-9]{30,}\b",
}
BIG_FILE = 5_000_000


# ---- output ------------------------------------------------------------------------------------------------------
class Out:
    def __init__(self) -> None:
        self.buf = io.StringIO()

    def line(self, text: str = "") -> None:
        print(text, flush=True)
        self.buf.write(text + "\n")

    def head(self, t: str) -> None:
        self.line(f"\n=== {t} ===")

    def ok(self, t: str) -> None:
        self.line(f"  [ok]   {t}")

    def skip(self, t: str) -> None:
        self.line(f"  [--]   {t}")

    def doing(self, t: str) -> None:
        self.line(f"  ->     {t}")

    def warn(self, t: str) -> None:
        self.line(f"  [!]    {t}")

    def err(self, t: str) -> None:
        self.line(f"  [ERR]  {t}")


OUT = Out()


@dataclass
class StageResult:
    name: str
    status: str = "pending"  # passed | failed | skipped
    seconds: float = 0.0
    note: str = ""
    details: dict = field(default_factory=dict)


# ---- running commands ----------------------------------------------------------------------------------------------
def kill_tree(pid: int) -> None:
    try:
        import psutil
        root = psutil.Process(pid)
        procs = root.children(recursive=True) + [root]
        for p in procs:
            with contextlib.suppress(psutil.Error):
                p.kill()
        psutil.wait_procs(procs, timeout=5)
    except Exception:  # noqa: BLE001 - psutil missing or the process is gone
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)


def run(cmd: list[str], *, cwd: Path = ROOT, timeout: float = 1800, env: Optional[dict] = None,
        retries: int = 0) -> tuple[bool, str]:
    """Run a command: (ok, combined output). On timeout the whole tree is killed. `retries` re-runs a failure after
    a short pause (a file briefly locked by an antivirus scan is not a real failure)."""
    for attempt in range(retries + 1):
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                                    start_new_session=not IS_WINDOWS)
        except FileNotFoundError:
            return False, f"{cmd[0]}: not found"
        try:
            out = proc.communicate(timeout=timeout)[0] or ""
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            with contextlib.suppress(Exception):
                proc.communicate(timeout=5)
            return False, f"{' '.join(map(str, cmd))}: timed out after {timeout:.0f}s (process tree killed)"
        if proc.returncode == 0:
            return True, out
        if attempt < retries:
            time.sleep(3)
    return False, out


def venv_python() -> str:
    py = ROOT / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
    return str(py) if py.is_file() else sys.executable


def tail(text: str, n: int = 3500) -> str:
    return text if len(text) <= n else "...\n" + text[-n:]


# ---- the lock ------------------------------------------------------------------------------------------------------
class Lock:
    """One pipeline at a time. A lock whose pid is gone is stale and taken over."""

    def __enter__(self) -> "Lock":
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        if LOCK.exists():
            try:
                held = json.loads(LOCK.read_text(encoding="utf-8"))
                pid = int(held.get("pid", 0))
            except (ValueError, OSError):
                pid = 0
            if pid and pid_alive(pid):
                raise SystemExit(f"another pipeline run (pid {pid}, started {held.get('started')}) is still going; "
                                 f"wait for it, or delete {LOCK} if that process is not really a pipeline")
            OUT.warn(f"took over a stale lock left by pid {pid}")
        LOCK.write_text(json.dumps({"pid": os.getpid(), "started": dt.datetime.now().isoformat(timespec="seconds")}),
                        encoding="utf-8")
        return self

    def __exit__(self, *exc) -> None:
        with contextlib.suppress(OSError):
            LOCK.unlink()


def pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        if IS_WINDOWS:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
            return str(pid) in r.stdout
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ---- change detection ------------------------------------------------------------------------------------------------
def changed_files() -> tuple[Optional[set[str]], str]:
    """(files this push changes, the range), or (None, why) when it cannot be told: then everything runs."""
    rng = None
    if os.environ.get("VMH_PIPELINE_HOOK") == "1":
        with contextlib.suppress(Exception):
            for line in sys.stdin.read().splitlines():
                parts = line.split()
                if len(parts) == 4 and set(parts[3]) != {"0"}:
                    rng = f"{parts[3]}..{parts[1]}"
    if rng is None:
        ok, up = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], timeout=30)
        if not ok:
            return None, "no upstream branch"
        ok, base = run(["git", "merge-base", "HEAD", up.strip()], timeout=30)
        if not ok:
            return None, "no merge base with the upstream"
        rng = f"{base.strip()}..HEAD"
    ok, out = run(["git", "diff", "--name-only", rng], timeout=60)
    if not ok:
        return None, f"git diff {rng} failed (history rewritten?)"
    files = {f.strip().replace("\\", "/") for f in out.splitlines() if f.strip()}
    # Uncommitted work is part of what is being verified in a manual run.
    ok, st = run(["git", "status", "--porcelain"], timeout=30)
    if ok:
        files |= {ln[3:].strip().strip('"').replace("\\", "/") for ln in st.splitlines() if len(ln) > 3}
    return files, rng


def touches(changed: Optional[set[str]], prefixes: tuple[str, ...]) -> bool:
    return changed is None or any(f.startswith(prefixes) for f in changed)


# ---- stages --------------------------------------------------------------------------------------------------------
def stage_preflight(ctx: dict) -> tuple[bool, str]:
    py = venv_python()
    ok, ver = run([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"], timeout=60)
    if not ok:
        return False, f"python does not run: {tail(ver, 400)}"
    if tuple(int(x) for x in ver.strip().split(".")[:2]) < MIN_PYTHON:
        return False, f"Python {ver.strip()} is older than {'.'.join(map(str, MIN_PYTHON))}"
    OUT.ok(f"python {ver.strip()} ({'venv' if '.venv' in py else 'system'}: {py})")
    ok, out = run([py, "-c", "import vm_harness, pytest, psutil, aiohttp; print(vm_harness.__file__)"], timeout=120)
    if not ok:
        return False, "the project or its test tools do not import (pip install -e .[dev]):\n" + tail(out, 1500)
    ok, out = run([py, "-m", "pip", "check"], timeout=180)
    if ok:
        OUT.ok("dependencies consistent (pip check)")
    else:
        OUT.warn("pip check reports conflicts:\n" + tail(out, 800))
    free = shutil.disk_usage(ROOT).free / 1024 ** 3
    if free < MIN_FREE_GB:
        return False, f"only {free:.1f} GB free on the project's drive (need {MIN_FREE_GB})"
    OUT.ok(f"{free:.1f} GB free")
    ok, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=30)
    ok2, head = run(["git", "rev-parse", "--short", "HEAD"], timeout=30)
    ctx["commit"] = head.strip() if ok2 else ""
    OUT.ok(f"git: {branch.strip()} @ {ctx['commit']}")
    qemu = shutil.which("qemu-system-x86_64") or (Path("C:/Program Files/qemu/qemu-system-x86_64.exe").is_file() and "C:/Program Files/qemu")
    OUT.ok(f"QEMU: {qemu}") if qemu else OUT.warn("QEMU not found: live QEMU tests will skip")
    ctx["java"] = bool(shutil.which("java") or os.environ.get("JAVA_HOME"))
    return True, ""


def stage_static(ctx: dict) -> tuple[bool, str]:
    py = venv_python()
    ok, out = run([py, "-m", "compileall", "-q", "src", "gui", "tests", "ci"], timeout=600)
    if not ok:
        return False, "does not compile:\n" + tail(out)
    OUT.ok("every file compiles")
    ruff = [py, "-m", "ruff"] if run([py, "-m", "ruff", "--version"], timeout=60)[0] else (
        [shutil.which("ruff")] if shutil.which("ruff") else None)
    if ruff:
        # The gate: things that are bugs (syntax errors, undefined names that crash when reached, shared mutable
        # defaults). Hygiene (unused imports, redefinitions) is reported, not blocking.
        ok, out = run([*ruff, "check", "--no-cache", "--output-format", "concise", "--select", "E9,F63,F7,F82,B006,B008",
                       "src", "gui", "ci", "tests"], timeout=600)
        if not ok:
            return False, "ruff (bugs):\n" + tail(out)
        OUT.ok("ruff: no syntax errors, undefined names or shared mutable defaults")
        _, hyg = run([*ruff, "check", "--no-cache", "--output-format", "concise", "--select", "F401,F811,F841", "src", "gui"], timeout=600)
        n = len([ln for ln in hyg.splitlines() if re.match(r".+:\d+:\d+: F", ln)])
        if n:
            OUT.warn(f"{n} unused import/variable finding(s) (ruff --select F401,F811,F841 --fix src gui to clean up)")
    else:
        OUT.warn("ruff is not installed in the venv: pip install ruff (lint skipped)")
    bare = []
    for base in ("src", "gui"):
        for p in (ROOT / base).rglob("*.py"):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if re.match(r"\s*except\s*:\s*(#.*)?$", line):
                    bare.append(f"{p.relative_to(ROOT)}:{n}")
    if bare:
        return False, "bare `except:` (catches KeyboardInterrupt/SystemExit): " + ", ".join(bare[:15])
    OUT.ok("no bare except")
    rng = ctx.get("range")
    if rng:
        ok, diff = run(["git", "log", "-p", "--no-color", "--format=", rng], timeout=300)
        hits, current = set(), "?"
        for line in diff.splitlines() if ok else []:
            if line.startswith("+++ b/"):
                current = line[6:]
            elif line.startswith("+") and not line.startswith("+++"):
                for kind, pat in SECRET_PATTERNS.items():
                    if re.search(pat, line):
                        hits.add(f"{current} ({kind})")
        if hits:
            return False, "secret-shaped strings in this push (never push them): " + ", ".join(sorted(hits))
        ok, objs = run(["git", "rev-list", "--objects", rng], timeout=300)
        big = []
        if ok and objs.strip():
            check = subprocess.run(["git", "cat-file", "--batch-check=%(objecttype) %(objectsize) %(rest)"], cwd=ROOT,
                                   input=objs, capture_output=True, text=True)
            for row in check.stdout.splitlines():
                t, size, *rest = row.split(" ", 2)
                if t == "blob" and int(size) > BIG_FILE:
                    big.append(f"{rest[0] if rest else '?'} ({int(size) / 1e6:.1f} MB)")
        if big:
            return False, "files over 5 MB in this push (build output?): " + ", ".join(big[:10])
        OUT.ok("no secrets or big files in this push")
    return True, ""


def stage_tests(ctx: dict) -> tuple[bool, str]:
    py = venv_python()
    REPORTS.mkdir(parents=True, exist_ok=True)
    junit = REPORTS / "junit.xml"
    extra: list[str] = []
    ok, _ = run([py, "-c", "import xdist"], timeout=60)
    if ok:
        extra += ["-n", "auto", "--dist", "loadgroup"]
    ok, _ = run([py, "-c", "import pytest_timeout"], timeout=60)
    if ok:
        extra += ["--timeout", "600"]
    env = {**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"), "PYTHONUTF8": "1"}
    OUT.doing("pytest " + " ".join(extra or ["(serial)"]))
    ok, out = run([py, "-m", "pytest", "-q", "-rfE", f"--junitxml={junit}", *extra], timeout=3600, env=env)
    summary = next((ln for ln in reversed(out.strip().splitlines()) if " passed" in ln or " failed" in ln), "")
    ctx["tests"] = summary.strip("= ")
    if ok:
        OUT.ok(ctx["tests"] or "tests passed")
        return True, ""
    failed = [re.sub(r"@[\w.-]+$", "", t) for t in re.findall(r"(?m)^(?:FAILED|ERROR) (\S+::\S+)", out)]
    if not failed or len(failed) > FLAKY_RERUN_LIMIT:
        return False, tail(out)
    OUT.warn(f"{len(failed)} test(s) failed; running just those again to tell a flake from a real failure")
    ok2, out2 = run([py, "-m", "pytest", "-q", "-rfE", "-p", "no:randomly", *failed], timeout=1800, env=env)
    if not ok2:
        return False, "failed again on its own (a real failure):\n" + tail(out2)
    OUT.warn("FLAKY (failed in the full run, passed alone): " + ", ".join(failed))
    ctx["flaky"] = failed
    return True, ""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, token: str = "", body: Optional[dict] = None, timeout: float = 30) -> tuple[int, object]:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        return e.code, None


def stage_smoke(ctx: dict) -> tuple[bool, str]:
    """A real hub in a throwaway home: health, catalog, a call, MCP over HTTP and stdio, clean shutdown."""
    py = venv_python()
    home = Path(tempfile.mkdtemp(prefix="vmh-smoke-"))
    port = free_port()
    env = {**os.environ, "VMH_HOME": str(home), "PYTHONUTF8": "1", "QT_QPA_PLATFORM": "offscreen"}
    log = open(home / "hub.log", "w", encoding="utf-8")  # noqa: SIM115 - closed in finally
    proc = subprocess.Popen([py, "-m", "vm_harness", "serve", "--port", str(port)], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
                            start_new_session=not IS_WINDOWS)
    try:
        control = home / "control.json"
        deadline = time.time() + 90
        while time.time() < deadline and not control.is_file():
            if proc.poll() is not None:
                return False, "the hub exited at start:\n" + tail((home / "hub.log").read_text(encoding="utf-8", errors="replace"), 2000)
            time.sleep(0.5)
        if not control.is_file():
            return False, "the hub did not write control.json in 90 s"
        d = json.loads(control.read_text(encoding="utf-8"))
        url, token = d["url"].rstrip("/"), d["token"]
        status, health = http("GET", url + "/v1/health")
        if status != 200:
            return False, f"/v1/health answered {status}"
        OUT.ok(f"hub up on {url} (pid {d.get('pid')})")
        status, _ = http("GET", url + "/v1/operations")
        if status not in (401, 403):
            return False, f"/v1/operations without a token answered {status}; it must refuse"
        status, body = http("GET", url + "/v1/operations", token)
        ops = body.get("operations") if isinstance(body, dict) else body
        if status != 200 or not isinstance(ops, list) or len(ops) < 100:
            return False, f"the catalog is wrong: status {status}, {len(ops) if isinstance(ops, list) else 0} operations"
        OUT.ok(f"catalog: {len(ops)} operations; refuses callers without the token")
        status, res = http("POST", url + "/v1/call/host.info", token, {}, timeout=120)
        if status != 200:
            status, res = http("POST", url + "/v1/call/vm.list", token, {}, timeout=120)
        if status != 200:
            return False, f"a read-only call failed with {status}"
        OUT.ok("a read-only call works")
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ci", "version": "1"}}}
        status, r = http("POST", url + "/mcp", token, init)
        if status != 200 or not isinstance(r, dict) or "result" not in r:
            return False, f"MCP initialize over HTTP failed ({status})"
        status, r = http("POST", url + "/mcp", token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = (r or {}).get("result", {}).get("tools", []) if isinstance(r, dict) else []
        if status != 200 or len(tools) < 10:
            return False, f"MCP tools/list returned {len(tools)} tools"
        OUT.ok(f"MCP over HTTP: {len(tools)} tools")
        # stdio MCP server (what editors and agents launch)
        mcp = subprocess.run([py, "-m", "vm_harness", "mcp", "--tools", "compact"], cwd=str(ROOT), env=env,
                             input=json.dumps(init) + "\n", capture_output=True, text=True, timeout=120)
        first = next((ln for ln in mcp.stdout.splitlines() if ln.strip().startswith("{")), "")
        if '"result"' not in first:
            return False, "the stdio MCP server did not answer initialize:\n" + tail(mcp.stdout + mcp.stderr, 1500)
        OUT.ok("MCP over stdio answers")
        status, _ = http("POST", url + "/v1/service/stop", token, {})
        try:
            proc.wait(timeout=30)
            OUT.ok("the hub shut down cleanly")
        except subprocess.TimeoutExpired:
            return False, "the hub did not stop within 30 s of /v1/service/stop"
        return True, ""
    finally:
        if proc.poll() is None:
            kill_tree(proc.pid)
        log.close()
        shutil.rmtree(home, ignore_errors=True)


def find_jdk(minimum: int = 17) -> Optional[Path]:
    """A JDK home the Android build can use: a valid $JAVA_HOME, Android Studio's bundled JDK, or the `java` on PATH,
    whichever is at least `minimum`. (A $JAVA_HOME pointing at a removed JDK is common after an upgrade.)"""
    candidates: list[Path] = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]))
    for studio in ("C:/Program Files/Android/Android Studio/jbr", "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
                   "/opt/android-studio/jbr"):
        candidates.append(Path(studio))
    if shutil.which("java"):
        candidates.append(Path(shutil.which("java")).resolve().parent.parent)
    for home in candidates:
        java = home / "bin" / ("java.exe" if IS_WINDOWS else "java")
        if not java.is_file():
            continue
        r = subprocess.run([str(java), "-version"], capture_output=True, text=True)
        m = re.search(r'version "(\d+)', r.stderr + r.stdout)
        if m and int(m.group(1)) >= minimum:
            return home
    return None


def find_android_sdk() -> Optional[Path]:
    """The Android SDK: $ANDROID_HOME / $ANDROID_SDK_ROOT, else where Android Studio installs it."""
    for c in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk") if IS_WINDOWS else None,
              str(Path.home() / "Library" / "Android" / "sdk"), str(Path.home() / "Android" / "Sdk")):
        if c and (Path(c) / "platforms").is_dir():
            return Path(c)
    return None


def stage_android(ctx: dict) -> tuple[bool, str]:
    gradlew = ROOT / "android" / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    if not gradlew.is_file():
        return True, "skip:no android/gradlew"
    jdk = find_jdk()
    if jdk is None:
        return True, "skip:no JDK 17+ found (install one, or set JAVA_HOME)"
    if os.environ.get("JAVA_HOME") and Path(os.environ["JAVA_HOME"]) != jdk:
        OUT.warn(f"JAVA_HOME ({os.environ['JAVA_HOME']}) is not a usable JDK 17+; using {jdk}")
    sdk = find_android_sdk()
    if sdk is None and not (ROOT / "android" / "local.properties").is_file():
        return True, "skip:no Android SDK found (install Android Studio, or set ANDROID_HOME)"
    OUT.doing(f"JDK: {jdk}; Android SDK: {sdk or 'from android/local.properties'}")
    env = {**os.environ, "JAVA_HOME": str(jdk), **({"ANDROID_HOME": str(sdk)} if sdk else {})}
    ok, out = run([str(gradlew), "testDebugUnitTest", "assembleDebug", "--console=plain", "--no-daemon"],
                  cwd=ROOT / "android", timeout=2400, retries=1, env=env)
    if not ok:
        return False, tail(out)
    apks = sorted((ROOT / "android").rglob("*-debug.apk"))
    OUT.ok("unit tests passed; " + (f"built {apks[-1].name}" if apks else "built"))
    return True, ""


def stage_build(ctx: dict) -> tuple[bool, str]:
    script = ROOT / "scripts" / "build_pyinstaller.py"
    if not script.is_file():
        return True, "skip:no scripts/build_pyinstaller.py"
    has, _ = run([venv_python(), "-c", "import PyInstaller"], timeout=60)
    if not has:
        return True, "skip:PyInstaller is not installed in the venv (pip install pyinstaller)"
    ok, out = run([venv_python(), str(script)], timeout=3600)
    if not ok:
        return False, tail(out)
    exes = sorted((ROOT / "dist").rglob("*.exe"), key=lambda p: p.stat().st_mtime) if (ROOT / "dist").is_dir() else []
    if not exes:
        return False, "PyInstaller finished but produced no .exe under dist/"
    OUT.ok(f"built {exes[-1].relative_to(ROOT)} ({exes[-1].stat().st_size / 1e6:.1f} MB)")
    return True, ""


def stage_deploy(ctx: dict) -> tuple[bool, str]:
    """Restart a hub running from this checkout so it serves the new code."""
    home = Path(os.environ.get("VMH_HOME") or Path.home() / ".vmharness")
    control = home / "control.json"
    if not control.is_file():
        return True, "skip:no hub is running"
    d = json.loads(control.read_text(encoding="utf-8"))
    pid = int(d.get("pid", 0))
    if not pid or not pid_alive(pid):
        return True, "skip:no hub is running"
    try:
        import psutil
        proc = psutil.Process(pid)
        cwd = Path(proc.cwd()).resolve()
        cmd = proc.cmdline()
    except Exception as e:  # noqa: BLE001
        return True, f"skip:cannot inspect the running hub ({e})"
    if cwd != ROOT.resolve() and not any(str(ROOT) in c for c in cmd):
        return True, f"skip:the running hub is not from this checkout ({cwd})"
    OUT.doing(f"restarting the hub (pid {pid}) so it serves this code")
    http("POST", d["url"].rstrip("/") + "/v1/service/stop", d.get("token", ""), {})
    for _ in range(60):
        if not pid_alive(pid):
            break
        time.sleep(0.5)
    else:
        kill_tree(pid)
    flags = (0x00000008 | 0x00000200 | 0x08000000) if IS_WINDOWS else 0
    subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, start_new_session=not IS_WINDOWS)
    for _ in range(120):
        with contextlib.suppress(Exception):
            nd = json.loads(control.read_text(encoding="utf-8"))
            if int(nd.get("pid", 0)) != pid and http("GET", nd["url"].rstrip("/") + "/v1/health")[0] == 200:
                OUT.ok(f"hub restarted (pid {nd['pid']})")
                return True, ""
        time.sleep(0.5)
    return False, "the hub did not come back within 60 s"


STAGE_FNS: dict[str, Callable[[dict], tuple[bool, str]]] = {
    "preflight": stage_preflight, "static": stage_static, "tests": stage_tests, "smoke": stage_smoke,
    "android": stage_android, "build": stage_build, "deploy": stage_deploy,
}


# ---- the run -------------------------------------------------------------------------------------------------------
def plan(args: argparse.Namespace, changed: Optional[set[str]]) -> dict[str, str]:
    """stage -> "" (run) or the reason it is skipped."""
    why: dict[str, str] = {}
    code = touches(changed, CODE)
    for s in STAGES:
        if args.only and s not in args.only:
            why[s] = "not asked for (--only)"
        elif s in ("preflight", "static"):
            why[s] = ""
        elif s in ("tests", "smoke"):
            why[s] = "" if (args.full or code) else "no code changed"
            if s == "smoke" and args.fast:
                why[s] = "--fast"
        elif s == "android":
            why[s] = "" if (args.full or touches(changed, ANDROID)) and not args.fast else (
                "--fast" if args.fast else "android/ did not change")
        elif s == "build":
            why[s] = "" if (args.build or args.full) and not args.fast else "--build not given"
        elif s == "deploy":
            why[s] = "" if not (args.no_deploy or args.fast) and (args.full or touches(changed, DEPLOY)) else (
                "--no-deploy" if args.no_deploy else "--fast" if args.fast else "nothing the hub runs changed")
    if args.only:
        for s in args.only:
            why[s] = ""
    return why


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # a Windows console is cp1252; tool output can hold any character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="every stage, whatever changed")
    ap.add_argument("--fast", action="store_true", help="preflight, static checks and tests only")
    ap.add_argument("--only", action="append", choices=STAGES, help="just these stages")
    ap.add_argument("--build", action="store_true", help="also build the executable")
    ap.add_argument("--no-deploy", action="store_true", help="do not restart a running hub")
    ap.add_argument("--list", action="store_true", help="show the plan and exit")
    args = ap.parse_args()

    started = time.time()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    OUT.line("VM-Harness local CI/CD pipeline")
    changed, rng = changed_files()
    ctx: dict = {"range": rng if changed is not None else None}
    OUT.head("Change detection")
    if changed is None:
        OUT.warn(f"cannot tell what changed ({rng}): running everything")
    else:
        OUT.ok(f"{len(changed)} file(s) changed ({rng})")
    why = plan(args, changed)
    if args.list:
        for s in STAGES:
            OUT.line(f"  {'run ' if not why[s] else 'skip'}  {s:<10} {why[s]}")
        return 0

    results: list[StageResult] = []
    try:
        with Lock():
            for s in STAGES:
                r = StageResult(s)
                results.append(r)
                if why[s]:
                    r.status, r.note = "skipped", why[s]
                    OUT.head(s)
                    OUT.skip(why[s])
                    continue
                OUT.head(s)
                t0 = time.time()
                try:
                    ok, msg = STAGE_FNS[s](ctx)
                except Exception as e:  # noqa: BLE001 - a crashing stage is a failed stage, with its reason
                    ok, msg = False, f"{type(e).__name__}: {e}"
                r.seconds = round(time.time() - t0, 1)
                if ok and msg.startswith("skip:"):
                    r.status, r.note = "skipped", msg[5:]
                    OUT.skip(r.note)
                elif ok:
                    r.status = "passed"
                else:
                    r.status, r.note = "failed", msg
                    OUT.err(f"{s} failed:\n{msg}")
                    break
    except SystemExit as e:
        OUT.err(str(e))
        return 1
    failed = [r for r in results if r.status == "failed"]
    OUT.head("Summary")
    for r in results:
        mark = {"passed": "[ok]  ", "skipped": "[--]  ", "failed": "[ERR] "}[r.status]
        OUT.line(f"  {mark} {r.name:<10} {r.seconds:>7.1f}s  {r.note.splitlines()[0][:90] if r.note else ''}")
    total = round(time.time() - started, 1)
    OUT.line(f"\n  {'FAILED' if failed else 'PASSED'} in {total}s" + (f"  (flaky: {', '.join(ctx['flaky'])})" if ctx.get("flaky") else ""))

    LOGS.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = {"started": stamp, "seconds": total, "status": "failed" if failed else "passed", "commit": ctx.get("commit"),
              "range": ctx.get("range"), "changed": sorted(changed) if changed else None, "tests": ctx.get("tests"),
              "flaky": ctx.get("flaky", []), "stages": [r.__dict__ for r in results]}
    (REPORTS / f"{stamp}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    (REPORTS / "latest.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    with open(REPORTS / "history.jsonl", "a", encoding="utf-8") as h:
        h.write(json.dumps({k: report[k] for k in ("started", "seconds", "status", "commit", "tests")} |
                           {"stages": {r.name: [r.status, r.seconds] for r in results}}) + "\n")
    (LOGS / f"{stamp}.log").write_text(OUT.buf.getvalue(), encoding="utf-8")
    for old in sorted(LOGS.glob("*.log"))[:-50]:
        old.unlink(missing_ok=True)
    for old in sorted(REPORTS.glob("2*.json"))[:-50]:
        old.unlink(missing_ok=True)
    print(f"\n(log: {(LOGS / f'{stamp}.log').relative_to(ROOT)}, report: ci/reports/latest.json)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
