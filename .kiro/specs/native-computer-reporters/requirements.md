# Native Computer Reporters — Requirements

## 1. Scope and compatibility

1.1 The Remote Energy Meter add-on shall accept whole-system power reports from independently installed native
reporters without changing the existing iDRAC/IPMI polling behavior or MQTT v1 contract.

1.2 The reporter shall live in a separate public repository, `superkus/remote_energy_meter-reporter`; neither
repository shall use a Git submodule.

1.3 The first qualified platform shall be an Apple-silicon Mac laptop. Intel macOS and PC providers shall be separate
qualification increments and shall reuse the same protocol.

1.4 If a platform cannot produce a qualified whole-system measurement, the reporter shall send the signed
`WHOLE_SYSTEM_UNAVAILABLE` report defined in 6.3; it shall not publish CPU/GPU/package measurements or estimates as
whole-system power. Silence shall not stand in for this explicit state.

1.5 The existing static-device configuration shall remain the top-level `devices` list with its current `DeviceEntry`
shape; it shall not move beneath `reporters` or change the meaning of any existing field. The list shall accept zero to
50 entries after this feature is implemented. An empty list shall be valid only when `reporters.enabled` is true, so a
reporter-only installation can start with no enrolled reporters and wait for enrollment. If `devices` is empty while
`reporters.enabled` is false, startup shall fail with the existing no-accepted-device behavior. A legacy options
document that predates this feature and omits `reporters` shall parse as the disabled reporter defaults in 7.6, retain
its omission for lossless compatibility printing, and otherwise parse, print, and behave unchanged. Canonical printing
of current configuration shall include the complete defaulted `reporters` block.

## 2. Zero trust and privacy

2.1 Network location (LAN, VPN, loopback, Docker network, broker network, mDNS) shall grant no trust.

2.2 Every reporter shall have a client-generated P-256 key pair and independent identity, authorization, revocation,
and rotation. No permanent bearer API key or shared fleet credential shall exist.

2.3 Every operation after enrollment shall be authenticated and authorized for its reporter identity. First-contact
requests shall remain provisional until they prove possession of the proposed key and complete SAS or managed-token
trust establishment. Every request shall be strictly validated, bounded, and protected against replay before it can
create active authority or change durable state.

2.4 All reporter, control, and edge-to-core IPC traffic shall use authenticated encryption. Reporter TLS shall require
TLS 1.3 and disable 0-RTT. After enrollment, reporters shall authenticate the server as specified by the certificate-mode
matrix in 5.5 and shall authenticate every application request with their enrolled RFC 9421 signing key; the server
shall bind that signature to the TLS request. Post-pair control traffic shall use pinned server TLS plus the
integration's enrolled request-signing key; only bootstrap/recovery may use the encryption-only provisional TLS state
confined by 7.5a and 7.5e. Plaintext modes shall not exist.

2.5 Protocol and measurement data required by sections 4 and 6 may be transmitted. Device-identifying metadata shall be
limited to a user-editable display name, platform, architecture, and reporter version after trust is established. No
other device-identifying metadata may be transmitted; prohibited examples include a hardware UUID/serial number,
user/account name, and MAC address.

2.6 Reporter signatures shall not transmit `created` or any other client wall-clock timestamp. Client clocks shall
never drive freshness, ordering, expiry, or energy integration; the add-on shall stamp accepted reports with server
receipt time.

2.7 Every ingress and downstream schema, including enrollment, roster state, control snapshots/events, MQTT config, and
Home Assistant registry updates, shall use the same metadata constraints and reject rather than truncate or repair
invalid values. `display_name` shall be NFC-normalized UTF-8 of 1–128 bytes, with no leading/trailing whitespace, control
characters, line/paragraph separators, unassigned/private-use/surrogate code points, or Unicode format characters,
including bidirectional controls. `platform` and `architecture` shall each be 1–32 bytes and match
`[a-z0-9](?:[a-z0-9._-]{0,31})`; initially accepted registered values shall be `macos` for platform and `arm64` or
`x86_64` for architecture. `reporter_version` shall be a 1–64-byte canonical ASCII Semantic Version 2.0.0 string without
build metadata. A future value requires an explicit versioned registry/schema update, not permissive acceptance.

## 3. Reporter identity and runtime

3.1 The stable reporter ID shall be the full base64url RFC 7638 thumbprint captured from the initial enrollment public
key. That identifier shall remain immutable after routine replacement of the active signing key. Loss or suspected
compromise of the active key shall require revocation and re-enrollment rather than an unauthenticated recovery path.

Routine reporter-key rotation shall have two reporter-facing phases followed by one HA-administrator confirmation.
The initiation method and path shall be exactly
`POST /api/v1/reporters/{reporter_id}/key-rotations/challenges`, signed by the active reporter key with purpose
`rem-rotate-challenge-v1`, and shall return `201 Created`. Its strict JSON request shall contain only
`schema_version: 1`, `purpose: "rem-reporter-key-rotation-v1"`, `operation_id` as a random canonical 22-character
base64url encoding of 128 bits, `current_generation` as uint64, `proposed_jwk` as the exact canonical public P-256 JWK,
and `proposed_thumbprint` as the canonical 43-character base64url encoding of its RFC 7638 hash. After authenticating
the path reporter, active key and generation, and proposed-key consistency, the core shall durably generate a random
22-character `rotation_id`, fresh 32-byte `core_nonce`, and fixed `expires_at` no more than ten minutes after creation.
The response shall contain only `schema_version: 1`, `purpose: "rem-reporter-key-rotation-v1"`, `rotation_id`,
`reporter_id`, `current_generation`, `next_generation`, `proposed_thumbprint`, `core_nonce` as canonical 43-character
base64url, `expires_at` as uint64 Unix seconds, and `rotation_digest` as canonical 43-character base64url.

The reporter completion method and path shall be exactly
`POST /api/v1/reporters/{reporter_id}/key-rotations/{rotation_id}`, signed by the active reporter key with purpose
`rem-rotate-v1`, and shall return `202 Accepted`. Its strict JSON request shall contain only `schema_version: 1`,
`purpose: "rem-reporter-key-rotation-v1"`, `rotation_id`, `rotation_digest`, `current_signature`, and
`proposed_signature`; each signature shall be canonical unpadded base64url of the 64-byte IEEE P1363 P-256 signature
of the exact transcript by the named key. The core shall verify both before durably changing the challenge state to
`pending_admin_confirmation`. The response shall contain only `schema_version: 1`, `rotation_id`, `rotation_digest`,
`state: "pending_admin_confirmation"`, and the original `expires_at`.

The transcript shall be at most 4 KiB and shall use the common encoding over purpose
`rem-reporter-key-rotation-v1` and, in order, schema version, purpose, reporter ID, current key thumbprint and uint64
generation, exact proposed JWK and thumbprint, next generation, decoded operation ID, decoded rotation ID, the
core-generated 32-byte nonce, and uint64 expiry. `next_generation` shall equal current plus one without wrap. A valid
HA-administrator confirmation shall call the exact signed control route
`POST /control/v1/reporters/key-rotations/confirm` and name only `schema_version: 1`, `operation_id` as a fresh random
128-bit canonical value, `reporter_id`, `rotation_id`, `rotation_digest`, `current_generation`, `core_nonce`, and
`expires_at`; success shall be `200 OK` with only `schema_version: 1`, `operation_id`, `reporter_id`, `rotation_id`,
`rotation_digest`, `state: "active"`, and `active_generation`. The core shall atomically verify the unchanged pending
record and administrator authorization, consume the nonce and transcript, advance the key, and store the result.

Challenge, signatures, and completion state shall survive restart through the fixed expiry; no restart or request may
extend it. There shall be at most one live reporter-key rotation per reporter. An exact initiation duplicate with the
same operation ID and canonical fields shall return the original challenge byte-for-byte; an operation-ID rebind or a
different initiation while one is live shall return 409 `conflict`. Exact reporter completion and administrator-
confirmation duplicates shall return their original status and body without repeating the transition; a changed,
reordered, expired, already-consumed, or out-of-phase request shall return 409 `conflict`. Expired and consumed
challenge tombstones shall survive for five minutes after expiry to reject replay before compaction. A rotation shall
take effect only after both reporter signatures and that exact HA-administrator confirmation; missing, expired,
replayed, or mismatched signatures, nonce, digest, fields, or confirmation shall fail closed.

3.2 The installed macOS reporter shall contain a boot-time system daemon and a separately sandboxed menu-bar
controller. On every XPC connection, each process shall authenticate the other's audit token against a pinned bilateral
code-signing designated requirement before exchanging application data. They shall then establish a fresh ephemeral
P-256 ECDH session and derive independent directional XChaCha20-Poly1305 keys and 16-byte nonce prefixes with
HKDF-SHA-256 bound to the complete handshake, both authenticated code identities, and a fresh random session ID. Every
message shall be an authenticated-encryption envelope whose 24-byte nonce is the sending direction's derived prefix
followed by a uint64 big-endian counter starting at zero. Its AAD shall fix protocol version, session ID, direction, the
enrollment/status-only method, and that counter. Each peer shall reject plaintext, a failed or absent peer
authentication, modified ciphertext or AAD, a counter other than the next expected value, counter wrap, nonce reuse,
and envelopes from another or previous session. Closing an XPC connection shall erase its keys and counter state and
invalidate its session ID before another connection is accepted; the fresh handshake plus exact-next-counter rule is
the freshness and replay boundary. No fallback transport, plaintext compatibility mode, arbitrary command, or arbitrary
file method shall exist.

3.3 The daemon shall store a non-exportable software P-256 private key in the System Keychain. Persistent installation
shall require Developer ID signing, hardened runtime, and Apple notarization.

3.4 Unsigned/ad-hoc code shall be limited to a foreground measurement spike on explicitly approved development
hardware. Public artifacts and qualification reports shall use neutral machine identifiers. No unsigned persistent
daemon or distributed installer shall be produced.

3.5 The reporter shall have no self-updater. Updates shall use MDM or explicit package replacement.

## 4. Discovery and enrollment

4.1 Home Assistant Core, not the bridged add-on, shall advertise `_rem-reporter._tcp.local.` through Core's
zeroconf service only while the reporter port is enabled. The DNS-SD Service Name `rem-reporter` is 12 characters,
within the 15-character limit in [RFC 6763 section 7.2](https://www.rfc-editor.org/rfc/rfc6763#section-7.2).

4.2 mDNS shall provide endpoint discovery only. The reporter shall support a manually configured server URL for
routed networks.

4.3 Interactive enrollment shall use commit-before-reveal with a 32-byte random `reporter_nonce` and an independently
generated 32-byte random `core_nonce`. Each side shall first send `SHA-256(encode("rem-interactive-commit-v1",
[("role", role), ("nonce", nonce)]))`, where `role` is literal ASCII `reporter` or `core`; neither nonce may be
revealed until both commitments are fixed. The reporter shall reveal first, the core shall reject a mismatch before
revealing, and the reporter shall then verify the core commitment. `encode` shall be the domain-separated binary
encoding defined in 7.5a: exact UTF-8/ASCII or decoded binary values, fixed field order, and unsigned big-endian lengths,
with no Unicode or JSON reserialization.

The canonical SAS transcript shall be `encode("rem-interactive-enroll-v1", ...)` over these named fields in order:
`schema_version` as byte `0x01`; `purpose` as literal ASCII `rem-interactive-enroll-v1`; `instance_id` as 1–128 bytes
matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`; `server_identity` as the exact canonical ASCII URI defined below;
`trust_mode` as literal ASCII `ca-spki-sha256` or `platform-pki`; `trust_anchor_spki_sha256` as exactly 32 bytes for a
CA pin or empty for platform PKI; `proposed_reporter_jwk` as at most 256 exact RFC 7638 canonical bytes;
`proposed_reporter_thumbprint` as exactly 32 bytes; `reporter_commitment` and `core_commitment` as exactly 32 bytes
each; `reporter_nonce` and `core_nonce` as exactly 32 bytes each; and `expires_at` as unsigned 64-bit Unix seconds.
Each variable field shall also satisfy its endpoint/schema cap, the complete transcript shall be at most 4 KiB, and
expiry shall be no more than 10 minutes after creation. Let the six HKDF output bytes in order be `b[0]` through
`b[5]`. The normative SAS word tables shall be the exact 256-entry **Even Word List** and **Odd Word List** from the
PGP Word List identified by RFC 6189 section 5.1.6 and its `[pgpwordlist]` reference, preserving every ASCII word and its
zero-based index; task 3.3 shall vendor those tables as normative protocol data. The six words shall be exactly
`Even[b[0]] Odd[b[1]] Even[b[2]] Odd[b[3]] Even[b[4]] Odd[b[5]]`, joined in that order by one U+0020 SPACE. Each
byte is an unsigned index from 0 through 255. Implementations shall not swap a position's list, renumber either list,
change spelling or case, add punctuation, or apply locale-dependent rendering. The indexed bytes shall be
`HKDF-SHA-256(IKM=reporter_nonce || core_nonce, salt=SHA-256(transcript),
info="rem-interactive-enroll-sas-v1", L=6)`.

Interactive `server_identity` shall be an ASCII URI of the exact form `https://<host>:<port>` and no more than 267
bytes. The scheme shall be literal lowercase `https`. `host` shall be the configured canonical lowercase IDNA2008
A-label DNS name, 1–253 ASCII bytes, with valid 1–63-byte labels and no trailing dot. IP literals, Unicode U-labels,
percent encoding, an empty host, and alternate host encodings shall be rejected. `port` shall be explicit canonical
unsigned decimal from 1 through 65535 with no leading zeroes. Userinfo, a path including `/`, query, fragment, or any
other URI component shall be rejected, and the client shall never follow a redirect during enrollment. Before the
reporter fixes or sends its commitment, it shall derive `server_identity` from the connection target and compare the
exact scheme, host, and port with its configured canonical target. The start request shall carry that identity with the
reporter commitment. Before the core fixes or returns its commitment or allocates pending state, it shall compare the
candidate's exact parsed scheme, host, and port with its configured canonical reporter identity. Either mismatch shall
abort. After SAS succeeds, both sides shall use those exact transcript fields for the authenticated request and durable
trust record; normalization, redirection, cross-origin substitution, or alternate encoding shall not produce an
equivalent identity.

The interaction shall start over provisional TLS that provides encryption but no server identity until SAS completion.
Before completion, that connection may call only bounded enrollment challenge/start/reveal/confirm/status endpoints,
may create only memory-only pending state, and shall receive no secrets, roster data, control data, reports, or durable
authority. Home Assistant approval shall record only its half of the decision; the roster shall not authorize reports
until the reporter submits a signed confirmation of the same transcript digest after its user explicitly verifies the
same phrase. An untrusted `approved` response alone shall never establish server trust or activate the identity. After
both confirmations, generated and operator private-CA modes shall pin the exact CA SPKI from the transcript; operator
public-PKI mode shall retain the canonical server name and require normal platform PKI validation. All later TLS shall
verify that mode before sending application data, while the server independently verifies reporter request signatures.

4.4 Home Assistant shall expose each pending reporter as a normal Discovered card. Only an HA administrator may
approve, reject, revoke, rotate, or delete a reporter.

4.5 Managed enrollment shall use a unique 256-bit single-use token per machine, delivered with a canonical server
identity (scheme, IDNA A-label host, and explicit port) and the certificate-mode trust selection from 5.5. Token
issuance shall return exactly one per-machine strict JSON artifact with this shape:

```json
{
  "schema": "rem-managed-enrollment-v1",
  "server": {
    "scheme": "https",
    "host": "<IDNA A-label>",
    "port": 443,
    "trust_mode": "<ca-spki-sha256|platform-pki>",
    "ca_spki_sha256": "<base64url SHA-256 or null>"
  },
  "token_id": "<base64url 128-bit value>",
  "token": "<base64url 256-bit value>",
  "expires_at": "<RFC 3339 UTC timestamp>"
}
```

It shall contain no other fields. The angle-bracketed strings and example port 443 denote typed placeholders, not
prescribed literal values; `port` shall be encoded as a JSON number. Generated and operator private-CA modes shall use
`ca-spki-sha256` with the exact CA SPKI pin; operator public-PKI mode shall use `platform-pki` with JSON `null` for
`ca_spki_sha256` and shall verify the artifact host through normal platform trust. Leaf-SPKI pins shall not be issued or
accepted. Before sending its token proof or any other enrollment data, the managed reporter shall validate the selected
mode immediately; a pin, chain, or hostname mismatch shall terminate the connection and allocate no server state.

Issuance shall use exactly `POST /control/v1/managed-enrollment-tokens` with a strict request containing only
`schema_version: 1`, `operation_id` as a client-generated random canonical 22-character base64url value,
`managed_subject_id` as a 1–128-byte identifier matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`, and
`expires_in_seconds: 86400`. A successful `201 Created` response shall be exactly the artifact above and shall include
`Cache-Control: no-store`. In the same durable transaction that stores only the token verifier, the core shall store a
public operation record binding operation ID, managed subject ID, token ID, issuance time, absolute expiry, and state
`issued`; it shall never store the raw token or artifact. An exact issuance retry shall return 409 `conflict`, never
mint another token and never claim to reproduce the lost artifact. At most one `issued` token may exist per managed
subject, and at most 2,000 unexpired managed-token operation records may exist globally; exhaustion shall reject before
minting.

Recovery from a lost issuance response shall use the signed, HA-administrator-authorized methods
`POST /control/v1/managed-enrollment-token-operations/status` and
`POST /control/v1/managed-enrollment-tokens/revoke`, both with `200 OK` success. Status shall accept only
`schema_version: 1` and `operation_id`, and return only `schema_version: 1`, `operation_id`, `managed_subject_id`,
`token_id`, `state` as one of `issued`, `revoked`, `consumed`, or `expired`, and `expires_at`; it shall never return the
token or verifier. Revoke shall accept only `schema_version: 1`, a fresh `revocation_operation_id`, and the original
`issuance_operation_id`. It shall atomically change an `issued` token to `revoked` and return only
`schema_version: 1`, both operation IDs, `managed_subject_id`, `token_id`, `state: "revoked"`, and `revoked_at`.
An exact revoke duplicate shall return the original response; rebinding either operation ID shall return 409
`conflict`. Status and revocation records shall survive restart and remain queryable through five minutes after token
expiry, after which unknown operations shall return 404 `not_found`.

Replacement shall require a new issuance operation ID and shall be rejected until status for the prior subject's
operation is durably `revoked`, `consumed`, or `expired`. Thus an administrator who loses an issuance or revocation
response can query by the client-held operation ID, revoke the identified token without knowing its raw response, prove
that every proof using the old token is rejected, and only then issue one replacement. The administrator shall import
the successful artifact directly into the MDM's per-device secret store for delivery only to the target machine
selected for that per-device record over the MDM's authenticated encrypted channel. Read and delivery
permissions shall be limited to enrollment administrators and that machine. If an MDM requires a staging file, it shall
be created with `O_EXCL` and owner-only mode 0600, excluded from backup and synchronization, and unlinked immediately
after import. The token and artifact shall expire 24 hours after issuance. The MDM shall remove its copy after durable
enrollment success, revocation, or expiry; the reporter shall delete its raw token and local artifact immediately after
durable success and no later than expiry. End-to-end tests shall drop the issuance response, recover status by operation
ID after restart, revoke without the raw response, prove every old-token enrollment proof is rejected before
replacement issuance, and then enroll with the replacement. They shall also drop the revocation response and prove
exact revocation recovery does not mint or revoke twice.

The reporter shall never send the token to the TLS edge. Instead, after receiving a fresh 60-second challenge containing
a 32-byte `core_nonce`, it shall generate a fresh 32-byte `reporter_nonce` and send a token-derived proof over the
canonical `encode("rem-managed-enrollment-v1", ...)` transcript. Its named fields, in order, shall be:
`schema_version` byte `0x01`; `purpose` literal ASCII `rem-managed-enrollment-v1`; decoded 16-byte `token_id`;
`opaque_challenge` of at most 256 bytes; 32-byte `core_nonce`; 32-byte `reporter_nonce`; `scheme` literal ASCII
`https`; canonical IDNA A-label `host` of 1–253 ASCII bytes; `port` as unsigned 16-bit; `trust_mode` as literal
`ca-spki-sha256` or `platform-pki`; `ca_spki_sha256` as the decoded 32-byte pin or empty for platform PKI;
`proposed_jwk` as at most 256 exact RFC 7638 canonical bytes; `proposed_thumbprint` as exactly 32 bytes; and
`expires_at` as unsigned 64-bit Unix seconds. The complete transcript shall be at most 4 KiB. The proof and proposed-key
signature shall both cover those exact bytes. The metering core, not the edge, shall validate the proof against its stored token verifier and
the key-possession signature against the proposed JWK. In one durable atomic transaction it shall mark the token and
nonce consumed and create the identity before returning success; failure shall perform neither action. A substituted
JWK or server identity, replayed proof or nonce, already-used/expired/revoked token, or edge-generated request shall
fail closed without revealing whether any other token is valid. Raw tokens, token verifiers, and token-derived keys
shall never be available to the edge.

4.6 Interactive pending state shall expire after 10 minutes and remain memory-only. The add-on shall permit at most
16 interactive pending enrollments globally and two per source address.

## 5. Certificates

5.1 Default mode shall generate a P-256 reporter CA under `/data` with a ten-year lifetime and a one-year
reporter-edge leaf renewed 30 days before expiry. Routine reporter leaf renewal shall retain its private key and SPKI
and shall not require re-enrollment. The control listener shall use the separate mode-independent identity in 7.5d. A
planned private-key replacement is an identity transition governed by 5.4 or 7.5d, not a leaf renewal.

5.2 Advanced mode shall accept operator certificate/key/CA files resolved only under read-only `/ssl`. Operator
public-PKI mode shall omit a configured private CA and use platform PKI, including chain, validity, EKU, and canonical
hostname checks. Operator private-CA mode shall use the exact SHA-256 of the configured CA certificate's DER SPKI as
its trust anchor. Leaf-SPKI pins shall not be used.

5.3 Supervisor backup/restore shall preserve generated CA/private keys, listener leaf keys/certificates, reporter trust
and energy state, control-SPKI transition/revocation state, integration control public key/generation, and every
unexpired consumed control-pairing-credential digest. Backups shall be encrypted and access-controlled. Every pairing
credential embeds its absolute expiry, every proof binds that expiry, and restore/configuration reload shall reject it
after that original instant; restoring an older backup shall never restart or extend its lifetime. A consumed digest
shall be retained until at least five minutes after expiry, then may be compacted because the credential is
cryptographically unusable regardless of the tombstone. Add-on-only or Core-only restore shall compare independent
control generations/SPKI pins and disable control on mismatch.

A coordinated full-system backup rollback cannot be cryptographically distinguished from its original state without
an external monotonic witness. The declared restore workflow, rather than restored data, shall therefore place both the
add-on and integration in a durable post-restore quarantine before either service starts normal operation. Restored
control authority shall be revoked and shall not administer reporters; only bootstrap with a fresh pairing credential
may establish new control authority. Every restored reporter identity shall remain quarantined: it shall receive no
challenge or session, have no report accepted, and confer no administration or publication authority. Proof by a
restored enrolled key shall never release quarantine, and no reporter-revalidation route shall exist. To return a
reporter to service, an HA administrator shall first durably delete the restored identity through the authenticated
reporter-action route and then complete from scratch either the interactive enrollment protocol in 4.3 or the managed
enrollment protocol in 4.5, including all fresh nonces, credentials, transcript proofs, and required confirmations. The
resulting identity is a new enrollment; the restored identity and its authority shall never be reactivated. Until a
restored identity is durably deleted, it shall remain quarantined.

Before leaving post-restore quarantine, both endpoints shall durably mark every restored MQTT key-generation record,
key ID, and nonce prefix unusable; preserve non-secret invalidation tombstones; and delete every restored secret key,
counter reservation, replay baseline, overlap key, and key-bearing retry copy. Through the newly paired pinned control
channel, the core shall provision a fresh random 256-bit key, independent random 16-byte `key_id`, and independent random 128-bit nonce prefix as a new key-generation record. The integration shall durably
store that exact record and acknowledge its key ID, generation, key digest, and nonce prefix; the core shall durably
record the acknowledgement before either endpoint permits MQTT v2 subscription, retained-state application,
connection, or publication. No restored key, key ID, nonce prefix, counter, or ciphertext shall be used to resume v2.
Post-restore quarantine shall remain fail closed until fresh control pairing, MQTT reprovisioning and acknowledgement,
and disposition of every restored reporter identity are durable. Missing or inconsistent generated PKI, control
identity, restore-quarantine state, or acknowledgement shall likewise fail closed and require the authenticated
re-enrollment/re-pairing paths below. Restoring an operator certificate never restores an external private key absent
from `/ssl`.

5.4 Routine generated-CA or operator private-CA transition shall require a bounded transition statement over the old
and new CA SPKI hashes, instance ID, validity interval, and generation, signed by the old CA. Reporters shall verify it
through their currently trusted mode, retain both CA SPKI pins only for the stated overlap, observe a valid chain to the
new CA, acknowledge the new generation, and delete the old pin at explicit completion. Platform-PKI renewal shall
continue to require a valid platform chain and canonical hostname. Any unsigned transition; stale or rollback
generation; loss of the old private key before a transition; suspected CA compromise; or replacement of platform PKI
with private/generated trust shall revoke affected trust and require authenticated reporter re-enrollment. There shall
be no trust-on-first-use or unsigned CA-recovery path.

5.5 Reporter TLS behavior shall follow this matrix exactly:

| Certificate mode | First interactive connection | Managed first connection | Durable post-enrollment trust | Routine leaf renewal | Trust-mode transition |
| --- | --- | --- | --- | --- | --- |
| Generated CA | Encryption-only provisional TLS, enrollment endpoints only; SAS transcript binds CA SPKI | Verify artifact CA-SPKI pin before proof | Verify chain to pinned CA SPKI, then use reporter request signatures for client identity | Allowed under same CA/SPKI trust anchor | Old-CA-signed overlap per 5.4; otherwise re-enroll |
| Operator private-CA | Encryption-only provisional TLS, enrollment endpoints only; SAS transcript binds configured CA SPKI | Verify artifact CA-SPKI pin before proof | Verify chain to pinned CA SPKI, then use reporter request signatures for client identity | Allowed under same CA/SPKI trust anchor | Old-CA-signed overlap per 5.4; otherwise re-enroll |
| Operator public-PKI | Encryption-only provisional TLS, enrollment endpoints only; SAS binds `platform-pki` and canonical server identity | Verify platform chain and artifact hostname before proof | Verify platform chain, hostname, validity, and EKU, then use reporter request signatures for client identity | Allowed while platform verification succeeds | Private/generated transition requires re-enrollment; public renewal remains platform-verified |

For all rows, the SAS transcript bytes and verification are exactly those in 4.3; managed enrollment uses the exact
artifact fields in 4.5. The control listener has a separate, exact control-SPKI identity and lifecycle defined in 7.5a
through 7.5f; reporter trust in this matrix shall never be inferred from control trust or vice versa.

## 6. Reporting protocol

6.1 Protocol v1 shall use RFC 9421 HTTP Message Signatures with P-256, RFC 9530 Content-Digest, RFC 7638 JWK
thumbprints, and RFC 9457 problem responses. Fixed Python/Rust interoperability vectors shall be normative.

6.2 To obtain a session challenge, an enrolled reporter shall submit a bounded RFC 9421 request signed by its active
long-term key. The request shall bind purpose `rem-challenge-v1`, reporter ID, method, path, content type, content
digest, a fresh random 128-bit client nonce, and the core's current five-minute challenge-allocation context. That
context shall be stateless, shall change on core restart and at fixed five-minute boundaries without request-driven
lifetime extension, and shall be accepted only during its stated interval. The metering core shall verify the
signature, purpose, reporter authorization, allocation context, and nonce replay state before allocating a single-use
60-second challenge. An exact duplicate while the original challenge remains live shall
return that challenge without extending its lifetime; a different request while one is outstanding, a duplicate after
consumption/expiry, or a nonce previously used by that reporter shall allocate no state. The reporter shall sign
session creation and receive a random 128-bit memory-only session ID. The server shall permit at most one challenge
per enrolled identity, 2,000 outstanding challenges globally, and 500 active sessions; exhaustion shall not replace or
expire another identity's state.

6.3 Each report shall cover method, path, content type, content digest, session ID, sequence, and signature parameters.
Its strict body shall be exactly one arm of a discriminated union: an available report contains
`{"state":"available","watts":<number>}`, while an unavailable report contains only
`{"state":"whole_system_unavailable"}` and therefore cannot carry watts. Both arms use the same canonical digest,
session signature, sequence, replay, size, and rate rules. The reporter shall send an unavailable report immediately
when the qualified source is lost or the machine enters a power mode that has no qualified source, then repeat that
state at the normal cadence while it remains connected. The server shall atomically end the active energy baseline and
publish reporter unavailability as soon as it accepts this report. The first later available report shall publish
recovery and establish a new baseline without integrating across the unavailable interval. Network silence shall only
activate the watchdog in 8.4 and shall never be interpreted as receipt of this signed state. The sequence shall
strictly increase per session. For the last accepted sequence only, a retry is identical when its session ID, sequence,
canonical body bytes, Content-Digest, and every signed request field equal those of the accepted request and its
signature verifies. The core shall retain that request identity and the exact successful HTTP status and response body
for the session lifetime. An identical retry shall return that original result without repeating validation side
effects, energy integration, state transitions, or publication. The same sequence with any different identity field,
and every older sequence, shall fail with 409.

6.4 Server restart shall invalidate all sessions without a disk write per report; reporters shall open a new session.

6.5 Reports shall be at most 1 KiB and enrollments at most 4 KiB. JSON shall reject duplicate keys, extra fields,
NaN/infinity, wrong types, and non-finite/negative watts. The hard ceiling shall be 100 kW, with a lower configurable
operational ceiling.

6.6 Normal cadence shall be 30 seconds. The server shall rate-limit a reporter to one report per 10 seconds with burst
three and shall return bounded 429/503 responses under load.

6.7 Enrollment, challenge, session, status-poll, and rotation endpoints shall have separate global and per-source or
per-identity rate and concurrency budgets. There shall be at most one outstanding challenge per enrolled identity and
one active status poll per pending enrollment. A source address shall be only a pre-authentication quota key, never
identity or authority; identity quotas shall be charged only after authentication. Exhaustion shall fail closed without
evicting another identity's valid state.

6.8 Ingress shall enforce these hard limits: 64 accepted reporter TCP connections globally and four per source, and
eight control connections globally; eight concurrent reporter TLS handshakes and two control handshakes; 16 active
reporter HTTP parsers and four control parsers; after parsing releases its parser slot, at most 16 parked reporter long polls and four control long polls; and
eight concurrent core cryptographic verifications with eight queued verifications; a 256-byte request target; at most
24 headers and 8 KiB of header bytes total; at most 1 KiB each for `Signature` and `Signature-Input` and 256 bytes for
`Content-Digest`; a five-second TLS-handshake timeout; five-second header and body idle timeouts; a ten-second total
non-long-poll request timeout; a 25-second long-poll timeout; and a 2 KiB maximum RFC 9457 response including headers
and body. The endpoint body ceilings in 6.5 and the API table are absolute. Connection, handshake, target, header, and pre-authentication
source/global rate limits shall be enforced before body parsing, JSON allocation, digest verification, ECDSA, or state
allocation whenever that ordering is technically possible. Parser and cryptographic work shall use fixed-size
semaphores and bounded queues; overload shall reject rather than queue without bound.

6.9 Non-report request token buckets shall have these hard ceilings: managed or interactive enrollment start, 2/minute
per source and 16/minute globally with burst equal to the limit; enrollment reveal or confirmation, 4/minute per source
and per pending enrollment and 32/minute globally, burst four; challenge and session creation, 6/minute per source and
per authenticated identity and 128/minute globally, burst two per identity and 16 globally; enrollment status poll,
one concurrent per pending enrollment, 3/minute per source and enrollment and 48/minute globally, burst one; key
rotation, 2/hour per authenticated identity, 4/hour per source, and 32/hour globally, burst one; authenticated control
operations, 30/minute per control identity and 60/minute globally, burst five and ten respectively. Rejected requests
shall not extend nonce, challenge, pending-enrollment, session, or approval lifetimes.

6.10 The following request-body/response-body byte ceilings shall be enforced while streaming, before allocating an
object larger than the applicable ceiling. `none` means a body is forbidden. Every RFC 9457 error response, including
headers and body, shall remain within the complete-response 2 KiB ceiling in 6.8; the table controls successful bodies:

| Reporter endpoint | Request | Response | Success |
| --- | ---: | ---: | --- |
| `POST /api/v1/managed-enrollment-challenges` | 1 KiB | 1 KiB | `201 Created` |
| `POST /api/v1/enrollments` | 4 KiB | 2 KiB | `201 Created` |
| `POST /api/v1/enrollments/{id}/reveal` | 256 B | 2 KiB | `200 OK` |
| `POST /api/v1/enrollments/{id}/confirm` | 512 B | 1 KiB | `200 OK` |
| `GET /api/v1/enrollments/{id}` | none | 2 KiB | `200 OK` |
| `GET /api/v1/challenge-context` | none | 1 KiB | `200 OK` |
| `POST /api/v1/challenges` | 512 B | 1 KiB | `201 Created` |
| `POST /api/v1/sessions` | 1 KiB | 1 KiB | `201 Created` |
| `POST /api/v1/reports` | 1 KiB | 1 KiB | `200 OK` |
| `POST /api/v1/reporters/{id}/key-rotations/challenges` | 2 KiB | 2 KiB | `201 Created` |
| `POST /api/v1/reporters/{id}/key-rotations/{rotation_id}` | 1 KiB | 2 KiB | `202 Accepted` |

| Control endpoint | Request | Response |
| --- | ---: | ---: |
| `POST /control/v1/bootstrap/commit` | 2 KiB | 2 KiB |
| `POST /control/v1/bootstrap/reveal` | 1 KiB | 2 KiB |
| `POST /control/v1/bootstrap/confirm` | 1 KiB | 2 KiB |
| `POST /control/v1/recovery/commit` | 1 KiB | 2 KiB |
| `POST /control/v1/recovery/reveal` | 256 B | 1 KiB |
| `POST /control/v1/recovery/revoke` | 1 KiB | 2 KiB |
| `POST /control/v1/managed-enrollment-tokens` | 1 KiB | 2 KiB |
| `POST /control/v1/managed-enrollment-token-operations/status` | 512 B | 1 KiB |
| `POST /control/v1/managed-enrollment-tokens/revoke` | 512 B | 1 KiB |
| `GET /control/v1/roster/events` | none | 64 KiB and 100 events |
| `GET /control/v1/roster/snapshot` | none | 64 KiB and 100 records |
| `POST /control/v1/reporters/actions` | 2 KiB | 2 KiB |
| `POST /control/v1/reporters/key-rotations/confirm` | 1 KiB | 2 KiB |
| `POST /control/v1/control-keys/rotation-challenges` | 2 KiB | 2 KiB |
| `POST /control/v1/control-keys/rotations` | 4 KiB | 2 KiB |
| `POST /control/v1/control-spki/rotations` | 4 KiB | 2 KiB |
| `POST /control/v1/mqtt-keys/provision` | 4 KiB | 4 KiB |
| `POST /control/v1/mqtt-keys/acknowledge` | 1 KiB | 2 KiB |
| `POST /control/v1/leases/refresh` | 512 B | 1 KiB |

The event stream shall return the next contiguous prefix fitting both 100 events and 64 KiB, never build an uncapped
intermediate list, and require the caller to continue at `through_generation`. Snapshot pagination shall apply the same
count and byte limits before materialization. A single record or event that cannot fit its endpoint ceiling shall be
rejected when the originating mutation is validated, so pagination can always make progress. Request decompression
shall not be supported; chunked bodies shall be counted cumulatively and aborted one byte over the limit.

## 7. Add-on isolation and networking

7.1 Reporter ingress and administrative control parsing shall run as two dedicated non-root s6 processes under
separate AppArmor child profiles. The reporter edge shall receive only its active reporter TLS leaf key and its own
authenticated IPC credential. The control edge shall receive only the dedicated control TLS leaf key and a distinct
authenticated IPC credential scoped to the control method set. Neither edge shall have direct access to `/data`,
`/homeassistant`, Home Assistant configuration, Supervisor token material, reporter CA signing material, enrollment-
token hashes or verifiers, control-signature verifiers, the other edge's key or IPC credential, or unrelated secrets.

7.2 Each edge shall communicate with the metering core only over its own mutually authenticated, encrypted,
mode-restricted Unix-socket channel. Separate per-boot IPC credentials shall authenticate the expected edge and bind
it to only its reporter or control request schema; one edge's credential shall not authenticate the other profile. The
metering core shall authenticate its IPC peer and independently verify request signatures, authorization, and all
canonical request material. Missing or invalid credentials, cross-profile methods, direct state access, and profile or
file-permission violations shall fail closed before request dispatch.

7.3 Reporter HTTPS shall listen on container port 8443 only when enabled. `config.yaml` shall default
`"8443/tcp"` to `null`; documentation shall suggest host port 58443 after conflict detection. `host_network` shall
remain disabled.

7.4 The administrative control listener shall terminate only in the dedicated control-edge process on internal port
8444 and shall not be externally mapped. The control edge shall remain subject to the separate non-root profile and
state/secret denials in 7.1–7.2 for provisional and post-pair requests alike. Except for the
strictly provisional bootstrap/recovery state in 7.5a and 7.5e, it shall require TLS pinned to the exact control SPKI plus
the RFC 9421 profile in 7.5f signed by the Home Assistant integration's independently enrolled P-256 control key. It
shall accept no non-bootstrap/recovery control operation before that key is approved. The control TLS identity shall be
a dedicated P-256 key whose identifier is
`base64url-no-pad(SHA-256(DER-encoded SubjectPublicKeyInfo))`; hashes of a certificate, PEM text, leaf fields, or CA key
shall not be accepted as the control-SPKI identifier.

7.5 Supervisor discovery shall deliver routing metadata only: control URL, effective reporter port, instance ID,
reported add-on slug, and schema version. Neither a key, SPKI value, token, nor any field in discovery shall establish
trust or authority. The integration shall bind all later discovery updates to the approved add-on slug.

7.5a Initial control-channel trust shall use a one-time credential
`remcp1.<expires_at>.<secret>`, where `expires_at` is canonical unsigned-decimal Unix seconds no more than 10 minutes
after generation and `secret` is exactly 43 unpadded base64url characters decoding to 32 CSPRNG bytes, as the
independent HA-administrator authority. `control_pairing_credential` means the complete canonical ASCII bytes of all
three fields and both literal `.` separators. `control_pairing_key` means only the exact decoded 32 `secret` bytes;
every bootstrap and recovery HMAC shall use that byte string directly, without the prefix, expiry text, encoded secret,
terminator, hashing, or key derivation. `consumed_credential_digest` means
`SHA-256(control_pairing_credential)` over those complete ASCII bytes, including the expiry and encoded secret; it is
not the SHA-256 of `control_pairing_key`, a transcript digest, or an HMAC output. Non-canonical text shall be rejected
before either value is derived. The Home Assistant integration config flow shall generate and display the whole
credential once; the administrator shall paste the same value into the masked `reporters.control_pairing_secret` add-on
option. The absolute expiry is part of every HMAC transcript and shall be checked before expensive work; loading or
restoring the option shall never reset or extend it. The credential shall never be accepted
from Supervisor discovery, an HTTP body, a URL, a log, ingress headers, or internal-network identity, and shall never be
persisted by the integration. The add-on necessarily receives it in protected options; it shall register it for log
redaction, retain only the digests of consumed values in the durable consumed set described below, refuse reuse, and instruct the
administrator to remove the option after successful pairing. A proxied-session claim, forwarded header, discovery
value, internal-network reachability, or
an `approved` response shall not by itself authenticate or authorize bootstrap.

The integration shall generate its P-256 control key and a 32-byte random `integration_nonce`; the core shall generate
an independent 32-byte random `core_nonce`. Bootstrap may open TLS 1.3 with certificate verification deliberately
suppressed only as encryption-only provisional transport while the same fresh, unconsumed secret is active in the
masked add-on option and the authenticated integration config flow, or solely to retry an exact completed confirm
through its original expiry under the restricted cache-return rule below. The integration shall hash the DER SubjectPublicKeyInfo
of the leaf key actually observed in that TLS handshake and require that exact 32-byte value in the HMAC-authenticated
pairing transcript; the core shall require it to equal the SPKI served on that connection. During bootstrap, a
provisional connection shall dispatch only the three exact bootstrap routes and shall return only their schema-valid
success responses or the bounded RFC 9457 errors defined below. It shall reject every other path, method, query,
redirect, protocol upgrade, and response class without dispatching the target route. Recovery uses an independently
selected provisional connection confined to its three routes under 7.5e. After either side accepts pairing, the
integration shall close the provisional connection, reconnect with exact SPKI verification, and reject any attempt to
fall back to provisional TLS, including after a pin mismatch, unless an HA administrator has explicitly started a new
config flow with another fresh secret. A server with no fresh active secret shall reject provisional bootstrap before
creating pending state; the only exception is dispatch of an exact completed-confirm candidate to the bounded cache-
return lookup below.

The `control_pairing_key` shall be used only as the key to HMAC-SHA-256 proofs; neither the key nor the complete
credential shall cross the control connection. Both sides shall commit before either reveal using
`HMAC-SHA-256(control_pairing_key, encode("rem-control-pairing-commit-v1", [("role", role), ("nonce", nonce)]))`,
where `role` is literal ASCII `integration` or `core`. The integration shall submit
its exact canonical public JWK and integration commitment; the core shall fix the instance ID, exact observed
control-SPKI identifier, core commitment, and an expiry no more than 10 minutes after creation before returning that
commitment. The integration shall reveal first; the core shall reject a mismatch before revealing; the integration
shall then reject a mismatched core reveal. Pending control pairing shall be unique per add-on instance, memory-only,
rate-limited, and expire after 10 minutes; restart, mismatch, or expiry shall discard it without partial authority or
lifetime extension.

The three bootstrap phases shall have these exact strict schemas and no implicit transition.
`POST /control/v1/bootstrap/commit` shall accept only `schema_version: 1`, `purpose: "rem-control-pairing-v1"`,
`instance_id`, `control_spki_sha256`, `integration_jwk`, `integration_thumbprint`, and `integration_commitment`.
All 32-byte hashes and commitments shall use canonical 43-character unpadded base64url and the JWK shall be the exact
public P-256 form. Its `201 Created` response shall contain only `schema_version: 1`,
`purpose: "rem-control-pairing-v1"`, random `bootstrap_id` as canonical 22-character base64url of 128 bits, the request's fixed
identity/key fields, `integration_commitment`, `core_commitment`, and uint64 `expires_at` no later than either ten
minutes after creation or the credential's embedded expiry.

`POST /control/v1/bootstrap/reveal` shall accept only `schema_version: 1`, `bootstrap_id`, and `integration_nonce` as
canonical base64url of exactly 32 bytes. After commitment verification, its `200 OK` response shall contain only
`schema_version: 1`, `bootstrap_id`, and `core_nonce` as canonical base64url of exactly 32 bytes.
`POST /control/v1/bootstrap/confirm` shall accept only `schema_version: 1`, `bootstrap_id`, and `integration_proof` as
canonical base64url of exactly 32 bytes. Its `200 OK` response shall contain only `schema_version: 1`, `bootstrap_id`,
`pairing_digest`, `integration_thumbprint`, `control_spki_sha256`, `control_key_generation`, and `core_proof`; hashes
and proof shall use canonical 43-character encodings, and `control_key_generation` shall equal the next durable
non-revoked generation, which is one for first pairing. Unknown fields, an unknown ID, phase reordering, a second
non-identical live commit, or any changed duplicate shall return 409
`conflict` without changing state or expiry. While pending state remains in memory, an exact commit or reveal duplicate
shall return its original status and byte-identical body; restart discards that pending state, after which reveal or
confirm returns 409 and the still-unconsumed credential may start a new bootstrap.

Before releasing confirm success, one durable transaction shall authorize the integration key and SPKI, consume the
credential, and store an integrity-protected completed-confirm record containing the confirm request hash, all fixed
transcript identifiers, original status and exact response bytes. The result shall be authenticated with a dedicated
random core state-authentication key stored separately under the owner-only atomic persistence rules; a missing key,
invalid tag, partial record, or widened permissions shall fail closed and return no success. Through the original
expiry, the provisional dispatcher shall admit only a confirm request that names a completed `bootstrap_id` to this
cache-return check even though the credential and bootstrap authority are now consumed; it shall continue to reject
all other consumed-secret traffic. The lookup by `bootstrap_id` and exact request hash shall occur before ordinary
pending-state lookup and before consumed-credential rejection. For `now < expires_at`, an exact confirm retry, including after response loss or restart, shall return the
recorded response byte-for-byte without reauthorizing or consuming again; any changed confirm for that ID shall return
409. At `now == expires_at` the retry window is closed. The completed record shall remain as a non-returning replay and
consumption tombstone through `expires_at + 300 seconds`, then may be compacted; after the retry window, ordinary
expired/consumed credential rejection applies. A crash before the atomic commit shall leave the credential and
authority unconsumed and expose no completed result, while a crash after commit shall expose only the exact durable
result. Tests shall cover every phase and schema, exact and changed duplicate phases, dropped commit/reveal/confirm
responses, expiry immediately before and at the boundary, restart with pending state, crash immediately before and
after final commit, and exact versus changed confirm retry after restart.

For all pairing, recovery, and rotation transcripts, `encode(purpose, fields)` shall be the bytes `REM-TRANSCRIPT` followed by one
version byte `0x01`, then `uint16be(len(purpose)) || purpose`, then each field in the specified order as
`uint16be(len(name)) || ASCII(name) || uint32be(len(value)) || value`. Lengths count bytes. Purpose and field names
shall be literal ASCII; text values shall be exact NFC UTF-8 where the field permits Unicode; integers shall be
fixed-width unsigned big-endian; hashes, nonces, and decoded base64url values shall be raw bytes; and JWKs shall be the
exact RFC 7638 canonical UTF-8 JSON bytes. Duplicate, omitted, reordered, unknown, non-canonical, or overlong fields
shall be rejected. The complete control-pairing transcript shall be at most 4 KiB.

The canonical transcript shall be `encode("rem-control-pairing-v1", ...)` over these named fields in order:
`schema_version` as byte `0x01`; `purpose` as literal ASCII `rem-control-pairing-v1`; `instance_id` as 1–128 bytes
matching `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`; `control_spki_sha256` as exactly 32 bytes;
`integration_jwk` as at most 256 exact RFC 7638 canonical bytes; `integration_thumbprint` as exactly 32 bytes;
`integration_commitment` and `core_commitment` as exactly 32 bytes each; `integration_nonce` and `core_nonce` as exactly
32 bytes each; and `expires_at` as unsigned 64-bit Unix seconds. After both reveals, each side shall compute
`pairing_digest = SHA-256(transcript)` and independently send
`HMAC-SHA-256(control_pairing_key, encode("rem-control-pairing-proof-v1", [("role", role),
("pairing_digest", pairing_digest)]))`. Each side shall verify the opposite-role proof in constant time before the core
atomically authorizes the integration key and before the integration pins the control SPKI. That same transaction shall
store `consumed_credential_digest` until at least five minutes after the credential's embedded
absolute expiry. Any reuse before expiry shall be rejected before pending state is created; after expiry the credential
is invalid even if a restored backup lacks its consumed-digest record. Reload/restore shall never derive a new expiry
from current time. The secret bytes shall be zeroized from integration flow memory after success/failure, bootstrap
shall close after one successful pairing, and the administrator shall remove the consumed option. Fixed
byte-for-byte Python/Rust vectors shall fix the full canonical credential bytes, exact decoded 32-byte HMAC key,
consumed-credential digest, transcript encoding, commitments, transcript digest, both HMAC proofs, observed-SPKI
binding, and success; mutation tests shall alter each field, length, order, commitment, reveal, nonce, key, SPKI,
instance ID, expiry, and proof and require failure. Additional tests shall reject TLS downgrade and provisional fallback,
route or method confusion, non-bootstrap response classes, every provisional request after pairing, and reuse of the
first, middle, or latest consumed secret after multiple generations, restart, and backup/restore.

7.5b Control-key rotation shall use a durable challenge phase followed by completion. Initiation shall be exactly
`POST /control/v1/control-keys/rotation-challenges`, use the 7.5f signed profile with purpose
`rem-control-key-rotation-challenge-v1`, and return `201 Created`. Its strict JSON request shall contain only
`schema_version: 1`, `purpose: "rem-control-key-rotation-v1"`, `operation_id` as a client-generated random canonical
22-character base64url value, `current_jwk`, `current_thumbprint`, `current_generation`, `proposed_jwk`,
`proposed_thumbprint`, and `next_generation`. JWKs shall be exact canonical public P-256 JWKs; thumbprints shall be
canonical 43-character base64url; generations shall be uint64; and next shall equal current plus one without wrap.
After authenticating the active key and every current/proposed binding, the core shall durably generate a random
22-character `rotation_id`, fresh 32-byte `core_nonce`, and fixed uint64 `expires_at` no later than ten minutes after
creation. The response shall contain only `schema_version: 1`, `purpose: "rem-control-key-rotation-v1"`,
`operation_id`, `rotation_id`, `current_thumbprint`, `current_generation`, `proposed_thumbprint`, `next_generation`,
`core_nonce` as canonical 43-character base64url, `expires_at`, and `rotation_digest` as canonical 43-character
base64url.

The challenge shall bind one canonical transcript no larger than 4 KiB: `encode("rem-control-key-rotation-v1", ...)`
over these named fields in order: `schema_version` as byte `0x01`; `purpose` as literal ASCII
`rem-control-key-rotation-v1`; `instance_id` under the 1–128-byte rule in 7.5a; `current_jwk` as at most 256 exact RFC
7638 canonical bytes; `current_thumbprint` as exactly 32 bytes; `current_generation` as unsigned 64-bit;
`proposed_jwk` as at most 256 exact RFC 7638 canonical bytes; `proposed_thumbprint` as exactly 32 bytes;
`next_generation`; decoded `operation_id`; decoded `rotation_id`; the core-generated 32-byte `core_nonce`; and
`expires_at`. `rotation_digest` shall be SHA-256 of those exact bytes.

Completion shall be exactly `POST /control/v1/control-keys/rotations`, use purpose `rem-control-key-rotation-v1`, and
return `200 OK`. Its strict JSON request shall contain only `schema_version: 1`,
`purpose: "rem-control-key-rotation-v1"`, `operation_id`, `rotation_id`, `rotation_digest`, `current_jwk`,
`current_thumbprint`, `current_generation`, `proposed_jwk`, `proposed_thumbprint`, `next_generation`, `core_nonce`,
`expires_at`, and `administrator_confirmation_id` as a fresh random canonical 22-character value created only after an
authenticated HA administrator confirms the displayed transcript. The same RFC 9421 request shall carry the required
`rem` signature by the current key and an additional `rem-proposed` signature by the proposed key. Both signatures
shall use the exact 7.5f component list and parameters and shall bind the whole completion body and therefore the same
digest, instance, generations, core nonce, expiry, operation ID, rotation ID, and administrator confirmation. Success
shall contain only `schema_version: 1`, `operation_id`, `rotation_id`, `rotation_digest`, `state: "active"`, and
`active_generation`.

The durable challenge and its fixed expiry shall survive restart, and at most one live control-key rotation may exist
per instance. An exact initiation duplicate with the same operation ID and canonical fields shall return the original
challenge byte-for-byte; operation-ID rebinding or a second different live initiation shall return 409 `conflict`.
In one durable transaction completion shall verify the active current key and generation, both signatures,
administrator confirmation, exact challenge and proposed JWK/thumbprint, and freshness; consume the core nonce and
transcript; advance the generation; authorize only the proposed key; invalidate all old control leases and outstanding
signed requests; and retain the exact result and replay tombstone. Before active-key/generation validation, an exact
completion retry shall return the original result through the original completion request expiry without repeating the
transition. A changed duplicate, wrong phase, stale or wrapped generation, expired challenge, reused nonce/digest, or
replay shall return 409 `conflict` without changing authority or extending expiry. Challenge and consumed tombstones
shall remain until five minutes after challenge expiry. Tests shall cover challenge acquisition, signed completion,
response loss in either phase, exact and changed duplicate phases, expiry boundaries, restart before completion,
restart before exact result retry, and crashes immediately before and after the atomic completion commit, in addition
to every field/signature mutation.

7.5c The integration control private key shall be non-exportable when the Home Assistant platform offers a protected
key facility; otherwise it shall be stored only in Home Assistant protected credential storage encrypted at rest, with
the narrowest service-account access and owner-only file permissions. Creation and replacement shall use a same-filesystem
temporary file opened exclusively, fsync of file data, atomic replace, and directory fsync; partial or permission-widened
state shall fail closed. Encrypted Home Assistant backups may include the active key only together with its instance ID,
control-SPKI pin, and generation; restore shall reject any mismatch or generation rollback and require authenticated
re-pairing. Unencrypted backups, diagnostics, support bundles, exports, logs, traces, exceptions, API responses, and
MQTT shall never contain the private key or its serialized form. Completed replacement or revocation shall securely
delete the superseded stored key and all recoverable temporary copies; deletion failure shall keep control disabled and
surface a bounded non-secret error.

7.5d The dedicated control TLS key/SPKI shall be generated and stored under `/data` separately from reporter CA keys,
reporter-edge leaf keys, and operator files. Its certificate shall be a mode-independent self-signed control certificate;
generated, operator private-CA, and operator public-PKI reporter modes shall not sign, replace, or establish trust in the
control identity. During bootstrap, successful bilateral HMAC proof under the administrator-configured
`control_pairing_key` derived exactly as in 7.5a and the exact transcript establish the SPKI pin; after bootstrap,
exact SPKI verification is the TLS server-authentication rule. Routine certificate renewal shall
reuse the control key/SPKI and shall not change authority. Planned SPKI rotation shall require, over the already
authenticated old control channel, a bounded
transition containing instance ID, old and new SPKI hashes, current and next generation, nonce, and expiry, signed by
both old and new SPKI private keys and confirmed by an authenticated HA administrator in the already paired
integration flow. The
integration shall pin both only during a maximum ten-minute overlap, prove a TLS connection to the new SPKI, durably
advance the generation, then remove and reject the old pin. Any mismatch, expiry, rollback, loss of the old key, or
suspected old-key compromise shall forbid this routine transition and use 7.5e. Reporter CA transitions and
operator public-PKI certificate renewals do not change the control SPKI or its pin.

7.5e Loss or suspected compromise of the integration control key, control TLS key, or control SPKI shall
require an HA administrator to configure a fresh, unexpired `remcp1.<expires_at>.<secret>` pairing credential whose
`consumed_credential_digest` is absent from every still-retained consumed record and to enter it in the authenticated
integration recovery
flow. Recovery shall parse `control_pairing_credential`, derive `control_pairing_key` and
`consumed_credential_digest`, and use those exact byte definitions from 7.5a. The complete credential and decoded key
shall remain only in the integration flow and protected add-on option, shall never cross the provisional connection,
and the exact decoded key shall be used directly for every recovery HMAC. Recovery shall use TLS 1.3 as encryption-
only provisional transport under the trust rules in 7.5a, but its dispatcher shall admit only the three exact recovery
routes and shall return only their schema-valid success responses or the bounded RFC 9457 errors defined below. The
integration shall bind the leaf SPKI observed on that connection even when it differs from the previously pinned SPKI.
Discovery shall remain a routing hint and shall not select the instance or any recovery field.

Recovery shall have exactly three phases and no implicit state transitions. `POST /control/v1/recovery/commit` shall
accept a strict JSON object containing only `schema_version: 1`, `purpose: "rem-control-recovery-v1"`, `instance_id`,
`current_control_key_thumbprint`, `current_control_key_generation`, `current_control_spki_sha256`,
`current_control_spki_generation`, `observed_control_spki_sha256`, `revocation_scope`, `recovery_generation`, and
`integration_commitment`. The two hashes and thumbprint shall use canonical 43-character unpadded base64url encodings
of 32 bytes; `integration_commitment` shall be the canonical 43-character unpadded base64url encoding of 32 bytes;
generations shall be unsigned 64-bit integers; `recovery_generation` shall equal the durable recovery high-water mark
plus one without wrap; and `revocation_scope` shall be exactly `control-key`, `control-spki`, or
`control-key-and-spki`. The core shall compare all current values to durable state, the observed SPKI to the TLS leaf,
the instance to its configured identity, the credential's embedded expiry to current UTC, and
`consumed_credential_digest` to unexpired consumed records before allocating one memory-only pending recovery. The integration and core shall generate independent 32-byte nonces and compute commitments as
`HMAC-SHA-256(control_pairing_key, encode("rem-control-recovery-commit-v1", [("role", role), ("nonce", nonce)]))`.
The commit response shall contain only `schema_version: 1`, `purpose: "rem-control-recovery-v1"`, a random canonical
22-character `recovery_id`, `instance_id`, `current_control_key_thumbprint`, `current_control_key_generation`,
`current_control_spki_sha256`, `current_control_spki_generation`, `observed_control_spki_sha256`, `revocation_scope`,
`recovery_generation`, `integration_commitment`, `core_commitment`, and `expires_at`. Every binary field shall retain
its canonical encoding, `core_commitment` shall encode exactly 32 bytes, `expires_at` shall equal or precede the
credential's embedded absolute expiry and shall be no more than 10 minutes after creation.

`POST /control/v1/recovery/reveal` shall accept only `schema_version: 1`, the canonical 22-character `recovery_id`,
and `integration_nonce` as the canonical 43-character encoding of exactly 32 bytes. The core shall reject an
integration-commitment mismatch before returning the only success fields `schema_version: 1`, `recovery_id`, and
`core_nonce` as the canonical 43-character encoding of exactly 32 bytes; the integration shall reject a core-commitment
mismatch before revoke. `POST /control/v1/recovery/revoke` shall accept only `schema_version: 1`, the canonical
`recovery_id`, and `integration_proof` as the canonical 43-character encoding of exactly 32 bytes. Duplicate, omitted,
reordered transcript fields, unknown JSON members, malformed encodings, a second pending recovery, reveal reordering,
or changed fixed values shall fail without state change or expiry extension.

The at-most-4-KiB recovery transcript shall be
`encode("rem-control-recovery-v1", ...)` over these named fields in this exact order: `schema_version` as byte `0x01`;
`purpose` as literal ASCII `rem-control-recovery-v1`; `instance_id` under the rule in 7.5a;
`current_control_key_thumbprint` as exactly 32 bytes; `current_control_key_generation` as unsigned 64-bit;
`current_control_spki_sha256` as exactly 32 bytes; `current_control_spki_generation` as unsigned 64-bit;
`observed_control_spki_sha256` as exactly 32 bytes; `revocation_scope` as one of the literal ASCII values above;
`integration_commitment` and `core_commitment` as exactly 32 bytes each; `integration_nonce` and `core_nonce` as
exactly 32 bytes each; `expires_at` as unsigned 64-bit Unix seconds; and `recovery_generation` as unsigned 64-bit.
Both sides shall compute `recovery_digest = SHA-256(transcript)` and the role-specific proof
`HMAC-SHA-256(control_pairing_key, encode("rem-control-recovery-proof-v1", [("role", role),
("recovery_digest", recovery_digest)]))`.

The core shall verify the integration-role proof in constant time before one transaction atomically consumes both
nonces and the transcript digest; stores `consumed_credential_digest` through embedded expiry plus five minutes; advances the
recovery high-water mark; revokes the current control-key generation, control-SPKI generation, or both exactly as
selected by
`revocation_scope`; writes non-reusable revocation tombstones; invalidates every control lease, pending rotation,
signed-request nonce/result, and in-flight control operation; and closes authenticated control channels. Only after that transaction is durable shall the revoke
response disclose exactly `schema_version: 1`, the canonical `recovery_id`, `recovery_digest` as the canonical
43-character encoding of exactly 32 bytes, `recovery_generation` as unsigned 64-bit, and `core_proof` as the canonical
43-character encoding of exactly 32 bytes. The integration shall verify the core-role proof, digest, and generation before accepting revocation success and only
then remove revoked private keys and pins under 7.5c. An exact revoke retry may return the durably recorded response
without reapplying revocation; every other replay shall fail against durable nonce, transcript, secret-digest, and
generation tombstones. Crash injection before commit shall expose no success or partial revocation; a crash after
commit shall retain the revocation and exact retry result.

Recovery shall never reactivate, replace, or transfer old authority. After confirmed revocation, re-pairing shall use a
second fresh administrator-generated pairing credential and a new pending generation through all of 7.5a, with explicit instance
selection and no inherited approval. One-sided restore shall fail against the independently held revocation/recovery
high-water mark. A full-system rollback follows the explicit trust-rollback rule in 5.3 and shall not be claimed as
automatically detectable. Fixed vectors shall reuse 7.5a's exact full-credential, decoded-HMAC-key, and consumed-
credential-digest bytes and cover every recovery encoding, phase, commitment, proof, and response; negative tests shall
cover cross-instance and cross-purpose substitution; current or observed SPKI substitution; each scope;
nonce/transcript replay before and after restart; expiry boundaries; secret reuse; and crashes at every durable boundary.

7.5f Every post-pair control request, meaning every route in the control table except the six bootstrap/recovery routes,
shall use the narrow RFC 9421 profile below over TLS 1.3 with exact verification of the currently authorized control
SPKI, except that a completed mutation whose route-specific rule permits exact retry uses the unexpired cache-return
path specified below before active integration-key/generation rejection. New work always requires the active key. The required signature label shall be `rem`, algorithm `ecdsa-p256-sha256`, and `keyid` shall be the canonical
RFC 7638 thumbprint of the active integration key. The control-key rotation route shall additionally require a
`rem-proposed` signature with the identical component list and parameters except for `keyid`, which shall be the
canonical thumbprint of the proposed key; no other route shall accept that label. A strict `REM-Purpose` header shall carry the route purpose below;
`REM-Control-Key-Generation` shall be the canonical unsigned-decimal active generation; `REM-Request-Nonce` shall be a
fresh random 128-bit value encoded as exactly 22 unpadded base64url characters; and `REM-Request-Expires` shall be
canonical unsigned-decimal Unix seconds. The signature parameters shall occur exactly once in this order: `created`,
`expires`, `nonce`, `keyid`, `alg`, and `tag`. `created` and `expires` shall be unsigned Unix seconds; `nonce` shall equal
`REM-Request-Nonce`, `expires` shall equal `REM-Request-Expires`, and `tag` shall equal `REM-Purpose`. At server receipt,
`created` shall be no more than 30 seconds in the future, `expires` shall be from one through 60 seconds after `created`
and not earlier than receipt, and attempts shall never extend either value.

Every signature component list shall begin, in this order, with `@method`, `@authority`, `@path`, `@query`,
`rem-purpose`, `rem-control-key-generation`, `rem-request-nonce`, and `rem-request-expires`. `@method` shall be the exact
uppercase method in the table. `@authority` shall be the lower-case IDNA A-label host plus explicit port from the
approved instance control URL, with no userinfo or default-port elision, and shall exactly match the received authority.
`@path` shall be the literal table path with no decoding, dot segments, duplicate slash, alias, or normalization.
Routes with a JSON body shall append `content-type` and `content-digest` in that order, require exactly
`Content-Type: application/json`, and use RFC 9530 SHA-256 over the exact bounded body bytes. Bodyless routes shall
forbid both headers. No-query routes shall reject even an empty `?` or any query member.

The roster event wire target shall be exactly
`/control/v1/roster/events?after_generation=<uint64>&wait_seconds=<0..25>`, with parameters in that order, canonical
unsigned decimal without leading zeroes except `0`, and no percent encoding. Its component list shall append
`"@query-param";name="after_generation"` then `"@query-param";name="wait_seconds"`. An initial roster snapshot target
shall be exactly `/control/v1/roster/snapshot?after_generation=<uint64>` and append
`"@query-param";name="after_generation"`. A continuation target shall instead be exactly
`/control/v1/roster/snapshot?continuation_token=<canonical-unpadded-base64url>` and append only
`"@query-param";name="continuation_token"`. Duplicate, reordered, unknown, omitted, empty, alternatively encoded, or
fragment-bearing query input shall be rejected. Verification shall compare both the signed canonical `@query` and every
required individually named query value before parsing application fields.

| Post-pair route | Exact `REM-Purpose` / `tag` | Replay and idempotency rule |
| --- | --- | --- |
| `POST /control/v1/managed-enrollment-tokens` | `rem-control-managed-token-v1` | Bind issuance to the client operation ID and durable public status. Never persist or replay the raw response; duplicates return 409 and recovery uses status then revocation before a new issuance. |
| `POST /control/v1/managed-enrollment-token-operations/status` | `rem-control-managed-token-status-v1` | A fresh signed request reads only the durable public operation state; request nonce replay follows normal rejection. |
| `POST /control/v1/managed-enrollment-tokens/revoke` | `rem-control-managed-token-revoke-v1` | Couple signed nonce, revocation operation ID, token revocation, and exact public result; exact retries return the result without revoking again. |
| `GET /control/v1/roster/events` | `rem-control-roster-events-v1` | Durably retain nonce and exact response through signature expiry; an exact duplicate returns the same completed page/timeout response without starting another wait. |
| `GET /control/v1/roster/snapshot` | `rem-control-roster-snapshot-v1` | Durably retain nonce and exact response through signature expiry; exact duplicates and continuation-token retries return the same page byte-for-byte. |
| `POST /control/v1/reporters/actions` | `rem-control-reporter-action-v1` | Couple nonce consumption to the durable `operation_id` result in 8.6d; exact retries return that result, while nonce or operation-ID rebinding fails. |
| `POST /control/v1/reporters/key-rotations/confirm` | `rem-control-reporter-key-rotation-v1` | Couple nonce consumption to the pending transcript result; exact retries return the original result and never reconfirm or extend expiry. |
| `POST /control/v1/control-keys/rotation-challenges` | `rem-control-key-rotation-challenge-v1` | Bind a client operation ID to one durable core-nonce challenge; an exact duplicate returns the original fixed challenge and any rebind fails. |
| `POST /control/v1/control-keys/rotations` | `rem-control-key-rotation-v1` | Atomically consume nonce and rotation transcript. Before active-key/generation validation, an exact request-byte hash and nonce match in the unexpired completed-operation cache returns the recorded result; it never re-authorizes the superseded key or advances generation. |
| `POST /control/v1/control-spki/rotations` | `rem-control-spki-rotation-v1` | Atomically consume nonce and transition transcript; an exact retry during the authenticated overlap returns the recorded result without changing pins or overlap. |
| `POST /control/v1/mqtt-keys/provision` | `rem-control-mqtt-key-provision-v1` | Atomically consume nonce and provisioning transcript; an exact retry returns the separately AEAD-at-rest-protected original key-provisioning response without installing another key, only within the mandatory lifetime below. |
| `POST /control/v1/mqtt-keys/acknowledge` | `rem-control-mqtt-key-ack-v1` | Atomically consume nonce and acknowledgement; an exact retry returns the original result without retiring another key. |
| `POST /control/v1/leases/refresh` | `rem-control-lease-refresh-v1` | Atomically consume nonce and record the granted lease expiry; an exact retry returns that expiry and never extends it. |

Except for reporter actions, whose freshly signed semantic retries and 24-hour result/30-day marker are governed by
8.6d, completed mutating requests whose route-specific rules permit exact wire retry shall use lookup before ordinary
active-key/generation rejection as a cache-return path, not authorization or business logic. The durable record shall
bind SHA-256 of the exact request bytes, nonce, purpose, original key thumbprint/generation, any proposed key, status,
exact response bytes, and expiry. A candidate must match every field and still be within the original request/record
expiry; the core verifies the record's integrity and returns its bytes without re-running signatures or state
transition. This exception is what lets a control-key rotation retry succeed after its original current key became
superseded. A missing, expired, altered, or partially matched record falls through to normal validation, where a
superseded/revoked key or stale generation fails. Reporter-action records contain no key material; 8.6d's longer
retention does not widen or alter the MQTT provisioning cache below.

A completed MQTT-key-provisioning retry record is the only such record permitted to contain raw MQTT key response
bytes. Before durable commit, the core shall encrypt those exact response bytes with XChaCha20-Poly1305 under a random
256-bit retry-wrapping key that is generated and stored separately from MQTT keys, control credentials, and the
retry-record store and is never derived from or serialized with them. The wrapping key may be replaced only after no
live retry record remains, and replacement shall atomically remove every old copy. Each record shall use a unique
24-byte nonce, and its AAD shall bind the exact request hash, request nonce, purpose, key ID, key generation, status,
and expiry. The wrapping key and each
ciphertext shall use owner-only mode 0600 and the same exclusive temporary-file, fsync, atomic-replace, directory-fsync
protocol as other core state. The core shall decrypt only after every exact-retry field matches and shall fail closed,
without returning key bytes, on a missing or unreadable wrapping key, widened ownership/permissions, nonce reuse, or
AEAD authentication failure. Neither wrapping material, ciphertext records, plaintext responses, nor temporary or
decrypted copies shall enter logs, traces, errors, diagnostics, support exports, or backups. The record expiry shall be
no later than the original signed request expiry and in all cases no more than 60 seconds after record creation; this
maximum is mandatory and shall not be extended by retry or restart. Successful acknowledgement, provisioning abort,
record expiry, or retirement/revocation of a key shall atomically delete every retry record and temporary copy that
contains that key. Cleanup failure shall keep provisioning/control fail closed until all copies are removed.

For every mutation, nonce consumption, transcript or operation tombstones, state change, and retry result shall commit
in one durable transaction. Read-route nonce/result records shall also survive restart through their fixed expiry;
expired records may be compacted only after they cannot authorize a request. A duplicate with any changed method,
authority, path, whole query, named query value, purpose, key generation, body byte, content type/digest, nonce, expiry,
or signature parameter shall fail rather than use the cached result. A stale generation, revoked key, unknown purpose,
expired request, missing required component, extra signature component, or replay outside the route-specific exact-retry
rule shall fail before business logic. Shared conformance tests shall replay and independently mutate every signed field
and signature parameter for every post-pair route, including both roster-snapshot query forms, and shall prove that a
route's valid signature cannot authorize any other route or purpose.

7.6 The canonical current configuration shall contain a top-level `reporters` block alongside the existing top-level
`poll_interval_s`, `log_level`, `mqtt`, and `devices` fields. For migration only, the Supervisor schema and Python
parser shall also accept a pre-feature raw options document that omits the entire block. That omission shall construct
`ReporterSettings()` with reporting disabled and retain input-presence metadata so the lossless compatibility printer
can omit the block again if no reporter setting is changed. The canonical printer, Supervisor defaults, and any print
after a reporter setting changes shall emit the complete defaulted block. The Python model shall be
`ReporterSettings` containing `enabled: bool = False`, `operational_max_w: float = 10000.0`,
`control_pairing_secret: SecretStr | None = None`, and `tls: ReporterTlsSettings`. A non-null pairing credential shall
match `remcp1.<expires_at>.<secret>`, where expiry is canonical unsigned-decimal Unix seconds no more than 10 minutes
after generation and `secret` is exactly 43 unpadded base64url characters decoding to 32 bytes; any other form shall be
rejected. `ReporterTlsSettings` shall contain
`mode: Literal["generated", "operator"] = "generated"` plus optional `certificate_path`, `private_key_path`, and
`ca_certificate_path` strings. `operational_max_w` shall be finite and in 1.0–100000.0 W inclusive; it shall never raise
the 100 kW protocol hard ceiling. Generated mode shall reject all three operator path fields. Operator mode shall
require `certificate_path` and `private_key_path`; all supplied paths shall be absolute, normalized descendants of
read-only `/ssl`, with symlink resolution unable to escape `/ssl`. `ca_certificate_path` shall be optional: omission
selects platform trust for a public certificate, while presence selects that private CA root. The Supervisor current
default and schema shape shall be:

```yaml
options:
  reporters:
    enabled: false
    operational_max_w: 10000.0
    control_pairing_secret: null
    tls:
      mode: generated
schema:
  # The mapping is optional as a whole only for legacy stored options.
  reporters:
    enabled: bool?
    operational_max_w: float(1,100000)?
    control_pairing_secret: password?
    tls:
      mode: list(generated|operator)?
      certificate_path: str?
      private_key_path: str?
      ca_certificate_path: str?
ports:
  8443/tcp: null
```

Omitted scalar fields inside a present block shall receive the model defaults above. The reporter edge shall start only
when `reporters.enabled` is true; the null default port mapping shall remain independently opt-in. Configuration tests
shall submit old raw options without `reporters`, assert the parsed disabled model and unchanged runtime behavior, and
assert that lossless printing preserves the omission while canonical printing emits the full defaults. Bidirectional
model/Supervisor schema-agreement tests shall cover both the legacy omission and every field and conditional mode/path
rule in the current shape without changing existing static-device names or defaults.

## 8. Dynamic state and Home Assistant

8.1 Static polled devices shall remain capped at 50. The reporter roster shall be separate and support 1,000 enrolled
reporters and 500 concurrently active reporters.

8.2 Accepted push readings shall enter a push-reading destination directly, not the polling scheduler.

8.3 The reporter maximum energy-integration gap shall be 90 seconds measured from same-process monotonic receipt
timestamps. A baseline is fresh only while its age is strictly less than 90 seconds; it is stale at exactly 90 seconds.
A reading at or after that boundary shall rebaseline before energy accumulates. The server shall also record UTC server
receipt time as metadata and as the externally published measurement timestamp, but wall-clock time shall never drive
integration or watchdog deadlines. On process restart it shall preserve cumulative energy and UTC metadata but discard
the monotonic baseline and rebaseline on the first new reading.

8.4 A single bounded min-heap watchdog shall mark a reporter unavailable when monotonic age reaches 90 seconds.
Watchdog and report handling shall serialize per reporter. If report receipt and the watchdog deadline have the same
monotonic timestamp, the stale transition shall be committed first and the report shall then be handled as recovery,
producing unavailable before recovered state and no energy across the boundary. Revocation and an accepted signed
`whole_system_unavailable` report shall end the baseline and publish unavailable immediately without waiting for the
watchdog.

8.5 MQTT v2 shall publish bounded retained per-device config, power, energy, and availability topics plus one
process-wide availability topic. Every v2 value, including process availability, shall be an end-to-end AEAD envelope
from metering core to integration; broker TLS and ACLs shall be defense in depth, not message authority. Each MQTT key
shall have an independent random 16-byte identifier serialized only as the canonical 22-character unpadded base64url
`key_id`; a durable key record shall map exactly one such ID to one unsigned 64-bit generation and key. Canonical
authenticated data shall use the length-delimited encoding from 7.5a and bind protocol version, exact derived topic,
reporter ID when applicable, message type, payload schema version, the decoded 16 `key_id` bytes, key generation encoded
as unsigned 64-bit big-endian, process generation, state generation when applicable, per-stream sequence,
message-specific freshness fields, and nonce; the AEAD ciphertext and tag shall authenticate the complete bounded
payload. Nonces shall be unique for every publication under a key. Before decryption, the integration shall decode only
canonical `key_id`, look up its durable record, and reject unknown IDs, any ID/generation pair not exactly matching that
record, duplicate IDs assigned to another generation, malformed encodings, or rollback. It shall then use those same
raw ID and generation bytes in AAD construction; envelope and AAD values may not be independently supplied. The
integration shall also reject topic/type/reporter substitution, malformed or forged ciphertext, future-dated or
expired authority-bearing messages, lower process/state generations or sequences, the same sequence with different
bytes, and any state generation below
its durable high-water mark. An exact duplicate of the last accepted envelope may be a no-op. Power, energy,
per-device availability, and process-online envelopes shall expire no later than 90 seconds after publication; online
state shall be refreshed often enough that one missed normal report does not expire it. Per-device config shall expire
within 24 hours and be refreshed at least every 12 hours. Suppression or absence past expiry shall make existing
entities unavailable and shall never authorize stale state. MQTT v1 shall continue unchanged for static devices.
Adding a reporter shall not grow or republish a fleet-wide inventory document.

Before releasing any restored MQTT v2 state, a coordinated full-system restore shall enforce requirement 5.3's
durable post-restore quarantine at both endpoints. The core shall reject reporter challenge, session, and report traffic
and shall not connect or publish v2; the integration shall not subscribe to, decrypt, use as a replay baseline, or expose
retained v2 messages. Those gates shall remain closed until fresh control pairing, fresh MQTT key-generation
provisioning with durable integration acknowledgement, and administrator disposition of every restored reporter are
complete. Retained envelopes under every restored key ID shall remain untrusted even if they pass AEAD and restored
high-water checks.

For each MQTT connection attempt, the core shall durably reserve a strictly increasing process generation before
CONNECT and construct an AEAD-protected retained `offline` Will for that generation and the exact process-availability
topic. Process-online and process-offline shall be separate sequence domains; the precomputed Will shall use the sole
offline sequence value for its generation, while online refreshes may advance their own stream sequence. Accepting an
offline envelope shall durably close that process generation, after which the integration shall reject every online or
per-reporter envelope from the same generation regardless of delivery order. Only online from a strictly greater
process generation may reopen the gate. The MQTT CONNECT shall carry the offline envelope as a QoS 1 retained Last
Will; after CONNACK the core shall publish and receive PUBACK for a retained, expiring `online` envelope before
publishing any per-reporter state for that process generation. A clean shutdown shall publish and acknowledge the same
`offline` envelope before DISCONNECT; an unclean connection loss shall cause the broker to publish the registered Will.
The precomputed offline envelope shall bind its creation time but shall have no freshness expiry. The integration may
use it only to withdraw availability, only after successful AEAD verification, and only when its process generation is
not below the durable process high-water mark. It can therefore remain safe as an old MQTT Will without granting
authority; delayed Wills from older connections cannot override a newer generation. Every per-reporter envelope shall
bind the current process generation, and the integration shall expose it only while a matching unexpired process-online
envelope and reporter control lease are both live. Receipt of a valid matching process-offline envelope shall gate all
reporter entities unavailable before any later per-reporter message is applied. On broker or integration reconnect,
the integration shall subscribe to and resolve process availability before subscribing to retained per-reporter
state; any per-reporter message received before both process and control gates are live shall be dropped, never exposed
or used as a replay baseline. Expiry of the last process-online envelope remains the fail-closed fallback when broker
failure prevents Will delivery.

8.6a MQTT v2 AEAD keys shall be random 256-bit per-installation keys provisioned only through the authenticated pinned
control channel. The add-on shall store active MQTT keys mode 0600 and the integration shall use Home Assistant
protected credential storage. Any durable exact-retry copy of a raw provisioning response shall obey the separate
wrapping-key, AEAD, 60-second maximum, backup exclusion, fail-closed cleanup, and key-retirement deletion rules in
7.5f; mode 0600 alone is not sufficient protection for that response. Provisioning and rotation shall mutually confirm
the canonical 16-byte/22-character `key_id`, its unique unsigned 64-bit generation, the SHA-256 digest of the exact key
bytes, and independent 32-byte nonces from core
and integration in one canonical control transcript. Each provisioned record shall also contain an independent random
128-bit nonce prefix generated for that provisioning operation; post-restore provisioning shall never restore or derive
it from previous state. Both endpoints shall bind that exact prefix into the transcript and durable acknowledgement.
Both endpoints shall reject a reused ID or nonce prefix, a generation associated with any other ID, a non-canonical
ID encoding, or any offered record that does not match the transcript and durable record. Rotation shall use a bounded overlap, and the old key shall be deleted only after
the integration acknowledges the new generation over that channel. After a coordinated full-system restore, overlap
is forbidden: both endpoints shall durably mark every restored key ID, generation, and nonce prefix unusable, retain
non-secret invalidation tombstones, and delete associated secret keys, counter/replay state, overlap keys, and retry
copies before provisioning one fresh record through newly paired control. The core shall not publish and the
integration shall not subscribe or accept retained v2 state until the integration's acknowledgement of the fresh key
ID, generation, key digest, and nonce prefix is durable at both endpoints. On startup, before subscribing, the
integration shall load the trusted durable one-to-one key-ID/generation map and per-process/per-reporter/per-message
state-generation and sequence high-water marks; missing or corrupt replay state shall fail closed and require
authenticated reconciliation, never acceptance of broker retained state as a new baseline.

8.6b The authenticated control stream shall use a `roster_generation` unsigned 64-bit cursor initialized to zero. The
core shall increment it exactly once for each control-visible roster mutation: pending-enrollment creation, HA approval,
rejection, or expiry; reporter-confirmed activation; display-metadata change; pending-key-rotation creation, HA
approval, expiry, or completed key swap; revocation; and deletion. The core shall
persist the new cursor before exposing its event. Pending-event payloads shall remain memory-only as required by 4.6;
if restart discards them, the persisted cursor shall force snapshot reconciliation rather than replay or cursor reuse.
Failed requests, exact retries, reads, lease refreshes, and MQTT publication shall not increment it. Each
immutable event shall contain `generation`, `event_type`, `reporter_id`, `reporter_state_generation`, and the complete
current public roster record or deletion tombstone; generations shall be contiguous and events shall be ordered by
generation.

8.6c The generation long-poll request shall use the exact signed wire query in 7.5f containing `after_generation` (the
integration's last durably applied cursor) and `wait_seconds` from 0 through 25. A successful response shall contain `after_generation`, `through_generation`,
`current_generation`, an ordered `events` array of at most 100 events, and `timed_out`; the complete body shall be at
most 64 KiB and shall be constructed incrementally without an uncapped intermediate list. If newer events exist, the
core shall return
immediately with the next contiguous events, set `through_generation` to the last returned event, and set `timed_out`
to false; a bounded partial page is complete and the integration shall immediately request the next page from that
cursor. If no newer event exists, the core shall park the request until the first event is committed or `wait_seconds`
elapses. The first-event condition shall return immediately with `timed_out: false`; timeout shall return an empty array
with all three generations equal to `after_generation` and `timed_out: true`. A cursor ahead of the core or older than
retained event history shall return 409 with `snapshot_required`; responses shall never skip a generation.

8.6d Approve, reject, revoke, and delete requests shall contain `operation_id` (a random 128-bit value), `action`,
`reporter_id`, `expected_roster_generation`, and the action-specific fields. `operation_digest` shall be SHA-256 over
strict canonical JSON containing `rem-control-operation-v1` and those exact fields. The successful result record shall
be durable from `committed_at` through the half-open interval `committed_at <= now < committed_at + 24 hours` and shall
bind operation ID, digest, fields, original status and exact response body. After authenticating a currently authorized
control key and a fresh signed wrapper, but before rejecting the now-stale expected generation, the core shall compare
that record. An exact semantic retry may use a new 7.5f request nonce and expiry but shall have the same operation ID,
digest, and operation fields; it shall return the original status and body without reapplying the operation or
incrementing either generation. Reuse of an operation ID with a different digest or field shall return 409 `conflict`;
a new operation with a stale expected generation shall also return 409.

At exactly `committed_at + 24 hours`, lookup shall logically treat the record as a compact marker binding the
operation ID and digest but no response body. The marker interval is half-open from that instant until
`committed_at + 24 hours + 30 days`. Physical compaction shall atomically delete the response body and retain the
marker, but cleanup timing shall not affect behavior: even if the un-compacted full record is still present at the
first boundary, no lookup may return its status or body. During the marker interval, a valid freshly signed exact retry
shall return 410 `operation_result_expired` before stale-generation evaluation and shall not reapply the operation; a
digest rebind shall return 409. At exactly the marker expiry, lookup shall ignore the marker regardless of whether
physical cleanup has removed it and shall follow ordinary authentication, nonce-freshness, and generation rules, so an
expired or invalid signature returns 401 and a valid request with the old expected generation returns 409 rather than
410. Invalid authentication always returns 401 before either result or marker lookup. Tests shall exercise one instant
before, exactly at, and one instant after both boundaries, with physical compaction both completed and deliberately
lagging; restart in each interval; exact and changed operation digests; valid and invalid fresh signatures; and proof
that no action or generation is repeated. These non-secret action records do not change the separate MQTT key-bearing
retry-record rule: that encrypted cache expires no later than the signed request and never more than 60 seconds after
creation.

8.6e The stream shall authorize each reporter and exact state generation, deliver revocation/deletion tombstones, and
refresh a 60-second authorization lease. Initial startup, `snapshot_required`, and every control reconnect shall begin
with an authenticated authoritative roster snapshot before MQTT state is applied.

8.6f An initial snapshot request shall use the exact signed 7.5f query containing `after_generation` and omitting
`continuation_token`. The core shall freeze records in ascending canonical `reporter_id` order at one `snapshot_generation`, create a random 128-bit `snapshot_id`, and retain that immutable
view for five minutes. Every response shall contain `snapshot_id`, `snapshot_generation`, zero-based `page_index`,
`total_count`, `records`, `complete`, and `expires_at`; a non-final response shall also contain an opaque authenticated
`continuation_token` that binds the snapshot ID and generation, next page index and record offset, total count, and
expiry. The next request shall use only the exact signed `continuation_token` query form in 7.5f. Retrying the same token before expiry shall return the same
page and next token byte-for-byte without consuming either; roster mutations shall not alter or invalidate the frozen
view, and their later events shall remain available from `snapshot_generation`.

8.6g Pages shall contain at most 100 records and 64 KiB. For bounded records that fit without the byte limit reducing
the page, rosters of 0, 1, 100, 101, and 1,000 records shall complete in respectively 1, 1, 1, 2, and 10 pages; the empty
snapshot shall be page zero with an empty `records` array. The final page shall set `complete: true`, omit
`continuation_token`, and include `completeness_digest`; every earlier page shall set `complete: false` and omit that
digest. The digest shall cover the snapshot ID, generation, total count, and exact ordered canonical records. A token
that is expired, unknown, altered, or lost across a core restart shall return 410 `stale_snapshot`; the integration
shall discard the partial result and begin a new initial request. Until expiry, retrying a token that returned the final
page shall return that same final response byte-for-byte. Server restart shall not recreate a snapshot under the old
ID. An exact byte-for-byte duplicate produced by retrying the same request token shall be accepted idempotently and
shall not append records twice. A missing page, conflicting duplicate, non-successor index, changed snapshot ID or
generation, count/digest mismatch, timeout, or interrupted snapshot shall otherwise discard the partial result and
restart without treating absence as deletion.

8.6h Only after one complete snapshot is durably applied may the integration set its cursor to
`snapshot_generation`, derive deletion of a previously known reporter from its absence, advance that reporter's local
tombstone, remove its registry device, and enable MQTT subscriptions or release buffered MQTT data.
Incremental updates then resume with `after_generation` equal to that exact cursor; if retained event history no longer
covers it, `snapshot_required` starts a fresh snapshot. The integration shall apply MQTT only for an authorized
generation while its lease is live and shall mark affected entities unavailable if control updates are suppressed or
stale. Revocation and deletion tombstones shall advance and retain the relevant high-water marks so delayed ciphertext
cannot restore authority. Empty retained MQTT payloads shall not themselves authorize deletion; deletion remains an
authenticated control operation.

8.6i Both core-to-broker and integration-to-broker hops shall use certificate-verified TLS with distinct least-privilege
credentials and topic ACLs.

8.7 The integration shall support v1 and v2 concurrently under its existing single config entry per add-on instance;
it shall not create one config entry per reporter. That config entry shall retain its current stable unique ID equal to
the add-on `instance_id` and shall own both the existing v1 coordinator path and the new authenticated control/v2 path.
The integration shall register one stable hub device with Device Registry identifier
`(DOMAIN, "hub:" + instance_id)`. Existing static devices shall remain flat, shall keep identifiers
`(DOMAIN, static_device_id)` and entity unique IDs `rem_<static_device_id.lower()>_<role>` byte-for-byte, and shall not
receive `via_device_id`. Static device IDs remain limited to their accepted `[A-Za-z0-9_-]{1,64}` grammar. Each reporter
shall create one child device with identifier `(DOMAIN, "reporter:" + reporter_id)`,
`via_device_id=(DOMAIN, "hub:" + instance_id)`, and only power and energy entities whose unique IDs are
`rem_reporter:<reporter_id>:<role>`. Home Assistant accepts `:` in an entity unique ID, while that delimiter is outside
the static device-ID grammar; therefore the existing static ID `reporter_<reporter_id>` continues to produce
`rem_reporter_<reporter_id>_<role>` and cannot collide with the reporter entity. Compatibility tests shall register
that static device and the same reporter simultaneously for every role and assert distinct entity registry entries,
stable IDs across reload/restart, and no migration of existing static entities. Connectivity shall be represented by
entity availability.

8.8 Pending reporters learned from the authenticated control stream shall use transient integration-discovery config
flows associated with the already configured instance. A transient flow shall use `reporter:<reporter_id>` as its
flow unique ID for duplicate suppression, show the normal Discovered card, and perform approve or reject through the
existing instance's authenticated control client; completion or dismissal shall not call `async_create_entry` and
shall not unload, reload, or replace that config entry. Reconnect or Home Assistant restart shall recreate only still
pending flows after authenticated roster reconciliation and shall suppress a flow already in progress for the same
reporter. If the instance config entry is absent, unloaded, or lacks an authenticated live control channel, no pending
reporter flow shall grant authority or be shown as actionable. The live control client exists only after bilateral
HMAC proof under the one-time administrator-configured `control_pairing_secret` in 7.5a has authorized the integration's
P-256 key and the integration has pinned the add-on control SPKI. Supervisor discovery only locates that bootstrap and
never establishes it.

8.9 Revocation shall retain the device, entities, and energy history while publishing unavailable. Explicit deletion
shall be a separate confirmed operation that durably advances roster and reporter generations before it clears retained
state or removes the registry device. The core shall keep at most 1,000 pending deletion-cleanup records and shall
reject a new delete without changing roster state if that capacity is exhausted. For every accepted delete it shall
retry QoS 1 empty-retained cleanup for each reporter topic with capped exponential backoff until every PUBACK and an
authenticated integration acknowledgement of the new roster generation are durable. Cleanup delay or HA being
offline shall not restore authority: the complete control snapshot applied under 8.6h remains authoritative, and the
integration shall retain its local reporter tombstone until all MQTT keys capable of authenticating that reporter's
older retained ciphertext have been retired. Only then may both sides compact the completed cleanup record. Delayed empty-retained
messages are transport cleanup only, and delayed non-empty ciphertext remains rejected by the control lease,
generation, and tombstone checks.

## 9. Measurement qualification and capacity

9.1 The selected `AppleSmartBattery.PowerTelemetryData` property has no documented public contract in the official
Apple sources cited in the design. The Apple-silicon spike may read that candidate via public IOKit registry APIs, but
code-only qualification can establish only consistent software-visible semantics and runtime behavior, not physical
accuracy. User-facing values shall be identified as **Apple-reported system power**. Documentation and status surfaces
shall state that the source is undocumented, is not externally calibrated or billing-grade, and must not be used for
billing, settlement, or safety-critical decisions. No artifact shall claim a known physical-accuracy bound.

9.2 AC and battery shall be qualified independently before either mode is released. Each candidate mode shall complete
one five-minute run for each of idle, CPU load, and GPU load. One mode's failure shall leave that mode unsupported
without qualifying or disqualifying the other; an Apple-silicon release requires at least one supported mode. The selected Apple-reported system-power field shall be `SystemPowerIn` on AC so battery charging is included,
and `SystemLoad` on battery because no external input is present. The reporter shall never substitute one field for the
other. Every selected value shall be finite, greater than zero, and no more than the 100 kW protocol hard ceiling.
Missing, malformed, stale, negative, zero, over-ceiling, or subsystem-only selected input shall fail the mode. At
runtime the same failure shall produce signed `WHOLE_SYSTEM_UNAVAILABLE` for the complete system, without cross-mode
fallback, stale-value reuse, or reuse of a sample or accumulator baseline from a prior mode. Reporter overhead shall
satisfy 9.3.

All candidate power fields shall be converted to milliwatts before consistency checks. For two milliwatt values `a` and
`b`, "within 1%" shall mean `abs(a - b) <= max(1 mW, 0.01 * max(abs(a), abs(b)))`; this is an internal software-
consistency tolerance, not a physical-accuracy bound. For every accepted snapshot, `SystemPowerIn` shall be within that
tolerance of `SystemLoad + BatteryPower`. On AC, `SystemPowerIn` shall also be within that tolerance of the milliwatt
value obtained from `SystemVoltageIn × SystemCurrentIn` after applying the candidate fields' explicitly recorded unit
conversion. On battery, `SystemPowerIn`, `SystemVoltageIn`, and `SystemCurrentIn` may be zero, while `BatteryPower` shall
be negative and its magnitude shall be within that tolerance of `SystemLoad`.

The mandatory accumulator tuples are exact, atomic, and mode-specific:

| Mode | Accumulated-value field | Count field | Direct field |
| --- | --- | --- | --- |
| AC | `AccumulatedSystemPowerIn`: running sum of `SystemPowerIn` samples, converted to a milliwatt sample-sum; positive interval delta | `SystemPowerInAccumulatorCount`: unsigned integer sample count | `SystemPowerIn`: direct milliwatts, positive |
| Battery | `AccumulatedSystemLoad`: running sum of `SystemLoad` samples, converted to a milliwatt sample-sum; positive interval delta | `SystemLoadAccumulatorCount`: unsigned integer sample count | `SystemLoad`: direct milliwatts, positive |
| Battery | `AccumulatedBatteryDischarge`: running sum of `BatteryPower` discharge samples, converted to a milliwatt sample-sum; negative interval delta | `BatteryDischargeAccumulatorCount`: unsigned integer sample count | `BatteryPower`: direct milliwatts, negative for discharge |

AC requires its one listed tuple. Battery requires both listed battery tuples in the same candidate snapshot. No tuple
or member from the other mode may be substituted. Every member of every mandatory tuple shall be available, present,
and well-formed in every candidate snapshot. An unavailable or missing member, wrong type, non-finite value, count
outside the unsigned integer range, or direct value or interval delta with the wrong sign shall fail the complete
mode. No member or baseline may be retained from an earlier snapshot or another mode to complete or repair a tuple.

For each mandatory tuple, the first valid count observation shall establish an atomic accumulator-value/count/direct-
value endpoint and start its monotonic freshness age, even if that count never advances. A later count shall never
decrease or wrap. A zero count delta is valid only while less than 90 monotonic seconds have elapsed since that first
observation or the most recent positive delta; at exactly 90 seconds it is stale and shall fail the mode. A negative or
wrapped count delta shall fail immediately. Only a positive count delta closes a source-owned hardware-update
interval. The accumulator-value delta divided by that positive count delta shall be finite, shall have the tuple's
milliwatt sign convention, and shall have an absolute magnitude no greater than the 100 kW hard ceiling. A wrong-sign
or wrapped accumulator-value delta shall fail the complete mode. A zero count delta before the freshness deadline
leaves the current interval open. Direct reads are not synchronized to Apple's private accumulator interval
boundaries, so qualification shall not assign repeated polled values to hidden source intervals or claim a pointwise
accuracy comparison between them. When both mandatory battery tuples close on the same endpoints, their positive
`SystemLoad` and negative `BatteryPower` interval means shall be signed inverses within the same tolerance.

Apple's public IOPowerSources API shall independently classify AC versus battery and charging state. Each candidate
snapshot shall be bracketed by public-state reads taken no more than one monotonic second apart; both reads shall report
the same classifiable mode. Qualification CSV shall allowlist only those public fields plus documented battery voltage
and signed current when the API supplies them. Public voltage/current absence shall be recorded as missing rather than
replaced or inferred; these fields are diagnostic and are not an admission gate because supported Apple-silicon systems
may omit them. Every observed mode change shall emit signed `WHOLE_SYSTEM_UNAVAILABLE`, clear every prior-mode
sample and accumulator baseline, and admit no available value until a complete bracketed snapshot in the new mode
passes that mode's criteria. An unsupported new mode shall remain unavailable on the normal cadence. Source loss, an
unclassifiable or changing bracket, failed internal consistency, stale counters, or a failed transition shall fail
closed in the same way.

For each mode, qualification shall use exactly the one five-minute idle, CPU-load, and GPU-load run required above;
it shall not add or repeat runs for workload-direction checks. The selected direct-value aggregate for each run shall
be the median of all valid selected direct readings captured in that run. For each mandatory accumulator tuple and
run, qualification shall separately collect every valid interval mean closed by a positive count delta in that run and
use their median as that tuple's run aggregate; a run with no such interval for a mandatory tuple shall fail
qualification. For either aggregation, the median is the middle value after numeric sorting, or the arithmetic mean of
the two middle values for an even-sized set. The CPU-load and GPU-load aggregates shall each be strictly greater than
the idle aggregate for the positive AC `SystemPowerIn` and battery `SystemLoad` values and tuples. The negative
battery `BatteryPower` tuple aggregates shall each be strictly less than the idle aggregate, meaning a greater
discharge magnitude. These workload-direction comparisons are run-level qualification gates only. Production shall
not infer a workload class or enforce workload direction; it shall continue to enforce interval finiteness, sign, hard-
ceiling, freshness, and same-endpoint battery signed-inverse checks. This workload-response check establishes
repeatable software-visible separation only. `powermetrics` may be recorded only as a secondary software diagnostic
and never as system-power ground truth.

9.3 Reporter overhead shall average below 0.5% of one CPU core and remain below 30 MiB physical memory.

9.4 A load test shall sustain 1,000 enrolled, 500 active reporters at a 30-second cadence (approximately 17 reports/s)
without dropping valid reports, starving existing pollers, or unbounded memory growth. A 500-client reconnect storm
shall recover without process restart.

## 10. Release gates

10.1 The combined enrollment/SAS profile shall receive independent security review before distribution.

10.2 Both repositories shall use protected `main`, development branches, fail-closed secret/local-reference scans,
pinned CI actions/tools, dependency advisory/license checks, tests, and review gates.

10.3 Add-on/integration release shall be 0.2.0 and reporter release 0.1.0. Deployment shall come only from reviewed,
merged `main` commits; no direct live patch is permitted.
