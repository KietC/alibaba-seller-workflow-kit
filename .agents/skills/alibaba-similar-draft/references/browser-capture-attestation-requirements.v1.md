# Browser capture attestation requirements

## Current decision

The Codex in-app Browser currently exposes useful URL, DOM, locator, screenshot and CDP observations, but no documented browser-backend signature or non-forgeable result handle. Persisted JSON and SHA-256 therefore prove integrity and cross-file consistency only. They do not authenticate that a local file originated from the browser.

`source`, `tool_result_id_sha256`, adapter flags, session hashes and local signatures must never promote evidence to `LIVE_ATTESTED`. Until a browser-backend attestation capability exists, production execution remains `BLOCKED_LIVE_ATTESTATION` and `implementation_ready=false`.

## Required producer boundary

A production producer must run inside the browser backend, not in caller-controlled JavaScript or a local Python/Node wrapper. It must collect the predefined visible DOM/ARIA, page identity and complete CDP window itself, redact secrets inside that boundary, and sign the canonical capture with a platform key unavailable to the local caller.

The signature payload must bind:

- one-time 256-bit challenge, run ID, evidence role and action-plan SHA-256;
- producer issuer/build/version and pinned key ID;
- browser surface, browser/tab/session identifiers;
- exact origin, path and query hash;
- page fingerprint and capture start/end/expiry;
- content-addressed DOM/screenshot/network artifacts;
- CDP cursor/sequence range with `has_more=false` and `truncated=false`;
- prior-attestation hash where a sequence is required;
- secret-redaction profile and zero final-write signals.

The signed representation must use RFC 8785/JCS or an equally strict canonical encoding. Duplicate JSON keys, invalid UTF-8, non-canonical numbers, path traversal, symlinks and read/stat time-of-check/time-of-use substitutions must be rejected.

## Required verifier

Every live validator must consume a common verified-attestation receipt instead of checking adapter booleans. The verifier must:

1. use a pinned production trust bundle, never a key embedded in the capture;
2. verify signature, issuer, key ID, producer allowlist, time window and one-time challenge;
3. recompute all artifact hashes and role-specific semantic material;
4. bind run/action plan/role/session/tab/page and reject cross-run reuse;
5. reject incomplete/truncated CDP windows and sequence gaps;
6. reject credentials, tokens, Cookie/Storage, secret-like fields and every final-write signal;
7. fail closed when the trust bundle or verifier is unavailable.

Coverage is required for zero-write probe, `schema.get`, current values, pre-save readback, non-target snapshot, visible Save Draft event, the exact paused request, save response, inventory pages and post-refresh readback. Attesting only post-save inventory is insufficient.

## Test boundary

Deterministic test keys may produce `TEST_ATTESTED` only in isolated tests. Production code must reject test keys. The existing loopback harness produces `LOCAL_CONTRACT_VERIFIED` and can never produce `LIVE_ATTESTED`.
