"""List and load FLIR calibration blocks (temperature ranges).

FLIR cameras store multiple calibration blocks, each covering a
different temperature range. Select the block that matches your
scene temperature for best radiometric accuracy.

Run with::

    python examples/03_calibration_block.py
"""

from __future__ import annotations

from pyflir import Camera


def main() -> None:
    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        blocks = cam.get_calibration_blocks()
        print("Available calibration blocks:")
        for b in blocks:
            print(f"  [{b['index']}] {b.get('name', '')}  "
                  f"range={b['tmin']:.0f}–{b['tmax']:.0f} °C  "
                  f"lens={b.get('lens', 'n/a')}")

        active = cam.get_calibration_block()
        print(f"\nActive block: {active}")

        # Select block 0 (the first / coldest range)
        cam.set_calibration_block(0)
        print(f"Set to block 0: {blocks[0]}")

        # Trigger a NUC (non-uniformity correction) after switching blocks
        cam.trigger_nuc()
        print("NUC triggered.")


if __name__ == "__main__":
    main()
