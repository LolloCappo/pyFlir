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
# ROI / sensor geometry
# ============================================================
REG_OFFSET_X = 0x4E058020  # horizontal pixel offset (RW)
REG_OFFSET_Y = 0x4E058024  # vertical pixel offset (RW)
REG_WIDTH_MIN = 0x4E05800C  # minimum ROI width (RO)
REG_WIDTH_INC = 0x4E058010  # width increment step (RO)
REG_HEIGHT_MIN = 0x4E058018  # minimum ROI height (RO)
REG_HEIGHT_INC = 0x4E05801C  # height increment step (RO)

# ============================================================
# NUC (Non-Uniformity Correction): one register per preset (PS0–PS3).
# GenICam name: "PS{n}CorrectionLoad" ("Load the non uniformity correction
# given by LoadName into preset {n}"). Write 1 (its CommandValue) to
# re-trigger loading whatever correction name is already resident in
# REG_CORRECTION_LOAD_NAME[preset]. Address recorded for reference; pyflir
# no longer exposes a method that writes this (see Camera.perform_nuc()
# for computing a fresh correction instead) -- re-triggering a reload of
# an already-active correction was found live to measurably degrade image
# noise rather than help, with no corresponding benefit demonstrated.
# ============================================================
REG_NUC_LOAD = {
    0: 0x4E059DB0,
    1: 0x4E059DB4,
    2: 0x4E059DB8,
    3: 0x4E059DBC,
}

# Non uniformity correction currently loaded for each preset, 256-byte
# ASCII string (RO). GenICam name: "PS{n}CorrectionName".
REG_CORRECTION_NAME = {
    0: 0x4E0591B0,
    1: 0x4E0592B0,
    2: 0x4E0593B0,
    3: 0x4E0594B0,
}

# Name of the correction to load into each preset when REG_NUC_LOAD[preset]
# is triggered, 256-byte ASCII string (RW). GenICam name:
# "PS{n}CorrectionLoadName". Address recorded for completeness, but nothing
# in pyflir currently writes to it: doing so needs a GVCP WRITEMEM, and
# pyGigEVision only exposes read_mem(), not write_mem().
REG_CORRECTION_LOAD_NAME = {
    0: 0x4E0595B0,
    1: 0x4E0596B0,
    2: 0x4E0597B0,
    3: 0x4E0598B0,
}

# ============================================================
# NUC "Correction Perform": the actual compute-a-new-NUC-now workflow, as
# distinct from REG_NUC_LOAD (re-loads an already-stored correction) above.
# GenICam Group "CorrectionPerform": "Registers used to perform a new non
# uniformity correction." A state machine: set CorrectionType/Source/PS{n},
# write Start, poll Status until Ready (Internal source needs no further
# action; External source needs Continue once a uniform target is in the
# FOV, at the "WaitingFor...SourceExternal" status), then check Result and
# call Accept or Discard/Abort.
# ============================================================
REG_CORRECTION_TYPE = (
    0x4E060C00  # 0=OnePoint(offset only) 1=TwoPoint(gain+offset) 2=UpdateOffset (RW)
)
CORRECTION_TYPE_NAMES = {0: "OnePoint", 1: "TwoPoint", 2: "UpdateOffset"}

# The camera's own XML has EnumEntry Name/ToolTip swapped for this feature
# (Name="External" carries ToolTip "Internal NUC flag", and vice versa) --
# confirmed backwards by cross-checking against CorrectionStatus's own
# self-consistent naming just below it ("WaitingForFirstSourceExternal":
# "camera is waiting for the user to fill the FOV... and command continue";
# "WaitingForFirstSourceInternal": "camera is bringing the internal NUC flag
# to temperature", no user action). Trust these Name/Value pairs, not the
# ToolTip text on CorrectionSource itself.
REG_CORRECTION_SOURCE = 0x4E060C04  # 0=External(user blackbody) 1=Internal(camera's own flag) (RW)
CORRECTION_SOURCE_NAMES = {0: "External", 1: "Internal"}

REG_CORRECTION_START = 0x4E060C08  # begin the correction process (write 1)
REG_CORRECTION_CONTINUE = 0x4E060C0C  # advance past a "WaitingFor...External" status (write 1)
REG_CORRECTION_ABORT = (
    0x4E060C10  # cancel; must accept, discard, or abort to end the process (write 1)
)
REG_CORRECTION_ACCEPT = 0x4E060C14  # keep the new correction (write 1)
REG_CORRECTION_DISCARD = 0x4E060C18  # revert to the previous correction (write 1)

REG_CORRECTION_STATUS = 0x4E060C1C  # poll this during the process (RO)
REG_CORRECTION_STATUS_TEXT = 0x4E060C20  # human-readable status, 256 bytes (RO)
CORRECTION_STATUS_NAMES = {
    0: "Ready",
    1: "SavingOldNUC",
    2: "WaitingForFirstSourceExternal",
    3: "WaitingForFirstSourceInternal",
    4: "CollectingFirstSource",
    5: "DoneCollectingFirstSource",
    6: "WaitingForSecondSourceExternal",
    7: "WaitingForSecondSourceInternal",
    8: "CollectingSecondSource",
    9: "DoneCollectingSecondSource",
    10: "CollectingTwinkleSource",
    11: "DoneCollectingTwinkleSource",
    12: "ComputingCoefficients",
    13: "Starting",
    14: "ApplyingFactoryBadPixelList",
}

REG_CORRECTION_NUM_FRAMES = 0x4E060D20  # frames to average per uniform source (int, RW)

REG_CORRECTION_RESULT = 0x4E060D6C  # outcome of the last correction process (RO)
REG_CORRECTION_RESULT_TEXT = 0x4E060D70  # human-readable result, 256 bytes (RO)
CORRECTION_RESULT_NAMES = {
    0: "Okay",
    1: "Abort",
    2: "AbortInvalidParam",
    3: "AbortFlagCoolerRunaway",
}

# Enable/disable each preset for the correction being performed; multiple
# can be corrected at once. Addresses read directly from XML, not assumed
# from a stride pattern.
REG_CORRECTION_PS = {
    0: 0x4E060D5C,
    1: 0x4E060D60,
    2: 0x4E060D64,
    3: 0x4E060D68,
}

# ============================================================
# Internal NUC flag (physical shutter on cameras that have one).
# GenICam Group "Flag": "Registers used to control the internal non
# uniformity correction flag."
# ============================================================
REG_FLAG_PRESENT = 0x4E05C400  # True if this camera has a NUC flag (RO)
REG_FLAG_TEMP_CONTROLLED = 0x4E05C404  # True if the flag is temperature controlled (RO)
REG_FLAG_STATE = 0x4E05C408  # 0=Stowed, 1=InFOV (RO)
REG_FLAG_DESIRED_TEMP = 0x4E05C40C  # target flag temp if temp-controlled (float, RW)
REG_FLAG_COOLER_ENABLED = 0x4E05C410  # flag cooler/heater on/off (RW)
REG_FLAG_STOWED = 0x4E05C414  # move flag out of FOV (write 1)
REG_FLAG_IN_FOV = 0x4E05C418  # move flag into FOV (write 1)

FLAG_STATE_NAMES = {0: "Stowed", 1: "InFOV"}

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
