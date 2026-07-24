"""Tests for the GenICam XML parser."""

from pyflir.genicam import parse_genicam_xml

# An Enumeration carries the name->value map but references its backing register
# via <pValue> and has no <Address> of its own. The parser must copy the entries
# onto the register node so read_enum()/write_enum() can map names.
_XML = """<?xml version="1.0"?>
<RegisterDescription xmlns="http://www.genicam.org/GenApi/Version_1_1">
  <Enumeration Name="PixelFormat">
    <EnumEntry Name="Mono14"><Value>17825829</Value></EnumEntry>
    <EnumEntry Name="Mono16"><Value>17825799</Value></EnumEntry>
    <pValue>PixelFormatReg</pValue>
  </Enumeration>
  <IntReg Name="PixelFormatReg">
    <Address>0x1000</Address>
    <Length>4</Length>
    <AccessMode>RW</AccessMode>
  </IntReg>
</RegisterDescription>
"""


class TestEnumEntryAttachment:
    def test_enum_entries_attached_to_backing_register(self):
        nodes = parse_genicam_xml(_XML)
        # The address-less Enumeration node itself is not stored.
        assert "PixelFormat" not in nodes
        reg = nodes["PixelFormatReg"]
        assert reg.address == 0x1000
        # ...but its entries now live on the register node (both directions).
        assert reg.enum_entries == {"Mono14": 17825829, "Mono16": 17825799}
