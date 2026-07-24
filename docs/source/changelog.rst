Changelog
=========

Unreleased
----------

Version 0.2.3 (2026-07-24)
--------------------------

The release that makes radiometry trustworthy. Two defects were corrupting every
temperature this driver produced -- byte-swapped pixel data, and a calibration
load that left the detector offset stale -- and both are fixed here. Read the
warning below before upgrading: despite the patch-level version, this release
changes the values frames carry and removes part of the NUC API.

.. warning::

   **Behaviour change:** frames returned by :meth:`~pyflir.Camera.grab` /
   :meth:`~pyflir.Camera.read` now have their bytes in the correct order, so the
   *values* differ from 0.2.x. Any hard-coded count thresholds, saved ``.npy``
   recordings, or downstream conversions calibrated against 0.2.x output must be
   re-derived. Recordings made with 0.2.x can be corrected after the fact with
   ``frame.byteswap()``.

   :meth:`~pyflir.Camera.load_calibration` now runs a NUC by default (the flag
   is in the field of view for a few seconds); pass ``nuc=False`` for the old
   behaviour. ``perform_nuc()`` and the ``correction_*`` state machine have been
   removed -- see below for why.

- **Root cause of the wrong temperatures: the pixel data was byte-swapped.**
  GigE Vision transmits multi-byte pixel values most-significant-byte first.
  Decoded with the host's native little-endian order, every pixel arrives
  byte-swapped -- not obviously broken in aggregate, but physically meaningless.

  Found by reading the camera's *own* radiometry as ground truth. In
  ``IRFormat = TemperatureLinear100mK``, pointed at a room-temperature scene,
  the raw 1/50/99 percentiles were ``39435 / 40971 / 41995`` = ``0x9A0B /
  0xA00B / 0xA40B``. The **low** byte is pinned at ``0x0B`` while the high byte
  varies -- the signature of a swap, since that constant is really the *high*
  byte of a narrow-range quantity. Byte-swapped they read ``0x0B9A / 0x0BA0 /
  0x0BA4`` = 2970 / 2976 / 2980, and ``x0.1 - 273.15`` gives **23.9 / 24.5 /
  24.9 °C** -- the correct room temperature. The same signature appears in
  Radiometric mode, with the low byte pinned at ``0x15``.

  This one defect explains the whole cluster of symptoms chased through this
  release: raw values exceeding the 14-bit sensor range, counts that did not
  scale with integration time, and blocks 0 and 1 disagreeing by ~44 °C on an
  identical scene. It also reframes the "MSB-aligned, divide by 4" behaviour
  below -- that divisor was compensating for swapped bytes, not recovering a
  genuine left-shift.

  ``Camera.byte_order`` (``"auto"`` / ``"native"`` / ``"swapped"``) controls
  this; ``"auto"`` decides once from the first frame and caches the result, so
  a later uniform scene cannot flip it mid-stream (``reset_byte_order()``
  clears it). Detection compares byte planes: a correctly ordered image spreads
  fine detail across all 256 low-byte values, while a swapped one collapses the
  low byte to a handful. The thresholds sit far from genuinely MSB-aligned data,
  which still gives the low byte 64 distinct values. pyGigEVision is unmodified
  -- the correction lives entirely in pyflir's frame path.

  **Not yet verified end-to-end**: that byte-swapping the *Radiometric* counts
  makes blocks 0 and 1 agree and match the ~24 °C ground truth. The
  TemperatureLinear evidence is conclusive on its own, but the full
  counts→temperature chain still needs a live confirmation run.

- **Fixed metadata-row stripping taking the wrong end of the frame.** The row
  count was stripped from the bottom unconditionally, but on the A6751sc the
  metadata row is the **first** row: after stripping, row 0 still held 605 zeros
  and ~35 sparse non-zero fields (telemetry, not a dead detector row). So every
  frame kept a row of telemetry *and* discarded a row of real pixels. The end is
  now chosen per frame, by which candidate is the bigger outlier against the
  frame's interior.

- **Hardened raw-count identification and the calibration/integration-time
  contract**, the two things that silently corrupt temperatures:

  - ``Camera.to_adc_counts(frame)`` makes the conversion domain explicit. The
    wire frame is a 16-bit transport word holding a left-justified 14-bit ADC
    value (so raw numbers reach ~65532 on a 14-bit sensor); the calibration's
    ``counts_min``/``counts_max`` are in **ADC counts**, and that is what gets
    converted. The division is done in floating point, so the low bits -- which
    carry sub-count precision from the camera's on-board processing, *not*
    padding -- are preserved rather than truncated.
  - ``_count_divisor()`` now lets the **data override the register**: the camera
    accepts ``PixelFormat = Mono14`` while still streaming Mono16, and trusting
    the register then divided by 1 instead of 4, inflating counts 4x and pinning
    every pixel at ``tmax``. A 14-bit stream cannot exceed 16383, so anything
    above that is treated as Mono16 regardless of what the register claims.
  - ``load_calibration()`` records the integration time it set, and
    ``counts_to_temperature()`` **warns if the two have since diverged** --
    counts scale directly with integration time, so a mismatch biases every
    reading (the warning states the approximate factor).
  - ``Camera.check_scene_fit(frame)`` reports what fraction of the scene falls
    outside the loaded block's count range and suggests a better block. A
    calibration is only valid between its own ``counts_min``/``counts_max``;
    outside it the values are not measurements.
  - ``apply_calibration``/``counts_to_temperature`` take ``clip`` (default
    ``True``, unchanged). With ``clip=False`` out-of-range pixels become ``NaN``
    instead of being pinned to ``tmin``/``tmax``, so an unsuitable calibration
    block is visible rather than silently rendered as a solid block of endpoint
    temperatures.

- **Added FLIR's official R/B/F radiance→temperature formula, now the default.**
  The camera calibration is an "RBF" formula (FLIR KB a_id/3321):
  ``T_kelvin = B / ln(R / radiance + F)``, using the ``CalibrationQueryR/B/F``
  coefficients the camera exposes. pyFlir previously used only the temperature
  *polynomial* (``CalibrationQueryTempCoeff``), which is a fit -- self-consistent
  at the block endpoints by construction, but potentially wrong in between.
  ``apply_calibration``/``counts_to_temperature`` now take ``method="rbf"``
  (default) or ``method="polynomial"``, and ``get_calibration()`` returns
  ``r``/``b``/``f`` (plus ``radiance_min``/``radiance_max``). RBF falls back to
  the polynomial if a (e.g. cached) cal dict lacks the coefficients. Note: this
  camera exposes no separate J0/J1, so the counts→radiance step remains the
  polynomial (the J0/J1 linear form is its special case).

- Added ``Camera.pixel_format`` (``"Mono16"`` / ``"Mono14"``); the count divisor
  adapts automatically (Mono16 left-justifies the 14-bit value, ``÷4``; Mono14 is
  native 14-bit, ``÷1``). Fixed a GenICam-parser bug that dropped enum name↔value
  maps for split ``Enumeration``/``Reg`` nodes, which had silently broken
  ``read_enum``/``write_enum`` (returning ``<unknown:N>`` and rejecting valid
  values) -- this affected ``pixel_format`` and any enum feature.

- **Rebuilt NUC handling on the camera's offset-update path, and made
  ``load_calibration()`` run one.** This closes the last known source of wrong
  absolute temperatures.

  The camera exposes two distinct correction paths, and pyFlir previously drove
  the wrong one. ``CorrectionPerform`` (``CorrectionType`` / ``CorrectionStart``
  / ``CorrectionAccept``) collects and averages "frames from each uniform
  temperature source" -- it *recomputes and overwrites the stored gain
  coefficients*. Running its one-point mode against the internal flag, a single
  near-ambient source, is not a substitute for a factory calibration fit against
  real blackbodies: it replaces good gain terms with garbage and visibly
  corrupts the image (swirl and contour artifacts on object edges). That damage
  is stored, so it survives power cycles. **pyFlir no longer exposes it**, and a
  regression test asserts those registers are never written.

  ``CorrectionAuto`` is the routine one -- the camera's XML describes it as
  "automatic non uniformity correction with the internal flag (offset update)".
  It re-levels the per-pixel *offset* on top of the factory calibration and
  leaves the factory gain terms alone. This is what the vendor software runs on
  range select (flag click, a few seconds, correct image), and it is now
  ``Camera.nuc()``.

  - ``Camera.nuc()`` triggers an offset update and waits for it to finish.
  - ``load_calibration()`` calls it by default (``nuc=False`` to skip).
    **This was the missing step.** Loading a calibration changes the integration
    time, which shifts the detector's dark level; until the offset is re-levelled
    the stored one no longer matches and every pixel carries a constant count
    error. That is why the same scene read ~36 °C on block 0 and ~81 °C on block
    1, with counts *rising* despite a 2.4x shorter integration -- the signature
    of a stale offset, not a bad conversion. A failed NUC is reported but does
    not discard an otherwise successful load.
  - ``Camera.configure_auto_nuc()`` / ``get_auto_nuc_config()`` let the camera
    re-level itself on a temperature-drift or elapsed-time trigger, as the
    vendor software does. Opt-in, since it injects flag frames into the stream
    at unpredictable moments.
  - Restored as read-only observability: ``FlagPresent`` / ``FlagState``. pyFlir
    never parks or moves the flag itself; the camera does that internally.

  Still not exposed (unchanged from the removal): ``load_nuc`` / ``list_nucs`` /
  ``query_nuc`` / ``load_matching_nuc``, ``get_nuc_status``, ``flag_move_*``.

- Added camera-side temperature output and a host-side offset, the two things
  that actually matter for correct temperatures:

  - ``Camera.ir_format`` selects what the camera streams -- ``"Radiometric"``
    (raw counts, converted host-side by the calibration polynomial) or the
    on-board ``"TemperatureLinear10mK"`` / ``"TemperatureLinear100mK"`` modes.
    ``Camera.frame_to_celsius(frame)`` converts either (a rescale in temperature
    mode, the polynomial in raw mode).
  - ``Camera.set_offset_reference(known_temp_c)`` calibrates a one-point offset
    against a target of known temperature, to correct a constant shift in the
    raw counts. Now demoted to a last resort: the supported fix for a counts
    offset is ``Camera.nuc()``, which corrects it on the camera where it
    belongs. This remains a host-side workaround for a *residual* bias, it
    assumes the conversion's shape is right and only the offset is wrong, and it
    is only as good as your knowledge of the target's true temperature.
    Absolute radiometric accuracy is still unverified end-to-end -- that needs a
    known-temperature reference (ice water, or a calibrated blackbody).

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
