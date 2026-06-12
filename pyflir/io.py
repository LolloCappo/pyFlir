"""pyflir.io: File I/O for FLIR thermal recording formats.

Built-in (no extra dependencies)
---------------------------------
- ATS-US  (.ats): FLIR ResearchIR recordings
    read_ats(path)  →  (raw: ndarray, metadata: ATSMetadata)
    FLIRATSReader: full reader class with temperature conversion and export

Optional (pip install pyflir[io])
--------------------------------------
- SFMOV   (.sfmov): FLIR scientific camera sequence files
    read_sfmov(path)       →  raw data array
    read_sfmov_meta(path)  →  metadata dict

Examples
--------
>>> from pyflir.io import read_ats
>>> raw, meta = read_ats("recording.ats")
>>> print(meta.camera_model, raw.shape)

>>> from pyflir.io import read_sfmov, read_sfmov_meta
>>> data = read_sfmov("sequence.sfmov")
>>> meta = read_sfmov_meta("sequence.sfmov")
"""

# ── ATS reader ────────────────────────────────────────────────────────────────

import os
import re
import struct
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# First 6 bytes of the per-frame sync marker (bytes 6-7 vary as a frame counter)
_SYNC_PREFIX = bytes([0x14, 0x00, 0xD5, 0x03, 0x34, 0x00])

# ATS record header size (bytes)
_RECORD_HDR = 77

# Fallback resolution candidates when XML dimensions are absent
_RESOLUTIONS = [
    (1280, 1024), (640, 512), (640, 480),
    (384, 288), (320, 256), (320, 240), (160, 120),
]


@dataclass
class ATSMetadata:
    """All metadata extracted from a FLIR ATS-US file.

    Attributes
    ----------
    filepath : str
        Absolute path to the source file.
    file_size_bytes : int
        File size in bytes.

    Camera
    ------
    camera_model : str or None
    camera_part  : str or None
    lens         : str or None
    filter       : str or None

    Acquisition
    -----------
    width, height : int
        Image dimensions in pixels.
    n_frames : int
        Number of frames loaded.
    frame_start_byte : int
        Byte offset of the first frame block in the file.
    stride_bytes : int
        Bytes between consecutive frame block starts.
    sync_row_bytes : int
        Bytes skipped at the start of each block (sync header row).

    Scene / object parameters
    -------------------------
    emissivity, distance, relative_humidity : float or None
    reflected_temp, atmosphere_temp : float or None
    ext_optics_temp, ext_optics_transmission : float or None

    Calibration ranges (from XML)
    -----------------------------
    range_counts_min/max, range_radiance_min/max,
    range_temperaturec_min/max, range_temperaturek_min/max,
    range_temperaturef_min/max, range_temperaturer_min/max : float or None

    Source / display
    ----------------
    source_unit : str or None
    temperature_type : str or None
    apply_nuc, apply_bp : str or None
    display_min_C, display_max_C : float or None
    display_mode : str or None
    scale_mode : str or None
    segmentation_enabled : str or None
    """

    # File
    filepath: str = ""
    file_size_bytes: int = 0

    # Camera
    camera_model: Optional[str] = None
    camera_part:  Optional[str] = None
    lens:         Optional[str] = None
    filter:       Optional[str] = None

    # Acquisition
    width:            int = 0
    height:           int = 0
    n_frames:         int = 0
    frame_start_byte: int = 0
    stride_bytes:     int = 0
    sync_row_bytes:   int = 0

    # Scene parameters
    emissivity:               Optional[float] = None
    distance:                 Optional[float] = None
    relative_humidity:        Optional[float] = None
    reflected_temp:           Optional[float] = None
    atmosphere_temp:          Optional[float] = None
    ext_optics_temp:          Optional[float] = None
    ext_optics_transmission:  Optional[float] = None

    # Calibration ranges
    range_counts_min:       Optional[float] = None
    range_counts_max:       Optional[float] = None
    range_radiance_min:     Optional[float] = None
    range_radiance_max:     Optional[float] = None
    range_temperaturec_min: Optional[float] = None
    range_temperaturec_max: Optional[float] = None
    range_temperaturek_min: Optional[float] = None
    range_temperaturek_max: Optional[float] = None
    range_temperaturef_min: Optional[float] = None
    range_temperaturef_max: Optional[float] = None
    range_temperaturer_min: Optional[float] = None
    range_temperaturer_max: Optional[float] = None

    # Source / display
    source_unit:            Optional[str]   = None
    temperature_type:       Optional[str]   = None
    apply_nuc:              Optional[str]   = None
    apply_bp:               Optional[str]   = None
    display_min_C:          Optional[float] = None
    display_max_C:          Optional[float] = None
    display_mode:           Optional[str]   = None
    scale_mode:             Optional[str]   = None
    segmentation_enabled:   Optional[str]   = None

    def as_dict(self) -> dict:
        """Return all metadata as a plain dictionary."""
        import dataclasses
        return dataclasses.asdict(self)

    def __str__(self) -> str:
        lines = [
            "=" * 56,
            "  FLIR ATS-US Recording",
            "=" * 56,
            f"  File          : {os.path.basename(self.filepath)}",
            f"  Size          : {self.file_size_bytes:,} bytes",
            "",
            f"  Camera model  : {self.camera_model or 'unknown'}",
            f"  Part / serial : {self.camera_part  or 'unknown'}",
            f"  Lens          : {self.lens         or 'unknown'}",
            f"  Filter        : {self.filter       or 'unknown'}",
            "",
            f"  Resolution    : {self.width} × {self.height} px",
            f"  Frames        : {self.n_frames}",
            "",
        ]

        scene = {
            "emissivity":            self.emissivity,
            "distance":              self.distance,
            "relative_humidity":     self.relative_humidity,
            "reflected_temp":        self.reflected_temp,
            "atmosphere_temp":       self.atmosphere_temp,
            "ext_optics_temp":       self.ext_optics_temp,
            "ext_optics_transmission": self.ext_optics_transmission,
        }
        scene_lines = [f"    {k:28s}: {v:.6g}" for k, v in scene.items() if v is not None]
        if scene_lines:
            lines += ["  Scene parameters:"] + scene_lines + [""]

        cal_pairs = [
            ("counts",   self.range_counts_min,       self.range_counts_max),
            ("radiance", self.range_radiance_min,     self.range_radiance_max),
            ("temp_C",   self.range_temperaturec_min, self.range_temperaturec_max),
            ("temp_K",   self.range_temperaturek_min, self.range_temperaturek_max),
            ("temp_F",   self.range_temperaturef_min, self.range_temperaturef_max),
            ("temp_R",   self.range_temperaturer_min, self.range_temperaturer_max),
        ]
        cal_lines = [f"    {lbl:14s}: {lo:.6g} – {hi:.6g}"
                     for lbl, lo, hi in cal_pairs if lo is not None]
        if cal_lines:
            lines += ["  Calibration ranges (from XML):"] + cal_lines + [""]

        src = {
            "source_unit":      self.source_unit,
            "temperature_type": self.temperature_type,
            "apply_nuc":        self.apply_nuc,
            "apply_bp":         self.apply_bp,
        }
        src_lines = [f"    {k:28s}: {v}" for k, v in src.items() if v is not None]
        if self.display_min_C is not None:
            src_lines.append(
                f"    {'display_window_C':28s}: {self.display_min_C:.2f} – {self.display_max_C:.2f}"
            )
        if src_lines:
            lines += ["  Source / display:"] + src_lines + [""]

        lines += [
            "  File layout:",
            f"    {'frame_start_byte':28s}: {self.frame_start_byte:,}",
            f"    {'stride_bytes':28s}: {self.stride_bytes:,}",
            f"    {'sync_row_bytes':28s}: {self.sync_row_bytes}",
            "=" * 56,
        ]
        return "\n".join(lines)


def _get_xml(buf: bytes) -> Optional[str]:
    s = buf.find(b"<workspaceFileSettings>")
    if s == -1:
        return None
    e = buf.find(b"</workspaceFileSettings>", s)
    e = (e + len(b"</workspaceFileSettings>")) if e != -1 else s + 65536
    return buf[s:e].decode("latin-1", errors="replace")


def _parse_xml(xml: str) -> dict:
    raw = {}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        try:
            root = ET.fromstring("<r>" + xml + "</r>")
        except ET.ParseError:
            root = None

    if root is not None:
        for el in root.iter("roiList"):
            if "imageWidth" in el.attrib:
                raw["image_width"]  = int(el.attrib["imageWidth"])
                raw["image_height"] = int(el.attrib["imageHeight"])
        for el in root.iter("segmentationValue"):
            unit = el.attrib.get("unit", "")
            lo, hi = el.attrib.get("min"), el.attrib.get("max")
            if lo and hi:
                k = unit.lower().replace(" ", "_")
                raw[f"range_{k}_min"] = float(lo)
                raw[f"range_{k}_max"] = float(hi)
        for el in root.iter("objectParameters"):
            _map = {
                "emissivity":           "emissivity",
                "distance":             "distance",
                "relativeHumidity":     "relative_humidity",
                "reflectedTemp":        "reflected_temp",
                "atmosphereTemp":       "atmosphere_temp",
                "extOpticsTemp":        "ext_optics_temp",
                "extOpticsTransmission":"ext_optics_transmission",
            }
            for xml_key, py_key in _map.items():
                if xml_key in el.attrib:
                    raw[py_key] = float(el.attrib[xml_key])
        for el in root.iter("source"):
            if "unit" in el.attrib:
                raw["source_unit"] = el.attrib["unit"]
            if "temperatureType" in el.attrib:
                raw["temperature_type"] = el.attrib["temperatureType"]
            if "applyNUC" in el.attrib:
                raw["apply_nuc"] = el.attrib["applyNUC"]
            if "applyBP" in el.attrib:
                raw["apply_bp"] = el.attrib["applyBP"]
        for el in root.iter("levelAndSpan"):
            raw["display_mode"] = el.attrib.get("mode", "")
            for sub in el.iter("levelAndSpanValue"):
                if sub.attrib.get("unit") == "TemperatureC":
                    raw["display_min_C"] = float(sub.attrib.get("min", 0))
                    raw["display_max_C"] = float(sub.attrib.get("max", 0))
        for el in root.iter("scaleOptions"):
            raw["scale_mode"] = el.attrib.get("mode", "")
        for el in root.iter("segmentation"):
            raw["segmentation_enabled"] = el.attrib.get("enabled", "False")

    if "image_width" not in raw:
        mw = re.search(r'imageWidth="(\d+)"', xml)
        mh = re.search(r'imageHeight="(\d+)"', xml)
        if mw and mh:
            raw["image_width"]  = int(mw.group(1))
            raw["image_height"] = int(mh.group(1))
    return raw


def _parse_source_info(buf: bytes) -> dict:
    """Extract camera ID strings and image dimensions from the SourceInfo record.

    Layout confirmed on FLIR A8581 and A6751sc:
      payload offset  80 : image width  (uint16 LE)
      payload offset  82 : image height (uint16 LE)
      payload offset 142 : camera model (null-terminated ASCII)
      payload offset 190 : part / serial number
      payload offset 206 : lens description
      payload offset 270 : filter description
    """
    idx = buf.find(b"SourceInfo")
    if idx == -1:
        return {}

    p = idx + _RECORD_HDR

    def read_nts(offset: int) -> Optional[str]:
        end = offset
        while p + end < len(buf) and 32 <= buf[p + end] < 127:
            end += 1
        s = buf[p + offset : p + end].decode("ascii", errors="replace").strip()
        if len(s) >= 2 and all(c.isalnum() or c in " .-_/" for c in s):
            return s
        return None

    out: dict = {
        "camera_model": read_nts(142),
        "camera_part":  read_nts(190),
        "lens":         read_nts(206),
        "filter":       read_nts(270),
    }

    if p + 84 <= len(buf):
        w = struct.unpack_from("<H", buf, p + 80)[0]
        h = struct.unpack_from("<H", buf, p + 82)[0]
        if w > 0 and h > 0:
            out["image_width"]  = w
            out["image_height"] = h

    return out


def _find_sync(buf: bytes, search_limit: int = 50_000) -> int:
    """Return the byte offset of the first frame sync marker, or -1."""
    _KNOWN_PREFIXES = [
        bytes([0x14, 0x00, 0xD5, 0x03, 0x34, 0x00]),  # FLIR A8581
        bytes([0x14, 0x00, 0xD9, 0x04, 0x03, 0x70]),  # FLIR A6751sc
    ]

    for prefix in _KNOWN_PREFIXES:
        idx = buf.find(prefix)
        if 0 <= idx < search_limit:
            return idx

    min_stride = min((h + 1) * w * 2 for w, h in _RESOLUTIONS)
    for offset in range(0, min(search_limit, len(buf) - 6), 2):
        candidate = buf[offset : offset + 6]
        if candidate == bytes(6):
            continue
        matches = 0
        for w, h in _RESOLUTIONS:
            stride = (h + 1) * w * 2
            next_pos = offset + stride
            if next_pos + 6 <= len(buf) and buf[next_pos : next_pos + 6] == candidate:
                matches += 1
                if matches >= 2:
                    return offset
    return -1


def _detect_layout(buf: bytes, w: int, h: int) -> tuple:
    """Auto-detect frame layout. Returns (frame0_offset, stride, sync_row_bytes)."""
    f0 = _find_sync(buf)
    if f0 == -1:
        raise RuntimeError(
            "Sync marker not found in first 50 KB of file. "
            "The file may not be a supported ATS-US variant."
        )

    row_bytes   = w * 2
    image_bytes = w * h * 2
    expected    = (h + 1) * row_bytes

    p1 = f0 + expected + row_bytes
    if p1 + image_bytes <= len(buf):
        c0 = np.frombuffer(buf[f0 + row_bytes : f0 + row_bytes + image_bytes], dtype="<u2").reshape(h, w)
        c1 = np.frombuffer(buf[p1 : p1 + image_bytes], dtype="<u2").reshape(h, w)
        mad = float(np.abs(c0[10:-10, 10:-10].astype(np.int32) - c1[10:-10, 10:-10].astype(np.int32)).mean())
        if mad < 2000:
            return f0, expected, row_bytes

    c0 = np.frombuffer(buf[f0 + row_bytes : f0 + row_bytes + image_bytes], dtype="<u2").reshape(h, w)[10:h-10, 10:w-10].astype(np.float32)
    best_mad, best_stride = 1e18, expected
    for stride in range(expected - 5 * row_bytes, expected + 5 * row_bytes + 1, 2):
        p = f0 + stride + row_bytes
        if p + image_bytes > len(buf):
            break
        c1 = np.frombuffer(buf[p : p + image_bytes], dtype="<u2").reshape(h, w)[10:h-10, 10:w-10].astype(np.float32)
        mad = float(np.abs(c0 - c1).mean())
        if mad < best_mad:
            best_mad, best_stride = mad, stride

    return f0, best_stride, row_bytes


class FLIRATSReader:
    """Reader for FLIR ATS-US thermal recording files (.ats).

    Parameters
    ----------
    filepath : str
        Path to the .ats file.

    Attributes (populated after calling read())
    -------------------------------------------
    raw : np.ndarray, shape (N, H, W), dtype uint16
        Raw sensor counts for all N frames.
    temperature_C : np.ndarray, shape (N, H, W), dtype float32, or None
        Temperature in °C using linear calibration from the embedded XML.
        None if calibration data is absent.
    metadata : ATSMetadata
        All extracted metadata as a structured dataclass.

    Examples
    --------
    >>> reader = FLIRATSReader("recording.ats").read()
    >>> print(reader.metadata)
    >>> print(reader.raw.shape)          # (N, H, W)
    >>> print(reader.temperature_C.shape)
    """

    FILE_MAGIC = b"FLIR ATS-US File"

    def __init__(self, filepath: str):
        self.filepath = os.path.abspath(filepath)
        self.raw:           Optional[np.ndarray] = None
        self.temperature_C: Optional[np.ndarray] = None
        self.metadata:      Optional[ATSMetadata] = None

    def read(self) -> "FLIRATSReader":
        """Parse the file and populate raw, temperature_C, and metadata. Returns self."""
        with open(self.filepath, "rb") as fh:
            buf = fh.read()

        if not buf[:16].startswith(self.FILE_MAGIC):
            raise ValueError(
                f"Not a FLIR ATS-US file (got magic bytes {buf[:16]!r})"
            )

        meta = ATSMetadata(filepath=self.filepath, file_size_bytes=len(buf))

        si = _parse_source_info(buf)
        for key, val in si.items():
            if hasattr(meta, key):
                setattr(meta, key, val)

        xml = _get_xml(buf)
        if xml:
            xd = _parse_xml(xml)
            for key, val in xd.items():
                if hasattr(meta, key):
                    setattr(meta, key, val)
            w = xd.get("image_width")
            h = xd.get("image_height")
        else:
            w = h = None

        if w is None or h is None:
            w = si.get("image_width")
            h = si.get("image_height")

        if w is None or h is None:
            warnings.warn(
                "No image dimensions found in XML metadata or SourceInfo record. "
                f"Falling back to resolution candidates {_RESOLUTIONS}. "
                "If the recording uses a non-standard resolution, frames will be corrupted.",
                RuntimeWarning,
                stacklevel=2,
            )
            w, h = _RESOLUTIONS[0]

        f0, stride, row_skip = _detect_layout(buf, w, h)
        image_bytes = w * h * 2

        n_max = (len(buf) - f0) // stride
        out = np.empty((n_max, h, w), dtype=np.uint16)
        count = 0
        for i in range(n_max):
            img_start = f0 + i * stride + row_skip
            if img_start + image_bytes > len(buf):
                break
            out[count] = np.frombuffer(buf[img_start : img_start + image_bytes], dtype="<u2").reshape(h, w)
            count += 1

        if count == 0:
            raise RuntimeError("No image frames found in file.")

        self.raw = out[:count]
        self.temperature_C = self._compute_celsius(meta)

        meta.width            = w
        meta.height           = h
        meta.n_frames         = count
        meta.frame_start_byte = f0
        meta.stride_bytes     = stride
        meta.sync_row_bytes   = row_skip
        self.metadata = meta

        return self

    def get_temperature(self, unit: str = "C") -> np.ndarray:
        """Return temperature array in the requested unit (C, K, or F)."""
        if self.temperature_C is None:
            raise RuntimeError(
                "No calibration data found in file; cannot convert to temperature."
            )
        u = unit.upper()
        if u == "C":
            return self.temperature_C
        if u == "K":
            return self.temperature_C + 273.15
        if u == "F":
            return self.temperature_C * 9.0 / 5.0 + 32.0
        raise ValueError(f"Unknown unit '{unit}'. Use 'C', 'K', or 'F'.")

    def export_csv(self, idx: int = 0, unit: str = "C",
                   path: Optional[str] = None) -> str:
        """Save one frame as a CSV file. Returns the output path."""
        self._check_read()
        arr = (self.raw[idx].astype(float) if unit.upper() == "COUNTS"
               else self.get_temperature(unit)[idx])
        path = path or f"frame_{idx:04d}_{unit.upper()}.csv"
        np.savetxt(path, arr, delimiter=",", fmt="%.4f")
        return path

    def export_npy(self, path: Optional[str] = None) -> str:
        """Save all raw uint16 frames as a .npy file. Returns the output path."""
        self._check_read()
        path = path or os.path.splitext(self.filepath)[0] + "_raw.npy"
        np.save(path, self.raw)
        return path

    def export_tiff(self, unit: str = "C", path: Optional[str] = None) -> str:
        """Save all frames as a multi-page float32 TIFF. Requires tifffile."""
        try:
            import tifffile
        except ImportError as exc:
            raise ImportError(
                "tifffile is required for TIFF export: pip install tifffile"
            ) from exc
        self._check_read()
        arr = (self.raw.astype(np.float32) if unit.upper() == "COUNTS"
               else self.get_temperature(unit))
        path = path or os.path.splitext(self.filepath)[0] + f"_{unit.upper()}.tiff"
        tifffile.imwrite(path, arr, photometric="minisblack")
        return path

    def _check_read(self):
        if self.raw is None:
            raise RuntimeError("Call read() before exporting.")

    def _compute_celsius(self, meta: ATSMetadata) -> Optional[np.ndarray]:
        lo_c = meta.range_temperaturec_min
        hi_c = meta.range_temperaturec_max
        lo_k = meta.range_counts_min
        hi_k = meta.range_counts_max
        if None in (lo_c, hi_c, lo_k, hi_k):
            return None
        if hi_k == lo_k:
            # Degenerate calibration block: no count range to map.
            return None
        slope = (hi_c - lo_c) / (hi_k - lo_k)
        result = self.raw.astype(np.float32)
        result -= lo_k
        result *= slope
        result += lo_c
        return result


def read_ats(filepath: str) -> tuple:
    """Read a FLIR ATS-US recording file (.ats).

    Returns
    -------
    raw : np.ndarray, shape (N, H, W), dtype uint16
    metadata : ATSMetadata

    Examples
    --------
    >>> from pyflir.io import read_ats
    >>> raw, meta = read_ats("recording.ats")
    >>> print(meta.camera_model, raw.shape)
    """
    reader = FLIRATSReader(filepath).read()
    return reader.raw, reader.metadata


# ── SFMOV reader (optional; requires pysfmov) ────────────────────────────────

def read_sfmov(filepath: str):
    """Read a FLIR SFMOV sequence file (.sfmov).

    Requires ``pysfmov``: pip install pyflir[io]
    """
    try:
        import pysfmov
    except ImportError:
        raise ImportError(
            "pysfmov is required to read .sfmov files.\n"
            "Install it with:  pip install pyflir[io]"
        )
    return pysfmov.get_data(filepath)


def read_sfmov_meta(filepath: str) -> dict:
    """Read metadata from a FLIR SFMOV sequence file (.sfmov).

    Requires ``pysfmov``: pip install pyflir[io]
    """
    try:
        import pysfmov
    except ImportError:
        raise ImportError(
            "pysfmov is required to read .sfmov files.\n"
            "Install it with:  pip install pyflir[io]"
        )
    return pysfmov.get_meta_data(filepath)


__all__ = [
    "FLIRATSReader",
    "ATSMetadata",
    "read_ats",
    "read_sfmov",
    "read_sfmov_meta",
]
