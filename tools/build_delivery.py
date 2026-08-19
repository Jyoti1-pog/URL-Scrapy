"""Build the client ZIP.

Two things this gets right that a `zip -r` does not, and both were learned by
installing the result rather than by reading the list.

EXCLUSIONS ARE ANCHORED TO THE ROOT. `images` and `store` are each BOTH a
run-output directory at the top level AND a source package inside
`haat_lister/`. Matching the bare name at any depth silently drops two packages
and the client's install dies with `No module named haat_lister.images`. The
repo's own .gitignore carries a comment about being caught by exactly this; the
first version of this script was caught by it again.

NOTHING PRIVATE LEAVES THE MACHINE. `.env` holds secrets, `store/ledger.db` and
`runs/` hold the operator's own catalogue, and `domains.yaml` records which
shops refused them. None of it is the client's, so none of it ships.

Usage:  python tools/build_delivery.py [--out PATH]
"""

from __future__ import annotations

import argparse
import fnmatch
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Anchored: only excluded when they ARE the top-level directory.
EXCLUDE_ROOT_DIRS = {
    ".git",
    ".venv",
    "runs",
    "store",
    "downloads",
    "images",
    "profiles",
    "haat_lister.egg-info",
    ".vscode",
    ".idea",
    "docs",
    "tools",
}

# Junk wherever it appears.
EXCLUDE_ANY_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
}

EXCLUDE_FILES = [
    ".env",  # secrets
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.tmp",
    "*.bak",
    "*.db",  # the ledger: the operator's own catalogue
    "*.db-*",
    "domains.yaml",  # which shops refused them
    "listings.csv",  # loose output at the root
    "review.csv",
    "image_manifest.csv",
    "my-urls.txt",
    "haat-lister-delivery.zip",
]

# Must be in the ZIP or the client cannot start. Checked after writing, because
# a delivery that is missing one of these fails on their machine, not ours.
REQUIRED = [
    "pyproject.toml",
    "config.yaml",
    "taxonomy.yaml",
    ".env.example",
    "START-HERE.md",
    "README.md",
    "haat-bulk-listings-template.csv",
    "web/dist/index.html",
    "haat_lister/cli.py",
]

REQUIRED_PACKAGES = [
    "images",
    "store",
    "fetch",
    "output",
    "extract",
    "api",
    "ingest",
    "enrich",
    "policy",
    "utils",
]


def excluded(path: pathlib.Path) -> str | None:
    parts = path.relative_to(ROOT).parts
    if parts[0] in EXCLUDE_ROOT_DIRS:
        return f"root:{parts[0]}"
    if any(part in EXCLUDE_ANY_DIRS for part in parts[:-1]):
        return "junk"
    for pattern in EXCLUDE_FILES:
        if fnmatch.fnmatch(path.name, pattern):
            return f"file:{pattern}"
    return None


def build(out: pathlib.Path) -> int:
    files = [p for p in sorted(ROOT.rglob("*")) if p.is_file() and not excluded(p)]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, pathlib.Path("haat-lister") / path.relative_to(ROOT))

    names = set(zipfile.ZipFile(out).namelist())
    problems: list[str] = []

    for needed in REQUIRED:
        if f"haat-lister/{needed}" not in names:
            problems.append(f"missing: {needed}")

    for package in REQUIRED_PACKAGES:
        if not any(f"/haat_lister/{package}/" in n for n in names):
            problems.append(f"missing package: haat_lister/{package}/")

    # The leak check, stated as its own pass so it cannot be skimmed past.
    for label, hit in (
        (".env", [n for n in names if n.endswith("/.env")]),
        ("ledger", [n for n in names if n.endswith(".db")]),
        ("runs/", [n for n in names if "/runs/" in n]),
        (".git", [n for n in names if "/.git/" in n]),
        ("node_modules", [n for n in names if "node_modules" in n]),
    ):
        if hit:
            problems.append(f"LEAK {label}: {hit[:2]}")

    size = out.stat().st_size / 1_048_576
    print(f"{len(files)} files -> {out}  ({size:.1f} MB)")
    if problems:
        for line in problems:
            print(f"  {line}")
        return 1
    print("  all required files and packages present; nothing private included")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "haat-lister-delivery.zip")
    args = parser.parse_args()
    return build(args.out)


if __name__ == "__main__":
    sys.exit(main())
