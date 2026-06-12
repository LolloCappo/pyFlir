Changelog
=========

Version 0.2.0
-------------

- First release published to PyPI.
- The pyGigEVision dependency now installs from PyPI
  (``pyGigEVision>=0.2.1``) instead of a local install.
- Robust GenICam XML downloader replacing pyGigEVision's default parser:

  - FLIR bare-hex FIRST_URL fields (no ``0x`` prefix) are parsed correctly.
  - All READMEM requests are rounded to a multiple of 4 bytes to satisfy the
    A6751sc firmware requirement.
  - ZIP-wrapped XML (FLIR default) is unpacked automatically.

- ``Camera.connect()`` retries for up to 90 seconds to survive stale CCP
  heartbeat locks left by crashed sessions.
- ``Camera.connect()`` auto-loads a cached GenICam XML from
  ``docs/camera_*.xml`` or ``camera_*.xml`` in the working directory, so
  ``cam.model`` and ``cam.serial`` are populated immediately after connect.
- ``cam.model`` now correctly reports the product name (e.g. ``"A6751sc"``)
  by reading the FLIR-specific ``CameraModel`` register instead of the
  firmware platform name (``"Xsc Series"``).
- Metadata-row stripping: the A6751sc appends one telemetry row to each
  frame. pyFlir detects this during ``load_xml()`` and strips it from every
  returned frame. ``cam.height``, ``cam.get_roi()``, and ``frame.shape`` all
  reflect the correct 512 usable rows. The raw metadata row is accessible via
  ``cam.last_metadata_rows``.
- ``get_roi()`` returns the true image height (excluding metadata rows).
- ``set_roi()`` adds metadata rows back when writing to the Height register so
  the camera always sees its required total row count.
- ``discover()`` uses ``_local_ip()`` (connected-socket approach) to determine
  the correct outgoing interface, avoiding wrong NIC selection on hosts with
  multiple link-local adapters.

Version 0.1.0
-------------

- Initial FLIR camera driver: discovery, GVCP control, GVSP streaming, GenICam
  XML download and parse, frame rate, exposure, ROI, calibration blocks,
  radiometric object parameters, NUC, flag, temperature sensors, file I/O
  (ATS, SFMOV).
