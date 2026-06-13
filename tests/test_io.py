"""Tests for pyflir.io: ATS reader and calibration formula."""

import numpy as np
import pytest

from pyflir.io import ATSMetadata, FLIRATSReader, read_ats

# ---------------------------------------------------------------------------
# _compute_celsius (linear calibration formula)
# ---------------------------------------------------------------------------


class TestComputeCelsius:
    """Tests for FLIRATSReader._compute_celsius."""

    def _reader_with_raw(self, raw: np.ndarray) -> FLIRATSReader:
        reader = FLIRATSReader.__new__(FLIRATSReader)
        reader.raw = raw
        return reader

    def test_linear_midpoint(self):
        # counts at midpoint of [0, 16383] → midpoint of [-20, 55] = 17.5 °C
        raw = np.array([[[8191]]], dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert result is not None
        assert abs(float(result[0, 0, 0]) - 17.5) < 0.1

    def test_linear_at_min(self):
        raw = np.array([[[0]]], dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert abs(float(result[0, 0, 0]) - (-20.0)) < 0.01

    def test_linear_at_max(self):
        raw = np.array([[[16383]]], dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert abs(float(result[0, 0, 0]) - 55.0) < 0.01

    def test_returns_none_when_calibration_missing(self):
        raw = np.zeros((1, 4, 4), dtype=np.uint16)
        meta = ATSMetadata()  # no calibration ranges
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert result is None

    def test_returns_none_on_degenerate_range(self):
        raw = np.zeros((1, 4, 4), dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=5000.0,
            range_counts_max=5000.0,  # hi == lo
            range_temperaturec_min=0.0,
            range_temperaturec_max=100.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert result is None

    def test_output_shape_matches_input(self):
        raw = np.zeros((5, 8, 10), dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert result is not None
        assert result.shape == (5, 8, 10)

    def test_output_dtype_is_float32(self):
        raw = np.zeros((1, 4, 4), dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        assert result.dtype == np.float32

    def test_non_zero_count_min(self):
        # counts in [3000, 13000] → temperature in [-20, 55]
        raw = np.array([[[8000]]], dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=3000.0,
            range_counts_max=13000.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
        )
        reader = self._reader_with_raw(raw)
        result = reader._compute_celsius(meta)
        expected = -20.0 + (8000 - 3000) / (13000 - 3000) * 75.0
        assert abs(float(result[0, 0, 0]) - expected) < 0.01


# ---------------------------------------------------------------------------
# ATSMetadata
# ---------------------------------------------------------------------------


class TestATSMetadata:
    def test_as_dict_returns_dict(self):
        meta = ATSMetadata(camera_model="A6751sc", width=640, height=512)
        d = meta.as_dict()
        assert isinstance(d, dict)
        assert d["camera_model"] == "A6751sc"

    def test_str_contains_camera_info(self):
        meta = ATSMetadata(camera_model="A6751sc", width=640, height=512, n_frames=10)
        s = str(meta)
        assert "A6751sc" in s
        assert "640" in s

    def test_get_temperature_kelvin(self):
        raw = np.ones((1, 4, 4), dtype=np.uint16) * 8191
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=-20.0,
            range_temperaturec_max=55.0,
            width=4,
            height=4,
            n_frames=1,
        )
        reader = FLIRATSReader.__new__(FLIRATSReader)
        reader.raw = raw
        reader.temperature_C = reader._compute_celsius(meta)
        reader.metadata = meta
        result_k = reader.get_temperature("K")
        result_c = reader.get_temperature("C")
        assert abs(float((result_k - result_c)[0, 0, 0]) - 273.15) < 0.01

    def test_get_temperature_fahrenheit(self):
        raw = np.zeros((1, 4, 4), dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=16383.0,
            range_temperaturec_min=0.0,
            range_temperaturec_max=100.0,
            width=4,
            height=4,
            n_frames=1,
        )
        reader = FLIRATSReader.__new__(FLIRATSReader)
        reader.raw = raw
        reader.temperature_C = reader._compute_celsius(meta)
        reader.metadata = meta
        result_f = reader.get_temperature("F")
        assert abs(float(result_f[0, 0, 0]) - 32.0) < 0.01  # 0°C = 32°F

    def test_get_temperature_invalid_unit_raises(self):
        reader = FLIRATSReader.__new__(FLIRATSReader)
        raw = np.zeros((1, 4, 4), dtype=np.uint16)
        meta = ATSMetadata(
            range_counts_min=0.0,
            range_counts_max=100.0,
            range_temperaturec_min=0.0,
            range_temperaturec_max=100.0,
        )
        reader.raw = raw
        reader.temperature_C = reader._compute_celsius(meta)
        reader.metadata = meta
        with pytest.raises(ValueError, match="[Uu]nknown unit"):
            reader.get_temperature("X")

    def test_get_temperature_raises_without_calibration(self):
        reader = FLIRATSReader.__new__(FLIRATSReader)
        reader.raw = np.zeros((1, 4, 4), dtype=np.uint16)
        reader.temperature_C = None
        reader.metadata = ATSMetadata()
        with pytest.raises(RuntimeError):
            reader.get_temperature("C")


# ---------------------------------------------------------------------------
# read_ats raises on non-ATS file
# ---------------------------------------------------------------------------


class TestReadATSValidation:
    def test_raises_on_non_ats_file(self, tmp_path):
        bad = tmp_path / "bad.ats"
        bad.write_bytes(b"NOT A FLIR ATS FILE" + b"\x00" * 200)
        with pytest.raises(ValueError, match="[Nn]ot a FLIR"):
            read_ats(str(bad))
