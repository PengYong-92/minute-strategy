#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="$ROOT_DIR/dist"
PACKAGE_PREFIX="event-contract-monitor"
INCLUDE_REPORTS="${INCLUDE_REPORTS:-0}"

usage() {
  cat <<'USAGE'
Usage: scripts/package.sh [--output-dir DIR] [--name NAME] [--include-reports]

Creates portable source archives for macOS and Linux:
  - NAME.tar.gz
  - NAME.zip

Environment:
  INCLUDE_REPORTS=1  Include reports/ in the package.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --name)
      PACKAGE_PREFIX="$2"
      shift 2
      ;;
    --include-reports)
      INCLUDE_REPORTS="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-python3}"
TIMESTAMP="$("$PYTHON_BIN" - <<'PY'
from datetime import datetime
print(datetime.now().strftime("%Y%m%d-%H%M%S"))
PY
)"
PACKAGE_NAME="${PACKAGE_PREFIX}-${TIMESTAMP}"

mkdir -p "$OUTPUT_DIR"

"$PYTHON_BIN" - "$ROOT_DIR" "$OUTPUT_DIR" "$PACKAGE_NAME" "$INCLUDE_REPORTS" <<'PY'
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


root = Path(sys.argv[1]).resolve()
output_dir = Path(sys.argv[2]).resolve()
package_name = sys.argv[3]
include_reports = sys.argv[4] == "1"

excluded_dirs = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "data",
}
if not include_reports:
    excluded_dirs.add("reports")

excluded_suffixes = {".pyc", ".pyo", ".log"}
runtime_roots = (
    ".gitignore",
    "README.md",
    "requirements.txt",
    "app",
    "scripts",
)


def should_include(path: Path) -> bool:
    relative = path.relative_to(root)
    parts = set(relative.parts)
    if parts & excluded_dirs:
        return False
    if path.name == ".DS_Store":
        return False
    if path.suffix in excluded_suffixes:
        return False
    return True


def copy_tree(target_root: Path) -> None:
    package_root = target_root / package_name
    package_root.mkdir(parents=True)
    selected_roots = list(runtime_roots)
    if include_reports:
        selected_roots.append("reports")
    for selected in selected_roots:
        source_root = root / selected
        if not source_root.exists():
            continue
        sources = (source_root,) if source_root.is_file() else source_root.rglob("*")
        for source in sources:
            if not should_include(source):
                continue
            relative = source.relative_to(root)
            target = package_root / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for script in (package_root / "scripts").glob("*.sh"):
        mode = script.stat().st_mode
        script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


with tempfile.TemporaryDirectory() as tmp:
    temp_root = Path(tmp)
    copy_tree(temp_root)
    package_root = temp_root / package_name

    tar_path = output_dir / f"{package_name}.tar.gz"
    zip_path = output_dir / f"{package_name}.zip"

    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(package_root, arcname=package_name)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package_root.rglob("*")):
            archive.write(path, path.relative_to(temp_root))

print(f"created: {output_dir / (package_name + '.tar.gz')}")
print(f"created: {output_dir / (package_name + '.zip')}")
PY
