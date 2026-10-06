"""Where QEMU's built-in VNC server listens, per VM.

QEMU's ``-vnc`` takes a *display number*, and the TCP port it binds is always
``5900 + display``. There is no way to hand it a literal port, so a "per-VM VNC
port" scheme has to be a display-number scheme, and the number has to come from
something a VM already owns uniquely.

What a VM already owns uniquely is its QMP management port. The launcher
allocates those from a fixed base (:data:`DEFAULT_QMP_PORT_BASE`, 4444) and
increments per VM, so two VMs never share one. Offsetting the QMP port into a
reserved display window therefore gives every VM a distinct VNC display without
the launcher allocating, persisting or reclaiming anything extra -- the streaming
bridge can work out any VM's VNC endpoint from the registry entry it already
reads the QMP endpoint out of.

The window, and why it is where it is::

    QMP port 4444  ->  display 100  ->  VNC port 6000
    QMP port 4445  ->  display 101  ->  VNC port 6001
    ...
    QMP port 4543  ->  display 199  ->  VNC port 6099

:data:`VNC_DISPLAY_BASE` of 100 puts the window at TCP 6000-6099, deliberately
clear of two ranges this host already uses:

* **5900-5999**, where QEMU's own default display :0 lives and where
  ``gui.panels_display`` puts a hand-configured VNC port. Overlapping it would
  mean a capture endpoint could be taken by, or take, a display somebody opened
  on purpose.
* **5930+**, where ``QEMUBackend`` allocates SPICE (``DEFAULT_SPICE_PORT_BASE``).

:func:`vnc_display_for_qmp_port` raises rather than folding back onto the bottom
of the window when a QMP port falls outside the range it maps, because the only
collision-free alternative -- wrapping with a modulo -- turns two distinct VMs
into the same display as soon as more than :data:`VNC_DISPLAY_SPAN` VMs exist.
A loud refusal at launch is worth more than a capture endpoint that silently
belongs to somebody else's VM.

Security
--------

The bind address is loopback and the share mode is ``force-shared``. Together
those mean: **anything on this host that can open a loopback socket can attach
and watch, and no client can lock another out.** That is the same posture as the
``-spice disable-ticketing=on`` already used in this repository for a
host-local display, and it is a deliberate choice for a capture endpoint -- but
it is not authentication, and an unauthenticated VNC on a routable address would
be a remote desktop nobody asked for. :func:`vnc_display_arg` therefore refuses
any host that is not loopback, so the mistake cannot be made by editing a config
file or a launch script. If a VM genuinely needs remote VNC, it needs
``password-secret`` and a different bind, both of which are out of scope here.
"""

from __future__ import annotations

#: QEMU computes the TCP port from the display number, always this way.
VNC_DISPLAY_PORT_BASE = 5900

#: Mirrors ``hypervisor.qemu.backend.DEFAULT_QMP_PORT_BASE``. Duplicated rather
#: than imported: that module is the QEMU launcher (process management, firmware
#: discovery, disk creation), and this module is arithmetic on a port number that
#: both the launcher and the streaming bridge need without dragging the rest in.
DEFAULT_QMP_PORT_BASE = 4444

#: First display number this module hands out, and how many it will hand out.
VNC_DISPLAY_BASE = 100
VNC_DISPLAY_SPAN = 100

#: The bind address, and the first half of the security note in the module
#: docstring. Not configurable: a caller that wants a different address has to
#: edit the command line by hand, where it is visible.
VNC_BIND_HOST = "127.0.0.1"

#: QEMU's default is ``allow-exclusive``, which refuses a second client while one
#: is connected. A capture endpoint that can be locked out by an unrelated
#: viewer is a capture endpoint that silently stops capturing, so the share mode
#: is forced. The cost -- no client can claim exclusive mode -- is acceptable
#: because the only thing sharing the socket is other viewers of the same guest.
VNC_SHARE_MODE = "force-shared"

#: Hosts that mean "this machine". ``[::1]`` is the bracketed form because that
#: is how it appears inside a ``-vnc`` spec.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "ip6-localhost"})

#: First and last TCP port this module will hand out: displays 100..199.
MIN_VNC_PORT = VNC_DISPLAY_PORT_BASE + VNC_DISPLAY_BASE
MAX_VNC_PORT = VNC_DISPLAY_PORT_BASE + VNC_DISPLAY_BASE + VNC_DISPLAY_SPAN - 1


class VncEndpointError(ValueError):
    """A VNC endpoint that cannot be built, or that would not be safe."""


def is_loopback(host: object) -> bool:
    """True when ``host`` is one this machine resolves without a network hop."""
    if not isinstance(host, str):
        return False
    return host.strip().strip("[]").lower() in {h.strip("[]").lower() for h in LOOPBACK_HOSTS}


def vnc_display_for_qmp_port(
    qmp_port: int,
    qmp_port_base: int = DEFAULT_QMP_PORT_BASE,
) -> int:
    """The VNC display number that belongs to the VM whose QMP port is this.

    ``qmp_port_base`` is the base the launcher allocated from; it is a parameter
    rather than a constant because ``QEMUBackend`` lets a deployment move it, and
    a window anchored to the wrong base would hand two VMs the same display.
    """
    if isinstance(qmp_port, bool) or not isinstance(qmp_port, int):
        raise VncEndpointError(f"QMP port must be an int, got {qmp_port!r}")
    if qmp_port <= 0:
        raise VncEndpointError(f"QMP port must be positive, got {qmp_port}")
    if isinstance(qmp_port_base, bool) or not isinstance(qmp_port_base, int) or qmp_port_base <= 0:
        raise VncEndpointError(f"QMP port base must be a positive int, got {qmp_port_base!r}")
    display = VNC_DISPLAY_BASE + (qmp_port - qmp_port_base)
    if display < VNC_DISPLAY_BASE or display >= VNC_DISPLAY_BASE + VNC_DISPLAY_SPAN:
        raise VncEndpointError(
            f"QMP port {qmp_port} maps outside the reserved VNC window "
            f"(displays {VNC_DISPLAY_BASE}-{VNC_DISPLAY_BASE + VNC_DISPLAY_SPAN - 1}, "
            f"ports {MIN_VNC_PORT}-{MAX_VNC_PORT}); move qmp_port_base or widen "
            f"VNC_DISPLAY_SPAN rather than letting this VM collide with another"
        )
    return display


def vnc_port_for_qmp_port(
    qmp_port: int,
    qmp_port_base: int = DEFAULT_QMP_PORT_BASE,
) -> int:
    """The TCP port QEMU's VNC server will listen on for that VM."""
    return VNC_DISPLAY_PORT_BASE + vnc_display_for_qmp_port(qmp_port, qmp_port_base)


def vnc_display_arg(
    port: int,
    *,
    host: str = VNC_BIND_HOST,
    share: str = VNC_SHARE_MODE,
) -> str:
    """The right-hand side of ``-vnc`` for a given TCP port.

    ``6000`` becomes ``127.0.0.1:100,share=force-shared``: loopback only, forced
    shared, no password. The security note in the module docstring is the reason
    for all three.
    """
    if isinstance(port, bool) or not isinstance(port, int):
        raise VncEndpointError(f"VNC port must be an int, got {port!r}")
    if port < MIN_VNC_PORT or port > MAX_VNC_PORT:
        raise VncEndpointError(
            f"VNC port {port} is outside this project's range {MIN_VNC_PORT}-{MAX_VNC_PORT}; "
            f"see VNC_DISPLAY_BASE for why the window is reserved"
        )
    if not is_loopback(host):
        raise VncEndpointError(
            f"refusing to bind VNC to {host!r}: this endpoint has no password, so it must "
            f"be loopback-only"
        )
    display = port - VNC_DISPLAY_PORT_BASE
    return f"{host}:{display},share={share}"


def vnc_arg_for_qmp_port(
    qmp_port: int,
    qmp_port_base: int = DEFAULT_QMP_PORT_BASE,
    *,
    host: str = VNC_BIND_HOST,
    share: str = VNC_SHARE_MODE,
) -> str:
    """``-vnc``'s argument for the VM whose QMP port is ``qmp_port``.

    This is the one call the launcher needs: it already knows the QMP port, and
    this turns it into a display number and a loopback bind in one step.
    """
    return vnc_display_arg(
        vnc_port_for_qmp_port(qmp_port, qmp_port_base), host=host, share=share
    )


def parse_vnc_port(spec: object, *, host: str = VNC_BIND_HOST) -> int:
    """Read the TCP port back out of a ``-vnc`` spec.

    Accepts the forms QEMU does -- ``"100"``, ``":100"``, ``"127.0.0.1:100"``,
    ``"[::1]:100"`` -- and returns the TCP port, not the display number, because
    that is what a caller comparing against a registry entry wants. A bare
    number is a *display* here for the same reason it is one to QEMU; taking it
    as a literal port would silently disagree with the process being described.

    The host, when present, must be loopback: a spec that names a routable
    address is refused rather than quietly accepted, because the only way to
    reach this function is to describe an endpoint somebody is about to use.
    """
    if isinstance(spec, bool):
        raise VncEndpointError(f"VNC spec must be a string, got {spec!r}")
    if isinstance(spec, int):
        raise VncEndpointError(
            f"{spec!r} is a bare number, which means display {spec} to QEMU and "
            f"port {spec + VNC_DISPLAY_PORT_BASE} here; write the display explicitly"
        )
    if not isinstance(spec, str) or not spec.strip():
        raise VncEndpointError(f"VNC spec must be a non-empty string, got {spec!r}")
    text = spec.strip()
    # Drop ",option,option" -- share=, to=, password-secret=, and the rest are
    # not part of the address.
    text = text.split(",", 1)[0].strip()
    if not text:
        raise VncEndpointError(f"no address in VNC spec {spec!r}")
    if text.count(":") == 0:
        return _display_port(text, spec)
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or not text[end + 1:].startswith(":"):
            raise VncEndpointError(f"malformed IPv6 VNC spec {spec!r}")
        bound, text = text[1:end], text[end + 2:]
    else:
        bound, _, text = text.partition(":")
    if bound and not is_loopback(bound):
        raise VncEndpointError(
            f"VNC spec {spec!r} binds to {bound!r}, which is not loopback; this "
            f"endpoint is unauthenticated"
        )
    return _display_port(text, spec)


def _display_port(text: str, spec: object) -> int:
    candidate = text.strip().lstrip(":").strip()
    if not candidate.isdigit():
        raise VncEndpointError(f"VNC spec {spec!r} has no display number")
    display = int(candidate)
    if display < 0:
        raise VncEndpointError(f"VNC display number must not be negative, got {display}")
    port = VNC_DISPLAY_PORT_BASE + display
    if port > 65535:
        raise VncEndpointError(f"VNC display {display} is past the last usable port")
    return port