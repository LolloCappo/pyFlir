Examples
========

Runnable scripts demonstrating pyFlir. Each needs a connected FLIR GigE Vision camera.

* ``01_connect_and_grab.py`` — discover, connect, download XML, grab one frame.
* ``02_continuous_live.py`` — continuous streaming with ``latest=True`` for a
  lag-free live display.
* ``03_calibration_block.py`` — list calibration blocks, select a temperature
  range, trigger NUC.
* ``04_object_params.py`` — set radiometric object parameters (emissivity,
  distance, atmospheric temperature, humidity).
* ``05_roi_subwindow.py`` — reduce the ROI for higher frame rates, then restore.
* ``06_force_ip.py`` — re-home a camera on the wrong subnet by assigning a new IP
  by MAC (FORCEIP).
* ``07_temperatures_and_nuc.py`` — read all on-board temperature sensors, move the
  internal flag, and trigger a flat-field NUC.
