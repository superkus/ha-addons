"""Conformance checks for the RFC-cited PGP word-list data and provenance."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).parents[2]
PROFILE_DATA: Final = ROOT / "docs" / "protocol" / "v1" / "pgp-word-list"
PACKAGE_DATA: Final = resources.files("remote_energy_meter.protocol_v1").joinpath("pgp_word_list")
EXPECTED_HASHES: Final = {
    "even.txt": "37a65f88512467edd12a1ab3eeb5f4328230e86711a8efde6333361ce10f4fcf",
    "odd.txt": "c2f23c2233d4d7291107e8f796c374d0cb1fb30c1391bcb4f6480243de8ebf5a",
}


def _assert_exact_list(raw: bytes, expected_hash: str) -> list[str]:
    assert hashlib.sha256(raw).hexdigest() == expected_hash
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert b"\r" not in raw
    assert raw.decode("ascii").encode("ascii") == raw
    words = raw[:-1].decode("ascii").split("\n")
    assert len(words) == 256
    assert len(set(words)) == 256
    assert all(word and word == word.strip() and word.isalpha() for word in words)
    return words


def test_profile_word_lists_are_exact_complete_ascii_ordered_and_unique() -> None:
    even = _assert_exact_list((PROFILE_DATA / "even.txt").read_bytes(), EXPECTED_HASHES["even.txt"])
    odd = _assert_exact_list((PROFILE_DATA / "odd.txt").read_bytes(), EXPECTED_HASHES["odd.txt"])

    assert even[0x00] == "aardvark"
    assert odd[0x00] == "adroitness"
    assert even[0x09] == "Algol"
    assert odd[0x0A] == "Apollo"
    assert even[0xE5] == "topmost"
    assert odd[0x82] == "Istanbul"
    assert even[0xFF] == "Zulu"
    assert odd[0xFF] == "Yucatan"


def test_packaged_word_lists_and_licensing_are_byte_identical_to_profile_data() -> None:
    for name in (*EXPECTED_HASHES, "notice.md", "gfdl-1.2.txt"):
        assert PACKAGE_DATA.joinpath(name).read_bytes() == (PROFILE_DATA / name).read_bytes()


def test_adjacent_notice_records_source_integrity_extraction_attribution_and_cross_checks() -> None:
    notice = (PROFILE_DATA / "notice.md").read_text(encoding="utf-8")

    required = {
        "oldid=400752943",
        "20200228011544",
        "2026-11-10T18:48:48Z",
        "text/html; charset=UTF-8",
        "cdf0421415db5b3bd428004c237371543f7c70964fa82741ee24f1c8ad69fa71",
        EXPECTED_HASHES["even.txt"],
        EXPECTED_HASHES["odd.txt"],
        "Patrick Juola",
        "Philip Zimmermann",
        "Jon Callas",
        "PGP Corporation",
        "GNU Free Documentation License",
        "ba8cfdd06ca2680fc46881a16e0149ff3a669f05",
        "0c95b82de3cfda2e189cf334e948c701f61fb752",
        "69ed843a4a11820fb6647f06228ca982e5e1777f",
        "byte-for-byte",
        "repository code remains MIT",
    }
    assert all(value in notice for value in required)
    assert "non-commercial" not in notice.lower()
    assert (
        (PROFILE_DATA / "gfdl-1.2.txt")
        .read_text(encoding="ascii")
        .startswith("                GNU Free Documentation License\n                  Version 1.2, November 2002\n")
    )
