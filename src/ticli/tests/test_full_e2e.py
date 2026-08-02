"""End-to-end tests for Ticli CLI.

Tests the CLI entry point and installed command via subprocess.
"""

import os
import shutil
import subprocess
import sys

import pytest
from click.testing import CliRunner

from ticli.cli import cli


class TestCLIHelp:
    """Test CLI help and basic invocation."""

    def setup_method(self):
        self.runner = CliRunner()

    def test_main_help(self):
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Ticli" in result.output

    def test_quality_flag(self):
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "quality" in result.output.lower()

    def test_quality_choices(self):
        result = self.runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "low" in result.output.lower()
        assert "high" in result.output.lower()
        assert "medium" in result.output.lower()
        assert "max" in result.output.lower()


class TestCLISubprocess:
    """Test the installed CLI command via subprocess."""

    @staticmethod
    def _resolve_cli(name: str) -> str:
        if os.environ.get("CLI_ANYTHING_FORCE_INSTALLED") == "1":
            path = shutil.which(name)
            if path:
                return path
            pytest.skip(f"{name} not found in PATH")
        return None

    def _run(self, args: list[str], **kwargs) -> subprocess.CompletedProcess:
        exe = self._resolve_cli("ticli")
        if exe:
            cmd = [exe] + args
        else:
            cmd = [sys.executable, "-m", "ticli.cli"] + args
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10, **kwargs)

    def test_help_exit_code(self):
        result = self._run(["--help"])
        assert result.returncode == 0
        assert "Ticli" in result.stdout

    def test_quality_in_help(self):
        result = self._run(["--help"])
        assert result.returncode == 0
        assert "quality" in result.stdout.lower()
