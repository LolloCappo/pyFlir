"""GenICam XML parser for FLIR cameras.

Extracts register addresses and metadata from a camera's GenICam XML,
downloaded via pyGigEVision.fetch_genicam_xml().

Supports the most common node types with direct <Address> tags:
  Integer, IntReg, Float, FloatReg, Enumeration, Command, Boolean,
  Register, StringReg

Nodes whose address is computed via <pAddress> or IntSwissKnife are
skipped — these are a small minority in typical FLIR XMLs.
"""
import io
import struct
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Bootstrap register address for FIRST_URL (GigE Vision spec, §16.3)
_REG_FIRST_URL = 0x00000200


@dataclass
class RegNode:
    """Describes one GenICam feature backed by a hardware register."""
    name:         str
    node_type:    str           # Integer, Float, Enumeration, Command, …
    address:      int           # byte address in camera register space
    length:       int = 4       # register length in bytes
    access:       str = "RW"    # RO | WO | RW | NA
    endianness:   str = "Big"   # Big | Little
    sign:         str = "Unsigned"
    cmd_value:    Optional[int] = None
    enum_entries: Dict[str, int] = field(default_factory=dict)
    description:  str = ""


def parse_genicam_xml(xml_bytes: bytes) -> Dict[str, RegNode]:
    """Parse a GenICam XML blob and return {feature_name: RegNode}.

    Only features with a plain numeric <Address> tag are included.
    Features whose address is computed at runtime (via <pAddress>,
    IntSwissKnife, StructEntry, etc.) are silently skipped.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Could not parse GenICam XML: {exc}") from exc

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def _tag(name: str) -> str:
        return f"{ns}{name}" if ns else name

    def _int(text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        text = text.strip()
        try:
            return int(text, 0)
        except ValueError:
            return None

    def _child_text(elem: ET.Element, child_name: str) -> Optional[str]:
        child = elem.find(_tag(child_name))
        return child.text if child is not None else None

    nodes: Dict[str, RegNode] = {}

    KNOWN_TYPES = (
        "Integer", "IntReg",
        "Float", "FloatReg",
        "Enumeration",
        "Command",
        "Boolean",
        "Register",
        "StringReg",
    )

    for node_type in KNOWN_TYPES:
        for elem in root.iter(_tag(node_type)):
            name = elem.get("Name")
            if not name:
                continue

            addr_text = _child_text(elem, "Address")
            if addr_text is None:
                continue
            address = _int(addr_text)
            if address is None:
                continue

            length      = _int(_child_text(elem, "Length")) or 4
            access      = (_child_text(elem, "AccessMode") or "RW").strip()
            endianness  = (_child_text(elem, "Endianess") or "Big").strip()
            sign        = (_child_text(elem, "Sign") or "Unsigned").strip()
            description = (_child_text(elem, "Description") or "").strip()

            node = RegNode(
                name=name,
                node_type=node_type,
                address=address,
                length=length,
                access=access,
                endianness=endianness,
                sign=sign,
                description=description,
            )

            if node_type == "Command":
                node.cmd_value = _int(_child_text(elem, "CommandValue"))

            if node_type == "Enumeration":
                for entry in elem.iter(_tag("EnumEntry")):
                    entry_name = entry.get("Name", "")
                    val = _int(_child_text(entry, "Value"))
                    if entry_name and val is not None:
                        node.enum_entries[entry_name] = val

            nodes[name] = node

    return nodes


# ---------------------------------------------------------------------------
# Robust XML downloader (works around pyGigEVision's hex-prefix assumption)
# ---------------------------------------------------------------------------

def _parse_url_int(s: str, flir_bare_hex: bool = False) -> int:
    """Parse one numeric field from a GigE Vision FIRST_URL.

    Standard cameras use '0x'-prefixed hex or plain decimal.
    FLIR cameras omit the '0x' prefix on both the address and the size
    fields.  When *flir_bare_hex* is True the string is always decoded
    as hexadecimal regardless of whether it contains a–f.
    """
    s = s.strip()
    if flir_bare_hex or any(c in s.lower() for c in "abcdef"):
        return int(s, 16)
    return int(s, 0)


def _read_mem_aligned(gvcp_client, addr: int, size: int, chunk: int = 512) -> bytes:
    """Read *size* bytes from *addr*, rounding every chunk to a 4-byte multiple.

    The FLIR A6751sc (and possibly other FLIR cameras) rejects READMEM
    requests whose byte-count is not a multiple of 4.  This wrapper rounds
    each chunk up and trims the result to the requested size.
    """
    result = bytearray()
    offset = 0
    while offset < size:
        want = min(chunk, size - offset)
        # Round up to next multiple of 4
        aligned = (want + 3) & ~3
        data = gvcp_client._read_mem_raw(addr + offset, aligned)
        result.extend(data[:want])
        offset += want
    return bytes(result)


def fetch_genicam_xml(gvcp_client) -> tuple:
    """Download and return (xml_bytes, filename) from the camera.

    Works around two pyGigEVision bugs seen with FLIR cameras:
    1. Hex addresses in FIRST_URL without '0x' prefix cause ValueError.
    2. READMEM rejects byte-counts that are not multiples of 4.
    """
    url_bytes = gvcp_client.read_mem(_REG_FIRST_URL, 512)
    url = url_bytes.split(b"\x00", 1)[0].decode("ascii", errors="replace")
    parts = url.split(";")
    if len(parts) < 3:
        raise ValueError(f"Malformed FIRST_URL: {url!r}")
    filename = parts[0].split(":")[-1]
    # If the address field contains a–f (no 0x prefix) it is FLIR-style bare
    # hex — treat the size field as bare hex too (same convention, same URL).
    flir_hex = any(c in parts[1].lower() for c in "abcdef")
    addr = _parse_url_int(parts[1], flir_bare_hex=flir_hex)
    size = _parse_url_int(parts[2], flir_bare_hex=flir_hex)

    data = _read_mem_aligned(gvcp_client, addr, size)

    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
            return zf.read(xml_name), xml_name

    return data, filename


# ---------------------------------------------------------------------------
# Register read/write helpers
# ---------------------------------------------------------------------------

def reg_to_float(raw: int) -> float:
    """Interpret a 32-bit register value as a big-endian IEEE 754 float."""
    return struct.unpack(">f", struct.pack(">I", raw))[0]


def float_to_reg(value: float) -> int:
    """Pack a Python float into a 32-bit big-endian register word."""
    return struct.unpack(">I", struct.pack(">f", value))[0]


def reg_to_double(raw_hi: int, raw_lo: int) -> float:
    """Interpret two 32-bit registers (hi, lo) as a big-endian double."""
    return struct.unpack(">d", struct.pack(">II", raw_hi, raw_lo))[0]


def double_to_regs(value: float) -> Tuple[int, int]:
    """Pack a Python float into two 32-bit big-endian register words (hi, lo)."""
    hi, lo = struct.unpack(">II", struct.pack(">d", value))
    return hi, lo


def summarise(nodes: Dict[str, RegNode], filter_type: Optional[str] = None) -> str:
    """Return a multi-line summary of the parsed register map."""
    lines: List[str] = []
    for name, node in sorted(nodes.items()):
        if filter_type and node.node_type != filter_type:
            continue
        line = (f"  {name:<40s}  {node.node_type:<12s}  "
                f"addr=0x{node.address:08X}  len={node.length}  {node.access}")
        if node.node_type == "Enumeration" and node.enum_entries:
            entries = ", ".join(f"{k}={v}" for k, v in sorted(node.enum_entries.items()))
            line += f"  [{entries}]"
        if node.node_type == "Command" and node.cmd_value is not None:
            line += f"  cmd_value={node.cmd_value}"
        lines.append(line)
    return "\n".join(lines)
