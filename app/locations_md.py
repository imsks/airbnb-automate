"""Read location lines from ``locations.md`` (one place per line)."""

from __future__ import annotations

from pathlib import Path


def read_locations_md(path: str | Path) -> list[str]:
    """Return non-empty, non-comment lines from a markdown/text file."""
    p = Path(path)
    if not p.is_file():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def project_locations_md(project_root: str | Path) -> Path:
    return Path(project_root) / "locations.md"
