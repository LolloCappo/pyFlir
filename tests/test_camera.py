"""Tests for Camera class.

Unit tests use mocking. Hardware tests require --hardware flag.
"""

from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pyGigEVision import GVCPError

from pyflir.camera import Camera, CameraError, discover


def _make_fake_connected_camera():
    """Return a Camera wired with mock GVCP/GVSP, bypassing the network."""
    cam = Camera()
    cam._gvcp = MagicMock()
    cam._gvcp.read_reg.return_value = 0
    cam._gvsp = MagicMock()
    cam._gvsp.get_frame.return_value = None
    cam._gvsp.port = 3957
    cam._gvsp._sock.getsockname.return_value = ("169.254.1.1", 3957)
    cam.width  = 320
    cam.height = 256
    cam.ip     = "169.254.1.1"
    return cam


class TestCameraInit:
    """Test Camera construction (no network)."""

    def test_default_init(self):
        cam = Camera()
        assert cam.ip is None
        assert not cam.is_connected
        assert not cam.is_streaming

    def test_init_with_ip(self):
        cam = Camera(ip="169.254.1.1")
        assert cam.ip == "169.254.1.1"

    def test_repr_disconnected(self):
        cam = Camera(ip="169.254.1.1")
        assert "disconnected" in repr(cam)

    def test_not_connected_raises_on_grab(self):
        cam = Camera()
        with pytest.raises((RuntimeError, CameraError)):
            cam.grab()

    def test_not_connected_raises_on_read(self):
        cam = Camera()
        with pytest.raises((RuntimeError, CameraError)):
            cam.read()


class TestStreamingAPI:
    """Unit tests for streaming, grab, read, acquire."""

    def test_grab_returns_frame(self):
        cam = _make_fake_connected_camera()
        frame = np.zeros((256, 320), dtype=np.uint16)

        cam._gvsp = MagicMock()
        cam._gvsp.get_frame.return_value = frame
        cam._streaming = True

        with patch.object(cam, "start_stream", side_effect=lambda **kw: setattr(cam, "_streaming", True)):
            with patch.object(cam, "stop_stream", side_effect=lambda: setattr(cam, "_streaming", False)):
                result = cam.grab(timeout=1.0)

        assert result is not None
        assert result.shape == (256, 320)

    def test_read_raises_when_not_streaming(self):
        cam = _make_fake_connected_camera()
        cam._streaming = False
        cam._gvsp = None
        with pytest.raises(CameraError, match="[Nn]ot streaming"):
            cam.read()

    def test_read_latest_drains_queue(self):
        cam = _make_fake_connected_camera()
        cam._streaming = True
        frame1 = np.full((256, 320), 10, dtype=np.uint16)
        frame2 = np.full((256, 320), 20, dtype=np.uint16)
        frame3 = np.full((256, 320), 30, dtype=np.uint16)
        cam._gvsp.get_frame.side_effect = [frame1, frame2, frame3, None]

        result = cam.read(latest=True, timeout=0.0)
        assert (result == 30).all()

    def test_acquire_returns_list(self):
        cam = _make_fake_connected_camera()
        frame = np.zeros((256, 320), dtype=np.uint16)
        cam._gvsp.get_frame.return_value = frame
        cam._streaming = True

        results = cam.acquire(3, timeout=5.0)
        assert len(results) == 3

    def test_start_stream_requires_xml(self):
        cam = _make_fake_connected_camera()
        cam.width  = None
        cam.height = None
        with pytest.raises(CameraError, match="[Ll]oad_xml|[Dd]imensions"):
            cam.start_stream()

    def test_stop_stream_is_idempotent(self):
        cam = _make_fake_connected_camera()
        cam._streaming = False
        cam.stop_stream()  # should not raise


class TestFeatureAccess:
    """Unit tests for read_int, write_int, read_float, write_float."""

    def test_read_int_unknown_feature_raises(self):
        cam = _make_fake_connected_camera()
        cam._nodes = {}
        with pytest.raises((KeyError, CameraError)):
            cam.read_int("NonExistentFeature")

    def test_frame_rate_max_returns_none_on_error(self):
        cam = _make_fake_connected_camera()
        cam._nodes = {}
        result = cam.get_max_frame_rate()
        assert result is None


class TestPropertiesWithMock:
    """Test property getters/setters via mocked read_float/write_float."""

    def test_frame_rate_setter_raises_above_max(self):
        cam = _make_fake_connected_camera()

        with patch.object(cam, "get_max_frame_rate", return_value=50.0):
            with patch.object(cam, "write_float") as mock_write:
                with pytest.raises(CameraError, match="[Mm]ax|[Ee]xceeds"):
                    cam.frame_rate = 200.0
                mock_write.assert_not_called()

    def test_exposure_ms_property_conversion(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_float", return_value=0.008):
            assert abs(cam.exposure_ms - 8.0) < 1e-6

    def test_exposure_ms_setter_conversion(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "write_float") as mock_write:
            cam.exposure_ms = 8.0
            mock_write.assert_called_once()
            feature, val = mock_write.call_args.args
            assert abs(val - 0.008) < 1e-9


class TestDiscover:
    """Test discover() function."""

    @patch("pyflir.camera.GVCPClient")
    def test_discover_returns_list(self, mock_gvcp_cls):
        mock_gvcp_cls.discover.return_value = [
            {"ip": "169.254.10.1", "manufacturer": "FLIR Systems", "model": "A50"}
        ]
        cameras = discover()
        assert isinstance(cameras, list)

    @patch("pyflir.camera.GVCPClient")
    def test_discover_empty(self, mock_gvcp_cls):
        mock_gvcp_cls.discover.return_value = []
        cameras = discover()
        assert cameras == []


# ============================================================
# Hardware tests (skipped without --hardware flag)
# ============================================================


@pytest.mark.hardware
class TestHardwareGrab:
    """Basic hardware smoke tests."""

    @pytest.fixture(scope="class")
    def cam(self):
        c = Camera()
        c.connect()
        c.download_xml()
        c.load_xml(f"camera_{c.serial or c.ip.replace('.', '_')}.xml")
        yield c
        c.disconnect()

    def test_is_connected(self, cam):
        assert cam.is_connected

    def test_grab_returns_array(self, cam):
        frame = cam.grab(timeout=10.0)
        assert frame is not None
        assert frame.ndim == 2
        assert frame.dtype == np.uint16

    def test_grab_has_real_data(self, cam):
        frame = cam.grab(timeout=10.0)
        assert frame.std() > 0

    def test_acquire_multiple(self, cam):
        frames = cam.acquire(5, timeout=15.0)
        assert len(frames) == 5
        assert all(f.shape == frames[0].shape for f in frames)
