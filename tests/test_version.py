"""The three version declarations must agree; doctor's skew warning relies on it."""

from __future__ import annotations

import re
from pathlib import Path

from bonsai_mcp import __version__

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "no version field in pyproject.toml"
    return match.group(1)


def _addon_version() -> str:
    text = (_REPO_ROOT / "blender_addon" / "bonsai_bridge.py").read_text(encoding="utf-8")
    match = re.search(r'"version":\s*\(([^)]*)\)', text)
    assert match, "no bl_info version in bonsai_bridge.py"
    return ".".join(part.strip() for part in match.group(1).split(",") if part.strip())


def test_versions_agree():
    assert __version__ == _pyproject_version() == _addon_version()
