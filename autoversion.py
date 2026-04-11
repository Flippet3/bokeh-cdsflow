#!/usr/bin/env python3
"""
Resolve ``version = "...${{X}}..."`` in pyproject.toml against PyPI.

For the current major.minor prefix (e.g. 0.1.), X is 0 when no matching
``major.minor.N`` release exists on PyPI; otherwise max(N) + 1.
Uses only the standard library (urllib, no requests).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PLACEHOLDER = "${{X}}"
VERSION_LINE_RE = re.compile(
    r"(?m)^(?P<prefix>version\s*=\s*\")(?P<base>[^\"]*)"
    + re.escape(PLACEHOLDER)
    + r"(?P<suffix>\")$"
)
BASE_RE = re.compile(r"^(\d+)\.(\d+)\.$")
PYPI_JSON = "https://pypi.org/pypi/{name}/json"
RELEASE_SUFFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_project_name(pyproject_path: Path) -> str:
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    try:
        name = data["project"]["name"]
    except KeyError as e:
        raise SystemExit("pyproject.toml: missing [project] name") from e
    if not isinstance(name, str) or not name:
        raise SystemExit("pyproject.toml: [project] name must be a non-empty string")
    return name


def parse_version_template(pyproject_text: str) -> tuple[str, int, int]:  # (full line, major, minor)
    m = VERSION_LINE_RE.search(pyproject_text)
    if not m:
        raise SystemExit(
            f'pyproject.toml: no line matching version = "...{PLACEHOLDER}..." '
            "(single line, double-quoted value)."
        )
    base = m.group("base")
    bm = BASE_RE.match(base)
    if not bm:
        raise SystemExit(
            f'pyproject.toml: version prefix before {PLACEHOLDER!r} must look like '
            f'"major.minor." (got {base!r}).'
        )
    return m.group(0), int(bm.group(1)), int(bm.group(2))


def fetch_release_versions(project_name: str, index_url: str) -> list[str]:
    url = index_url.format(name=project_name)
    req = Request(url, headers={"User-Agent": "bokeh-cdsflow-autoversion/1"})
    try:
        with urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except HTTPError as e:
        if e.code == 404:
            return []
        raise SystemExit(f"PyPI HTTP {e.code} for {url}") from e
    except URLError as e:
        raise SystemExit(f"PyPI request failed: {e}") from e

    releases = payload.get("releases")
    if not isinstance(releases, dict):
        return []
    return [k for k in releases if isinstance(k, str)]


def next_build_number(major: int, minor: int, release_keys: list[str]) -> int:
    candidates: list[int] = []
    for key in release_keys:
        rm = RELEASE_SUFFIX_RE.match(key)
        if not rm:
            continue
        mj, mn, micro = int(rm.group(1)), int(rm.group(2)), int(rm.group(3))
        if mj == major and mn == minor:
            candidates.append(micro)
    if not candidates:
        return 0
    return max(candidates) + 1


def apply_version(pyproject_text: str, new_line: str) -> str:
    m = VERSION_LINE_RE.search(pyproject_text)
    assert m is not None
    return pyproject_text[: m.start()] + new_line + pyproject_text[m.end() :]


def run(
    pyproject_path: Path,
    *,
    dry_run: bool,
    index_url: str,
) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    old_line, major, minor = parse_version_template(text)
    project_name = read_project_name(pyproject_path)
    keys = fetch_release_versions(project_name, index_url)
    n = next_build_number(major, minor, keys)
    new_line = old_line.replace(PLACEHOLDER, str(n), 1)
    new_text = apply_version(text, new_line)

    resolved = f"{major}.{minor}.{n}"
    if dry_run:
        print(f"[dry-run] would set version to {resolved} (PyPI package {project_name!r})")
        print(new_line)
        return resolved

    pyproject_path.write_text(new_text, encoding="utf-8")
    print(f"Set version to {resolved} ({pyproject_path})")
    return resolved


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--pyproject",
        type=Path,
        default=Path(__file__).parent / "pyproject.toml",
   
        help="Path to pyproject.toml (default: ./pyproject.toml)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved version line but do not write the file.",
    )
    p.add_argument(
        "--index-json-url",
        default=PYPI_JSON,
        help=f"PyPI JSON API URL template with {{name}} placeholder (default: {PYPI_JSON!r})",
    )
    args = p.parse_args(argv)
    path = args.pyproject.resolve()
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")
    run(path, dry_run=args.dry_run, index_url=args.index_json_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
