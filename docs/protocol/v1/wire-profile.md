# REM protocol v1 — wire profile

Status: **normative draft, not approved for distribution.** Task 3.4's independent security review must approve this
profile before any reporter or integration build that speaks it is distributed. Nothing in this repository listens on
or sends this protocol yet.

The key words MUST, MUST NOT, SHALL, SHOULD and MAY are used as in RFC 2119 and RFC 8174. Where this profile and the
specification in `.kiro/specs/native-computer-reporters/` disagree, the specification wins, and the disagreement is a
bug in this profile. Section 15 lists every place where this profile fixes a detail the specification left open.

Machine-readable parts are authoritative over the prose tables that summarize them:

| Artifact | Authoritative for |
| --- | --- |
| `routes.json` | Every method, path template, query form, request/success byte cap, item cap, success status, purpose label, signature profile, trust state, and replay rule |
| `schemas/*.schema.json` | Every JSON body, the problem object, the MQTT envelope and payloads, and the vector format (JSON Schema Draft 2020-12) |
| `vectors/*.json` | The exact bytes every implementation must reproduce and the mutations it must reject |
| `vectors/manifest.json` | Which vector files and contract files make up profile version 1, and their SHA-256 checksums |

## 1. Primitive encodings

| Type | Wire form | Rule |
| --- | --- | --- |
| `b64u<n>` | Unpadded base64url (RFC 4648 §5) of exactly *n* bytes | No `=`, no whitespace, no standard-alphabet `+` or `/`. Unused trailing bits MUST be zero, so the final character is restricted (16 bytes → 22 chars ending in `[AQgw]`; 32 bytes → 43 chars ending in `[AEIMQUYcgkosw048]`; 64 bytes → 86 chars ending in `[AQgw]`). Decoders MUST re-encode and compare. |
| `opaque128`, `opaque256` | Canonical unpadded base64url of 1–128 or 1–256 bytes | Same canonical rule; the content is opaque to the receiver. |
| `uint64` | JSON integer 0–18446744073709551615; in headers, canonical unsigned decimal | No sign, exponent, fraction or leading zero except `0`. JSON decoders MUST parse integers exactly (no float conversion) and reject values outside the range. |
| `unixSeconds` | `uint64` seconds since 1970-01-01T00:00:00Z | Always produced from the core's clock. |
| `rfc3339Utc` | `YYYY-MM-DDTHH:MM:SSZ` | A real proleptic-Gregorian date in years 0001–9999, with the month's valid day count and Gregorian leap-year rules. Offset forms, fractional seconds, leap seconds and `24:00:00` are rejected. Only used in the managed-enrollment artifact and token status/revocation bodies. |
| `identifier` | `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` | `instance_id`, `managed_subject_id`. |
| `dnsHost` | Canonical lowercase IDNA2008 A-label hostname, 1–253 ASCII bytes, labels 1–63 bytes, no trailing dot | Every label is an NR-LDH label or a valid A-label. IP literals, every alternate numeric IP form accepted by the connection stack, U-labels, percent encoding and upper case are rejected. |
| `serverIdentity` | `https://<dnsHost>:<port>` | At most 267 bytes; `dnsHost` receives the full semantic validation above; `port` is canonical decimal 1–65535 with no leading zero; userinfo, path (including `/`), query and fragment are rejected. |
| `p256PublicJwk` | `{"crv":"P-256","kty":"EC","x":b64u32,"y":b64u32}` | Exactly these four members. `d`, any other member, a compressed point, a point not on the curve, or the point at infinity is rejected. |
| P-256 SPKI | DER SubjectPublicKeyInfo, 91 bytes, prefix `3059301306072a8648ce3d020106082a8648ce3d03010703420004` | Its identity is SHA-256 of these exact DER bytes. |

`rfc3339Utc` uses the schema's exact lexical pattern as an assertion, not merely the implementation-defined JSON Schema
`format` annotation. A parser MUST also construct the represented proleptic-Gregorian UTC date and reject normalization
(for example, converting an invalid month day into the next month). Parsing and formatting MUST round-trip to the exact
input, including upper-case `T` and `Z` and two digits for every month, day, hour, minute and second field.

`dnsHost` validation is semantic. The schema patterns are only a lexical prefilter. A parser MUST split the ASCII input
into labels, reject reserved LDH labels, decode every `xn--` label with non-transitional IDNA2008 lookup processing,
re-encode it, and require an exact byte-for-byte match with the input label. A parser MUST reject the input if the same
address parser used by its URL or connection stack accepts the complete host as any IPv4 or IPv6 address, including
single-integer, shortened-component, hexadecimal and octal IPv4 spellings. `serverIdentity` MUST apply this complete
`dnsHost` procedure to its extracted host before opening a connection. Implementations MUST NOT infer semantic IDNA or
numeric-address validity from the regular expressions alone. The schema annotations `rem-idna2008-alabel-host-v1`,
`rem-server-identity-v1` and `rem-rfc3339-utc-gregorian-v1` identify these mandatory parser checks.

`display_name` follows the text profile `rem-display-name-v1`: UTF-8, already NFC, 1–128 **bytes**, no leading or
trailing White_Space, and no code point in general categories Cc, Cf (which includes every bidirectional control), Cs,
Co, Cn, Zl or Zp. `platform` is `macos`; `architecture` is `arm64` or `x86_64`; `reporter_version` is a 1–64-byte
Semantic Version 2.0.0 string without build metadata. The schema regexes screen edges and control characters only;
implementations MUST enforce the full text profile in code and MUST reject rather than repair.

## 2. Canonical JSON (`rem-canonical-json-v1`)

Every request body, success body, problem body, MQTT payload, MQTT envelope, roster record, and `operation_digest`
input is canonical JSON. Receivers MUST reject a non-canonical body with `invalid_request`; the digest and signature
are therefore over the only possible byte form.

- UTF-8 without BOM; no insignificant whitespace; no duplicate member names; nesting depth at most 8.
- Object members sorted by the byte order of their (ASCII) names. This coincides with RFC 8785 for this profile.
- Strings: `"` and `\` escaped as `\"` and `\\`; U+0008, U+0009, U+000A, U+000C, U+000D as `\b \t \n \f \r`; other
  code points below U+0020 as `\u00xx` with lowercase hex; everything else literal UTF-8. (Every field this profile
  defines forbids control characters, so the escapes exist only to make the rule total.)
- Integers in canonical decimal. `true`, `false`, `null` literal.
- Non-integer numbers occur only where a schema says `x-rem-number-grammar`:
  - `rem-decimal-3` (`watts`): `0|[1-9][0-9]*` optionally followed by `.` and 1–3 digits whose last digit is not `0`.
    A whole value has no fraction (`12`, never `12.0`). Range 0–100000.
  - `rem-decimal-6` (`total_kwh`): the same grammar with 1–6 fraction digits.
  - NaN, infinities, exponents, a leading `+`, and `-0` are rejected.

`operation_digest` (reporter actions) is `SHA-256(canonical JSON of the request body with one added member
"purpose":"rem-control-operation-v1")`; see vector `common.operation-digest`.

## 3. Common transcript encoding (`REM-TRANSCRIPT`)

All commitments, proofs, transcript digests, signed transcripts, and MQTT AAD use one length-delimited binary encoding:

```text
encode(purpose, fields) =
    "REM-TRANSCRIPT"                      14 ASCII bytes
 || 0x01                                  encoding version
 || uint16be(len(purpose)) || purpose     ASCII
 || for each (name, value) in order:
        uint16be(len(name)) || name       ASCII
     || uint32be(len(value)) || value     raw bytes
```

Value rules: text is exact ASCII (or NFC UTF-8 where a field permits Unicode); integers are fixed-width unsigned
big-endian (`u16`, `u64`); values declared as `bytes[n]` in a field table use decoded raw bytes, while values declared
as ASCII retain their exact text (notably MQTT AAD `reporter_id`, which binds the same canonical text used in its
derived topic); JWKs are the exact RFC 7638 canonical bytes (section 5); `schema_version` is the single byte `0x01`;
an absent optional value (MQTT AAD only) is the zero-length value. A decoder MUST reject duplicate, omitted,
reordered, unknown, overlong or non-canonical fields. Pairing, recovery, rotation, enrollment and provisioning
transcripts are capped at 4096 bytes.

### 3.1 Transcript field tables

Type notation: `b1` the byte `0x01`; `ascii(literal)`; `bytes[n]` exactly *n* raw bytes; `bytes[≤n]`; `u16`; `u64`.

**Interactive commitment** — `encode("rem-interactive-commit-v1", …)` uses these fields in order:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `role` | ascii(`reporter`\|`core`) |
| 2 | `nonce` | bytes[32] |

Commitment = SHA-256 of the encoding.

**Interactive SAS transcript** — `encode("rem-interactive-enroll-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-interactive-enroll-v1`) |
| 3 | `instance_id` | identifier, 1–128 bytes |
| 4 | `server_identity` | serverIdentity ASCII, ≤ 267 bytes |
| 5 | `trust_mode` | ascii(`ca-spki-sha256`\|`platform-pki`) |
| 6 | `trust_anchor_spki_sha256` | bytes[32] for `ca-spki-sha256`; empty for `platform-pki` |
| 7 | `proposed_reporter_jwk` | RFC 7638 canonical bytes, ≤ 256 |
| 8 | `proposed_reporter_thumbprint` | bytes[32] |
| 9 | `reporter_commitment` | bytes[32] |
| 10 | `core_commitment` | bytes[32] |
| 11 | `reporter_nonce` | bytes[32] |
| 12 | `core_nonce` | bytes[32] |
| 13 | `expires_at` | u64, ≤ creation + 600 |

`transcript_digest = SHA-256(transcript)`. SAS bytes `b[0..5] = HKDF-SHA-256(IKM = reporter_nonce || core_nonce,
salt = transcript_digest, info = "rem-interactive-enroll-sas-v1", L = 6)`.

**Managed enrollment transcript** — `encode("rem-managed-enrollment-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-managed-enrollment-v1`) |
| 3 | `token_id` | bytes[16] |
| 4 | `opaque_challenge` | bytes[≤256] |
| 5 | `core_nonce` | bytes[32] |
| 6 | `reporter_nonce` | bytes[32] |
| 7 | `scheme` | ascii(`https`) |
| 8 | `host` | dnsHost ASCII |
| 9 | `port` | u16 |
| 10 | `trust_mode` | ASCII |
| 11 | `ca_spki_sha256` | bytes[32], or empty for `platform-pki` |
| 12 | `proposed_jwk` | RFC 7638 canonical bytes, ≤ 256 |
| 13 | `proposed_thumbprint` | bytes[32] |
| 14 | `expires_at` | u64, the challenge expiry |

Then `token_verifier = SHA-256(token)`, `proof_key = HKDF-SHA-256(IKM = token_verifier, salt = core_nonce, info =
"rem-managed-enroll-proof-v1", L = 32)`, `proof = HMAC-SHA-256(proof_key, transcript)`, and `proposed_signature` =
section 6 signature of the transcript by the proposed key. The `opaque_challenge` layout is private to the core; the
reference core uses `0x01 || core_nonce || u64 expires_at || HMAC-SHA-256(core_challenge_key, …)` and the vector's MAC
bytes are a fixture.

**Control pairing credential** — `control_pairing_credential` is the exact ASCII `remcp1.<expires_at>.<secret>`
(`expires_at` canonical decimal ≤ generation + 600; `secret` b64u32). `control_pairing_key` is only the 32 bytes
decoded from `secret`. `consumed_credential_digest = SHA-256(control_pairing_credential)` over all ASCII bytes. These
three values are distinct and the vector `bootstrap.commit-reveal-confirm` fixes each one.

**Control pairing** — commitments are
`HMAC-SHA-256(control_pairing_key, encode("rem-control-pairing-commit-v1", …))`, with these encoded fields:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `role` | ascii(`integration`\|`core`) |
| 2 | `nonce` | bytes[32] |

The pairing transcript is `encode("rem-control-pairing-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-control-pairing-v1`) |
| 3 | `instance_id` | identifier |
| 4 | `control_spki_sha256` | bytes[32], the handshake-observed leaf |
| 5 | `integration_jwk` | RFC 7638 canonical bytes, ≤ 256 |
| 6 | `integration_thumbprint` | bytes[32] |
| 7 | `integration_commitment` | bytes[32] |
| 8 | `core_commitment` | bytes[32] |
| 9 | `integration_nonce` | bytes[32] |
| 10 | `core_nonce` | bytes[32] |
| 11 | `expires_at` | u64 |

`pairing_digest = SHA-256(transcript)`. Proofs are
`HMAC-SHA-256(control_pairing_key, encode("rem-control-pairing-proof-v1", …))`, with these encoded fields:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `role` | ascii(`integration`\|`core`) |
| 2 | `pairing_digest` | bytes[32] |

**Control recovery** — commitments use `rem-control-recovery-commit-v1`, proofs use
`rem-control-recovery-proof-v1` over `recovery_digest`, and both are keyed by the second fresh credential's
`control_pairing_key`. The transcript is `encode("rem-control-recovery-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-control-recovery-v1`) |
| 3 | `instance_id` | identifier |
| 4 | `current_control_key_thumbprint` | bytes[32] |
| 5 | `current_control_key_generation` | u64 |
| 6 | `current_control_spki_sha256` | bytes[32] |
| 7 | `current_control_spki_generation` | u64 |
| 8 | `observed_control_spki_sha256` | bytes[32] |
| 9 | `revocation_scope` | ascii(`control-key`\|`control-spki`\|`control-key-and-spki`) |
| 10 | `integration_commitment` | bytes[32] |
| 11 | `core_commitment` | bytes[32] |
| 12 | `integration_nonce` | bytes[32] |
| 13 | `core_nonce` | bytes[32] |
| 14 | `expires_at` | u64 |
| 15 | `recovery_generation` | u64 |

**Reporter-key rotation** — `encode("rem-reporter-key-rotation-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-reporter-key-rotation-v1`) |
| 3 | `reporter_id` | bytes[32], decoded |
| 4 | `current_thumbprint` | bytes[32] |
| 5 | `current_generation` | u64 |
| 6 | `proposed_jwk` | RFC 7638 canonical bytes |
| 7 | `proposed_thumbprint` | bytes[32] |
| 8 | `next_generation` | u64, = current + 1 with no wrap |
| 9 | `operation_id` | bytes[16] |
| 10 | `rotation_id` | bytes[16] |
| 11 | `core_nonce` | bytes[32] |
| 12 | `expires_at` | u64 |

`rotation_digest = SHA-256(transcript)`. `current_signature` and `proposed_signature` are section 6 signatures of the
transcript.

**Control-key rotation** — `encode("rem-control-key-rotation-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-control-key-rotation-v1`) |
| 3 | `instance_id` | identifier |
| 4 | `current_jwk` | RFC 7638 canonical bytes |
| 5 | `current_thumbprint` | bytes[32] |
| 6 | `current_generation` | u64 |
| 7 | `proposed_jwk` | RFC 7638 canonical bytes |
| 8 | `proposed_thumbprint` | bytes[32] |
| 9 | `next_generation` | u64 |
| 10 | `operation_id` | bytes[16] |
| 11 | `rotation_id` | bytes[16] |
| 12 | `core_nonce` | bytes[32] |
| 13 | `expires_at` | u64 |

`rotation_digest = SHA-256(transcript)`.

**Control-SPKI rotation** — `encode("rem-control-spki-rotation-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-control-spki-rotation-v1`) |
| 3 | `instance_id` | identifier |
| 4 | `operation_id` | bytes[16], the propose operation |
| 5 | `transition_id` | bytes[16] |
| 6 | `old_spki_sha256` | bytes[32] |
| 7 | `new_spki_sha256` | bytes[32] |
| 8 | `current_spki_generation` | u64 |
| 9 | `next_spki_generation` | u64 |
| 10 | `integration_nonce` | bytes[32] |
| 11 | `core_nonce` | bytes[32] |
| 12 | `expires_at` | u64, ≤ creation + 600, the maximum overlap |

`transition_digest = SHA-256(transcript)`. The old and the new control TLS private keys each sign the transcript
(section 6).

**MQTT key provisioning** — `encode("rem-mqtt-key-provision-v1", …)`, ≤ 4096 bytes:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-mqtt-key-provision-v1`) |
| 3 | `instance_id` | identifier |
| 4 | `operation_id` | bytes[16] |
| 5 | `key_id` | bytes[16] |
| 6 | `key_generation` | u64 |
| 7 | `previous_key_generation` | u64, 0 when none |
| 8 | `key_sha256` | bytes[32], SHA-256 of the exact 32 key bytes |
| 9 | `nonce_prefix` | bytes[16] |
| 10 | `integration_nonce` | bytes[32] |
| 11 | `core_nonce` | bytes[32] |
| 12 | `expires_at` | u64, ≤ the signed request expiry |

`provision_digest = SHA-256(transcript)`. Key confirmation proves possession of the exact key. It is
`HMAC-SHA-256(key, encode("rem-mqtt-key-confirm-v1", …))`, with these encoded fields:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `role` | ascii(`integration`\|`core`) |
| 2 | `provision_digest` | bytes[32] |

The integration sends the confirmation in the acknowledgement, and the core returns it in its response.

**MQTT AAD** — `encode("rem-mqtt-v2-aad-v1", …)`:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `protocol_version` | byte `0x02` |
| 2 | `topic` | exact ASCII topic derived by the receiver |
| 3 | `reporter_id` | ASCII 43-character reporter ID, or empty for the process topic |
| 4 | `message_type` | ASCII |
| 5 | `payload_schema_version` | b1 |
| 6 | `key_id` | bytes[16] from the durable key record |
| 7 | `key_generation` | u64 from the durable key record |
| 8 | `process_generation` | u64 |
| 9 | `state_generation` | u64 or empty |
| 10 | `stream_sequence` | u64 |
| 11 | `issued_at` | u64 |
| 12 | `expires_at` | u64 or empty, offline Will only |
| 13 | `nonce` | bytes[24] |

**Roster snapshot completeness** — each `r` is a canonical JSON record, and
`records_sha256 = SHA-256(concat over ordered records of uint32be(len(r)) || r)`. The completeness transcript is
`encode("rem-roster-snapshot-v1", …)`, with these fields:

| # | Field | Type |
| ---: | --- | --- |
| 1 | `schema_version` | b1 |
| 2 | `purpose` | ascii(`rem-roster-snapshot-v1`) |
| 3 | `snapshot_id` | bytes[16] |
| 4 | `snapshot_generation` | u64 |
| 5 | `total_count` | u64 |
| 6 | `records_sha256` | bytes[32] |

`completeness_digest = SHA-256(transcript)`.

## 4. Commit-before-reveal, SAS indexes, and future rendering

Each side MUST fix its commitment before either nonce is revealed. The proposing side (reporter or integration) reveals
first; the core MUST verify that reveal against the stored commitment and discard the pending state on mismatch
**before** revealing its own nonce; the proposing side MUST then verify the core's commitment before any proof,
confirmation or trust persistence. Proofs and commitments are compared in constant time.

After task 3.3 vendors the normative tables, implementations MUST render the six SAS words as
`Even[b[0]] Odd[b[1]] Even[b[2]] Odd[b[3]] Even[b[4]] Odd[b[5]]`, joined by single U+0020 spaces, using the exact
256-entry **Even Word List** and **Odd Word List** of the PGP Word List referenced by RFC 6189 §5.1.6
(`[pgpwordlist]`), zero-based, ASCII spelling and case preserved. RFC 6189 renders only two words (16 bits); this
profile deliberately extends that rendering to six bytes and six words and does not claim RFC 6189 conformance.

**Task 3.3 boundary and provenance.** This repository does not yet vendor the PGP Word List. The sole normative
lineage is the **PGP Word List** referenced by [RFC 6189 section 5.1.6](https://www.rfc-editor.org/rfc/rfc6189#section-5.1.6):
that RFC's `[pgpwordlist]` citation fixes the 2010-12 source as the Wikipedia revision with
`oldid=400752943`. Task 3.3 MUST preserve an immutable copy of that cited revision and record, beside the extracted
lists:

1. the exact source URL including `oldid`, retrieval UTC timestamp, media type, and SHA-256 of the unmodified source
   bytes;
2. the source's authorship/licensing notice and the repository's redistribution determination, without silently
   treating a later Wikipedia revision or another implementation as authoritative;
3. the reviewed extraction procedure and SHA-256 of each final newline-terminated ASCII data file;
4. proof that each file has exactly 256 unique non-empty entries in zero-based order, preserves spelling and case,
   contains ASCII only, and has no leading/trailing whitespace; and
5. an independent comparison against a second published implementation used only as a cross-check. Any disagreement
   blocks vendoring until resolved against the RFC-cited revision.

Until those requirements are met and task 3.3 pins and vendors both tables as normative data, the vector field
`sas.words` uses the encoding `sas-placeholder` and the text `EVEN[i] ODD[j] …`. That text is **not** a rendered phrase
and MUST NOT be shown to a user or accepted by an implementation. The transcript, digest, HKDF output bytes,
per-position list selection and indexes are normative now; task 3.3 adds only the word strings and cross-language
rendering tests. No implementation may ship SAS confirmation before task 3.3.

## 5. Keys and identifiers

- Key thumbprint: RFC 7638 SHA-256 over the canonical JWK bytes `{"crv":"P-256","kty":"EC","x":"…","y":"…"}` (members
  in that order, no whitespace). `reporter_id` is the unpadded base64url thumbprint of the **initial** enrollment key
  and never changes; after rotation, `keyid` names the active key and the core maps it to the reporter.
- Integration control keys: `keyid` is the active control key's thumbprint; `REM-Control-Key-Generation` is its
  durable generation.
- `key_id` (MQTT) is a random 16-byte value carried only as canonical b64u16; its durable record maps it one-to-one to
  a `key_generation`, key, and `nonce_prefix`.
- Every random value (`nonce`, `*_id`, keys, tokens, prefixes) MUST come from the platform CSPRNG. Vector values are
  repeated-byte fixtures chosen to be recognizable; they are public and MUST NOT be used outside tests.

## 6. ECDSA signature encoding

Every signature in this profile is ECDSA over P-256 with SHA-256 of the exact signed bytes, encoded as the 64-byte
**IEEE P1363** concatenation `r || s` (each 32-byte big-endian, left-padded). Signers MUST emit **low-S**
(`s ≤ n/2` with `n` the P-256 group order) and verifiers MUST reject `s > n/2`, `r` or `s` equal to 0 or ≥ `n`, and any
length other than 64. In JSON bodies the value is canonical b64u (86 characters); in the RFC 9421 `Signature` header
it is an RFC 8941 byte sequence. DER signatures are never accepted. Every HTTP signature vector records the high-S
twin of its signature (`signature.<label>.high-s`), and the managed-enrollment and rotation vectors substitute a
high-S twin for one body signature; accepting any twin is a conformance failure.

## 7. Content-Digest

Body routes MUST send exactly one RFC 9530 `Content-Digest` member, `sha-256=:<standard padded base64 of
SHA-256(body)>:`, over the exact received body bytes, at most 256 header bytes, with `Content-Type:
application/json` exactly (no parameters). Other algorithms, multiple members, `Repr-Digest`, `Content-Encoding`, and
compressed or chunk-extended bodies are rejected. Bodyless routes MUST NOT send `Content-Type`, `Content-Digest`,
`Content-Length` other than `0`, or `Transfer-Encoding`.

## 8. RFC 9421 signature profiles

Both profiles use exactly one signature label `rem` (plus `rem-proposed` only on
`control.control-key-rotation.complete`), algorithm `ecdsa-p256-sha256`, and the signature encoding of section 6. The
`Signature-Input` and `Signature` headers each appear once, at most 1024 bytes, and contain only the permitted labels in
the order `rem`, then `rem-proposed`.

### 8.1 Signature base

The base is built exactly as RFC 9421 §2.5: one line per covered component, `"<name>": <value>` (or
`"@query-param";name="<n>": <value>`), joined by a single LF (0x0A), followed by the final line
`"@signature-params": <inner list and parameters>` with no trailing LF. Component values:

| Component | Value |
| --- | --- |
| `@method` | Uppercase method, exactly as in `routes.json` |
| `@authority` | Lowercase A-label host, `:`, explicit port, equal to both the received authority and the configured identity (reporter: the enrolled server identity without scheme; control: the approved instance control URL) |
| `@path` | The received path, byte-exact; it MUST match the route template with no decoding, dot segments, duplicate slashes or trailing slash |
| `@query` | `?` followed by the exact received query, or `?` alone when the route defines no query |
| `@query-param;name=…` | The named query value, byte-exact (no percent decoding is ever needed because percent encoding is rejected) |
| header components | The single header value with leading and trailing whitespace removed |

### 8.2 `reporter-rfc9421-v1`

Covered components, in this order: `@method`, `@authority`, `@path`, `@query`, `rem-purpose`; then route extras
(`rem-session-id`, `rem-sequence` on `POST /api/v1/reports`; `"@query-param";name="after_status_generation"`,
`"@query-param";name="wait_seconds"` on enrollment status); then `content-type`, `content-digest` on body routes.

Signature parameters, exactly once and in this order: `keyid`, `alg`, `tag`. `keyid` is the RFC 7638 thumbprint of
the signing key (the proposed key on enrollment routes, the active key otherwise); `tag` equals the `REM-Purpose`
header and the route purpose (`rem-enroll-v1` for interactive and `rem-managed-enroll-v1` for managed enrollment).
A reporter MUST NOT transmit `created` or any other client wall-clock timestamp. Reporter freshness and replay
protection come exclusively from server-issued state: pending enrollment, the single-use challenge, the session, and
the strictly increasing `REM-Sequence`.

Headers: `REM-Purpose` (route tag), `REM-Session-Id` (b64u16), `REM-Sequence` (canonical uint64).

### 8.3 `control-rfc9421-v1`

Covered components, in this order: `@method`, `@authority`, `@path`, `@query`, `rem-purpose`,
`rem-control-key-generation`, `rem-request-nonce`, `rem-request-expires`; then, on the roster routes, every named query
input in wire order; then `content-type`, `content-digest` on body routes.

Signature parameters, exactly once and in this order: `created`, `expires`, `nonce`, `keyid`, `alg`, `tag`.
`nonce` equals `REM-Request-Nonce` (b64u16, fresh random 128 bits); `expires` equals `REM-Request-Expires`; `tag`
equals `REM-Purpose`; `keyid` is the active (or, for `rem-proposed`, the proposed) control key's thumbprint. At server
receipt time `created ≤ now + 30`, `created + 1 ≤ expires ≤ created + 60`, and `now ≤ expires`. The nonce is recorded
durably through `expires`. Query forms are exact:

```text
/control/v1/roster/events?after_generation=<uint64>&wait_seconds=<0..25>
/control/v1/roster/snapshot?after_generation=<uint64>
/control/v1/roster/snapshot?continuation_token=<opaque128>
```

Unknown, duplicate, reordered, omitted, empty, percent-encoded or fragment-bearing query input is rejected, and a
route without a query form rejects any `?`.

### 8.4 Header serialization

```text
Reporter Signature-Input: rem=("@method" "@authority" … "content-digest");keyid="…";alg="ecdsa-p256-sha256";tag="…"
Control Signature-Input: rem=("@method" "@authority" … "content-digest");created=…;expires=…;nonce="…";keyid="…";alg="ecdsa-p256-sha256";tag="…"
Signature: rem=:<standard base64 of the 64-byte P1363 signature>:
```

With `rem-proposed`, each header holds two dictionary members separated by `, ` in the order `rem`, `rem-proposed`;
the inner lists and parameters are identical except `keyid`. The vectors fix every byte of both headers.

## 9. Admission and verification order

Implementations MUST apply checks in this order and return the first matching code, so that the vectors' expected
codes are deterministic:

1. Transport admission uses the processing phase to select one deterministic outcome:
   - A connection-wide or per-source connection-admission limit MUST reject the connection without an HTTP response.
   - A TLS-handshake limit MUST terminate the handshake without an HTTP response.
   - After the TLS handshake succeeds, an exhausted parser, worker, queue or global request-capacity limit MUST return
     `overloaded`.
2. Request target over 256 bytes → `target_too_long`; target grammar, percent encoding, fragment, a query on a
   no-query route, or a non-canonical query form → `invalid_request`.
3. Unknown path → `not_found`; known path with another method → `method_not_allowed`.
4. More than 24 headers or 8 KiB of headers, or an oversized signature/digest header → `headers_too_large`.
5. Pre-authentication source/global rate limits → `rate_limited`.
6. Body over the route cap (counted while streaming) → `request_body_too_large`; wrong or parameterized
   `Content-Type`, or any `Content-Encoding` → `unsupported_media_type`.
7. Missing, duplicate or malformed REM header, malformed or duplicate `Content-Digest`, digest mismatch,
   non-canonical JSON, an invalid enrollment body `mode`, or another schema violation → `invalid_request`.
8. Missing or duplicate signature header, a label, component list, component order or parameter set that differs
   from the profile, a parameter that differs from its header, `alg` other than `ecdsa-p256-sha256`, an unknown or
   superseded `keyid`, a stale `REM-Control-Key-Generation`, an allowed enrollment purpose that does not match the
   authenticated body `mode`, freshness outside section 8.3, signature verification failure, or a high-S or non-64-byte
   signature → `authentication_required`. (Route-specific exact-retry lookups that the route manifest permits run
   immediately before the active-key/generation check.)
9. Replayed control nonce outside the route's exact-retry rule → `authentication_required`.
10. Authenticated identity lacks authority for the resource → `authorization_denied`.
11. Business state: conflicts → `conflict`; roster cursor gaps → `snapshot_required`; stale snapshot tokens →
    `stale_snapshot`; compacted reporter-action results → `operation_result_expired`.

Cryptographic proof failures inside bodies (commitments, HMAC proofs, managed proofs, P1363 transcript signatures, key
confirmations) also return `authentication_required` and, where the specification requires, discard pending state.

## 10. Routes

`routes.json` is authoritative. Byte caps count body bytes only; `none` forbids a body. Every problem response fits
2048 bytes including headers.

| ID | Trust state | Method and target | Request | Success body | Status | Purpose label | Signature |
| --- | --- | --- | ---: | ---: | --- | --- | --- |
| `reporter.managed-enrollment-challenge.create` | pre-enrollment | `POST /api/v1/managed-enrollment-challenges` | 1 KiB | 1 KiB | 201 | `rem-managed-enroll-challenge-v1` | none |
| `reporter.enrollment.create` | pre-enrollment | `POST /api/v1/enrollments` | 4 KiB | 2 KiB | 201 | `rem-enroll-v1 rem-managed-enroll-v1` | reporter-rfc9421-v1 |
| `reporter.enrollment.reveal` | pre-enrollment | `POST /api/v1/enrollments/{enrollment_id}/reveal` | 256 B | 2 KiB | 200 | `rem-enroll-reveal-v1` | reporter-rfc9421-v1 |
| `reporter.enrollment.confirm` | pre-enrollment | `POST /api/v1/enrollments/{enrollment_id}/confirm` | 512 B | 1 KiB | 200 | `rem-enroll-confirm-v1` | reporter-rfc9421-v1 |
| `reporter.enrollment.status` | pre-enrollment | `GET /api/v1/enrollments/{enrollment_id}` `?after_status_generation={uint64}&wait_seconds={0..25}` | none | 2 KiB | 200 | `rem-enroll-status-v1` | reporter-rfc9421-v1 |
| `reporter.challenge-context.read` | server-authenticated | `GET /api/v1/challenge-context` | none | 1 KiB | 200 | `rem-challenge-context-v1` | none |
| `reporter.challenge.create` | enrolled-reporter | `POST /api/v1/challenges` | 512 B | 1 KiB | 201 | `rem-challenge-v1` | reporter-rfc9421-v1 |
| `reporter.session.create` | enrolled-reporter | `POST /api/v1/sessions` | 1 KiB | 1 KiB | 201 | `rem-session-v1` | reporter-rfc9421-v1 |
| `reporter.report.create` | enrolled-reporter | `POST /api/v1/reports` | 1 KiB | 1 KiB | 200 | `rem-report-v1` | reporter-rfc9421-v1 |
| `reporter.key-rotation-challenge.create` | enrolled-reporter | `POST /api/v1/reporters/{reporter_id}/key-rotations/challenges` | 2 KiB | 2 KiB | 201 | `rem-rotate-challenge-v1` | reporter-rfc9421-v1 |
| `reporter.key-rotation.complete` | enrolled-reporter | `POST /api/v1/reporters/{reporter_id}/key-rotations/{rotation_id}` | 1 KiB | 2 KiB | 202 | `rem-rotate-v1` | reporter-rfc9421-v1 |
| `control.bootstrap.commit` | provisional-control | `POST /control/v1/bootstrap/commit` | 2 KiB | 2 KiB | 201 | `rem-control-pairing-v1` | none |
| `control.bootstrap.reveal` | provisional-control | `POST /control/v1/bootstrap/reveal` | 1 KiB | 2 KiB | 200 | `rem-control-pairing-v1` | none |
| `control.bootstrap.confirm` | provisional-control | `POST /control/v1/bootstrap/confirm` | 1 KiB | 2 KiB | 200 | `rem-control-pairing-v1` | none |
| `control.recovery.commit` | provisional-control | `POST /control/v1/recovery/commit` | 1 KiB | 2 KiB | 201 | `rem-control-recovery-v1` | none |
| `control.recovery.reveal` | provisional-control | `POST /control/v1/recovery/reveal` | 256 B | 1 KiB | 200 | `rem-control-recovery-v1` | none |
| `control.recovery.revoke` | provisional-control | `POST /control/v1/recovery/revoke` | 1 KiB | 2 KiB | 200 | `rem-control-recovery-v1` | none |
| `control.managed-token.issue` | post-pair | `POST /control/v1/managed-enrollment-tokens` | 1 KiB | 2 KiB | 201 | `rem-control-managed-token-v1` | control-rfc9421-v1 |
| `control.managed-token.status` | post-pair | `POST /control/v1/managed-enrollment-token-operations/status` | 512 B | 1 KiB | 200 | `rem-control-managed-token-status-v1` | control-rfc9421-v1 |
| `control.managed-token.revoke` | post-pair | `POST /control/v1/managed-enrollment-tokens/revoke` | 512 B | 1 KiB | 200 | `rem-control-managed-token-revoke-v1` | control-rfc9421-v1 |
| `control.roster.events` | post-pair | `GET /control/v1/roster/events` `?after_generation={uint64}&wait_seconds={0..25}` | none | 64 KiB, 100 items | 200 | `rem-control-roster-events-v1` | control-rfc9421-v1 |
| `control.roster.snapshot` | post-pair | `GET /control/v1/roster/snapshot` `?after_generation={uint64}` or `?continuation_token={opaque128}` | none | 64 KiB, 100 items | 200 | `rem-control-roster-snapshot-v1` | control-rfc9421-v1 |
| `control.reporter.action` | post-pair | `POST /control/v1/reporters/actions` | 2 KiB | 2 KiB | 200 | `rem-control-reporter-action-v1` | control-rfc9421-v1 |
| `control.reporter-key-rotation.confirm` | post-pair | `POST /control/v1/reporters/key-rotations/confirm` | 1 KiB | 2 KiB | 200 | `rem-control-reporter-key-rotation-v1` | control-rfc9421-v1 |
| `control.control-key-rotation-challenge.create` | post-pair | `POST /control/v1/control-keys/rotation-challenges` | 2 KiB | 2 KiB | 201 | `rem-control-key-rotation-challenge-v1` | control-rfc9421-v1 |
| `control.control-key-rotation.complete` | post-pair | `POST /control/v1/control-keys/rotations` | 4 KiB | 2 KiB | 200 | `rem-control-key-rotation-v1` | control-rfc9421-v1 |
| `control.control-spki-rotation` | post-pair | `POST /control/v1/control-spki/rotations` | 4 KiB | 2 KiB | 200 | `rem-control-spki-rotation-v1` | control-rfc9421-v1 |
| `control.mqtt-key.provision` | post-pair | `POST /control/v1/mqtt-keys/provision` | 4 KiB | 4 KiB | 200 | `rem-control-mqtt-key-provision-v1` | control-rfc9421-v1 |
| `control.mqtt-key.acknowledge` | post-pair | `POST /control/v1/mqtt-keys/acknowledge` | 1 KiB | 2 KiB | 200 | `rem-control-mqtt-key-ack-v1` | control-rfc9421-v1 |
| `control.lease.refresh` | post-pair | `POST /control/v1/leases/refresh` | 512 B | 1 KiB | 200 | `rem-control-lease-refresh-v1` | control-rfc9421-v1 |

Trust states: **pre-enrollment** — transport depends on the enrollment mode. Interactive encryption-only provisional
TLS admits exactly four reporter route IDs: `reporter.enrollment.create` only when its body selects interactive mode,
`reporter.enrollment.reveal`, `reporter.enrollment.confirm`, and `reporter.enrollment.status`; its dispatcher rejects
all other reporter routes. Authenticated managed first contact is separate: the reporter MUST verify the
artifact-selected CA-SPKI pin or platform-PKI identity before `reporter.managed-enrollment-challenge.create`, and that
verified transport remains mandatory for the managed branch of `reporter.enrollment.create`.
**server-authenticated** — the reporter verified the server per its enrolled trust mode; no request signature.
**enrolled-reporter** — verified server TLS plus `reporter-rfc9421-v1` under the active key.
**provisional-control** — TLS 1.3 encryption only while a fresh credential is active, a dispatcher that admits only the
three bootstrap or the three recovery routes and emits only their success schema or a problem, and HMAC proofs over the
observed SPKI. **post-pair** — exact pinned control SPKI plus `control-rfc9421-v1`.

Response headers: the managed-enrollment artifact and the MQTT provisioning response carry `Cache-Control: no-store`
(`x-rem-response-headers` in the schema); every response is `application/json` or `application/problem+json`.

## 11. Problem responses (RFC 9457)

Every error is `Content-Type: application/problem+json` with exactly the members `type`, `title`, `status`, `code`
(`schemas/problem.schema.json`). `code` is one of the 17 closed values, `status` equals both the HTTP status and the
fixed mapping, `type` is `https://remote-energy-meter.example/protocol/v1/problems/<code>`, and `title` is the fixed
title for that code. No member, including a `detail`, ever carries request input, identifiers or secrets. The
complete response, status line and headers included, is at most 2048 bytes (vector `common.problem-response`).

## 12. MQTT v2 envelope

Topics are derived by the receiver from durable state, never read from the payload:

| `message_type` | Topic | `state_generation` | `expires_at` |
| --- | --- | --- | --- |
| `process-availability` | `<base>/v2/meta/process-availability` | null | online ≤ issued + 90; offline Will null |
| `device-config` | `<base>/v2/devices/<reporter_id>/config` | required | ≤ issued + 86400, refreshed every 12 h |
| `device-power` | `<base>/v2/devices/<reporter_id>/power/state` | required | ≤ issued + 90 |
| `device-energy` | `<base>/v2/devices/<reporter_id>/energy/state` | required | ≤ issued + 90 |
| `device-availability` | `<base>/v2/devices/<reporter_id>/availability` | required | ≤ issued + 90 |

The retained payload is the canonical JSON envelope in `schemas/mqtt-v2.schema.json` (at most 1024 bytes). The AEAD is
**XChaCha20-Poly1305** (IETF construction: HChaCha20 subkey, 24-byte nonce, Poly1305 tag) with the 32-byte provisioned
key:

```text
nonce      = nonce_prefix (16 bytes, from the key record) || uint64be(publication_counter)
aad        = encode("rem-mqtt-v2-aad-v1", section 3.1 fields)
ciphertext = XChaCha20-Poly1305-Encrypt(key, nonce, aad, canonical payload JSON) || 16-byte tag
envelope.ciphertext = b64u(ciphertext)
```

The core reserves `publication_counter` ranges durably before use and never reuses a counter under a key. The
integration's acceptance order is: canonical envelope JSON; canonical `key_id`; durable key-record lookup and exact
`key_generation` match (unknown ID, mismatch, padded or alternate encoding, or rollback rejected); topic/type/reporter
derivation; AAD construction from the record and the derived values only; AEAD verification; then freshness
(`issued_at` no more than 30 s ahead of receipt; `expires_at` not reached), process gate, control lease, and
high-water checks on process generation, state generation, and per-stream sequence. An exact duplicate of the last
accepted envelope is a no-op; the same sequence with different bytes is rejected. The offline Will carries no expiry,
may only withdraw availability, and is rejected below the durable process-generation high-water mark.

## 13. Vector conformance semantics

See `README.md` for the vector file format. The Python Task 3.2 conformance implementation reproduces the primitive
layer's `outputs` values from the recorded `inputs`, matches the relevant `intermediates`, and executes that layer's
mutations against the runtime. Each mutation changes exactly one named field while leaving everything else, including
any signature or proof computed over the original, unchanged; the implementation rejects it with `expected_code`.
The structural conformance test validates every declaration. Rust and cross-language execution, AEAD, stateful replay,
route-schema outcomes, and business-state mutations remain assigned to later work.

## 14. Source of truth, versioning, and checksums

This repository (`superkus/remote_energy_meter`, `docs/protocol/v1/`) is the single source of truth for profile
version 1. Once the reporter repository implements this profile, it MUST consume these files byte-for-byte at a pinned
commit, verify `vectors/manifest.json` before running conformance tests, and never edit the files there.

- `vectors/manifest.json` lists every vector file with its SHA-256 and case IDs, and every normative contract file
  (`routes.json`, each schema, and this document) with its SHA-256. Once a consumer exists, a checksum mismatch MUST
  fail conformance.
- Any change to a vector or contract file updates its checksum in the same commit. Checksums are computed last, after
  every other file in the change is final.
- Profile version 1 is append-only once task 3.4 approves it: new cases may be added, but no published byte, field,
  label, cap, status, or code may change. Any incompatible change is profile version 2 under `docs/protocol/v2/`,
  with new purpose labels so that no v1 signature, proof or transcript can verify under v2.
- Before approval, corrections are allowed and are recorded in git history and the review record.

## 15. Profile decisions beyond the specification

These details were not fixed by the specification; this profile fixes them, and task 3.4's review covers each one.

1. **Canonical JSON bodies are mandatory** (section 2) so "canonical body bytes" equal received bytes; `watts` and
   `total_kwh` use bounded decimal grammars (milliwatt and microkilowatt-hour resolution).
2. **Problem objects have no `detail` member** (the design allowed an optional one): a closed four-member object
   cannot leak input and keeps every schema closed.
3. **Per-route reporter tags** `rem-enroll-reveal-v1`, `rem-enroll-confirm-v1`, `rem-enroll-status-v1`; all are signed
   by the proposed key so a third party cannot poll, reveal into, or confirm someone else's pending enrollment, and no
   signature can be reused across routes.
4. **Reporter signature parameters** are `keyid`, `alg`, `tag` only. Reporters send no client wall-clock timestamp;
   freshness is derived exclusively from server-issued state.
5. **Enrollment status long-poll** uses the exact query `after_status_generation=<uint64>&wait_seconds=<0..25>`,
   mirroring roster events.
6. **Device metadata** is sent only in the signed session-creation body, after trust is established, satisfying
   requirement 2.5; pending records carry `metadata: null`.
7. **Managed challenge request** carries token ID, server identity and proposed JWK/thumbprint but no reporter nonce:
   requirement 4.5 has the reporter generate its nonce after receiving the challenge, and that ordering is adopted over
   the design table's purpose summary.
8. **Control-SPKI rotation** uses one route with a `phase` discriminator (`propose`, then `confirm`) and the
   transcript in section 3.1; confirm is accepted only on a connection presenting the new SPKI.
9. **MQTT provisioning** uses the request fields `operation_id`, `integration_nonce`, `previous_key_generation`, and
   key confirmation HMACs under the new key in both directions of the acknowledgement.
10. **Pending HA cards** receive `sas_bytes` (the six HKDF bytes), not rendered words; the integration renders them
    with the same vendored tables as the reporter.
11. **Lease refresh** binds the integration's applied `roster_generation`; the core returns the granted
    `lease_expires_at` (≤ 60 s).
12. **Every route manifest `purpose`** is the exact machine label; for `reporter.enrollment.create` it lists two
    labels separated by one space, selected by the body `mode`.
