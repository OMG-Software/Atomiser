#!/usr/bin/env python3
"""Build a production deploy zip for Atomiser.

The zip contains only what is needed to run the app in production and nothing
else — no secrets, no virtualenv, no test suite, no docs, no runtime data, no
debug artifacts. Extract it over the install directory on the server
(e.g. /opt/atomiser) and `pip install -r requirements.txt`.

Include:
  app/            the application (code, templates, static)
  db/             migration scripts (run by init_db() on startup)
  scripts/        bootstrap + helpers (needed to create the first Configurator)
  nginx/          nginx site config + systemd unit (deployment reference)
  requirements.txt

Exclude (by omission or pattern):
  .env            SECRETS — never shipped; the server keeps its own
  tests/, venv/, data/, uploads/, *.md, .env.example, .pytest_cache,
  __pycache__/, *.pyc, cookies*.txt, test_video.mp4, the zip itself.

Usage:
  python scripts/make_deploy_zip.py            # -> ./atomiser-deploy-<ts>.zip
  python scripts/make_deploy_zip.py -o out.zip
  python scripts/make_deploy_zip.py --list     # just list what would be added
"""

import argparse
import datetime
import os
import sys
import zipfile
from pathlib import Path

# Top-level paths to include, as an explicit allowlist. Anything not listed
# here is left out — this is safer than a denylist because a stray .env or
# secrets file at the root is excluded by default.
INCLUDE_DIRS = ("app", "db", "scripts", "nginx")
INCLUDE_FILES = ("requirements.txt",)

# Directory names to skip anywhere in the tree (applies inside INCLUDE_DIRS).
EXCLUDE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".git",
    ".claude",
    ".idea",
    ".vscode",
}

# Filename suffixes/patterns to skip anywhere.
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".zip", ".egg-info")
EXCLUDE_FILENAMES = {".DS_Store", "Thumbs.db", "make_deploy_zip.py"}


def project_root() -> Path:
    # scripts/make_deploy_zip.py -> repo root is the parent of scripts/.
    return Path(__file__).resolve().parent.parent


def should_exclude_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS


def should_exclude_file(name: str) -> bool:
    if name in EXCLUDE_FILENAMES:
        return True
    name_lower = name.lower()
    # Debug artifacts that should never ship.
    if name_lower.startswith("cookies") and name_lower.endswith(".txt"):
        return True
    if name_lower == "test_video.mp4":
        return True
    return any(name_lower.endswith(suf) for suf in EXCLUDE_SUFFIXES)


def collect_files(root: Path):
    """Yield Path objects to include, relative to root."""
    seen = set()

    # Explicit top-level files first.
    for name in INCLUDE_FILES:
        f = root / name
        if f.is_file():
            seen.add(f.resolve())
            yield f

    # Then walk each included top-level directory.
    for top in INCLUDE_DIRS:
        top_path = root / top
        if not top_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top_path):
            # Prune excluded dirs in place so os.walk doesn't descend into them.
            dirnames[:] = [d for d in dirnames if not should_exclude_dir(d)]
            for fn in filenames:
                if should_exclude_file(fn):
                    continue
                p = Path(dirpath) / fn
                if p.resolve() in seen:
                    continue
                seen.add(p.resolve())
                yield p


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "-o", "--out",
        help="Output zip path (default: atomiser-deploy-<timestamp>.zip in project root)",
    )
    ap.add_argument(
        "--list", action="store_true",
        help="List the files that would be added and exit (no zip written)",
    )
    args = ap.parse_args()

    root = project_root()
    files = sorted(collect_files(root), key=lambda p: str(p.relative_to(root)))

    if args.list:
        total = 0
        for p in files:
            rel = p.relative_to(root).as_posix()
            size = p.stat().st_size
            total += size
            print(f"{human_size(size):>10}  {rel}")
        print(f"\n{len(files)} files, {human_size(total)} total")
        return 0

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = root / args.out
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = root / f"atomiser-deploy-{ts}.zip"

    # Never write the zip inside one of the included dirs; default is project
    # root, which is not walked (we only walk INCLUDE_DIRS), so it's safe.
    print(f"Writing {out_path}")
    total = 0
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            arcname = p.relative_to(root).as_posix()
            zf.write(p, arcname)
            size = p.stat().st_size
            total += size
            count += 1
            print(f"  + {arcname}")

    zip_size = out_path.stat().st_size
    print(
        f"\nDone: {count} files, {human_size(total)} uncompressed "
        f"-> {human_size(zip_size)} compressed"
    )
    print(f"Deploy: scp {out_path} server:/opt/atomiser/  then unzip there")
    return 0


if __name__ == "__main__":
    sys.exit(main())