Streaming and acquisition
=========================

pyFlir supports two acquisition styles: manual stream control (start, read in a
loop, stop) and the convenience wrappers (``grab`` for a single frame,
``acquire`` for a fixed batch).

Convenience acquisition
-----------------------

For one-shot or batch acquisition the simplest path is:

.. code-block:: python

    frame  = cam.grab()           # single frame → numpy (H, W) uint16
    frames = cam.acquire(20)      # list of 20 (H, W) arrays

Both wrap the start/read/stop cycle automatically. ``grab()`` starts the stream,
reads one frame, and stops. ``acquire(n)`` does the same for ``n`` frames.

Live streaming
--------------

For continuous display or processing loops, control the stream manually:

.. code-block:: python

    cam.start_stream()
    try:
        while True:
            frame = cam.read(timeout=2.0, latest=True)
            process_and_display(frame)
    finally:
        cam.stop_stream()

Pass ``latest=True`` to :meth:`~pyflir.Camera.read` in display loops. Without
it, if your processing loop falls behind the camera frame rate, frames
accumulate in the internal queue and the displayed image drifts further behind
real time each second. ``latest=True`` drains the queue and always returns the
most recent frame, keeping end-to-end latency bounded to one frame period.

ROI and frame rate
------------------

Reducing the sensor window (ROI) increases the maximum achievable frame rate.
Query the camera's constraints first:

.. code-block:: python

    limits = cam.get_roi_limits()
    # {'width_min': 16, 'width_inc': 16, 'height_min': 5, 'height_inc': 4,
    #  'sensor_width': 640, 'sensor_height': 512}

    print(cam.get_roi())
    # {'width': 640, 'height': 512, 'offset_x': 0, 'offset_y': 0}

    # Switch to a smaller window (stop the stream first)
    cam.set_roi(320, 256)
    print(f"Max FPS at new ROI: {cam.frame_rate_max:.1f} Hz")

Width must be a multiple of ``width_inc``; height must satisfy
``(height - height_min) % height_inc == 0``. Heights are reported in usable
image rows — the driver adds any camera-appended metadata rows internally and
they do not appear in the returned arrays.

Metadata rows
-------------

Some FLIR cameras (including the A6751sc) append one or more telemetry rows to
each frame. pyFlir detects these automatically during ``load_xml()`` by reading
the ``SensorHeight`` register and comparing it to the frame height. The extra
rows are stripped from every returned frame; the stripped data is accessible on
``cam.last_metadata_rows`` if needed.

NUC and flag
------------

Non-Uniformity Correction improves image uniformity. ``perform_nuc()`` computes
a fresh correction and drives the internal flag automatically -- no manual flag
handling needed:

.. code-block:: python

    cam.perform_nuc()         # compute a fresh correction, blocks until done

The camera must be streaming or in a suitable state for the NUC command to be
accepted; refer to the camera's hardware manual for timing requirements.

Calibration blocks
------------------

FLIR A-series cameras store multiple factory calibrations, each covering a
different temperature range **and fit at one specific integration time**.
Loading a calibration applies it to the live stream and sets that integration
time together -- you do not set integration time independently. List and load:

.. code-block:: python

    for b in cam.get_calibration_blocks():
        active = " ← active" if b["index"] == cam.get_calibration_block() else ""
        print(f"[{b['index']}] {b.get('name', '')}  "
              f"{b['tmin']:.0f}–{b['tmax']:.0f} °C{active}")

    cam.load_calibration(index=0)   # load the first (coldest) range;
                                    # also sets its integration time

Writing :attr:`Camera.exposure_ms` yourself desyncs the raw counts from the
loaded calibration (temperatures read wrong) and emits a warning. After loading,
if the image shows fixed-pattern noise, run :meth:`Camera.perform_nuc` to compute
a fresh correction at the new integration time.

Radiometric object parameters
------------------------------

These values affect the accuracy of temperature measurements derived from raw
digital counts. Set them to match your measurement scenario:

.. code-block:: python

    cam.set_object_params(
        emissivity  = 0.95,     # surface emissivity (0–1)
        distance_m  = 1.0,      # object-to-camera distance in metres
        atm_temp_K  = 293.15,   # atmospheric temperature in Kelvin
        refl_temp_K = 293.15,   # reflected apparent temperature in Kelvin
        humidity    = 0.50,     # relative humidity (0–1)
    )
    print(cam.get_object_params())

Temperature sensors
-------------------

Read all on-board thermistors:

.. code-block:: python

    for name, celsius in cam.get_temperatures().items():
        print(f"{name}: {celsius:.1f} °C")

The FPA (focal-plane array) temperature is also available as a property:

.. code-block:: python

    print(f"FPA: {cam.detector_temperature:.1f} °C")

Raw GenICam feature access
--------------------------

Features not exposed as properties can be read and written by GenICam name:

.. code-block:: python

    cam.read_int("Width")
    cam.write_int("Height", 256)
    cam.read_float("AcquisitionFrameRate")
    cam.read_enum("PixelFormat")
    cam.execute_command("AcquisitionStart")

Use :meth:`~pyflir.Camera.list_features` to see every feature node loaded from
the XML.
