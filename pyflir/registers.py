"""FLIR camera register addresses and constants.

FLIR Xsc-series specific register addresses. Direct-address registers
can be accessed without loading the GenICam XML. Feature-backed registers
(Width, Height, ExposureTime, etc.) require load_xml() first.

Source: GenICam XML downloaded from a FLIR A-series / Xsc camera.
"""

# ============================================================
# Calibration / radiometry (FLIR Xsc series)
# ============================================================
REG_CAL_INDEX       = 0x4E062404  # active calibration block index (RW)
REG_CAL_INDEX_MAX   = 0x4E062408  # highest valid index = n_blocks - 1 (RO)
REG_CAL_TAG         = 0x4E06240C  # internal tag string, 256 bytes (RO)
REG_CAL_NAME        = 0x4E06250C  # friendly name string, 256 bytes (RO)
REG_CAL_LENS        = 0x4E06260C  # lens name string, 256 bytes (RO)
REG_CAL_LENS_FILTER = 0x4E06270C  # lens filter string, 256 bytes (RO)
REG_CAL_TMIN        = 0x4E062910  # min temperature of current block (float, °C)
REG_CAL_TMAX        = 0x4E062918  # max temperature of current block (float, °C)

# ============================================================
# ROI / sensor geometry
# ============================================================
REG_OFFSET_X   = 0x4E058020  # horizontal pixel offset (RW)
REG_OFFSET_Y   = 0x4E058024  # vertical pixel offset (RW)
REG_WIDTH_MIN  = 0x4E05800C  # minimum ROI width (RO)
REG_WIDTH_INC  = 0x4E058010  # width increment step (RO)
REG_HEIGHT_MIN = 0x4E058018  # minimum ROI height (RO)
REG_HEIGHT_INC = 0x4E05801C  # height increment step (RO)

# ============================================================
# NUC (Non-Uniformity Correction) — one register per preset (PS0–PS3)
# Write 1 to apply the stored NUC coefficients for that preset.
# ============================================================
REG_NUC_LOAD = {
    0: 0x4E059DB0,
    1: 0x4E059DB4,
    2: 0x4E059DB8,
    3: 0x4E059DBC,
}

# ============================================================
# Internal NUC flag (physical shutter on cameras that have one)
# ============================================================
REG_FLAG_STOWED = 0x4E05C414  # move flag out of FOV (write 1)
REG_FLAG_IN_FOV = 0x4E05C418  # move flag into FOV (write 1)

# ============================================================
# On-board temperature sensors
# ============================================================
REG_TEMP_SELECTOR = 0x4E05B418  # selects which sensor to read (RW)
REG_TEMP_VALUE    = 0x4E05B41C  # temperature in °C — float (RO)

TEMP_SENSORS = {
    "FPA":        0,
    "Digitizer":  1,
    "PowerBoard": 2,
    "FrontPanel": 3,
}

# ============================================================
# Sensor geometry defaults (FLIR Xsc 640 × 512 series)
# ============================================================
SENSOR_WIDTH  = 640
SENSOR_HEIGHT = 512
