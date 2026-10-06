# Guarded and portable browser workflow

## Separation of trust domains

The distributable plugin contains code, schemas, a logical adapter, tests, and offline dependencies. The private workspace contains product evidence and materials. The signed-in Codex in-app Browser owns the live session. These three domains must not be merged into one archive.

The target machine must rebind private data by relative path plus SHA-256, manually sign in to Alibaba, and produce a new current-session probe. A copied browser profile or old probe grants no authority.

## Inputs and compiled artifacts

- `workspace_profile`: machine-independent private-data paths and expected content hashes.
- `target_model` and `template_product_id`: per-run intent; the template is form structure only.
- `approval_decision`: explicit field/asset decisions; silence is rejection.
- `approved_source_manifest` and `approved_asset_manifest`: canonical compiler output.
- `commercial_decision`: mandatory source for approved price/MOQ; at least three normalized strict comparables.
- `draft_write_approval`: independent, expiring approval bound to the target, run, template and compiled input hashes; permits one visible-UI draft save and never a final write.
- `run_spec`: `save_draft`, one draft maximum, direct endpoint write false, final write false.
- `browser_action_plan`: immutable bindings, clear/write/upload phases, current-probe requirement, visible draft-save action, and hard-denied final actions.

`PARTIAL_DRAFT_FILL_APPROVED_ONLY` may write only explicitly approved fields. Price and MOQ stay blank unless separately qualified. It cannot authorize Save Draft: the run must reach independently recomputed `VALIDATED` first. If an unexpected external draft is nevertheless observed, preserve it and use a separate recovery workflow; never skip the gate or create a second draft.

## Pass A: current-session zero-write probe

1. Bind the existing in-app Browser tab by exact origin/path and record online/draft counts.
2. Install fail-closed observation/interception before opening the exact similar-post URL.
3. Classify by exact host, path, API method parameter, request body shape and observed intent. HTTP verb alone is not authoritative because official Alibaba read APIs may use POST. During zero-write probing, abort every request classified as a mutation or unknown write and record the redacted path, method, body hash, initiator, and decision.
4. Confirm `post.alibaba.com`, `pubType=similarPost`, exact template ID, login validity, and no CAPTCHA.
5. Capture redacted URL, page fingerprint, ARIA/DOM labels, inherited values, validation messages, media-slot counts, and bundle hashes. Never capture Cookie/Storage/token/signature values.
6. Confirm both counts are unchanged and every observed write was blocked; require a non-empty captured request list, a recomputable page fingerprint, exact route/query binding and a current v3 probe verification receipt.
7. Treat the three probe conclusions independently: `zero_write_confirmed=true` proves only absence of writes; the exact READ overlay may then yield `execution_authorized=true`, but production execution additionally requires `production_execution_authorized=true`, both authorization-error arrays empty, and every captured request decision exactly `ALLOW`. Any `DENY`, `HARD_DENY`, `UNKNOWN`, `ALLOW_UI_ONLY`, missing decision, empty classification list, or unready production adapter is a hard execution stop even when status remains `PASS_ZERO_WRITE`.
8. Derive the official current-values ledger from a separate raw browser-observation capture. Recompute every observation and capture-body hash, and require the redundant ledger values/states/observation IDs to match exactly. A self-reported Codex source remains `LOCAL_CONTRACT`; rules may pass, but this cannot authorize execution.
9. Stop on ambiguity or drift. Do not reuse locator handles after navigation or major DOM updates.

## Pass B: hash-bound visible draft write

1. Recheck login/CAPTCHA, route, duplicate-draft search, adapter version, page fingerprint, endpoint policy, action-plan bindings, approved manifests, asset bytes/hashes, counts, the v3 probe execution gate, and the unexpired independent draft-write approval. Neither fill verification nor the one-shot draft-save activation may accept a non-`ALLOW` probe classification.
2. Default-deny all writes. Only current-session, explicitly classified step validation and the single visible draft-save action may be allowed; direct endpoint replay is forbidden.
3. Run `validate_next_product_checklist.py --stage pre_write` and require a new passing receipt before the first clear, fill, or upload.
4. Execute every `clear_inherited` action and read back empty state for title, model, price, MOQ, attributes, media, detail, logistics, services, certificates, and claims.
5. Resolve dynamic category attributes from the current visible form/schema. The logical adapter must not contain CSS coordinates, cached handles, or concrete `p-*` IDs.
6. Fill one plan-approved value at a time. Record intended value hash, displayed value hash, source/decision ID, and result.
   For localized values, resolve the currently visible language label rather than a fixed index. Reacquire the editor after every render, verify LTR/RTL, and bind the same-control counter text/count/maximum. `PLATFORM_COUNTER` requires a `LOCALIZED_COUNTER` capture cross-check; code-point assumptions are forbidden.
7. Recalculate each asset hash immediately before selecting it. Upload serially and verify thumbnail, role, main-image choice, order, and count.
8. Before saving, run `validate_pre_save_readback.py`. It independently requires the exact action-plan field/clear/asset sets, unchanged non-target snapshot, current run/probe/session bindings, no `draft_id`, `draft_save_count=0`, and zero final-write signals. Post-refresh evidence cannot substitute for this pre-save gate.
9. Run `validate_next_product_checklist.py --stage pre_save`; the exact pre-save receipt and checklist must pass and the checkpoint must reach `VALIDATED` before the save control can be touched.
10. Immediately before the visible save click, page through and capture complete online, pending, and draft ID inventories. Derive and freeze `inventory-before.json` from raw capture evidence. Earlier duplicate searches are preliminary only. The frozen snapshot timestamp must be strictly earlier than the save event and may never be reconstructed after the request.
11. The visible Save Draft click may create one exact paused request. `consume` must, under one checkpoint mutation lock, claim the fixed run ledger, write a non-authorizing attempt claim, commit checkpoint `SAVE_ATTEMPTED`, and only then issue `release_authorized=true`. There is no public `transition SAVE_ATTEMPTED`. Release that same request instance once only after CLI exit `0`, a valid final receipt, and an exact checkpoint-after SHA/revision match. Never call or replay an internal write endpoint directly.
12. Require business success in addition to HTTP success; bind one unique draft ID across the save response, parsed reopen URL, and post-refresh readback.
13. Capture `inventory-after.json` independently and run `validate_post_save_inventory.py` against the already frozen pre-click inventory. It must derive complete before/after online, pending, and draft ID sets, recompute every set hash, prove online/pending unchanged, prove `draft_added == [saved_draft_id]` and `draft_removed == []`, and bind the same ID to the response/reopen/persistence evidence. Then run the `post_save` checklist and stop at `FINAL_REVIEW_READY`. If one draft exists but a later check fails, retain the lock and terminate `FAILED_DRAFT_PRESERVED`.

## Required stops

- `REVIEW_REQUIRED`: candidate package, field decisions, commercial values, or assets await review.
- `BLOCKED_PREFLIGHT`: source, schema, profile, session, policy, or hash gate failed before online writing.
- `BLOCKED_PROBE`: current-session zero-write evidence failed.
- `PASS_ZERO_WRITE / EXECUTION_BLOCKED`: zero-write is confirmed, but one or more read requests are not exactly `ALLOW`; remain read-only and do not fill or save.
- `PASS_ZERO_WRITE / PRODUCTION_EXECUTION_BLOCKED`: exact READ classification passed, but the production adapter/attestation gate did not; remain read-only and do not fill or save. This is the expected bundled `iab-v1` result.
- `ABORTED_NO_WRITE`: the operator CAS-aborted before `SAVE_ATTEMPTED`; all external-write signals were zero, no consumed attempt ledger/attempt claim/irreversible mutation lock existed, and the model lock was released. This terminal state is forbidden after any save-attempt artifact, even if a crash left checkpoint text at `VALIDATED`.
- `FAILED_DRAFT_PRESERVED`: one draft exists but is incomplete or a later check failed.
- `DRAFT_SAVED_FINAL_REVIEW_READY`: one verified full draft exists and no final write occurred.

Never report `published`, `online`, production completion, or cross-machine browser readiness from this skill.

## Offline integration layer

Before any later conformance work, run `tests/offline-iab-harness/run.ps1` against a new output directory. This is a synthetic loopback browser harness, not IAB conformance. It covers 18 locales, RTL, dynamic DOM replacement, current-control counters, default-deny HTTP request classification, exact query/body draft matching, paused exact draft release, a synthetic single-attempt state machine, final hard deny, one-draft inventory delta, and refresh/reopen persistence. It does not claim WebSocket coverage or production filesystem/CAS coupling.

This layer is intentionally `SYNTHETIC_TEST / LOCAL_CONTRACT_ONLY`, with a local harness result of `PASS_LOCAL_CONTRACT`. It never contacts Alibaba and cannot prove Codex Browser provenance. A successful harness is necessary but not sufficient; independently host-attested Browser conformance, a live zero-write probe, and one separately approved draft canary remain distinct gates.
