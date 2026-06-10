"""Continuous acquisition for a live display.

Uses ``read(latest=True)`` so the displayed frame never lags behind
real time when the draw loop is slower than the camera frame rate.

Run with::

    python examples/02_continuous_live.py
"""

from __future__ import annotations

import time

from pyflir import Camera


def main() -> None:
    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        cam.frame_rate  = 30.0
        cam.exposure_ms = 8.0

        cam.start_stream()
        try:
            t_end = time.monotonic() + 5.0
            while time.monotonic() < t_end:
                frame = cam.read(timeout=2.0, latest=True)
                if frame is not None:
                    print(f"latest frame mean: {frame.mean():.0f}")
        finally:
            cam.stop_stream()


if __name__ == "__main__":
    main()
