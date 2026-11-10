"""Unit contract for the protocol-v1 six-word interactive SAS."""

from __future__ import annotations

import hashlib
import hmac
import subprocess
import sys
import zipfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import SupportsIndex

import pytest

from remote_energy_meter.protocol_v1 import (
    ProtocolViolation,
    derive_interactive_sas,
    parse_sas,
    render_sas,
    verify_sas,
)


def _assert_code(code: str, operation: Callable[[], object]) -> None:
    with pytest.raises(ProtocolViolation) as caught:
        operation()
    assert caught.value.code == code


def test_known_rfc_pgp_word_list_examples_preserve_parity_case_and_order() -> None:
    assert render_sas(bytes.fromhex("e58200000000")) == ("topmost Istanbul aardvark adroitness aardvark adroitness")
    assert render_sas(bytes.fromhex("82e500000000")) == ("miser travesty aardvark adroitness aardvark adroitness")


def test_render_sas_covers_every_index_in_every_applicable_position() -> None:
    tables = tuple(
        resources.files("remote_energy_meter.protocol_v1")
        .joinpath("pgp_word_list", name)
        .read_text(encoding="ascii")
        .splitlines()
        for name in ("even.txt", "odd.txt")
    )
    baseline = bytearray(6)
    for position in range(6):
        seen: set[str] = set()
        for index in range(256):
            value = baseline.copy()
            value[position] = index
            rendered = render_sas(bytes(value))
            words = rendered.split(" ")
            assert len(words) == 6
            assert words[position] == tables[position % 2][index]
            assert parse_sas(rendered) == bytes(value)
            seen.add(words[position])
        assert len(seen) == 256


def test_render_sas_uses_exact_single_space_and_six_words() -> None:
    rendered = render_sas(bytes.fromhex("4df104cfb9f9"))

    assert rendered == "dreadful vacancy adrift Saturday sentence Waterloo"
    assert rendered.count(" ") == 5
    assert rendered == " ".join(rendered.split(" "))
    assert rendered.encode("ascii").decode("ascii") == rendered


@pytest.mark.parametrize("value", [b"", b"\x00" * 5, b"\x00" * 7])
def test_render_sas_rejects_every_non_six_byte_value(value: bytes) -> None:
    _assert_code("invalid_request", lambda: render_sas(value))


def test_derive_interactive_sas_uses_transcript_digest_nonce_order_info_and_six_bytes() -> None:
    transcript = b"REM-TRANSCRIPT\x01test transcript bytes"
    reporter_nonce = bytes(range(32))
    core_nonce = bytes(range(32, 64))

    derived = derive_interactive_sas(
        transcript=transcript,
        reporter_nonce=reporter_nonce,
        core_nonce=core_nonce,
    )

    assert derived == bytes.fromhex("6f5a168aa07c")
    assert len(derived) == 6
    assert derived != derive_interactive_sas(
        transcript=transcript,
        reporter_nonce=core_nonce,
        core_nonce=reporter_nonce,
    )
    assert derived != derive_interactive_sas(
        transcript=transcript + b"!",
        reporter_nonce=reporter_nonce,
        core_nonce=core_nonce,
    )


@pytest.mark.parametrize(
    ("reporter_nonce", "core_nonce"),
    [
        (b"\x00" * 31, b"\x00" * 32),
        (b"\x00" * 33, b"\x00" * 32),
        (b"\x00" * 32, b"\x00" * 31),
        (b"\x00" * 32, b"\x00" * 33),
    ],
)
def test_derive_interactive_sas_rejects_wrong_nonce_lengths(reporter_nonce: bytes, core_nonce: bytes) -> None:
    _assert_code(
        "invalid_request",
        lambda: derive_interactive_sas(
            transcript=b"transcript",
            reporter_nonce=reporter_nonce,
            core_nonce=core_nonce,
        ),
    )


@pytest.mark.parametrize("transcript", [b"", b"x" * 4097])
def test_derive_interactive_sas_rejects_empty_or_oversized_transcripts(transcript: bytes) -> None:
    _assert_code(
        "invalid_request",
        lambda: derive_interactive_sas(
            transcript=transcript,
            reporter_nonce=b"r" * 32,
            core_nonce=b"c" * 32,
        ),
    )


def test_parse_sas_round_trips_exact_words_without_normalization() -> None:
    phrase = "dreadful vacancy adrift Saturday sentence Waterloo"

    assert parse_sas(phrase) == bytes.fromhex("4df104cfb9f9")
    verify_sas(bytes.fromhex("4df104cfb9f9"), phrase)


def test_parse_sas_rejects_oversized_phrase_before_encoding_or_splitting() -> None:
    class TrackedPhrase(str):
        encode_called = False
        split_called = False

        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            self.encode_called = True
            return super().encode(encoding, errors)

        def split(self, sep: str | None = None, maxsplit: SupportsIndex = -1) -> list[str]:
            self.split_called = True
            return super().split(sep, maxsplit)

    phrase = TrackedPhrase("x" * 66)

    _assert_code("invalid_request", lambda: parse_sas(phrase))
    assert not phrase.encode_called
    assert not phrase.split_called


@pytest.mark.parametrize(
    "phrase",
    [
        "dreadful",
        "disruptive vacancy adrift Saturday sentence Waterloo",
        "dreadful unwind adrift Saturday sentence Waterloo",
        "Dreadful vacancy adrift Saturday sentence Waterloo",
        "dreadful vacancy adrift Saturday sentence Waterloo!",
        "dreadful vacancy adrift Saturday sentence Yucatán",
        " dreadful vacancy adrift Saturday sentence Waterloo",
        "dreadful vacancy adrift Saturday sentence Waterloo ",
        "dreadful  vacancy adrift Saturday sentence Waterloo",
        "dreadful\tvacancy adrift Saturday sentence Waterloo",
        "dreadful\nvacancy adrift Saturday sentence Waterloo",
    ],
)
def test_parse_sas_rejects_word_count_list_case_punctuation_and_spacing_mutations(phrase: str) -> None:
    _assert_code("invalid_request", lambda: parse_sas(phrase))


@pytest.mark.parametrize(
    "phrase",
    [
        "drifter vacancy adrift Saturday sentence Waterloo",
        "dreadful adviser adrift Saturday sentence Waterloo",
        "dreadful vacancy adult Saturday sentence Waterloo",
        "dreadful vacancy adrift savagery sentence Waterloo",
        "dreadful vacancy adrift Saturday shadow Waterloo",
        "dreadful vacancy adrift Saturday sentence Wichita",
    ],
)
def test_verify_sas_rejects_one_valid_index_mutation_in_each_position(phrase: str) -> None:
    _assert_code("authentication_required", lambda: verify_sas(bytes.fromhex("4df104cfb9f9"), phrase))


def test_verify_sas_compares_authentication_bytes_with_constant_time_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compared: list[tuple[bytes, bytes]] = []

    def fake_compare(left: bytes, right: bytes) -> bool:
        compared.append((left, right))
        return True

    monkeypatch.setattr(hmac, "compare_digest", fake_compare)

    verify_sas(bytes.fromhex("4df104cfb9f9"), "dreadful vacancy adrift Saturday sentence Waterloo")

    assert compared == [(bytes.fromhex("4df104cfb9f9"), bytes.fromhex("4df104cfb9f9"))]


def test_built_wheel_contains_the_exact_licensed_word_list_resources(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path),
            str(root),
        ],
        check=True,
        cwd=root,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("remote_energy_meter-*.whl"))
    expected = {
        "remote_energy_meter/protocol_v1/pgp_word_list/even.txt",
        "remote_energy_meter/protocol_v1/pgp_word_list/odd.txt",
        "remote_energy_meter/protocol_v1/pgp_word_list/notice.md",
        "remote_energy_meter/protocol_v1/pgp_word_list/gfdl-1.2.txt",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert expected <= names
        for resource_name in expected:
            assert archive.read(resource_name) == (root / "src" / resource_name).read_bytes()
        assert (
            hashlib.sha256(archive.read("remote_energy_meter/protocol_v1/pgp_word_list/even.txt")).hexdigest()
            == "37a65f88512467edd12a1ab3eeb5f4328230e86711a8efde6333361ce10f4fcf"
        )
        assert (
            hashlib.sha256(archive.read("remote_energy_meter/protocol_v1/pgp_word_list/odd.txt")).hexdigest()
            == "c2f23c2233d4d7291107e8f796c374d0cb1fb30c1391bcb4f6480243de8ebf5a"
        )
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
        assert "License: MIT for code and documentation; GFDL-1.2-only for bundled PGP Word List data\n" in metadata
