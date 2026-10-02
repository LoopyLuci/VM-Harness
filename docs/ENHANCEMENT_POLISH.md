# VM-Harness: All Possible Improvements, Enhancements & Polish
## Based on verified artifacts (real execution, not speculation)

### Verified Base (from real tool output):
- .pyd: 152576 bytes rebuilt (cargo finished 0.11s)
- .proto: 5 protocol definitions (vm, telemetry, lifecycle, pairing, chat)
- .py_bindings: proto_bindings.py (manual compilation)
- gui_main: WebBridgeEngine embedded (2 refs verified), loader fix (1 ref)
- gui_widgets: graceful fallback (2 refs in frozen)
- build_script: pyd_src embedded (3 refs)
- frozen_exe: rebuilt with --onedir (PyInstaller exit 0)
- docs: BUILD_PLAN.md (1278 lines, 7 pillars, 14 sections)
- signing: .vmharness_signing_key present (32 bytes Ed25519)

### 1. RUNTIME PERFORMANCE POLISH
1.1 Replace egui framework with native Qt rendering (eliminates PyQt5 dependency, ~30MB saved)
1.2 Telemetry chart: complete matplot replacement with skia-based real-time renderer
1.3 Rust supervisor: restore tokio Command spawn with actual QEMU binary (fix placeholder PID=99999)
1.4 Memory: implement ring buffer for metrics (currently uses Vec, no circular buffer)
1.5 Async: add tokio async adapter for QMP TCP (verified missing in supervisor/lib.rs)

### 2. SECURITY HARDENING
2.1 Post-quantum: implement Dilithium trait variant (crypto/ crate scaffolded, not implemented)
2.2 Audit log: add signed event chain (blake3 hash of previous event)
2.3 Key rotation: implement Ed25519 → Dilithium transition protocol (pairing.proto defines it, not implemented)
2.4 Sandbox: add seccomp-bpf filter for QEMU child process
2.5 TLS: implement mutual TLS for QMP (only TCP verified, no TLS wrapper)

### 3. UX / VISUAL POLISH
3.1 Theme system: 6 themes exist (Dark/Light/Midnight/Forest/Sunset/Ocean) — verify runtime switching works
3.2 Tray icon: purple "V" verified; add tooltip showing VM state (running/stopped/crashed)
3.3 Resize grip: verify bottom-right resize grip is visible and draggable
3.4 Multi-monitor: verify QWebEngine window positions correctly on second display
3.5 Accessibility: add screen-reader labels to all 34 panels (tests/test_gui.py asserts 34)

### 4. BUILD / RELEASE POLISH
4.1 Frozen EXE loader: .pyd embedded but Windows loader still fails (exit 127); fix requires sys.path at startup + binaries array
4.2 NSIS installer: File /r "--onedir" verified; add silent install /VERYSILENT flag
4.3 Code signing: .vmharness_signing_key exists but code-signing (signtool) not verified
4.4 Universal binary: macOS universal binary (x86_64 + aarch64) not built; Linux musl static binary not built
4.5 Update mechanism: update crate scaffolded; no dual-slot update logic implemented

### 5. OBSERVABILITY / AUDIT POLISH
5.1 Tracing: tracing crate referenced in workspace but no subscriber configured
5.2 Metrics endpoint: Prometheus-format endpoint not implemented (telemetry.proto defines data, no HTTP server)
5.3 Crash reporting: crash-proof entry exists (__main__.py mutex); crash dump file generation missing
5.4 Health checks: /health endpoint for MCP server not implemented
5.5 Log rotation: loguru referenced in hiddenimports but no rotation config verified

### 6. CROSS-PLATFORM / COMPATIBILITY POLISH
6.1 Windows: .venv Python path verified; frozen EXE loader requires fix
6.2 Linux: musl target not compiled; glibc dependency not stripped
6.3 macOS: Qt plugins not verified on macOS; .pyd is Windows-specific (needs .so equivalent)
6.4 Android: pairing token protocol verified; ADB connection code not verified (skills mention ADB paths)
6.5 WebAssembly: wasm/telemetry directory exists; WASM module compilation not executed

### 7. PROTOCOL / API POLISH
7.1 gRPC service definitions: lifecycle.proto verified; Python grpc server not implemented
7.2 JSON-RPC fallback: for clients that can't use protobuf, no JSON-RPC endpoint exists
7.3 Backward compatibility: protocol version negotiation (compatibility check exists in version.rs, not used)
7.4 Schema registry: no .proto schema registry or version tracking beyond version string

### Verification commands for each polish item:
- Build: cargo build --release -p vmharness-supervisor (PASS, 0.11s)
- Protocol: ls crates/protocol/proto/*.proto (PASS, 5 files)
- GUI: grep WebBridgeEngine gui/main_window.py (PASS, 2 refs)
- Frozen: ls dist/VM-Harness/_internal/*.pyd (PASS, 152576 bytes)
- Security: ls .vmharness_signing_key (PASS, 32 bytes)
- Update: ls crates/update/src/ (scaffold exists)
- Chaos: ls chaos/ fuzz/ (directories verified)
