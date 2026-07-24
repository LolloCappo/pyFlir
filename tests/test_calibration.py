"""Tests for apply_calibration() and the two-polynomial radiometric conversion."""

import numpy as np

from pyflir.camera import STATUS_OK, STATUS_OVERFLOW, STATUS_UNDERFLOW, apply_calibration


def _make_cal(
    tmin: float = -20.0,
    tmax: float = 55.0,
    counts_min: float = 0.0,
    counts_max: float = 16383.0,
    counts_coeffs=None,  # lowest degree first
    temp_coeffs=None,  # lowest degree first
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
        temp_coeffs = [tmin + 273.15, tmax - tmin] if kelvin_output else [tmin, tmax - tmin]

    return {
        "tmin": tmin,
        "tmax": tmax,
        "counts_min": counts_min,
        "counts_max": counts_max,
        "counts_order": len(counts_coeffs) - 1,
        "counts_coeffs": counts_coeffs,
        "counts_background": counts_background,
        "temp_order": len(temp_coeffs) - 1,
        "temp_coeffs": temp_coeffs,
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


class TestApplyCalibrationBackground:
    """counts_background must be subtracted in the counts -> radiance step."""

    def test_background_subtraction_exact(self):
        # Linear cal: W = x / counts_max, T = tmin + (tmax - tmin) * W.
        # Subtracting `background` from W shifts T by -background * (tmax - tmin).
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        cal_ref = _make_cal(counts_background=0.0)
        cal_bg = _make_cal(counts_background=0.1)
        result_ref = apply_calibration(raw, cal_ref)
        result_bg = apply_calibration(raw, cal_bg)
        expected_shift = 0.1 * 75.0  # 0.1 * (tmax - tmin)
        assert abs((float(result_ref[0, 0]) - float(result_bg[0, 0])) - expected_shift) < 1e-6

    def test_zero_background_is_unaffected(self):
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        cal = _make_cal(counts_background=0.0)
        result = apply_calibration(raw, cal)
        assert abs(float(result[0, 0]) - 17.5) < 0.2


class TestApplyCalibrationClipAndStatus:
    """Out-of-range counts must clip to the calibrated domain and flag status."""

    def test_in_range_status_ok(self):
        cal = _make_cal()
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        _, status = apply_calibration(raw, cal, return_status=True)
        assert status[0, 0] == STATUS_OK

    def test_underflow_clips_and_flags(self):
        cal = _make_cal(counts_min=1000.0, counts_max=16383.0)
        raw_below = np.array([[500 * 4]], dtype=np.uint16)
        raw_at_min = np.array([[1000 * 4]], dtype=np.uint16)
        temp_below, status = apply_calibration(raw_below, cal, return_status=True)
        temp_at_min = apply_calibration(raw_at_min, cal)
        assert status[0, 0] == STATUS_UNDERFLOW
        assert abs(float(temp_below[0, 0]) - float(temp_at_min[0, 0])) < 1e-9

    def test_overflow_clips_and_flags(self):
        cal = _make_cal(counts_min=0.0, counts_max=10000.0)
        raw_above = np.array([[15000 * 4]], dtype=np.uint16)
        raw_at_max = np.array([[10000 * 4]], dtype=np.uint16)
        temp_above, status = apply_calibration(raw_above, cal, return_status=True)
        temp_at_max = apply_calibration(raw_at_max, cal)
        assert status[0, 0] == STATUS_OVERFLOW
        assert abs(float(temp_above[0, 0]) - float(temp_at_max[0, 0])) < 1e-9

    def test_return_status_false_returns_plain_array_not_tuple(self):
        cal = _make_cal()
        raw = np.zeros((4, 4), dtype=np.uint16)
        result = apply_calibration(raw, cal)
        assert isinstance(result, np.ndarray)

    def test_return_status_true_returns_tuple_with_matching_shape(self):
        cal = _make_cal()
        raw = np.zeros((4, 4), dtype=np.uint16)
        temp, status = apply_calibration(raw, cal, return_status=True)
        assert temp.shape == status.shape == (4, 4)
        assert status.dtype == np.uint8


class TestApplyCalibrationObjectParameters:
    """Emissivity/atmosphere/reflected-temperature compensation."""

    def test_default_params_unchanged_from_no_correction(self):
        cal = _make_cal()
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        result_default = apply_calibration(raw, cal)
        result_explicit = apply_calibration(raw, cal, emissivity=1.0, tau=1.0)
        assert abs(float(result_default[0, 0]) - float(result_explicit[0, 0])) < 1e-9

    def test_reflected_temp_only_matters_when_emissivity_below_one(self):
        cal = _make_cal()
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        r_cold = apply_calibration(raw, cal, emissivity=0.9, tau=1.0, refl_temp_c=0.0)
        r_hot = apply_calibration(raw, cal, emissivity=0.9, tau=1.0, refl_temp_c=50.0)
        assert not np.isclose(float(r_cold[0, 0]), float(r_hot[0, 0]))

    def test_atm_temp_only_matters_when_tau_below_one(self):
        cal = _make_cal()
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        r_cold = apply_calibration(raw, cal, emissivity=1.0, tau=0.9, atm_temp_c=0.0)
        r_hot = apply_calibration(raw, cal, emissivity=1.0, tau=0.9, atm_temp_c=50.0)
        assert not np.isclose(float(r_cold[0, 0]), float(r_hot[0, 0]))

    def test_object_signal_formula_exact(self):
        # Linear cal: W_total = x / counts_max, T(W) = tmin + (tmax - tmin) * W.
        # Verify apply_calibration's arithmetic matches the documented
        # object-signal equation exactly, not just "changes with params".
        tmin, tmax, counts_max = -20.0, 55.0, 16383.0
        cal = _make_cal(tmin=tmin, tmax=tmax, counts_min=0.0, counts_max=counts_max)
        raw_14bit = 8192
        raw = np.array([[raw_14bit * 4]], dtype=np.uint16)
        emissivity, tau = 0.9, 0.95
        refl_temp_c, atm_temp_c = 20.0, 15.0

        w_total = raw_14bit / counts_max
        w_refl = (refl_temp_c - tmin) / (tmax - tmin)
        w_atm = (atm_temp_c - tmin) / (tmax - tmin)
        w_obj = (w_total - (1 - emissivity) * tau * w_refl - (1 - tau) * w_atm) / (emissivity * tau)
        expected_t = tmin + (tmax - tmin) * w_obj

        result = apply_calibration(
            raw, cal, emissivity=emissivity, refl_temp_c=refl_temp_c, atm_temp_c=atm_temp_c, tau=tau
        )
        assert abs(float(result[0, 0]) - expected_t) < 1e-6


class TestApplyCalibrationRBF:
    """R/B/F Planck conversion (FLIR's official radiance->temperature formula)."""

    def _rbf_cal(self, r, b, f):
        # counts->radiance is identity in 14-bit (coeff1=1) so radiance == count,
        # letting us hand-check T_kelvin = B / ln(R/count + F).
        return {
            "tmin": -50.0,
            "tmax": 500.0,
            "counts_min": 1.0,
            "counts_max": 16000.0,
            "counts_background": 0.0,
            "counts_coeffs": [0.0, 1.0],
            "temp_coeffs": [0.0, 1.0],
            "r": r,
            "b": b,
            "f": f,
        }

    def test_rbf_matches_flir_formula(self):
        # Chosen so T lands in [tmin, tmax] (not clamped): ~99.9 C.
        r, b, f = 213300.0, 1400.0, 0.0
        cal = self._rbf_cal(r, b, f)
        count = 5000
        expected_c = b / np.log(r / count + f) - 273.15  # FLIR: T_K = B/ln(R/rad + F)
        assert -50.0 < expected_c < 500.0  # guard: in range, so no clamp
        raw = np.full((2, 2), count, dtype=np.uint16)
        out = apply_calibration(raw, cal, count_divisor=1.0, method="rbf")
        assert abs(float(out.mean()) - expected_c) < 1e-6

    def test_rbf_differs_from_polynomial(self):
        cal = self._rbf_cal(213300.0, 1400.0, 0.0)
        raw = np.full((2, 2), 5000, dtype=np.uint16)
        t_rbf = float(apply_calibration(raw, cal, count_divisor=1.0, method="rbf").mean())
        t_poly = float(apply_calibration(raw, cal, count_divisor=1.0, method="polynomial").mean())
        assert not np.isclose(t_rbf, t_poly)

    def test_rbf_falls_back_to_polynomial_without_coeffs(self):
        # A cal dict lacking r/b/f (e.g. cached) must not blow up under method=rbf.
        cal = _make_cal()  # no r/b/f
        raw = np.array([[8192 * 4]], dtype=np.uint16)
        out_rbf = apply_calibration(raw, cal, method="rbf")
        out_poly = apply_calibration(raw, cal, method="polynomial")
        assert np.isclose(float(out_rbf[0, 0]), float(out_poly[0, 0]))


class TestApplyCalibrationClipOption:
    """clip=False exposes out-of-range pixels as NaN instead of faking endpoints."""

    def test_clip_true_pins_out_of_range_to_endpoints(self):
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=2000.0, counts_max=12000.0)
        over = np.full((2, 2), 15000 * 4, dtype=np.uint16)  # above counts_max
        out = apply_calibration(over, cal, clip=True)
        assert np.allclose(out, 55.0)

    def test_clip_false_marks_out_of_range_nan(self):
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=2000.0, counts_max=12000.0)
        over = np.full((2, 2), 15000 * 4, dtype=np.uint16)
        under = np.full((2, 2), 100 * 4, dtype=np.uint16)
        assert np.all(np.isnan(apply_calibration(over, cal, clip=False)))
        assert np.all(np.isnan(apply_calibration(under, cal, clip=False)))

    def test_clip_false_leaves_in_range_untouched(self):
        cal = _make_cal(tmin=-20.0, tmax=55.0, counts_min=2000.0, counts_max=12000.0)
        mid = np.full((2, 2), 7000 * 4, dtype=np.uint16)
        a = apply_calibration(mid, cal, clip=True)
        b = apply_calibration(mid, cal, clip=False)
        assert np.allclose(a, b)
        assert not np.any(np.isnan(b))
