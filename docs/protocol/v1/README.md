# REM protocol v1 profile

This directory is the server-owned, versioned contract for native computer reporters, the Home Assistant
integration's control channel, and MQTT v2. It is specification task 3.1 of
`.kiro/specs/native-computer-reporters/`. Task 3.2 will implement Python and Rust primitives against these files. The
normative SAS word tables (task 3.3) and rendering tests do not exist yet; task 3.3 will vendor and implement them. Task
3.4's independent security review must approve the profile before anything that speaks it is distributed.

Status: **draft, not approved for distribution.** No listener, route handler, or production cryptographic code exists
in this repository yet.

| File | Content |
| --- | --- |
| `wire-profile.md` | Normative wire rules: encodings, canonical JSON, `REM-TRANSCRIPT`, every transcript field table, RFC 7638 / RFC 9530 / RFC 9421 / RFC 9457 profiles, P1363 low-S signatures, XChaCha20-Poly1305 MQTT envelopes, admission order, the route table, and the profile decisions beyond the specification |
| `threat-model.md` | Assets, adversaries, threats, mitigations, and residual risks |
| `routes.json` | All 30 routes: method, path template, query forms, request and success caps, item cap, success status, purpose label, schema references, signature profile, trust state, replay rule |
| `schemas/` | JSON Schema Draft 2020-12, every object closed with every property required: `common`, `problem`, `reporter-api`, `control-api`, `mqtt-v2`, `vector` |
| `vectors/` | Eight public vector files and `manifest.json`, which records SHA-256 checksums for those vectors and the normative contract files |

## Fixture policy

All vector values are **public, non-secret fixtures**. Nonces, IDs, tokens, pairing secrets and the MQTT key are
repeated-byte patterns (for example 32 × `0x11`) chosen to be recognizable; they must never be used outside tests.
The vectors publish no private signing-key members: no JSON member is named `d`, `private_jwk`, `private_key`,
`signing_key`, or `private_key_hex`, and the conformance test enforces that. Only public JWKs and, for the control TLS
keys, public SPKI DER are published.

### Provenance and verification boundary

Every value except an ECDSA signature is specified as a deterministic function of the fixture inputs recorded in the
same case, using SHA-256, HMAC-SHA-256, HKDF-SHA-256, the encodings in `wire-profile.md`, and
XChaCha20-Poly1305. Future task 3.2 implementations must recompute and verify those values. The maintainer's provenance
is that seven ephemeral P-256 key pairs were generated in memory, each signature was normalized to low-S, its high-S
twin was recorded, and the private halves were discarded without serialization. Because neither that generator nor an
audit record of the run is included, this repository cannot prove how the keys were generated or that no private copy
was retained elsewhere.

`tests/conformance/test_protocol_v1_artifacts.py` checks structure, closed-object declarations, internal references,
vector and contract checksums, canonical named-value encodings, mutation declarations, the absence of named private-key
members, and every published P-256 signature against its public key and exact signed bytes. It also proves each declared
high-S twin is mathematically related to its accepted low-S signature while requiring the mutation to be rejected. It
does not decrypt AEAD fixtures, execute the declared mutations against a protocol implementation, validate documents
with a Draft 2020-12 implementation, or reproduce the maintainer-reported generation run. Executable cryptographic and
mutation rejection remain requirements for task 3.2's reviewed Python and Rust primitives. Implementations must verify
the published signatures and reject the declared mutations; they must not treat this provenance statement as
key-destruction evidence.

## Vector format (`rem-vector-set-v1`)

Each file holds `schema`, `profile_version`, `description`, and `cases`. Each case is closed
(`schemas/vector.schema.json`):

| Member | Meaning |
| --- | --- |
| `id` | Unique across all files (`<group>.<name>`) |
| `requirements` | Specification requirement numbers the case covers |
| `routes` | Route IDs from `routes.json` the case exercises |
| `inputs` | Named values the implementation starts from |
| `intermediates` | Named values it must reproduce on the way (encoded transcript fields, digests, HKDF output, signature bases, AAD, nonces) |
| `expected` | `accept: true`, the success `status` (or `null` when the case is not one HTTP exchange), and named `outputs` |
| `substitutes` | Replacement values for every `substitute` mutation, by field |
| `mutations` | Declarations of independent one-field changes and their expected rejection classifications |

A named value is `{name, encoding, value}` with `encoding` one of `ascii`, `utf8`, `hex`, `base64url` (unpadded),
`base64` (padded), `decimal`, or `sas-placeholder`. HTTP requests are given as `request.method`,
`request.authority`, `request.target`, `headers.<lowercase-name>`, and `body` (the exact canonical bytes); signature
material as `signature-params.<label>`, `signature-base.<label>`, `signature.<label>.p1363`, and
`signature.<label>.high-s`.

### Mutation semantics

Each mutation declaration identifies one field to change while keeping everything else, including any digest, proof,
or signature computed over the original bytes. A future conformance implementation must construct the change and reject
the result with `expected_code`; the current structural test validates the declaration but does not execute it. A field
names an input, intermediate, output, header, signature parameter, JSON member (`<message>.<member>`),
transcript field (`transcript.<field>`, `aad.<field>`), or one of these pseudo-fields:

| Pseudo-field | Meaning |
| --- | --- |
| `request`, `envelope`, `issue.request` | The whole message (with `duplicate`: an exact replay) |
| `reveal.order` | The core reveals before the proposing side (with `reorder`) |
| `credential.reuse` | A consumed pairing credential presented again |
| `confirm.retry-after-success.<member>` | A changed confirm retried after the bootstrap committed |
| `confirm.connection_spki` | The SPKI of the TLS connection that carries the request |
| `request.target.path`, `request.target.path-parameter`, `request.target.query`, `request.target.query-order`, `request.target.empty-query` | The fixed path, a path parameter, a query value, the query member order, or an empty `?` appended to a no-query target |
| `signature-params.<label>.components`, `.component-list` | The covered-component inner list |
| `signature-input.<label>`, `signature-input.labels` | One dictionary member, or the member order |
| `<name>.low-s`, `signature.<label>.high-s` | The signature replaced by its high-S twin |

| Operation | Effect |
| --- | --- |
| `alter-byte` | XOR `0x01` into the last byte of the decoded value (for text, the last byte) and re-encode canonically |
| `alter-value` | Replace with the next value of the same type: integer + 1; enum or literal the next allowed value (method GET↔POST, `schema_version` 2, protocol version 3); binary XOR `0x01` into the first decoded byte; authority port + 1; an encoded transcript field's length prefix + 1; an empty value becomes one `0x00` byte |
| `duplicate` | Repeat the field once (header line, JSON member, transcript field, dictionary member) or replay the whole message where the field is the message |
| `omit` | Remove the field (for a component list, its last component; for a ciphertext tag, its last 16 bytes) |
| `reorder` | Swap the field with its successor (with its predecessor when last); for `*.field-order`, swap the first two fields after `purpose`; for `hkdf.ikm`, `core_nonce` first; for `sas.position-list`, swap Even and Odd |
| `substitute` | Replace with the case's `substitutes` entry for that field |

`expected_code` follows the admission order in `wire-profile.md` section 9. Where the rejecting party is a client
(an integration checking a core response, a reporter checking an artifact or a SAS), the code is the classification
the same violation receives at the server; for a non-conforming server output (for example a malformed problem
response) it is `internal_error`, and the client must never treat it as success.

## Updating the profile

1. Change the documents and vectors. Recompute every changed value with two independent implementations (task 3.2's
   Python and Rust primitives once they exist) and re-verify from the published bytes; never commit a private key.
2. Run `PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/conformance/test_protocol_v1_artifacts.py`.
3. Recompute every SHA-256 in `vectors/manifest.json` **last**, after all other files are final.
4. Once task 3.4 approves version 1, published bytes never change; see `wire-profile.md` section 14.
