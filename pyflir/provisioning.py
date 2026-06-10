"""Camera IP provisioning helpers for FLIR cameras."""

from __future__ import annotations

from pyGigEVision import GVCPClient


def force_ip(camera: dict, ip: str, mask: str, gateway: str | None = None) -> None:
    """Assign a new IP to a discovered camera by MAC, via GVCP FORCEIP.

    Re-homes a camera that is on the wrong subnet (or fell back to
    link-local) without changing host NIC configuration. The camera
    reboots its IP stack; re-run :func:`pyflir.discover` afterwards
    to see the new address.

    Parameters
    ----------
    camera : dict
        A camera dict from :func:`pyflir.discover` (must contain ``"mac"``).
    ip, mask : str
        New IPv4 address and subnet mask for the camera.
    gateway : str or None, optional
        Default gateway, or ``None`` for none.
    """
    mac = camera.get("mac")
    if not mac:
        raise ValueError(
            "camera dict has no 'mac' (discovered by an older pyGigEVision?); "
            "cannot send FORCEIP without the target MAC."
        )
    GVCPClient.force_ip(mac, ip, mask, gateway or "0.0.0.0")
