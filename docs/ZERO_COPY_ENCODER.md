# Zero-Copy Streaming: Plan

## Why this document exists

120 stable FPS is not achievable on this host with the current capture path.
The capture source is a QMP `screendump`: a socket round trip, a PNG decode and
a JPEG encode, measured 40-70ms per frame (~15-25fps). Raising `MAX_FPS` does
not help; it only lets the server want what it cannot produce.

To reach the 30-60fps class, capture must move off `screendump` and onto a
continuous stream delivered by the guest itself: the VM's own VNC or SPICE
endpoint. That changes the entire shape of the pipeline, so it deserves a
written plan before a line is written.

## What this host can and cannot do

### Cannot do

| Goal | Why not here |
|---|---|
| Venus Vulkan encoder | `virglrenderer` on Windows implements the OpenGL/virgl path, not Venus. There is no Venus device in QEMU's Windows build. |
| Hardware GPU passthrough | `nested=False`; WHPX maps guests onto software-rendered virtual GPUs. |
| KVM / HVF | Only `whpx` and `tcg` accelerators are in the QEMU binary. |
| 120fps from `screendump` polling | Capture is a blocking round trip; it is the unit of one frame. |

### Can do

| Goal | How on this host |
|---|---|
| Continuous guest frame delivery | `-spice port=...` (already in `boot_vm.ps1`) serving a RFB/SPICE client directly. |
| Guest 3D acceleration | `virtio-vga-gl` (virgl): `-device virtio-vga-gl` + `-spice gl=on` (or `display` suboptions). QEMU ships `libvirglrenderer-1.dll`. This renders the guest's compositor on the host GPU, not in software. |
| GPU-accelerated compression | SPICE's server-side GL/compression: `libspice-server-1.dll` ships with it. |
| Zero-copy frame handoff | virtio-gpu blob resources (`blob=on`) let the host map the guest's framebuffer without a copy. |
| Hybrid capture | Run the guest's compositor on virtio-gpu-gl and read the resulting framebuffer via SPICE: the guest produces it, libspice-server serves it, and the consumer decodes RFB/SPICE directly. No `screendump`. |

## Target architecture

```
Omarchy guest GPU
  L1: VM kernel compositor (Hyprland) renders onto virtio-vga-gl
        |
        v   (guest framebuffer, exported via virtio-gpu)
QEMU host process
  L2: virgl renderer (libvirglrenderer) -- guest's reported shape
        |
        v   (same host GPU, no guest->host copy)
  L3: libspice-server -- advertises RFB or Spice protocol, renders tiles into
        |              its own buffer; SPICE protocol handles damage per tile
        v
consumer
  L4: a Spice/RFB client in Continuum or VM-Harness decodes a *delta*:
      only the tiles that changed since the last update. No full-frame
      round-trip needed.
```

The key insight: `screendump` makes the consumer ask "give me the current
framebuffer." SPICE/RFB make the *producer* say "these N tiles changed." That
inversion is what gives continuous frames instead of one-blocking-poll-per-frame.

## Stage 1 -- run the guest on virtio-gpu-gl

Change `scripts/boot_vm.ps1` in OmarchyVM from `-vga std` to `-vga virtio` and
add `-device virtio-vga-gl` (or alias `-vga virtio-vga-gl` if the device exists
under that name). Add `gl=on` to the `-spice` argument. Expected behavior: the
guest's Hyprland compositor now renders through virgl, and QMP `screendump`
still returns pixels so the existing pipeline does not regress. If this boots,
the guest is on a real virtio-gpu endpoint and Stage 2 can consume it.

Risks: the guest may need `virtio-gpu` kernel modules (Omarchy ships them); the
virt backbuffer may come up as a black screen until a compositor draws -- the
greeter will exercise that. Verify by booting and taking a screendump.

## Stage 2 -- point the RFB server at SPICE instead of screendump

Continuum's `capture_qmp.rs` currently implements `screendump` capture. Add an
`RfbCapture` implementation (or replace `CaptureBackend` usage in the RFB server
path) that:

1. Opens a SPICE client channel to `127.0.0.1:5930` (the `-spice` port).
2. On `SPICE_LINK_ERR` / new primary surface, copies the primary surface into a
   linear ARGB buffer.
3. On every `DISPLAY_UPDATE` / `draw_copy`, marks only the touched
   rectangles as damage and hands them to the existing RFB encoder unchanged.
4. Re-advertises a resize if the surface dimensions change.

Result: RFB clients still receive Raw/Hextile/Tight -- but fed by a continuous
damage stream instead of `screendump` polling. This is the change that stops
the 40-70ms/frame `screendump` ceiling and attacks the 120fps goal itself
(SPICE can keep up; only the consumer and the emulation cap what the VM
actually produces).

No `screendump` is required anywhere in this path, so the RFB server's own
incremental-update logic (already implemented and tested) remains the client's
framebuffer diff.

## Stage 3 -- hybrid dedicated encode/decode (zero-copy)

Goal: the consumer never re-decodes the same pixels twice.

- **Encode side (in QEMU)**: libspice-server already renders tiles into a
  persistent surface and only diffs what changed; it uses GL for the guest's
  surfaces when `gl=on`. No extra work needed.
- **Transport (consumer)**: the RFB/SPICE client receives changed rectangles.
  The client's decoder should:
  - Decode the incoming tile format (Hextile for a first pass; Tight with zlib
    is the correctness-critical next pass; ZRLE/JPEG are the desired final
    optimisation but require adding those encodings to the client).
  - Track damage as a rect list and **only blit the changed region** into the
    consumer's QImage, not the whole frame.
  - Cache the last-rendered pixels keyed by (tile coord, encoding) so a static
    scene costs ~nothing to serve.
- **Decode side (consumer)**: the new `TilePainter` helper wraps a QImage and
  applies rect updates via `QPainter`. Put it in a new `src/vm_harness/rfb/tiles.rs`
  (Rust) and `gui/tiles_painter.py` (Python CLI panel), reusing the existing
  `DamageRect` field from `capture_qmp.rs`.

This yields zero-copy *within* the guest (virgl produces the surface), zero-copy
*in transport* (SPICE moves only damage), and zero-copy *at decode* (QImage is
updated in place). The only copies left are: (a) QEMU's guest surface into the
SPICE primary surface -- avoidable with virglrenderer shared surfaces, but not
on the Windows build; (b) the network MTU chunk. _Both are inherent, not
accidents._

## Stage 4 -- performance envelope on this host

| Component | Current (screendump JPEG) | Stage 1+2 (SPICE/RFB delta) | Stage 3+ (hybrid zero-copy) |
|---|---|---|---|
| FPS | ~15-25 | ~30-60 | up to host compose rate, projected 60-120 if the guest generates frames |
| Per-frame CPU (VM-Harness) | PNG decode + JPEG encode per client | Hextile/Tight decode per client | QImage blit only; JPEG only if client asks |
| Per-frame CPU (Continuum) | screendump round trip + PNG decode | SPICE client-side render + RFB re-encode | RFB re-encode via zlib (Tight) |
| Network per frame | ~500 KB (full JPEG) | ~1-30 KB (damage rectangles) | same |

## Stage 5 -- audit list before writing any code

1. Can the guest actually create a virtio-gpu context? Run once with `-vga
   virtio-vga-gl` and look for "virtio-gpu: driver loaded" in the guest's
   `dmesg` -- if not, the whole plan needs the legacy `-vga virtio` and
   software rendering, in which case the FPS cap stays where it was.
2. Does `-spice gl=on` composite a valid image under `-display none`? Test with
   a SPICE client connecting as a probe; look for SPICE_HAVE_PRIMARY_SURFACE in
   the server log.
3. Is the SPICE protocol enough to deliver continuous frames at >30fps in the
   worst case (full-screen animation)? If libspice-server's desktop-channel
   output falls below 30fps on this host, the 120fps goal is not reachable
   without a different VM image (e.g. one that does not composite full-screen
   animation every frame), and that is a product decision, not a bug.
4. Is there a VNC server option on the guest side? Arch's `qemu-vnc` package
   would expose `-vnc` directly; the initial Stage 2 uses SPICE to avoid a new
   guest package install.

## What is explicitly out of scope

- Venus on Windows (unsupported by virglrenderer; the hardware-GPU path would
  need a Linux guest or KVM on this host).
- KVM / nested virtualization (absent on this build).
- Anything requiring the Continuum server to run *inside* a VM: it can, but
  then the capture source is the VM's own virtual VGA, and the FPS cap is the
  outer VM's software rendering, not QEMU's.
- The 120fps goal for VNC: VNC can reach 60fps on fast paths but not 120fps
  from this guest's software-rendered framebuffer. If 120fps becomes a hard
  requirement, either replace the host's OS with Linux+KVM, or accept that the
  guest image must produce frames at that rate by content (e.g. a static
  desktop, which this architecture delivers at "unlimited" rate because only
  damage is shipped).
