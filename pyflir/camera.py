"""FLIR thermal camera driver over GigE Vision.

Provides a Pythonic interface to FLIR Xsc-series and A-series cameras
using pyGigEVision for the transport layer. Handles discovery, streaming,
frame acquisition, ROI, calibration block selection, NUC, and diagnostics.

Usage::

    from pyflir import Camera

    with Camera() as cam:
        cam.download_xml()            # once; saves camera_<serial>.xml
        cam.load_xml("camera_xxx.xml")
        cam.frame_rate = 50.0
        cam.exposure_ms = 8.0
        cam.start_stream()
        frame = cam.read()            # numpy array (H, W), uint16
        cam.stop_stream()
"""

import logging
import socket
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver, fetch_genicam_xml
from pyGigEVision.standard import (
    REG_SC_HOST_PORT,
    REG_SC_PACKET_SIZE,
    REG_SC_PACKET_DELAY,
    REG_SC_DEST_ADDR,
)

from .genicam import parse_genicam_xml, RegNode
from . import registers as reg


# Default MTU-safe packet size for 1 GbE without jumbo frames
DEFAULT_PACKET_SIZE = 1500

# SFNC → FLIR-camera-specific feature name aliases.
# Populated by load_xml() after inspecting available node names.
_SFNC_CANDIDATES = {
    "Width":                   ["Width", "WidthReg"],
    "Height":                  ["Height", "HeightReg"],
    "PixelFormat":             ["PixelFormat", "PixelFormatReg"],
    "AcquisitionStart":        ["AcquisitionStart", "AcquisitionStartReg"],
    "AcquisitionStop":         ["AcquisitionStop", "AcquisitionStopReg"],
    "AcquisitionMode":         ["AcquisitionMode", "AcquisitionModeReg"],
    "ExposureTime":            ["ExposureTime", "PS0IntegrationTimeReg",
                                "IntegrationTimeReg"],
    "AcquisitionFrameRate":    ["AcquisitionFrameRate", "PS0FrameRateReg",
                                "FrameRateReg", "SuperframeRateReg"],
    "AcquisitionFrameRateMax": ["PS0FrameRateMax", "AcquisitionFrameRateMax",
                                "FrameRateMax"],
    "DeviceTemperature":       ["DeviceTemperature", "FPAColdReg",
                                "FPATemperatureReg"],
    "Emissivity":              ["ObjectEmissivityReg"],
    "ObjectDistance":          ["ObjectDistanceReg"],
    "AtmosphericTemperature":  ["AtmosphericTemperatureReg"],
    "ReflectedTemperature":    ["ReflectedTemperatureReg"],
    "RelativeHumidity":        ["RelativeHumidityReg"],
    "CalibrationBlock":        ["CalibrationQueryIndexReg"],
}


class CameraError(Exception):
    """Raised for high-level camera operation failures."""


class Camera:
    """FLIR GigE Vision camera controller.

    Uses pyGigEVision for GVCP control and GVSP image streaming.
    Vendor-specific registers are accessed directly via addresses in
    :mod:`pyflir.registers`.

    Args:
        ip: Camera IP address. None = auto-discover first camera.
        interface_ip: Local NIC IP for discovery and streaming.
            Useful with multiple network interfaces.
        timeout: GVCP socket timeout in seconds.

    Examples::

        with Camera() as cam:
            cam.download_xml()
            cam.load_xml("camera_xxx.xml")
            cam.start_stream()
            frame = cam.read()
            cam.stop_stream()

        # Single-shot grab
        with Camera() as cam:
            cam.load_xml("camera_xxx.xml")
            frame = cam.grab()
    """

    def __init__(
        self,
        ip: Optional[str] = None,
        interface_ip: Optional[str] = None,
        timeout: float = 2.0,
    ):
        self.ip           = ip
        self.interface_ip = interface_ip or ""
        self._timeout     = timeout

        self._gvcp: Optional[GVCPClient]    = None
        self._gvsp: Optional[GVSPReceiver]  = None
        self._nodes: Dict[str, RegNode]     = {}
        self._aliases: Dict[str, str]       = {}
        self._streaming: bool               = False

        # Populated after load_xml() or connect()
        self.width:  Optional[int] = None
        self.height: Optional[int] = None
        self.serial: str = ""
        self.model:  str = ""
        # Number of trailing metadata rows the camera appends to each frame.
        # These rows are stripped from grab()/read() results and exposed via
        # the last_metadata_rows attribute after each acquisition.
        self._metadata_rows: int = 0
        self.last_metadata_rows: Optional["np.ndarray"] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def __repr__(self) -> str:
        if not self._gvcp:
            return f"Camera(ip={self.ip!r}, disconnected)"

        def _s(fn):
            try:
                return fn()
            except Exception:
                return "n/a"

        lines = [f"Camera  {self.model or '?'}  s/n {self.serial or '?'}  @ {self.ip}"]
        lines.append(f"  streaming : {self._streaming}")
        if self._nodes:
            w    = _s(lambda: self.read_int("Width"))
            h    = _s(lambda: self.read_int("Height"))
            fps  = _s(lambda: f"{self.read_float('AcquisitionFrameRate'):.1f} Hz")
            fmax = _s(lambda: f"{self.get_max_frame_rate():.1f} Hz")
            exp  = _s(lambda: f"{self.read_float('ExposureTime') * 1e3:.3f} ms")
            temp = _s(lambda: f"{self.read_float('DeviceTemperature'):.1f} °C")
            cal  = _s(lambda: self._gvcp.read_reg(reg.REG_CAL_INDEX))
            lines.append(f"  ROI       : {w} × {h} px")
            lines.append(f"  frame rate: {fps}  (max {fmax})")
            lines.append(f"  exposure  : {exp}")
            lines.append(f"  FPA temp  : {temp}")
            lines.append(f"  cal block : {cal}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Discover (if needed), open GVCP connection, and acquire control.

        Raises:
            CameraError: If no camera is found or the connection fails.
        """
        if self._gvcp is not None:
            return

        if not self.ip:
            # With an empty interface_ip pyGigEVision broadcasts on every
            # local interface, so cameras reachable via USB-to-GigE dongles
            # are not missed (Linux default-route issue).
            logger.debug(
                "Discovering cameras on %s…",
                self.interface_ip or "all interfaces",
            )
            found = GVCPClient.discover(
                interface_ip=self.interface_ip, timeout=3.0
            )
            if not found:
                raise CameraError(
                    "No GigE Vision cameras found. Check:\n"
                    "  • Ethernet cable and link LED\n"
                    "  • Camera and PC are on the same subnet\n"
                    "  • No other software (SpinView, RevealIR) holds CCP control\n"
                    "  • Firewall allows inbound UDP\n"
                    "  Tip: Camera(interface_ip='<your NIC IP>')"
                )
            cam_info = found[0]
            self.ip     = cam_info["ip"]
            self.serial = cam_info.get("serial", "")
            self.model  = cam_info.get("model", "")
            if not self.interface_ip:
                # Reuse the NIC that received the discovery reply for
                # control and streaming.
                self.interface_ip = cam_info.get("interface_ip", "")
            logger.info(
                "Found: %s %s  serial=%s  at %s",
                cam_info.get("manufacturer", ""),
                cam_info.get("model", ""),
                cam_info.get("serial", ""),
                self.ip,
            )

        if not self.interface_ip:
            # Camera given by explicit IP: sweep all interfaces and bind the
            # one whose discovery reply matches. OS routing can pick the
            # wrong NIC for link-local cameras on hosts with several
            # adapters (VPNs, secondary NICs). _local_ip() stays as the
            # fallback for cameras on routed subnets the sweep cannot see.
            try:
                for info in GVCPClient.discover(timeout=self._timeout):
                    if info.get("ip") == self.ip:
                        self.interface_ip = info.get("interface_ip") or ""
                        break
            except Exception:
                pass

        import time as _time
        local_ip = self._local_ip()
        # pyGigEVision's connect() already polls for ACCESS_DENIED up to 15 s.
        # FLIR cameras can have a heartbeat timeout up to 60 s, so we recreate
        # the client and retry beyond that limit.
        _total_wait = 90.0
        _deadline = _time.monotonic() + _total_wait
        attempt = 0
        while True:
            self._gvcp = GVCPClient(self.ip, local_ip=local_ip, timeout=self._timeout)
            try:
                self._gvcp.connect()
                break
            except GVCPError as exc:
                if _time.monotonic() >= _deadline:
                    raise CameraError(
                        f"Could not take control of camera at {self.ip}: {exc}\n"
                        "Another client may hold exclusive access; close FLIR software "
                        f"or wait for the heartbeat to expire (waited {_total_wait:.0f}s)."
                    ) from exc
                attempt += 1
                logger.debug("CCP attempt %d failed (%s), retrying…", attempt, exc)
                _time.sleep(1.0)
        logger.info("Connected to %s via %s", self.ip, local_ip)

        # Auto-load a cached GenICam XML so model/serial are correct immediately.
        # Search docs/ then the working directory for camera_*.xml files.
        if not self._nodes:
            import glob as _glob
            candidates = (
                _glob.glob("docs/camera_*.xml")
                + _glob.glob("camera_*.xml")
            )
            if candidates:
                try:
                    self.load_xml(candidates[0])
                    logger.info("Auto-loaded cached XML: %s", candidates[0])
                except Exception as exc:
                    logger.debug("Auto-load XML failed: %s", exc)

    def disconnect(self) -> None:
        """Stop streaming (if active), release CCP control, close sockets."""
        if self._streaming:
            self.stop_stream()
        if self._gvcp:
            self._gvcp.disconnect()
            self._gvcp = None
        logger.info("Disconnected.")

    @property
    def is_connected(self) -> bool:
        """Whether the camera GVCP session is active."""
        return self._gvcp is not None

    @property
    def is_streaming(self) -> bool:
        """Whether GVSP streaming is currently active."""
        return self._streaming

    @property
    def resolution(self) -> tuple[int, int]:
        """Current image resolution as (width, height) in pixels."""
        return (self.width or 0, self.height or 0)

    # ------------------------------------------------------------------
    # GenICam XML
    # ------------------------------------------------------------------

    def download_xml(self, save_path: Optional[str] = None) -> bytes:
        """Download the GenICam XML from the camera and save it to disk.

        Uses pyGigEVision to fetch and decompress the descriptor stored
        in the camera's on-board memory (bootstrap register 0x0200).

        Args:
            save_path: Output file path. Defaults to camera_<serial>.xml.

        Returns:
            Raw XML bytes.
        """
        self._require_connected()
        xml_bytes, xml_filename = fetch_genicam_xml(self._gvcp)

        if save_path is None:
            tag = self.serial or (self.ip.replace(".", "_") if self.ip else "unknown")
            save_path = f"camera_{tag}.xml"

        Path(save_path).write_bytes(xml_bytes)
        logger.info(
            "Saved %d bytes of GenICam XML (%s) → %s",
            len(xml_bytes), xml_filename, save_path,
        )
        return xml_bytes

    def load_xml(self, xml_path: str) -> None:
        """Load and parse a previously saved GenICam XML file.

        After loading, width/height are read from the camera registers
        and feature aliases are built for SFNC compatibility.

        Args:
            xml_path: Path to the GenICam XML file.
        """
        xml_bytes = Path(xml_path).read_bytes()
        self._nodes = parse_genicam_xml(xml_bytes)
        logger.info("Loaded %d register nodes from %s", len(self._nodes), xml_path)

        # Build SFNC → camera-specific alias table
        self._aliases = {}
        for sfnc, candidates in _SFNC_CANDIDATES.items():
            for name in candidates:
                if name in self._nodes:
                    if name != sfnc:
                        self._aliases[sfnc] = name
                        logger.debug("Alias: %s → %s", sfnc, name)
                    break

        self._require_connected()
        try:
            self.width  = self.read_int("Width")
            self.height = self.read_int("Height")
            logger.info("Image size: %d × %d", self.width, self.height)
        except (KeyError, CameraError) as exc:
            logger.warning("Could not read image dimensions: %s", exc)

        # Detect FLIR metadata rows (e.g. A6751sc reports Height=513 but
        # the detector is 512 rows; the extra row contains per-frame telemetry).
        self._metadata_rows = 0
        sensor_h_node = self._nodes.get("SensorHeight")
        if sensor_h_node is None:
            # Fall back to FLIR bootstrap register 0x4E058004
            try:
                sensor_h = self._gvcp.read_reg(0x4E058004)
                if sensor_h and self.height and sensor_h < self.height:
                    self._metadata_rows = self.height - sensor_h
                    self.height = sensor_h
                    logger.info(
                        "Detected %d metadata row(s); effective height → %d",
                        self._metadata_rows, self.height,
                    )
            except Exception:
                pass

        # Populate model / serial from StringReg nodes when not already set
        # by discovery.  Try SFNC names first, then FLIR-specific aliases.
        for attr, candidates in (
            # FLIR-specific registers hold the product name (e.g. "A6751sc");
            # DeviceModelName is the platform / firmware family name ("Xsc Series").
            # Always overwrite; GVCP discovery values are less specific than XML registers.
            ("model",  ["CameraModel", "MfgDeviceModelName", "DeviceModelName"]),
            ("serial", ["CameraSerial", "DeviceSerialNumber", "DeviceID"]),
        ):
            for feat in candidates:
                node = self._nodes.get(feat)
                if node is None or node.node_type != "StringReg":
                    continue
                try:
                    raw = self._gvcp.read_mem(node.address, node.length)
                    value = raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()
                    if value:
                        setattr(self, attr, value)
                        break
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def start_stream(
        self,
        packet_size: int = DEFAULT_PACKET_SIZE,
        packet_delay: int = 0,
    ) -> None:
        """Configure stream channel 0 and send AcquisitionStart.

        Args:
            packet_size: GVSP packet size in bytes (≤1500 for standard
                Ethernet; up to 9000 for jumbo frames if both ends support it).
            packet_delay: Inter-packet delay in timestamp ticks. 0 = max speed.

        Raises:
            CameraError: If not connected or image dimensions are unknown.
        """
        self._require_connected()
        if self._streaming:
            return

        if self.width is None or self.height is None:
            raise CameraError("Image dimensions unknown; call load_xml() first.")

        local_ip = self._local_ip()

        # FLIR cameras send Mono16 data in big-endian byte order;
        # byteswap=True corrects for little-endian host systems.
        self._gvsp = GVSPReceiver(
            local_ip=local_ip,
            gvcp_client=self._gvcp,
            packet_size=packet_size,
            byteswap=True,
        )
        self._gvsp.start()
        dest_port = self._gvsp.port
        logger.debug("GVSP receiver on %s:%d", local_ip, dest_port)

        dest_ip_int = struct.unpack(">I", socket.inet_aton(local_ip))[0]
        self._gvcp.write_reg(REG_SC_HOST_PORT,    dest_port)
        self._gvcp.write_reg(REG_SC_PACKET_SIZE,  packet_size & 0xFFFF)
        self._gvcp.write_reg(REG_SC_PACKET_DELAY, packet_delay)
        self._gvcp.write_reg(REG_SC_DEST_ADDR,    dest_ip_int)

        self.execute_command("AcquisitionStart")
        self._streaming = True
        logger.info("Acquisition started.")

    def stop_stream(self) -> None:
        """Send AcquisitionStop and shut down the GVSP receiver."""
        if not self._streaming:
            return
        try:
            self.execute_command("AcquisitionStop")
        except Exception as exc:
            logger.warning("AcquisitionStop failed: %s", exc)
        # Zero out stream channel so camera stops sending
        try:
            self._gvcp.write_reg(REG_SC_HOST_PORT, 0)
        except Exception:
            pass
        self._streaming = False
        if self._gvsp:
            self._gvsp.stop()
            self._gvsp.close()
            self._gvsp = None
        logger.info("Acquisition stopped.")

    # ------------------------------------------------------------------
    # Frame acquisition
    # ------------------------------------------------------------------

    def _strip_metadata(self, frame: np.ndarray) -> np.ndarray:
        """Strip trailing metadata rows and cache them in last_metadata_rows."""
        if self._metadata_rows and frame.shape[0] > self._metadata_rows:
            self.last_metadata_rows = frame[-self._metadata_rows:]
            return frame[:-self._metadata_rows]
        return frame

    def grab(self, timeout: float = 5.0) -> np.ndarray:
        """Start streaming, capture one frame, stop streaming, and return it.

        Convenience for single-shot capture. XML must be loaded first.

        Args:
            timeout: Seconds to wait for a frame.

        Returns:
            2D numpy array (H, W), dtype uint16.
        """
        self.start_stream()
        try:
            return self.read(timeout=timeout)
        finally:
            self.stop_stream()

    def read(self, timeout: float = 5.0, latest: bool = False) -> np.ndarray:
        """Return a frame from the live stream as a numpy array (H × W).

        Args:
            timeout: Seconds to wait for a frame before raising CameraError.
            latest: If True, drain the queue and return only the most recent
                frame. Use in live-display loops to prevent lag from building up.

        Returns:
            2D numpy array (H, W), dtype uint16.

        Raises:
            CameraError: If not streaming or no frame arrives in time.
        """
        if not self._streaming or self._gvsp is None:
            raise CameraError("Not streaming; call start_stream() first.")

        if latest:
            frame = None
            while True:
                candidate = self._gvsp.get_frame(
                    timeout=timeout if frame is None else 0.0
                )
                if candidate is None:
                    break
                frame = candidate
            if frame is None:
                raise CameraError(
                    f"No frame received within {timeout:.1f} s. Check:\n"
                    "  • Firewall allows inbound UDP on the listen port\n"
                    "  • SC_PACKET_SIZE ≤ network MTU\n"
                    "  • Camera is not in trigger mode"
                )
            return self._strip_metadata(frame)

        frame = self._gvsp.get_frame(timeout=timeout)
        if frame is None:
            raise CameraError(
                f"No frame received within {timeout:.1f} s. Check:\n"
                "  • Firewall allows inbound UDP on the listen port\n"
                "  • SC_PACKET_SIZE ≤ network MTU\n"
                "  • Camera is not in trigger mode"
            )
        return self._strip_metadata(frame)

    def acquire(self, n_frames: int, timeout: float = 30.0) -> List[np.ndarray]:
        """Capture exactly ``n_frames`` frames and return them as a list.

        If streaming is already active, reads from the live stream. Otherwise
        starts it automatically, captures, then stops::

            frames = cam.acquire(50)   # no start_stream() / stop_stream() needed

        Args:
            n_frames: Number of frames to capture.
            timeout: Total seconds to wait before raising CameraError.

        Returns:
            List of (H, W) numpy arrays, dtype uint16.
        """
        managed = not self._streaming
        if managed:
            self.start_stream()
        try:
            frames = []
            deadline = time.monotonic() + timeout
            while len(frames) < n_frames:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CameraError(
                        f"Timeout: acquired {len(frames)}/{n_frames} frames "
                        f"in {timeout:.1f} s"
                    )
                frame = self._gvsp.get_frame(timeout=min(remaining, 2.0))
                if frame is not None:
                    frames.append(self._strip_metadata(frame))
            return frames
        finally:
            if managed:
                self.stop_stream()

    # ------------------------------------------------------------------
    # Feature access (requires XML loaded)
    # ------------------------------------------------------------------

    def read_int(self, feature: str) -> int:
        """Read an Integer register feature from the camera."""
        node = self._get_node(feature)
        raw = self._gvcp.read_reg(node.address)
        if node.sign == "Signed":
            raw = struct.unpack(">i", struct.pack(">I", raw))[0]
        return raw

    def write_int(self, feature: str, value: int) -> None:
        """Write an Integer register feature."""
        node = self._get_node(feature)
        self._gvcp.write_reg(node.address, value & 0xFFFFFFFF)

    def read_float(self, feature: str) -> float:
        """Read a Float register feature (32-bit IEEE 754)."""
        node = self._get_node(feature)
        return self._gvcp.read_float(node.address)

    def write_float(self, feature: str, value: float) -> None:
        """Write a Float register feature."""
        node = self._get_node(feature)
        self._gvcp.write_float(node.address, value)

    def execute_command(self, feature: str) -> None:
        """Execute a Command register feature (e.g. AcquisitionStart)."""
        node = self._get_node(feature)
        cmd_value = node.cmd_value if node.cmd_value is not None else 1
        self._gvcp.write_reg(node.address, cmd_value)

    def read_enum(self, feature: str) -> str:
        """Read an Enumeration feature and return the entry name."""
        node = self._get_node(feature)
        raw = self._gvcp.read_reg(node.address)
        rev = {v: k for k, v in node.enum_entries.items()}
        return rev.get(raw, f"<unknown:{raw}>")

    def write_enum(self, feature: str, entry_name: str) -> None:
        """Write an Enumeration feature by entry name."""
        node = self._get_node(feature)
        if entry_name not in node.enum_entries:
            valid = list(node.enum_entries.keys())
            raise CameraError(
                f"Invalid enum value '{entry_name}' for '{feature}'. "
                f"Valid values: {valid}"
            )
        self._gvcp.write_reg(node.address, node.enum_entries[entry_name])

    def list_features(self) -> Dict[str, RegNode]:
        """Return the full register map loaded from GenICam XML."""
        return self._nodes

    # ------------------------------------------------------------------
    # Properties for common settings
    # ------------------------------------------------------------------

    @property
    def frame_rate(self) -> float:
        """Acquisition frame rate in Hz."""
        return self.read_float("AcquisitionFrameRate")

    @frame_rate.setter
    def frame_rate(self, fps: float) -> None:
        max_fps = self.frame_rate_max
        if max_fps is not None and fps > max_fps:
            raise CameraError(
                f"Requested frame rate {fps:.1f} Hz exceeds camera maximum "
                f"{max_fps:.1f} Hz for the current ROI."
            )
        self.write_float("AcquisitionFrameRate", fps)

    @property
    def frame_rate_max(self) -> Optional[float]:
        """Maximum frame rate for the current ROI (read-only, from camera)."""
        return self.get_max_frame_rate()

    @property
    def exposure_ms(self) -> float:
        """Integration time in milliseconds."""
        return self.read_float("ExposureTime") * 1e3

    @exposure_ms.setter
    def exposure_ms(self, ms: float) -> None:
        self.write_float("ExposureTime", ms / 1e3)

    @property
    def detector_temperature(self) -> float:
        """Detector (FPA) temperature in degrees Celsius."""
        return self.read_float("DeviceTemperature")

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get_exposure(self) -> float:
        """Return current integration time in seconds."""
        return self.read_float("ExposureTime")

    def set_exposure(self, seconds: float) -> None:
        """Set integration time in seconds."""
        self.write_float("ExposureTime", seconds)

    def get_frame_rate(self) -> float:
        """Return current acquisition frame rate in Hz."""
        return self.frame_rate

    def set_frame_rate(self, fps: float) -> None:
        """Set acquisition frame rate in Hz."""
        self.frame_rate = fps

    def get_width(self) -> int:
        return self.read_int("Width")

    def set_width(self, pixels: int) -> None:
        self.write_int("Width", pixels)

    def get_height(self) -> int:
        return self.read_int("Height")

    def set_height(self, pixels: int) -> None:
        self.write_int("Height", pixels)

    def get_temperature(self) -> float:
        """Return detector temperature in degrees Celsius."""
        return self.detector_temperature

    def get_max_frame_rate(self) -> Optional[float]:
        """Return camera's max frame rate for the current ROI, or None."""
        try:
            return self.read_float("AcquisitionFrameRateMax")
        except (KeyError, Exception):
            return None

    # ------------------------------------------------------------------
    # Calibration blocks (temperature range selection)
    # ------------------------------------------------------------------

    def get_calibration_blocks(self) -> List[dict]:
        """Return a list of all calibration blocks with their temperature ranges.

        Each entry has keys: ``index``, ``name``, ``lens``, ``tmin``, ``tmax``.
        Temporarily iterates all blocks and restores the original selection.
        """
        self._require_connected()
        n_max   = self._gvcp.read_reg(reg.REG_CAL_INDEX_MAX)
        current = self._gvcp.read_reg(reg.REG_CAL_INDEX)
        blocks  = []
        for i in range(n_max + 1):
            self._gvcp.write_reg(reg.REG_CAL_INDEX, i)
            tmin = self._gvcp.read_float(reg.REG_CAL_TMIN)
            tmax = self._gvcp.read_float(reg.REG_CAL_TMAX)

            def _readstr(addr: int) -> str:
                try:
                    raw = self._gvcp.read_mem(addr, 64)
                    return raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()
                except Exception:
                    return ""

            name = _readstr(reg.REG_CAL_NAME)
            lens = _readstr(reg.REG_CAL_LENS)
            blocks.append({"index": i, "tmin": tmin, "tmax": tmax,
                           "name": name, "lens": lens})
        self._gvcp.write_reg(reg.REG_CAL_INDEX, current)
        return blocks

    def get_calibration_block(self) -> int:
        """Return the currently active calibration block index."""
        self._require_connected()
        return self._gvcp.read_reg(reg.REG_CAL_INDEX)

    def set_calibration_block(self, index: int) -> None:
        """Select a calibration block (temperature range)."""
        self._require_connected()
        self._gvcp.write_reg(reg.REG_CAL_INDEX, index)

    # ------------------------------------------------------------------
    # Radiometry parameters
    # ------------------------------------------------------------------

    def get_emissivity(self) -> float:
        return self.read_float("Emissivity")

    def set_emissivity(self, value: float) -> None:
        self.write_float("Emissivity", value)

    def get_object_params(self) -> dict:
        """Return radiometry object parameters as a dict."""
        return {
            "emissivity":          self.read_float("Emissivity"),
            "object_distance_m":   self.read_float("ObjectDistance"),
            "atmospheric_temp_K":  self.read_float("AtmosphericTemperature"),
            "reflected_temp_K":    self.read_float("ReflectedTemperature"),
            "relative_humidity":   self.read_float("RelativeHumidity"),
        }

    def set_object_params(
        self,
        emissivity: Optional[float] = None,
        distance_m: Optional[float] = None,
        atm_temp_K: Optional[float] = None,
        refl_temp_K: Optional[float] = None,
        humidity: Optional[float] = None,
    ) -> None:
        """Set radiometry object parameters (any subset)."""
        if emissivity  is not None: self.write_float("Emissivity",             emissivity)
        if distance_m  is not None: self.write_float("ObjectDistance",         distance_m)
        if atm_temp_K  is not None: self.write_float("AtmosphericTemperature", atm_temp_K)
        if refl_temp_K is not None: self.write_float("ReflectedTemperature",   refl_temp_K)
        if humidity    is not None: self.write_float("RelativeHumidity",        humidity)

    # ------------------------------------------------------------------
    # ROI / Resolution
    # ------------------------------------------------------------------

    def get_roi(self) -> dict:
        """Return current ROI as ``{width, height, offset_x, offset_y}``.

        ``height`` reflects the usable image rows only; any trailing metadata
        rows appended by the camera firmware are excluded.
        """
        self._require_connected()
        return {
            "width":    self.read_int("Width"),
            "height":   self.read_int("Height") - self._metadata_rows,
            "offset_x": self._gvcp.read_reg(reg.REG_OFFSET_X),
            "offset_y": self._gvcp.read_reg(reg.REG_OFFSET_Y),
        }

    def get_roi_limits(self) -> dict:
        """Return ``{width_min, width_inc, height_min, height_inc, sensor_width, sensor_height}``.

        All height values are in usable image rows (metadata rows excluded).
        """
        self._require_connected()
        h_min_raw = self._gvcp.read_reg(reg.REG_HEIGHT_MIN)
        return {
            "width_min":     self._gvcp.read_reg(reg.REG_WIDTH_MIN),
            "width_inc":     self._gvcp.read_reg(reg.REG_WIDTH_INC),
            # REG_HEIGHT_MIN counts total rows (image + metadata); subtract so
            # the returned value represents the minimum usable image rows.
            "height_min":    max(1, h_min_raw - self._metadata_rows),
            "height_inc":    self._gvcp.read_reg(reg.REG_HEIGHT_INC),
            "sensor_width":  reg.SENSOR_WIDTH,
            "sensor_height": reg.SENSOR_HEIGHT,
        }

    def set_roi(
        self,
        width: int,
        height: int,
        offset_x: int = 0,
        offset_y: int = 0,
    ) -> None:
        """Set the acquisition ROI. Stream must be stopped first.

        Width and height are validated against the camera's increment
        constraints before being written. After writing, the max frame
        rate register updates automatically.

        Args:
            width:    ROI width in pixels (must be a multiple of WidthInc).
            height:   ROI height in pixels.
            offset_x: Horizontal offset from left edge of sensor.
            offset_y: Vertical offset from top edge of sensor.
        """
        self._require_connected()
        if self._streaming:
            raise CameraError("Stop the stream before changing ROI.")

        limits = self.get_roi_limits()
        w_min = limits["width_min"]
        w_inc = limits["width_inc"]
        h_min = limits["height_min"]
        h_inc = limits["height_inc"]
        w_max = limits["sensor_width"]
        h_max = limits["sensor_height"]

        if width < w_min:
            raise CameraError(f"Width {width} is below the minimum {w_min}.")
        if w_inc > 0 and (width % w_inc) != 0:
            raise CameraError(
                f"Width {width} is not a multiple of the increment {w_inc}."
            )
        if width + offset_x > w_max:
            raise CameraError(
                f"Width {width} + offset_x {offset_x} = {width + offset_x} "
                f"exceeds sensor width {w_max}."
            )
        if height < h_min:
            raise CameraError(f"Height {height} is below the minimum {h_min}.")
        if h_inc > 0 and ((height - h_min) % h_inc) != 0:
            raise CameraError(
                f"Height {height} does not satisfy the increment constraint "
                f"(height − {h_min}) must be a multiple of {h_inc}."
            )
        if height + offset_y > h_max:
            raise CameraError(
                f"Height {height} + offset_y {offset_y} = {height + offset_y} "
                f"exceeds sensor height {h_max}."
            )

        self._gvcp.write_reg(reg.REG_OFFSET_X, offset_x)
        self._gvcp.write_reg(reg.REG_OFFSET_Y, offset_y)
        self.write_int("Width", width)
        # Write image rows + metadata rows so the camera gets the correct total
        self.write_int("Height", height + self._metadata_rows)
        self.width  = self.read_int("Width")
        self.height = self.read_int("Height") - self._metadata_rows
        max_fps = self.frame_rate_max
        fps_str = f"  max FPS now: {max_fps:.1f} Hz" if max_fps else ""
        logger.info(
            "ROI set: %d×%d  offset (%d,%d)%s",
            self.width, self.height, offset_x, offset_y, fps_str,
        )

    # ------------------------------------------------------------------
    # NUC (Non-Uniformity Correction)
    # ------------------------------------------------------------------

    def trigger_nuc(self, preset: int = 0) -> None:
        """Apply the stored NUC correction coefficients for the given preset.

        Loads pre-computed correction data (PS0–PS3) into the active pipeline.
        Does not compute a new NUC; to do that, move the physical flag into
        the FOV, capture a flat-field frame, then save and re-apply.

        Args:
            preset: Preset index to correct (0–3). Default 0.
        """
        self._require_connected()
        if preset not in reg.REG_NUC_LOAD:
            raise CameraError(f"Invalid preset {preset}. Must be 0–3.")
        self._gvcp.write_reg(reg.REG_NUC_LOAD[preset], 1)
        logger.info("NUC correction applied for preset %d.", preset)

    def flag_move_in_fov(self) -> None:
        """Move the internal NUC flag into the field of view.

        Only available on cameras equipped with a physical shutter
        (e.g. FLIR A6751sc). Call before capturing a flat-field reference.
        """
        self._require_connected()
        self._gvcp.write_reg(reg.REG_FLAG_IN_FOV, 1)
        logger.info("NUC flag commanded into FOV.")

    def flag_move_stowed(self) -> None:
        """Move the internal NUC flag out of the field of view."""
        self._require_connected()
        self._gvcp.write_reg(reg.REG_FLAG_STOWED, 1)
        logger.info("NUC flag commanded to stowed position.")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_temperatures(self) -> Dict[str, float]:
        """Read all available on-board temperature sensors.

        Returns a dict with keys ``FPA``, ``Digitizer``, ``PowerBoard``,
        ``FrontPanel`` (degrees Celsius). The selector register is
        restored to its original value after reading.
        """
        self._require_connected()
        original = self._gvcp.read_reg(reg.REG_TEMP_SELECTOR)
        temps: Dict[str, float] = {}
        try:
            for name, idx in reg.TEMP_SENSORS.items():
                self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, idx)
                temps[name] = self._gvcp.read_float(reg.REG_TEMP_VALUE)
        finally:
            self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, original)
        return temps

    def info(self) -> dict:
        """Return a dict of the camera's current state.

        Includes IP, model, serial, ROI, frame rate, exposure,
        calibration block, detector temperature, and streaming status.
        """
        out: dict = {
            "ip":        self.ip,
            "model":     self.model,
            "serial":    self.serial,
            "streaming": self._streaming,
        }
        if not self._nodes:
            return out

        def _safe(fn):
            try:
                return fn()
            except Exception:
                return None

        out["width"]             = _safe(lambda: self.read_int("Width"))
        out["height"]            = _safe(lambda: self.read_int("Height") - self._metadata_rows)
        out["frame_rate_hz"]     = _safe(
            lambda: round(self.read_float("AcquisitionFrameRate"), 2)
        )
        fmax = self.get_max_frame_rate()
        out["frame_rate_max_hz"] = round(fmax, 2) if fmax is not None else None
        out["exposure_ms"]       = _safe(
            lambda: round(self.read_float("ExposureTime") * 1e3, 3)
        )
        out["detector_temp_C"]   = _safe(
            lambda: round(self.read_float("DeviceTemperature"), 2)
        )
        out["calibration_block"] = _safe(
            lambda: self._gvcp.read_reg(reg.REG_CAL_INDEX)
        )
        return out

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------

    def live_view(self, colormap: str = "inferno", scale: int = 2) -> None:
        """Open a live thermal image viewer window.

        Requires the 'gui' extra: ``pip install pyflir[gui]``

        XML must be loaded first (for width/height). If streaming is
        already active it will be stopped and restarted by the viewer.

        Args:
            colormap: Matplotlib colormap name (default "inferno").
            scale: Display upscale factor (default 2 = double size).
        """
        from .gui import LiveView
        viewer = LiveView(self, colormap=colormap, scale=scale)
        viewer.run()

    # ------------------------------------------------------------------
    # Low-level register access (direct)
    # ------------------------------------------------------------------

    def read_register(self, addr: int) -> int:
        """Read a raw 32-bit register value."""
        self._require_connected()
        return self._gvcp.read_reg(addr)

    def write_register(self, addr: int, value: int) -> None:
        """Write a raw 32-bit register value."""
        self._require_connected()
        self._gvcp.write_reg(addr, value)

    def read_float_register(self, addr: int) -> float:
        """Read a register as IEEE 754 float."""
        self._require_connected()
        return self._gvcp.read_float(addr)

    def write_float_register(self, addr: int, value: float) -> None:
        """Write a register as IEEE 754 float."""
        self._require_connected()
        self._gvcp.write_float(addr, value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._gvcp is None:
            raise CameraError("Not connected; call connect() first.")

    def _get_node(self, feature: str) -> RegNode:
        if not self._nodes:
            raise CameraError("Register map not loaded; call load_xml() first.")
        resolved = self._aliases.get(feature, feature)
        if resolved not in self._nodes:
            raise KeyError(f"Feature '{feature}' not found in register map.")
        return self._nodes[resolved]

    def _local_ip(self) -> str:
        """Return the local IP that would be used to reach the camera."""
        if self.interface_ip:
            return self.interface_ip
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((self.ip, 3956))
            return s.getsockname()[0]


# ---------------------------------------------------------------------------
# Module-level discover() convenience function
# ---------------------------------------------------------------------------

def discover(
    interface_ip: Optional[str] = None,
    timeout: float = 2.0,
) -> List[Dict]:
    """Discover GigE Vision cameras on the network.

    Args:
        interface_ip: Bind to this local IP to target a specific NIC.
            Omit to broadcast on all interfaces.
        timeout: Seconds to wait for discovery replies.

    Returns:
        List of dicts with keys: ip, mac, spec_version, manufacturer,
        model, device_version, manufacturer_info, serial, user_name,
        interface_ip (the local NIC that received the reply).

    Example::

        import pyflir
        cameras = pyflir.discover(interface_ip="169.254.100.1")
        for cam in cameras:
            print(cam["manufacturer"], cam["model"], cam["ip"])
    """
    return GVCPClient.discover(interface_ip=interface_ip or "", timeout=timeout)
