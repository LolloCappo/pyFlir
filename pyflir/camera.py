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

import contextlib
import logging
import socket
import struct
import time
import warnings
from pathlib import Path

import numpy as np
from pyGigEVision import GVCPClient, GVCPError, GVSPReceiver
from pyGigEVision.standard import (
    REG_SC_DEST_ADDR,
    REG_SC_HOST_PORT,
    REG_SC_PACKET_DELAY,
    REG_SC_PACKET_SIZE,
)

from . import registers as reg
from .genicam import RegNode, fetch_genicam_xml, parse_genicam_xml

logger = logging.getLogger(__name__)

# Default MTU-safe packet size for 1 GbE without jumbo frames
DEFAULT_PACKET_SIZE = 1500

# SFNC → FLIR-camera-specific feature name aliases.
# Populated by load_xml() after inspecting available node names.
_SFNC_CANDIDATES = {
    "Width": ["Width", "WidthReg"],
    "Height": ["Height", "HeightReg"],
    "PixelFormat": ["PixelFormat", "PixelFormatReg"],
    "AcquisitionStart": ["AcquisitionStart", "AcquisitionStartReg"],
    "AcquisitionStop": ["AcquisitionStop", "AcquisitionStopReg"],
    "AcquisitionMode": ["AcquisitionMode", "AcquisitionModeReg"],
    "ExposureTime": ["ExposureTime", "PS0IntegrationTimeReg", "IntegrationTimeReg"],
    "AcquisitionFrameRate": [
        "AcquisitionFrameRate",
        "PS0FrameRateReg",
        "FrameRateReg",
        "SuperframeRateReg",
    ],
    "AcquisitionFrameRateMax": [
        "AcquisitionFrameRateMax",
        "PS0FrameRateMaxReg",
        "FrameRateMaxReg",
    ],
    # No FLIR-specific fallback here: "FPAColdReg" looks like a temperature
    # register by name but actually backs a Boolean status flag ("FPACold"),
    # not a Float feature -- reading it with read_float() silently
    # reinterprets a 0/1 integer as an IEEE-754 float. detector_temperature
    # uses the selector-based get_temperatures()["FPA"] instead, which reads
    # the real live FPA sensor correctly on every camera tested so far.
    "DeviceTemperature": ["DeviceTemperature"],
    "Emissivity": ["ObjectEmissivityReg"],
    "ObjectDistance": ["ObjectDistanceReg"],
    "AtmosphericTemperature": ["AtmosphericTemperatureReg"],
    "ReflectedTemperature": ["ReflectedTemperatureReg"],
    "RelativeHumidity": ["RelativeHumidityReg"],
    "CalibrationBlock": ["CalibrationQueryIndexReg"],
}


class CameraError(Exception):
    """Raised for high-level camera operation failures."""


def _find_matching_interface(camera_ip: str, timeout: float = 1.0) -> str:
    """Return the local interface that can actually reach *camera_ip*.

    ``GVCPClient.discover()`` sweeps every local interface internally and
    returns one deduped entry per camera IP, picking whichever socket's
    reply arrived first. That is not necessarily the interface on the
    camera's own subnet: GVCP control replies go straight back to the
    request's source address regardless of subnet match, so a host with
    several interfaces (VPN, secondary NIC, stale static IP left on the
    camera's adapter) can have its discovery reply "won" by the wrong one.
    GVSP streaming, which the camera itself originates, then silently fails
    since it has no real route to that address.

    Re-derives the correct interface by querying each local interface
    individually via ``discover(interface_ip=...)`` (a single socket bound
    to one address -- no cross-interface race to get wrong), preferring
    same-subnet candidates so the common case resolves in one round trip.

    Parameters
    ----------
    camera_ip : str
        IPv4 address of the camera to find a route to.
    timeout : float, optional
        Seconds to wait for each per-interface discovery attempt.

    Returns
    -------
    str
        The local interface IP that got a reply from *camera_ip*, or ``""``
        if none did (falls back to OS routing via :meth:`Camera._local_ip`).
    """
    import ipaddress

    import psutil

    candidates: list[tuple[str, str | None]] = []
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st is None or not st.isup:
            continue
        for a in addrs:
            if a.family == socket.AF_INET and a.address and not a.address.startswith("127."):
                candidates.append((a.address, a.netmask))

    def _same_subnet(ip: str, netmask: str | None) -> bool:
        if not netmask:
            return False
        try:
            return ipaddress.IPv4Address(camera_ip) in ipaddress.IPv4Network(
                f"{ip}/{netmask}", strict=False
            )
        except ValueError:
            return False

    candidates.sort(key=lambda c: not _same_subnet(*c))

    for ip, _netmask in candidates:
        try:
            found = GVCPClient.discover(interface_ip=ip, timeout=timeout)
        except Exception:
            continue
        if any(info.get("ip") == camera_ip for info in found):
            return ip
    return ""


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
        ip: str | None = None,
        interface_ip: str | None = None,
        timeout: float = 2.0,
    ):
        self.ip = ip
        self.interface_ip = interface_ip or ""
        self._timeout = timeout

        self._gvcp: GVCPClient | None = None
        self._gvsp: GVSPReceiver | None = None
        self._nodes: dict[str, RegNode] = {}
        self._aliases: dict[str, str] = {}
        self._streaming: bool = False

        # Populated after load_xml() or connect()
        self.width: int | None = None
        self.height: int | None = None
        self.serial: str = ""
        self.model: str = ""
        # Number of trailing metadata rows the camera appends to each frame.
        # These rows are stripped from grab()/read() results and exposed via
        # the last_metadata_rows attribute after each acquisition.
        self._metadata_rows: int = 0
        self.last_metadata_rows: np.ndarray | None = None

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
            w = _s(lambda: self.read_int("Width"))
            h = _s(lambda: self.read_int("Height"))
            fps = _s(lambda: f"{self.read_float('AcquisitionFrameRate'):.1f} Hz")
            fmax = _s(lambda: f"{self.get_max_frame_rate():.1f} Hz")
            exp = _s(lambda: f"{self.read_float('ExposureTime') * 1e3:.3f} ms")
            temp = _s(lambda: f"{self.detector_temperature:.1f} °C")
            cal = _s(lambda: self._gvcp.read_reg(reg.REG_CAL_INDEX))
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
            found = GVCPClient.discover(interface_ip=self.interface_ip, timeout=3.0)
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
            self.ip = cam_info["ip"]
            self.serial = cam_info.get("serial", "")
            self.model = cam_info.get("model", "")
            logger.info(
                "Found: %s %s  serial=%s  at %s",
                cam_info.get("manufacturer", ""),
                cam_info.get("model", ""),
                cam_info.get("serial", ""),
                self.ip,
            )

        if not self.interface_ip:
            # Don't trust discover()'s own interface_ip field here: it
            # dedupes multiple replies for the same camera down to one
            # entry by whichever local socket answered first, which is not
            # necessarily the interface actually on the camera's subnet
            # (GVCP control replies reach the request's source address
            # regardless of subnet match; only GVSP streaming, which the
            # camera itself originates, needs a real route). Re-derive it
            # by querying each local interface individually. _local_ip()
            # stays as the fallback for cameras on routed subnets this
            # can't see.
            with contextlib.suppress(Exception):
                self.interface_ip = _find_matching_interface(self.ip, timeout=self._timeout)

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

            candidates = _glob.glob("docs/camera_*.xml") + _glob.glob("camera_*.xml")
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

    def download_xml(self, save_path: str | None = None) -> bytes:
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
            len(xml_bytes),
            xml_filename,
            save_path,
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
            self.width = self.read_int("Width")
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
                        self._metadata_rows,
                        self.height,
                    )
            except Exception:
                pass

        # Populate model / serial from StringReg nodes when not already set
        # by discovery.  Try SFNC names first, then FLIR-specific aliases.
        for attr, candidates in (
            # FLIR-specific registers hold the product name (e.g. "A6751sc");
            # DeviceModelName is the platform / firmware family name ("Xsc Series").
            # Always overwrite; GVCP discovery values are less specific than XML registers.
            ("model", ["CameraModel", "MfgDeviceModelName", "DeviceModelName"]),
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
        self._gvcp.write_reg(REG_SC_HOST_PORT, dest_port)
        self._gvcp.write_reg(REG_SC_PACKET_SIZE, packet_size & 0xFFFF)
        self._gvcp.write_reg(REG_SC_PACKET_DELAY, packet_delay)
        self._gvcp.write_reg(REG_SC_DEST_ADDR, dest_ip_int)

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
        with contextlib.suppress(Exception):
            self._gvcp.write_reg(REG_SC_HOST_PORT, 0)
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
            self.last_metadata_rows = frame[-self._metadata_rows :]
            return frame[: -self._metadata_rows]
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
                candidate = self._gvsp.get_frame(timeout=timeout if frame is None else 0.0)
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

    def acquire(self, n_frames: int, timeout: float = 30.0) -> list[np.ndarray]:
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
                        f"Timeout: acquired {len(frames)}/{n_frames} frames in {timeout:.1f} s"
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
                f"Invalid enum value '{entry_name}' for '{feature}'. Valid values: {valid}"
            )
        self._gvcp.write_reg(node.address, node.enum_entries[entry_name])

    def list_features(self) -> dict[str, RegNode]:
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
    def frame_rate_max(self) -> float | None:
        """Maximum frame rate for the current ROI (read-only, from camera)."""
        return self.get_max_frame_rate()

    @property
    def exposure_ms(self) -> float:
        """Integration time in milliseconds.

        The backing register on this camera family is ``PS{n}IntegrationTime``,
        which the camera's GenICam XML documents directly *in milliseconds*
        (``IntegrationTimeMax`` reads 687000 -- sensible as ms, absurd as
        seconds), NOT the SFNC-standard microseconds. So this property is a
        1:1 passthrough of the register value; no unit scaling is applied.

        On this camera family the active preset's integration time is coupled
        to its loaded factory calibration: each calibration polynomial is fit
        at one specific integration time. The camera boots with the two in
        sync. Changing the integration time away from that value (via the
        setter) desyncs the raw counts from the calibration and makes
        :meth:`counts_to_temperature` read wrong -- see the setter's warning.
        """
        return self.read_float("ExposureTime")

    @exposure_ms.setter
    def exposure_ms(self, ms: float) -> None:
        warnings.warn(
            "Setting exposure_ms changes only the integration time, not the "
            "loaded factory calibration -- and each calibration is fit at one "
            "specific integration time. Changing it away from the value the "
            "camera booted with will desync raw counts from the calibration "
            "polynomial and make counts_to_temperature() read wrong. Leave "
            "integration time at its boot value unless you also load a matching "
            "calibration. See get_calibration().",
            stacklevel=2,
        )
        # Register is in milliseconds (see the getter's docstring), so write
        # the value straight through -- no seconds->ms scaling.
        self.write_float("ExposureTime", ms)

    @property
    def detector_temperature(self) -> float:
        """Detector (FPA) temperature in degrees Celsius.

        Reads via the selector-based on-board sensor mechanism
        (:meth:`_read_temp_sensor`) rather than a single aliased register:
        on the A6751sc, the SFNC ``DeviceTemperature`` name has no direct
        match, and the FLIR-specific fallback that looked closest by name
        (``FPAColdReg``) turned out to back a Boolean status flag, not a
        temperature reading. Reads only the FPA sensor, independent of
        :meth:`get_temperatures`, so it can't fail because some other
        sensor isn't populated on this unit.
        """
        return self._read_temp_sensor(reg.TEMP_SENSORS["FPA"])

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get_exposure(self) -> float:
        """Return current integration time in seconds.

        The backing register is in milliseconds (see :attr:`exposure_ms`),
        so this divides by 1000 to honour its seconds contract.
        """
        return self.read_float("ExposureTime") / 1e3

    def set_exposure(self, seconds: float) -> None:
        """Set integration time in seconds.

        The backing register is in milliseconds (see :attr:`exposure_ms`),
        so this multiplies by 1000. Note the same calibration-coupling caveat
        as :attr:`exposure_ms`.
        """
        self.exposure_ms = seconds * 1e3

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

    def get_max_frame_rate(self) -> float | None:
        """Return camera's max frame rate for the current ROI, or None."""
        try:
            return self.read_float("AcquisitionFrameRateMax")
        except (KeyError, Exception):
            return None

    # ------------------------------------------------------------------
    # Calibration blocks (temperature range selection)
    # ------------------------------------------------------------------

    def get_calibration_blocks(self) -> list[dict]:
        """Return a list of all calibration blocks with their temperature ranges.

        Each entry has keys: ``index``, ``name``, ``lens``, ``tmin``, ``tmax``.
        Temporarily iterates all blocks and restores the original selection.
        """
        self._require_connected()
        n_max = self._gvcp.read_reg(reg.REG_CAL_INDEX_MAX)
        current = self._gvcp.read_reg(reg.REG_CAL_INDEX)
        blocks = []
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
            blocks.append({"index": i, "tmin": tmin, "tmax": tmax, "name": name, "lens": lens})
        self._gvcp.write_reg(reg.REG_CAL_INDEX, current)
        return blocks

    def _active_calibration_index(self) -> int | None:
        """Return the browse index whose tag matches the *loaded* calibration.

        ``CalibrationQueryIndex`` (``REG_CAL_INDEX``) is only a read-only
        cursor into the factory calibration library; it does not necessarily
        point at the calibration the streaming preset is actually using. The
        live calibration is named by ``PS{n}CalibrationTag`` for the active
        preset ``n``. This matches that tag back to a browse index so
        :meth:`get_calibration` can return the coefficients that genuinely
        apply to captured frames. Returns ``None`` if no block matches (the
        caller then falls back to whatever the cursor currently points at).
        The cursor is restored before returning.
        """
        self._require_connected()
        try:
            preset = self._gvcp.read_reg(reg.REG_ACTIVE_PRESET)
        except Exception:
            preset = 0
        tag_addr = reg.REG_PS_CALIBRATION_TAG.get(preset, reg.REG_PS_CALIBRATION_TAG[0])
        try:
            loaded_tag = self._read_string(tag_addr)
        except Exception:
            return None
        if not loaded_tag:
            return None
        n_max = self._gvcp.read_reg(reg.REG_CAL_INDEX_MAX)
        current = self._gvcp.read_reg(reg.REG_CAL_INDEX)
        try:
            for i in range(n_max + 1):
                self._gvcp.write_reg(reg.REG_CAL_INDEX, i)
                try:
                    if self._read_string(reg.REG_CAL_TAG) == loaded_tag:
                        return i
                except Exception:
                    continue
        finally:
            self._gvcp.write_reg(reg.REG_CAL_INDEX, current)
        return None

    def get_calibration_block(self) -> int:
        """Return the calibration block actually applied to the live stream.

        This is the browse index whose tag matches the calibration loaded on
        the active preset (``PS{n}CalibrationTag``), not merely where the
        ``CalibrationQueryIndex`` browse cursor happens to sit. Falls back to
        the raw cursor value only if no loaded-tag match is found.
        """
        self._require_connected()
        idx = self._active_calibration_index()
        if idx is not None:
            return idx
        return self._gvcp.read_reg(reg.REG_CAL_INDEX)

    def load_calibration(
        self,
        tag: str | None = None,
        index: int | None = None,
        preset: int | None = None,
        timeout: float = 10.0,
    ) -> dict:
        """Load a factory calibration into a preset (changes the live stream).

        This is the real "select a temperature range" operation. Writing
        ``CalibrationQueryIndex`` alone only moves a read-only browse cursor
        and does **not** change what the stream uses; the live calibration is
        whatever is *loaded* into the preset via ``PS{n}CalibrationLoad``. This
        method stages the calibration's tag into ``PS{n}CalibrationLoadTag``
        (written 4 bytes at a time, since pyGigEVision has no WRITEMEM -- see
        :meth:`_write_string_reg`) and executes the load command.

        Loading a factory calibration **also sets that preset's integration
        time** to the value the calibration was fit at -- the integration-time
        ↔ calibration coupling FLIR's own software enforces, and the reason you
        should not set :attr:`exposure_ms` independently. Verified live:
        loading "25mm, Empty, -20C - 55C" moved the integration time from
        0.1 ms to 2.354 ms.

        Note: loading also brings in whatever NUC correction is stored with the
        calibration, which may be stale for the current detector state. If the
        image shows heavy fixed-pattern noise afterwards, run
        :meth:`perform_nuc` to compute a fresh correction at the new
        integration time.

        Args:
            tag: Exact calibration tag to load (e.g. ``"25mm, Empty, -20C - 55C"``);
                see :meth:`get_calibration_blocks` for available tags. Mutually
                exclusive with *index*.
            index: Browse index (0..``CalibrationQueryIndexMax``) whose tag to
                load. Mutually exclusive with *tag*.
            preset: Preset to load into. Defaults to the active preset.
            timeout: Seconds to wait for the loaded tag to take effect.

        Returns:
            dict with ``preset``, ``tag`` (the tag now loaded), and
            ``exposure_ms`` (the integration time the load set).

        Raises:
            CameraError: if neither/both of *tag*/*index* are given, if the
                index has no tag, or if the load does not take effect in time.
        """
        self._require_connected()
        if (tag is None) == (index is None):
            raise CameraError("Pass exactly one of tag= or index=.")
        if preset is None:
            try:
                preset = self._gvcp.read_reg(reg.REG_ACTIVE_PRESET)
            except Exception:
                preset = 0
        if preset not in reg.REG_PS_CALIBRATION_LOAD_TAG:
            raise CameraError(f"Invalid preset {preset}; expected 0-3.")

        if index is not None:
            # Resolve the tag from the browse cursor, then restore the cursor.
            n_max = self._gvcp.read_reg(reg.REG_CAL_INDEX_MAX)
            if not (0 <= index <= n_max):
                raise CameraError(f"Calibration index {index} out of range 0-{n_max}.")
            cursor = self._gvcp.read_reg(reg.REG_CAL_INDEX)
            try:
                self._gvcp.write_reg(reg.REG_CAL_INDEX, index)
                tag = self._read_string(reg.REG_CAL_TAG)
            finally:
                self._gvcp.write_reg(reg.REG_CAL_INDEX, cursor)
            if not tag:
                raise CameraError(f"Calibration index {index} has no tag to load.")

        # Stage the tag and verify it landed byte-exactly before executing.
        self._write_string_reg(reg.REG_PS_CALIBRATION_LOAD_TAG[preset], tag)
        staged = self._read_string(reg.REG_PS_CALIBRATION_LOAD_TAG[preset])
        if staged != tag:
            raise CameraError(
                f"Failed to stage calibration tag (wrote {tag!r}, read back {staged!r})."
            )

        self._gvcp.write_reg(reg.REG_PS_CALIBRATION_LOAD[preset], 1)

        # Poll until the loaded tag reflects the request. The camera is busy
        # reconfiguring during the load and may briefly stop answering READMEM
        # (transient GVCP timeout), so treat a failed read as "not ready yet"
        # and keep polling rather than aborting.
        deadline = time.monotonic() + timeout
        loaded = ""
        while time.monotonic() < deadline:
            try:
                loaded = self._read_string(reg.REG_PS_CALIBRATION_TAG[preset])
            except GVCPError:
                loaded = ""
            if loaded == tag:
                break
            time.sleep(0.1)
        if loaded != tag:
            raise CameraError(
                f"Calibration load did not take effect within {timeout}s "
                f"(requested {tag!r}, loaded tag is {loaded!r})."
            )
        return {"preset": preset, "tag": loaded, "exposure_ms": self.exposure_ms}

    def set_calibration_block(self, index: int) -> None:
        """Load the factory calibration at browse *index* into the active preset.

        Thin wrapper over :meth:`load_calibration` (``index=`` form). Note this
        genuinely loads the calibration -- which also changes the preset's
        integration time -- unlike merely moving the browse cursor. See
        :meth:`load_calibration` for the full contract and the NUC caveat.
        """
        self.load_calibration(index=index)

    def get_calibration(self, block: int | None = None) -> dict:
        """Read calibration data for a calibration block.

        If *block* is ``None`` the calibration **actually loaded** on the
        active preset is used (matched by tag, see
        :meth:`_active_calibration_index`), not merely whatever the browse
        cursor points at -- so :meth:`counts_to_temperature` converts against
        the polynomial that genuinely applies to captured frames. Pass an
        explicit *block* index to read a specific library entry instead. The
        browse cursor is restored after reading.

        Temperature conversion uses two polynomials, applied by
        :func:`apply_calibration` (see its docstring for the full formula,
        including background subtraction and object-parameter compensation,
        and their provenance):

        1. counts → radiance:  W = Σ counts_coeffs[i] · counts^i − counts_background
        2. radiance → temp °C: T_C = Σ temp_coeffs[i] · W^i

        tmin/tmax are confirmed °C despite the camera's GenICam XML
        mislabeling the backing registers "in Kelvin" in free-text only (no
        structured ``<Unit>`` tag); see the comment on
        :data:`pyflir.registers.REG_CAL_TMIN`.

        Returns:
            dict with keys: ``block``, ``tmin``, ``tmax`` (°C),
            ``counts_order``, ``counts_coeffs``, ``counts_background``,
            ``temp_order``, ``temp_coeffs``.
        """
        self._require_connected()
        prev = self._gvcp.read_reg(reg.REG_CAL_INDEX)
        # When no explicit block is requested, point the browse cursor at the
        # calibration actually loaded on the active preset (matched by tag),
        # not wherever the cursor happens to sit, so the coefficients returned
        # are the ones the live stream really uses.
        target = block if block is not None else self._active_calibration_index()
        if target is not None:
            self._gvcp.write_reg(reg.REG_CAL_INDEX, target)
        try:
            c_order = self.read_int("CalibrationQueryOrderReg")
            c_coeffs = [self.read_float(f"CalibrationQueryCoeff{i}Reg") for i in range(c_order + 1)]
            t_order = self.read_int("CalibrationQueryTempOrderReg")
            t_coeffs = [
                self.read_float(f"CalibrationQueryTempCoeff{i}Reg") for i in range(t_order + 1)
            ]
            return {
                "block": self._gvcp.read_reg(reg.REG_CAL_INDEX),
                "tmin": self._gvcp.read_float(reg.REG_CAL_TMIN),
                "tmax": self._gvcp.read_float(reg.REG_CAL_TMAX),
                # IntReg on the A6751sc (native 14-bit ADC counts), not FloatReg;
                # read_float() would reinterpret the raw integer bits as IEEE-754.
                "counts_min": self.read_int("CalibrationQueryMinCountsReg"),
                "counts_max": self.read_int("CalibrationQueryMaxCountsReg"),
                "counts_order": c_order,
                "counts_coeffs": c_coeffs,
                "counts_background": self.read_float("CalibrationQueryBackgroundValueReg"),
                "temp_order": t_order,
                "temp_coeffs": t_coeffs,
            }
        finally:
            if target is not None:
                self._gvcp.write_reg(reg.REG_CAL_INDEX, prev)

    def counts_to_temperature(
        self,
        counts: "np.ndarray",
        emissivity: float | None = None,
        refl_temp_c: float | None = None,
        atm_temp_c: float | None = None,
        tau: float = 1.0,
        return_status: bool = False,
    ) -> "np.ndarray":
        """Convert a raw uint16 frame to temperature in degrees Celsius.

        Reads calibration for the currently active block, then applies
        :func:`apply_calibration` -- see its docstring for the exact formula,
        including the object-parameter (emissivity/atmosphere/reflected)
        compensation and its provenance.

        If *emissivity*, *refl_temp_c*, or *atm_temp_c* are omitted they
        default to the values previously set via :meth:`set_object_params`.

        Args:
            counts:        2-D uint16 array (H, W) from :meth:`grab` or :meth:`read`.
            emissivity:    Object surface emissivity (0–1).
            refl_temp_c:   Reflected apparent temperature in °C.
            atm_temp_c:    Atmospheric temperature in °C.
            tau:           Atmospheric transmission (0–1). 1 = no atmosphere.
            return_status: If True, also return a per-pixel status array (see
                :func:`apply_calibration`).

        Returns:
            Float64 array (H, W) with temperature in degrees Celsius, or
            ``(temperature, status)`` if *return_status* is True.

        Example::

            frame = cam.grab()
            temp  = cam.counts_to_temperature(frame)
            print(f"Centre pixel: {temp[256, 320]:.1f} °C")
        """
        if emissivity is None or refl_temp_c is None or atm_temp_c is None:
            try:
                params = self.get_object_params()
                if emissivity is None:
                    emissivity = params.get("emissivity", 1.0)
                if refl_temp_c is None:
                    refl_temp_c = params.get("reflected_temp_K", 296.15) - 273.15
                if atm_temp_c is None:
                    atm_temp_c = params.get("atmospheric_temp_K", 296.15) - 273.15
            except Exception:
                emissivity = emissivity if emissivity is not None else 1.0
                refl_temp_c = refl_temp_c if refl_temp_c is not None else 23.0
                atm_temp_c = atm_temp_c if atm_temp_c is not None else 23.0

        cal = self.get_calibration()
        return apply_calibration(
            counts, cal, emissivity, refl_temp_c, atm_temp_c, tau, return_status=return_status
        )

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
            "emissivity": self.read_float("Emissivity"),
            "object_distance_m": self.read_float("ObjectDistance"),
            "atmospheric_temp_K": self.read_float("AtmosphericTemperature"),
            "reflected_temp_K": self.read_float("ReflectedTemperature"),
            "relative_humidity": self.read_float("RelativeHumidity"),
        }

    def set_object_params(
        self,
        emissivity: float | None = None,
        distance_m: float | None = None,
        atm_temp_k: float | None = None,
        refl_temp_k: float | None = None,
        humidity: float | None = None,
    ) -> None:
        """Set radiometry object parameters (any subset)."""
        if emissivity is not None:
            self.write_float("Emissivity", emissivity)
        if distance_m is not None:
            self.write_float("ObjectDistance", distance_m)
        if atm_temp_k is not None:
            self.write_float("AtmosphericTemperature", atm_temp_k)
        if refl_temp_k is not None:
            self.write_float("ReflectedTemperature", refl_temp_k)
        if humidity is not None:
            self.write_float("RelativeHumidity", humidity)

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
            "width": self.read_int("Width"),
            "height": self.read_int("Height") - self._metadata_rows,
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
            "width_min": self._gvcp.read_reg(reg.REG_WIDTH_MIN),
            "width_inc": self._gvcp.read_reg(reg.REG_WIDTH_INC),
            # REG_HEIGHT_MIN counts total rows (image + metadata); subtract so
            # the returned value represents the minimum usable image rows.
            "height_min": max(1, h_min_raw - self._metadata_rows),
            "height_inc": self._gvcp.read_reg(reg.REG_HEIGHT_INC),
            "sensor_width": reg.SENSOR_WIDTH,
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
            raise CameraError(f"Width {width} is not a multiple of the increment {w_inc}.")
        if width + offset_x > w_max:
            raise CameraError(
                f"Width {width} + offset_x {offset_x} = {width + offset_x} "
                f"exceeds sensor width {w_max}."
            )
        if height < h_min:
            raise CameraError(f"Height {height} is below the minimum {h_min}.")
        # Validate the increment against the camera's raw row domain (image +
        # metadata rows), not the usable-row h_min from get_roi_limits(): that
        # value is clamped to a floor of 1 for sensible display, which silently
        # corrupts this modular check whenever the true unclamped baseline
        # (h_min_raw - metadata_rows) is <= 0 -- confirmed live on the A6751sc,
        # where it wrongly rejected height=512 (raw 513), a value every capture
        # this session had already proven valid.
        h_min_raw = self._gvcp.read_reg(reg.REG_HEIGHT_MIN)
        raw_height = height + self._metadata_rows
        if h_inc > 0 and ((raw_height - h_min_raw) % h_inc) != 0:
            raise CameraError(
                f"Height {height} does not satisfy the increment constraint "
                f"(raw height {raw_height} − {h_min_raw}) must be a multiple of {h_inc}."
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
        self.width = self.read_int("Width")
        self.height = self.read_int("Height") - self._metadata_rows
        max_fps = self.frame_rate_max
        fps_str = f"  max FPS now: {max_fps:.1f} Hz" if max_fps else ""
        logger.info(
            "ROI set: %d×%d  offset (%d,%d)%s",
            self.width,
            self.height,
            offset_x,
            offset_y,
            fps_str,
        )

    # ------------------------------------------------------------------
    # NUC (Non-Uniformity Correction)
    # ------------------------------------------------------------------

    def get_nuc_status(self, preset: int = 0) -> dict:
        """Return the NUC correction currently loaded for *preset*.

        Args:
            preset: Preset index to query (0–3). Default 0.

        Returns:
            dict with key ``name`` (str, the currently loaded correction's
            name; empty string if none is loaded).
        """
        self._require_connected()
        if preset not in reg.REG_CORRECTION_NAME:
            raise CameraError(f"Invalid preset {preset}. Must be 0–3.")
        return {"name": self._read_string(reg.REG_CORRECTION_NAME[preset])}

    def has_flag(self) -> bool:
        """Return whether this camera has a physical NUC flag (shutter)."""
        self._require_connected()
        return bool(self._gvcp.read_reg(reg.REG_FLAG_PRESENT))

    def get_flag_state(self) -> str:
        """Return the NUC flag's current position: ``"Stowed"`` or ``"InFOV"``.

        Raises:
            CameraError: If this camera has no NUC flag (see :meth:`has_flag`).
        """
        self._require_connected()
        if not self.has_flag():
            raise CameraError("This camera has no NUC flag (has_flag() is False).")
        raw = self._gvcp.read_reg(reg.REG_FLAG_STATE)
        return reg.FLAG_STATE_NAMES.get(raw, f"<unknown:{raw}>")

    def flag_move_in_fov(self) -> None:
        """Move the internal NUC flag into the field of view.

        Only available on cameras equipped with a physical shutter
        (e.g. FLIR A6751sc). Call before capturing a flat-field reference.

        This only sends the move command; the flag is a physical mechanism
        and takes time to actually reach position. Poll
        :meth:`get_flag_state` for ``"InFOV"`` before capturing a flat-field
        frame rather than assuming the move completed instantly.
        """
        self._require_connected()
        self._gvcp.write_reg(reg.REG_FLAG_IN_FOV, 1)
        logger.info("NUC flag commanded into FOV.")

    def flag_move_stowed(self) -> None:
        """Move the internal NUC flag out of the field of view.

        See :meth:`flag_move_in_fov` for why this doesn't wait for the
        physical move to complete.
        """
        self._require_connected()
        self._gvcp.write_reg(reg.REG_FLAG_STOWED, 1)
        logger.info("NUC flag commanded to stowed position.")

    # ------------------------------------------------------------------
    # NUC: performing a new correction. GenICam group "CorrectionPerform":
    # a state machine, see registers.py for the full status/result enum
    # tables.
    # ------------------------------------------------------------------

    def get_correction_status(self) -> dict:
        """Return the live status of an in-progress (or just-finished) NUC correction.

        Returns:
            dict with keys ``status`` (str, e.g. ``"Ready"``,
            ``"CollectingFirstSource"``, ``"WaitingForFirstSourceExternal"``
            -- see :data:`pyflir.registers.CORRECTION_STATUS_NAMES` for the
            full set) and ``text`` (str, human-readable detail from the
            camera, descriptive enough to know what to do next).
        """
        self._require_connected()
        raw = self._gvcp.read_reg(reg.REG_CORRECTION_STATUS)
        status = reg.CORRECTION_STATUS_NAMES.get(raw, f"<unknown:{raw}>")
        return {"status": status, "text": self._read_string(reg.REG_CORRECTION_STATUS_TEXT)}

    def get_correction_result(self) -> dict:
        """Return the outcome of the most recently completed NUC correction.

        Returns:
            dict with keys ``result`` (str: ``"Okay"``, ``"Abort"``,
            ``"AbortInvalidParam"``, or ``"AbortFlagCoolerRunaway"``) and
            ``text`` (str, human-readable detail from the camera).
        """
        self._require_connected()
        raw = self._gvcp.read_reg(reg.REG_CORRECTION_RESULT)
        result = reg.CORRECTION_RESULT_NAMES.get(raw, f"<unknown:{raw}>")
        return {"result": result, "text": self._read_string(reg.REG_CORRECTION_RESULT_TEXT)}

    def correction_start(
        self,
        preset: int = 0,
        correction_type: str = "OnePoint",
        source: str = "Internal",
    ) -> None:
        """Start a new NUC correction process (low-level).

        Prefer :meth:`perform_nuc` for the common case (internal flag,
        fully automatic, blocks until done). Use this directly for a
        "TwoPoint" or ``source="External"`` workflow, which need a person
        to present a uniform target and call :meth:`correction_continue`
        partway through -- :meth:`perform_nuc` doesn't support those.

        After calling this, poll :meth:`get_correction_status` and act on
        the status:

        - ``"WaitingForFirst/SecondSourceExternal"``: present a uniform
          target in the field of view, then call :meth:`correction_continue`.
        - ``"WaitingForFirst/SecondSourceInternal"``: no action needed, the
          camera is bringing its own flag to temperature; keep polling.
        - ``"Ready"``: the process is complete. Check
          :meth:`get_correction_result`, then call :meth:`correction_accept`
          or :meth:`correction_discard`.

        Args:
            preset: Preset index to correct (0–3). Default 0.
            correction_type: ``"OnePoint"`` (offset only -- the standard
                routine flat-field NUC), ``"TwoPoint"`` (gain and offset,
                needs two distinct uniform sources), or ``"UpdateOffset"``
                (offset only, gain unchanged, faster than a full OnePoint).
            source: ``"Internal"`` (the camera's own NUC flag, fully
                automatic) or ``"External"`` (a uniform target you present
                yourself).

        Raises:
            CameraError: If *preset*, *correction_type*, or *source* is invalid.
        """
        self._require_connected()
        if preset not in reg.REG_CORRECTION_PS:
            raise CameraError(f"Invalid preset {preset}. Must be 0–3.")
        type_values = {v: k for k, v in reg.CORRECTION_TYPE_NAMES.items()}
        if correction_type not in type_values:
            raise CameraError(
                f"Invalid correction_type {correction_type!r}. Must be one of {list(type_values)}."
            )
        source_values = {v: k for k, v in reg.CORRECTION_SOURCE_NAMES.items()}
        if source not in source_values:
            raise CameraError(f"Invalid source {source!r}. Must be one of {list(source_values)}.")

        self._gvcp.write_reg(reg.REG_CORRECTION_TYPE, type_values[correction_type])
        self._gvcp.write_reg(reg.REG_CORRECTION_SOURCE, source_values[source])
        for p, addr in reg.REG_CORRECTION_PS.items():
            self._gvcp.write_reg(addr, 1 if p == preset else 0)
        self._gvcp.write_reg(reg.REG_CORRECTION_START, 1)
        logger.info(
            "NUC correction started for preset %d (%s, %s source).", preset, correction_type, source
        )

    def correction_continue(self) -> None:
        """Advance past a "WaitingFor...SourceExternal" status.

        Call after presenting a uniform target in the field of view.
        """
        self._require_connected()
        self._gvcp.write_reg(reg.REG_CORRECTION_CONTINUE, 1)

    def correction_accept(self) -> None:
        """Keep the new correction computed by the current/last process."""
        self._require_connected()
        self._gvcp.write_reg(reg.REG_CORRECTION_ACCEPT, 1)

    def correction_discard(self) -> None:
        """Discard the new correction and revert to the previous one."""
        self._require_connected()
        self._gvcp.write_reg(reg.REG_CORRECTION_DISCARD, 1)

    def correction_abort(self) -> None:
        """Cancel an in-progress correction process.

        Per the camera's own GenICam description, a started process must be
        accepted, discarded, or aborted to properly end it.
        """
        self._require_connected()
        self._gvcp.write_reg(reg.REG_CORRECTION_ABORT, 1)

    def perform_nuc(
        self,
        preset: int = 0,
        correction_type: str = "OnePoint",
        timeout: float = 60.0,
        poll_interval: float = 0.5,
        auto_accept: bool = True,
    ) -> dict:
        """Perform a new NUC (Non-Uniformity Correction) now.

        This is the "just run it and it does NUC" command: it computes a
        fresh correction and blocks until done, using the camera's own
        internal NUC flag as the uniform reference -- fully automatic, the
        camera brings the flag to temperature and captures the reference
        itself, no user action needed.

        For an external-blackbody or "TwoPoint" workflow, which need a
        person to present a target and call :meth:`correction_continue`
        partway through, use :meth:`correction_start` directly instead.

        Args:
            preset: Preset index to correct (0–3). Default 0.
            correction_type: ``"OnePoint"`` (offset only -- the standard
                routine flat-field NUC, default) or ``"UpdateOffset"``
                (offset only, faster, meant for use between full
                corrections). Not ``"TwoPoint"``: that needs two distinct
                uniform sources, which a single internal flag at one
                temperature can't provide -- use :meth:`correction_start`
                for it.
            timeout: Seconds to wait for the process to reach ``"Ready"``
                before aborting and raising. Generous by default since the
                flag may take a while to reach its target temperature.
            poll_interval: Seconds between status polls.
            auto_accept: If True (default), call :meth:`correction_accept`
                automatically once the result is ``"Okay"``. If False,
                leave the correction pending for you to accept or discard.

        Returns:
            dict from :meth:`get_correction_result` (keys ``result``, ``text``).

        Raises:
            CameraError: If the camera has no NUC flag, *preset* or
                *correction_type* is invalid, the process times out, the
                camera unexpectedly asks for an external source, or the
                result is not ``"Okay"``.

        Warning:
            Not yet verified against a live camera -- implemented from the
            camera's own GenICam XML (register addresses and the
            Start/Continue/Accept/Discard/Abort state machine), not tested
            end to end. Try a short *timeout* first and watch logs before
            relying on it.
        """
        self._require_connected()
        if not self.has_flag():
            raise CameraError(
                "This camera has no NUC flag (has_flag() is False); perform_nuc() "
                "requires the internal-flag source. Use correction_start(source="
                "'External', ...) with a uniform target instead."
            )
        if correction_type == "TwoPoint":
            raise CameraError(
                "TwoPoint correction needs two distinct uniform sources; not "
                "meaningful with a single internal flag at one temperature. "
                "Use correction_start() directly for a TwoPoint/External workflow."
            )

        self.correction_start(preset=preset, correction_type=correction_type, source="Internal")

        deadline = time.monotonic() + timeout
        external_statuses = {"WaitingForFirstSourceExternal", "WaitingForSecondSourceExternal"}
        while True:
            status = self.get_correction_status()
            if status["status"] == "Ready":
                break
            if status["status"] in external_statuses:
                with contextlib.suppress(Exception):
                    self.correction_abort()
                raise CameraError(
                    f"Camera unexpectedly asked for an external uniform source "
                    f"({status['status']}) despite requesting the internal flag; "
                    f"aborted. {status['text']}"
                )
            if time.monotonic() >= deadline:
                with contextlib.suppress(Exception):
                    self.correction_abort()
                raise CameraError(
                    f"NUC correction timed out after {timeout:.0f}s "
                    f"(last status: {status['status']} - {status['text']})."
                )
            time.sleep(poll_interval)

        result = self.get_correction_result()
        if result["result"] != "Okay":
            raise CameraError(f"NUC correction failed: {result['result']} - {result['text']}")

        if auto_accept:
            self.correction_accept()
            logger.info("NUC correction complete and accepted for preset %d.", preset)
        else:
            logger.info(
                "NUC correction complete for preset %d; call correction_accept() "
                "or correction_discard() to finish.",
                preset,
            )
        return result

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_temperatures(self) -> dict[str, float]:
        """Read all available on-board temperature sensors.

        :data:`pyflir.registers.TEMP_SENSORS` is defined generically from
        the camera's ``DeviceTemperatureSelector`` enum (9 entries), which
        is shared across the whole product line -- not every physical unit
        necessarily has every sensor populated. A sensor whose read fails
        is skipped (logged at debug level) rather than aborting the whole
        call, confirmed necessary live: on the A6751sc this session, one of
        the newer selector indices raised ``GVCPError: GENERIC_ERROR``.

        Returns:
            dict mapping sensor name to temperature in degrees Celsius, for
            whichever of :data:`pyflir.registers.TEMP_SENSORS` responded
            successfully. See :meth:`detector_temperature` for reading just
            FPA, which doesn't depend on any of the others being available.
        """
        self._require_connected()
        original = self._gvcp.read_reg(reg.REG_TEMP_SELECTOR)
        temps: dict[str, float] = {}
        try:
            for name, idx in reg.TEMP_SENSORS.items():
                try:
                    self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, idx)
                    temps[name] = self._gvcp.read_float(reg.REG_TEMP_VALUE)
                except GVCPError as exc:
                    logger.debug(
                        "Temperature sensor %r (index %d) not readable: %s", name, idx, exc
                    )
        finally:
            self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, original)
        return temps

    def _read_temp_sensor(self, index: int) -> float:
        """Read one on-board temperature sensor by selector index.

        Independent of :meth:`get_temperatures`: reads only this one
        sensor, so it can't fail because a *different* sensor isn't
        populated on this unit. Restores the selector register afterward.
        """
        self._require_connected()
        original = self._gvcp.read_reg(reg.REG_TEMP_SELECTOR)
        try:
            self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, index)
            return self._gvcp.read_float(reg.REG_TEMP_VALUE)
        finally:
            self._gvcp.write_reg(reg.REG_TEMP_SELECTOR, original)

    def info(self) -> dict:
        """Return a dict of the camera's current state.

        Includes IP, model, serial, ROI, frame rate, exposure,
        calibration block, detector temperature, and streaming status.
        """
        out: dict = {
            "ip": self.ip,
            "model": self.model,
            "serial": self.serial,
            "streaming": self._streaming,
        }
        if not self._nodes:
            return out

        def _safe(fn):
            try:
                return fn()
            except Exception:
                return None

        out["width"] = _safe(lambda: self.read_int("Width"))
        out["height"] = _safe(lambda: self.read_int("Height") - self._metadata_rows)
        out["frame_rate_hz"] = _safe(lambda: round(self.read_float("AcquisitionFrameRate"), 2))
        fmax = self.get_max_frame_rate()
        out["frame_rate_max_hz"] = round(fmax, 2) if fmax is not None else None
        out["exposure_ms"] = _safe(lambda: round(self.read_float("ExposureTime") * 1e3, 3))
        out["detector_temp_C"] = _safe(lambda: round(self.detector_temperature, 2))
        # The block actually applied to the stream (matched by loaded tag),
        # not the raw browse-cursor position.
        out["calibration_block"] = _safe(self.get_calibration_block)
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

    def _read_string(self, addr: int, length: int = 256) -> str:
        """Read a null-terminated ASCII string from a fixed-length StringReg."""
        raw = self._gvcp.read_mem(addr, length)
        return raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()

    def _write_string_reg(self, addr: int, text: str, length: int = 256) -> None:
        """Write an ASCII string to a fixed-length StringReg region.

        pyGigEVision exposes no GVCP WRITEMEM, only single-register WRITEREG,
        so the string is written 4 bytes at a time to consecutive addresses.
        These StringReg regions store bytes little-endian within each 32-bit
        word (verified live: a big-endian write read back byte-swapped per
        word), so each 4-byte group is packed little-endian. The value is
        NUL-padded to *length* so any previous longer contents are cleared.
        """
        data = text.encode("ascii")[: length - 1]
        data = data + b"\x00" * (length - len(data))
        for off in range(0, length, 4):
            word = struct.unpack("<I", data[off : off + 4])[0]
            self._gvcp.write_reg(addr + off, word)


# ---------------------------------------------------------------------------
# Standalone radiometric conversion (works offline with a cached cal dict)
# ---------------------------------------------------------------------------


# Per-pixel status codes for apply_calibration(return_status=True), mirroring
# FLIR's own fnv.file.ImagerFile.status ("overflow, underflow, warning").
STATUS_OK = 0
STATUS_UNDERFLOW = 1  # raw (14-bit) count below this block's counts_min
STATUS_OVERFLOW = 2  # raw (14-bit) count above this block's counts_max


def apply_calibration(
    counts: np.ndarray,
    cal: dict,
    emissivity: float = 1.0,
    refl_temp_c: float = 23.0,
    atm_temp_c: float = 23.0,
    tau: float = 1.0,
    return_status: bool = False,
) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Convert raw uint16 counts to °C using a pre-read calibration dict.

    This is the offline equivalent of :meth:`Camera.counts_to_temperature`.
    Useful for applying radiometric conversion to saved frames without a
    live camera connection::

        cal   = cam.get_calibration()          # read once, cache to disk
        temp  = apply_calibration(frame, cal)  # apply later, no camera needed

    Applies two polynomial steps:

    1. counts → radiance: W = Σ counts_coeffs[i] · counts^i − counts_background
    2. radiance → temp:   T = Σ temp_coeffs[i] · W^i

    Counts are first un-shifted from MSB-aligned Mono16 to the native
    14-bit ADC range when needed (uint16 = adc << 2), then clipped to
    [counts_min, counts_max] (out-of-range pixels are flagged in the
    optional *status* output rather than silently extrapolated). Whether
    the temperature polynomial outputs Kelvin or Celsius is resolved
    automatically by checking the chain against the block endpoints
    (tmin/tmax, confirmed °C -- see the comment on
    :data:`pyflir.registers.REG_CAL_TMIN`).

    Provenance, since none of this is directly visible from the camera's
    GenICam XML alone: FLIR's own Science File SDK (extracted read-only from
    ``FLIRScienceFileSDK-2026.1.2+10-Linux-aarch64.run`` for inspection, not
    executed -- it targets Linux ARM64 and this project runs on Windows) has
    a ``CNicevilleFactoryCalReduceObject`` struct whose fields
    (``polyOrder``, ``coeffs[7]``, ``bgValue``, ``tempPolyOrder``,
    ``tempCoeffs[7]``, ``cmin``/``cmax`` as ``uint16_t``) map 1:1 onto this
    camera's ``CalibrationQuery*`` registers, with ``bgValue`` grouped under
    the same "count->rad" comment as the counts polynomial -- strong
    evidence it belongs in step 1, matching what this function now does.
    Separately, ``fnv.file.ImagerFile.status`` ("overflow, underflow,
    warning") and ``IConverter``'s explicit clip parameters confirm FLIR's
    reference implementation never silently extrapolates out-of-range
    pixels, which is why this function now clips and flags instead.

    When *emissivity* != 1.0 or *tau* != 1.0, the standard object-signal
    equation (published thermography practice, not extracted from FLIR's
    compiled code -- that part is closed-source) is applied on top:

        W_total = ε·τ·W_obj + (1−ε)·τ·W_refl + (1−τ)·W_atm

    solved for W_obj, where W_refl/W_atm are obtained by numerically
    inverting the radiance→temperature polynomial at *refl_temp_c*/
    *atm_temp_c*. With the defaults (ε=1, τ=1) this reduces exactly to
    W_obj = W_total, i.e. unchanged behavior from before this parameter was
    wired in -- existing callers using the defaults are unaffected.

    None of the above (background subtraction, clipping, or the object-
    parameter compensation) has been verified against a live camera yet --
    there wasn't one connected while this was written. The most direct
    check: for every calibration block index (0..CalibrationQueryIndexMax),
    evaluate this chain at that block's own counts_min/counts_max and
    compare against that block's own tmin/tmax -- each block carries its own
    ground truth, no external reference file needed.

    Parameters
    ----------
    counts : np.ndarray
        2-D uint16 array (H, W).
    cal : dict
        Calibration dict from :meth:`Camera.get_calibration`.
    emissivity : float
        Object surface emissivity (0-1]. Default 1.0 (no correction).
    refl_temp_c : float
        Reflected apparent temperature in °C. Only used when emissivity != 1.
    atm_temp_c : float
        Atmospheric temperature in °C. Only used when tau != 1.
    tau : float
        Atmospheric transmission (0-1]. Default 1.0 (no correction).
    return_status : bool
        If True, also return a per-pixel status array (:data:`STATUS_OK`,
        :data:`STATUS_UNDERFLOW`, :data:`STATUS_OVERFLOW`). Default False.

    Returns
    -------
    np.ndarray
        Float64 array (H, W) with temperature in degrees Celsius.
    np.ndarray, optional
        uint8 array (H, W) of status codes, only if *return_status* is True.
    """
    cmin = float(cal["counts_min"])
    cmax = float(cal["counts_max"])
    tmin = float(cal["tmin"])
    background = float(cal.get("counts_background", 0.0))

    # The A6751sc has a 14-bit ADC but GigE Vision streams Mono16 with the
    # value left-justified (uint16 = adc_14bit << 2).  The calibration
    # endpoints counts_min/counts_max are in native 14-bit space, so divide
    # raw pixel values by 4 to bring them into the same domain.
    x = counts.astype(np.float64) / 4.0

    status = np.full(x.shape, STATUS_OK, dtype=np.uint8)
    status[x < cmin] = STATUS_UNDERFLOW
    status[x > cmax] = STATUS_OVERFLOW
    x = np.clip(x, cmin, cmax)

    # Two-polynomial radiometric conversion:
    #   1. counts (14-bit) → radiance w, background-subtracted
    #   2. radiance w → temperature
    # np.polyval expects highest-degree coefficient first; the camera stores
    # them lowest-degree first, so reverse before calling polyval.
    c_hi = np.asarray(cal["counts_coeffs"], dtype=np.float64)[::-1]
    t_hi = np.asarray(cal["temp_coeffs"], dtype=np.float64)[::-1]
    w_total = np.polyval(c_hi, x) - background

    # Determine K→C offset by checking the polynomial output at the known
    # block endpoints (tmin/tmax are confirmed Celsius).  If the polynomial
    # outputs Kelvin, the endpoint values will be ~273 higher than tmin/tmax.
    w_lo = float(np.polyval(c_hi, cmin)) - background
    t_lo = float(np.polyval(t_hi, w_lo))
    offs = 273.15 if abs(t_lo - 273.15 - tmin) < abs(t_lo - tmin) else 0.0

    w_obj = w_total
    if emissivity != 1.0 or tau != 1.0:
        w_cmin = float(np.polyval(c_hi, cmin)) - background
        w_cmax = float(np.polyval(c_hi, cmax)) - background

        def _radiance_at(temp_c: float) -> float:
            """Invert the radiance→temperature polynomial at a known temperature."""
            target = temp_c + offs
            shifted = t_hi.copy()
            shifted[-1] -= target
            roots = np.roots(shifted)
            real_roots = roots[np.abs(roots.imag) < 1e-6].real
            lo, hi = sorted((w_cmin, w_cmax))
            if real_roots.size == 0:
                raise ValueError(f"Could not invert temperature→radiance polynomial for {temp_c}°C")
            in_domain = real_roots[(real_roots >= lo) & (real_roots <= hi)]
            candidates = in_domain if in_domain.size else real_roots
            return float(candidates[np.argmin(np.abs(candidates - (lo + hi) / 2))])

        w_refl = _radiance_at(refl_temp_c)
        w_atm = _radiance_at(atm_temp_c)
        w_obj = (w_total - (1 - emissivity) * tau * w_refl - (1 - tau) * w_atm) / (emissivity * tau)

    t = np.polyval(t_hi, w_obj)
    result = t - offs

    if return_status:
        return result, status
    return result


# ---------------------------------------------------------------------------
# Module-level discover() convenience function
# ---------------------------------------------------------------------------


def discover(
    interface_ip: str | None = None,
    timeout: float = 2.0,
) -> list[dict]:
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
