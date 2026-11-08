# Native Computer Reporters — Tasks

Each task is test-driven, ends with the new behavior wired into the running product or an executable spike, and is
landed through the repository's security/review gates. No task leaves orphaned production code.

Documentation ships with each implementation increment rather than being deferred to the release task. Every increment
that changes add-on user-facing behavior, configuration, security, or runtime operations updates
`remote_energy_meter/DOCS.md`; every increment that changes development, build, test, or release work updates
`README.md`; and every increment that changes shipped code, runtime dependencies, the base image, or CI/security
behavior that alters the shipped image adds the applicable entry under `CHANGELOG.md`'s Unreleased section. Dev-only
package pins and their bumps are recorded in git history and dependency PRs, not `CHANGELOG.md`. Reporter-repository
increments follow the same rule in that repository for its user/developer docs and Unreleased changelog. Task 15.1 is a
completeness and accuracy review of documentation already landed with the implementation increments, not the first
documentation pass.

- [ ] 1. Establish zero-trust repository baselines
  - _Requirements: 1.2, 2.1, 2.2, 2.3, 2.4, 3.4, 10.2_
  - [x] 1.1 Add zero-trust rules to this server repository's workspace steering and commit the approved
    requirements/design/tasks.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.4, 10.2_
  - [ ] 1.2 Create public `superkus/remote_energy_meter-reporter` (no submodule), topic it, create `main`/`dev`, and
    install protected-main plus pinned CI for rustfmt, Clippy, tests, gitleaks, local-reference scan, cargo-audit,
    cargo-deny, hooks, and Dependabot. Add contributor documentation in both repositories that explains the repository
    boundary, branch workflow, exact local toolchain/bootstrap commands, hooks, and matching local/CI gates.
    - _Requirements: 1.2, 10.2_
  - [ ] 1.3 Add negative hook/secret/local-reference tests and verify a direct main update is blocked through API
    inspection, not a live push.
    - _Requirements: 2.5, 3.4, 10.2_
  - [ ] 1.4 In this server repository, select a maintained Python dependency-license checker, pin it to one exact
    version in the `dev` extra, and configure an explicit approved-license policy. Add fixture tests plus matching
    fail-closed pre-commit and required CI checks over both the hash-locked runtime dependencies and the complete
    resolved development environment; a missing checker or missing, unknown, unreadable, or unapproved license
    result must fail. Document the selected checker, approved-license policy, and matching local/CI commands in
    `README.md`. If this adds or renames a CI job, update and verify protected-branch required checks through the GitHub
    API in the same PR. Do not add the dev-only checker pin or its future bumps to `CHANGELOG.md`; record any resulting
    runtime dependency or shipped-image change under the normal add-on changelog policy.
    - _Requirements: 10.2_
  - Demo: both repositories have green baseline CI and protected main.

- [ ] 2. Qualify Apple-silicon whole-system power
  - _Requirements: 1.3, 1.4, 2.5, 3.4, 9.1, 9.2, 9.3_
  - [ ] 2.1 Write failing tests for `PowerSource`, units, missing/stale/non-finite/implausible telemetry, source loss,
    and AC-to-battery and battery-to-AC transitions. Then implement the foreground Rust probe so every unavailable or
    unqualified mode produces the explicit `WHOLE_SYSTEM_UNAVAILABLE` result rather than silence or subsystem watts.
    - _Requirements: 1.4, 9.1, 9.2_
  - [ ] 2.2 Add synchronized CSV/reference collection and run the full five-minute idle, CPU-load, and GPU-load AC
    matrix against an external wall meter; every run is a mandatory release gate at ±10%. Run the same battery matrix
    before claiming battery support, using a predeclared credible independent power or energy reference and the same
    per-run bound. If the battery matrix cannot run, has no credible reference, or fails, record battery as unsupported
    and verify that transition into battery produces a signed `WHOLE_SYSTEM_UNAVAILABLE` immediately and on the normal
    cadence. Record CPU and physical memory. Use neutral hardware IDs in every public artifact.
    - _Requirements: 2.5, 3.4, 9.1, 9.2, 9.3_
  - [ ] 2.3 Publish a go/no-go Apple-silicon report that blocks release unless the complete AC matrix passes and names
    battery as supported only when its complete independent-reference matrix passes. Otherwise require an explicit
    battery-unsupported declaration and passing signed-unavailable transition/cadence tests. Probe Intel separately
    with independent outcomes for each power mode. Stop product implementation if AC misses ±10%, overhead exceeds
    0.5% CPU or 30 MiB, or transition into any unsupported mode cannot emit unavailable reliably.
    - _Requirements: 1.3, 1.4, 3.4, 9.1, 9.2, 9.3_
  - Demo: reproducible qualification report naming each qualified and fail-closed unsupported mode, or an explicit
    unsupported result.

- [ ] 3. Implement protocol primitives and normative vectors
  - _Requirements: 2.3, 2.7, 3.1, 4.3, 4.5, 6.1, 7.5a, 7.5b, 7.5e, 7.5f, 8.5, 10.1_
  - [ ] 3.1 Write the threat model, API/profile spec, strict JSON schemas, the complete exact reporter and control
    method/path/request-cap/successful-response-cap/success-status tables and closed RFC 9457 `code` values, and failing
    shared vectors for every signed transcript, bilateral nonce commitment/reveal, SAS phrase, control recovery,
    bootstrap commit/reveal/confirm, reporter/control key-rotation challenge and completion, managed-token
    issue/status/revoke recovery, managed-enrollment proof, and MQTT v2 AEAD envelope. Fix every canonical byte, field
    length/order, digest, thumbprint, `key_id` encoding, generation, HKDF output, and rendered word; mutate each one
    independently and require rejection.
    - _Requirements: 2.3, 2.7, 4.3, 4.5, 6.1, 7.5a, 7.5b, 7.5e, 8.5_
  - [ ] 3.2 Implement Python and Rust P-256 JWK, RFC 7638, RFC 9530, and narrow RFC 9421 primitives against the
    vectors. The post-pair control profile shall cover purpose, active key generation, exact uppercase method, canonical
    authority, exact path, whole query and every individually named query value, content type/digest, bounded
    `created`/expiry, signed nonce, and signature parameters. Reject every one-field mutation, route/purpose reuse,
    replay outside the endpoint's exact-retry rule, and any rewritten, normalized, aliased, or mismatched target.
    - _Requirements: 3.1, 6.1, 7.5f_
  - [ ] 3.3 Implement requirement 4.3's normative SAS rendering: pin and vendor the exact 256-entry PGP Even Word List
    and Odd Word List identified there, preserve their zero-based index order and ASCII spelling, and render output
    bytes `b[0]` through `b[5]` with the exact even/odd position selection defined there. Document that this profile
    extends RFC 6189's two-word B256 rendering to six bytes/six words, and run cross-language/tamper tests that cover
    every position, list, index, and rendered word.
    - _Requirements: 4.3, 6.1_
  - [ ] 3.4 Obtain independent security review; block distribution until approved.
    - _Requirements: 10.1_
  - Demo: Rust-signed fixture verifies in Python; any one-byte mutation fails.

- [ ] 4. Add configuration, durable PKI, roster, and one-use token state
  - _Requirements: 1.5, 2.2, 3.1, 4.5, 5.1, 5.2, 5.3, 5.4, 6.5, 7.3, 7.6, 8.1_
  - [ ] 4.1 First extract and promote the existing `state_store.py` `_write_atomically` and `_fsync_directory`
    mechanism into a shared persistence helper, keep `EnergyStateStore` using that helper, and preserve its regression
    tests. Then test and implement the atomic owner-only PKI, roster, token, revocation, control-authority, control-SPKI,
    and energy stores with that shared helper rather than a parallel writer. Preserve same-filesystem `O_EXCL`
    mode-0600 temporary creation, file flush and fsync, atomic replace, directory fsync, failure cleanup, and the
    existing exact-boundary/interrupted-write tests; add fail-closed permission, partial-write, generation-rollback,
    generated reporter CA/leaf, separate mode-independent self-signed control-SPKI identity, transition/revocation
    generation; managed-token public operation/revocation state; absolute-expiry pairing credentials and consumed
    digests retained only through expiry plus grace; separately keyed integrity-protected bootstrap-confirm records
    committed atomically with authority and credential consumption; durable reporter/control rotation challenges;
    durable control/recovery replay and exact-result records; 24-hour action results followed by 30-day compact
    markers; one-sided restore generation/SPKI mismatch; and explicit
    full-system trust-rollback documentation/tests. Add encrypted backup/restore consistency coverage across multiple
    secret generations and prove restore never extends an embedded expiry.
    - _Requirements: 2.2, 5.1, 5.3, 5.4, 5.5, 7.5a, 7.5d, 7.5e, 7.5f_
  - [ ] 4.2 Test and implement reporter lifecycle, stable IDs, public-key rotation, revocation tombstones, and
    capacities.
    - _Requirements: 2.2, 3.1, 8.1_
  - [ ] 4.3 Test and implement only the managed-enrollment durable-store primitives: owner-only atomic records for
    unique hashed token verifiers and bounded public operation/revocation state, expiry-plus-grace lookup and
    compaction, one-live-record-per-subject uniqueness, the 2,000-unexpired-operation capacity bound, and a generic
    atomic compare-and-consume transaction primitive. Use synthetic records and crash injection to prove restart
    recovery, rollback rejection, failure cleanup, no lifetime extension, and exclusion of raw tokens and raw responses
    from storage. This increment does not implement token generation, issuance/status/revoke business logic,
    managed-proof verification or consumption, roster creation, route schemas, production routes, or feature
    enablement; task 13.1 owns that complete production behavior and its end-to-end response-recovery tests.
    - _Requirements: 4.5_
  - [ ] 4.4 Write failing model, parser, canonical-printer, lossless-compatibility-printer, and bidirectional Supervisor
    schema-agreement tests for the exact top-level `reporters`/nested `tls` shape, scalar defaults and ranges,
    generated/operator path conditionals, and `/ssl` path confinement. Submit pre-feature raw options without
    `reporters`; assert their before/after raw shape is unchanged through parse plus compatibility print, their parsed
    model has disabled reporter defaults, and canonical print adds the complete defaulted block. Include the optional
    credential-marked `control_pairing_secret`; its non-null value is the complete canonical ASCII
    `control_pairing_credential`, accepted only as
    `remcp1.<expires_at>.<43-character-unpadded-base64url>`, where the final field decodes to the exact 32-byte
    `control_pairing_key` and the absolute expiry is no more than 10 minutes after generation. Define a field-specific,
    write-only printer contract for this new ephemeral credential: whenever either current-shape printer emits the
    `reporters` block, it emits `control_pairing_secret: null` instead of unwrapping the `SecretStr`, so neither the
    complete `control_pairing_credential` nor `control_pairing_key` enters the returned document. Keep the existing
    model-driven recursive printer behavior and exact print/parse assertions unchanged for every pre-feature field,
    including nested fields and pre-existing credential-marked fields; add focused tests proving that a non-null
    `control_pairing_secret` is the only field replaced with null. Assert the option is masked by Supervisor,
    registered for redaction, absent from logs and diagnostics, and rejected on malformed
    prefix/time/length/alphabet/padding. Prove it never gets a new lifetime after options reload, print/parse, or
    restore. Assert changing any
    reporter setting migrates legacy input to that current shape. Then add `ReporterSettings` and
    `ReporterTlsSettings`, update `ConfigurationObject` and `remote_energy_meter/config.yaml` to accept both the legacy
    omission and current shape, retain omission provenance for compatibility printing, and preserve the existing flat
    static `devices` shape. Change static device-list bounds to 0–50, accepting zero only when reporters are enabled;
    prove a reporter-only configuration can start while an empty fully disabled configuration still fails. In the same
    increment, document the exact options, defaults, validation failures, legacy-omission compatibility, and
    reporter-only configuration in `remote_energy_meter/DOCS.md`; document schema-test maintenance in `README.md`; and
    add the user-visible configuration change under `CHANGELOG.md`'s Unreleased section.
    - _Requirements: 1.5, 5.2, 6.5, 7.3, 7.6, 8.1_
  - [ ] 4.5 Wire disabled-by-default reporter initialization into application startup and cleanup only after task 4.4's
    configuration contract is enforced.
    - _Requirements: 1.5, 5.1, 7.3, 7.6_
  - Demo: synthetic identity and generated PKI survive restart; pending/session state does not; configuration
    parse-print round trips and schema agreement pass; no secret enters logs/MQTT.

- [ ] 5. Add privilege-separated TLS edge
  - _Requirements: 2.1, 2.3, 2.4, 6.5, 6.6, 6.7, 6.8, 6.9, 7.1, 7.2, 7.3, 7.4, 7.6_
  - [ ] 5.1 Test and implement separate non-root reporter and control s6 edges, distinct AppArmor child profiles,
    mutually authenticated encrypted Unix IPC channels, separate per-boot profile-scoped IPC credentials, and core-side
    peer/request revalidation. Give the control edge only its control leaf key and IPC credential and the reporter edge
    only its reporter leaf key and IPC credential. Add mounted sentinels and explicit AppArmor/file-permission denial
    tests proving each edge cannot directly read `/data`, HA configuration, reporter CA signing material, enrollment
    token hashes/verifiers, control verifiers, Supervisor credentials, the other profile's keys/credential, or unrelated
    secrets; prove a reporter IPC credential/method cannot cross into control dispatch or vice versa, and fail closed on
    missing credentials or widened permissions.
    - _Requirements: 2.1, 2.3, 2.4, 7.1, 7.2_
  - [ ] 5.2 Test and implement the TLS 1.3-only 8443 reporter listener and internal 8444 control listener in their
    respective isolated edge processes, with
    every normative request/response body, exact method/path and successful status, closed RFC 9457 `code`, item-count,
    connection, per-source, handshake, target/header/signature, parser, timeout, long-poll, rate, queue, and complete
    RFC 9457 error-response limit of 2 KiB including headers and body. Add exact-boundary/one-over, wrong-method/path,
    status, and problem-code tests for each reporter and control endpoint, including 64 reporter connections globally
    and four per source, streamed/chunked body abort, incremental pages capped at 100 events and 64 KiB without
    unbounded intermediates, provisional-control TLS downgrade/fallback, route and response-class confinement,
    post-pair provisional rejection, slowloris, single-source flood, and distributed-source flood; prove caps apply
    before expensive work and valid traffic retains bounded service.
    - _Requirements: 2.3, 2.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 7.2, 7.4, 7.5a, 7.5e_
  - [ ] 5.3 Add opt-in `8443/tcp: null`, verify suggested 58443 is free on the target, retain no host network, and
    verify that `reporters.enabled` and the external port mapping remain independently opt-in. Update
    `remote_energy_meter/DOCS.md` with enablement, conflict-check, firewall/exposure, and safe-disable/rollback steps;
    update `README.md` with edge/AppArmor test procedures; and record the shipped listener isolation and opt-in port in
    the Unreleased changelog.
    - _Requirements: 7.3, 7.6_
  - Demo: the disabled-by-default listener completes only TLS 1.3 handshakes when enabled; separate reporter/control
    edge identities and profiles are observable, and neither edge can read any mounted forbidden-state or secret
    sentinel.

- [ ] 6. Implement enrollment and reciprocal human pairing
  - _Requirements: 2.2, 2.3, 4.2, 4.3, 4.4, 4.5, 4.6, 6.5, 6.7, 6.8, 6.9_
  - [ ] 6.1 Test/implement two 32-byte nonce commitments, fixed reveal order, exact length-delimited transcript and
    six-word derivation, expiry/rates, at most 16 pending enrollments globally and two per source, provisional
    endpoint-only TLS scope, mode-specific post-SAS trust persistence, and the closed public-metadata schema. Cover every fixed vector and
    one-field/one-byte mutation, exact UTF-8 byte boundary, one-over multibyte value, non-NFC/forbidden Unicode,
    unregistered token, commitment mismatch, reveal reordering, restart, and expiry. Fix interactive `server_identity` as
    exact `https://<canonical-lowercase-IDNA-A-label-host>:<canonical-explicit-port>`; test the reporter compares it
    with its configured connection target before sending its commitment, the core compares it with its configured
    reporter identity before returning its commitment or allocating state, and both persist/use the same fields after
    SAS. Reject and
    mutation-test redirects, cross-origin substitution, userinfo, IP/Unicode/percent-encoded or case/number alternate
    encodings, a trailing-dot/empty host, implicit/zero/out-of-range/leading-zero port, and any path (including `/`),
    query, or fragment.
    - _Requirements: 2.3, 2.7, 4.3, 4.6, 5.5, 6.5, 6.7, 6.8, 6.9, 6.10_
  - [ ] 6.2 Test/implement reciprocal HA/reporter confirmation and atomic activation so neither an HA-only approval nor
    a fake approved response establishes server trust or authorizes reports. In the same server and reporter increments,
    document interactive pairing, phrase comparison on both surfaces, cancellation/expiry/failure behavior, revocation,
    and the requirement to re-pair after identity loss or compromise; add the shipped enrollment behavior to both
    Unreleased changelogs.
    - _Requirements: 2.2, 2.3, 4.3, 4.4_
  - [ ] 6.3 Build an executable cross-language managed-enrollment conformance harness using only in-memory fake server and
    reporter adapters. Exercise the stateless core challenge, token-verifier-derived transcript proof, and proposed-key
    signature bound to core nonce, reporter nonce, canonical server identity, exact trust mode, CA-SPKI artifact value,
    and exact JWK. Cover honest flow, JWK/server substitution, proof and nonce replay, forged/expired challenge,
    expired/revoked/used token, compromised-edge attempts, and atomic failure without partial token/identity state.
    This task lands fixtures and the executable harness only; it adds no production server route, reporter client, MDM
    integration, feature enablement, user documentation, or changelog entry. Task 13.1 owns that complete production
    increment so this task cannot leave dormant or orphaned production code.
    - _Requirements: 2.2, 2.3, 4.5_
  - Demo: real/fake client pairing; altered certificate/key blocks approval.

- [ ] 7. Add authenticated HA control and discovered-card flow
  - _Requirements: 4.1, 4.4, 7.4, 7.5, 7.5a, 7.5b, 7.5c, 7.5d, 7.5e, 7.5f, 8.6b, 8.6c, 8.6d, 8.6e, 8.6f, 8.6g, 8.6h, 8.8_
  - [ ] 7.1 Land control bootstrap as one complete increment. First write vectors and negative tests for an
    integration-flow-generated `remcp1.<expires_at>.<43-character-secret>` credential entered into the masked add-on
    option; strict canonical credential validation and redaction; exact decoded 32-byte `control_pairing_key` use for
    every HMAC; SHA-256 over the full canonical ASCII credential as the distinct `consumed_credential_digest`;
    discovery as routing metadata only; the dedicated observed control-SPKI identity; an integration-generated P-256
    control key; bilateral HMAC-SHA-256 nonce commitments and opposite-role proofs over the canonical bounded
    transcript; durable integration-key authorization and SPKI pinning only after both proofs; expected add-on slug
    binding; and fail-closed 10-minute memory-only pending state. Permit TLS 1.3 encryption-only provisional transport
    only while that fresh secret is active, dispatch only the three bootstrap routes, bind the handshake-observed SPKI,
    then close and reconnect under the exact pin. Test TLS downgrade and pin-failure fallback, route/method confusion,
    forbidden redirects/upgrades/response classes, and every provisional request after pairing.

    In the authorizing transaction store the consumed credential digest through its embedded expiry plus five minutes;
    prove reuse before expiry fails, expiry is absolute across restart/restore, a missing tombstone from an old backup
    cannot make an expired credential usable, and compaction after the grace is safe. The secret bytes never cross the
    network and are never persisted by the integration. After one success, disable bootstrap and refuse any still-live
    consumed credential. Store the integration private key non-exportably or encrypted in protected credential storage
    with least privilege,
    using the shared atomic persistence helper promoted from `state_store.py` rather than a second writer; test encrypted
    backup/restore binding, omission from logs/diagnostics/exports/errors, deletion after replacement/revocation, and
    fail-closed cleanup errors. Also test fixed vectors that distinguish the decoded HMAC key from the full-credential
    digest; spoofed discovery/add-on slug, direct internal calls, proxy/header claims,
    secret transmission, fake approved responses, proof/key/SPKI/nonce mismatch, reveal order, restart/expiry,
    unauthorized-channel requests, non-admin attempts, and every canonical-vector mutation.
    - _Requirements: 2.1, 2.4, 5.3, 7.4, 7.5, 7.5a, 7.5c_
  - [ ] 7.2 Land the complete post-pair signed-control and roster-management increment. Complete the nested subtasks
    in order so the signed-control profile is enforced before any route business logic.
    - [ ] 7.2.1 Test/implement the post-pair signed-control profile. For every route, verify the route-specific
      purpose/tag, active integration-key thumbprint and generation, exact method, canonical authority, exact path,
      whole query plus every individually named query input, content type/digest when a body is present, bounded
      `created`/expiry, signed random nonce, and fixed signature parameters. Define the roster-event wire query and
      both snapshot query forms exactly as in 7.5f. Durably couple nonce consumption, state changes, and the
      endpoint-specific result or rejection policy. For mutating routes, test the exact-retry cache-return path before
      active-key/generation validation: bind exact request hash/nonce/purpose, original/proposed keys and generations,
      status/response bytes, and expiry; return only an unexpired exact match without reauthorization or business logic,
      so a control-key-rotation retry remains possible after the old key is superseded. For MQTT key provisioning,
      encrypt the raw response at rest with XChaCha20-Poly1305 using a unique nonce and a separately managed random
      256-bit wrapping key; bind record metadata as AAD; use owner-only atomic storage; exclude every key, ciphertext,
      plaintext, and temporary copy from logs/diagnostics/exports/backups; fail closed on key, permission, or tag error;
      cap the record at the original request expiry and 60 seconds from creation; and delete every cached copy on
      acknowledgement, abort, expiry, or contained-key retirement/revocation. Test restart/exact retry, nonce/tag/AAD
      alteration, missing wrapping key, widened permissions, rejected wrapping-key replacement while a record is live,
      atomic old-key removal after replacement, mandatory expiry without extension, cleanup failure, and proof that no
      raw response survives retirement. Cover restart replay, exact
      retries, cross-route/purpose reuse,
      and a one-field mutation of every component and parameter for every route.
    - [ ] 7.2.2 Test/implement roster event polling with the uint64 roster cursor and contiguous generation
      long-poll. Cover every specified increment event; exact request/response fields, methods, paths, successful
      statuses, and RFC 9457 `code` values; 100-event/64-KiB bounded construction; immediate, partial-page,
      first-event, and 25-second timeout completion; future/compacted cursors with exact `409`
      `snapshot_required`; and snapshot fallback.
    - [ ] 7.2.3 Test/implement snapshot synchronization with immutable five-minute snapshots in canonical
      reporter-ID order, random 128-bit snapshot IDs, and opaque authenticated continuation tokens bound to
      generation, next page/index offset, total, and expiry. Cover deterministic same-token retries, concurrent
      roster mutation, 0/1/100/101/1,000-record completion, 64-KiB page splits, final digest/completion, idempotent
      exact same-token duplicate retries without duplicate records, and discard-and-restart on missing, conflicting
      duplicate, or non-successor pages, interrupted transfer, stale/altered/expired token, or server restart.
    - [ ] 7.2.4 Test/implement approve/reject/revoke/delete roster actions with canonical operation IDs/digests.
      Retain the full non-secret result for the half-open first 24 hours, then logically treat it as a digest-only
      marker for the next 30 days. Physical compaction shall atomically delete the response body, but lookup shall
      return exact 410 `operation_result_expired` for valid freshly signed exact retries throughout the marker interval
      even when cleanup lags, and shall ignore a physically retained expired marker after that interval before falling
      through to ordinary authentication/stale-generation behavior. Test immediately before, exactly at, and
      immediately after both boundaries with compaction completed and deliberately lagging, restart in each interval,
      invalid authentication precedence, changed digest conflict, and no reapplication. Keep this retention independent
      of the encrypted MQTT key-bearing cache's mandatory maximum of 60 seconds.
    - [ ] 7.2.5 Test/implement reporter-key rotation through exact signed challenge, reporter completion, and HA-admin
      confirmation methods, paths, strict schemas, statuses, and caps. Durably bind client operation ID, rotation ID,
      proposed key, generations, fixed expiry, and core-generated nonce before old/new IEEE-P1363 transcript signatures
      can be submitted. Enforce one live challenge per reporter, restart survival, and atomic confirmation. Add end-to-
      end challenge acquisition and completion plus dropped-response, exact/changed duplicate phase, out-of-order,
      concurrent attempt, expiry-boundary, restart-before-completion/retry, and pre/post-commit crash tests, along with
      negative conformance for every omitted or changed transcript field/length/order, signature, confirmation,
      key/thumbprint, generation, nonce, digest, and replay.
    - [ ] 7.2.6 Separately test/implement control-key rotation through exact signed challenge and dual-signed completion
      methods, paths, strict schemas, statuses, and caps. Durably bind client operation ID, rotation ID, exact current/
      proposed JWKs and thumbprints, generations, fixed expiry, and core-generated 32-byte nonce before accepting the
      current key, proposed key, and one-use authenticated HA-admin confirmation over the same at-most-4-KiB transcript.
      Atomically swap authority, consume replay state, and invalidate old leases/requests. Add end-to-end challenge
      acquisition and signed completion plus response-loss, exact/changed duplicate phase, out-of-order, concurrent
      attempt, expiry-boundary, restart-before-completion/result-retry, and pre/post-commit crash tests, and mutate every
      field, length/order, signature, confirmation, key/thumbprint, generation, nonce, digest, and replay binding.
    - _Requirements: 3.1, 4.4, 6.10, 7.5b, 7.5f, 8.6b, 8.6c, 8.6d, 8.6e, 8.6f, 8.6g, 8.6h_
  - [ ] 7.3 Test/implement `SOURCE_INTEGRATION_DISCOVERY` cards and Core zeroconf advertisement of the exact
    `_rem-reporter._tcp.local.` service type, with manual URL fallback. Keep the existing config entry unique ID and
    lifecycle; create transient `reporter:<reporter_id>` flows from the authenticated pending roster, suppress
    duplicates across reconnect/restart, and complete approval/rejection without `async_create_entry`, reload,
    replacement, or unload. Verify no actionable flow appears without the loaded instance entry and its authenticated
    live control client. Update `remote_energy_meter/DOCS.md` with discovery, manual fallback, approve/reject/revoke/
    delete behavior, and recovery when the authenticated control channel is unavailable; update `README.md` with the
    integration control/discovery test workflow; and add the shipped Home Assistant flow to the Unreleased changelog.
    - _Requirements: 4.1, 4.2, 4.4, 8.8_
  - [ ] 7.4 Test/implement bootstrap's exact commit/reveal/confirm methods, paths, schemas, statuses, phase ordering,
    memory-only pending retries, and fixed expiry. In the authorizing transaction, atomically persist authority,
    credential consumption, and a separately keyed integrity-protected exact confirm result; look it up by bootstrap ID
    and request hash before consumed-secret rejection through the original expiry, then retain only the replay marker
    through expiry plus five minutes. Cover dropped response in every phase, exact/changed and duplicate/out-of-order
    phases, expiry immediately before/at the boundary, pending restart, exact and changed confirm retry after restart,
    and crashes immediately before and after final commit.

    Then test/implement control certificate renewal under unchanged SPKI and prove generated,
    operator private-CA and operator public-PKI reporter certificate modes cannot establish or change the
    mode-independent control identity; implement old/new-SPKI signed planned transition with bounded overlap and
    durable generation advancement. Implement recovery as the exact commit/reveal/revoke schemas and
    `rem-control-recovery-v1` transcript: encryption-only TLS confined to those routes while a fresh credential is
    active; HMACs keyed by its exact decoded 32-byte secret and consumed tombstones computed as SHA-256 of the full
    canonical ASCII credential; fixed vectors distinguishing those bytes for bootstrap and recovery; current key
    thumbprint/generation, current and observed SPKI/generations, exact scope,
    independent committed nonces, fixed expiry, and next recovery generation; integration proof verification before one
    atomic revocation; and core proof verification by the integration before success. Persist nonce/transcript/result
    tombstones and retain each consumed recovery credential digest through its embedded expiry plus grace; require a
    second fresh credential for subsequent bootstrap. Cover cross-instance, cross-purpose, field/scope substitution, replay before and after
    restart, expiry boundaries, loss/compromise, stale/unsigned transitions, one-sided restore mismatch, documented
    full-system trust rollback, crashes at every durable boundary, exact revoke
    retry, interrupted replacement, failed key deletion, and proof that re-pairing never reactivates old authority.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 7.5a, 7.5d, 7.5e_
  - Demo: fake reporter produces a native Discovered card and approval activates it without creating another config
    entry.

- [ ] 8. Build signed macOS daemon/menu application shell
  - _Requirements: 3.2, 3.3, 3.4, 3.5_
  - [ ] 8.1 Test/implement System-Keychain non-exportable P-256 identity and signed SMAppService daemon.
    - _Requirements: 2.2, 3.2, 3.3_
  - [ ] 8.2 Test/implement bilateral audit-token/designated-requirement XPC authentication followed by a fresh
    ephemeral P-256 ECDH handshake and HKDF-SHA-256 directional XChaCha20-Poly1305 keys and nonce prefixes bound to
    both code identities, handshake, and random session ID. Use each directional 16-byte prefix plus the next uint64
    big-endian counter as the 24-byte nonce, and bind version, session, direction, enrollment/status-only method, and
    counter as AAD. Add negative tests for plaintext/fallback attempts, either unauthenticated or wrongly signed peer,
    altered ciphertext or AAD, skipped/duplicate/non-increasing/wrapped counters, nonce reuse, replay from a prior
    session, failure to erase/invalidate session state on disconnect, direction/method substitution, and arbitrary
    command/file requests.
    - _Requirements: 2.4, 3.2_
  - [ ] 8.3 Add Developer ID, hardened runtime, notarized package and release rejection of unsigned artifacts. Record
    the shipped packaging/security behavior in the reporter Unreleased changelog.
    - _Requirements: 3.3, 3.4, 3.5_
  - [ ] 8.4 In the reporter repository, add a user guide for verified package installation and removal, daemon/menu-app
    permissions, log locations and redacted diagnostic collection, common startup/permission failures, explicit package
    or MDM updates, and verified rollback without a self-updater. Add a developer guide with the exact supported
    toolchain, local build/test commands, foreground-spike restrictions, Developer ID signing, hardened-runtime,
    notarization, package inspection, and release-verification commands. Keep unimplemented enrollment, measurement,
    certificate, and managed-deployment modes explicitly unavailable and fail closed; later implementation increments
    must update these guides and the reporter Unreleased changelog before enabling each mode.
    - _Requirements: 3.3, 3.4, 3.5, 10.2_
  - Demo: install/reboot; daemon runs before login and menu app shows status after login.

- [ ] 9. Establish the secured MQTT v2 destination before report ingress
  - _Requirements: 1.1, 2.1, 2.4, 7.5, 8.1, 8.5, 8.6a, 8.6b, 8.6c, 8.6d, 8.6e, 8.6f, 8.6g, 8.6h,
    8.6i, 8.7, 8.8, 8.9_
  - [ ] 9.1 Test and implement one complete roster-lifecycle path from approved/revoked/deleted reporter state through
    bounded XChaCha20-Poly1305 core publications with canonical 16-byte/22-character key IDs bound one-to-one to
    uint64 generations in AAD, authenticated control authorization and key rotation, verified-TLS broker hops, durable replay high-water marks, and the v1+v2 HA coordinator. The same increment shall include the
    process-wide availability topic and AEAD retained QoS 1 LWT, matching process/control gates, authenticated paginated
    roster reconciliation, and the bounded deletion-cleanup queue; none of these security dependencies may ship as a
    later add-on to an enabled v2 path. MQTT key provisioning shall use 7.5f's separately wrapped AEAD retry record,
    mandatory 60-second maximum, fail-closed owner-only atomic storage, backup/diagnostic exclusion, and deletion of
    every response copy on acknowledgement, abort, expiry, or contained-key retirement/revocation. Approval shall
    create a dynamically registered HA reporter device with power and energy entities initially unavailable beneath the stable `(DOMAIN, "hub:" + instance_id)` hub. Preserve the
    existing one-config-entry lifecycle, flat static device identifiers and unique IDs, and v1 behavior. Document the
    hub/child entity model, initial/offline/revoked/deleted states, retained history, and mixed static/reporter behavior
    in `remote_energy_meter/DOCS.md`; document v2 fixtures and compatibility tests in `README.md`; and add the shipped
    authenticated MQTT v2 and registry behavior to the Unreleased changelog.
    - _Requirements: 1.1, 2.1, 2.4, 7.5, 8.1, 8.5, 8.6a, 8.6b, 8.6c, 8.6d, 8.6e, 8.6f, 8.6g, 8.6h,
      8.6i, 8.7, 8.8, 8.9_
  - [ ] 9.2 Cover startup, clean shutdown, reconnect, broker disconnect, and unclean core exit with adversarial delivery
    order. Assert process offline durably closes its generation and gates all reporter entities before retained or
    delayed per-reporter/online state from that generation can be exposed; only a greater process generation may
    recover. Also cover HA being offline during deletion, stale retained ciphertext before and after delayed QoS 1
    cleanup, partial/changing snapshots, deterministic continuation retries, 0/1/100/101/1,000-record snapshots,
    interrupted and stale continuations including core restart, cleanup-capacity rejection, acknowledgement,
    key-retirement compaction; wrapped provisioning-response exact retry across restart; wrapping-key separation;
    ciphertext/nonce/AAD alteration; missing key, tag, or owner-only permission; 60-second expiry without extension;
    exclusion from backup, logs, diagnostics, and support exports; cleanup failure; proof that acknowledgement, abort,
    expiry, and retirement/revocation remove every cached key copy; restarts at durable boundaries; mixed v1/v2; 1,000
    bounded configs; ciphertext/tag and
    bound-field substitution, malformed/padded/unknown/duplicate key IDs, ID/generation mismatch, replay/reordering,
    retained-state and key-generation rollback, control-lease expiry, interrupted key rotation, revocation, forged empty-retained deletion, and replay after durable tombstones.
    - _Requirements: 8.1, 8.5, 8.6a, 8.6b, 8.6c, 8.6d, 8.6e, 8.6f, 8.6g, 8.6h, 8.6i, 8.9_
  - [ ] 9.3 Add Home Assistant compatibility tests proving one config entry owns v1 and v2 concurrently, the hub
    identifier is stable across reload/restart, static devices remain flat with byte-for-byte existing identifiers and
    entity unique IDs, reporter entities use `rem_reporter:<reporter_id>:<role>`, and reporter devices have the
    specified `via_device_id`. Simultaneously register accepted static ID `reporter_<reporter_id>` and the matching
    reporter for every role; prove their underscore and colon unique IDs create distinct stable registry entries across
    reload/restart with no static migration. Also prove concurrent pending reporters use deduplicated transient flows, and flow completion creates no config
    entry. Run the existing companion-integration tests unchanged as regression coverage.
    - _Requirements: 1.1, 8.7, 8.8_
  - Demo: roster approval creates an unavailable native HA device without report ingress or another config entry; an
    unclean core exit makes all reporter entities unavailable, and a reconnect cannot expose retained reporter state
    before process/control gates.

- [ ] 10. Complete energy/staleness handling and then enable signed report ingress
  - _Requirements: 1.1, 1.4, 2.3, 2.6, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 7.6, 8.2, 8.3, 8.4_
  - [ ] 10.1 Land this as one atomic increment. First test and wire `PushReadingDestination`, monotonic energy,
    per-reporter transition serialization, and the min-heap watchdog through the already-live dynamic MQTT v2
    destination while the report endpoint remains disabled. Then implement the stateless five-minute
    challenge-allocation context, long-term-key-signed challenge requests, sessions, canonical report verification,
    sequence/idempotency, and restart invalidation. Enable report ingestion only after every accepted report has that
    complete production destination; do not land either an orphan input or an orphan output stage. In the same
    increment, document report availability/staleness, energy-gap behavior, restart/recovery behavior, operational
    ceilings, and troubleshooting in `remote_energy_meter/DOCS.md`; document report-conformance and watchdog testing in
    `README.md`; and add signed report ingestion and energy handling to the Unreleased changelog.
    - _Requirements: 1.1, 2.3, 2.6, 6.2, 6.3, 6.4, 8.2, 8.3, 8.4_
  - [ ] 10.2 Test strict bodies, targets, header counts/bytes, signature headers, available watts and the signed
    `whole_system_unavailable` union arm, every normative timeout/rate/concurrency/queue/error ceiling,
    one-challenge and one-status-poll limits, and RFC 9457 errors whose complete response, including headers and body,
    is at most 2 KiB. Include unsigned/forged challenge requests, stale/pre-restart allocation contexts, exact
    duplicate and changed nonce, replay after consume/expiry, and report retries with identical versus changed
    canonical identity fields. Assert an identical report retry returns the exact original successful status/body
    without repeating energy, state, or publication effects. Also cover victim-ID churn, reduced-cap exhaustion by
    valid identities, proof that invalid identities allocate nothing, slowloris, and single/distributed-source floods.
    Exercise the 1.0 W and 100000.0 W operational-ceiling configuration boundaries, one-over rejection, and a
    configured lower ceiling without weakening the protocol hard cap.
    - _Requirements: 1.4, 2.3, 6.2, 6.3, 6.5, 6.6, 6.7, 6.8, 6.9, 7.6_
  - [ ] 10.3 Add source-loss, AC-to-battery, battery-to-AC, explicit unavailable/recovery, revoke, restart, irregular,
    and concurrent transition tests. Assert that 89.999 seconds is fresh, exactly 90 seconds is stale, 90.001 seconds is
    stale, and a report tied with its watchdog deadline commits unavailable before recovery and rebaselines without
    energy across the gap. Move wall clock sharply forward and backward while monotonic time advances normally and
    assert energy/deadlines do not change; after restart, preserve cumulative energy and UTC receipt metadata but
    rebaseline monotonic state. Re-run all existing poller properties unchanged.
    - _Requirements: 1.1, 1.4, 2.6, 6.3, 6.4, 8.2, 8.3, 8.4_
  - Demo: a fake enrolled reporter's signed available and unavailable reports produce bounded, ordered HA
    power/energy/availability changes; replay and tampering are rejected deterministically.

- [ ] 11. Wire qualified Apple-silicon measurement into the reporter loop
  - _Requirements: 1.3, 1.4, 2.2, 2.3, 2.5, 2.6, 6.2, 6.3, 6.4, 6.6_
  - [ ] 11.1 Test and implement the qualified source, enrollment/session/report client, strict signed
    available/unavailable bodies, 30-second cadence, immediate source/mode-transition unavailable report, and jittered
    backoff.
    - _Requirements: 1.3, 1.4, 2.2, 2.3, 2.5, 2.6, 6.2, 6.3, 6.4, 6.6_
  - [ ] 11.2 Test sleep/wake, AC/battery transitions for every qualified and unsupported mode, source loss,
    network/DNS/certificate/server restart/revoke, and resource limits. Update the reporter user guide with the measured
    support matrix, explicit fail-closed unavailable modes, pairing and revocation/re-enrollment procedures, status and
    log interpretation, and troubleshooting for each tested failure. Update its developer guide with qualification and
    runtime test reproduction, and add the qualified source/runtime behavior to the reporter Unreleased changelog.
    - _Requirements: 1.4, 3.1, 6.3, 6.4, 9.2, 9.3_
  - Demo: a real Mac reports for one hour through power-source transitions, sleep/wake, and add-on restart; every
    unsupported interval becomes explicitly unavailable rather than silent.

- [ ] 12. Complete certificate modes and rotation
  - _Requirements: 4.3, 4.5, 5.1, 5.2, 5.3, 5.4, 5.5, 7.5d, 7.6_
  - [ ] 12.1 Test the exact mode matrix and implement generated CA/leaf lifetime and early renewal; operator mode's
    required certificate/key, optional CA, selection between operator public-PKI and operator private-CA modes, and
    `/ssl` path/symlink confinement at every boundary. Verify interactive provisional TLS remains endpoint-only until
    SAS, managed mode validates its configured CA pin or platform identity before proof, post-enrollment verifies the server plus reporter signatures,
    leaf pins are rejected, and each renewal retains the expected trust identity.
    - _Requirements: 4.3, 4.5, 5.1, 5.2, 5.5, 7.6_
  - [ ] 12.2 Test reporter-edge and control leaf renewal continuity, old-CA-signed transition, exact control-SPKI
    continuity/rotation, bounded overlap, loss/compromise/revocation/re-enrollment, authenticated control re-pairing,
    and encrypted backup/restore with mismatch and rollback rejection. Update `remote_energy_meter/DOCS.md` and the
    reporter user guide with the exact supported certificate/trust-mode matrix, CA-SPKI versus platform-PKI
    verification, rejected leaf pins, renewal/rotation, backup/restore, compromise recovery, and re-pairing behavior;
    update both developer guides with certificate fixtures and transition tests; and add the shipped certificate and
    recovery behavior to both Unreleased changelogs.
    - _Requirements: 5.1, 5.3, 5.4, 5.5, 7.5d, 7.5e_
  - Demo: rotate a server leaf without interrupting or re-enrolling reporters; operator public-PKI mode verifies
    through platform trust.

- [ ] 13. Add MDM fleet enrollment
  - _Requirements: 3.5, 4.5_
  - [ ] 13.1 Land managed enrollment as one atomic production increment; no server-only route, token-management
    production logic, or reporter-only client may land separately. The add-on owns the managed canonical server
    identity, exact CA-SPKI or platform-PKI trust mode, public token ID, and unique 256-bit single-use MDM-token
    generation, issuance, status, idempotent revocation, and expiry, with only the token verifier persisted. Bind
    issuance to client-generated operation and managed-subject
    IDs; require authenticated administrator authority and `Cache-Control: no-store`; persist through task 4.3's
    primitives only the bounded public operation/token state and verifier; enforce one issued token per subject and at
    most 2,000 unexpired operation records; retain status through expiry plus five minutes; and block replacement until
    the prior operation is revoked, consumed, or expired. Keep raw tokens and verifiers out of the edge, raw tokens and
    raw responses out of storage, and raw-token or artifact copies out of retained add-on state.

    In the same increment, the add-on implements the stateless core challenge, token-verifier-derived proof and
    proposed-key verification, and one durable transaction that consumes the proof/token and creates the roster
    identity. Inject crashes around every durable transaction boundary. Test a dropped issuance response by querying
    the exact signed public status by operation ID, revoking without a token ID or raw response, proving the old token
    is rejected, and only then issuing a replacement. Drop the revoke response too, recover its exact public
    result/status after restart, and prove duplicate or rebound operation IDs cannot mint, revoke, consume, or create a
    roster identity twice.

    The reporter owns selected-trust verification before proof, direct per-device MDM secret-store import,
    access-restricted handling, proof generation, proposed-key signing, and deletion of its local raw token and artifact
    after durable success or by expiry. The MDM side owns authenticated per-device delivery and removal after durable
    enrollment, revocation, or expiry. The one-time `rem-managed-enrollment-v1` JSON artifact expires after 24 hours;
    exact field validation rejects leaf pins. Any unavoidable staging uses non-backed-up, non-synchronized,
    mode-0600 files created with `O_EXCL` and deleted immediately after import. Tests cover exact fields, trust before
    proof, delivery and access restrictions, honest enrollment, substituted identity/JWK, proof and nonce replay,
    forged or expired challenge, expired/revoked/used/copied token, deletion timing, atomic failure without partial
    token/identity state, and crashes at each durable boundary.

    Task 13.1 alone enables managed enrollment in production, and only after the add-on, reporter, and MDM flow pass
    end-to-end tests together. In that same increment, update the add-on and reporter user guides with MDM-neutral
    deployment, update, rollback, revocation, token-expiry, secret-removal, and troubleshooting instructions; update
    both developer guides with reproducible managed-enrollment tests and release checks; and record the shipped
    behavior in both Unreleased changelogs. This atomic boundary prevents the conformance-only work in task 6.3 from
    becoming dormant production code and prevents either production side from landing orphaned.
    - _Requirements: 3.5, 4.5_
  - Demo: multiple clean profiles enroll distinct identities; copied/used token fails.

- [ ] 14. Qualify scale, abuse, and recovery
  - _Requirements: 6.6, 6.7, 6.8, 6.9, 8.1, 9.4_
  - [ ] 14.1 Build load/fuzz harness for 1,000 roster, 500 sessions, 17 reports/s, reconnect storms and hostile input.
    - _Requirements: 6.6, 6.7, 6.8, 6.9, 8.1, 9.4_
  - [ ] 14.2 Tune bounded queues/rates and verify no valid drops, poll starvation, or unbounded memory.
    - _Requirements: 6.7, 6.8, 9.4_
  - Demo: repeatable load report/dashboard with 500 reporters plus existing pollers.

- [ ] 15. Release Apple-silicon MVP
  - _Requirements: 3.3, 3.4, 3.5, 5.3, 10.1, 10.2, 10.3_
  - [ ] 15.1 Audit `remote_energy_meter/DOCS.md`, `README.md`, and the reporter repository's user/developer guides
    against the released behavior. Confirm they completely cover settings, reporter-only installation, install/removal,
    permissions, interactive and managed pairing/re-pairing, supported and fail-closed modes, certificate trust,
    privacy, backup/recovery, updates/rollback, logs/troubleshooting, and build/test/sign/notarize/release verification.
    Verify all applicable shipped feature, runtime-dependency, base-image, and image-affecting CI/security entries are
    already under each repository's Unreleased changelog, and that dev-only tool pins are absent. Fix only completeness
    or accuracy gaps found by this review. Complete external protocol review and run all repository gates and review
    reports.
    - _Requirements: 3.5, 5.3, 10.1, 10.2_
  - [ ] 15.2 Bump add-on/integration version 0.2.0 in `remote_energy_meter/config.yaml` and `pyproject.toml` together in
    the same release change, verify `CHANGELOG.md` has moved all applicable Unreleased entries into 0.2.0 without
    dropping any, and release the signed/notarized reporter 0.1.0 through the protected main branches.
    - _Requirements: 3.3, 3.4, 10.2, 10.3_
  - [ ] 15.3 Deploy only reviewed, merged main artifacts; live-test enrollment/report/offline/revoke/backup/rollback.
    - _Requirements: 5.3, 10.3_
  - Demo: fresh Mac → Discovered → pairing → signed power/energy → revoke/history retained.

- [ ] 16. Qualify macOS Intel separately
  - _Requirements: 1.3, 1.4, 9.1, 9.2, 9.3_
  - [ ] 16.1 Run the external-meter matrix; add a provider only if the same criteria pass, otherwise document unsupported.
    - _Requirements: 1.3, 1.4, 9.1, 9.2, 9.3_
  - Demo: qualified provider release or explicit fail-closed result.

- [ ] 17. Research/qualify PC providers
  - _Requirements: 1.3, 1.4, 9.1, 9.2, 9.3_
  - [ ] 17.1 Treat Windows/Linux separately; use documented sources and wall validation; never relabel package telemetry.
    - _Requirements: 1.3, 1.4, 9.1, 9.2, 9.3_
  - [ ] 17.2 Plug qualified providers into existing reporter protocol without server changes.
    - _Requirements: 1.1, 1.3, 1.4_
  - Demo: provider reuses enrollment/signatures/TLS/entities/fleet controls or explicitly remains unsupported.
