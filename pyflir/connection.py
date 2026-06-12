"""Connection diagnostics for FLIR cameras."""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field

from .errors import ConnectionStats

logger = logging.getLogger(__name__)

_VPN_NAME_KEYS = ("tailscale", "zerotier", "wireguard", "vpn", "wg")
_USB_ADAPTER_KEYS = ("usb", "ax88", "asix", "rtl8153", "rtl8156", "hub")


@dataclass
class ConnectionReport:
    """Result of :func:`tune_connection`.

    Attributes
    ----------
    recommended : dict
        Suggested ``packet_size`` and any other kwargs to pass to
        :meth:`Camera.start_stream`.
    probe : ConnectionStats or None
        Read-only facts gathered from the host NIC (speed, USB, MTU).
    warnings : list of str
        Human-readable warnings about the network environment.
    """

    recommended: dict
    probe: ConnectionStats | None = None
    warnings: list[str] = field(default_factory=list)

    def apply(self, cam) -> dict:
        """Store the recommended packet_size on *cam* for future streams.

        Does not change any system or camera setting; records the
        recommendation as ``cam._recommended_packet_size``.
        """
        ps = self.recommended.get("packet_size")
        if ps is not None:
            cam._recommended_packet_size = int(ps)
        return dict(self.recommended)


def tune_connection(
    cam,
    probe_only: bool = True,
    read_nic_info: bool = False,
) -> ConnectionReport:
    """Probe the host NIC and return connection recommendations.

    FLIR cameras do not have an onboard memory buffer, so this function
    performs a read-only probe phase only: it inspects the host NIC
    carrying the camera link (speed, USB vs. PCIe, MTU) and returns
    recommended ``start_stream`` kwargs.

    Parameters
    ----------
    cam : Camera
        A connected FLIR camera.
    probe_only : bool, optional
        Ignored (always ``True`` for pyFlir; kept for API parity with
        pyTelops). Reserved for a future live-stream sweep.
    read_nic_info : bool, optional
        Read (never change) host NIC facts to annotate the report.
        Requires ``psutil``. Default ``False``.

    Returns
    -------
    ConnectionReport
    """
    warnings: list[str] = list(_preflight_warnings(cam))
    probe = ConnectionStats()

    if read_nic_info:
        _read_nic_info(cam, probe, warnings)

    # MTU-safe default for 1 GbE without jumbo frames. A live jumbo-frame
    # probe could raise this; nothing populates probe.max_packet_size today.
    recommended: dict = {"packet_size": 1500}

    return ConnectionReport(recommended=recommended, probe=probe, warnings=warnings)


def _link_local_warning(interfaces: list[tuple[str, str]]) -> str | None:
    """Return a warning when the camera's link-local route is ambiguous."""
    link_local = [(n, ip) for n, ip in interfaces if ip.startswith("169.254.")]
    if not link_local:
        return None
    vpn = [n for n, _ in interfaces if any(k in n.lower() for k in _VPN_NAME_KEYS)]
    if len(link_local) > 1 or vpn:
        names = ", ".join(sorted({n for n, _ in link_local} | set(vpn)))
        return (
            f"Multiple link-local / VPN adapters are up ({names}); a VPN adapter "
            f"can take over the camera route. If discovery or streaming misbehaves, "
            f"stop it or pass an explicit interface_ip for the camera NIC."
        )
    return None


def _is_usb_adapter(name: str, description: str = "") -> bool:
    text = f"{name} {description or ''}".lower()
    return any(k in text for k in _USB_ADAPTER_KEYS)


def _host_interfaces() -> list[tuple[str, str]]:
    try:
        import psutil
        out = []
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    out.append((name, a.address))
        return out
    except Exception:
        return []


def _adapter_descriptions() -> dict[str, str]:
    import sys
    if not sys.platform.startswith("win"):
        return {}
    try:
        import json
        import subprocess
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetAdapter | Select-Object Name,InterfaceDescription | ConvertTo-Json"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(proc.stdout or "[]")
        if isinstance(data, dict):
            data = [data]
        return {d.get("Name", ""): d.get("InterfaceDescription", "") for d in data}
    except Exception:
        return {}


def _preflight_warnings(cam) -> list[str]:
    out: list[str] = []
    w = _link_local_warning(_host_interfaces())
    if w:
        out.append(w)
    return out


def _read_nic_info(cam, probe: ConnectionStats, warnings: list[str]) -> None:
    try:
        import psutil
    except Exception:
        warnings.append("read_nic_info=True but psutil is not installed; skipping NIC facts.")
        return
    try:
        local_ip = getattr(cam, "interface_ip", "") or getattr(cam, "_local_ip", "") or ""
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        descriptions = _adapter_descriptions()

        target = None
        for name, alist in addrs.items():
            if any(a.family == socket.AF_INET and a.address == local_ip for a in alist):
                target = name
                break
        if target is None:
            for name, st in stats.items():
                if st.isup and getattr(st, "speed", 0):
                    target = name
                    break
        if target is None:
            return

        st = stats.get(target)
        desc = descriptions.get(target, "")
        probe.adapter_name = target
        probe.link_speed_mbps = getattr(st, "speed", None) if st else None
        probe.is_usb_adapter = _is_usb_adapter(target, desc)
        if probe.is_usb_adapter:
            label = f"NIC '{target}'" + (f" ({desc})" if desc else "")
            warnings.append(
                f"{label} looks like a USB adapter; USB-to-GigE adapters typically "
                f"deliver half the throughput of a native PCIe NIC and may exhibit "
                f"packet drops at high frame rates."
            )
    except Exception:
        pass
