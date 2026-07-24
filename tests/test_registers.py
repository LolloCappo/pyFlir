"""Tests for register definitions."""

from pyflir import registers as reg


class TestRegisterAddresses:
    """Verify critical register addresses haven't drifted."""

    def test_calibration_registers(self):
        assert reg.REG_CAL_INDEX == 0x4E062404
        assert reg.REG_CAL_INDEX_MAX == 0x4E062408

    def test_calibration_load_dicts(self):
        # One entry per preset (0-3), addresses distinct.
        for d in (
            reg.REG_PS_CALIBRATION_TAG,
            reg.REG_PS_CALIBRATION_LOAD_TAG,
            reg.REG_PS_CALIBRATION_LOAD,
        ):
            assert set(d.keys()) == {0, 1, 2, 3}
            assert len(set(d.values())) == 4
        assert reg.REG_PS_CALIBRATION_TAG[0] == 0x4E059DE0
        assert reg.REG_PS_CALIBRATION_LOAD_TAG[0] == 0x4E05A1E0
        assert reg.REG_PS_CALIBRATION_LOAD[0] == 0x4E05A5E0
        assert reg.REG_ACTIVE_PRESET == 0x4E05882C

    def test_ir_format_registers(self):
        assert reg.REG_IR_FORMAT == 0x4E064E00
        assert reg.IR_FORMAT_NAMES[0] == "Radiometric"
        assert reg.IR_FORMAT_KELVIN_PER_COUNT[2] == 0.01

    def test_roi_registers_exist(self):
        assert hasattr(reg, "REG_OFFSET_X")
        assert hasattr(reg, "REG_OFFSET_Y")
        assert hasattr(reg, "REG_WIDTH_MIN")
        assert hasattr(reg, "REG_HEIGHT_MIN")

    def test_temperature_sensors_dict(self):
        assert isinstance(reg.TEMP_SENSORS, dict)
        assert len(reg.TEMP_SENSORS) > 0

    def test_sensor_geometry_constants(self):
        assert hasattr(reg, "SENSOR_WIDTH")
        assert hasattr(reg, "SENSOR_HEIGHT")
        assert reg.SENSOR_WIDTH > 0
        assert reg.SENSOR_HEIGHT > 0
