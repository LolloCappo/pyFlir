"""Tests for register definitions."""

from pyflir import registers as reg


class TestRegisterAddresses:
    """Verify critical register addresses haven't drifted."""

    def test_calibration_registers(self):
        assert reg.REG_CAL_INDEX     == 0x4E062404
        assert reg.REG_CAL_INDEX_MAX == 0x4E062408

    def test_nuc_load_dict_populated(self):
        assert isinstance(reg.REG_NUC_LOAD, dict)
        assert len(reg.REG_NUC_LOAD) > 0

    def test_roi_registers_exist(self):
        assert hasattr(reg, "REG_OFFSET_X")
        assert hasattr(reg, "REG_OFFSET_Y")
        assert hasattr(reg, "REG_WIDTH_MIN")
        assert hasattr(reg, "REG_HEIGHT_MIN")

    def test_flag_registers_exist(self):
        assert hasattr(reg, "REG_FLAG_STOWED")
        assert hasattr(reg, "REG_FLAG_IN_FOV")

    def test_temperature_sensors_dict(self):
        assert isinstance(reg.TEMP_SENSORS, dict)
        assert len(reg.TEMP_SENSORS) > 0

    def test_sensor_geometry_constants(self):
        assert hasattr(reg, "SENSOR_WIDTH")
        assert hasattr(reg, "SENSOR_HEIGHT")
        assert reg.SENSOR_WIDTH  > 0
        assert reg.SENSOR_HEIGHT > 0
