"""Tests for apply_calibration() and the two-polynomial radiometric conversion."""

import numpy as np
import pytest

from pyflir.camera import apply_calibration


def _make_cal(
    tmin: float = -20.0,
    tmax: float = 55.0,
    counts_min: float = 0.0,
    counts_max: float = 16383.0,
    counts_coeffs=None,  # lowest degree first
    temp_coeffs=None,    # lowest degree first
    kelvin_output: bool = False,
    counts_background: float = 0.0,
) -> dict:
    """Build a synthetic calibration dict for unit testing.

    Default polynomials implement a simple linear map:
      counts (14-bit) → radiance W ∈ [0, 1]
      radiance W      → temperature (Celsius or Kelvin)
    """
    if counts_coeffs is None:
        # W = x / counts_max  (linear, W ∈ [0,1] for x ∈ [0, counts_max])
        counts_coeffs = [0.0, 1.0 / counts_max]

    if temp_coeffs is None:
        if kelvin_output:
            # T_K = (tmin+273.15) + (tmax-tmin) * W
            temp_coeffs = [tmin + 273.15, tmax - tmin]
        else:
            # T_C = tmin + (tmax-tmin) * W
            temp_coeffs = [tmin, tmax - tmin]

    return {
        "tmin":               tmin,
        "tmax":               tmax,
        "counts_min":         counts_min,
        "counts_max":         counts_max,
        "counts_order":       len(counts_coeffs) - 1,
        "counts_coeffs":      counts_coeffs,
        "counts_background":  counts_background,
        "temp_order":         len(temp_coeffs) - 1,
        "temp_coeffs":        temp_coeffs,
    }


class TestApplyCalibration:
    """Unit tests for apply_calibration()."""

    def test_midpoint_celsius_output(self):
        # 14-bit midpoint: raw uint16 = 8191 * 4 = 32764 (MSB-aligned)
        # Expected T ≈ midpoint of [-20, 55] = 17.5°C
        cal = _make_cal()
        raw = np.array([[32764]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - 17.5) < 0.2

    def test_tmin_at_counts_min(self):
        # counts_min * 4 → T = tmin
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=0.0, counts_max=16383.0)
        raw = np.array([[0]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - (-20.0)) < 0.1

    def test_tmax_at_counts_max(self):
        # counts_max * 4 → T = tmax
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=0.0, counts_max=16383.0)
        raw = np.array([[16383 * 4]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - 55.0) < 0.2

    def test_kelvin_output_auto_detected(self):
        # Polynomial outputs Kelvin; apply_calibration must subtract 273.15
        cal = _make_cal(kelvin_output=True)
        raw = np.array([[0]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        # At counts=0 (14-bit), T_K = tmin + 273.15 → T_C = tmin
        assert abs(float(result[0, 0]) - (-20.0)) < 0.2

    def test_kelvin_output_midpoint(self):
        cal = _make_cal(kelvin_output=True)
        raw = np.array([[8191 * 4]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - 17.5) < 0.2

    def test_output_shape_2d(self):
        cal = _make_cal()
        raw = np.zeros((512, 640), dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert result.shape == (512, 640)
        assert result.dtype == np.float64

    def test_output_shape_single_pixel(self):
        cal = _make_cal()
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert result.shape == (1, 1)

    def test_monotone_response(self):
        # Temperature must increase with counts.
        cal = _make_cal()
        counts_14bit = np.linspace(100, 16200, 50)
        raw = (counts_14bit * 4).astype(np.uint16).reshape(1, -1)
        result = apply_calibration(raw, cal)
        diffs = np.diff(result[0])
        assert np.all(diffs > 0), "Temperature must be monotonically increasing"

    def test_msb_alignment_divides_by_4(self):
        # Two frames: same 14-bit value, one given as MSB-aligned uint16 (×4).
        # They must produce the same temperature after /4 correction.
        cal = _make_cal()
        adc_14bit = 8000
        raw_msb = np.array([[adc_14bit * 4]], dtype=np.uint16)
        # Force a 14-bit input (no /4 in polynomial — just check that x/4 is used)
        # Computed reference: T = tmin + adc_14bit/counts_max * (tmax - tmin)
        result = apply_calibration(raw_msb, cal)
        expected = -20.0 + (adc_14bit / 16383.0) * 75.0
        assert abs(float(result[0, 0]) - expected) < 0.2

    def test_returns_float64(self):
        cal = _make_cal()
        raw = np.zeros((4, 4), dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert result.dtype == np.float64

    def test_room_temperature_approximately_correct(self):
        # Synthetic cal where room temp (23°C) raw count is known.
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=0.0, counts_max=16383.0)
        # 14-bit counts for 23°C with linear mapping: 43/75 * 16383 ≈ 9385
        raw_14bit = int((23 - (-20)) / 75.0 * 16383)
        raw = np.array([[raw_14bit * 4]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - 23.0) < 0.5


class TestApplyCalibrationEdgeCases:
    def test_uniform_frame_gives_uniform_temperature(self):
        cal = _make_cal()
        val = 8000 * 4
        raw = np.full((8, 8), val, dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert result.std() < 1e-9

    def test_integer_input_accepted(self):
        cal = _make_cal()
        raw = np.array([[10000 * 4]], dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert result is not None
        assert np.isfinite(result).all()
