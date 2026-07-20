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


class TestNucAndFlag:
    """Unit tests for get_nuc_status and the NUC flag."""

    def test_get_nuc_status_reads_correction_name(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_mem.return_value = b"FactoryDefault" + b"\x00" * (256 - 14)
        status = cam.get_nuc_status(preset=0)
        assert status == {"name": "FactoryDefault"}
        cam._gvcp.read_mem.assert_called_once_with(reg.REG_CORRECTION_NAME[0], 256)

    def test_get_nuc_status_invalid_preset_raises(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(CameraError, match="preset"):
            cam.get_nuc_status(preset=5)

    def test_has_flag_true(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 1
        assert cam.has_flag() is True

    def test_has_flag_false(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0
        assert cam.has_flag() is False

    def test_get_flag_state_stowed(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda addr: 1 if addr == reg.REG_FLAG_PRESENT else 0
        assert cam.get_flag_state() == "Stowed"

    def test_get_flag_state_in_fov(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.side_effect = lambda addr: (
            1 if addr in (reg.REG_FLAG_PRESENT, reg.REG_FLAG_STATE) else 0
        )
        assert cam.get_flag_state() == "InFOV"

    def test_get_flag_state_no_flag_raises(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0
        with pytest.raises(CameraError, match="no NUC flag"):
            cam.get_flag_state()


class TestCorrectionPerform:
    """Unit tests for the CorrectionPerform state machine (correction_start,
    get_correction_status/result, correction_accept/discard/abort, and the
    high-level perform_nuc())."""

    def test_get_correction_status_reads_enum_and_text(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 4  # CollectingFirstSource
        cam._gvcp.read_mem.return_value = b"collecting..." + b"\x00" * (256 - 13)
        status = cam.get_correction_status()
        assert status == {"status": "CollectingFirstSource", "text": "collecting..."}

    def test_get_correction_result_reads_enum_and_text(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0  # Okay
        cam._gvcp.read_mem.return_value = b"done" + b"\x00" * (256 - 4)
        result = cam.get_correction_result()
        assert result == {"result": "Okay", "text": "done"}

    def test_correction_start_writes_type_source_presets_and_start(self):
        cam = _make_fake_connected_camera()
        cam.correction_start(preset=2, correction_type="TwoPoint", source="External")

        calls = {
            addr: value for addr, value in (c.args for c in cam._gvcp.write_reg.call_args_list)
        }
        assert calls[reg.REG_CORRECTION_TYPE] == 1  # TwoPoint
        assert calls[reg.REG_CORRECTION_SOURCE] == 0  # External
        assert calls[reg.REG_CORRECTION_PS[0]] == 0
        assert calls[reg.REG_CORRECTION_PS[1]] == 0
        assert calls[reg.REG_CORRECTION_PS[2]] == 1
        assert calls[reg.REG_CORRECTION_PS[3]] == 0
        assert calls[reg.REG_CORRECTION_START] == 1

    def test_correction_start_invalid_preset_raises(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(CameraError, match="preset"):
            cam.correction_start(preset=9)

    def test_correction_start_invalid_type_raises(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(CameraError, match="correction_type"):
            cam.correction_start(correction_type="ThreePoint")

    def test_correction_start_invalid_source_raises(self):
        cam = _make_fake_connected_camera()
        with pytest.raises(CameraError, match="source"):
            cam.correction_start(source="Sideways")

    def test_correction_continue_writes_register(self):
        cam = _make_fake_connected_camera()
        cam.correction_continue()
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_CORRECTION_CONTINUE, 1)

    def test_correction_accept_writes_register(self):
        cam = _make_fake_connected_camera()
        cam.correction_accept()
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_CORRECTION_ACCEPT, 1)

    def test_correction_discard_writes_register(self):
        cam = _make_fake_connected_camera()
        cam.correction_discard()
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_CORRECTION_DISCARD, 1)

    def test_correction_abort_writes_register(self):
        cam = _make_fake_connected_camera()
        cam.correction_abort()
        cam._gvcp.write_reg.assert_called_once_with(reg.REG_CORRECTION_ABORT, 1)


class TestPerformNuc:
    """Unit tests for the high-level perform_nuc() convenience method."""

    def _cam_with_status_sequence(self, statuses, result=0, result_text="Okay"):
        """Return a fake connected camera whose CorrectionStatus reads walk
        through *statuses* (list of status enum ints) then hold on the last
        one forever; CorrectionResult/ResultText are fixed."""
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 1  # has_flag() -> True by default

        status_iter = iter(statuses)
        remaining = {"status": None}

        def fake_read_reg(addr):
            if addr == reg.REG_FLAG_PRESENT:
                return 1
            if addr == reg.REG_CORRECTION_STATUS:
                remaining["status"] = next(status_iter, remaining["status"])
                return remaining["status"]
            if addr == reg.REG_CORRECTION_RESULT:
                return result
            return 0

        cam._gvcp.read_reg.side_effect = fake_read_reg

        def fake_read_mem(addr, length):
            if addr == reg.REG_CORRECTION_RESULT_TEXT:
                return result_text.encode("ascii") + b"\x00" * (256 - len(result_text))
            return b"\x00" * length

        cam._gvcp.read_mem.side_effect = fake_read_mem
        return cam

    def test_happy_path_auto_accept(self):
        # Starting(13) -> WaitingForFirstSourceInternal(3) -> CollectingFirstSource(4) -> Ready(0)
        cam = self._cam_with_status_sequence([13, 3, 4, 0], result=0, result_text="Okay")
        with patch("pyflir.camera.time.sleep"):
            result = cam.perform_nuc(preset=0, timeout=5.0, poll_interval=0.0)

        assert result == {"result": "Okay", "text": "Okay"}
        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_SOURCE, 1) in write_calls  # Internal
        assert (reg.REG_CORRECTION_START, 1) in write_calls
        assert (reg.REG_CORRECTION_ACCEPT, 1) in write_calls

    def test_auto_accept_false_does_not_accept(self):
        cam = self._cam_with_status_sequence([0], result=0, result_text="Okay")
        with patch("pyflir.camera.time.sleep"):
            result = cam.perform_nuc(timeout=5.0, poll_interval=0.0, auto_accept=False)

        assert result["result"] == "Okay"
        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_ACCEPT, 1) not in write_calls

    def test_no_flag_raises_before_starting(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 0  # has_flag() -> False
        with pytest.raises(CameraError, match="no NUC flag"):
            cam.perform_nuc()
        cam._gvcp.write_reg.assert_not_called()

    def test_two_point_rejected(self):
        cam = _make_fake_connected_camera()
        cam._gvcp.read_reg.return_value = 1  # has_flag() -> True
        with pytest.raises(CameraError, match="TwoPoint"):
            cam.perform_nuc(correction_type="TwoPoint")
        cam._gvcp.write_reg.assert_not_called()

    def test_non_okay_result_raises_and_does_not_accept(self):
        cam = self._cam_with_status_sequence([0], result=1, result_text="user cancelled")
        with patch("pyflir.camera.time.sleep"), pytest.raises(CameraError, match="Abort"):
            cam.perform_nuc(timeout=5.0, poll_interval=0.0)

        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_ACCEPT, 1) not in write_calls

    def test_unexpected_external_wait_aborts_and_raises(self):
        # Camera asks for an external source despite source=Internal being requested.
        cam = self._cam_with_status_sequence([2])  # WaitingForFirstSourceExternal
        with patch("pyflir.camera.time.sleep"), pytest.raises(CameraError, match="external"):
            cam.perform_nuc(timeout=5.0, poll_interval=0.0)

        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_ABORT, 1) in write_calls

    def test_timeout_aborts_and_raises(self):
        # Status never reaches Ready.
        cam = self._cam_with_status_sequence([13, 3, 4])  # holds on CollectingFirstSource
        with patch("pyflir.camera.time.sleep"), pytest.raises(CameraError, match="timed out"):
            cam.perform_nuc(timeout=0.02, poll_interval=0.0)

        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_CORRECTION_ABORT, 1) in write_calls


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
            res = cam.load_calibration(tag="TAG")
        mock_ws.assert_called_once_with(reg.REG_PS_CALIBRATION_LOAD_TAG[0], "TAG")
        write_calls = [c.args for c in cam._gvcp.write_reg.call_args_list]
        assert (reg.REG_PS_CALIBRATION_LOAD[0], 1) in write_calls
        assert res == {"preset": 0, "tag": "TAG", "exposure_ms": 2.354}

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
