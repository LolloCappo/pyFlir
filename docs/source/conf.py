import os
import sys

sys.path.insert(0, os.path.abspath("../.."))

project = "pyFlir"
author = "Lorenzo Capponi"
copyright = "2026, Lorenzo Capponi"
release = "0.2.0"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_llms_txt",
]

html_baseurl = "https://pyflir.readthedocs.io/en/latest/"
llms_txt_title = "pyFlir Documentation"
llms_txt_summary = (
    "pyFlir is a pure-Python driver for FLIR thermal cameras over GigE Vision, "
    "with no vendor SDK required. It speaks GVCP and GVSP directly over UDP (on "
    "top of pyGigEVision) to discover cameras, download and parse GenICam XML, "
    "configure frame rate, exposure, ROI, calibration blocks, and radiometric "
    "parameters, stream live frames, and trigger NUC corrections."
)

templates_path = ["_templates"]
exclude_patterns = []

autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_mock_imports = ["matplotlib", "PIL", "psutil"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_use_param = True
napoleon_use_rtype = True

html_theme = "sphinx_book_theme"
html_static_path = ["_static"]
html_title = "pyFlir"
html_theme_options = {
    "repository_url": "https://github.com/LolloCappo/pyFlir",
    "use_repository_button": True,
    "use_issues_button": True,
    "path_to_docs": "docs/source",
}
