# REM protocol v1 — threat model

Status: draft for task 3.4's independent security review. This document covers the wire contract in
`wire-profile.md` for native reporters, the Home Assistant integration's control channel, and MQTT v2. It does not
cover the existing polled devices or MQTT v1, which are unchanged.

## Scope of this increment

Task 3.2 currently provides the Python half of the protocol primitive layer. The production package
`src/remote_energy_meter/protocol_v1/` implements bounded canonical encodings and JSON, public P-256 JWK and RFC 7638
identity parsing, RFC 9530 Content-Digest, low-S P1363 verification, signed-route profiles, and narrow reporter/control
RFC 9421 request verification. The add-on's exact runtime dependencies now include `cryptography==50.0.0` and
`idna==3.20`.

This increment still adds **no listener**, route handler, replay store, authorization policy, or protocol business
state. Nothing opens a protocol port or holds protocol keys. `tests/unit/test_protocol_v1_primitives.py` and
`tests/property/test_protocol_v1_properties.py` exercise the Python primitives;
`tests/conformance/test_protocol_v1_runtime.py` independently recomputes the Task 3.2 vector intermediates, verifies
all fixed P-256 signatures, rejects every high-S twin, and executes the primitive-layer HTTP and transcript mutations.
`tests/conformance/test_protocol_v1_artifacts.py` checks the contract, vector structure, checksums, and public-key-only
fixture boundary.

Rust implementation, Python/Rust interoperability, AEAD, stateful replay, authorization, route-schema outcomes, and
business-state behavior remain assigned to later work. Task 3.2 remains incomplete until its Rust and cross-language
requirements pass.

## Security principles

These are requirements for the complete protocol. The Python primitive layer implements only the subset identified
above; later implementation tasks must enforce the remaining requirements.

- **Network location grants no trust.** The LAN, VPN, loopback, Docker and add-on bridge networks, the Supervisor
  internal network, the MQTT broker, and mDNS/zeroconf are untrusted transport. Discovery is a routing hint only.
- Every identity is independent and revocable: one P-256 key per reporter, one control key and one control TLS SPKI
  per add-on instance, one MQTT key generation at a time (plus a bounded overlap). There is no shared fleet secret and
  no permanent bearer token.
- First contact is authenticated by a human (six-word SAS compared on both surfaces, or a one-time pairing
  credential copied by an administrator) or by MDM-delivered configuration; never by reachability.
- Every message is bound to its purpose, route, peer identity, key generation, and freshness state, so it cannot be
  replayed on another route, against another instance, or after rotation.
- Energy uses **server receipt time**. Reporter signatures transmit no client wall-clock timestamp, and no reporter
  timestamp drives integration, freshness, ordering, or expiry.
- Every input is capped and strictly validated before allocation; unknown fields, duplicates, and non-canonical
  encodings are rejected, never repaired.

## Assets

| Asset | Where it lives | Compromise impact |
| --- | --- | --- |
| Reporter P-256 private key | Reporter System Keychain, non-exportable | Forged reports for that one reporter until revoked |
| Integration control private key | HA protected storage | Administer the roster of that instance until recovery |
| Control TLS key / SPKI pin | Add-on `/data` / integration storage | Impersonate the control endpoint to the integration |
| `remcp1` pairing credential | Integration flow memory and masked add-on option, minutes only | Pair or recover a rogue control key before expiry |
| Managed-enrollment token | MDM per-device secret store and reporter, ≤ 24 h | Enroll one rogue identity under that subject |
| MQTT AEAD key | Add-on `/data` and integration protected storage | Forge or read v2 state for that instance |
| Reporter CA key | Add-on `/data` | Impersonate the reporter endpoint to enrolled reporters |
| Roster, energy totals, replay high-water marks | Add-on and integration durable state | Rollback, resurrection of revoked reporters, energy corruption |

## Adversaries

1. **Network attacker** on any hop (LAN, VPN, bridge, broker path): observes, drops, delays, replays, reorders,
   rewrites, and injects; can answer mDNS and run a fake server or fake reporter.
2. **Malicious or compromised reporter**: holds a valid enrolled key; tries to affect other reporters, exhaust
   shared resources, or skew energy.
3. **Compromised edge process** (reporter or control TLS/HTTP edge): sees every request byte it parses.
4. **Compromised or curious broker** (Mosquitto or its credentials): stores, replays, suppresses, and reorders retained
   messages.
5. **Local user on a reporter machine** without administrator rights.
6. **Rollback attacker** who restores an old backup of one side.

Out of scope: a fully compromised Home Assistant host or Supervisor, a compromised HA administrator account, a
compromised MDM, physical extraction of non-exportable keys, and denial of service by an attacker who controls the
only network path (they can always drop traffic; the protocol fails closed and visible).

## Threats and required mitigations

The table states the complete protocol's required mitigations. “Declared vector coverage” names the fixtures that
declare the expected values and rejection codes. The Python tests identified above execute the primitive-layer subset
only; a listed vector does not imply executable Rust, cross-language, AEAD, replay, authorization, schema, or
business-state coverage.

| # | Threat | Required mitigation | Declared vector coverage |
| ---: | --- | --- | --- |
| T1 | Fake server during interactive first contact (MITM over provisional TLS) | Commit-before-reveal nonces bound into the SAS transcript with server identity, trust anchor and both commitments; after task 3.3, six rendered words compared on both surfaces; reciprocal confirmation of the same digest before activation; provisional TLS limited to the four interactive route IDs (`reporter.enrollment.create` in interactive mode, `reporter.enrollment.reveal`, `reporter.enrollment.confirm`, and `reporter.enrollment.status`) and memory-only state | `interactive.commitments`, `interactive.sas`, enrollment HTTP cases |
| T2 | Attacker grinds nonces to match a SAS | Each side commits before seeing the other's nonce; a mismatched reveal discards the pending enrollment before the core reveals | `interactive.commitments` (`reveal.order`) |
| T3 | Server identity confusion, redirect, alternate host encodings | Exact `https://host:port` identity compared by both sides before commitment; redirects never followed; IP literals, U-labels, case, leading zeros, paths rejected | `common.server-identity` |
| T4 | Managed token theft at the TLS edge | Token never sent; proof is an HMAC under an HKDF key derived from SHA-256(token) and the core nonce; the core, not the edge, verifies; one durable transaction consumes token, nonce and creates identity | `managed.proof` |
| T5 | Managed proof replay or rebinding to another server/JWK | Transcript binds token ID, opaque challenge, both nonces, scheme/host/port, trust mode and pin, proposed JWK and thumbprint, and expiry; proposed key signs the same bytes | `managed.proof` |
| T6 | Lost issuance response leaves an unidentifiable live token | Client-held operation ID indexes durable public status and idempotent revocation; replacement blocked until the old token is revoked, consumed or expired | `managed-token.issue-status-revoke` |
| T7 | Rogue control pairing over the Supervisor network | One-time administrator credential with absolute expiry; HMAC commitments and opposite-role proofs over the observed control SPKI; consumed-credential digest retained past expiry; provisional dispatcher admits only three routes | `bootstrap.commit-reveal-confirm` |
| T8 | Confusion between the credential, its HMAC key and its consumed digest | Three distinct byte definitions, each fixed by a vector and each substitution rejected | `bootstrap.commit-reveal-confirm` |
| T9 | Recovery used to transfer or reactivate old authority | Separate purposes, a second fresh credential, exact current/observed bindings and scope, recovery generation high-water mark, core proof released only after the durable revocation | `recovery.commit-reveal-revoke` |
| T10 | Signed control request replayed, retargeted, or reused on another route | `control-rfc9421-v1` covers method, authority, path, whole query and each named query value, purpose tag, key generation, nonce, expiry, content type and digest; nonces durable through expiry; per-route tags | every `http.route.control.*` and `http.control-signature` |
| T11 | Signature malleability or encoding confusion | 64-byte IEEE P1363 only, low-S required, DER rejected; exact component list and parameter order | `signature.*.high-s` substitutions |
| T12 | Report replay or reordering | Single-use challenge, memory-only session, strictly increasing `REM-Sequence`, exact-retry record for the last sequence only | `http.reporter-signature` |
| T13 | Key rotation hijack or downgrade | Durable core nonce, both old and new keys sign one transcript, HA administrator confirms the exact digest, generation and expiry; control-key completion carries `rem` and `rem-proposed` | `rotation.reporter`, `rotation.control-key` |
| T14 | Control TLS key replacement without authority | Old and new SPKI keys both sign the transition; administrator confirmation; confirm only on a connection presenting the new SPKI; bounded overlap | `rotation.control-spki` |
| T15 | Broker forges, replays or rolls back v2 state | XChaCha20-Poly1305 with AAD binding topic, reporter, type, key ID and generation, process/state generations, sequence, freshness and nonce; durable high-water marks; process gate and control lease; Will can only withdraw | `mqtt.aead-envelope`, `mqtt.process-offline-will` |
| T16 | Nonce reuse under the MQTT key | Random per-provisioning 128-bit prefix plus a durably reserved 64-bit counter; provisioning rejects a reused prefix or key ID | `mqtt.key-provision` |
| T17 | Wrong or swapped MQTT key installed | Provisioning transcript binds SHA-256 of the key bytes; key-confirmation HMACs in both directions | `mqtt.key-provision`, `mqtt.key-acknowledge` |
| T18 | Resource exhaustion and parser attacks | Fixed byte, item, header, target, connection, parser, crypto-queue and rate caps from `routes.json` and the specification; streaming byte counting; no compression; bounded RFC 9457 errors that never echo input | route manifest, `common.problem-response` |
| T19 | Information leak through errors or metadata | Closed four-member problem object; device metadata limited to four validated fields and sent only after trust | `common.problem-response`, session schema |
| T20 | Roster snapshot tampering or truncation | Frozen snapshot ID and generation, authenticated continuation tokens, completeness digest over the exact ordered records | `common.roster-snapshot-digest` |
| T21 | Rollback by restoring one side | Independent generation/high-water state on both ends and durable tombstones; coordinated full restore requires declared quarantine and fresh pairing (specification 5.3) | specification tests (tasks 4, 7, 9) |
| T22 | Client clock manipulation skews energy | Reporter signatures carry no client wall-clock timestamp; energy uses server receipt time, and freshness uses server-issued state | profile section 8.2 |

## Residual risks and open questions for review

- **SAS strength.** Six words carry 48 bits; an online attacker gets one guess per pending enrollment, bounded by
  the enrollment rate limits. The review should confirm this margin.
- **SAS word tables not yet vendored.** Until task 3.3, vectors carry a placeholder rendering. No user-facing SAS may
  ship before task 3.3 lands the pinned PGP Word List with recorded provenance.
- **Interactive enrollment creation replay.** The initial interactive create request carries no client clock and no
  prior server-issued freshness value. A captured valid request can recreate pending state after the previous pending
  window expires or the core restarts. One pending enrollment per proposed identity, source/global rate limits,
  memory-only expiry, and mandatory SAS/admin confirmation contain its effect, but do not prevent repeated operator
  prompts. Task 3.4 must explicitly accept this exposure or require a server-issued pre-enrollment challenge in a later
  profile correction. Managed enrollment is separately bound to its server-issued opaque challenge.
- **Interactive provisional TLS** gives confidentiality but no server authentication until SAS completes. Its dispatcher
  admits exactly four reporter route IDs: `reporter.enrollment.create` only in interactive mode,
  `reporter.enrollment.reveal`, `reporter.enrollment.confirm`, and `reporter.enrollment.status`. Managed first contact is
  separate: the reporter verifies the artifact-selected CA-SPKI pin or platform-PKI identity before requesting
  `reporter.managed-enrollment-challenge.create` and keeps that verified transport for the managed branch of
  `reporter.enrollment.create`. Provisional control transport remains confined to three bootstrap or three recovery
  routes per phase. These allowlists are load-bearing and must be tested.
- **Profile decisions** in `wire-profile.md` section 15 fill gaps in the specification and need explicit review.
- **Verification-only fixture intent.** The published vectors contain public verification keys and public, test-only
  symmetric fixture values such as pairing credentials, tokens, and an MQTT key. They contain no private signing-key
  members. Except for each deliberately derived high-S twin, which necessarily repeats its source signature's ECDSA
  `r` value, distinct base signatures under one public key do not expose a repeated `r` value. The maintainer reports
  that the private halves were discarded, but the repository has neither the generator nor an audit record and
  therefore cannot prove private-key destruction. Implementations must use the fixtures only for tests and must never
  rely on the maintainer-reported generation or disposal provenance.
