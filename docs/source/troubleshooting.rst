Troubleshooting
===============

Symptoms, causes, and fixes for the most common issues encountered with
pyFlir and FLIR A-series cameras.

Camera not found on ``discover()``
-----------------------------------

**Symptom:** ``discover()`` returns an empty list.

**Cause:** the camera is still booting, unreachable, or on a different subnet.

**Fixes:**

- **Wait for boot.** The FLIR A6751sc takes 60–90 seconds from power-on before
  it responds to GigE Vision discovery. If ``discover()`` returns nothing right
  after power-on, wait the full boot time and retry.
- Confirm the camera has an address and your host has a reachable IP on the same
  subnet. FLIR A-series cameras use DHCP; if no DHCP server is present they fall
  back to a link-local address (``169.254.x.x``). Use ``tcpdump`` or ``arp -a``
  to see the camera's ARP broadcasts immediately after boot.
- On Linux, raise the kernel UDP receive buffer:

  .. code-block:: bash

      sudo sysctl -w net.core.rmem_max=16777216

- Check that reverse-path filtering is not dropping replies from an unexpected
  subnet (``cat /proc/sys/net/ipv4/conf/ethX/rp_filter``). Set it to ``2``
  (loose mode) if needed.

Finding the camera IP on Linux (USB-to-GigE dongle)
----------------------------------------------------

If the camera boots and gets an APIPA address you may not know which IP to use.
Capture the ARP broadcast immediately after power-on:

.. code-block:: bash

    sudo tcpdump -i enxa0cec86a36c8 -n arp

The camera broadcasts an ARP announcement within the first few seconds of boot.
The source IP in that packet is the camera's current address.

Persistent dongle IP with NetworkManager
-----------------------------------------

NetworkManager can remove manually-assigned link-local addresses from a dongle
between boots. Create a persistent connection profile so the address survives
reboots:

.. code-block:: bash

    # Create or modify a connection profile named "camera"
    nmcli connection modify camera \
        ipv4.addresses 169.254.61.52/16 \
        ipv4.method manual
    nmcli connection up camera

``ACCESS_DENIED`` on reconnect (stale CCP lock)
------------------------------------------------

**Symptom:** after a crashed kernel or ``Ctrl-C``, ``cam.connect()`` raises
``GVCPError: ACCESS_DENIED`` and retries for up to 90 seconds.

**Cause:** the previous session held GVCP control (CCP) and didn't release it.
The camera keeps the old session alive until its heartbeat timeout expires
(up to 60 seconds on some firmware versions).

**Fix:** this is normal. ``Camera.connect()`` automatically retries in a loop
until the heartbeat expires. Wait up to 90 seconds. If it still fails, restart
the Jupyter kernel (or Python process) that may be holding the connection open,
then retry.

``ValueError: invalid literal for int()`` during XML download
--------------------------------------------------------------

**Symptom:** ``cam.download_xml()`` raises a ``ValueError`` about a hex address
without a ``0x`` prefix.

**Cause:** FLIR cameras put bare hex values (e.g. ``3fff8000``) in the
FIRST_URL bootstrap register, without the ``0x`` prefix required by the
pyGigEVision default parser.

**Fix:** this is handled automatically by pyFlir's ``fetch_genicam_xml``
implementation. If you see this error you may be using a stale version of
pyFlir; update with ``pip install --upgrade pyFlir``.

``INVALID_PARAMETER`` during XML download
-----------------------------------------

**Symptom:** ``cam.download_xml()`` raises ``GVCPError: INVALID_PARAMETER``
partway through.

**Cause:** the FLIR A6751sc rejects READMEM requests whose byte count is not a
multiple of 4. The stock pyGigEVision implementation can issue odd-sized reads
on the last chunk.

**Fix:** handled automatically by pyFlir's aligned read wrapper. See above for
the update note.

Height reported as 513 instead of 512
--------------------------------------

**Symptom:** ``cam.height``, ``cam.get_roi()``, or ``frame.shape[0]`` show 513
instead of the expected 512 (on a 640×512 sensor).

**Cause:** the A6751sc appends one telemetry/metadata row to each frame. The
``Height`` register therefore reads 513 (512 image rows + 1 metadata row).

**Fix:** handled automatically after ``load_xml()``. pyFlir reads the
``SensorHeight`` register, detects the discrepancy, sets ``_metadata_rows = 1``,
adjusts ``self.height = 512``, and strips the extra row from every frame. All
user-facing values (``cam.height``, ``cam.get_roi()``, ``frame.shape``) reflect
the 512 usable image rows. The metadata row is accessible on
``cam.last_metadata_rows``.

Model shows "Xsc Series" instead of "A6751sc"
----------------------------------------------

**Symptom:** ``cam.model`` returns ``"Xsc Series"`` after discovery or connect.

**Cause:** the standard GigE Vision ``DeviceModelName`` register on FLIR cameras
returns the firmware platform name ("Xsc Series"), not the product SKU.

**Fix:** handled automatically after ``load_xml()``. pyFlir reads the
FLIR-specific ``CameraModel`` register (address ``0x4EA53C64``) which contains
``"A6751sc"``, and uses it to overwrite the discovery value.

Camera model / serial blank before ``load_xml()``
-------------------------------------------------

**Symptom:** ``cam.model`` or ``cam.serial`` are empty right after
``cam.connect()``.

**Cause:** the A6751sc does not populate the standard GigE Vision device-info
registers that pyGigEVision reads during discovery. The correct values live in
FLIR-specific registers that can only be read after the GenICam XML is loaded.

**Fix:** ``cam.connect()`` auto-loads the XML from any cached
``docs/camera_*.xml`` or ``camera_*.xml`` file in the working directory. After
that, ``cam.model == "A6751sc"`` and ``cam.serial == "00332"`` (or whichever
unit you have). If no cache exists, call ``cam.download_xml()`` first.

Live streaming lag that grows over time
---------------------------------------

**Symptom:** a live display loop starts in sync but drifts further behind real
time each second.

**Cause:** the display or processing code is slower than the camera frame rate.
Frames accumulate in pyFlir's internal GVSP queue and the loop processes them
in order, each one staler than the last.

**Fix:** use ``latest=True`` in the read loop:

.. code-block:: python

    while True:
        frame = cam.read(timeout=2.0, latest=True)
        display(frame)

This drops intermediate frames when the consumer is slow but keeps displayed
latency bounded to one frame period. Use the default ``latest=False`` when you
need every frame (logging, measurement).
