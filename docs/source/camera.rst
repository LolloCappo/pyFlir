Camera API reference
====================

.. currentmodule:: pyflir

.. autoclass:: Camera
   :members:
   :undoc-members:
   :show-inheritance:

Module-level helpers
--------------------

.. autofunction:: discover

.. autofunction:: force_ip

Connection diagnostics
----------------------

.. autofunction:: tune_connection

.. autoclass:: ConnectionReport
   :members:

.. autoclass:: ConnectionStats
   :members:

Errors
------

.. autoexception:: CameraError
   :members:

GenICam XML
-----------

.. autofunction:: parse_genicam_xml

.. autoclass:: RegNode
   :members:

File I/O
--------

.. autofunction:: read_ats

.. autofunction:: read_sfmov

.. autofunction:: read_sfmov_meta

.. autoclass:: FLIRATSReader
   :members:

.. autoclass:: ATSMetadata
   :members:
