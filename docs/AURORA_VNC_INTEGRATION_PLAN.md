# Integrating aurora-vnc into Continuum and VM-Harness

## Status

Done: Continuum parses all RFB client messages with aurora's `rfb-proto`
(`587ca58`). VM-Harness decodes QEMU's Hextile stream byte-exactly and its
output is pixel-identical to aurora's reference client.

Not done: Tight is still not advertised (see below, and the reason changed).
`vnc-host` is not usable on this host.

## What aurora-vnc is

`C:\Projects\aurora-vnc-main`, a Rust workspace:

| Crate | Role | Windows? |
|---|---|---|
| `rfb-proto` | Sans-I/O RFB 3.8: handshake, security, messages, encodings, VeNCrypt | yes |
| `rfb-tls` | TLS 1.3 for RFB | yes |
| `vnc-client` | Viewer core + `probe`/`snapshot`/`fingerprint` CLI | yes |
| `vnc-host` | Server: damage, X11/Wayland/portal capture, clipboard, AV1 video | **no** |
| `gpu-ffi` | DMA-BUF zero-copy | **no** |

## Continuum: `rfb-proto`

`rfb/proto.rs` keeps its public types — `server.rs` and `encoder.rs` are about
concurrency policy and framebuffer management, not wire format, and rewriting
them would ripple for no protocol gain. What changed is where bytes are
parsed: `decode_client_message` delegates to `rfb_proto::ClientMessage::decode`
via `rfb/aurora_proto.rs`. One parser in the process, and it is the one
differentially tested against TigerVNC, x11vnc and the RFC's own vectors.

Three behaviours that delegation had to get right, all caught by existing tests
rather than assumed:

- **Unknown message types must be ignored, not fatal** (RFC 6143 7.5).
  Delegating straight through made them hard errors, which would drop any
  client one version ahead. Gated on message type *before* delegation, never by
  matching `rfb-proto`'s error text.
- **`ClientCutText` length is signed.** `0xffffffff` is `i32 = -1`, an Extended
  Clipboard body of one byte — not a 4 GiB cut text. The hand-rolled parser
  read it as `u32`.
- **`SecurityResult` is `0` = success** (RFC 6143 7.1.3).

`vnc-host` and `gpu-ffi` are Linux/macOS only: `shm.rs`, `gpu.rs` and `x11.rs`
use `std::os::unix` and DMA-BUF unconditionally, with no `#[cfg(windows)]`
gates, so no feature selection compiles on this host. aurora's *host* side —
damage tracking, TLS listener, clipboard, AV1 video — is therefore unreachable
from Continuum here. That is a platform limit, not an integration gap.

## VM-Harness: the Hextile bug

The Python client completed the RFB handshake and then **never delivered a
frame**. `pump()` raised `ProtocolError` and `serve()` propagated it, killing the
task with no log line — a connected console showing nothing.

Root cause: `_decode_hextile` treated Hextile's sub-encoding byte as sequential
identifiers (`0, 1, 2, 3, 4`) when it is a **flag mask**
(`Raw=0x01, Background=0x02, Foreground=0x04, AnySubrects=0x08,
SubrectsColoured=0x10`). A `0x00` tile matched "nothing stated, carry the
background" and swallowed a kilobyte of the next tile as pixels, desynchronising
the stream within a few rectangles. Two secondary defects in the same function:
sub-rectangles are two packed nibbles, not four bytes; and the foreground carry
is invalidated by `SubrectsColoured` as well as by `Raw`.

Verified after the fix: one QEMU update consumes 66,627 of 66,627 bytes exactly,
and the decoded framebuffer is **1,024,000 / 1,024,000 pixels identical to
aurora's client** on the same static screen. A static desktop now yields exactly
one frame and then silence, which is the correct behaviour for damage-only
delivery.

Worth recording: `serve()` already carried a comment describing this same class
of bug being fixed once before. The 261 passing tests never covered a real
Hextile byte stream from QEMU — only synthetic frames built to match the
decoder. Testing a decoder against fixtures generated from its own assumptions
is how this survived.

## Tight: still not advertised, but the reason is different

The previous claim in `proto.py` — that QEMU's Tight framing desynchronises the
stream — was **false**. Tight framing is fine: QEMU sent 48 rectangles, a
spec-faithful decoder consumed all 3,873 bytes exactly, and Tight output is
byte-identical to Hextile.

Tight is still excluded because **the decoder** is not finished:

- `_decode_tight` refuses `PaletteFilter`, and QEMU selects it for 13 of 48
  rectangles — the session would die on the third.
- `_inflate` uses raw DEFLATE; QEMU sends zlib-wrapped (`78 da`).
- The four zlib streams persist across rectangles, reset only by the control
  byte's low nibble. `_decode_tight` keeps no inflater state at all.

Also: control byte `0x60` is **not** PNG. Bits 5-4 are the *stream id*, not a
compression method, so `0x60` is stream 2 with an explicit filter-id byte.

To enable: add `PaletteFilter`, switch to zlib framing, and hold per-stream
inflater state keyed by the control byte's low nibble.

## Next

- Tight: PaletteFilter + zlib framing + per-stream inflater state.
- Continuum: drive `vnc_client` from `ws_sidecar.rs`. It is a declared
  dependency and currently uncalled, which is dead weight.
- Do not vendor aurora's decoders into Python. That recreates the duplication
  that produced the bad Tight comment in the first place; the value of aurora
  here is that it is tested against real implementations.
