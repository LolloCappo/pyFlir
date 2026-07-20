![pyFlir](pyflir_logo.png)

# pyFlir

[![Tests](https://github.com/LolloCappo/pyFlir/actions/workflows/testing.yml/badge.svg)](https://github.com/LolloCappo/pyFlir/actions/workflows/testing.yml)
[![PyPI](https://img.shields.io/pypi/v/pyFlir.svg)](https://pypi.org/project/pyFlir/)
[![Python](https://img.shields.io/pypi/pyversions/pyFlir.svg)](https://pypi.org/project/pyFlir/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docs](https://readthedocs.org/projects/pyflir/badge/?version=latest)](https://pyflir.readthedocs.io/en/latest/)

Pure-Python driver for FLIR® thermal cameras over GigE Vision. No vendor SDK
required; communicates directly via GVCP/GVSP protocols over UDP using
[pyGigEVision](https://github.com/ladisk/pyGigEVision) as the transport layer.

Supported cameras:

- FLIR A-series GigE Vision cameras (tested on A6751sc)
- Other FLIR GigE Vision cameras (GenICam-compliant, untested)

## Features

- **Auto-discovery**: finds cameras on the network regardless of IP
- **GenICam XML-driven**: downloads and parses the camera's feature descriptor;
  no hardcoded register maps for standard features
- **Live streaming**: real-time frame acquisition, `read(latest=True)` for lag-free display
- **ROI / subwindow**: configurable resolution for higher frame rates
- **Calibration blocks**: list and load temperature-range calibrations (sets the
  matching integration time, like the vendor software)
- **Radiometry**: emissivity, distance, atmospheric temperature, humidity
- **Temperature sensors**: read all on-board thermistors
- **NUC**: perform a fresh non-uniformity correction, flag-in-FOV / stow
- **File I/O**: read FLIR ATS and SFMOV recorded files

## Installation

```bash
pip install pyFlir
```

Requires Python 3.10 or later. The [pyGigEVision](https://github.com/ladisk/pyGigEVision)
transport layer is installed automatically.

## Quick start

```python
from pyflir import Camera

with Camera() as cam:
    cam.download_xml()                             # once; saves camera_<serial>.xml
    cam.load_xml("camera_xxx.xml")
    cam.load_calibration(index=0)                  # select range; sets integration time
    cam.frame_rate  = 30.0                         # Hz

    frame = cam.grab()                             # single frame -> numpy (H, W), uint16
    frames = cam.acquire(10)                       # 10 frames -> list of (H, W)
```

Frames are returned as numpy arrays (uint16, H × W).

## Discovery

```python
from pyflir import discover

for cam in discover():
    print(cam["model"], cam["ip"], cam.get("serial", "?"))
```

## Connect

```python
from pyflir import Camera

with Camera() as cam:              # or Camera(ip="169.254.1.10")
    info = cam.info()
    print(info)
```

## Configure

```python
cam.frame_rate   = 30.0            # Hz
cam.exposure_ms                    # integration time in ms (read-only in practice;
                                   # set by load_calibration(), not independently)
cam.frame_rate_max                 # max Hz for current ROI (read-only)
cam.detector_temperature           # FPA temperature in °C (read-only)
```

> **Integration time is coupled to the calibration.** Each factory calibration
> is fit at a specific integration time; `load_calibration()` sets it for you.
> Setting `cam.exposure_ms` yourself desyncs the raw counts from the calibration
> polynomial (temperatures read wrong) and emits a warning. See below.

## GenICam feature access

Features not exposed as properties are accessible by name:

```python
cam.read_int("Width")
cam.write_int("Height", 256)
cam.read_float("AcquisitionFrameRate")
cam.read_enum("PixelFormat")
cam.execute_command("AcquisitionStart")
```

## Calibration blocks (temperature range selection)

Each factory calibration covers a temperature range **and is fit at one
specific integration time**. Loading a calibration sets both together — you do
not (and must not) set integration time independently of the calibration.

```python
for b in cam.get_calibration_blocks():
    print(b["index"], b.get("name"), b["tmin"], "–", b["tmax"], "°C", b["lens"])

# Load a range into the active preset. This changes the live stream AND sets
# the integration time the calibration was fit at (e.g. 2.35 ms for -20–55 °C).
cam.load_calibration(index=0)                       # by browse index, or:
cam.load_calibration(tag="25mm, Empty, -20C - 55C") # by exact tag

print(cam.get_calibration_block())   # index actually applied to the stream
```

> **Note** After loading a calibration, the image may show fixed-pattern noise
> if the stored NUC is stale for the new integration time. Run `cam.perform_nuc()`
> to compute a fresh correction (see below).

## Radiometry

```python
cam.set_object_params(
    emissivity  = 0.95,
    distance_m  = 3.0,
    atm_temp_k  = 293.15,
    refl_temp_k = 293.15,
    humidity    = 0.50,
)
print(cam.get_object_params())

frame = cam.grab()
temp  = cam.counts_to_temperature(frame)   # °C, uses the object params above

# Per-pixel validity (out-of-range counts get clipped, not silently extrapolated)
temp, status = cam.counts_to_temperature(frame, return_status=True)
```

## ROI / subwindow

```python
cam.set_roi(128, 64)               # width × height; stop stream first
print(cam.get_roi())
print(cam.frame_rate_max)          # higher fps at smaller ROI
```

## Temperature sensors

```python
for name, celsius in cam.get_temperatures().items():
    print(f"{name}: {celsius:.1f} °C")
```

## NUC and flag

```python
cam.perform_nuc()                  # compute a fresh correction now
                                    # (blocks until done; uses the internal
                                    # flag automatically, no user action needed)

print(cam.get_nuc_status())        # {"name": "..."} -- what's currently loaded
print(cam.has_flag())              # does this camera have a physical flag?
print(cam.get_flag_state())        # "Stowed" or "InFOV"
```

For a two-point correction or an external blackbody target (needs a person
to present it and confirm), drive the state machine directly instead of
`perform_nuc()`:

```python
cam.correction_start(correction_type="TwoPoint", source="External")
# ... present the first uniform target, then:
cam.correction_continue()
# ... present the second uniform target, then:
cam.correction_continue()
print(cam.get_correction_status())
print(cam.get_correction_result())
cam.correction_accept()            # or correction_discard() / correction_abort()
```

## Live streaming

```python
cam.start_stream()
try:
    while True:
        frame = cam.read(timeout=2.0, latest=True)   # latest=True prevents lag
        print(frame.mean())
finally:
    cam.stop_stream()
```

## File I/O

```python
from pyflir import read_ats, read_sfmov

data, meta = read_ats("recording.ats")
frames, meta = read_sfmov("recording.sfmov")
```

## CLI

```bash
pyflir discover               # find cameras on the network
pyflir info                   # show camera configuration
pyflir grab -o frame.npy      # grab a single frame
pyflir setup                  # configure OS (firewall, MTU)
```

## Network setup

GigE Vision requires a firewall rule to allow inbound UDP from the camera.

**Linux:**

```bash
sudo sysctl -w net.core.rmem_max=16777216
sudo sysctl -w net.core.rmem_default=16777216
```

**Windows** (run once as admin):

```bash
netsh advfirewall firewall add rule name="pyFlir-GVSP" dir=in action=allow protocol=UDP program="C:\path\to\python.exe"
```

Or use the built-in helper:

```bash
pyflir setup
```

## Disclaimer

pyFlir is an independent, community-developed project. It is **not** affiliated
with, endorsed by, sponsored by, or supported by Teledyne FLIR, LLC in any way.

The name "pyFlir" is used solely to describe compatibility with cameras that
implement the GigE Vision standard and happen to be manufactured by Teledyne FLIR.
Register addresses and feature names were obtained from the GenICam XML descriptor
that the camera itself serves over the network, and from publicly available
documentation shipped with the hardware. No proprietary source code, SDK, or
trade secrets from Teledyne FLIR were used or reverse-engineered.

FLIR® is a registered trademark of Teledyne FLIR, LLC. All product and company
names are trademarks or registered trademarks of their respective owners and are
used here for identification purposes only.

## License

MIT
