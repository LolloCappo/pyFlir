Getting started
===============

Installation
------------

.. code-block:: bash

    pip install pyFlir

Requirements
------------

* Python 3.10 or newer
* A network interface reachable from the camera — usually a dedicated Ethernet
  adapter or USB-to-GigE dongle with a link-local address (``169.254.x.x``)
* Inbound UDP allowed for the Python process (see :doc:`troubleshooting`)

First contact
-------------

Discover cameras on all host network interfaces, then connect and grab a frame:

.. code-block:: python

    from pyflir import discover, Camera

    for cam in discover():
        state = "reachable" if cam.get("reachable", True) else "not reachable"
        print(cam.get("manufacturer", "?"), cam.get("model", "?"),
              cam["ip"], state)

    with Camera() as cam:
        cam.download_xml()              # download once; saves camera_<serial>.xml
        cam.load_xml("camera_xxx.xml")  # or use a cached copy from docs/
        cam.frame_rate  = 30.0
        cam.exposure_ms = 8.0
        frame = cam.grab()
        print(frame.shape, frame.dtype)   # e.g. (512, 640) uint16

``discover()`` searches every host network interface, so cameras on a USB-to-GigE
adapter or secondary NIC are found without manual interface selection.

GenICam XML
-----------

FLIR cameras store all their feature definitions in a GenICam XML file. Download
it once and cache the result locally; subsequent reconnects can load from disk and
skip the network transfer:

.. code-block:: python

    import glob

    xml_files = glob.glob("docs/camera_*.xml") + glob.glob("camera_*.xml")
    if xml_files:
        cam.load_xml(xml_files[0])
    else:
        cam.download_xml()              # saves camera_<serial>.xml in cwd
        cam.load_xml(f"camera_{cam.serial}.xml")

After ``load_xml()`` the camera's ``model``, ``serial``, ``width``, and
``height`` attributes are populated from the device's own registers.

Network setup
-------------

**Linux — raise the UDP receive buffer (once per boot):**

.. code-block:: bash

    sudo sysctl -w net.core.rmem_max=16777216
    sudo sysctl -w net.core.rmem_default=16777216

**Windows — add an inbound UDP firewall rule (once, run as admin):**

.. code-block:: bat

    netsh advfirewall firewall add rule name="pyFlir-GVSP" dir=in ^
        action=allow protocol=UDP program="C:\path\to\python.exe"

Or use the built-in helper:

.. code-block:: bash

    pyflir setup

.. warning::

   **FLIR A-series cameras boot slowly.**
   The A6751sc takes 60–90 seconds from power-on before it responds to GigE
   Vision discovery packets. ``discover()`` and ``connect()`` will return empty
   or raise until the camera is fully up. Allow the full boot time before
   calling either.
