# Streaming: Plan

## Verdict, corrected

GPU-accelerated capture is not available on this host/guest pair. That closes the
virgl/Venus/virtio-gpu track. Three independent reasons, any one of which is
sufficient:

1. **The guest has no userspace driver.** The guest is Omarchy 4.0.4 on
   `linux-omarchy 7.2.5-3` (live ISO kernel `linux-t2 7.2.4`) with Mesa
   `26.2.2`. Mesa `26.2.2` ships no `virgl_dri.so` and no `kms_virtio.so`. A
   full ELF audit of `libgallium-26.2.2-arch1.1.so` found zero
   `virglrenderer` references, zero `vrend_*` / `virgl_*` / `vg_*` symbols and
   no `libvirglrenderer.so.1` dependency. A grep across all 1248 packages in the
   offline mirror found **0** packages containing `virgl` or `venus`. There is no
   virtio/Venus Vulkan ICD. The guest would render through `llvmpipe` — the same
   software rendering it already does — plus extra per-frame virtio
   resource-flush round trips.
2. **The kernel half is fine, and that is the trap.** `CONFIG_DRM_VIRTIO_GPU=m`,
   `CONFIG_DRM_VIRTIO_GPU_KMS=y`, the shipped `virtio-gpu.ko.zst` exists, and
   `udev` autoloads it on a `virtio-vga-gl` device via modalias. The driver
   loads, the KMS device appears, and then Mesa has nothing to bind it to. Note
   that `CONFIG_DRM_VIRTGPU` no longer exists upstream — it was renamed to
   `DRM_VIRTIO_GPU` — so a missing `DRM_VIRTGPU` line is not a finding.
3. **Host Venus is compiled out.** `-device virtio-vga-gl,help` does list
   `venus=<bool>` (default off) and the QEMU binary contains the string
   `venus requires enabled blob and hostmem options`, so the code is compiled in
   and reachable. It is inert: the shipped `libvirglrenderer-1.dll` contains
   `Render server support was not enabled in virglrenderer` and `failed to
   initialize venus renderer`, and has zero references to any Vulkan loader
   (`vulkan-1.dll`, `libvulkan.so.1`, `vkCreateInstance`). `drm_native_context`
   is equally inert. The OpenGL half *is* present (`vrend_*` symbols,
   `eglGetPlatformDisplay` / `eglInitialize` / `eglCreateContext`, over QEMU's
   shipped ANGLE `libEGL.dll`), so the host virgl path would probably
   initialise — there is simply no point, per reason 1.

And before any of that, the device does not boot here at all.

### Dead end: `virtio-vga-gl` under `-display none`

Booting the guest with `-device virtio-vga-gl` alongside `-display none` aborts
QEMU at device realisation:

```
-device virtio-vga-gl: The display backend does not have OpenGL support enabled
It can be enabled with '-display BACKEND,gl=on' where BACKEND is the name of the
display backend to use.
```

The VM never starts. `serial.log` is 0 bytes and no guest code runs. QEMU's own
display path is selected independently of the VNC/SPICE server, so "there is a
VNC server" does not satisfy it.

An isolated probe shows QEMU *does* stay alive with
`-display egl-headless,gl=on -device virtio-vga-gl` (process up, `query-status`
-> `prelaunch`), and `egl-headless` is an available backend on this build. That
is recorded here so nobody retries it as a fix: it converts a non-booting VM
into a booting VM that still renders in software, because of reason 1.

Hyprland 0.56.2 needs EGL/GLESv3 and llvmpipe satisfies it, so the guest keeps
working throughout this document. It is simply CPU-bound, and will stay that
way.

### What follows from that

The bottleneck is the **capture source**, not the GPU. QMP `screendump` costs
40-70ms per frame because it is a full-frame, poll-driven path: a socket round
trip, QEMU serialising the entire framebuffer, VM-Harness decoding it, then a
JPEG encode per client. No damage tracking, no persistence, and nothing survives
between polls.

QEMU ships a built-in VNC server. It is a persistent TCP connection, it tracks
damage itself, it sends only changed regions, and it offers
Raw/CopyRect/RRE/Hextile/Tight/Zlib/ZRLE. **That is the real optimization, and
it needs no GPU.** Everything below is that path.

## What this host can and cannot do

### Cannot do

| Goal | Why not here |
|---|---|
| Guest GPU acceleration via virgl | Mesa `26.2.2` in the guest has no `virgl_dri.so` / `kms_virtio.so`; no `virglrenderer` or `venus` package in the offline mirror. Guest falls back to llvmpipe. |
| Venus / virglrenderer Vulkan encoding | Compiled out of the host `libvirglrenderer-1.dll`; zero Vulkan loader references. `venus=on` is accepted and does nothing. |
| `virtio-vga-gl` as the display device | Aborts QEMU realisation under `-display none`. Needs `-display BACKEND,gl=on`. |
| Hardware GPU passthrough | `nested=False`; WHPX maps guests onto software-rendered virtual GPUs. |
| KVM / HVF | Only `whpx` and `tcg` accelerators are in the QEMU binary. |
| 120fps by raising `MAX_FPS` | `screendump` capture is a blocking round trip; it *is* the unit of one frame. A higher cap only asks for what the path cannot produce. |

### Can do

| Goal | How on this host |
|---|---|
| Continuous guest frame delivery | QEMU's built-in RFB server: `-vnc 127.0.0.1:<n>`. Already wired as `display_type: vnc` in `src/vm_harness/hypervisor/qemu/backend.py`, which emits `-vnc :<n> -display none`. No new dependency. |
| Damage-only transport | The same server: it diffs the framebuffer per client and emits only changed rects, in whichever of Raw/CopyRect/RRE/Hextile/Tight/Zlib/ZRLE the client advertises. |
| Decoding the damage stream | `src/vm_harness/vnc/client.py` — a complete asyncio RFB client with VNC Auth, Hextile/Tight/RRE/CopyRect/Raw decoders and `DesktopSize`, decoding into a persistent BGRA `Framebuffer`. |
| Rendering it | `gui/widgets_vnc.py` — `VNCView` wraps the client's `Framebuffer` and paints it; pointer and key events go back over the same socket. |
| Retaining the current path | QMP `screendump` still works and stays as the fallback. |

## Target architecture

```
Omarchy guest (llvmpipe, CPU-bound)
  L1: Hyprland composites into a software framebuffer
        |
        v   damage is marked by the guest's own page copies
QEMU host process
  L2: built-in VNC server -- diffs the surface per connected client,
        |                encodes only changed rects, keeps a persistent
        |                desktop state per client
        v
VM-Harness
  L3: vm_harness.vnc.client -- RFB client, decodes rects into one persistent
        |                 BGRA Framebuffer (no full-frame reassembly)
        v
consumer
  L4: gui.widgets_vnc.VNCView -- QImage wrapped around that same buffer,
        painted as-is. Input travels back on the same connection.
```

The key insight, and the one the old version of this document got right for the
wrong reasons: `screendump` makes the consumer ask "give me the current
framebuffer." RFB makes the *producer* decide what changed and say so. That
inversion is the whole optimization.

## Stage 1 -- run the guest on QEMU's built-in VNC server

Nothing to invent. `display_type: vnc` in the QEMU backend already produces
`-vnc :<n> -display none` (see `backend.py`, the VNC branch of
`_build_qemu_args`). Bind it to loopback explicitly (`-vnc 127.0.0.1:<n>`) and
set `display_password` if the port is not strictly loopback.

Leave the display device alone. The guest stays on its current VGA with
`-display none`, which is the combination that actually boots. Do not add
`-device virtio-vga-gl`; see the dead end above.

Expected result: a persistent RFB endpoint that emits damage instead of a
poll-driven full frame.

## Stage 2 -- consume pushed damage instead of polling

Connect `vm_harness.vnc.VNCClient` to `127.0.0.1:<n>` and hand its `on_frame`
callback the existing `VNCView.set_framebuffer`.

### Push, not poll — the design point

This is the part that is easy to get wrong. RFB is nominally request/response:
the client sends `FramebufferUpdateRequest` and the server answers. If the
client sends that request every 50ms and treats each answer as a frame, we have
rebuilt `screendump` with a different wire format — still one round trip per
frame, still a fixed floor of one frame interval, and we would have thrown away
the damage tracking we just gained. Polling over VNC reintroduces exactly the
latency being removed.

The correct shape:

- Send `FramebufferUpdateRequest(incremental=1)` **once**, then block on the
  socket until the server pushes a `FramebufferUpdate`. Do not sleep between
  requests.
- Issue the next incremental request only after the previous update has been
  fully decoded and presented. That is backpressure: it keeps exactly one
  request outstanding, so a fast producer cannot queue updates we have not
  drained, and a slow renderer does not cause the server to pile up state.
- Request non-incremental only for the first frame after connect or after a
  `DesktopSize` pseudo-encoding resize. The client already tracks this
  (`_needs_full_refresh`).

`VNCClient.serve()` does **not** do this today. It sends a request, sleeps
`frame_interval` (default 0.05s), then pumps — a 20fps poll loop with the same
round-trip-per-frame structure as the screendump path, capped at 20fps before
any damage is considered. `serve()` needs to become: request once, `await` the
read until an update lands, present, re-request. `pump()` already blocks on
exactly one message and returns frames as they complete, so the change is
confined to the request/sleep ordering in that one loop.

Keep QMP `screendump` wired as a fallback for stills and for the case where the
VNC endpoint is absent.

## Stage 3 -- zero-copy encode/decode (retained)

Still valid, and independent of Stage 2's transport change.

- **Decode (guest surface -> consumer surface).** `proto.decode_rectangle`
  already writes each rectangle directly into the persistent
  `Framebuffer.data` `bytearray`; `Framebuffer.copy_rect` handles CopyRect in
  place; Hextile and Tight blit per subrect. Nothing decodes a whole frame, and
  nothing allocates per frame. `QImage::Format_RGB32` is BGRA little-endian on
  these platforms, so `VNCView` wraps the buffer with no conversion — the
  "wrap the `bytearray`" step is what makes the decode genuinely zero-copy
  rather than a copy per frame with better manners.
- **Blit, don't re-present.** `on_frame` fires once per completed update, after
  the last of its rectangles. That is deliberate: a partially applied update
  must not be shown. Damage is therefore tracked as the rect list of the update
  in flight, and only those rects need repainting on the widget.
- **Cache keyed by tile/encoding.** The persistent framebuffer *is* the cache:
  an unchanged tile is simply not sent, and its pixels are already in the right
  place. An explicit cache earns its keep in two places only — a tile cache for
  the ZRLE/Zlib decoders if they are added, and a render cache for surfaces
  redrawn identically (Hyprland chrome, panels). Do not build one for Hextile or
  Tight; they are already damage-scoped, and a second copy of the framebuffer
  would reintroduce the per-frame copy this stage exists to remove.
- **Encodings.** Start with what the client already decodes
  (`SUPPORTED_ENCODINGS` = Tight, Hextile, RRE, CopyRect, Raw, plus
  `DesktopSize`). QEMU also offers Zlib and ZRLE, which are undecoded today;
  they are a bandwidth win on full-screen animation and a CPU cost on a static
  desktop, so they are a later, measured decision.

### Inherent versus avoidable copies

| Copy | Status |
|---|---|
| Guest framebuffer -> QEMU's VNC server surface | **Inherent.** Inside QEMU; nothing in this codebase can remove it. |
| Server surface -> socket, chunked to MTU | **Inherent.** The network boundary. |
| Socket -> `Framebuffer.data` | Avoidable and avoided: rects are decoded straight into the persistent buffer. |
| `Framebuffer` -> `QImage` -> screen | Avoidable and avoided: the `QImage` wraps the same buffer. |
| Whole-frame decode, per frame | Avoidable and avoided: damage rects only. |
| Re-encode per client (PNG/JPEG of the whole frame) | Avoidable and removed on this path: nothing re-encodes the desktop. |
| Secondary copy for a tile cache | Avoidable; do not add it for Hextile/Tight. |

## Stage 4 -- performance envelope

Measured column is the current path. Everything in the VNC column is
**projected and unmeasured** — no numbers below have been taken on this host
since the path was not previously wired, and they are written as orders of
magnitude to be replaced by measurement.

| | Current: `screendump` + JPEG | VNC damage stream (projected) |
|---|---|---|
| Capture mode | Poll, full frame every time | Pushed, damage only |
| FPS | **~15-25 (measured, 40-70ms/frame)** | Equals the guest's redraw rate. A software-rendered guest sets the ceiling, so 15-25 is the realistic *animation* figure today; idle cost drops to near zero. |
| Per-frame host CPU (VM-Harness) | Full-frame raster decode + JPEG encode per client | Rect decode only, into an existing buffer. No full-frame work, no per-client encode. |
| Per-frame host CPU (QEMU) | Full-frame serialise every poll | Diff plus rect encode, per changed region |
| Network per frame | **~500 KB (measured, full JPEG)** | Proportional to what changed: near zero on a static desktop, low tens of KB on moderate damage. Unmeasured. |
| GPU required | No | **No.** This is the point: the win is available today on llvmpipe. |
| Transport latency floor | One blocking round trip per frame (40-70ms) | One socket read per damage batch; no polling interval in the path |

The number that matters is not the FPS ceiling. It is that per-frame cost
becomes proportional to *what changed* rather than to framebuffer resolution.
A 4K desktop that is 99% static costs about the same to stream as a 1080p one.

## Stage 5 -- runtime verification checklist

The findings above are from a static audit plus one boot probe. These are the
commands to confirm them on a live guest before building on them.

Guest side:

```sh
lspci -nnk -d 1af4:1050            # virtio-gpu present? which kernel driver is bound?
dmesg | grep -i virtio_gpu         # expect the module loading; absence = no device, not "no driver"
ls -l /usr/lib/dri | grep -i 'virtio\|virgl'   # no virgl_dri.so / kms_virtio.so == finding 1 confirmed
glxinfo -B                        # "OpenGL renderer string: llvmpipe" == no acceleration
eglinfo                            # same conclusion via EGL
```

`glxinfo -B` reporting `llvmpipe` is the single decisive check. If it ever
reports anything else, finding 1 is wrong and this document needs revisiting
before anything is built on it.

Host side:

```sh
qemu-system-x86_64 -device virtio-vga-gl,venus=on ...   # stderr: inert, no effect
qemu-system-x86_64 -device virtio-vga-gl,help            # venus=<bool> is offered
```

And on the streaming path itself:

```sh
ss -tnp 'sport = :<vnc-port>'        # connection stays ESTABLISHED across a long idle
```

A connection that is re-established every frame means the poll path is still in
place; the damage path holds one socket open.

## What is explicitly out of scope

- **Venus on this host.** Compiled out of `libvirglrenderer-1.dll`, and the
  guest has no Venus driver regardless. A hardware-GPU path needs a Linux host
  with KVM, or a different host build. Not a configuration problem.
- **`virtio-vga-gl` as the display device on this host.** It aborts QEMU under
  `-display none`, and `-display egl-headless,gl=on` only trades a boot failure
  for software rendering. Recorded so it is not retried.
- **KVM / nested virtualization.** Absent on this build; only `whpx` and `tcg`
  are compiled in.
- **120 FPS.** Not reachable from a software-rendered guest on this host, and not
  a matter of capture path. The guest composites through llvmpipe on CPU, and no
  amount of damage-streaming makes a CPU-bound compositor draw faster. If 120fps
  becomes a hard requirement, the options are a Linux host with KVM, a guest image
  whose compositor is not CPU-bound, or accepting a lower ceiling.

What this architecture does deliver is worth stating plainly, because it is a
different and more useful property than a frame-rate ceiling: frame cost becomes
proportional to what changed. A mostly-static desktop costs almost nothing to
stream regardless of resolution, and stays cheap to serve to many clients at
once, because no client causes a re-encode. On a desktop that is usually idle,
that is the win. On full-screen animation, the guest is the limit and this
architecture does not pretend otherwise.