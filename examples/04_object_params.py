"""Set radiometric object parameters for accurate temperature measurement.

Correct emissivity, object distance, atmospheric temperature,
reflected temperature, and relative humidity all affect the
accuracy of radiometric temperature readings.

Run with::

    python examples/04_object_params.py
"""

from __future__ import annotations

from pyflir import Camera


def main() -> None:
    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        # Read current parameters
        params = cam.get_object_params()
        print("Current object parameters:")
        for k, v in params.items():
            print(f"  {k}: {v}")

        # Set parameters for a typical outdoor scene
        cam.set_object_params(
            emissivity=0.95,        # most real surfaces are 0.90–0.98
            distance_m=3.0,         # 3 metres from object to camera
            atm_temp_K=293.15,      # 20 °C atmospheric temperature
            refl_temp_K=293.15,     # 20 °C reflected temperature
            humidity=0.50,          # 50 % relative humidity
        )
        print("\nUpdated object parameters:")
        for k, v in cam.get_object_params().items():
            print(f"  {k}: {v}")

        frame = cam.grab()
        print(f"\nFrame: {frame.shape}, mean={frame.mean():.0f}")


if __name__ == "__main__":
    main()
