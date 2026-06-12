pyFlir
======

Pure-Python driver for `FLIR <https://www.flir.com/>`_ thermal cameras over
GigE Vision. No vendor SDK required: pyFlir speaks the GVCP and GVSP protocols
directly over UDP, on top of `pyGigEVision
<https://github.com/ladisk/pyGigEVision>`_.

Quickstart
----------

.. code-block:: python

    from pyflir import Camera

    with Camera() as cam:
        cam.load_xml("docs/camera_<serial>.xml")   # cached after first download_xml()
        cam.frame_rate  = 30.0
        cam.exposure_ms = 8.0
        frame = cam.grab()   # numpy (H, W) uint16, metadata rows stripped

See :doc:`getting_started` for installation and first contact.

Contents
--------

.. toctree::
   :maxdepth: 2

   getting_started
   camera
   streaming
   troubleshooting
   examples
   changelog

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
