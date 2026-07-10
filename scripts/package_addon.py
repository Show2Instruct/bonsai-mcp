"""Package the Blender add-on as an installable zip."""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_FILE = REPO_ROOT / "blender_addon" / "bonsai_bridge.py"
PACKAGE_INIT = REPO_ROOT / "src" / "bonsai_mcp" / "__init__.py"


def addon_version(source: str) -> str:
    match = re.search(r'"version":\s*\(([^)]*)\)', source)
    if not match:
        raise SystemExit(f"Could not find the bl_info version in {ADDON_FILE}")
    parts = [p.strip() for p in match.group(1).split(",") if p.strip()]
    return ".".join(parts)


def package_version() -> str | None:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', PACKAGE_INIT.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Bundle the Blender add-on into a zip.")
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "dist"),
        help="Output directory (default: dist/).",
    )
    args = parser.parse_args()

    source = ADDON_FILE.read_text(encoding="utf-8")
    version = addon_version(source)

    pkg_version = package_version()
    if pkg_version and pkg_version != version:
        print(
            f"WARNING: add-on version {version} != package version {pkg_version}. "
            "Bump both before releasing."
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"bonsai_mcp_bridge-{version}.zip"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(ADDON_FILE, arcname="bonsai_bridge.py")

    print(f"Wrote {zip_path}")
    print("Install in Blender: Edit > Preferences > Add-ons > Install... > pick this zip.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
