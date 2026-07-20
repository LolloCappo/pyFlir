Changelog
=========

Version 0.2.1
-------------

- **Radiometry root-cause fix (live A6751sc).** Temperatures read far too hot
  (a room scene pinned near 79 °C) because *no factory calibration was loaded*
  (all ``PS{n}CalibrationTag`` empty) and the integration time (0.1 ms) matched
  no calibration. Three coupled fixes:

  - Added ``Camera.load_calibration(tag=... | index=...)`` -- the real "select a
    temperature range" operation. It stages the calibration tag into
    ``PS{n}CalibrationLoadTag`` and executes ``PS{n}CalibrationLoad``. Since
    pyGigEVision exposes no GVCP WRITEMEM, the 256-byte tag string is written 4
    bytes at a time with the existing ``write_reg`` (WRITEREG), little-endian
    within each word (verified live). Loading a calibration **also sets the
    preset's integration time** to the value the calibration was fit at
    (verified: loading "-20C - 55C" moved integration time 0.1 ms → 2.354 ms;
    "10C - 90C" → 0.978 ms) -- the integration-time ↔ calibration coupling the
    vendor software enforces. ``set_calibration_block()`` now delegates here
    (previously it only moved a read-only browse cursor and did **not** change
    the stream).
  - ``get_calibration()``/``get_calibration_block()`` now resolve the block
    **actually loaded** on the active preset (matching ``PS{n}CalibrationTag``
    against the browse cursor's ``CalibrationQueryTag``) instead of trusting
    wherever the cursor happened to sit, so ``counts_to_temperature()`` converts
    with the polynomial that genuinely applies to captured frames.
  - Fixed a 1000× unit error in ``Camera.exposure_ms``. The backing register
    ``PS{n}IntegrationTime`` is documented *in milliseconds* (its Max reads
    687000 -- sensible as ms, 8 days as seconds), but the property multiplied by
    1000, reporting 100 ms when the real integration time was 0.1 ms, and
    ``cam.exposure_ms = 8.0`` actually wrote 0.008 ms. Now a 1:1 passthrough;
    ``get_exposure``/``set_exposure`` (seconds) adjusted to match. The setter now
    warns that changing integration time desyncs the loaded calibration.

  ``perform_nuc()`` was also validated against the live camera for the first time
  (returned "Okay", flag stowed correctly). Note: loading a calibration brings in
  a stored NUC that may be stale for the current detector state; a residual
  fixed-pattern-noise issue after loading is still open (run ``perform_nuc()``).

- Removed ``Camera.trigger_nuc()``. It only re-triggered a reload of
  whatever correction was already resident for a preset ("PS{preset}
  CorrectionLoad") -- against a live A6751sc, calling it measurably
  increased image noise (higher std) each time, with no corresponding
  benefit ever observed. ``Camera.perform_nuc()`` (below) computes a
  genuinely new correction instead and is the supported way to run NUC.
  ``REG_NUC_LOAD`` is kept in ``registers.py`` for reference but nothing
  writes to it anymore.

- Fixed a live regression from the ``TEMP_SENSORS`` expansion (4 -> 9
  sensors, previous entry below): one of the new selector indices raised
  ``GVCPError: GENERIC_ERROR`` on the real A6751sc -- not every entry in
  the camera's generic ``DeviceTemperatureSelector`` enum is necessarily
  populated on a given unit. ``get_temperatures()`` now skips any sensor
  that fails to read instead of aborting the whole call.
  ``detector_temperature`` no longer goes through the full sweep at all --
  it reads only FPA via a new ``_read_temp_sensor()`` helper, so it can't
  be broken by an unrelated sensor being absent. 4 new regression tests.

- Added ``Camera.perform_nuc()``: actually computes and applies a fresh NUC
  (Non-Uniformity Correction), as opposed to ``trigger_nuc()`` which only
  re-loads an already-stored one. Drives the camera's "CorrectionPerform"
  GenICam state machine (``CorrectionType``/``Source``/``Start`` ->
  poll ``CorrectionStatus`` -> ``CorrectionResult`` -> ``Accept``/
  ``Discard``/``Abort``) using the internal NUC flag as the uniform
  reference, fully automatic, blocking until done. Lower-level building
  blocks (``correction_start()``, ``correction_continue()``,
  ``correction_accept()``, ``correction_discard()``, ``correction_abort()``,
  ``get_correction_status()``, ``get_correction_result()``) are exposed too,
  for a "TwoPoint" or external-blackbody workflow that needs a person to
  present a target partway through -- ``perform_nuc()`` doesn't support
  those. Found by reading the camera's own GenICam XML: its
  ``CorrectionSource`` enum has ``Name``/``ToolTip`` swapped (``Name=
  "External"`` carries the tooltip "Internal NUC flag" and vice versa) --
  cross-checked against ``CorrectionStatus``'s own self-consistent naming
  to resolve which is which; see the comment on
  ``pyflir.registers.REG_CORRECTION_SOURCE``. 17 new tests covering the
  state machine, including timeout, an unexpected-external-source abort,
  and a non-"Okay" result. **Not yet verified against a live camera** --
  implemented entirely from the XML, no hardware to test the actual
  multi-step process against yet.

- Fixed ``Camera.connect()`` picking the wrong local network interface when
  more than one is up. It used to trust the ``interface_ip`` field
  ``pyGigEVision.GVCPClient.discover()`` reports for a camera, but that
  library dedupes multiple replies for the same camera IP down to whichever
  local socket answered first -- not necessarily the interface actually on
  the camera's subnet, since GVCP control replies reach the request's
  source address regardless of subnet match. Only GVSP streaming, which the
  camera itself originates, needs a real route, so it would silently time
  out even though discovery and register access worked fine. Fixed entirely
  within pyflir (no pyGigEVision change needed): a new private
  ``_find_matching_interface()`` re-derives the correct interface by
  querying each local interface individually via
  ``discover(interface_ip=...)`` -- a single socket bound to one address
  has no cross-interface race to get wrong -- preferring same-subnet
  candidates first.

- NUC (Non-Uniformity Correction) and flag handling cross-checked against
  FLIR's own Science File SDK headers (read-only extraction, see the
  ``apply_calibration()`` provenance note) and the camera's GenICam XML:

  - Added ``get_nuc_status(preset)`` to read which correction is currently
    loaded for a preset ("PS{preset}CorrectionName").
  - Added ``has_flag()`` and ``get_flag_state()`` ("Stowed"/"InFOV") to
    query the physical NUC flag, which previously only had write-only
    move commands with no way to confirm the (mechanical, non-instant)
    move actually completed.
  - Confirmed correct and unchanged: the NUC-load and flag-move register
    addresses in ``registers.py`` do match their claimed GenICam features
    (unlike the earlier ``FPAColdReg`` mixup) -- this was a real concern
    given that precedent, checked explicitly rather than assumed.
  - Noted but not implemented: ``trigger_nuc()``'s GenICam feature,
    "PS{preset}CorrectionLoad", loads whichever correction is named in a
    *separate* "PS{preset}CorrectionLoadName" register, which nothing
    writes to -- so it can only re-trigger whatever name is already
    resident, never select a specific one. Selecting by name would need a
    GVCP WRITEMEM, which ``pyGigEVision.GVCPClient`` does not expose (only
    ``read_mem()``, no ``write_mem()``); not worked around here with
    duplicate protocol code, left as a documented limitation instead.

  Still open, not yet resolved: the camera's XML also exposes a *separate*
  "PS{preset}CalibrationLoad"/"CalibrationLoadTag" mechanism (loads a
  named factory radiometric calibration into a preset), distinct from both
  the NUC correction load above and the ``CalibrationQueryIndex``-based
  read-only browse mechanism ``get_calibration()``/``set_calibration_block()``
  already use. Whether ``CalibrationQueryIndex`` actually reflects the
  calibration applied to the live stream, or is purely an introspection
  cursor separate from ``PS0CalibrationLoad``, is unverified -- if the
  latter, ``set_calibration_block()`` may not do what its docstring/README
  claim. Needs live verification (see ``apply_calibration()``'s docstring
  for the general approach).

- Added 5 more on-board temperature sensors to ``get_temperatures()``:
  ``TEMP_SENSORS`` only listed 4 (``FPA``, ``Digitizer``, ``PowerBoard``,
  ``FrontPanel``) but the camera's ``DeviceTemperatureSelector`` enum lists
  9 (0-8); added ``AirGap``, ``Internal``, ``Chassis``, ``Lens``, and
  ``Flag`` (the NUC flag's own temperature). Address and °C unit confirmed
  via a structured GenICam description, no ambiguity here.

- Fixed three register-typing bugs found against a live A6751sc, all the
  same shape: code called ``read_float()`` on a register the camera's own
  GenICam XML declares as an integer (``IntReg``), silently reinterpreting
  the raw integer's bits as IEEE-754 and returning nonsense.

  - ``detector_temperature`` (and ``get_temperature()``, ``info()``,
    ``Camera.__repr__``) used the FLIR-specific fallback ``FPAColdReg``,
    which despite its name backs a *Boolean* status flag ("FPACold"), not a
    temperature. Now reads via the selector-based on-board sensor mechanism
    (:meth:`Camera.get_temperatures`) instead, which already worked
    correctly.
  - ``get_calibration()``'s ``counts_min``/``counts_max`` are native 14-bit
    ADC counts (``IntReg``); now read with ``read_int()`` instead of
    ``read_float()``.
  - ``frame_rate_max`` silently returned ``None`` on the A6751sc:
    ``_SFNC_CANDIDATES["AcquisitionFrameRateMax"]`` listed
    ``"PS0FrameRateMax"``, but the GenICam XML parser only captures nodes
    with a direct ``<Address>`` tag, so only the ``...Reg``-suffixed sibling
    (``PS0FrameRateMaxReg``) is actually in the register map. Fixed to match
    the pattern already used for ``AcquisitionFrameRate``.

- Fixed a wrong-case keyword argument bug in ``Showcase.ipynb``
  (``atm_temp_K``/``refl_temp_K`` instead of ``atm_temp_k``/``refl_temp_k``,
  the actual :meth:`Camera.set_object_params` parameter names).

- ``apply_calibration()``/``counts_to_temperature()`` radiometric conversion
  now matches FLIR's own Science File SDK reference design more closely,
  cross-checked by extracting (read-only, not executed -- it targets Linux
  ARM64) the SDK installer's headers and Python bindings:

  - Raw counts are now clipped to ``[counts_min, counts_max]`` instead of
    being silently extrapolated past the calibrated domain. A new
    ``return_status=True`` parameter returns a per-pixel status array
    (``STATUS_OK``/``STATUS_UNDERFLOW``/``STATUS_OVERFLOW``, also exported
    from the top-level ``pyflir`` package), mirroring
    ``fnv.file.ImagerFile.status`` ("overflow, underflow, warning") in
    FLIR's own SDK. Default behavior for existing callers is unchanged
    (returns a plain array) unless the new parameter is passed.
  - ``counts_background`` is now subtracted in the counts -> radiance step,
    matching the SDK's ``CNicevilleFactoryCalReduceObject`` struct, whose
    ``bgValue`` field groups with the counts polynomial coefficients under
    a "count->rad" comment, and matching what this function's own docstring
    had claimed since it was first written (the implementation just never
    did it).
  - ``emissivity``/``refl_temp_c``/``atm_temp_c``/``tau`` -- accepted as
    parameters since this function existed but previously documented as
    "unused (reserved)" and never applied -- now apply the standard
    object-signal radiometric equation (published thermography practice,
    matching the exact parameter set in the SDK's
    ``CObjectParametersReduceObject``). With the defaults (``emissivity=1.0``,
    ``tau=1.0``) this is mathematically a no-op, so existing callers relying
    on defaults see unchanged output.

  None of the above has been verified against a live camera yet (none was
  connected while this was implemented) -- see the ``apply_calibration()``
  docstring for the direct verification method once one is available.

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
