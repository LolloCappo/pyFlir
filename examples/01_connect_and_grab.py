"""Connect to a FLIR camera and grab one frame.

Run with::

    python examples/01_connect_and_grab.py
"""

from __future__ import annotations

from pyflir import Camera, discover


def main() -> None:
    for cam in discover():
        print(f"{cam.get('manufacturer', '?')} {cam.get('model', '?')} at {cam['ip']}")

    with Camera() as cam:
        cam.download_xml()
        cam.load_xml(f"camera_{cam.serial or cam.ip.replace('.', '_')}.xml")

        cam.frame_rate  = 30.0
        cam.exposure_ms = 8.0

        frame = cam.grab()
        print(f"Frame: {frame.shape}, {frame.dtype}, mean={frame.mean():.0f}")

        frames = cam.acquire(10)
        print(f"Batch: {len(frames)} frames")


if __name__ == "__main__":
    main()
