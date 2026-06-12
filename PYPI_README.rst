pyFlir
======

Pure-Python driver for FLIR® thermal cameras over GigE Vision. No vendor SDK
required: pyFlir speaks the GVCP and GVSP protocols directly over UDP.

It builds on `pyGigEVision <https://github.com/ladisk/pyGigEVision>`_ for the
GigE Vision protocol layer and adds the FLIR-specific calibration, registers,
and GenICam XML support.

Tested on the FLIR A6751sc (MWIR). Other FLIR GigE Vision cameras that expose
a standard GenICam XML should work.

* GitHub: https://github.com/LolloCappo/pyFlir
* Documentation: https://pyflir.readthedocs.io

Installation
------------

.. code-block:: bash

    pip install pyFlir

Quick start
-----------

.. code-block:: python

    from pyflir import Camera

    with Camera() as cam:
        cam.download_xml()              # once; saves camera_<serial>.xml
        cam.load_xml("camera_xxx.xml")
        cam.frame_rate  = 30.0          # Hz
        cam.exposure_ms = 8.0           # ms
        frame = cam.grab()              # numpy array (H, W), uint16

License: MIT.

pyFlir is an independent project, not affiliated with or endorsed by
Teledyne FLIR, LLC. FLIR® is a registered trademark of Teledyne FLIR, LLC.
