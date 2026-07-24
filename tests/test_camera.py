"""Tests for Camera class.

Unit tests use mocking. Hardware tests require --hardware flag.
"""

import socket
import struct
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from pyGigEVision import GVCPError

from pyflir import registers as reg
from pyflir.camera import Camera, CameraError, _find_matching_interface, discover
from pyflir.genicam import RegNode


def _snic(address, netmask, family=socket.AF_INET):
    return types.SimpleNamespace(family=family, address=address, netmask=netmask)


def _ifstats(isup=True):
    return types.SimpleNamespace(isup=isup, duplex=0, speed=1000, mtu=1500, flags="")


def _make_fake_connected_camera():
    """Return a Camera wired with mock GVCP/GVSP, bypassing the network."""
    cam = Camera()
    cam._gvcp = MagicMock()
    cam._gvcp.read_reg.return_value = 0
    cam._gvsp = MagicMock()
    cam._gvsp.get_frame.return_value = None
    cam._gvsp.port = 3957
    cam._gvsp._sock.getsockname.return_value = ("169.254.1.1", 3957)
    cam.width = 320
    cam.height = 256
    cam.ip = "169.254.1.1"
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

        with (
            patch.object(
                cam, "start_stream", side_effect=lambda **kw: setattr(cam, "_streaming", True)
            ),
            patch.object(cam, "stop_stream", side_effect=lambda: setattr(cam, "_streaming", False)),
        ):
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
        cam.width = None
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


class TestSetRoi:
    """Unit tests for set_roi(), in particular the height increment check.

    Regression coverage for a live bug found on the real A6751sc:
    get_roi_limits()'s height_min is clamped to a floor of 1 for sensible
    display (max(1, h_min_raw - metadata_rows)), but set_roi() used to reuse
    that clamped value in a precise modular-arithmetic check, which silently
    corrupted the math whenever the true unclamped baseline was <= 0 --
    exactly this camera's case (h_min_raw=1, metadata_rows=1). It wrongly
    rejected height=512 (raw 513), a value every capture that session had
    already proven valid.
    """

    def _cam_with_roi_nodes(self, height_min_raw=1, height_inc=4, width_min=16, width_inc=16):
        cam = _make_fake_connected_camera()
        cam._nodes = {
            "Width": RegNode(name="Width", node_type="IntReg", address=0x1000),
            "Height": RegNode(name="Height", node_type="IntReg", address=0x1004),
        }
        cam._aliases = {}
        cam._metadata_rows = 1

        registers = {
            reg.REG_WIDTH_MIN: width_min,
            reg.REG_WIDTH_INC: width_inc,
            reg.REG_HEIGHT_MIN: height_min_raw,
            reg.REG_HEIGHT_INC: height_inc,
            0x1000: 640,  # Width readback after write
            0x1004: 513,  # Height readback after write (raw, includes metadata row)
        }
        cam._gvcp.read_reg.side_effect = lambda addr: registers.get(addr, 0)
        return cam

    def test_accepts_previously_valid_height_despite_clamped_h_min(self):
        # h_min_raw=1, metadata_rows=1 -> unclamped baseline (1-1)=0, but
        # get_roi_limits() reports height_min=max(1,0)=1. The old formula
        # checked (512 - 1) % 4 == 0 -> False -> wrongly rejected.
        # The fix checks the raw domain instead: (513 - 1) % 4 == 0 -> True.
        cam = self._cam_with_roi_nodes(height_min_raw=1, height_inc=4)
        cam.set_roi(640, 512, 0, 0)  # must not raise

        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (0x1004, 513) in write_calls  # Height written raw = 512 + 1 metadata row

    def test_rejects_height_violating_raw_increment(self):
        cam = self._cam_with_roi_nodes(height_min_raw=1, height_inc=4)
        # height=511 -> raw=512 -> (512 - 1) % 4 == 3 != 0 -> must raise
        with pytest.raises(CameraError, match="increment"):
            cam.set_roi(640, 511, 0, 0)

    def test_rejects_width_violating_increment(self):
        cam = self._cam_with_roi_nodes()
        with pytest.raises(CameraError, match="increment"):
            cam.set_roi(641, 512, 0, 0)

    def test_rejects_height_below_minimum(self):
        cam = self._cam_with_roi_nodes()
        with pytest.raises(CameraError, match="minimum"):
            cam.set_roi(640, 0, 0, 0)

    def test_raises_while_streaming(self):
        cam = self._cam_with_roi_nodes()
        cam._streaming = True
        with pytest.raises(CameraError, match="Stop the stream"):
            cam.set_roi(640, 512, 0, 0)


class TestTemperatureSensors:
    """Unit tests for get_temperatures()/detector_temperature() resilience.

    Regression coverage for a live bug: after TEMP_SENSORS grew from 4 to
    9 entries (from the camera's DeviceTemperatureSelector enum), one of
    the new indices raised GVCPError: GENERIC_ERROR on the real A6751sc --
    not every enum entry is necessarily populated on a given unit. That one
    failure used to take down the whole get_temperatures() call, and with
    it detector_temperature (which only ever wanted FPA).
    """

    def test_get_temperatures_skips_failing_sensor(self):
        cam = _make_fake_connected_camera()

        def fake_read_float(addr):
            # Fail for whichever sensor is currently selected if it's index 8 (Flag).
            selected = cam._gvcp.write_reg.call_args_list[-1].args[1]
            if selected == reg.TEMP_SENSORS["Flag"]:
                raise GVCPError("Command 0x0080 failed", 0x8FFF)
            return 20.0 + selected

        cam._gvcp.read_float.side_effect = fake_read_float
        temps = cam.get_temperatures()

        assert "Flag" not in temps
        assert temps["FPA"] == 20.0 + reg.TEMP_SENSORS["FPA"]
        assert len(temps) == len(reg.TEMP_SENSORS) - 1

    def test_get_temperatures_restores_selector_even_if_a_sensor_fails(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 42  # original selector value
        cam._gvcp.read_float.side_effect = GVCPError("boom", 0x8FFF)

        cam.get_temperatures()

        last_write = cam._gvcp.write_reg.call_args_list[-1]
        assert last_write.args == (reg.REG_TEMP_SELECTOR, 42)

    def test_detector_temperature_unaffected_by_other_sensor_failures(self):
        cam = _make_fake_connected_camera()

        def fake_read_float(addr):
            # Only FPA (index 0) ever gets selected by detector_temperature;
            # any other index would raise, proving independence from
            # get_temperatures()'s full sweep.
            selected = cam._gvcp.write_reg.call_args_list[-1].args[1]
            if selected != reg.TEMP_SENSORS["FPA"]:
                raise GVCPError("should not be selected", 0x8FFF)
            return -199.2

        cam._gvcp.read_float.side_effect = fake_read_float
        assert cam.detector_temperature == -199.2

    def test_detector_temperature_propagates_fpa_failure(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.side_effect = GVCPError("FPA sensor down", 0x8FFF)
        with pytest.raises(GVCPError):
            _ = cam.detector_temperature


class TestLoadCalibration:
    """Unit tests for load_calibration(), _write_string_reg(), and the
    active-calibration tag matching used by get_calibration_block()."""

    def test_write_string_reg_little_endian_and_padded(self):
        cam = _make_fake_connected_camera()
        cam._write_string_reg(0x1000, "AB", length=8)
        calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert len(calls) == 2  # 8 bytes / 4
        assert calls[0] == (0x1000, struct.unpack("<I", b"AB\x00\x00")[0])
        assert calls[1] == (0x1004, 0)  # NUL padding

    def test_load_calibration_requires_exactly_one_selector(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(CameraError):
            cam.load_calibration()
        with pytest.raises(CameraError):
            cam.load_calibration(tag="x", index=0)

    def test_load_calibration_by_tag_stages_executes_and_returns(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0  # ActivePreset 0
        with (
            patch.object(cam, "_write_string_reg") as mock_ws,
            patch.object(cam, "_read_string", return_value="TAG"),
            patch.object(cam, "read_float", return_value=2.354),
        ):
            res = cam.load_calibration(tag="TAG", nuc=False)
        mock_ws.assert_called_once_with(reg.REG_PS_CALIBRATION_LOAD_TAG[0], "TAG")
        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_PS_CALIBRATION_LOAD[0], 1) in write_calls
        assert res == {"preset": 0, "tag": "TAG", "exposure_ms": 2.354, "nuc": None}

    def test_load_calibration_runs_nuc_by_default(self):
        """Loading changes the integration time, so the offset must be
        re-levelled afterwards -- that is what the vendor software does on
        range select, and skipping it is what made block switches read wrong."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0
        with (
            patch.object(cam, "_write_string_reg"),
            patch.object(cam, "_read_string", return_value="TAG"),
            patch.object(cam, "read_float", return_value=2.354),
            patch.object(cam, "nuc", return_value={"duration_s": 5.0}) as mock_nuc,
        ):
            res = cam.load_calibration(tag="TAG")
        mock_nuc.assert_called_once()
        assert res["nuc"] == {"duration_s": 5.0}

    def test_load_calibration_survives_failing_nuc(self):
        """A NUC failure still leaves a valid calibration loaded, so report it
        rather than discarding a successful load."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0
        with (
            patch.object(cam, "_write_string_reg"),
            patch.object(cam, "_read_string", return_value="TAG"),
            patch.object(cam, "read_float", return_value=2.354),
            patch.object(cam, "nuc", side_effect=CameraError("no flag")),
        ):
            res = cam.load_calibration(tag="TAG")
        assert res["tag"] == "TAG"
        assert res["nuc"] is None

    def test_load_calibration_by_index_resolves_and_restores_cursor(self):
        cam = _make_fake_connected_camera()

        def read_reg(addr):
            return {
                reg.REG_ACTIVE_PRESET: 0,
                reg.REG_CAL_INDEX_MAX: 9,
                reg.REG_CAL_INDEX: 5,
            }.get(addr, 0)

        cam._gvcp.read_reg.side_effect = read_reg
        with (
            patch.object(cam, "_write_string_reg") as mock_ws,
            patch.object(cam, "_read_string", return_value="BLOCK3TAG"),
            patch.object(cam, "read_float", return_value=1.0),
        ):
            res = cam.load_calibration(index=3)
        mock_ws.assert_called_once_with(reg.REG_PS_CALIBRATION_LOAD_TAG[0], "BLOCK3TAG")
        writes = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CAL_INDEX, 3) in writes  # moved cursor to read tag
        assert (reg.REG_CAL_INDEX, 5) in writes  # restored original cursor
        assert res["tag"] == "BLOCK3TAG"

    def test_load_calibration_index_out_of_range_raises(self):
        cam = _make_fake_connected_camera()

        def read_reg(addr):
            return {reg.REG_ACTIVE_PRESET: 0, reg.REG_CAL_INDEX_MAX: 9}.get(addr, 0)

        cam._gvcp.read_reg.side_effect = read_reg
        with pytest.raises(CameraError, match="out of range"):
            cam.load_calibration(index=10)

    def test_load_calibration_staging_mismatch_raises(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0
        with (
            patch.object(cam, "_write_string_reg"),
            patch.object(cam, "_read_string", return_value="WRONG"),
            pytest.raises(CameraError, match="stage"),
        ):
            cam.load_calibration(tag="TAG")

    def test_set_calibration_block_delegates_to_load(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "load_calibration") as mock_load:
            cam.set_calibration_block(2)
        mock_load.assert_called_once_with(index=2)

    def test_get_calibration_block_returns_index_matching_loaded_tag(self):
        cam = _make_fake_connected_camera()

        def read_reg(addr):
            return {
                reg.REG_ACTIVE_PRESET: 0,
                reg.REG_CAL_INDEX_MAX: 2,
                reg.REG_CAL_INDEX: 0,
            }.get(addr, 0)

        cam._gvcp.read_reg.side_effect = read_reg
        block_tags = {0: "T0", 1: "T1", 2: "T2"}

        def read_string(addr, length=256):
            if addr == reg.REG_PS_CALIBRATION_TAG[0]:
                return "T1"  # loaded calibration is block 1's tag
            # REG_CAL_TAG reflects wherever the browse cursor was last written.
            cursor = 0
            for c in reversed(cam._gvcp.write_reg.call_args_list):
                if c.args[0] == reg.REG_CAL_INDEX:
                    cursor = c.args[1]
                    break
            return block_tags.get(cursor, "")

        with patch.object(cam, "_read_string", side_effect=read_string):
            assert cam.get_calibration_block() == 1


class TestPixelFormatDivisor:
    """The count divisor adapts to the transport pixel format (14-bit sensor)."""

    def test_mono16_divisor_is_4(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_enum", return_value="Mono16"):
            assert cam._count_divisor() == 4.0

    def test_mono14_divisor_is_1(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_enum", return_value="Mono14"):
            assert cam._count_divisor() == 1.0

    def test_divisor_defaults_to_4_on_error(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_enum", side_effect=RuntimeError):
            assert cam._count_divisor() == 4.0

    def test_data_overrides_a_lying_pixel_format_register(self):
        # The camera accepts PixelFormat=Mono14 while still streaming Mono16.
        # A true 14-bit stream cannot exceed 16383, so data above that must be
        # Mono16 (divisor 4) regardless of what the register claims -- otherwise
        # counts go in 4x too high and every pixel pins at the block's tmax.
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_enum", return_value="Mono14"):
            mono16 = np.full((4, 4), 48919, dtype=np.uint16)
            assert cam._count_divisor(mono16) == 4.0
            real14 = np.full((4, 4), 8645, dtype=np.uint16)
            assert cam._count_divisor(real14) == 1.0

    def test_apply_calibration_respects_divisor(self):
        cal = {
            "counts_min": 1985,
            "counts_max": 13618,
            "tmin": 10.0,
            "tmax": 90.0,
            "counts_background": 0.0,
            "counts_coeffs": [-8.19e-05, 9.29e-08, 4.46e-13],
            "temp_coeffs": [-21.72, 400064, -1.0e9, 1.6e12, -1.43e15, 6.57e17, -1.2e20],
        }
        frame = np.full((4, 4), 8000, dtype=np.uint16)
        from pyflir import apply_calibration

        # Mono16: 8000/4 = 2000 counts -> near tmin; Mono14: 8000 counts -> hot.
        t16 = float(np.median(apply_calibration(frame, cal, count_divisor=4.0)))
        t14 = float(np.median(apply_calibration(frame, cal, count_divisor=1.0)))
        assert t16 < 20.0
        assert t14 > 60.0


class TestPropertiesWithMock:
    """Test property getters/setters via mocked read_float/write_float."""

    def test_frame_rate_setter_raises_above_max(self):
        cam = _make_fake_connected_camera()

        with (
            patch.object(cam, "get_max_frame_rate", return_value=50.0),
            patch.object(cam, "write_float") as mock_write,
            pytest.raises(CameraError, match="[Mm]ax|[Ee]xceeds"),
        ):
            cam.frame_rate = 200.0
        mock_write.assert_not_called()

    def test_exposure_ms_property_conversion(self):
        # The backing register (PS0IntegrationTime) is already in ms, so
        # exposure_ms is a 1:1 passthrough (no seconds->ms scaling).
        cam = _make_fake_connected_camera()
        with patch.object(cam, "read_float", return_value=8.0):
            assert abs(cam.exposure_ms - 8.0) < 1e-6

    def test_exposure_ms_setter_conversion(self):
        cam = _make_fake_connected_camera()
        with patch.object(cam, "write_float") as mock_write:
            # The setter warns that changing integration time desyncs the
            # loaded calibration; that is expected here, just check the math.
            with pytest.warns(UserWarning, match="integration time"):
                cam.exposure_ms = 8.0
            mock_write.assert_called_once()
            feature, val = mock_write.call_args.args
            assert abs(val - 8.0) < 1e-9


class TestFindMatchingInterface:
    """Unit tests for _find_matching_interface().

    pyGigEVision's discover() dedupes replies for the same camera down to
    whichever local socket answered first -- not necessarily the interface
    on the camera's own subnet. This is pyflir's own workaround, re-deriving
    the correct interface by querying each local interface individually,
    without needing any change to pyGigEVision itself.
    """

    def test_prefers_same_subnet_interface(self):
        camera_ip = "169.254.9.9"
        addrs = {
            "eth0": [_snic("192.168.0.10", "255.255.255.0")],  # off-subnet
            "usb0": [_snic("169.254.1.5", "255.255.0.0")],  # same /16 subnet
        }
        stats = {"eth0": _ifstats(), "usb0": _ifstats()}

        def fake_discover(interface_ip="", timeout=1.0):
            return [{"ip": camera_ip}] if interface_ip == "169.254.1.5" else []

        with (
            patch("psutil.net_if_addrs", return_value=addrs),
            patch("psutil.net_if_stats", return_value=stats),
            patch("pyflir.camera.GVCPClient.discover", side_effect=fake_discover),
        ):
            result = _find_matching_interface(camera_ip)
        assert result == "169.254.1.5"

    def test_tries_same_subnet_candidate_first(self):
        camera_ip = "169.254.9.9"
        addrs = {
            "eth0": [_snic("192.168.0.10", "255.255.255.0")],  # off-subnet
            "usb0": [_snic("169.254.1.5", "255.255.0.0")],  # same /16 subnet
        }
        stats = {"eth0": _ifstats(), "usb0": _ifstats()}
        queried = []

        def fake_discover(interface_ip="", timeout=1.0):
            queried.append(interface_ip)
            return [{"ip": camera_ip}] if interface_ip == "169.254.1.5" else []

        with (
            patch("psutil.net_if_addrs", return_value=addrs),
            patch("psutil.net_if_stats", return_value=stats),
            patch("pyflir.camera.GVCPClient.discover", side_effect=fake_discover),
        ):
            _find_matching_interface(camera_ip)
        assert queried[0] == "169.254.1.5"

    def test_returns_empty_when_nothing_matches(self):
        with (
            patch("psutil.net_if_addrs", return_value={}),
            patch("psutil.net_if_stats", return_value={}),
        ):
            result = _find_matching_interface("169.254.9.9")
        assert result == ""

    def test_skips_down_interfaces(self):
        camera_ip = "169.254.9.9"
        addrs = {"down0": [_snic("169.254.1.5", "255.255.0.0")]}
        stats = {"down0": _ifstats(isup=False)}

        with (
            patch("psutil.net_if_addrs", return_value=addrs),
            patch("psutil.net_if_stats", return_value=stats),
            patch("pyflir.camera.GVCPClient.discover") as mock_discover,
        ):
            result = _find_matching_interface(camera_ip)
        mock_discover.assert_not_called()
        assert result == ""


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


class TestIntegrationTimeGuard:
    """A calibration is only valid at its own integration time."""

    def test_warns_when_integration_diverges_from_calibration(self):
        cam = _make_fake_connected_camera()
        cam._cal_integration_ms = 2.354
        with (
            patch.object(cam, "read_float", return_value=4.0),  # exposure now 4.0 ms
            patch.object(cam, "get_calibration", return_value={}),
            patch("pyflir.camera.apply_calibration", return_value=np.zeros((2, 2))),
            patch.object(cam, "get_object_params", side_effect=RuntimeError),
            pytest.warns(UserWarning, match="no longer matches"),
        ):
            cam.counts_to_temperature(np.zeros((2, 2), dtype=np.uint16))

    def test_no_warning_when_integration_matches(self):
        import warnings as _w

        cam = _make_fake_connected_camera()
        cam._cal_integration_ms = 2.354
        with (
            patch.object(cam, "read_float", return_value=2.354),
            patch.object(cam, "get_calibration", return_value={}),
            patch("pyflir.camera.apply_calibration", return_value=np.zeros((2, 2))),
            patch.object(cam, "get_object_params", side_effect=RuntimeError),
            _w.catch_warnings(record=True) as caught,
        ):
            _w.simplefilter("always")
            cam.counts_to_temperature(np.zeros((2, 2), dtype=np.uint16))
        assert not [x for x in caught if "no longer matches" in str(x.message)]


class TestToAdcCounts:
    """The 14-bit ADC domain is what the calibration is defined in."""

    def test_mono16_wire_value_divided_by_4(self):
        cam = _make_fake_connected_camera()
        frame = np.full((2, 2), 65304, dtype=np.uint16)  # wire, 16-bit
        with patch.object(cam, "read_enum", return_value="Mono16"):
            adc = cam.to_adc_counts(frame)
        assert np.allclose(adc, 16326.0)  # within 14-bit range
        assert adc.max() <= 16383

    def test_sub_count_precision_preserved(self):
        # Low bits carry sub-count precision, not padding -> keep the fraction.
        cam = _make_fake_connected_camera()
        frame = np.full((2, 2), 65305, dtype=np.uint16)
        with patch.object(cam, "read_enum", return_value="Mono16"):
            adc = cam.to_adc_counts(frame)
        assert np.allclose(adc, 16326.25)


class TestNuc:
    """Unit tests for nuc() and the automatic-NUC configuration.

    pyflir drives only the camera's *offset update* path
    (CorrectionAutoPerform). The lower-level CorrectionPerform state machine
    recomputes the stored gain coefficients and corrupts the image when run
    against the internal flag, so it is deliberately not exposed -- see the
    comment above REG_CORRECTION_AUTO_ENABLED in pyflir.registers.
    """

    def test_nuc_triggers_and_polls_to_completion(self):
        cam = _make_fake_connected_camera()
        busy = iter([1, 1, 1, 0])  # in-progress for three polls, then done

        def read_reg(addr):
            if addr == reg.REG_FLAG_PRESENT:
                return 1
            if addr == reg.REG_CORRECTION_AUTO_IN_PROGRESS:
                return next(busy, 0)
            if addr == reg.REG_FLAG_STATE:
                return 0  # Stowed
            return 0

        cam._gvcp.read_reg.side_effect = read_reg
        res = cam.nuc(settle=0)
        writes = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_AUTO_PERFORM, 1) in writes
        assert res["flag_state"] == "Stowed"
        assert res["duration_s"] >= 0

    def test_nuc_never_touches_the_destructive_correction_registers(self):
        """Regression guard: running a NUC must not write CorrectionStart,
        CorrectionType or CorrectionAccept -- those overwrite the factory gain
        terms and visibly corrupt the image."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda a: 1 if a == reg.REG_FLAG_PRESENT else 0
        with patch("pyflir.camera.NUC_START_GRACE_S", 0.1):
            cam.nuc(settle=0)
        written = {c.args[0] for c in cam._gvcp.write_reg.call_args_list}
        assert written.isdisjoint({0x4E060C00, 0x4E060C08, 0x4E060C14, 0x4E060C0C})
        assert not any(hasattr(cam, n) for n in ("perform_nuc", "correction_start"))

    def test_nuc_without_flag_raises(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0  # FlagPresent = 0
        with pytest.raises(CameraError, match="no NUC flag"):
            cam.nuc(settle=0)

    def test_nuc_times_out_if_never_finishes(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda a: 1  # always busy
        with pytest.raises(CameraError, match="did not complete"):
            cam.nuc(timeout=0.3, settle=0)

    def test_nuc_completes_when_busy_flag_never_latches(self):
        """The in-progress bit may not be observable for a very fast update;
        that must not hang the call."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda a: 1 if a == reg.REG_FLAG_PRESENT else 0
        with patch("pyflir.camera.NUC_START_GRACE_S", 0.2):
            res = cam.nuc(timeout=10, settle=0)
        assert res["duration_s"] < 10

    def test_configure_auto_nuc_sets_triggers_and_enables(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.return_value = 2.0
        cam.configure_auto_nuc(enabled=True, delta_temp=2.0, delta_time_min=15)
        writes = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_AUTO_USE_DELTA_TEMP, 1) in writes
        assert (reg.REG_CORRECTION_AUTO_DELTA_TIME, 15) in writes
        assert (reg.REG_CORRECTION_AUTO_USE_DELTA_TIME, 1) in writes
        assert (reg.REG_CORRECTION_AUTO_ENABLED, 1) in writes
        cam._gvcp.write_float.assert_called_once_with(reg.REG_CORRECTION_AUTO_DELTA_TEMP, 2.0)

    def test_configure_auto_nuc_leaves_untouched_triggers_alone(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_float.return_value = 2.0
        cam.configure_auto_nuc(enabled=False)
        writes = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_AUTO_ENABLED, 0) in writes
        assert not cam._gvcp.write_float.called
        touched = {a for a, _ in writes}
        assert reg.REG_CORRECTION_AUTO_USE_DELTA_TEMP not in touched
        assert reg.REG_CORRECTION_AUTO_USE_DELTA_TIME not in touched

    def test_get_auto_nuc_config_shape(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 1
        cam._gvcp.read_float.return_value = 2.5
        cfg = cam.get_auto_nuc_config()
        assert cfg["enabled"] is True
        assert cfg["delta_temp"] == 2.5
        assert set(cfg) == {
            "enabled",
            "use_delta_temp",
            "delta_temp",
            "use_delta_time",
            "delta_time_min",
            "in_progress",
        }


class TestByteOrder:
    """Pixel byte-order normalization.

    GigE Vision sends pixel data big-endian; read natively on a little-endian
    host every value comes out byte-swapped. Verified live on the A6751sc: in
    TemperatureLinear100mK the low byte was pinned at 0x0B while the high byte
    varied, and byte-swapping gave 23.9-24.9 C for a room-temperature scene.
    """

    @staticmethod
    def _scene(base=5500, spread=400, shape=(64, 64)):
        """A plausible 14-bit scene: fine detail in the low byte."""
        rng = np.random.default_rng(0)
        return (base + rng.integers(0, spread, shape)).astype(np.uint16)

    def test_detects_swapped_data(self):
        native = self._scene()
        assert not Camera._looks_byteswapped(native)
        assert Camera._looks_byteswapped(native.byteswap())

    def test_msb_aligned_data_is_not_mistaken_for_swapped(self):
        """A genuinely left-shifted 14-bit value still gives the low byte 64
        distinct values, well clear of the cutoff."""
        aligned = (self._scene() << 2).astype(np.uint16)
        assert not Camera._looks_byteswapped(aligned)

    def test_auto_swaps_and_caches_decision(self):
        cam = _make_fake_connected_camera()
        swapped = self._scene().byteswap()
        out = cam._normalize_byte_order(swapped)
        assert cam._byte_order_detected == "swapped"
        assert np.array_equal(out, swapped.byteswap())
        # A later uniform frame must not flip the cached decision.
        uniform = np.full((64, 64), 5500, dtype=np.uint16)
        cam._normalize_byte_order(uniform)
        assert cam._byte_order_detected == "swapped"

    def test_explicit_override_wins(self):
        cam = _make_fake_connected_camera()
        frame = self._scene().byteswap()
        cam.byte_order = "native"
        assert np.array_equal(cam._normalize_byte_order(frame), frame)
        cam.byte_order = "swapped"
        assert np.array_equal(cam._normalize_byte_order(frame), frame.byteswap())

    def test_reset_byte_order_forces_redetect(self):
        cam = _make_fake_connected_camera()
        cam._normalize_byte_order(self._scene())
        assert cam._byte_order_detected == "native"
        cam.reset_byte_order()
        assert cam._byte_order_detected is None

    def test_swapped_counts_fall_inside_14_bit_range(self):
        """The point of the fix: swapped junk exceeds 14 bits, corrected data
        does not -- which is also why the /4 'MSB alignment' divisor was only
        ever compensating for the swap."""
        native = self._scene()
        assert native.max() <= 16383
        assert native.byteswap().max() > 16383


class TestMetadataRowPosition:
    """The metadata row is not always trailing; stripping the wrong end keeps
    telemetry in the image and throws away a row of real pixels."""

    @staticmethod
    def _frame(leading):
        body = np.full((8, 16), 5000, dtype=np.uint16)
        meta = np.zeros((1, 16), dtype=np.uint16)
        meta[0, :3] = [7, 42, 99]  # sparse telemetry fields
        return np.vstack([meta, body] if leading else [body, meta])

    def test_strips_leading_metadata_row(self):
        cam = _make_fake_connected_camera()
        cam._metadata_rows = 1
        out = cam._strip_metadata(self._frame(leading=True))
        assert out.shape == (8, 16)
        assert (out == 5000).all()
        assert cam.last_metadata_rows[0, 1] == 42

    def test_strips_trailing_metadata_row(self):
        cam = _make_fake_connected_camera()
        cam._metadata_rows = 1
        out = cam._strip_metadata(self._frame(leading=False))
        assert out.shape == (8, 16)
        assert (out == 5000).all()
        assert cam.last_metadata_rows[0, 1] == 42

    def test_no_metadata_rows_is_passthrough(self):
        cam = _make_fake_connected_camera()
        cam._metadata_rows = 0
        f = np.ones((4, 4), dtype=np.uint16)
        assert np.array_equal(cam._strip_metadata(f), f)


class TestByteOrderRegressionFromHardware:
    """Locks in the actual A6751sc measurement that identified the defect.

    Pointed at a room-temperature scene in TemperatureLinear100mK, the raw
    1/50/99 percentiles were 0x9A0B / 0xA00B / 0xA40B -- low byte pinned at
    0x0B. Byte-swapped they are 2970 / 2976 / 2980, which decode to the correct
    room temperature. If this ever regresses, temperatures silently become
    meaningless again.
    """

    OBSERVED = np.array([0x9A0B, 0xA00B, 0xA40B], dtype=np.uint16)

    def test_observed_frame_is_detected_as_swapped(self):
        """A realistic TemperatureLinear frame: the scene spans only a few
        Kelvin, so the high byte holds just the 0x9A..0xA4 seen live while the
        low byte stays pinned at 0x0B."""
        rng = np.random.default_rng(0)
        hi = rng.integers(0x9A, 0xA5, (64, 64)).astype(np.uint16)
        frame = ((hi << 8) | 0x0B).astype(np.uint16)
        assert Camera._looks_byteswapped(frame)

    def test_uniform_frame_is_not_guessed_as_swapped(self):
        """Both bytes constant carries no evidence either way."""
        assert not Camera._looks_byteswapped(np.full((64, 64), 0xA00B, np.uint16))

    def test_swapped_values_decode_to_room_temperature(self):
        swapped = self.OBSERVED.byteswap().astype(np.float64)
        assert list(swapped) == [2970, 2976, 2980]
        celsius = swapped * reg.IR_FORMAT_KELVIN_PER_COUNT[1] - 273.15
        assert np.allclose(celsius, [23.85, 24.45, 24.85], atol=0.01)

    def test_uncorrected_values_decode_to_nonsense(self):
        celsius = self.OBSERVED.astype(np.float64) * 0.1 - 273.15
        assert celsius.min() > 3600  # ~3800 C, the symptom originally seen
