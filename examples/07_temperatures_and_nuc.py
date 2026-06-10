"""Read temperature sensors and work with the NUC flag.

FLIR cameras expose several on-board temperature sensors. The internal
flag can be moved in front of the detector for a flat-field NUC, or
stowed to resume imaging.

Run with::

    python examples/07_temperatures_and_nuc.py
"""

from __future__ import annotations

from pyflir import Camera
from pyflir.registers import TEMP_SENSORS


def main() -> None:
    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        # Read all temperature sensors
        print("Temperature sensors:")
        temps = cam.get_temperatures()
        for name, celsius in temps.items():
            print(f"  {name:30s}: {celsius:.1f} °C")

        # FPA (detector) temperature via property
        print(f"\nFPA temperature: {cam.detector_temperature:.1f} °C")

        # Perform a flat-field NUC using the internal flag
        print("\nMoving flag in front of detector …")
        cam.flag_move_in_fov()
        cam.trigger_nuc()
        print("NUC triggered. Stowing flag …")
        cam.flag_move_stowed()
        print("Ready.")


if __name__ == "__main__":
    main()
