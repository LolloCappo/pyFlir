"""FLIR camera register addresses and constants.

FLIR Xsc-series specific register addresses. Direct-address registers
can be accessed without loading the GenICam XML. Feature-backed registers
(Width, Height, ExposureTime, etc.) require load_xml() first.

Source: GenICam XML downloaded from a FLIR A-series / Xsc camera.
"""

# ============================================================
# Calibration / radiometry (FLIR Xsc series)
# ============================================================
REG_CAL_INDEX = 0x4E062404  # active calibration block index (RW)
REG_CAL_INDEX_MAX = 0x4E062408  # highest valid index = n_blocks - 1 (RO)
REG_CAL_TAG = 0x4E06240C  # internal tag string, 256 bytes (RO)
REG_CAL_NAME = 0x4E06250C  # friendly name string, 256 bytes (RO)
REG_CAL_LENS = 0x4E06260C  # lens name string, 256 bytes (RO)
REG_CAL_LENS_FILTER = 0x4E06270C  # lens filter string, 256 bytes (RO)
# Confirmed °C empirically (observed range -20..55, impossible as Kelvin).
# The camera's own GenICam XML mislabels these "CalibrationQueryMinTemp"/
# "MaxTemp" as "in Kelvin" in free-text ToolTip/Description -- with no
# structured <Unit> tag backing it, unlike AtmosphericTemperature/
# ReflectedTemperature which do carry a real <Unit>Kelvin</Unit>. Trust the
# physically-required °C here, not that XML tooltip.
REG_CAL_TMIN = 0x4E062910  # min temperature of current block (float, °C)
REG_CAL_TMAX = 0x4E062918  # max temperature of current block (float, °C)

# ============================================================
# IRFormat: what the camera streams. In "Radiometric" mode it streams raw ADC
# counts and the host must apply the calibration polynomial (apply_calibration).
# In "TemperatureLinear" modes the camera does the radiometry ON-BOARD (its own
# NUC + calibration + object parameters) and streams temperature directly: each
# count is a fixed number of milli-kelvin. This is the same architecture the
# Telops cameras use (pyTelops CalibrationMode "RT"), and the simplest correct
# path -- host conversion is just count * kelvin_per_count - 273.15.
# "Only valid if the camera is factory calibrated and a factory calibration is
# loaded" (per the XML) -- so call load_calibration() first. Verified live: the
# camera's TemperatureLinear10mK output agrees with apply_calibration() on the
# same scene, confirming the polynomial path is correct. Changing the format
# only takes effect on the stream after a fresh acquisition start.
# ============================================================
REG_IR_FORMAT = 0x4E064E00  # (RW)
IR_FORMAT_NAMES = {0: "Radiometric", 1: "TemperatureLinear100mK", 2: "TemperatureLinear10mK"}
IR_FORMAT_VALUES = {v: k for k, v in IR_FORMAT_NAMES.items()}
# Kelvin per count for the temperature-linear modes (temp_K = count * this).
IR_FORMAT_KELVIN_PER_COUNT = {1: 0.1, 2: 0.01}

# IMPORTANT: REG_CAL_INDEX / REG_CAL_TAG and every REG_CAL_* above are the
# "CalibrationQuery*" family -- a READ-ONLY BROWSE CURSOR into the camera's
# factory calibration library. Writing REG_CAL_INDEX only moves the cursor
# used to read the *Query* registers back; it does NOT change the calibration
# applied to the live GVSP stream. Confirmed from the XML pInvalidator graph:
# no streaming register lists CalibrationQueryIndexReg as an invalidator (they
# list PS{n}CalibrationLoadReg instead). The calibration ACTUALLY applied to
# the stream for preset n is named by REG_PS_CALIBRATION_TAG[n] below.

# Which preset is output in single-preset mode. GenICam "ActivePreset".
REG_ACTIVE_PRESET = 0x4E05882C  # (RW)

# Tag name of the factory calibration currently loaded into each preset,
# 256-byte ASCII string (RO). GenICam name "PS{n}CalibrationTag". This is the
# calibration the live stream actually uses for that preset -- match it back
# against REG_CAL_TAG (CalibrationQueryTag) across the browse cursor to find
# which query block's coefficients apply to captured frames. Each factory
# calibration is fit at ONE specific integration time (PS{n}IntegrationTime);
# changing integration time away from that value desyncs the counts from this
# calibration's polynomial and makes temperatures read wrong.
REG_PS_CALIBRATION_TAG = {
    0: 0x4E059DE0,
    1: 0x4E059EE0,
    2: 0x4E059FE0,
    3: 0x4E05A0E0,
}

# Staging string for the calibration to load into each preset, 256-byte ASCII
# (RW). GenICam name "PS{n}CalibrationLoadTag". Written 4 bytes at a time via
# GVCP WRITEREG (little-endian within each word) since pyGigEVision exposes no
# WRITEMEM -- see Camera._write_string_reg(). Then execute REG_PS_CALIBRATION_LOAD
# to apply it. Loading a factory calibration also sets that preset's integration
# time to the value the calibration was fit at (verified live: loading
# "25mm, Empty, -20C - 55C" moved PS0IntegrationTime from 0.1 ms to 2.354 ms),
# which is exactly the integration-time <-> calibration coupling FLIR's own
# software enforces.
REG_PS_CALIBRATION_LOAD_TAG = {
    0: 0x4E05A1E0,
    1: 0x4E05A2E0,
    2: 0x4E05A3E0,
    3: 0x4E05A4E0,
}

# Command: write 1 to load the factory calibration named by
# REG_PS_CALIBRATION_LOAD_TAG[preset] into that preset. GenICam
# "PS{n}CalibrationLoad" (CommandValue 1).
REG_PS_CALIBRATION_LOAD = {
    0: 0x4E05A5E0,
    1: 0x4E05A5E4,
    2: 0x4E05A5E8,
    3: 0x4E05A5EC,
}

# ============================================================
# Automatic non-uniformity correction (offset update).
#
# This camera exposes *two* separate NUC paths, and only one of them is safe
# to drive routinely:
#
#   1. "CorrectionPerform" (CorrectionType / CorrectionStart / CorrectionAccept,
#      0x4E060C00..) -- the full correction procedure. Its own XML describes it
#      as collecting and averaging "frames ... from each uniform temperature
#      source", i.e. it *recomputes and overwrites* the stored gain and offset
#      coefficients. Running its OnePoint/TwoPoint modes against the internal
#      flag (a single, near-ambient source) is not a valid substitute for the
#      factory calibration, which was fit against real blackbody sources: it
#      replaces good factory gain terms with garbage and visibly corrupts the
#      image (swirl / contour artifacts on object edges). Confirmed live on the
#      A6751sc this session, and it survives power cycles because the bad
#      coefficients are stored. pyflir deliberately does NOT expose it.
#
#   2. "CorrectionAuto" (below) -- described by the XML as "automatic non
#      uniformity correction with the internal flag (OFFSET UPDATE)". This is
#      the routine, non-destructive one: it re-levels the per-pixel offset on
#      top of the factory NUC and leaves the factory gain terms alone. It is
#      what the vendor software runs when you select a temperature range (flag
#      clicks, a few seconds of noise, then a correct image), and it is the
#      operation pyflir exposes as Camera.nuc().
#
# An offset update is exactly what a calibration load needs: loading a
# calibration changes the integration time, which shifts the detector's dark
# level, and the stored offset no longer matches. See Camera.nuc().
# ============================================================
REG_CORRECTION_AUTO_ENABLED = 0x4E061400  # camera self-NUCs when triggers met (RW)
REG_CORRECTION_AUTO_USE_DELTA_TEMP = 0x4E061404  # trigger on front-panel drift (RW)
REG_CORRECTION_AUTO_DELTA_TEMP = 0x4E061408  # drift in °C that triggers (float, RW)
REG_CORRECTION_AUTO_USE_DELTA_TIME = 0x4E06140C  # trigger on elapsed time (RW)
REG_CORRECTION_AUTO_DELTA_TIME = 0x4E061410  # minutes between updates (RW)
REG_CORRECTION_AUTO_PERFORM = 0x4E061414  # command: update now (WO)
REG_CORRECTION_AUTO_IN_PROGRESS = 0x4E061418  # 1 while an update is running (RO)

# ============================================================
# NUC flag (shutter). Read-only here: pyflir never parks or moves the flag
# itself -- CorrectionAutoPerform drives it internally and returns it to its
# previous position. These are for observability (and for reporting a clear
# error if a unit has no flag, in which case an internal-source NUC is
# impossible and the scene would have to be covered manually).
# ============================================================
REG_FLAG_PRESENT = 0x4E05C400  # 1 if this camera has a NUC flag (RO)
REG_FLAG_STATE = 0x4E05C408  # 0=Stowed, 1=InFOV (RO)

FLAG_STATE_NAMES = {0: "Stowed", 1: "InFOV"}

# ============================================================
# ROI / sensor geometry
# ============================================================
REG_OFFSET_X = 0x4E058020  # horizontal pixel offset (RW)
REG_OFFSET_Y = 0x4E058024  # vertical pixel offset (RW)
REG_WIDTH_MIN = 0x4E05800C  # minimum ROI width (RO)
REG_WIDTH_INC = 0x4E058010  # width increment step (RO)
REG_HEIGHT_MIN = 0x4E058018  # minimum ROI height (RO)
REG_HEIGHT_INC = 0x4E05801C  # height increment step (RO)

# ============================================================
# On-board temperature sensors. GenICam "DeviceTemperatureSelector" lists
# 9 entries (0-8); confirmed °C via a structured Description on
# "DeviceTemperature" itself ("...in degrees Celsius"), no ambiguity here
# unlike REG_CAL_TMIN/TMAX. "Chassis" is spelled correctly here even though
# the camera's own XML has it as "Chasis" (EnumEntry Name="Chasis") -- this
# dict's keys are pyflir's own public API, not literal register names.
# ============================================================
REG_TEMP_SELECTOR = 0x4E05B418  # selects which sensor to read (RW)
REG_TEMP_VALUE = 0x4E05B41C  # temperature in °C (float, RO)

TEMP_SENSORS = {
    "FPA": 0,
    "Digitizer": 1,
    "PowerBoard": 2,
    "FrontPanel": 3,
    "AirGap": 4,
    "Internal": 5,
    "Chassis": 6,
    "Lens": 7,
    "Flag": 8,
}

# ============================================================
# Sensor geometry defaults (FLIR Xsc 640 × 512 series)
# ============================================================
SENSOR_WIDTH = 640
SENSOR_HEIGHT = 512
