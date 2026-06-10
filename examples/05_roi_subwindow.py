"""Reduce the ROI for higher frame rates.

Reducing the acquisition region allows the camera to stream faster.
This script queries the ROI limits, selects a smaller window, and
reports the maximum achievable frame rate.

Run with::

    python examples/05_roi_subwindow.py
"""

from __future__ import annotations

import numpy as np

from pyflir import Camera


def main() -> None:
    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        print("Full ROI:", cam.get_roi())
        print("ROI limits:", cam.get_roi_limits())
        if cam.frame_rate_max is not None:
            print(f"Max frame rate at full ROI: {cam.frame_rate_max:.1f} Hz")

        # Switch to a smaller ROI centred on the sensor
        limits = cam.get_roi_limits()
        w_inc = limits["width_inc"] or 1
        h_inc = limits["height_inc"] or 1
        new_w = max(limits["width_min"], 128 - (128 % w_inc))
        new_h = max(limits["height_min"], 64 - (64 % h_inc))

        cam.set_roi(new_w, new_h)
        print(f"\nNew ROI: {cam.get_roi()}")
        if cam.frame_rate_max is not None:
            print(f"Max frame rate at {new_w}×{new_h}: {cam.frame_rate_max:.1f} Hz")
            cam.frame_rate = cam.frame_rate_max

        frames = cam.acquire(5)
        arr = np.stack(frames)
        print(f"Acquired: {arr.shape}, dtype={arr.dtype}")

        # Restore full sensor ROI
        cam.set_roi(limits["sensor_width"], limits["sensor_height"])
        print("Restored full ROI.")


if __name__ == "__main__":
    main()
