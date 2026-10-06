---
name: alibaba-similar-draft
description: Prepare and validate an evidence-backed Alibaba similar-product draft contract locally. Use for 发布类似产品 planning, approvals, portable setup, resume contracts, and UI/schema drift review. This public release is LOCAL_CONTRACT_ONLY and blocks live navigation, filling, uploading, saving, submission, publication, login migration, and CAPTCHA bypass.
---

# Alibaba Similar Draft

Prepare the reviewed inputs and offline contracts for an intended one-draft Alibaba workflow. Actual live execution is not implemented in this public release.

## English

> **Current release block:** this checked-in release is `LOCAL_CONTRACT_ONLY / BLOCKED_LIVE_ATTESTATION`. Its production adapter declares `production_execution_ready=false`, has no reviewed request pause/resume implementation, no localized runtime implementation, and no browser-backend attestation. Do not navigate, fill, upload, or save on live Alibaba with this release. The commands below define and validate the intended evidence chain; they do not grant live execution authority.

## Start with the portable doctor

Resolve `SkillRoot` as the absolute directory containing this `SKILL.md`. Never infer the business workspace from the installed plugin/cache location.

```powershell
$SkillRoot = '<absolute-directory-containing-SKILL.md>'
$Workspace = '<absolute-private-business-data-workspace>'
$Profile = Join-Path $SkillRoot 'profiles\example-profile.v1.json'
$Py = '<absolute-path-to-your-venv-python>'
& $Py (Join-Path $SkillRoot 'scripts\runtime_config.py') doctor `
  --skill-root $SkillRoot --workspace $Workspace --strict-workspace --pretty
```

Resolution precedence is explicit `--workspace`, `SELLERKIT_ALIBABA_WORKSPACE`, `SELLERKIT_ALIBABA_CONFIG`, then markers under the caller's current-directory ancestors. A core-only doctor pass is suitable for package verification, not browser work.

The code/plugin package and the private-data snapshot are separate. Never package or copy Cookie, Storage, tokens, browser profiles, HAR/MHTML, prior run outputs, approved manifests, screenshots, or Alibaba login state. Log in manually on each target machine.

## Hard boundary

- Use the template only for category and visible form structure.
- Reject `UNUSABLE_WASTE_RECORD`, `allow_rag=false`, unknown provenance, hash drift, and stale dynamic `p-*` identifiers.
- A disposition overlay seal must include the actual disposition, base-catalog binding, build receipt, and verification report, each with matching byte size and SHA-256. A seal over unrelated files is insufficient; incomplete legacy overlays require a new reviewed seal, not a bypass.
- Commercial gates follow the reviewed field registry and its shared aliases (`price` / `trade_price`; MOQ is `minimum_order_quantity`). Each supplied numeric value must be finite and non-boolean. Compact or full commercial objects must match the sealed value and every supplied context key, including currency, unit, and trade term; unsupported commercial registry fields remain blocked.
- Validate commercial evidence against the canonical approved-commercial-decision schema before semantic qualification, using only bundled/local schema resources. Missing validation dependencies or unresolved references block the run. Reject multiple approved field names that resolve to the same registry target, including equal `price` and `trade_price` decisions; do not emit duplicate writes or silently merge approvals.
- Never click, enable, or call Submit/Publish. `FINAL_WRITE` remains hard denied and no `PUBLISHED` state exists.
- Do not treat a request to use this skill, silence, or a candidate value as approval. Compile an explicit decision.
- Save at most one draft through the visible UI. Preserve a failed draft; never auto-delete it or create a duplicate.
- Stop on CAPTCHA, expired login, ambiguous locator, page-fingerprint drift, unknown write, asset drift, unresolved source conflict, or missing current-session probe.

## Portable workflow

Read `references/workflow.md`, `references/next-product-sop.v2.md`, and `references/endpoint-policy.v1.json` before browser work. The v1 SOP is retained as historical audit material and is superseded by v2. This Skill supports `CREATE_SIMILAR_DRAFT` only. Editing an existing draft is a different run kind and must not be smuggled into this state machine.

1. Capture the current account/category publish schema read-only. Preserve the raw `schema.get` XML, parse it with `parse_official_schema_xml.py`, validate the current field ledger with `validate_official_schema_values.py`, and normalize the approved live field mapping with `normalize_dynamic_publish_schema.py`. A schema capture is valid for at most 15 minutes for execution. Any unsupported construct, unreviewed business rule, hash drift, or mismatch among run/model/template/category/session blocks the run. These local parsers grant no write authority.

   Current values must be derived from a separate `browser-observation-capture.v1` artifact. The ledger is redundant output: every value, disabled/targeted state, observation ID, and observation hash must equal the capture-derived row. A caller-rehashed ledger cannot substitute for the capture.

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_official_schema_values.py') `
     --normalized-schema '<official-schema-normalized.json>' `
     --capture '<browser-observation-capture.json>' `
     --current-values '<official-schema-current-values.json>' `
     --expected-run-id '<RUN_ID>' --expected-target-model '<MODEL>' `
     --expected-template-product-id '<NUMERIC_ID>' `
     --expected-browser-session-sha256 '<SHA256>' `
     --receipt '<official-schema-values-verification-receipt.json>'
   ```

   The current implementation deliberately returns exit code `4` for a clean rules result because its trust is only `LOCAL_CONTRACT`; exit `0` is reserved for a future independently verified `LIVE_ATTESTED` capture.

2. Build the candidate package with an explicit workspace, content-bound profile, current dynamic schema, and stable run ID:

   ```powershell
    & $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') prepare `
      --workspace $Workspace --workspace-profile $Profile `
      --target-model '<MODEL>' --template-product-id '<NUMERIC_ID>' `
      --dynamic-schema '<CURRENT_DYNAMIC_SCHEMA_JSON>' --run-id '<RUN_ID>' `
      --output '<NEW_RUN_DIRECTORY>'
   ```

3. Build and visually inspect the review workbook using the named-argument CLI and the Codex bundled spreadsheet runtime. The output, previews, and inspect files must be new paths. Browser evidence is never inferred from an adjacent directory; include it only through an explicit, fully hash-bound `--browser-evidence-dir`. The workbook is review evidence, not approval.

   ```powershell
   node (Join-Path $SkillRoot 'scripts\build_candidate_workbook.mjs') `
     --candidate-dir '<CANDIDATE_DIRECTORY>' `
     --output '<NEW_REVIEW_WORKBOOK.xlsx>'
   ```

4. Record an explicit JSON decision conforming to `references/schemas/approval-decision.schema.json`. Every candidate field must be either explicitly approved with its value/source decision or named in `blocked_fields`; silence never approves. Each approved price or MOQ field independently requires a hash-bound `sellerkit.approved-commercial-decision.v1` with at least three strict, normalized comparables, matching field value and decision ID. This applies when only one of those fields is approved; a qualified source for another field cannot substitute. Otherwise use `PARTIAL_DRAFT_FILL_APPROVED_ONLY`, block the unapproved fields, and leave them blank. Per-locale values belong in `approved_localized_fields`; each `LOCALIZED_FIELD` target must conform to `localized-field-target.schema.json` and bind its locale, current visible language label, LTR/RTL direction, canonical value hash, current-page fingerprint, and live maximum-length rule hash. A `PLATFORM_COUNTER` readback must also be cross-checked against a separate `LOCALIZED_COUNTER` observation with the same session, page, render, control, locale, label, direction, value, raw counter text, count, and maximum. `validate_localized_counter_capture.py` performs this local consistency check and returns exit `4`; it cannot authenticate browser origin. The bundled `iab-v1` adapter intentionally does not claim localized runtime capability, so any localized production plan fails closed until a reviewed adapter and platform attestation exist.

5. Compile canonical manifests atomically:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\compile_approval.py') `
     --candidate-dir '<CANDIDATE_DIRECTORY>' --decision '<APPROVAL_DECISION_JSON>' `
     --output '<NEW_APPROVAL_OUTPUT_DIRECTORY>'
   ```

6. Run schema-backed preflight, then compile a non-executing browser-intent plan candidate. It remains blocked unless a separately reviewed production adapter satisfies every runtime capability gate:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') preflight `
     --manifest '<approved_source_manifest.json>' `
     --asset-manifest '<approved_asset_manifest.json>'

   & $Py (Join-Path $SkillRoot 'scripts\compile_browser_action_plan.py') `
      --approved-source-manifest '<approved_source_manifest.json>' `
      --approved-asset-manifest '<approved_asset_manifest.json>' `
      --run-spec '<candidate-dir\run_spec.json>' `
      --write-approval '<draft-write-approval.json>' `
      --field-registry (Join-Path $SkillRoot 'references\publish-field-registry.v1.json') `
     --endpoint-policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
     --adapter (Join-Path $SkillRoot 'references\adapters\iab-v1.json') `
     --output '<NEW_browser_action_plan.json>'
   ```

   Initialize the checkpoint with the already prepared run ID and a shared, cross-run model-lock directory. Identity mismatches are rejected before either the checkpoint or lock is written:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') init-run `
     --run-dir '<RUN_DIRECTORY>' --run-id '<RUN_ID>' `
     --target-model '<MODEL>' --template-product-id '<NUMERIC_ID>' `
     --lock-root '<SHARED_MODEL_LOCK_DIRECTORY>'
   ```

7. Use only the Codex in-app Browser. Install the request guard before navigation, run the zero-write probe, and verify its redacted JSON with all four live bindings. Do not read or export Cookie/Storage.

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') verify-probe `
     --probe '<probe-receipt.json>' `
     --policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
     --run-spec '<candidate-dir\run_spec.json>' `
     --action-plan '<browser_action_plan.json>' `
     --adapter (Join-Path $SkillRoot 'references\adapters\iab-v1.json') `
     --probe-read-activation '<probe-read-activation-request.json>' `
     --probe-read-activation-output '<NEW_probe-read-activation-receipt.json>' `
     --output '<probe-verification-receipt.json>'
   ```

   The activation request is a short-lived, exact `GET/HEAD/OPTIONS` proposal derived from the just-completed probe and bound to the raw probe, run spec, plan, adapter, policy, session, and page. It never changes the global policy and cannot authorize a draft or final write. Do not hand-author it from memory or reuse it across probes.

   `PASS_ZERO_WRITE` and `zero_write_confirmed=true` prove only that this probe caused no write. They do **not** authorize filling. Continue only when the same v3 receipt also has `execution_authorized=true`, `production_execution_authorized=true`, `authorization_errors=[]`, `production_authorization_errors=[]`, a non-empty request-classification list, and every decision is exactly `ALLOW`. `DENY`, `HARD_DENY`, `UNKNOWN`, and `ALLOW_UI_ONLY` all block execution. The CLI exits `4` when zero-write is confirmed but production execution is not authorized, and `3` when zero-write itself is unproven. With the bundled `iab-v1` adapter the expected safe result is exit `4`, because `production_execution_ready=false`.

8. Recheck the v3 probe execution gate and every plan binding immediately before execution. A `PASS_ZERO_WRITE` receipt with `execution_authorized=false` is a mandatory stop. Before the first clear, fill, or upload, run the P00-P18 `pre_write` checklist; it must pass from a new receipt path:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
     --checklist '<next-product-checklist.json>' --stage pre_write `
     --receipt '<NEW_pre-write-checklist-verification.json>'
   ```

9. Only after `pre_write` passes, clear all inherited template fields, then execute only logical actions in the compiled plan. Reacquire DOM state after every page change; never use screen coordinates, cached element handles, fixed CSS selectors, or old `p-*` IDs.

10. Read back each field, clear operation, and uploaded asset before any save action. This pre-save receipt has no `draft_id`, requires `draft_save_count=0`, binds the exact action plan/run/probe files, and proves the non-target form snapshot is unchanged:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_pre_save_readback.py') `
     --action-plan '<browser_action_plan.json>' `
     --run-spec '<candidate-dir\run_spec.json>' `
     --probe-source '<probe-receipt.json>' `
     --probe-verification '<probe-verification-receipt.json>' `
     --pre-save-readback '<pre-save-readback.json>' `
     --receipt '<pre-save-verification-receipt.json>'
   ```

   After the exact readback passes, run the `pre_save` checklist before the visible save action:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
     --checklist '<next-product-checklist.json>' --stage pre_save `
     --receipt '<NEW_pre-save-checklist-verification.json>'
   ```

11. Immediately before touching **Save Draft**, capture and freeze a complete, independently derived `inventory-before.json` for online, pending, and draft IDs, including every page/cursor. Earlier duplicate searches are only preliminary uniqueness checks and cannot substitute for this final snapshot. Its capture timestamp must be strictly earlier than the visible save event. Then keep the global endpoint policy in default-deny mode. When the user-visible **Save Draft** click produces the exact paused request, create the redacted body/event/activation evidence and consume the one-shot overlay before releasing that exact request once:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_draft_save_activation.py') consume `
     --activation '<draft-save-activation.json>' `
     --action-plan '<browser_action_plan.json>' `
     --probe-verification '<probe-verification-receipt.json>' `
     --probe-source '<probe-receipt.json>' `
     --endpoint-policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
     --probe-read-activation '<PROBE_READ_ACTIVATION_RECEIPT.json>' `
     --body-semantics '<draft-save-body-semantics.json>' `
     --visible-ui-event '<visible-ui-draft-save-event.json>' `
     --run-spec '<candidate-dir\run_spec.json>' `
     --pre-save-readback '<pre-save-readback.json>' `
     --pre-save-verification '<pre-save-verification-receipt.json>' `
     --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
     --ledger-dir '<RUN_DIRECTORY>\draft-save-attempt-ledger' `
     --receipt '<RUN_DIRECTORY>\draft-save-activation-receipt.json'
   ```

   `check` is validation-only and never authorizes a request. `consume` re-runs pre-save validation, verifies typed `VALIDATED` checkpoint/checklist evidence, binds run/model/template/session/page, burns attempt `0 -> 1`, and commits checkpoint `SAVE_ATTEMPTED` under the same OS-level mutation lock. It first creates `<RUN_DIRECTORY>\draft-save-activation-receipt.attempt-claim.json` with `release_authorized=false`, then `<RUN_DIRECTORY>\draft-save-activation-receipt.checkpoint-transition-evidence.json`; the final `PASS_ONE_SHOT_DRAFT_SAVE_ACTIVATED / release_authorized=true` receipt is created only after the checkpoint commit. All three destinations must be new. There is no separate public `SAVE_ATTEMPTED` transition command. Release the exact paused request only after the CLI exits `0`, the final receipt revalidates, and its checkpoint-after canonical `state_store.checkpoint_hash`/revision equals the checkpoint on disk; do not substitute a raw `Get-FileHash`. Any uncertain response keeps the lock and permanently forbids retry. Never call the endpoint directly.

   The checked-in endpoint policy is immutable audit-only state: default DENY, zero global ALLOW rules, draft seed DENY, and final writes permanently denied. A separate fresh session/run receipt may activate only exact probe READ requests. Read-like POST routes remain blocked until an explicit body-semantics contract and reviewed adapter capability exist.

12. After the single save attempt, capture the independent complete `inventory-after.json`; compare it with the already captured pre-click `inventory-before.json`. Refresh and reopen the exact saved draft, run the post-refresh fill verifier, then require the inventory delta to be exactly `{saved_draft_id}` while online/pending stay byte-for-byte identical:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') verify-fill `
     --action-plan '<browser_action_plan.json>' `
     --run-spec '<candidate-dir\run_spec.json>' `
     --probe-receipt '<probe-verification-receipt.json>' `
     --fill-execution '<fill-execution.json>' `
     --post-refresh '<post-refresh.json>' `
     --output '<fill-verification-receipt.json>'

   & $Py (Join-Path $SkillRoot 'scripts\validate_post_save_inventory.py') `
     --action-plan '<browser_action_plan.json>' `
     --run-spec '<candidate-dir\run_spec.json>' `
     --probe-verification '<probe-verification-receipt.json>' `
     --pre-save-verification '<pre-save-verification-receipt.json>' `
     --activation-receipt '<RUN_DIRECTORY>\draft-save-activation-receipt.json' `
     --before-inventory '<inventory-before.json>' `
     --after-inventory '<inventory-after.json>' `
     --save-result '<draft-save-result-evidence.json>' `
     --post-refresh '<post-refresh.json>' `
     --adapter '<reviewed-adapter.json>' `
     --receipt '<inventory-delta-verification-receipt.json>'
   ```

   Only after both receipts pass may the `post_save` checklist run:

   ```powershell
   & $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
     --checklist '<next-product-checklist.json>' --stage post_save `
     --receipt '<NEW_post-save-checklist-verification.json>'
   ```

   Stop with final-submit interception still active.

## State model

Full validated branch:

`PRECHECK -> CANDIDATE_REVIEW_APPROVED -> TEMPLATE_PROBED -> INHERITED_DATA_CLEARED -> FORM_FILLED -> VALIDATED -> SAVE_ATTEMPTED -> DRAFT_SAVED -> FINAL_REVIEW_READY`

There is no `FORM_FILLED -> SAVE_ATTEMPTED` shortcut. An unexpected draft observed outside the authorized state machine must be preserved and reconciled by a separate recovery workflow; it cannot be represented by forging or skipping `VALIDATED`.

For every ordinary public transition, use the current checkpoint revision and the canonical `checkpoint_sha256` emitted by the preceding command (not a raw file hash):

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') transition `
  --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
  --expected-revision '<CURRENT_REVISION>' `
  --expected-sha256 '<CURRENT_CANONICAL_CHECKPOINT_SHA256>' `
  --new-state '<NEXT_STATE>' `
  --evidence '<TYPED_CHECKPOINT_TRANSITION_EVIDENCE.json>'
```

Required roles are exact: `CANDIDATE_REVIEW_APPROVED=APPROVAL_COMPILATION_RECEIPT`; `TEMPLATE_PROBED=PROBE_VERIFICATION_RECEIPT`; `INHERITED_DATA_CLEARED=INHERITED_CLEAR_PRE_SAVE_RECEIPT`; `FORM_FILLED=FORM_FILL_PRE_SAVE_RECEIPT`; `VALIDATED=PRE_SAVE_CHECKLIST_SOURCE+PRE_SAVE_CHECKLIST_RECEIPT`; `DRAFT_SAVED=INVENTORY_DELTA_VERIFICATION_RECEIPT` plus `--draft-id`; `FINAL_REVIEW_READY=FILL_VERIFICATION_RECEIPT+POST_SAVE_CHECKLIST_SOURCE+POST_SAVE_CHECKLIST_RECEIPT`. `SAVE_ATTEMPTED` is deliberately absent from public CLI choices and is committed only by `validate_draft_save_activation.py consume` using `DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT`.

Before `SAVE_ATTEMPTED`, an operator may terminate a run without external side effects through a CAS-bound safe abort:

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') abort-no-write `
  --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
  --expected-revision '<CURRENT_REVISION>' `
  --expected-sha256 '<CURRENT_CANONICAL_CHECKPOINT_SHA256>' `
  --reason '<WHY_THE_RUN_IS_BEING_STOPPED>'
```

This is allowed only from `PRECHECK` through `VALIDATED` while `draft_id=null`, all save/draft/final counts are zero, submit/publish are false, the model lock is held, and there is no consumed attempt ledger, attempt-claim receipt, or irreversible-attempt mutation lock. It commits terminal state `ABORTED_NO_WRITE` and then releases the lock. It is permanently forbidden once any save-attempt artifact exists, even if a crash left the checkpoint text at `VALIDATED`; an uncertain save must retain the lock and be reconciled instead of being disguised as an abort.

Use `draft_guard.py init-run` and CAS `transition`; every transition requires typed evidence roles, real artifact paths, exact byte/hash descriptors, role schemas, and validator receipts. Bare digests and artifact-hash reuse are rejected. Legacy v1 checkpoints must not resume as v2. Keep the model lock until `FINAL_REVIEW_READY`. No branch may transition to `PUBLISHED`.

## Knowledge and receipts

- Product facts: approved official sources, approved commercial decisions, and approved asset hashes only.
- Platform knowledge: schemas, logical locators, page fingerprints, endpoint classifications, and validation messages; never credentials.
- Run experience: redacted failures and draft receipts kept separately; never auto-promote an observed locator or request into a writable adapter.
- Historical Alibaba data is review-only until a recorded human decision approves a specific value.

Store the checkpoint, redacted probe, field readbacks, counts, draft ID, screenshots, DOM/ARIA evidence, action-plan hash, and file hashes under the run directory. Hash secrets instead of storing raw secrets.

Local hashes prove consistency and drift detection only; they do not authenticate a browser tool call against a local administrator. The bundled `iab-v1` adapter has no reviewed browser-backend attestation verifier, so new capture-derived receipts are `LOCAL_CONTRACT` (legacy inventory wording: `CONSISTENCY_ONLY`) and live inventory remains `BLOCKED_LIVE_ATTESTATION`. Read `references/browser-capture-attestation-requirements.v1.md`. `implementation_ready=false`; no current local receipt may unlock a production `VALIDATED`, `DRAFT_SAVED`, or `FINAL_REVIEW_READY` claim.

## Validation

For this public source distribution, create a virtual environment and install the root `requirements.txt`. Use the environment's Python interpreter. The historical wheelhouse resolver and PowerShell acceptance scripts are retained as source references only; no wheelhouse is distributed and those scripts are not the public setup entry point.

The standalone Skill suite is under `scripts/test_*.py` and has no archive-producer dependency. Full repository acceptance also runs `tests/test_draft_producer_overlay.py` from the repository root; its nine producer-shaped regressions require the separate local archive source and must execute, not skip. That repo-only test is outside the Skill snapshot boundary.

```powershell
& $Py '<repository-root>\tools\verify.py' `
  --output '<NEW_LOCAL_ACCEPTANCE_OUTPUT_DIRECTORY>'
```

The public runner executes isolated Python test groups and reports skips separately. The optional synthetic loopback Chromium harness needs its separately documented browser runtime; it is not silently counted as passing. Highest trust stays `LOCAL_CONTRACT_ONLY`, execution authority stays `NONE`, and local tests perform no Alibaba action.

An offline PASS proves package integrity and local gates only. A target machine is browser-capable only after manual login and a fresh v3 probe with `zero_write_confirmed=true`, `execution_authorized=true`, and `production_execution_authorized=true`; never infer production authority from `PASS_ZERO_WRITE` or a local READ overlay, and never report online completion from package tests.

## 中文

本 Skill 用于相似商品草稿的本地准备与校验，不是生产发布器。公开版固定为 `LOCAL_CONTRACT_ONLY / BLOCKED_LIVE_ATTESTATION`；不能凭本地回执去在线导航、清空、填写、上传或保存，更不能提交发布。

使用顺序：

1. 先按仓库[配置指南](../../../docs/GETTING_STARTED.md)建立虚拟环境、安装依赖和运行离线示例。`$Py` 指向该环境的 Python；公开版不提供旧 wheelhouse，不使用旧离线安装脚本作为入口。
2. 将自己的业务数据放在 Git 目录之外，显式指定工作区及已审核 profile。先运行 `runtime_config.py doctor`；空工作区通过只证明包可用，不证明业务就绪。
3. 官网事实、商务字段、图片和历史记录分别整理。废记录及 `allow_rag=false` 内容禁止进入事实建议；模板只提供类目和表单骨架，不能继承别的型号内容。
4. 按当前动态 schema 建候选审核包；字段、型号、类目、会话、页面、素材路径和 SHA-256 必须对应。属性 ID 和语言长度不能照搬旧页面。
5. 人工审批具体值，未批准字段保持阻断。价格和 MOQ 分别要求有口径一致的有效依据；不能为了填满而猜测。
6. 用 `compile_approval.py` 生成批准清单，再用 `compile_browser_action_plan.py` 生成不可执行的意图计划。生产适配器缺少独立浏览器认证与请求暂停/恢复能力时，必须停下。
7. 上文探测、写前回读、唯一草稿保存和保存后库存比对描述的是未来实现需要遵守的契约，不代表当前代码已经能执行。不得手工编造浏览器回执来解锁。
8. 使用根目录 `tools/verify.py` 跑隔离测试，单独检查失败、错误和跳过项。保留原始证据；哈希一致不等于浏览器来源真实。

状态链要求 `PRECHECK → 审批 → 探测 → 清除继承内容 → 填写 → 校验 → 唯一保存尝试 → 草稿 → 最终检查`，不能跳步。未知请求、验证码、登录失效、定位器不唯一、来源冲突、素材或页面漂移都要停止。正式发布始终禁止；保存结果不明时保留锁和草稿，不重试创建第二份。

完整字段约束、CLI 参数与状态证据见上面的英文技术段；易读操作说明和常见坑见仓库 docs。
