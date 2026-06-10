"""Test configuration and fixtures."""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--hardware",
        action="store_true",
        default=False,
        help="Run tests that require a connected FLIR camera",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "hardware: test requires a connected FLIR camera")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--hardware"):
        skip = pytest.mark.skip(reason="Need --hardware flag to run")
        for item in items:
            if "hardware" in item.keywords:
                item.add_marker(skip)
