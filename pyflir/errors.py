"""Typed result objects for pyFlir diagnostics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ConnectionStats:
    """Read-only facts gathered from a :func:`tune_connection` probe.

    Populated by :func:`pyflir.connection.tune_connection` and returned
    inside :class:`ConnectionReport`.
    """

    max_packet_size: int | None = None
    link_speed_mbps: int | None = None
    adapter_name: str | None = None
    is_usb_adapter: bool | None = None
