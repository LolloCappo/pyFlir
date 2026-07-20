"""pyFlir: Pure-Python driver for FLIR thermal cameras over GigE Vision.

Built on pyGigEVision for the transport layer (GVCP control + GVSP streaming).
Vendor-specific registers and calibration are handled by this package.

Quick start::

    from pyflir import Camera

    with Camera() as cam:
        cam.download_xml()              # once; saves camera_<serial>.xml
        cam.load_xml("camera_xxx.xml")
        cam.frame_rate  = 50.0         # Hz
        cam.exposure_ms = 8.0          # ms
        cam.start_stream()
        frame = cam.read()             # numpy array (H × W), uint16
        frame = cam.read(latest=True)  # newest frame only (live display)
        frame = cam.grab()             # single-shot without manual start/stop

    # Discover cameras on the network
    cameras = pyflir.discover(interface_ip="169.254.100.1")
"""

__version__ = "0.2.0"
from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver

from . import io
from .camera import (
    STATUS_OK,
    STATUS_OVERFLOW,
    STATUS_UNDERFLOW,
    Camera,
    CameraError,
    apply_calibration,
    discover,
)
from .connection import ConnectionReport, tune_connection
from .errors import ConnectionStats
from .genicam import RegNode, parse_genicam_xml
from .io import ATSMetadata, FLIRATSReader, read_ats, read_sfmov, read_sfmov_meta
from .provisioning import force_ip

# Tell Sphinx these classes live in the top-level `pyflir` namespace so they
# are not treated as aliased imports and do not generate duplicate doc entries.
ConnectionReport.__module__ = __name__
ConnectionStats.__module__ = __name__
ATSMetadata.__module__ = __name__
FLIRATSReader.__module__ = __name__
RegNode.__module__ = __name__

__all__ = [
    # Camera driver
    "Camera",
    "CameraError",
    "discover",
    "force_ip",
    "apply_calibration",
    "STATUS_OK",
    "STATUS_UNDERFLOW",
    "STATUS_OVERFLOW",
    # GenICam XML
    "parse_genicam_xml",
    "RegNode",
    # File I/O
    "io",
    "FLIRATSReader",
    "ATSMetadata",
    "read_ats",
    "read_sfmov",
    "read_sfmov_meta",
    # Connection diagnostics
    "ConnectionReport",
    "ConnectionStats",
    "tune_connection",
    # Low-level re-exports from pyGigEVision
    "GVCPClient",
    "GVCPError",
    "GVSPReceiver",
    # Version
    "__version__",
]
