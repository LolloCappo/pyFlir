"""FLIR thermal camera driver over GigE Vision.

Provides a Pythonic interface to FLIR Xsc-series and A-series cameras
using pyGigEVision for the transport layer. Handles discovery, streaming,
frame acquisition, ROI, calibration block selection, non-uniformity
correction, radiometric conversion, and diagnostics.

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

# How long Camera.nuc() waits for CorrectionAutoInProgress to go high before
# concluding the update was too quick to observe. Without this it would return
# immediately on the first read, since the bit is not guaranteed to have
# latched yet; with it, "never went busy" is treated as done rather than hung.
NUC_START_GRACE_S = 2.0

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
        # Optional software bad-pixel replacement. When correct_bad_pixels is
        # True, grab()/read() interpolate over pixels flagged in bad_pixel_mask
        # (built by detect_bad_pixels()). Fills the gap when the camera's own
        # NUC bad-pixel map is incomplete for an aged detector.
        self.bad_pixel_mask: np.ndarray | None = None
        self.correct_bad_pixels: bool = False
        # Host-side one-point radiometric offset, in 14-bit counts, applied by
        # counts_to_temperature() before the conversion. This is a last-resort
        # workaround for a residual bias against a known-temperature target;
        # the supported fix for a counts offset is Camera.nuc(), which corrects
        # it on the camera where it belongs. 0.0 = no correction.
        self.count_offset: float = 0.0
        # Integration time (ms) that the currently loaded calibration was fit at,
        # recorded by load_calibration(). A calibration is only valid at its own
        # integration time -- counts scale with it -- so counts_to_temperature()
        # warns if the two have since diverged. None = unknown (nothing loaded
        # through load_calibration this session).
        self._cal_integration_ms: float | None = None

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

    def _finish_frame(self, frame: np.ndarray) -> np.ndarray:
        """Strip metadata rows, then apply bad-pixel correction if enabled."""
        frame = self._strip_metadata(frame)
        if self.correct_bad_pixels:
            frame, _ = replace_bad_pixels(frame, mask=self.bad_pixel_mask)
        return frame

    def detect_bad_pixels(
        self,
        n_frames: int = 30,
        timeout: float = 2.0,
        dead_fraction: float = 0.5,
        twinkle_sigma: float = 10.0,
    ) -> int:
        """Build a bad-pixel mask by observing the live stream.

        Streams *n_frames* and flags pixels that are (a) stuck at/below zero in
        at least *dead_fraction* of frames (dead) or (b) far noisier over time
        than typical (twinkling), storing the result in
        :attr:`bad_pixel_mask`. Set :attr:`correct_bad_pixels` to ``True``
        afterwards to have :meth:`grab`/:meth:`read` interpolate over them.

        Point the camera at a normal scene (not a uniform blackbody) so dead
        pixels stand out. This complements the camera's on-board NUC bad-pixel
        map, which can be incomplete on an aged detector (observed live: the
        NUC listed ~10 bad pixels while ~600 were actually dead).

        Args:
            n_frames: Frames to average for detection.
            timeout: Per-frame read timeout in seconds.
            dead_fraction: Fraction of frames a pixel must read <= 0 to count
                as dead.
            twinkle_sigma: A pixel is flagged twinkling if its temporal std
                (global DC removed) exceeds this many times the median.

        Returns:
            Number of pixels flagged.
        """
        self._require_connected()
        prev = self.correct_bad_pixels
        self.correct_bad_pixels = False  # detect on raw frames
        was_streaming = self._streaming
        if not was_streaming:
            self.start_stream()
        try:
            for _ in range(min(10, n_frames)):
                self.read(timeout=timeout)
            frames = np.stack(
                [self.read(timeout=timeout).astype(np.float64) for _ in range(n_frames)]
            )
        finally:
            if not was_streaming:
                self.stop_stream()
            self.correct_bad_pixels = prev

        dead = (frames <= 0).mean(axis=0) >= dead_fraction
        dc = frames.mean(axis=(1, 2), keepdims=True)
        tstd = (frames - dc).std(axis=0)
        med = float(np.median(tstd))
        twinkle = tstd > max(50.0, twinkle_sigma * med)
        self.bad_pixel_mask = dead | twinkle
        return int(self.bad_pixel_mask.sum())

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
            return self._finish_frame(frame)

        frame = self._gvsp.get_frame(timeout=timeout)
        if frame is None:
            raise CameraError(
                f"No frame received within {timeout:.1f} s. Check:\n"
                "  • Firewall allows inbound UDP on the listen port\n"
                "  • SC_PACKET_SIZE ≤ network MTU\n"
                "  • Camera is not in trigger mode"
            )
        return self._finish_frame(frame)

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
                    frames.append(self._finish_frame(frame))
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
    def ir_format(self) -> str:
        """What the camera streams: ``"Radiometric"`` (raw counts) or a
        ``"TemperatureLinear10mK"``/``"TemperatureLinear100mK"`` mode.

        In a TemperatureLinear mode the camera does all radiometry on-board
        (its own NUC, calibration, and object parameters) and streams
        temperature directly -- ``frame_to_celsius()`` then just rescales, no
        polynomial needed. Requires a factory calibration to be loaded first
        (see :meth:`load_calibration`).
        """
        self._require_connected()
        raw = self._gvcp.read_reg(reg.REG_IR_FORMAT)
        return reg.IR_FORMAT_NAMES.get(raw, f"<unknown:{raw}>")

    @ir_format.setter
    def ir_format(self, mode: "str | int") -> None:
        self._require_connected()
        if isinstance(mode, str):
            if mode not in reg.IR_FORMAT_VALUES:
                raise CameraError(
                    f"Unknown IR format {mode!r}; expected one of {list(reg.IR_FORMAT_VALUES)}."
                )
            val = reg.IR_FORMAT_VALUES[mode]
        else:
            val = int(mode)
        # The format change only takes effect on a fresh acquisition, so bounce
        # the stream if it is running.
        was_streaming = self._streaming
        if was_streaming:
            self.stop_stream()
        self._gvcp.write_reg(reg.REG_IR_FORMAT, val)
        if was_streaming:
            self.start_stream()

    def frame_to_celsius(self, frame: np.ndarray) -> np.ndarray:
        """Convert a frame to °C using whatever the current IR format is.

        In a TemperatureLinear mode the camera already computed temperature, so
        this is the trivial ``count * kelvin_per_count - 273.15`` (the same
        one-liner pyTelops uses). In Radiometric mode it falls back to the
        calibration polynomial via :meth:`counts_to_temperature`.

        Dead pixels read 0 in temperature mode, which maps to -273.15 °C; use
        :attr:`correct_bad_pixels` / :meth:`detect_bad_pixels` to interpolate
        them out first if that matters for your display.
        """
        self._require_connected()
        fmt = self._gvcp.read_reg(reg.REG_IR_FORMAT)
        scale = reg.IR_FORMAT_KELVIN_PER_COUNT.get(fmt)
        if scale is not None:
            return frame.astype(np.float64) * scale - 273.15
        return self.counts_to_temperature(frame)

    @property
    def pixel_format(self) -> str:
        """Transport pixel format, e.g. ``"Mono16"`` or ``"Mono14"``.

        The A6751sc has a 14-bit ADC. In ``"Mono16"`` the 14-bit value is
        MSB-aligned (padded up 2 bits, so a full-scale pixel reads ~65500 and
        the native count is ``value / 4``). In ``"Mono14"`` the stream is the
        14-bit value directly (0-16383, no shift). :meth:`counts_to_temperature`
        adapts automatically, so either format converts correctly.
        """
        self._require_connected()
        return self.read_enum("PixelFormat")

    @pixel_format.setter
    def pixel_format(self, fmt: str) -> None:
        self._require_connected()
        # A format change only takes effect on a fresh acquisition, so bounce
        # the stream if it is running.
        was_streaming = self._streaming
        if was_streaming:
            self.stop_stream()
        self.write_enum("PixelFormat", fmt)
        if was_streaming:
            self.start_stream()

    def _count_divisor(self, frame: "np.ndarray | None" = None) -> float:
        """Divisor mapping a raw transport pixel to the native 14-bit ADC count.

        ``Mono16`` MSB-aligns the 14-bit value (<<2 -> divide by 4); ``Mono14``
        is the value directly (divide by 1). Defaults to 4 (Mono16).

        **The data wins over the register.** The camera will accept a write of
        ``PixelFormat = Mono14`` while still streaming Mono16, and trusting the
        register alone then divides by 1 instead of 4 -- inflating counts 4x and
        pinning every pixel at the block's tmax. A true 14-bit stream cannot
        exceed 16383, so any frame above that is Mono16 no matter what the
        register claims. Pass *frame* to enable that check.
        """
        if frame is not None and getattr(frame, "size", 0) and int(np.max(frame)) > 16383:
            return 4.0
        try:
            fmt = self.read_enum("PixelFormat")
        except Exception:
            return 4.0
        if "14" in fmt:
            return 1.0
        return 4.0

    def to_adc_counts(self, frame: np.ndarray) -> np.ndarray:
        """Return *frame* as native 14-bit ADC counts (float).

        The wire frame is a 16-bit transport word: on this camera the 14-bit ADC
        value is left-justified (value << 2), so raw numbers run up to ~65532
        even though the sensor is 14-bit (max 16383). **The calibration is
        defined in ADC counts** -- ``counts_min``/``counts_max`` from
        :meth:`get_calibration` are in this domain -- so this is the number that
        gets converted to temperature, not the wire value.

        The division is done in floating point, so the low bits (which carry
        sub-count precision from the camera's on-board processing, not padding)
        are preserved as a fraction rather than truncated.

        Example::

            f = cam.grab()
            print(f.max())                      # e.g. 65305  (wire, 16-bit)
            print(cam.to_adc_counts(f).max())   # e.g. 16326.25  (ADC, 14-bit)
        """
        return frame.astype(np.float64) / self._count_divisor(frame)

    def check_scene_fit(self, frame: np.ndarray | None = None) -> dict:
        """Report how well the loaded calibration block covers the scene.

        A calibration is only valid between its own ``counts_min``/``counts_max``;
        pixels outside are clamped to ``tmin``/``tmax`` and are not real
        measurements. If a lot of the scene falls outside, the loaded block is
        the wrong temperature range -- this reports that and names the block
        that would fit better.

        Args:
            frame: Frame to assess; grabbed automatically if omitted.

        Returns:
            dict with ``block``, ``tmin``/``tmax``, ``below_pct``/``above_pct``
            (percent of pixels under/over the block's count range),
            ``in_range_pct``, and ``suggestion`` (a better block index, or None).
        """
        self._require_connected()
        if frame is None:
            frame = self.grab()
        cal = self.get_calibration()
        adc = self.to_adc_counts(frame) - self.count_offset
        below = float((adc < cal["counts_min"]).mean() * 100.0)
        above = float((adc > cal["counts_max"]).mean() * 100.0)

        suggestion = None
        if below + above > 5.0:
            # Pick the block whose count range covers the most of this scene.
            best, best_cover = None, -1.0
            for b in self.get_calibration_blocks():
                try:
                    c = self.get_calibration(block=b["index"])
                except Exception:
                    continue
                cover = float(((adc >= c["counts_min"]) & (adc <= c["counts_max"])).mean() * 100.0)
                if cover > best_cover:
                    best, best_cover = b["index"], cover
            if best is not None and best != cal["block"] and best_cover > (100.0 - below - above):
                suggestion = best

        return {
            "block": cal["block"],
            "tmin": cal["tmin"],
            "tmax": cal["tmax"],
            "below_pct": below,
            "above_pct": above,
            "in_range_pct": 100.0 - below - above,
            "suggestion": suggestion,
        }

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
        nuc: bool = True,
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

        Because the integration time changes, the detector's dark level shifts
        and the stored per-pixel offset no longer matches -- leaving every pixel
        with a constant count error, and hence a wrong, block-dependent
        temperature. A :meth:`nuc` (offset update against the internal flag)
        re-levels it, so this method runs one by default once the load has taken
        effect. That is what the vendor software does on range select, and
        skipping it is why switching blocks used to produce nonsense readings.

        Args:
            tag: Exact calibration tag to load (e.g. ``"25mm, Empty, -20C - 55C"``);
                see :meth:`get_calibration_blocks` for available tags. Mutually
                exclusive with *index*.
            index: Browse index (0..``CalibrationQueryIndexMax``) whose tag to
                load. Mutually exclusive with *tag*.
            preset: Preset to load into. Defaults to the active preset.
            timeout: Seconds to wait for the loaded tag to take effect.
            nuc: Run :meth:`nuc` after loading. Leave this on unless you have a
                reason not to -- temperatures are not trustworthy until the
                offset matches the new integration time. The flag is in the
                field of view for a few seconds, so discard frames captured
                during the call.

        Returns:
            dict with ``preset``, ``tag`` (the tag now loaded),
            ``exposure_ms`` (the integration time the load set), and ``nuc``
            (the :meth:`nuc` result, or ``None`` if it was skipped or failed).

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
        # Record the integration time this calibration set, so
        # counts_to_temperature() can detect a later desync (see the warning
        # there -- counts scale directly with integration time).
        exposure = self.exposure_ms
        self._cal_integration_ms = exposure

        # Re-level the offset to the integration time the load just set. A
        # failure here leaves a perfectly valid calibration loaded -- it just
        # isn't offset-corrected yet -- so report it and let the caller decide
        # rather than discarding a successful load.
        nuc_result = None
        if nuc:
            try:
                nuc_result = self.nuc()
            except (CameraError, GVCPError) as exc:
                logger.warning(
                    "Calibration %r loaded, but the follow-up NUC failed: %s. "
                    "Temperatures may be offset until you call cam.nuc().",
                    loaded,
                    exc,
                )
        return {
            "preset": preset,
            "tag": loaded,
            "exposure_ms": exposure,
            "nuc": nuc_result,
        }

    def set_calibration_block(self, index: int) -> None:
        """Load the factory calibration at browse *index* into the active preset.

        Thin wrapper over :meth:`load_calibration` (``index=`` form). Note this
        genuinely loads the calibration -- which also changes the preset's
        integration time, and so runs a :meth:`nuc` afterwards -- unlike merely
        moving the browse cursor. See :meth:`load_calibration` for the full
        contract.
        """
        self.load_calibration(index=index)

    def nuc(self, timeout: float = 30.0, settle: float = 0.5) -> dict:
        """Run a non-uniformity correction (offset update) using the internal flag.

        This is the operation the vendor software performs when you select a
        temperature range: the flag swings into the field of view, the camera
        re-levels every pixel's offset against it, the flag retracts, and the
        image is correct a few seconds later. It is an **offset update layered
        on top of the factory calibration** -- the factory gain terms, which
        were fit against real blackbody sources, are left untouched.

        Run this after :meth:`load_calibration`. Loading a calibration changes
        the preset's integration time, which shifts the detector's dark level;
        until the offset is re-levelled to match, every pixel carries a
        constant count error and the converted temperatures are wrong by a
        block-dependent amount. :meth:`load_calibration` calls this for you by
        default (pass ``nuc=False`` to skip it).

        Also worth running whenever the image drifts -- the camera body warming
        up after power-on is the usual cause. See :meth:`configure_auto_nuc`
        to have the camera do this on its own schedule.

        .. note::

            pyflir intentionally does not expose the camera's other, lower-level
            correction procedure (``CorrectionType`` / ``CorrectionStart`` /
            ``CorrectionAccept``). That one *recomputes and overwrites* the
            stored gain coefficients, and running its one-point mode against the
            flag -- a single near-ambient source -- destroys the factory gain
            terms and visibly corrupts the image. It is a factory procedure
            needing real blackbody sources, not a field operation. See the
            comment above ``REG_CORRECTION_AUTO_ENABLED`` in
            :mod:`pyflir.registers`.

        The stream does not need to be stopped; frames captured while the flag
        is in the field of view show the flag, not the scene, so discard
        anything captured during the call.

        Args:
            timeout: Seconds to wait for the update to finish. The flag motion
                plus averaging typically takes ~5 s.
            settle: Seconds to wait after the camera reports completion, before
                returning, to let the pipeline flush flag frames.

        Returns:
            dict with ``duration_s`` (seconds the update took) and
            ``flag_state`` (the flag's position afterwards, normally
            ``"Stowed"``).

        Raises:
            CameraError: if this camera has no NUC flag, or the update does not
                finish within *timeout*.
        """
        self._require_connected()

        # A unit with no flag has no internal uniform source, so this whole
        # operation is impossible -- fail clearly rather than time out.
        try:
            if not self._gvcp.read_reg(reg.REG_FLAG_PRESENT):
                raise CameraError(
                    "This camera reports no NUC flag (FlagPresent=0), so an "
                    "internal-source correction is not possible. Cover the lens "
                    "with a uniform surface and use the vendor software's "
                    "external-source correction instead."
                )
        except GVCPError as exc:
            logger.debug("Could not read FlagPresent (%s); attempting NUC anyway.", exc)

        started = time.monotonic()
        self._gvcp.write_reg(reg.REG_CORRECTION_AUTO_PERFORM, 1)

        # The in-progress flag does not necessarily latch before the first
        # read, so treat "never saw it go high" as success rather than
        # hanging: poll until it reads 0 *after* having read 1, or until the
        # camera has clearly had time to start and is still idle.
        deadline = started + timeout
        seen_busy = False
        while time.monotonic() < deadline:
            try:
                busy = bool(self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_IN_PROGRESS))
            except GVCPError:
                # The camera is busy reconfiguring and may briefly stop
                # answering; that itself means the update is running.
                busy, seen_busy = True, True
            if busy:
                seen_busy = True
            elif seen_busy or time.monotonic() - started > NUC_START_GRACE_S:
                break
            time.sleep(0.1)
        else:
            raise CameraError(
                f"NUC did not complete within {timeout}s "
                f"(CorrectionAutoInProgress still set)."
            )

        duration = time.monotonic() - started
        if settle > 0:
            time.sleep(settle)

        try:
            flag_state = reg.FLAG_STATE_NAMES.get(
                self._gvcp.read_reg(reg.REG_FLAG_STATE), "unknown"
            )
        except GVCPError:
            flag_state = "unknown"

        logger.info("NUC complete in %.1fs (flag %s).", duration, flag_state)
        return {"duration_s": duration, "flag_state": flag_state}

    def configure_auto_nuc(
        self,
        enabled: bool = True,
        delta_temp: float | None = None,
        delta_time_min: int | None = None,
    ) -> dict:
        """Let the camera re-level its own offset on a drift/time trigger.

        With this on, the camera runs the same offset update as :meth:`nuc`
        by itself whenever a trigger fires, so temperatures stay accurate over
        a long session without the host doing anything. This is how the vendor
        software keeps the image stable; the cost is an occasional few seconds
        of flag frames appearing in the stream at unpredictable moments, which
        is why it is opt-in here rather than on by default.

        Args:
            enabled: Whether the camera should self-correct.
            delta_temp: Front-panel temperature drift in °C that triggers an
                update. ``None`` leaves the camera's current setting (and its
                enable flag) alone; a value enables the trigger.
            delta_time_min: Minutes between updates. ``None`` leaves the
                camera's current setting alone; a value enables the trigger.

        Returns:
            dict of the resulting auto-NUC settings, as read back from the
            camera -- see :meth:`get_auto_nuc_config`.
        """
        self._require_connected()
        if delta_temp is not None:
            self._gvcp.write_float(reg.REG_CORRECTION_AUTO_DELTA_TEMP, float(delta_temp))
            self._gvcp.write_reg(reg.REG_CORRECTION_AUTO_USE_DELTA_TEMP, 1)
        if delta_time_min is not None:
            self._gvcp.write_reg(reg.REG_CORRECTION_AUTO_DELTA_TIME, int(delta_time_min))
            self._gvcp.write_reg(reg.REG_CORRECTION_AUTO_USE_DELTA_TIME, 1)
        self._gvcp.write_reg(reg.REG_CORRECTION_AUTO_ENABLED, 1 if enabled else 0)
        return self.get_auto_nuc_config()

    def get_auto_nuc_config(self) -> dict:
        """Read the camera's automatic-NUC settings.

        Returns:
            dict with ``enabled``, ``use_delta_temp``, ``delta_temp`` (°C),
            ``use_delta_time``, ``delta_time_min``, and ``in_progress``.
        """
        self._require_connected()
        return {
            "enabled": bool(self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_ENABLED)),
            "use_delta_temp": bool(
                self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_USE_DELTA_TEMP)
            ),
            "delta_temp": self._gvcp.read_float(reg.REG_CORRECTION_AUTO_DELTA_TEMP),
            "use_delta_time": bool(
                self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_USE_DELTA_TIME)
            ),
            "delta_time_min": self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_DELTA_TIME),
            "in_progress": bool(
                self._gvcp.read_reg(reg.REG_CORRECTION_AUTO_IN_PROGRESS)
            ),
        }

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
                # R/B/F: the classic FLIR Planck-approximation radiance->temp
                # coefficients, an alternative to the temp polynomial above
                # (apply_calibration(method="rbf")). Min/max radiance bound the
                # valid domain.
                "r": self.read_float("CalibrationQueryRReg"),
                "b": self.read_float("CalibrationQueryBReg"),
                "f": self.read_float("CalibrationQueryFReg"),
                "radiance_min": self.read_float("CalibrationQueryMinRadianceReg"),
                "radiance_max": self.read_float("CalibrationQueryMaxRadianceReg"),
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
        method: str = "rbf",
        clip: bool = True,
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

        # A calibration is fit at one integration time and the counts scale
        # directly with it, so a mismatch silently biases every temperature.
        if self._cal_integration_ms:
            try:
                now_ms = self.exposure_ms
                if abs(now_ms - self._cal_integration_ms) > 0.01 * self._cal_integration_ms:
                    warnings.warn(
                        f"Integration time ({now_ms:.3f} ms) no longer matches the loaded "
                        f"calibration's ({self._cal_integration_ms:.3f} ms). Counts scale "
                        f"with integration time, so temperatures will be wrong by roughly "
                        f"{now_ms / self._cal_integration_ms:.2f}x in signal. Re-run "
                        f"load_calibration() (it resets the integration time).",
                        stacklevel=2,
                    )
            except Exception:
                pass

        cal = self.get_calibration()
        return apply_calibration(
            counts,
            cal,
            emissivity,
            refl_temp_c,
            atm_temp_c,
            tau,
            return_status=return_status,
            count_offset=self.count_offset,
            count_divisor=self._count_divisor(counts),
            method=method,
            clip=clip,
        )

    def set_offset_reference(
        self,
        known_temp_c: float,
        frame: "np.ndarray | None" = None,
        region: "tuple[int, int, int, int] | None" = None,
    ) -> float:
        """Calibrate the host-side count offset against a known-temperature target.

        .. warning::

            Try :meth:`nuc` first. If a uniform surface reads too hot or too
            cold by a roughly constant amount, the usual cause is a detector
            offset that no longer matches the integration time, and the correct
            fix is an offset update on the camera -- not a host-side fudge
            factor. This method exists for a *residual* bias that survives a
            NUC, and it is only as good as your knowledge of the target's true
            temperature. Skin, in particular, is not a reference: its apparent
            temperature depends on emissivity, blood flow, and the ambient
            reflection you are also measuring.

        Point the camera at a target whose temperature you genuinely know, fill
        the frame (or pass *region*), and call this: it measures the shift and
        stores it in :attr:`count_offset`, which
        :meth:`counts_to_temperature`/:meth:`frame_to_celsius` then subtract.
        Requires ``ir_format = "Radiometric"`` (raw counts).

        The target should fill the *region* and be uniform, and the image
        should already be spatially clean, since a single scalar offset cannot
        fix spatial non-uniformity.

        Args:
            known_temp_c: The true temperature of the target, in °C.
            frame: A raw frame to measure; grabbed automatically if omitted.
            region: ``(row0, row1, col0, col1)`` sub-window to average over.
                Defaults to a centred quarter-size box.

        Returns:
            The stored :attr:`count_offset` (14-bit counts).
        """
        self._require_connected()
        cal = self.get_calibration()
        if frame is None:
            frame = self.grab()
        x = frame.astype(np.float64) / self._count_divisor(frame)  # native 14-bit
        if region is None:
            h, w = x.shape
            region = (h // 2 - h // 4, h // 2 + h // 4, w // 2 - w // 4, w // 2 + w // 4)
        r0, r1, c0, c1 = region
        measured = float(np.median(x[r0:r1, c0:c1]))

        # Count (14-bit) that maps to known_temp_c under the calibration.
        c_hi = np.asarray(cal["counts_coeffs"], dtype=np.float64)[::-1]
        t_hi = np.asarray(cal["temp_coeffs"], dtype=np.float64)[::-1]
        bg = float(cal.get("counts_background", 0.0))
        grid = np.linspace(float(cal["counts_min"]), float(cal["counts_max"]), 40001)
        temps = np.polyval(t_hi, np.polyval(c_hi, grid) - bg)
        target = float(grid[int(np.argmin(np.abs(temps - known_temp_c)))])

        self.count_offset = measured - target
        return self.count_offset

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


def replace_bad_pixels(
    frame: np.ndarray,
    mask: "np.ndarray | None" = None,
    sigma: float = 6.0,
) -> "tuple[np.ndarray, np.ndarray]":
    """Interpolate over bad pixels by replacing them with a 3x3 neighbour median.

    Works on raw counts (before radiometric conversion). If *mask* is given
    (e.g. from :meth:`Camera.detect_bad_pixels`), exactly those pixels are
    replaced. If *mask* is ``None``, bad pixels are auto-detected per frame as
    those reading <= 0 or deviating from their local median by more than
    *sigma* robust standard deviations -- convenient for a one-off frame but
    slower and less stable than a precomputed mask.

    Args:
        frame: 2-D array of raw counts.
        mask: Optional boolean array (same shape) of pixels to replace.
        sigma: Outlier threshold (robust std units) for auto-detection.

    Returns:
        ``(corrected_frame, mask)`` -- the corrected copy (same dtype) and the
        boolean mask of replaced pixels.
    """
    f = frame.astype(np.float64)
    h, w = f.shape
    pad = np.pad(f, 1, mode="edge")
    neigh = np.stack([pad[i : i + h, j : j + w] for i in range(3) for j in range(3)])
    med = np.median(neigh, axis=0)
    if mask is None:
        resid = f - med
        mad = float(np.median(np.abs(resid - np.median(resid)))) + 1e-9
        mask = (f <= 0) | (np.abs(resid) > sigma * 1.4826 * mad)
    out = frame.copy()
    out[mask] = med[mask].astype(frame.dtype)
    return out, mask


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
    count_offset: float = 0.0,
    count_divisor: float = 4.0,
    method: str = "rbf",
    clip: bool = True,
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
    tmax = float(cal["tmax"])
    background = float(cal.get("counts_background", 0.0))

    # The A6751sc has a 14-bit ADC. The calibration endpoints counts_min/max are
    # in native 14-bit space. In Mono16 the transport left-justifies the value
    # (uint16 = adc_14bit << 2), so count_divisor=4 brings it back to 14-bit; in
    # Mono14 the stream is already 14-bit, so count_divisor=1. Camera.
    # counts_to_temperature() passes the divisor for the active pixel format.
    x = counts.astype(np.float64) / count_divisor

    # Host-side one-point offset (see Camera.set_offset_reference): the camera's
    # NUC can leave counts shifted from what the calibration expects; subtract
    # the measured shift here, in the 14-bit domain, before the polynomial.
    if count_offset:
        x = x - count_offset

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
    # radiance -> temperature (°C) and its inverse, for the selected method.
    rr, bb, ff = float(cal.get("r", 0.0)), float(cal.get("b", 0.0)), float(cal.get("f", 0.0))
    if method == "rbf" and not (rr and bb):
        # r/b/f unavailable (e.g. a cached cal dict from before this existed) ->
        # fall back to the temperature polynomial rather than produce NaNs.
        method = "polynomial"
    if method == "rbf":
        # FLIR's official radiometric formula (confirmed by FLIR KB a_id/3321):
        #     T_kelvin = B / ln(R / radiance + F)
        # with inverse  radiance = R / (exp(B / T_kelvin) - F). This is the
        # authoritative method; the temperature polynomial below is only a fit.

        def _rad_to_temp_c(w):
            with np.errstate(divide="ignore", invalid="ignore"):
                return bb / np.log(rr / w + ff) - 273.15

        def _temp_c_to_rad(temp_c: float) -> float:
            return rr / (np.exp(bb / (temp_c + 273.15)) - ff)
    else:
        # Polynomial. Detect whether it outputs Kelvin or Celsius from the block
        # endpoints (tmin/tmax are confirmed Celsius): a Kelvin fit reads ~273
        # higher at cmin.
        w_lo = float(np.polyval(c_hi, cmin)) - background
        t_lo = float(np.polyval(t_hi, w_lo))
        offs = 273.15 if abs(t_lo - 273.15 - tmin) < abs(t_lo - tmin) else 0.0
        w_cmin = float(np.polyval(c_hi, cmin)) - background
        w_cmax = float(np.polyval(c_hi, cmax)) - background

        def _rad_to_temp_c(w):
            return np.polyval(t_hi, w) - offs

        def _temp_c_to_rad(temp_c: float) -> float:
            shifted = t_hi.copy()
            shifted[-1] -= temp_c + offs
            roots = np.roots(shifted)
            real_roots = roots[np.abs(roots.imag) < 1e-6].real
            lo, hi = sorted((w_cmin, w_cmax))
            if real_roots.size == 0:
                raise ValueError(
                    f"Could not invert temperature->radiance polynomial for {temp_c} C"
                )
            in_domain = real_roots[(real_roots >= lo) & (real_roots <= hi)]
            candidates = in_domain if in_domain.size else real_roots
            return float(candidates[np.argmin(np.abs(candidates - (lo + hi) / 2))])

    w_obj = w_total
    if emissivity != 1.0 or tau != 1.0:
        w_refl = _temp_c_to_rad(refl_temp_c)
        w_atm = _temp_c_to_rad(atm_temp_c)
        w_obj = (w_total - (1 - emissivity) * tau * w_refl - (1 - tau) * w_atm) / (emissivity * tau)

    result = _rad_to_temp_c(w_obj)

    # The calibration is only valid within its own [tmin, tmax] limits (the SDK
    # carries the same tmin/tmax/rmin/rmax). With clip=True (default) results are
    # clamped to that range, which also stops object-parameter compensation from
    # pushing a saturated pixel to a physically impossible value above tmax.
    #
    # Clamping makes out-of-range pixels *look* like real measurements at the
    # endpoints (a scene that over-ranges reads as a solid block of tmax). With
    # clip=False they become NaN instead, so an unsuitable calibration block is
    # visible rather than silently faked. Either way they are flagged in `status`.
    result = (
        np.clip(result, tmin, tmax) if clip else np.where(status == STATUS_OK, result, np.nan)
    )

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
