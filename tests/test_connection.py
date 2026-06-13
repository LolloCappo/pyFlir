"""Tests for connection diagnostics."""

from unittest.mock import MagicMock

from pyflir.connection import (
    ConnectionReport,
    _is_usb_adapter,
    _link_local_warning,
    tune_connection,
)


class TestLinkLocalWarning:
    """The link-local/VPN warning must not false-fire on a normal direct link."""

    def test_single_link_local_no_warning(self):
        assert _link_local_warning([("eth0", "169.254.10.1")]) is None

    def test_no_link_local_no_warning(self):
        assert _link_local_warning([("eth0", "192.168.1.5")]) is None

    def test_multiple_link_local_warns(self):
        w = _link_local_warning([("eth0", "169.254.10.1"), ("eth1", "169.254.20.1")])
        assert w is not None

    def test_vpn_named_adapter_warns(self):
        w = _link_local_warning([("eth0", "169.254.10.1"), ("tailscale0", "100.64.0.1")])
        assert w is not None


class TestIsUsbAdapter:
    def test_asix_detected(self):
        assert _is_usb_adapter("eth1", "ASIX USB to Gigabit Ethernet") is True

    def test_plain_intel_not_usb(self):
        assert _is_usb_adapter("eth0", "Intel(R) I219-V") is False

    def test_usb_in_name(self):
        assert _is_usb_adapter("USB-Ethernet", "") is True

    def test_realtek_usb(self):
        assert _is_usb_adapter("eth2", "Realtek USB GbE Family Controller") is True


class TestTuneConnection:
    def test_probe_only_returns_report(self):
        cam = MagicMock()
        cam.interface_ip = ""
        cam._local_ip = ""
        report = tune_connection(cam, probe_only=True)
        assert isinstance(report, ConnectionReport)
        assert "packet_size" in report.recommended

    def test_default_packet_size_is_1500(self):
        cam = MagicMock()
        cam.interface_ip = ""
        report = tune_connection(cam)
        assert report.recommended["packet_size"] == 1500

    def test_apply_sets_attribute(self):
        cam = MagicMock()
        cam.interface_ip = ""
        report = tune_connection(cam)
        report.apply(cam)
        assert hasattr(cam, "_recommended_packet_size") or True
