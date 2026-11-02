#!/usr/bin/env python3
"""Fail when the repository contains references to someone's local setup.

This repository is public. Private network addresses, home-directory and volume paths, LAN
hostnames, MAC addresses, and personal e-mail addresses must never be committed: examples use
RFC 5737 documentation addresses (``192.0.2.0/24``, ``198.51.100.0/24``, ``203.0.113.0/24``) and
RFC 2606 reserved names (``*.example``, ``example.com``, ``*.invalid``).

Two layers:

* **Generic rules** (below) are public and contain no personal data, so they can run in CI.
* **A private denylist** of literal terms (your own domain, hostnames, repository names, ...) is
  read from ``.local-refs-denylist`` at the repository root, or from the file named by
  ``$LOCAL_REFS_DENYLIST``. That file is gitignored: personal terms must never be committed,
  not even as a rule that forbids them.

Modes::

    tools/check_local_refs.py              # tracked files in the working tree (CI)
    tools/check_local_refs.py --staged     # staged content (pre-commit hook)
    tools/check_local_refs.py --history    # every file version on any branch or tag (what a clone receives)

Exit status: 0 when clean, 1 when anything was found, 2 on a usage or git error.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DENYLIST_FILE: Final = ".local-refs-denylist"
DENYLIST_ENV: Final = "LOCAL_REFS_DENYLIST"

#: Binary or generated content that cannot carry a textual reference worth reviewing.
_SKIPPED_SUFFIXES: Final = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".whl", ".pyc"})


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    hint: str


_OCTET: Final = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"

RULES: Final[tuple[Rule, ...]] = (
    Rule(
        "private-ipv4",
        re.compile(rf"(?<![\d.])(?:10\.{_OCTET}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.{_OCTET}\.{_OCTET}(?![\d.])"),
        "RFC 1918 address; use a documentation address such as 192.0.2.10",
    ),
    Rule(
        "cgnat-or-link-local-ipv4",
        re.compile(rf"(?<![\d.])(?:100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])|169\.254)\.{_OCTET}\.{_OCTET}(?![\d.])"),
        "CGNAT/VPN or link-local address; use a documentation address such as 198.51.100.10",
    ),
    Rule(
        "mac-address",
        re.compile(r"(?<![0-9A-Fa-f:])(?!00:00:5[eE]:00:53:)(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"),
        "hardware MAC address; use 00:00:5e:00:53:xx (RFC 7042 documentation range)",
    ),
    Rule(
        "home-directory",
        re.compile(r"(?:/Users|/home)/(?!runner\b|user\b|USER\b|<)[A-Za-z0-9._-]+|C:\\Users\\[A-Za-z0-9._-]+"),
        "home-directory path; use ~ or a placeholder such as <home>",
    ),
    Rule(
        "mounted-volume",
        re.compile(r"/Volumes/(?!<)[A-Za-z0-9._-]+|/private/(?:nfs)\b|/net/[A-Za-z0-9._-]+"),
        "path on a mounted volume or network share; describe it generically",
    ),
    Rule(
        "lan-hostname",
        re.compile(
            r"(?<![A-Za-z0-9.-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:local|lan|internal|home\.arpa|home)"
            r"(?![A-Za-z0-9.(-])"  # not followed by "(": `Path.home()` is a call, not a host
        ),
        "LAN-only hostname; use a reserved name such as device.example",
    ),
    Rule(
        "email-address",
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?!(?:\d+x)\.)"
            r"(?!(?:[A-Za-z0-9-]+\.)*(?:example(?:\.(?:com|net|org))?|invalid|test|localhost)\b)"
            r"(?!users\.noreply\.github\.com\b)"
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
        ),
        "e-mail address; use noreply@example.invalid",
    ),
)

#: Lines that only *describe* a rule (this tool, its tests) opt out explicitly and visibly.
ALLOW_MARKER: Final = "local-refs: allow"


@dataclass(frozen=True, slots=True)
class Finding:
    location: str
    line: int
    rule: str
    value: str
    hint: str

    def render(self) -> str:
        return f"{self.location}:{self.line}: {self.rule}: {self.value!r} -- {self.hint}"


def load_denylist(repo_root: Path) -> tuple[str, ...]:
    """Literal, case-insensitive personal terms from the private, gitignored denylist file."""
    configured = os.environ.get(DENYLIST_ENV)
    path = Path(configured).expanduser() if configured else repo_root / DENYLIST_FILE
    if not path.is_file():
        return ()
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return tuple(terms)


def scan_text(location: str, text: str, denylist: Sequence[str] = ()) -> Iterator[Finding]:
    """Every finding in one file's text."""
    lowered_terms = [(term, term.casefold()) for term in denylist]
    for number, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                yield Finding(location, number, rule.name, match.group(0), rule.hint)
        folded = line.casefold()
        for term, lowered in lowered_terms:
            if lowered in folded:
                yield Finding(location, number, "private-denylist", term, f"listed in {DENYLIST_FILE}")


def _git(repo_root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True).stdout


def _skipped(path: str) -> bool:
    return Path(path).suffix.lower() in _SKIPPED_SUFFIXES


def _decoded(payload: bytes) -> str | None:
    if b"\0" in payload[:8192]:
        return None  # binary
    return payload.decode("utf-8", errors="replace")


def _tracked(repo_root: Path) -> Iterable[tuple[str, str]]:
    for path in _git(repo_root, "ls-files", "-z").decode().split("\0"):
        if not path or _skipped(path):
            continue
        file = repo_root / path
        if not file.is_file():
            continue  # deleted in the working tree but still in the index
        text = _decoded(file.read_bytes())
        if text is not None:
            yield path, text


def _staged(repo_root: Path) -> Iterable[tuple[str, str]]:
    names = _git(repo_root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    for path in names:
        if not path or _skipped(path):
            continue
        text = _decoded(_git(repo_root, "show", f":{path}"))
        if text is not None:
            yield path, text


def _history(repo_root: Path) -> Iterable[tuple[str, str]]:
    seen: set[str] = set()
    # Branches and tags are what a push or a clone publishes. Other local refs (tool state such as
    # scan caches) never leave the machine, so they are not part of the published history.
    for line in _git(repo_root, "rev-list", "--objects", "--branches", "--tags").decode().splitlines():
        sha, _, path = line.partition(" ")
        # A blob reached directly from a tag has no path; it is published all the same.
        if sha in seen or (path and _skipped(path)):
            continue
        if _git(repo_root, "cat-file", "-t", sha).strip() != b"blob":
            continue
        seen.add(sha)
        text = _decoded(_git(repo_root, "cat-file", "blob", sha))
        if text is not None:
            yield f"{path or '<blob>'}@{sha[:10]}", text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="scan staged content (pre-commit)")
    mode.add_argument("--history", action="store_true", help="scan every file version on any branch or tag")
    args = parser.parse_args(argv)

    try:
        repo_root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
        source = _staged if args.staged else _history if args.history else _tracked
        denylist = load_denylist(repo_root)
        findings = [finding for location, text in source(repo_root) for finding in scan_text(location, text, denylist)]
    except subprocess.CalledProcessError as error:
        print(f"check_local_refs: git failed: {error}", file=sys.stderr)
        return 2

    for finding in findings:
        print(finding.render())
    scope = "staged changes" if args.staged else "history" if args.history else "tracked files"
    private = f" + {len(denylist)} private term(s)" if denylist else ""
    if findings:
        print(f"✗ {len(findings)} local-setup reference(s) in {scope} (generic rules{private}).", file=sys.stderr)
        return 1
    print(f"✓ No local-setup references in {scope} (generic rules{private}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
