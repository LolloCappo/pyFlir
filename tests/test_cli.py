"""Tests for CLI commands (no camera needed for most)."""

from unittest.mock import patch

import pytest

from pyflir.cli import main


class TestCLIParser:
    """Test argument parsing and help."""

    def test_no_args_shows_help(self, capsys):
        result = main([])
        assert result == 0

    def test_version(self, capsys):
        with pytest.raises(SystemExit, match="0"):
            main(["--version"])

    def test_discover_no_cameras(self):
        with patch("pyflir.camera.GVCPClient") as mock_cls:
            mock_cls.discover.return_value = []
            with patch("pyflir.cli.cmd_discover") as mock_cmd:
                mock_cmd.return_value = 1
                result = main(["discover"])
                assert result == 1

    def test_discover_finds_camera(self, capsys):
        mock_cam = {
            "ip": "169.254.1.1",
            "manufacturer": "FLIR Systems",
            "model": "A50",
            "serial": "12345678",
            "device_version": "1.0",
        }
        with patch("pyflir.camera.discover", return_value=[mock_cam]):
            result = main(["discover"])
            assert result == 0
            output = capsys.readouterr().out
            assert "FLIR" in output or "169.254.1.1" in output

    def test_setup_windows(self, capsys):
        with patch("platform.system", return_value="Windows"):
            result = main(["setup"])
            assert result == 0
            output = capsys.readouterr().out
            assert "firewall" in output.lower() or "Firewall" in output

    def test_setup_linux(self, capsys):
        with patch("platform.system", return_value="Linux"):
            result = main(["setup"])
            assert result == 0
            output = capsys.readouterr().out
            assert "rmem" in output or "UDP" in output

    def test_setup_unknown_os(self, capsys):
        with patch("platform.system", return_value="FreeBSD"):
            result = main(["setup"])
            assert result == 1
