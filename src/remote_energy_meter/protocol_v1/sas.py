"""Exact six-word SAS derivation, rendering, parsing, and verification."""

from __future__ import annotations

import hashlib
import hmac
from importlib import resources
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from remote_energy_meter.protocol_v1.errors import authentication_required, invalid_request

_SAS_LENGTH: Final = 6
_NONCE_LENGTH: Final = 32
_MAX_TRANSCRIPT_LENGTH: Final = 4096
_MAX_SAS_PHRASE_LENGTH: Final = 65
_SAS_INFO: Final = b"rem-interactive-enroll-sas-v1"
_LIST_HASHES: Final = {
    "even.txt": "37a65f88512467edd12a1ab3eeb5f4328230e86711a8efde6333361ce10f4fcf",
    "odd.txt": "c2f23c2233d4d7291107e8f796c374d0cb1fb30c1391bcb4f6480243de8ebf5a",
}


def _load_word_list(name: str) -> tuple[str, ...]:
    raw = resources.files(__package__).joinpath("pgp_word_list", name).read_bytes()
    if hashlib.sha256(raw).hexdigest() != _LIST_HASHES[name]:
        raise RuntimeError("packaged PGP word list failed its integrity check")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise RuntimeError("packaged PGP word list is not ASCII") from None
    if not text.endswith("\n") or text.endswith("\n\n") or "\r" in text:
        raise RuntimeError("packaged PGP word list has invalid line endings")
    words = tuple(text[:-1].split("\n"))
    if len(words) != 256 or len(set(words)) != 256 or any(not word or word.strip() != word for word in words):
        raise RuntimeError("packaged PGP word list has invalid entries")
    return words


_EVEN_WORDS: Final = _load_word_list("even.txt")
_ODD_WORDS: Final = _load_word_list("odd.txt")
_WORD_INDEXES: Final = (
    {word: index for index, word in enumerate(_EVEN_WORDS)},
    {word: index for index, word in enumerate(_ODD_WORDS)},
)


def _check_sas_bytes(value: bytes) -> None:
    if len(value) != _SAS_LENGTH:
        raise invalid_request("SAS value has the wrong byte length")


def derive_interactive_sas(*, transcript: bytes, reporter_nonce: bytes, core_nonce: bytes) -> bytes:
    """Derive the six interactive-enrollment SAS bytes.

    Args:
        transcript: Non-empty enrollment transcript of at most 4096 bytes (4 KiB).
        reporter_nonce: Reporter's exact 32-byte nonce.
        core_nonce: Core's exact 32-byte nonce.

    Returns:
        The derived six-byte SAS value.

    Raises:
        ProtocolViolation: The transcript or either nonce has an invalid length.
            The failure has code ``invalid_request``.

    The transcript SHA-256 digest is the HKDF salt. The IKM is the reporter
    nonce followed by the core nonce, and the fixed profile info is
    ``rem-interactive-enroll-sas-v1``.
    """
    if not 0 < len(transcript) <= _MAX_TRANSCRIPT_LENGTH:
        raise invalid_request("interactive SAS transcript has the wrong length")
    if len(reporter_nonce) != _NONCE_LENGTH or len(core_nonce) != _NONCE_LENGTH:
        raise invalid_request("interactive SAS nonce has the wrong length")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_SAS_LENGTH,
        salt=hashlib.sha256(transcript).digest(),
        info=_SAS_INFO,
    ).derive(reporter_nonce + core_nonce)


def render_sas(value: bytes) -> str:
    """Render an exact six-byte SAS value as its canonical ASCII phrase.

    Args:
        value: Exact six-byte SAS value.

    Returns:
        Six PGP words separated by one ASCII space. Words alternate between the
        even list in positions one, three, and five and the odd list in positions
        two, four, and six, preserving the word lists' exact ASCII spelling and case.

    Raises:
        ProtocolViolation: ``value`` is not six bytes long. The failure has code
            ``invalid_request``.
    """
    _check_sas_bytes(value)
    return " ".join((_EVEN_WORDS if position % 2 == 0 else _ODD_WORDS)[index] for position, index in enumerate(value))


def parse_sas(phrase: str) -> bytes:
    """Parse a canonical six-word ASCII SAS phrase without normalization.

    Args:
        phrase: At most 65 characters containing exactly six PGP words with their
            published ASCII spelling and case. Words must use the even list in
            positions one, three, and five, the odd list in positions two, four,
            and six, and exactly one ASCII space between words.

    Returns:
        The exact six-byte SAS value represented by the words.

    Raises:
        ProtocolViolation: ``phrase`` exceeds 65 characters, is non-ASCII, or does
            not have the exact word count, list parity, spelling, case, or spacing.
            The failure has code ``invalid_request``.
    """
    if len(phrase) > _MAX_SAS_PHRASE_LENGTH:
        raise invalid_request("SAS phrase is too long")
    try:
        phrase.encode("ascii")
    except UnicodeEncodeError:
        raise invalid_request("SAS phrase is not ASCII") from None
    words = phrase.split(" ")
    if len(words) != _SAS_LENGTH or phrase != " ".join(words):
        raise invalid_request("SAS phrase does not use exact single-space rendering")
    indexes: list[int] = []
    for position, word in enumerate(words):
        index = _WORD_INDEXES[position % 2].get(word)
        if index is None:
            raise invalid_request("SAS phrase contains a word from the wrong list")
        indexes.append(index)
    return bytes(indexes)


def verify_sas(expected: bytes, phrase: str) -> None:
    """Verify a canonical rendered phrase against an expected six-byte SAS value.

    Args:
        expected: Exact six-byte SAS value derived for the enrollment.
        phrase: Canonical six-word ASCII phrase accepted by ``parse_sas``. The
            parser applies its 65-character cap and exact rendering rules.

    Returns:
        ``None`` after successful verification.

    Raises:
        ProtocolViolation: ``expected`` has an invalid length or ``phrase`` is
            malformed. The failure has code ``invalid_request``. A well-formed
            phrase that does not match fails with code ``authentication_required``.
    """
    _check_sas_bytes(expected)
    supplied = parse_sas(phrase)
    if not hmac.compare_digest(expected, supplied):
        raise authentication_required("SAS phrase does not match")
