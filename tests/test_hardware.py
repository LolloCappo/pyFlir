"""
Hardware test suite for pyFlir.

Requires a connected FLIR GigE Vision camera. Run with:
    pytest tests/test_hardware.py --hardware -v
"""

import numpy as np
import pytest

from pyflir import Camera, discover


@pytest.fixture(scope="module")
def cam():
    """Single camera instance shared across all tests in this module."""
    camera = Camera()
    camera.connect()
    camera.download_xml()
    camera.load_xml(f"camera_{camera.serial or camera.ip.replace('.', '_')}.xml")
    yield camera
    camera.disconnect()


@pytest.mark.hardware
class TestDiscovery:
    def test_discover_finds_camera(self):
        cameras = discover(timeout=3.0)
        assert len(cameras) > 0
        assert cameras[0]["ip"]

    def test_connected(self, cam):
        assert cam.is_connected


@pytest.mark.hardware
class TestProperties:
    def test_resolution(self, cam):
        w, h = cam.resolution
        assert w > 0 and h > 0

    def test_frame_rate_read(self, cam):
        fps = cam.frame_rate
        assert fps > 0

    def test_exposure_ms_read_write(self, cam):
        orig = cam.exposure_ms
        cam.exposure_ms = 10.0
        assert abs(cam.exposure_ms - 10.0) < 0.5
        cam.exposure_ms = orig

    def test_detector_temperature(self, cam):
        t = cam.detector_temperature
        assert isinstance(t, float)
        assert -50 < t < 200

    def test_info_dict(self, cam):
        info = cam.info()
        assert isinstance(info, dict)
        assert len(info) > 0


@pytest.mark.hardware
class TestStreaming:
    def test_grab_single_frame(self, cam):
        frame = cam.grab(timeout=10.0)
        assert frame is not None
        assert frame.ndim == 2
        assert frame.dtype == np.uint16
        w, h = cam.resolution
        assert frame.shape == (h, w)

    def test_grab_has_real_data(self, cam):
        frame = cam.grab(timeout=10.0)
        assert frame.std() > 0
        assert frame.max() > 0

    def test_acquire_multiple(self, cam):
        frames = cam.acquire(5, timeout=20.0)
        assert len(frames) == 5
        assert frames[0].ndim == 2

    def test_stream_start_stop_restart(self, cam):
        cam.start_stream()
        assert cam.is_streaming

        frame = cam.read(timeout=5.0)
        assert frame is not None

        cam.stop_stream()
        assert not cam.is_streaming

        cam.start_stream()
        assert cam.is_streaming
        frame = cam.read(timeout=5.0)
        assert frame is not None
        cam.stop_stream()


@pytest.mark.hardware
class TestCalibration:
    def test_get_calibration_blocks(self, cam):
        blocks = cam.get_calibration_blocks()
        assert isinstance(blocks, list)
        assert len(blocks) > 0
        assert "index" in blocks[0]
        assert "tmin" in blocks[0]

    def test_set_calibration_block(self, cam):
        orig = cam.get_calibration_block()
        cam.set_calibration_block(0)
        assert cam.get_calibration_block() == 0
        cam.set_calibration_block(orig)


@pytest.mark.hardware
class TestTemperatures:
    def test_get_temperatures(self, cam):
        temps = cam.get_temperatures()
        assert isinstance(temps, dict)
        assert len(temps) > 0
        for _name, t in temps.items():
            assert isinstance(t, float)


@pytest.mark.hardware
class TestROI:
    def test_get_roi(self, cam):
        roi = cam.get_roi()
        assert roi["width"] > 0
        assert roi["height"] > 0

    def test_get_roi_limits(self, cam):
        limits = cam.get_roi_limits()
        assert limits["sensor_width"] > 0
        assert limits["sensor_height"] > 0
