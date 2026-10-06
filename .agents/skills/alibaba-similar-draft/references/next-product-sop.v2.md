# Alibaba 下一商品高置信相似发布草稿 SOP v2

版本：`2.0.0-local-source-verified-live-blocked`  
运行类型：`CREATE_SIMILAR_DRAFT`  
目标终态：`FINAL_REVIEW_READY`  
永久不存在的状态：`PUBLISHED`  
当前生产资格：`BLOCKED_LIVE_ATTESTATION`  
当前执行权限：`NONE`

> 本版本已经把本地证据链、状态机、审批编译、动态 schema、精确读回、一次性草稿账本、迁移测试和合成浏览器链路做成机器门禁；但 Codex in-app Browser 当前没有可供本地验证器核验的不可伪造 tool-result 证明，bundled `iab-v1` 也明确声明 `production_execution_ready=false`。因此，本版本不得打开、填写、上传或保存实时 Alibaba 发布页。它是下一件商品的准确执行规范，不是当前 live 写权限。

## 1. 证据等级，不能混叫

| 等级 | 证明范围 | 是否允许 live 填写 |
|---|---|---|
| `PACKAGE_INTEGRITY_ONLY` | ZIP 内文件与 manifest/SHA-256 一致 | 否 |
| `LOCAL_CONTRACT_ONLY` | 本地 schema、规则、状态机及 synthetic harness 行为一致 | 否 |
| `LOCAL_SOURCE_VERIFIED` | 当前源码全量测试、doctor、负测和本地 harness 通过 | 否 |
| `LIVE_ATTESTED` | 可信宿主能证明 session/tab/page/DOM/network/counter 真来自当前 Browser 调用 | 仍需通过其余门禁后才可 |

本地 JSON、自报 `source=CODEX...`、文件 SHA-256 或 adapter 布尔值只能防漂移，不能证明来源。任何 `LOCAL_*` 回执都不得升级成 live 生产完成。

## 2. 官方平台边界

Alibaba 新商品流程以 `alibaba.icbu.product.schema.get` 返回的 XML 动态规则为准，再按该规则填写；新草稿能力为 `schema.add.draft`，正式发布及草稿转正式最终落到 `schema.add`。本 Skill 只允许可见 UI 触发一次可逆草稿保存，不直接调用任何写接口；`schema.add` 永久是 `FINAL_WRITE/HARD_DENY`。

官方依据：

- [新商品发布接入文档](https://developer.alibaba.com/docs/doc.htm?articleId=119213&docType=1&treeId=456)
- [商品接口变动说明](https://developer.alibaba.com/docs/doc.htm?articleId=119212&docType=1&treeId=456)
- [schema.get](https://developer.alibaba.com/docs/api.htm?apiId=50063)
- [schema.render](https://developer.alibaba.com/docs/api.htm?apiId=50188)
- [schema.render.draft](https://developer.alibaba.com/docs/api.htm?apiId=50205)

页面内部 `/product/submitDraft.json` 只是一条曾经观察到的 UI 实现细节，不是稳定 OpenAPI，禁止脚本直接重放。

## 3. 下一件商品必须先具备的输入

1. 唯一 `target_model`。
2. 唯一数字 `template_product_id`；模板只提供叶子类目和可见页面骨架。
3. 当前官网/PDF/OCR/附件/图片的逐项事实证据与 SHA-256。
4. 经处置表过滤的 Alibaba 历史候选；`UNUSABLE_WASTE_RECORD`、`allow_rag=false`、截止日及以后记录只能做负样本。
5. 独立商业审批；价格、MOQ、包装、交期、样品价、售后、证书分别审批。
6. 素材绝对路径、bytes、SHA-256、角色和审批；禁止借图、借视频或虚构角度。
7. 明确的人类 `approval-decision.json` 与独立 `draft-write-approval.json`。
8. 每次运行新建的 `RUN_ID`、新输出目录和共享型号锁目录。

跨型号 claim 只有在相同零件号或相同结构证据成立时才可作为候选，并必须记录来源型号、目标型号、组件、适用条件、降级措辞和审批。价格/MOQ/安装结构绝不随 claim 一起继承。

## 4. 统一运行环境

所有 Python 命令必须使用离线解析器，禁止在目标机假设裸 `python`、`uv` 或在线安装可用：

```powershell
$SkillRoot = '<ABSOLUTE_SKILL_ROOT>'
$Workspace = '<ABSOLUTE_PRIVATE_WORKSPACE>'
$Profile = Join-Path $SkillRoot 'profiles\example-profile.v1.json'
$Py = & (Join-Path $SkillRoot 'scripts\resolve_offline_python.ps1') -SkillRoot $SkillRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'

& (Join-Path $SkillRoot 'run_local_acceptance.ps1') `
  -SkillRoot $SkillRoot -Workspace $Workspace `
  -OutputDirectory '<NEW_LOCAL_ACCEPTANCE_DIRECTORY>'
```

该命令只跑全量 Python 测试、strict doctor 和 loopback synthetic Chromium harness；它不接触 Alibaba，最高结论为 `LOCAL_CONTRACT_ONLY`。

## 5. 本地准备阶段

### 5.1 当前 schema XML：原文、解析、规范化

真实运行未来必须由可信只读采集器保存当前 `schema.get` 原始 `itemSchema` XML。解析命令必须显式提供采集时间；省略时间只会得到 `REPLAY_REVIEW_ONLY` 和退出码 `4`：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\parse_official_schema_xml.py') `
  --xml-path '<RAW_ITEM_SCHEMA.xml>' --output '<NEW_OFFICIAL_SCHEMA_NORMALIZED.json>' `
  --capture-id '<CAPTURE_ID>' --run-id '<RUN_ID>' `
  --target-model '<MODEL>' --template-product-id '<TEMPLATE_ID>' `
  --category-id '<LEAF_CATEGORY_ID>' --product-type '<PRODUCT_TYPE>' `
  --captured-at '<UTC_ISO_8601>' --valid-minutes 15

& $Py (Join-Path $SkillRoot 'scripts\normalize_dynamic_publish_schema.py') `
  --raw-payload '<RAW_ITEM_SCHEMA.xml>' `
  --official-schema '<OFFICIAL_SCHEMA_NORMALIZED.json>' `
  --output '<NEW_DYNAMIC_SCHEMA.json>' `
  --run-id '<RUN_ID>' --target-model '<MODEL>' `
  --template-product-id '<TEMPLATE_ID>' --category-id '<LEAF_CATEGORY_ID>' `
  --product-type '<PRODUCT_TYPE>' `
  --source-kind OFFICIAL_SCHEMA_GET `
  --source-endpoint 'https://eco.taobao.com/router/rest' `
  --source-operation 'alibaba.icbu.product.schema.get' `
  --captured-at '<UTC_ISO_8601>' --valid-minutes 15
```

拒绝条件包括：非 XML、DTD/ENTITY、错误 endpoint/operation、未知 field type/联动、字段列表与 XML 重解析不一致、future/stale、hash 漂移、run/model/template/category 不一致。

### 5.2 当前页面值必须从 capture 派生

```powershell
& $Py (Join-Path $SkillRoot 'scripts\validate_official_schema_values.py') `
  --normalized-schema '<OFFICIAL_SCHEMA_NORMALIZED.json>' `
  --capture '<BROWSER_OBSERVATION_CAPTURE.json>' `
  --current-values '<OFFICIAL_SCHEMA_CURRENT_VALUES.json>' `
  --expected-run-id '<RUN_ID>' --expected-target-model '<MODEL>' `
  --expected-template-product-id '<TEMPLATE_ID>' `
  --expected-browser-session-sha256 '<SESSION_SHA256>' `
  --receipt '<NEW_OFFICIAL_VALUES_RECEIPT.json>'
```

ledger 只是冗余输出；字段值、disabled/targeted 状态、observation ID 和 hash 必须从 capture 重算。当前实现对真实来源无法 attestation，所以即使规则一致也返回 local trust/退出码 `4`。

### 5.3 候选审核包和工作簿

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') prepare `
  --workspace $Workspace --workspace-profile $Profile `
  --target-model '<MODEL>' --template-product-id '<TEMPLATE_ID>' `
  --dynamic-schema '<CURRENT_DYNAMIC_SCHEMA.json>' --run-id '<RUN_ID>' `
  --output '<NEW_RUN_DIRECTORY>'

node (Join-Path $SkillRoot 'scripts\build_candidate_workbook.mjs') `
  --candidate-dir '<NEW_RUN_DIRECTORY>' `
  --output '<NEW_REVIEW_WORKBOOK.xlsx>'
```

工作簿命令只接受 named arguments；输出、预览和 inspect 目标必须不存在。浏览器证据只有在显式 `--browser-evidence-dir` 且总描述符、逐文件、run spec 全部 hash-bound 时才纳入，绝不扫描相邻目录。

### 5.4 审批、preflight、计划和 checkpoint

```powershell
& $Py (Join-Path $SkillRoot 'scripts\compile_approval.py') `
  --candidate-dir '<RUN_DIRECTORY>' `
  --decision '<APPROVAL_DECISION.json>' `
  --output '<NEW_APPROVAL_DIRECTORY>'

& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') preflight `
  --manifest '<APPROVED_SOURCE_MANIFEST.json>' `
  --asset-manifest '<APPROVED_ASSET_MANIFEST.json>'

& $Py (Join-Path $SkillRoot 'scripts\compile_browser_action_plan.py') `
  --approved-source-manifest '<APPROVED_SOURCE_MANIFEST.json>' `
  --approved-asset-manifest '<APPROVED_ASSET_MANIFEST.json>' `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --write-approval '<DRAFT_WRITE_APPROVAL.json>' `
  --field-registry (Join-Path $SkillRoot 'references\publish-field-registry.v1.json') `
  --endpoint-policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
  --adapter (Join-Path $SkillRoot 'references\adapters\iab-v1.json') `
  --output '<NEW_BROWSER_ACTION_PLAN.json>'

& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') init-run `
  --run-dir '<RUN_DIRECTORY>' --run-id '<RUN_ID>' `
  --target-model '<MODEL>' --template-product-id '<TEMPLATE_ID>' `
  --lock-root '<SHARED_MODEL_LOCK_DIRECTORY>'
```

`init-run` 不再发明新 run ID；任一身份冲突必须在 checkpoint/lock 落盘前失败。同一共享锁目录下，同型号不同 run 只能有一个持锁者。

普通 checkpoint 状态只能按当前 revision 和 canonical `checkpoint_sha256` 做 CAS 推进：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') transition `
  --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
  --expected-revision '<CURRENT_REVISION>' `
  --expected-sha256 '<CURRENT_CANONICAL_CHECKPOINT_SHA256>' `
  --new-state '<NEXT_STATE>' `
  --evidence '<ABSOLUTE_TYPED_EVIDENCE_BUNDLE.json>'
```

`DRAFT_SAVED` 另加 `--draft-id '<VERIFIED_DRAFT_ID>'`。required evidence roles 固定如下：

| 目标状态 | 必需 role |
|---|---|
| `CANDIDATE_REVIEW_APPROVED` | `APPROVAL_COMPILATION_RECEIPT` |
| `TEMPLATE_PROBED` | `PROBE_VERIFICATION_RECEIPT` |
| `INHERITED_DATA_CLEARED` | `INHERITED_CLEAR_PRE_SAVE_RECEIPT` |
| `FORM_FILLED` | `FORM_FILL_PRE_SAVE_RECEIPT` |
| `VALIDATED` | `PRE_SAVE_CHECKLIST_SOURCE` + `PRE_SAVE_CHECKLIST_RECEIPT` |
| `DRAFT_SAVED` | `INVENTORY_DELTA_VERIFICATION_RECEIPT` + `--draft-id` |
| `FINAL_REVIEW_READY` | `FILL_VERIFICATION_RECEIPT` + `POST_SAVE_CHECKLIST_SOURCE` + `POST_SAVE_CHECKLIST_RECEIPT` |

`SAVE_ATTEMPTED` 不在公开 `transition` 的 choices 中；只能由保存 activation 的 `consume` 在固定 one-shot ledger 与 OS mutation lock 内写入，role 为 `DRAFT_SAVE_ATTEMPT_CLAIM_RECEIPT`。不得手工或重复推进。

## 6. 两次浏览器 pass（当前版本不得执行）

### Pass A：零写现场探测

1. request guard 必须在任何导航前安装。
2. 精确绑定 `https://post.alibaba.com/product/publish.htm`、`pubType=similarPost`、`behavior=copyNew`、模板 ID。
3. 不读/导出 Cookie、Storage、token。
4. CAPTCHA、登录失效、route 漂移、locator 不唯一、未知请求立即停。
5. 保存脱敏 DOM/ARIA/screenshot/bundle/page fingerprint 和完整只读请求列表。
6. 由经过审查的 runtime 从本次 probe 生成短时 exact READ activation request；禁止手写、复用或跨 session 使用。

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') verify-probe `
  --probe '<RAW_ZERO_WRITE_PROBE.json>' `
  --policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --action-plan '<BROWSER_ACTION_PLAN.json>' `
  --adapter '<REVIEWED_PRODUCTION_ADAPTER.json>' `
  --probe-read-activation '<PROBE_READ_ACTIVATION_REQUEST.json>' `
  --probe-read-activation-output '<NEW_PROBE_READ_ACTIVATION_RECEIPT.json>' `
  --output '<NEW_PROBE_VERIFICATION_RECEIPT.json>'
```

继续条件必须同时成立：

```text
status == PASS_ZERO_WRITE
zero_write_confirmed == true
execution_authorized == true
production_execution_authorized == true
authorization_errors == []
production_authorization_errors == []
request_classifications is non-empty
every request decision == ALLOW
```

退出码：`0` 才可能继续；`4` 表示零写已证明但生产执行仍被阻断；`3` 表示零写也未证明；`2` 表示输入/契约错误。bundled `iab-v1` 的预期安全结果固定为 `4`。

### Pass B：清空、填写、读回、一次保存

未来只有在 Pass A 的 production gate 通过后才可：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
  --checklist '<NEXT_PRODUCT_CHECKLIST.json>' --stage pre_write `
  --receipt '<NEW_PRE_WRITE_CHECKLIST_RECEIPT.json>'
```

只有 `pre_write` 回执通过，才可开始清空和填写：

1. 清空标题、型号、价格、MOQ、属性、图片、视频、详情、物流、服务、证书和 claim 的所有未批准模板残留。
2. 每次重渲染后重新从可见 label/role 定位，禁止坐标、旧 handle、固定语言索引和旧 `p-*`。
3. 逐字段写入并立即读回；未批准字段保持空白。
4. 资产写前重算 bytes/SHA，串行上传并核对主图/顺序/数量。
5. 多语言按当前可见 language label 定位。`PLATFORM_COUNTER` 必须绑定同一 session/page/render/control 的 raw counter text/count/maximum；不得假设等于 Unicode 字符数。

保存前精确读回：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\validate_pre_save_readback.py') `
  --action-plan '<BROWSER_ACTION_PLAN.json>' `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --probe-source '<RAW_ZERO_WRITE_PROBE.json>' `
  --probe-verification '<PROBE_VERIFICATION_RECEIPT.json>' `
  --pre-save-readback '<PRE_SAVE_READBACK.json>' `
  --receipt '<NEW_PRE_SAVE_VERIFICATION_RECEIPT.json>'

& $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
  --checklist '<NEXT_PRODUCT_CHECKLIST.json>' --stage pre_save `
  --receipt '<NEW_PRE_SAVE_CHECKLIST_RECEIPT.json>'
```

只有两个回执都通过、checkpoint 已以 typed evidence 到达 `VALIDATED`，才允许继续。触碰 Save Draft 前，必须完整分页采集 online、pending、draft 的全部 ID/cursor，从原始 capture 派生并冻结 `inventory-before.json`；其 timestamp 必须早于可见保存事件，禁止保存后补造 before inventory。

保存请求先暂停，再执行：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\validate_draft_save_activation.py') consume `
  --activation '<DRAFT_SAVE_ACTIVATION.json>' `
  --action-plan '<BROWSER_ACTION_PLAN.json>' `
  --probe-verification '<PROBE_VERIFICATION_RECEIPT.json>' `
  --probe-source '<RAW_ZERO_WRITE_PROBE.json>' `
  --endpoint-policy (Join-Path $SkillRoot 'references\endpoint-policy.v1.json') `
  --probe-read-activation '<PROBE_READ_ACTIVATION_RECEIPT.json>' `
  --body-semantics '<DRAFT_SAVE_BODY_SEMANTICS.json>' `
  --visible-ui-event '<VISIBLE_UI_SAVE_EVENT.json>' `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --pre-save-readback '<PRE_SAVE_READBACK.json>' `
  --pre-save-verification '<PRE_SAVE_VERIFICATION_RECEIPT.json>' `
  --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
  --ledger-dir '<RUN_DIRECTORY>\draft-save-attempt-ledger' `
  --receipt '<RUN_DIRECTORY>\draft-save-activation-receipt.json'
```

`check` 永远不授权。`consume` 在同一 mutation lock 内按固定顺序执行：O_EXCL 烧掉 attempt `0 -> 1`；生成 `draft-save-activation-receipt.attempt-claim.json`（`release_authorized=false`）；生成 typed transition evidence；原子提交 checkpoint=`SAVE_ATTEMPTED`；最后才 new-only 生成 `draft-save-activation-receipt.json`（`release_authorized=true`）。三个输出目标必须事先不存在，ledger/receipt 目录必须是 checkpoint 的同级 run 目录。

不得另行执行 `transition SAVE_ATTEMPTED`。只有 CLI 退出 `0`、最终 receipt 为 PASS 且 `release_authorized=true`、receipt 的 checkpoint-after canonical `state_store.checkpoint_hash`/revision 与磁盘 checkpoint 完全一致时，才可释放同一个 paused request 一次；禁止用 raw `Get-FileHash` 代替 canonical checkpoint hash。checkpoint commit 前失败只能留下非授权 claim；commit 后任何 crash、超时、业务结果不明或释放失败都按“草稿可能存在”处理，不得自动 retry。

## 7. 保存后证明

HTTP 200 不等于草稿成功。必须绑定唯一 draft ID，并从完整的 before/after online、pending、draft 分页清单重算集合：

```text
online_after == online_before
pending_after == pending_before
draft_after - draft_before == {saved_draft_id}
draft_before - draft_after == empty
save_response.draft_id == reopen_url.draft_id == post_refresh.draft_id
```

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') verify-fill `
  --action-plan '<BROWSER_ACTION_PLAN.json>' `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --probe-receipt '<PROBE_VERIFICATION_RECEIPT.json>' `
  --fill-execution '<FILL_EXECUTION.json>' `
  --post-refresh '<POST_REFRESH.json>' `
  --output '<NEW_FILL_VERIFICATION_RECEIPT.json>'

& $Py (Join-Path $SkillRoot 'scripts\validate_post_save_inventory.py') `
  --action-plan '<BROWSER_ACTION_PLAN.json>' `
  --run-spec '<RUN_DIRECTORY>\run_spec.json' `
  --probe-verification '<PROBE_VERIFICATION_RECEIPT.json>' `
  --pre-save-verification '<PRE_SAVE_VERIFICATION_RECEIPT.json>' `
  --activation-receipt '<RUN_DIRECTORY>\draft-save-activation-receipt.json' `
  --before-inventory '<INVENTORY_BEFORE.json>' `
  --after-inventory '<INVENTORY_AFTER.json>' `
  --save-result '<DRAFT_SAVE_RESULT.json>' `
  --post-refresh '<POST_REFRESH.json>' `
  --adapter '<REVIEWED_PRODUCTION_ADAPTER.json>' `
  --receipt '<NEW_INVENTORY_DELTA_RECEIPT.json>'

& $Py (Join-Path $SkillRoot 'scripts\validate_next_product_checklist.py') `
  --checklist '<NEXT_PRODUCT_CHECKLIST.json>' --stage post_save `
  --receipt '<NEW_POST_SAVE_CHECKLIST_RECEIPT.json>'
```

只有 live-attested inventory delta、fill refresh 和 post-save checklist 都通过，才可报告 `FINAL_REVIEW_READY`。最终提交按钮和网络拦截继续保持 HARD DENY。

## 8. 状态机、失败和安全退出

正常链只有：

```text
PRECHECK
-> CANDIDATE_REVIEW_APPROVED
-> TEMPLATE_PROBED
-> INHERITED_DATA_CLEARED
-> FORM_FILLED
-> VALIDATED
-> SAVE_ATTEMPTED
-> DRAFT_SAVED
-> FINAL_REVIEW_READY
```

不存在 `FORM_FILLED -> SAVE_ATTEMPTED`，也不存在 `PUBLISHED`。除 activation 内部提交的 `SAVE_ATTEMPTED` 外，每次公开 `transition` 必须使用上文命令并带 typed evidence bundle、真实绝对路径、bytes/SHA、role schema 和可重算 validator receipt。

保存前若决定终止，可以 CAS 安全退出并释放型号锁：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\draft_guard.py') abort-no-write `
  --checkpoint '<RUN_DIRECTORY>\checkpoint.json' `
  --expected-revision '<CURRENT_REVISION>' `
  --expected-sha256 '<CURRENT_CANONICAL_CHECKPOINT_SHA256>' `
  --reason '<REASON>'
```

仅当状态在 `PRECHECK..VALIDATED`、`draft_id=null`、attempt/draft/final 全为零、submit/publish=false、型号锁为 HELD，且不存在已消费 attempt ledger、attempt-claim 或 `HELD_FAIL_CLOSED_IRREVERSIBLE_ATTEMPT` mutation lock 时成功，终态为 `ABORTED_NO_WRITE`。即使 crash 后 checkpoint 文本仍是 `VALIDATED`，只要任一不可逆保存痕迹存在也永久拒绝 abort；必须保留锁并人工查回可能存在的草稿。只有已提交 `ABORTED_NO_WRITE` 且零写不变量仍成立时，重跑才可清理由终态落盘与锁删除之间 crash 留下的普通 stale lock。

停止状态：

| 状态 | 含义 | 下一步 |
|---|---|---|
| `REVIEW_REQUIRED` | 产品/商业/素材/文案待批 | 留在本地 |
| `BLOCKED_PREFLIGHT` | source/schema/hash/profile 不一致 | 修源后重新编译 |
| `PASS_ZERO_WRITE + production=false` | 零写成立但生产执行无权 | 不填、不保存 |
| `ABORTED_NO_WRITE` | 保存前安全终止，零外部写 | 可在新 run 重新开始 |
| `SAVE_ATTEMPTED_UNCONFIRMED` | 唯一保存机会已烧掉，结果不明 | 保留锁，查回，不重试 |
| `FAILED_DRAFT_PRESERVED` | 草稿存在但后置验证失败 | 保留并人工修复/核验 |
| `DRAFT_SAVED_FINAL_REVIEW_READY` | 且仅一个草稿通过全链 | 停在最终提交前 |

## 9. 下一件商品真正开跑前仍缺的五项 live 资格

1. Codex IAB 宿主提供本地管理员无法伪造的 capture/tool-call attestation，或可信外部 verifier 能验证它。
2. 经过代码审查的生产 runtime adapter：动态 DOM 重定位、18 locale、RTL、平台计数器、request guard、paused exact request resume。
3. IAB 对本地 loopback 页的 conformance canary；synthetic Playwright harness 不能替代。
4. Alibaba 当前账号的 zero-write live probe；不填、不保存。
5. 另行明确批准的一件处置合格型号 exactly-one-draft canary；到最终提交前停止。

在这五项完成前，`IMPLEMENTATION_READY=false` 必须保留，任何人不得把本地测试改写成生产可用。

## 10. 迁移

可迁移：Skill 源码、schemas、policy、SOP、wheelhouse、测试、合成 harness、package manifest。  
禁止迁移：Cookie、Storage、token、CSRF、浏览器 profile、HAR/MHTML、可重放写请求正文、私有运行输出和账号秘密。

新机器先运行 `run_local_acceptance.ps1`。包构建器只签 `PACKAGE_INTEGRITY_ONLY`，不能签测试质量或 live-ready：

```powershell
& $Py (Join-Path $SkillRoot 'scripts\build_local_contract_snapshot.py') `
  --skill-root $SkillRoot `
  --output-zip '<NEW_PACKAGE_INTEGRITY_ONLY.zip>' `
  --receipt '<NEW_PACKAGING_RECEIPT.json>'
```

run、acceptance、ZIP 和 packaging receipt 必须输出在 `$SkillRoot` 之外。打包器只接受固定顶层 allowlist；发现未知顶层目录（例如误放的私有 run、截图或审批清单）必须返回 `BLOCKED_PACKAGE / exit 3`。ZIP 成功只证明嵌入 manifest 与逐文件 SHA-256 一致。

迁移后重新登录、重新取 schema、重新 probe、重新审批；绝不迁移登录态或沿用旧 page fingerprint/locator/request。

## 11. 关键命令退出码

| 命令 | 继续码 | 常见阻断码 | `0` 的准确含义 |
|---|---:|---:|---|
| `draft_guard.py prepare` | `0` | `2` | 只生成本地候选/审批输入；无 live 权限 |
| `compile_approval.py` | `0` | `3` | 只编译显式审批决策与 manifests |
| `draft_guard.py preflight` | `0` | `3/2` | source/asset/schema/hash 前置契约通过 |
| `compile_browser_action_plan.py` | `0` | `2` | 只生成不可直接执行的 plan |
| `draft_guard.py init-run` | `0` | `2` | 只建立 `PRECHECK` 与型号锁 |
| `draft_guard.py transition` | `0` | `2` | 一个普通 typed CAS 状态推进；不含 `SAVE_ATTEMPTED` |
| `draft_guard.py verify-probe` | `0` | `4/3/2` | 零写、READ overlay 和 production gate 均通过；bundled adapter 当前应为 `4` |
| `validate_pre_save_readback.py` | `0` | `2` | 保存前页面读回契约通过，不授权保存 |
| `validate_next_product_checklist.py` | `0` | `2` | checklist 通过；当前 release 因 `IMPLEMENTATION_READY=false` 应为 `2` |
| `validate_draft_save_activation.py consume` | `0` | `2` | checkpoint 已是 `SAVE_ATTEMPTED`，只授权释放 exact paused request 一次；不证明草稿成功 |
| `draft_guard.py verify-fill` | `0` | `3/2` | fill 与 refresh 精确回读通过；不单独证明唯一草稿 |
| `validate_post_save_inventory.py` | `0` | `2` | live-attested exactly-one-draft delta；当前 release 应为 `2` |
| `draft_guard.py abort-no-write` | `0` | `2` | 安全零写终止或合格终态 stale-lock 清理 |
| `build_local_contract_snapshot.py` | `0` | `3` | 仅 `PACKAGE_INTEGRITY_ONLY` |
