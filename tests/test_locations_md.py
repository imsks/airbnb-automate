"""Tests for locations.md reader."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.locations_md import read_locations_md


def test_read_locations_md_skips_comments_and_blanks():
    fd, path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# header\n\nFoo, Bar\n  \n# skip\nBaz\n"
            )
        assert read_locations_md(path) == ["Foo, Bar", "Baz"]
    finally:
        os.unlink(path)
