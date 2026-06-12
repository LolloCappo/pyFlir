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

from .camera import Camera, CameraError, discover
from .connection import ConnectionReport, tune_connection
from .errors import ConnectionStats
from .genicam import RegNode, parse_genicam_xml
from . import io
from .io import ATSMetadata, FLIRATSReader, read_ats, read_sfmov, read_sfmov_meta
from .provisioning import force_ip

__all__ = [
    # Camera driver
    "Camera",
    "CameraError",
    "discover",
    "force_ip",
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
