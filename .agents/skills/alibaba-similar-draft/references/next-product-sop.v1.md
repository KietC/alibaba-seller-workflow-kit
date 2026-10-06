# Alibaba 下一商品高置信相似发布草稿 SOP v1

版本：`1.0.0-rc1-local-contract`  
适用 Skill：`alibaba-similar-draft`  
唯一允许的运行类型：`CREATE_SIMILAR_DRAFT`  
允许终态：`FINAL_REVIEW_READY`（已经保存且仅保存一个草稿）  
永久禁用终态：`PUBLISHED`  
当前执行资格：`LOCAL_CONTRACT_VERIFIED / BLOCKED_LIVE_ATTESTATION`；在 `IMPLEMENTATION_READY=true` 且真实浏览器取证、实时计数器和 request guard 端到端 canary 通过前，不得进入实时 Alibaba 填写或保存阶段。

## 1. 这份 SOP 解决什么问题

这不是“照着旧商品改几个字段”的操作说明，而是一条可审计、可恢复、默认拒绝的草稿生产链。它必须同时证明：

1. 目标产品事实、商业数据和素材分别有获批来源；
2. 模板只是类目和页面骨架，未批准的继承值已经清零；
3. 当前账号、当前类目、当前页面的动态规则已经现场绑定；
4. 浏览器实际填写值与审批值逐字段一致；
5. 只产生一个新草稿，没有产生在线或待审核商品；
6. 最终提交动作和最终发布请求始终为零。

本 SOP 不把 MODEL-019 的既有草稿编辑当成有效 canary。`EDIT_EXISTING_DRAFT` 与 `CREATE_SIMILAR_DRAFT` 的对象、基线和重复控制不同；前者必须由独立 Skill 或独立运行模式实现。

## 2. 官方规则基线

Alibaba 官方新发布体系规定：先以 `alibaba.icbu.product.schema.get` 获取当前类目发布 XML 规则，再按返回字段、类型、选项和联动规则填充，正式发布由 `alibaba.icbu.product.schema.add` 完成。官方变更说明把新草稿接口列为 `alibaba.icbu.product.schema.add.draft`，草稿读取列为 `alibaba.icbu.product.schema.render.draft`。

因此本 Skill 采用以下边界：

- `schema.get`、`schema.render`、`schema.render.draft`：只读证据源；即使 HTTP 方法是 POST，也不能按“POST=写”粗判，必须按 host、API method、path、body 共同分类。
- `schema.add.draft`：官方可逆草稿写能力，但本 Skill 不直接调用；只允许在可见 UI 中触发一次经现场确认的“保存草稿”。
- `schema.add`：正式发布，永久分类为 `FINAL_WRITE`，任何模式均不得放行。
- `/product/submitDraft.json` 等页面内部端点：只视为某次 UI 观测结果，不视为稳定 API，不允许脚本直接重放。
- 动态 `p-*` 属性、必填项、选项、联动、长度单位不能沿用上次运行；每次运行必须重新获取并绑定当前 schema。

官方依据：

- [新商品发布接入文档](https://developer.alibaba.com/docs/doc.htm?articleId=119213&docType=1&treeId=456)
- [商品接口变动说明](https://developer.alibaba.com/docs/doc.htm?articleId=119212&docType=1&treeId=456)
- [schema.get](https://developer.alibaba.com/docs/api.htm?apiId=50063)
- [schema.render](https://developer.alibaba.com/docs/api.htm?apiId=50188)
- [schema.render.draft](https://developer.alibaba.com/docs/api.htm?apiId=50205)

## 3. 状态机和停止规则

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

不定义 `PUBLISHED`。任何步骤出现以下情况，立即进入 `BLOCKED_*` 并保留现场：

上述 P00-P27 名称是编排清单 overlay，不是 checkpoint 状态名。`checkpoint.v2` 只使用上面的九个正式状态；每次跃迁还必须同时满足相应 P00-P27 清单回执。`SAVE_ATTEMPTED` 表示唯一保存机会已经烧掉但草稿结果尚未证实，此时锁必须保持，禁止自动重试。

- 登录失效、CAPTCHA、账号或站点 origin 不符；
- 产品/模板处置记录不允许草稿写入；
- schema、页面指纹、定位器或必填项与审批包不一致；
- 发现未知写请求、最终提交请求、业务错误或重复草稿；
- 素材哈希变化、字段回读不一致、非目标字段发生变化；
- 不能证明在线数和待审核数未增加。

失败草稿不得自动删除。已有目标草稿时，保留锁和证据，状态为 `FAILED_DRAFT_PRESERVED`，禁止自动创建第二份。

## 4. 证据与哈希契约

每个 JSON 证据文件均须：

- UTF-8、确定性键排序；
- 包含 `schema_version`、`run_id`、`run_kind`、`target_model`、UTC 时间；
- 保存自身之外所有输入文件的绝对路径、字节数和 SHA-256；
- 引用其他证据时同时写入文件 SHA-256 和该证据内的语义摘要哈希；
- 令牌、Cookie、签名和账号秘密只记录不可逆哈希，不记录明文。

只写一个 64 位字符串不算证据绑定。验证器必须重新计算文件 SHA-256，并验证 `run_id + target_model + template_product_id + current schema hash + action plan hash + approval hash` 的联合绑定。

### 4.1 本地一致性不等于实时来源认证

- `SHA-256 + JSON Schema + validator` 可以证明文件没有漂移、规则重算一致，不能证明文件一定来自 Codex 内置浏览器的真实会话。
- 测试夹具只能标记为 `SYNTHETIC_TEST / LOCAL_CONTRACT_ONLY`，不得生成生产 one-shot ledger，也不得被写成 live capture。
- 当前 `official-values` 可验证“值是否满足官方 XML 规则”，但其逐字段 `evidence_sha256` 仍是本地自报，不能单独证明该值来自当前可见 UI。
- `PLATFORM_COUNTER` 没有当前会话、当前页面和原始 counter artifact 时必须阻断。
- bundled `iab-v1` 尚无经过审查的 tool-provenance verifier，因此当前总状态固定为 `BLOCKED_LIVE_ATTESTATION`；任何本地 PASS 回执都不能把 `IMPLEMENTATION_READY` 改成 true。

## 5. 运行目录

```text
<run_root>\<MODEL>__<RUN_ID>\
  00_intent\
    run_intent.json
    run_spec.json
    run.lock.json
  01_policy\
    disposition_snapshot.jsonl
    disposition_binding.json
    policy_gate_receipt.json
  02_environment\
    doctor_receipt.json
    test_receipt.json
    adapter_inventory.json
  03_sources\
    source_inventory.jsonl
    source_files.manifest.json
    source_hashes.sha256
  04_ocr_visual\
    ocr_results.jsonl
    visual_observations.jsonl
    contact_sheet.pdf
  05_facts\
    claim_ledger.jsonl
    component_ledger.jsonl
    conflict_register.jsonl
    official_fact_set.json
  06_commercial\
    comparable_products.jsonl
    commercial_decisions.json
    commercial_blockers.json
  07_assets\
    asset_inventory.jsonl
    asset_decisions.jsonl
    approved_asset_manifest.json
  08_market\
    terminology_sources.jsonl
    title_research.jsonl
  09_content\
    term_policy.json
    localized_titles.jsonl
    content_version.json
    supersession_map.json
  10_approval\
    approval_decision.json
    approved_source_manifest.json
    approved_commercial_decision.json
    draft_write_approval.json
  11_compiled\
    browser_action_plan.json
    plan_diff.json
    compile_receipt.json
  12_probe\
    probe_receipt.json
    probe_verification_receipt.json
    page_snapshot.sanitized.html
    aria_snapshot.json
    page.png
    network_sanitized.jsonl
    redaction_receipt.json
  13_baseline\
    draft_inventory_before.json
    product_counts_before.json
    target_duplicate_check.json
  14_execution\
    clear_readbacks.jsonl
    field_readbacks.jsonl
    locale_readbacks.jsonl
    asset_upload_readbacks.jsonl
    non_target_diff.json
    checkpoints\
  15_save\
    save_ui_receipt.json
    save_network_receipt.json
    draft_id_receipt.json
  16_verification\
    reopen_readbacks.jsonl
    product_counts_after.json
    draft_inventory_after.json
    persistence_diff.json
  17_receipts\
    run_receipt.json
    evidence_manifest.json
    evidence_hashes.sha256
    human_handoff.md
```

## 6. P00-P27 标准步骤

| ID | 动作 | 必须通过的检查 | 产物 | 失败状态 |
|---|---|---|---|---|
| P00 | 定义运行 | `run_kind=CREATE_SIMILAR_DRAFT`；目标型号、模板 ID、账号别名、唯一 run_id 明确；`final_write_enabled=false` | `run_intent.json`, `run_spec.json`, lock | `BLOCKED_RUN_KIND` |
| P01 | 处置门禁 | 目标和模板均查到唯一处置记录；目标允许作为事实源且允许草稿写；模板至少允许作页面骨架；禁止 `UNUSABLE_WASTE_RECORD` | disposition binding/receipt | `BLOCKED_DISPOSITION` |
| P02 | 环境体检 | Python/依赖/adapter 版本、时区、磁盘、路径可用；Skill 全测试通过；无另一运行持锁 | doctor/test receipt | `BLOCKED_ENVIRONMENT` |
| P03 | 冻结来源 | 官网快照、PDF、图片、有效历史 Alibaba、商业资料逐文件清点、哈希；来源版本不再静默变化 | source inventory/manifest | `BLOCKED_SOURCE_DRIFT` |
| P04 | OCR 与识图 | PDF 附件、页面图、尺寸图和包装图全部 OCR；识图只作佐证；OCR/视觉结论可追溯到页码/坐标/图片哈希 | OCR/visual ledger | `BLOCKED_EVIDENCE_GAP` |
| P05 | 产品事实消歧 | 每个 claim 明确产品/组件 scope；冲突有胜出依据；`Full SUS304`、高压、节省百分比等强断言须单独证据 | claim/component/conflict ledgers | `BLOCKED_FACT_CONFLICT` |
| P06 | 跨型号继承判断 | 只有相同零件号或有结构证据证明同组件时才可继承组件卖点；记录来源型号、组件、适用条件和降级措辞 | inheritance decisions | `BLOCKED_COMPONENT_SCOPE` |
| P07 | 商业数据 | 价格至少 3 个严格同类，统一币种、计价单位、MOQ、贸易条款后算加权 P25/P50/P75；否则价格阻断；MOQ/包装/交期/售后分别审批 | comparables/decisions | `BLOCKED_COMMERCIAL` |
| P08 | 素材审核 | 每张图/每段视频独立 decision_id、绝对路径、SHA-256、型号归属、允许用途、顺序；衍生图有原图和变换记录 | asset manifest | `BLOCKED_ASSET` |
| P09 | 市场术语研究 | 官网不是唯一权威；官方产品事实、行业品牌、当地 B2B 用语分层；搜索证据保存 URL、日期、语言、用途 | market ledgers | `BLOCKED_TERMINOLOGY` |
| P10 | 卖点术语策略 | 每个 claim 每种语言分别定义语义是否必须保留、是否必须字面保留、当地首选表达、禁用词、反向翻译和审核状态 | `term_policy.json` | `BLOCKED_TERM_POLICY` |
| P11 | 多语言标题 | 按当前页面实际语言标签发现，不依赖数组顺序；各语言独立本地化和长度验证；RTL 语言保留方向证据 | localized titles | `BLOCKED_LOCALE` |
| P12 | 内容版本 | 只有一个 `current` 版本；旧候选标记 superseded；编译器只接受 current+approved | version/supersession | `BLOCKED_VERSIONING` |
| P13 | 人工审批 | 每字段、每素材、每商业值逐项 APPROVED/BLOCKED；审批文件与全部输入哈希绑定；不存在笼统“尽量填全”审批 | approval manifests | `BLOCKED_APPROVAL` |
| P14 | 编译动作计划 | 字段必须命中 registry 或现场 schema；普通字段不得自动降级成动态类目属性；`price` 必须映射到 `trade_price`；计划只包含获批字段 | plan/compile receipt | `BLOCKED_COMPILE` |
| P15 | 写前预检 | unresolved P0 为空；当前 schema、adapter、审批、action plan、write approval 哈希一致；审批未过期；最终写永禁 | checklist receipt | `BLOCKED_PREFLIGHT` |
| P16 | 零写探测与执行授权分离 | 先启用网络守卫再打开相似发布 URL；绑定正确 origin/path/template ID；不是登录/CAPTCHA；请求列表非空；`zero_write_confirmed=true` 只证明没有写入，只有全部请求 decision 精确为 `ALLOW` 且 `execution_authorized=true` 才能继续填表；`DENY/HARD_DENY/UNKNOWN/ALLOW_UI_ONLY` 一律停止 | probe v3 receipts | `BLOCKED_PROBE` 或 `PASS_ZERO_WRITE / EXECUTION_BLOCKED` |
| P17 | 唯一性基线 | 记录 online/pending/draft 数量；按目标型号、标题、内部 SKU 查询；不存在未处置的同型号草稿 | baseline receipts | `BLOCKED_DUPLICATE` |
| P18 | 武装写守卫 | 全局 policy 保持 `FAIL_CLOSED_AUDIT_ONLY`；守卫准备暂停可见 UI 触发的 Save Draft 请求；未知写默认拒绝；最终提交 UI/网络双重拒绝；此步不放行任何请求 | guard arm receipt | `BLOCKED_WRITE_GUARD` |
| P19 | 清空继承值 | 对标题、型号、属性、价格、MOQ、样品、包装、交期、图片、视频、详情、证书逐项清空或重建；每项立即回读；保存残留扫描 | clear readbacks | `BLOCKED_TEMPLATE_RESIDUE` |
| P20 | 填普通字段 | 每次页面变化后重读 DOM/ARIA；按 label/role/稳定属性定位；逐字段写入后立即回读；页面、审批、schema 三方一致 | field readbacks | `BLOCKED_FIELD_READBACK` |
| P21 | 填多语言标题 | 打开多语言编辑；按页面显示语言逐一匹配 locale；写入后读取页面计数/校验小字；禁止用最长语言统一截断全部语言 | locale readbacks | `BLOCKED_LOCALE_READBACK` |
| P22 | 串行上传素材 | 单 worker，逐文件验证路径和 SHA，再上传；校验缩略图、主图、顺序、数量；无确属视频则留空 | upload readbacks | `BLOCKED_UPLOAD` |
| P23 | 保存前三方核验 | `approved manifest ↔ action plan ↔ live readback` 精确集合和值相等；无模板残留；非目标字段 diff 为空；必填项均满足当前 schema | pre-save receipt | `BLOCKED_PRE_SAVE` |
| P24 | 单次保存草稿 | 仅点击现场确认的“保存草稿”；守卫先暂停该 exact 请求，在内存中脱敏并生成 body/event/activation 证据；`validate_draft_save_activation.py consume` 原子消费 attempt `0→1` 后只释放该请求一次；禁止直接重放；HTTP 200 之外还须业务 success、无 error code/message、唯一 draft ID | activation/save receipts | `FAILED_DRAFT_PRESERVED` |
| P25 | 刷新/重开回读 | 重新打开同一 draft ID；全部批准字段、语言标题、素材和商业值持久化；回读集合和值精确匹配；草稿状态证据成立 | reopen/persistence | `BLOCKED_PERSISTENCE` |
| P26 | 外部状态不变 | 仅新增一个目标草稿；online 和 pending 数量及 ID 集合不增加；无第二目标草稿；final requests=0 | after/baseline diff | `BLOCKED_EXTERNAL_STATE` |
| P27 | 停在终审前并封存 | 保持最终提交拦截；页面停在最终提交前；生成完整 receipt、文件清单、SHA-256 和人工交接；锁转为 final-review lock | run receipt/handoff | `FINAL_REVIEW_READY` |

## 7. 事实、卖点与多语言的精确规则

### 7.1 claim 不能只挂在型号名上

`claim_ledger` 至少包含：

```json
{
  "claim_id": "claim_easy_pull_trigger",
  "canonical_meaning": "spray-valve trigger requires less hand force",
  "scope": {"type": "component", "component_id": "..."},
  "evidence_ids": ["..."],
  "numeric_claim": null,
  "approval_status": "APPROVED"
}
```

若要写“省力 75%”，还必须有 `metric`、`baseline`、`test_method`、`sample_scope`、`component_scope`。缺少任一项，只能降级为非数值的“省力/易拉”表达，不能保留 75%。

### 7.2 每种语言保存语义，不强制保留英文词形

禁止用 `hairtrigger_required=true` 这类全语言字面锁。用：

```json
{
  "claim_id": "claim_easy_pull_trigger",
  "locale": "de_DE",
  "preserve_semantics": true,
  "literal_policy": "FORBIDDEN",
  "preferred_local_expression": "leichtgängiger Betätigungshebel",
  "forbidden_tokens": ["Hairtrigger"],
  "back_translation": "easy-operating trigger lever",
  "native_review_status": "REVIEWED",
  "approval_status": "APPROVED"
}
```

每种语言可使用不同长度和卖点密度。长度以本次 schema 的 `maxLengthRule`、`unit` 和可见 UI 计数/校验为准；官方示例中的 128 byte 不能被固化成所有语言、所有页面的通用字符上限。

### 7.3 标题质量门禁

- 首先说产品类型和商业用途，再放有证据的核心材质/结构/功能；
- 不重复类目已标准展示的内容；不堆同义词；
- 型号只有买家确实会搜索或平台要求时才放标题，否则放属性；
- `Luxury`、`Modern`、`High-grade` 等抽象词只有存在市场定位理由且不会挤占关键规格时使用；
- `Pot Filler`、`Pull Down` 等词只有结构事实吻合才可用；
- 每个 locale 保存原文、反向翻译、长度测量、证据 claims 和审批状态。

## 8. 浏览器执行的精确约束

1. 绑定浏览器时校验 `https://post.alibaba.com`、目标路径、模板 ID 和当前账号别名；不导出 Cookie/Storage。
2. 网络拦截必须在导航前安装；没有请求证据的“零写”不是通过。
3. 页面指纹必须由规范化 DOM/ARIA/bundle hash 计算，不能手工填任意 64 字符串。
4. locator 首选 label、role、稳定 data 属性；每次弹窗、联动或重渲染后重新发现，不使用陈旧 ref 或屏幕坐标。
5. 写入只使用可见 UI。CDP 仅作只读观测和请求分类，不直接调用页面内部写接口。
6. 所有写请求默认拒绝。全局 policy 永不直接放行草稿端点。只有可见 Save Draft 点击产生的 exact 请求被守卫暂停后，才能以 session-scoped overlay 同时绑定当前 run、action plan、fresh probe、页面指纹、精确 POST 路径、脱敏 body 语义哈希、单次 attempt 和可见按钮事件；原子消费成功后只释放该请求一次。
7. 路径/按钮/请求中包含 submit、publish、schema.add 或等价正式含义时永久拒绝；不能通过配置开启。
8. 原始 HTML/DOM/请求只在内存中处理；落盘前移除 Cookie、Token、CSRF、签名、账号标识和隐藏敏感字段。只保存 `page_snapshot.sanitized.html` 及带规则哈希、工具哈希、删改计数的 `redaction_receipt.json`。

## 9. 草稿成功的六重证明

“页面提示保存成功”或“HTTP 200”均不足。必须同时具备：

1. 可见 UI 保存动作回执；
2. 网络返回业务成功，且没有业务错误码/错误消息；
3. 返回或页面解析到唯一的新 draft ID；
4. 刷新/重开同一 draft 后全部字段持久化；
5. 草稿清单只增加该一个目标草稿；
6. online/pending ID 集合没有增加，最终写请求数为 0。

任何一项缺失，都不能标记 `FINAL_REVIEW_READY`。

## 10. 下一商品开跑前的最小口令

```text
$alibaba-similar-draft
  run_kind=CREATE_SIMILAR_DRAFT
  target_model=<MODEL>
  template_product_id=<PRODUCT_ID>
  mode=save_draft
  approved_source_manifest=<ABSOLUTE_PATH>
  approved_asset_manifest=<ABSOLUTE_PATH>
  approved_commercial_decision=<ABSOLUTE_PATH>
  approval_decision=<ABSOLUTE_PATH>
  draft_write_approval=<ABSOLUTE_PATH>
```

执行前，必须先运行机器清单验证器。验证器只有在 `unresolved_p0=[]`、所有文件/语义哈希一致、现场 schema 和 probe 新鲜、写授权仅限一个草稿、最终写永久关闭时才返回 `READY_FOR_CONTROLLED_UI_FILL`。

## 11. 本地测试矩阵

至少覆盖：

- 错 origin/path/template ID、登录页、CAPTCHA；
- 伪 fingerprint、空 request list、未知 POST；
- 任意非空 readback 伪造、缺字段、多字段、错值、错类型、错 locale；
- `price` 错映射为动态属性；未知普通字段自动落入 `p-*`；
- 没有独立 write approval 却生成 SAVE_DRAFT；
- 模板标题/价格/MOQ/图片/视频任一残留；
- current/superseded 冲突、未审核 locale、RTL 顺序错误；
- 资产 decision_id 自动生成、文件哈希漂移；
- save HTTP 200 但业务失败、没有 draft ID、刷新丢值；
- online/pending 增加、重复目标草稿；
- 所有最终提交路径、正式 `schema.add`、模拟 publish 请求必须被双重阻断。

## 12. 可迁移包验收

发布新便携包前：

1. 源 Skill 的全部测试和负向测试通过；
2. 生成新版本目录和 ZIP，不覆盖旧包；
3. 解压到新的临时目录，离线运行 doctor、schema 校验和测试；
4. 比较源文件与包内文件 SHA-256；
5. 验证不含 Cookie、Token、会话日志、真实账号秘密和某一型号的临时审批脚本；
6. 输出包字节数、SHA-256、文件清单和能力/限制说明；
7. 新机器仍须重新登录、重新探测页面和重新绑定 schema。离线包测试通过不等于目标机器可执行 live 草稿。

## 13. 浏览器证据与本地复刻验收（2026-08-08 加固）

### 13.1 当前值不能再由 ledger 自报

`official-schema-current-values.json` 只能是冗余派生结果。必须同时提供独立的 `browser-observation-capture.v1`，验证器从 capture 逐项派生字段值、禁用状态、是否计划写入、observation ID 和 observation SHA，再要求 ledger 精确相等。只改 ledger 并重算 `value_sha256`、`evidence_sha256` 或 body SHA 的做法必须失败。

当前 capture 无平台签名，因此即使规则全部通过，`trust_status` 仍为 `LOCAL_CONTRACT`，CLI 返回 `4`。只有未来独立、受信任的 Browser backend attestation verifier 才可产生 `LIVE_ATTESTED`；本地字段、adapter flag、`source=CODEX...` 或本机签名均不得提权。

### 13.2 多语言计数器不能按字符数猜

18 个 locale 必须各自绑定当前可见语言名、方向、值、当前 render、当前 control、页面指纹和浏览器会话。`PLATFORM_COUNTER` 只接受同一控件快照里的原始计数小字、count 和 maximum；不能假设它等于 Unicode code points，也不能拿另一语言、旧 render 或旧控件的哈希代替。

`validate_localized_counter_capture.py` 只验证 capture 与期望值的本地一致性，并固定返回 `LOCAL_CONTRACT`。它不能解除生产阻断。

### 13.3 离线浏览器集成测试

迁移或修改 adapter 后，先执行：

```powershell
& (Join-Path $SkillRoot 'tests\offline-iab-harness\run.ps1') -OutputDirectory '<NEW_OUTPUT_DIRECTORY>'
```

本地复刻必须一次跑通：

1. 18/18 locale 唯一定位，阿拉伯语/希伯来语 RTL；
2. 每种语言独立文案、动态 DOM 全量替换、旧 handle 失效并重定位；
3. 同控件 counter/max 回读，错计数、重复语言和 DOM 漂移负例阻断；
4. 守卫先于导航安装，未知/外部请求默认拒绝；
5. Save Draft 的同一 paused request 在 ledger `0→1` 与 checkpoint `SAVE_ATTEMPTED` 后只释放一次；
6. 直接 replay、第二次 ledger、第二次 release 全部失败；
7. final submit 永久 hard deny，服务端收到的 final write 为 0；
8. 只新增一个 synthetic draft，online/pending 不变，刷新重开后 18/18 值一致。

该测试的最高终态是 `FINAL_REVIEW_READY_LOCAL_CONTRACT`，不是生产 `FINAL_REVIEW_READY`。随后仍需依次完成平台签名 IAB conformance、Alibaba 零写 probe、单一草稿 canary；三层不可互相替代。

### 13.4 已关闭的时间链漏洞

fill 与 post-refresh 回执必须同 browser session、同 page fingerprint，并绑定当前 probe；fill/refresh 时间不得在未来，refresh 不得早于 fill。localized readback 必须核对 `observed_unit` 且 `post_write_counter_readback=true`。任一不满足即阻断。
