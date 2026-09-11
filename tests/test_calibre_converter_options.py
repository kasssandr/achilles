"""Conversion options handed to Calibre's ebook-convert.

The EPUB we produce is read once and thrown away, so none of the size limits
that exist for e-readers apply. Splitting by size makes Calibre abort with
``SplitError`` on books whose markup offers no split point.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.extractors.calibre_converter import CalibreConverter
from src.extractors.exceptions import ConversionError


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


@pytest.fixture
def converter():
    with patch.object(CalibreConverter, "_check_calibre_available", lambda self: None):
        return CalibreConverter(calibre_path="ebook-convert")


def _run_and_capture(converter, tmp_path, target_format):
    """Run a conversion against a stubbed ebook-convert and return its argv."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[2]).write_bytes(b"")
        return _Result()

    with patch("subprocess.run", side_effect=fake_run):
        converter._run_conversion(
            tmp_path / "book.azw3", tmp_path / f"out.{target_format}", target_format
        )
    return captured["cmd"]


def test_epub_conversion_disables_size_based_splitting(converter, tmp_path):
    cmd = _run_and_capture(converter, tmp_path, "epub")

    assert "--flow-size" in cmd
    assert cmd[cmd.index("--flow-size") + 1] == "0"


def test_pdf_conversion_keeps_its_own_options(converter, tmp_path):
    cmd = _run_and_capture(converter, tmp_path, "pdf")

    assert "--flow-size" not in cmd
    assert "--paper-size" in cmd


def test_conversion_failure_reports_calibre_stderr(converter, tmp_path):
    def fake_run(cmd, **kwargs):
        return _Result(returncode=1, stderr="SplitError: no sensible splitting point")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(ConversionError, match="SplitError"):
            converter._run_conversion(
                tmp_path / "book.azw3", tmp_path / "out.epub", "epub"
            )
