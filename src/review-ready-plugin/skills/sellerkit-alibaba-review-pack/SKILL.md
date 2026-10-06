---
name: sellerkit-alibaba-review-pack
description: Materialize current SellerKit website product images, build exhaustive local website-to-Alibaba product rankings, and inspect evidence-bound review packages from the approved frozen website replica and sanitized historical Alibaba archive. Use for website image recovery, local visual or structured matching, Top-1/Top-5 review, missing-model analysis, provenance inspection, review material preparation, and human decision records without opening, filling, saving, uploading to, submitting, or publishing on Alibaba.
---

# SellerKit Alibaba Review Pack

## English

Keep Alibaba local-only and fail closed.

1. Call `inspect_sources` and stop on any missing file, live tree/hash drift, reparse path, non-local drive, or incomplete disposition coverage.
2. Call `materialize_website_images` for `main_image` first. Require the exact approved main-image count, then materialize `gallery` and require its exact approved count. Build the verified by-model view and verify the manifest, CAS hashes, decoded dimensions, and receipt.
3. Call `reconcile_catalogs` after image materialization so local image evidence is bound into `official_current.jsonl`, SQLite, and review packages.
4. Call `build_vision_features` only through the isolated local vision workers. Treat missing dependencies, incomplete coverage, and failed product-image evaluation as explicit degraded or blocked states. Never substitute BLIP or an untested VL model.
5. Call `build_full_match` over exactly the approved website × eligible-history records. Require the complete Cartesian pair count, one decision per website model with Top-1 and Top-5, and one reverse-coverage row per presence record. Use `vision_required` only with complete configured-pair visual scores.
6. Treat forced Top-1 as retrieval, never confirmation. Keep `confirmed_same_product=false` unless a separately frozen, human-reviewed benchmark and confirmation gate explicitly support it.
7. Call `compile_review_package` for one model when a field-level package is needed. Treat `CANDIDATE_MISSING` as a review queue state, never confirmed absence.
8. Call `record_review_decision` only when the user provides an actual reviewer identity, role, decision, and rationale. Never invent approval.
9. Export the full-match report and `export_evidence_receipt` after requested local outputs are complete.

The implementation also enforces source/database preflight inside package compilation and review recording. Written workflow order is not the only guardrail.

Apply these hard boundaries:

- Do not use Browser, Chrome, Computer Use, Alibaba network APIs, remote drafts, uploads, submit, or publish actions. The only network exception is read-only HTTPS retrieval of publicly referenced images from `catalog.example.invalid` or `catalog.example.invalid`; never send cookies or Alibaba session data.
- Treat `PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE` only as a local build result. It does not mean an Alibaba draft was created or a product was released.
- Do not infer prices, MOQ, packaging, lead time, logistics, inventory, certificates, warranty, category IDs, or Alibaba field IDs from the website.
- Treat all historical commercial values as `SUGGESTED_FROM_HISTORICAL + HUMAN_CONFIRM_REQUIRED`.
- Exclude every `UNUSABLE_WASTE_RECORD` from RAG, facts, assets, and suggestions. Use only its minimal product/model identity as a duplicate-presence guard.
- Historical retrieval, image matching, and reports may use only the approved whitelist-sanitized derivative rows and their allowlisted local media manifests. Never ingest or train on raw filled pages, HTML, browser globals, seller/buyer/session state, or token-bearing payloads.
- Keep `aliases.jsonl` empty in V1. No trusted alias source is configured.
- Preserve duplicate website specification IDs and names in source order; never apply last-write-wins.
- Treat website product images as `LOCAL_URL_BOUND_VERIFIED` only after current-r3 URL binding, project-CAS containment, SHA-256 recomputation, and image decode succeed. Otherwise retain `REMOTE_REFERENCE_UNMATERIALIZED` and fail the required image gate.
- Keep all `UNUSABLE_WASTE_RECORD` rows out of text, specifications, images, OCR, RAG, suggestions, and score pairs. Use only their minimal product/model identity in the complete presence guard.
- Keep OpenCLIP, Paddle/RapidOCR, and other CUDA stacks in separate subprocesses. Record model revision, weight hash, coverage, runtime, and degraded states.
- Require worker=1 and automatic-multicore result hashes to match before trusting an optimized run.
- Never claim `PUBLISHED` or `SAVED_DRAFT`, and never infer remote or deployment readiness from a local package.

Read [data-contract.md](references/data-contract.md) when interpreting image manifests, visual scores, full-match statuses, source domains, or package files. Read [local-boundaries.md](references/local-boundaries.md) before any public image retrieval, field review, or discussion of remote operations.

## 中文

### 能力与平台边界

这是面向阿里巴巴商品工作的**本地审核包**技能：整理自己的已批准冻结网站证据与白名单清洗后的历史商品记录，建立图片证据、候选排序、字段来源、人工审核记录和本地报告。它不连接在线 Alibaba 表单，不打开浏览器，不填表、不上传、不保存远程草稿、不提交或发布；也不抓取客户通或 OKKI 客户。

### 需要的输入

先在 Git 之外准备自己的私有工作区和已审核配置：`SELLERKIT_ALIBABA_CONFIG` 指向本地 `sellerkit.review-config.v1` 配置，填写自己的绝对路径、获准来源、核实哈希/数量及对应版本。公开示例未批准，占位哈希不可直接使用。需要冻结网站数据、符合处置覆盖的历史清洗记录、允许使用的本地素材清单；视觉分析另需实际可用的本地依赖和完整覆盖证据。

读取 [data-contract.md](references/data-contract.md) 理解状态、图片与包内容；涉及公开图片获取、字段审核或远程操作讨论前先读 [local-boundaries.md](references/local-boundaries.md)。英文技术段及这两份参考的完整守门规则都必须保留，中文说明不授予额外权限。

### 按顺序使用

1. `inspect_sources` 检查来源。缺文件、树/哈希漂移、重解析路径、非本地盘或处置覆盖不完整就停。
2. `materialize_website_images` 先处理主图，再处理图库；两者均核对自己的已批准确切数量。验证型号视图、清单、项目内内容寻址目录、重新计算哈希与实际图片尺寸。不通过的远程引用不能冒充已验证本地图片。
3. `reconcile_catalogs` 把已核实图片证据绑定到当前产品事实、数据库与审核包，保留来源顺序和重复规格，不用后值覆盖前值。
4. 需要视觉时，`build_vision_features` 只使用隔离的本地视觉工作进程。缺依赖、覆盖不完整或图片评估失败必须降级或阻断，不换成未经测试的模型。不同 OCR/CUDA 栈分开进程，记录权重/版本、覆盖与实际状态；优化结果须与单 worker 的结果哈希一致。
5. `build_full_match` 比对全部批准的网站与可复用历史组合，核对完整配对、逐型号 Top-1/Top-5 和反向覆盖。视觉分数只有完整配置配对覆盖时才能作为必需证据。
6. 需要字段级审核时，对单型号 `compile_review_package`，查看事实、字段来源、历史候选、素材、商务建议与本地预览。强制 Top-1 只是检索排名，不确认同款；`CANDIDATE_MISSING` 进入人工队列，不证明线上没有商品。
7. 只有用户提供真实审核者身份、角色、决定与理由时才 `record_review_decision`。本地记录属于自述审核证据，不自动提升为内部批准事实，不编造审批。
8. 请求的本地输出完成后，导出完整匹配报告与 `export_evidence_receipt`。只声明实际生成和验证的本地结果。

### 硬停止点与限制

历史商务值全部待人工确认，不据网站推断价格、MOQ、包装、交期、物流、库存、证书、保修、类目或阿里字段 ID。不可用废记录不能进入事实、素材、OCR、RAG、建议或配对分数，只保留最小身份作重复存在守门。只用白名单清洗衍生记录与允许的媒体清单；不摄取原始已填页面、HTML、浏览器全局、买卖家会话或带令牌内容，不训练它们。V1 的 `aliases.jsonl` 保持空，没有可信别名源。

公开图片获取也不是通用联网权限：只能遵守英文和本地边界规定的只读 HTTPS 域名白名单，不发送 Cookie 或阿里会话，并保存到本地项目内容寻址目录。公开配置里的示例域名不会自动提供真实图片来源，缺自己的合规配置就停，不扩展白名单绕过阻断。

`PASS_LOCAL_REVIEW_PACKAGE_READY_NO_REMOTE_WRITE` 仅是本地构建结果，`READY_FOR_LOCAL_HUMAN_REVIEW` 仅是待人工审核；远程写入仍为禁用、版本未发布。本地哈希只能证明一致性，不是在线发布许可。永远不能从本技能报告 `PUBLISHED` 或 `SAVED_DRAFT`。
