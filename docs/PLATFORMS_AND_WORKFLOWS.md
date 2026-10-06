# Platforms and workflows

## English

This kit supports local preparation and a human-reviewed operating procedure. A platform name describes the intended destination; it does not promise that its current account, quota, UI, or API is connected.

## The actual platforms

| Platform | Role in this kit | What to check |
| --- | --- | --- |
| Codex Desktop and an available in-app browser | Read the project skills, run local tools, and inspect supported visible workflows | Correct project/skill path, available browser tool, your own login |
| [Alibaba.com](https://www.alibaba.com/) / MyAlibaba | Seller-side product and RFQ context | Correct seller account and module; account access is not supplied |
| [Product publishing](https://post.alibaba.com/) | Similar-product form and its changing category fields | Current category, labels, roles, constraints, template remnants; bundled live adapter remains blocked |
| Alibaba RFQ | Read one buyer request, compare verified facts, prepare a cautious quotation | Request validity, structural fit, exact model, quantity unit, independently approved terms |
| [Alibaba Customer CRM](https://alicrm.alibaba.com/) / 客户通 | EDM campaign plan and visible campaign workflow | The EDM channel's own availability, maintained audience, entitlement, quota, and send boundary |
| [OKKI](https://crm.xiaoman.cn/) | Historical standalone CRM integration context only | Separate account, data, permissions, and quota; no OKKI connector or customer migration is included |

MyAlibaba access does not prove Customer CRM EDM eligibility. RFQ quotation limits, product-publishing limits, EDM credits, and OKKI quota are not interchangeable. Read the actual module's visible status; do not infer a remaining quota from another platform.

## Product preparation: one reviewable candidate

Use `$alibaba-similar-draft` when you want to prepare a similar-product candidate from your own approved facts and assets, map the current fields, and keep evidence and approval decisions attached to exact values.

The local tools can validate sources, field contracts, approvals, and offline state transitions. Generic archive helpers can organize existing public/product records; optional retrieval and vision work can assist review. Neither feature supplies current commercial approval, certifies a claim, or implements a full automatic publisher.

Expected now: a local candidate with clear approved fields, blocked fields, and validation results. The bundled production adapter lacks reviewed browser-backend attestation and required execution capabilities, so it stays `LOCAL_CONTRACT_ONLY / BLOCKED_LIVE_ATTESTATION`. Do not navigate, fill, upload, or save on live Alibaba through this adapter.

If a future separately reviewed adapter is implemented, it still needs your own login, current account/category fields, exact model/template binding, fresh approvals, visible readback, and a verified save result. Filling a form is not saving a draft. A draft is not a published product. No offline result grants final-publish permission.

## RFQ: compare first, quote second

Use `$alibaba-rfq-light` to compare one actual buyer request with verified local product facts. Its Python helper is a finite heuristic, not a general semantic parser and not an automatic quoting engine.

Expected: `HEURISTIC_SHORTLIST_ONLY`, with exact/near, custom, review, or rejected candidates and explicit conflicts. Unknown required facts and uncertain options remain unresolved. A high score does not settle an option, approve a material change, or waive another mandatory requirement. Customization permission must cover every mismatch; size-only permission does not authorize other structural changes.

`SKIP_RFQ` can be the correct result for an unsuitable request. `PREPARE_AND_REVIEW` means do not fill automatically. `FILL_TO_SUBMIT` is only a recommendation within the skill's gates, not proof that a quotation was filled or submitted.

Before any supported visible form work, a person confirms the buyer's request, exact model, units, price, MOQ, Incoterm, port, and unsupported claims. Use approved data for the actual model; do not borrow another model's price, image, lead time, or certificate. Stop before final quotation submission. If price or a required fact is unresolved, keep it unresolved and stop.

## EDM: validate a small plan, keep the audience boundary

Use `$alibaba-edm-light` for a small campaign plan in Alibaba Customer CRM. Its local helper validates plan structure, types, and privacy constraints. The browser procedure uses an authorized Alibaba-maintained audience; it is not a customer-list export or scraping workflow.

Expected: a validated or explicitly blocked plan. A local plan passing checks does not prove that the account has an EDM channel, available credits, a configured sender, or a ready template. Inspect those on the actual Customer CRM page before supported form work.

Re-read the intended audience, sender, subject, links, template, and verified claims. Stop before test send, scheduling, publishing, final send, or paid activation. Do not substitute an OKKI list, upload private recipients, or infer permission from an available button.

## Moving to another machine

Move the public repository, not a session bundle:

1. Create a clean checkout and complete [local setup](GETTING_STARTED.md).
2. Run synthetic checks before adding business data.
3. Open that checkout as the Codex project and confirm its skill paths.
4. Prepare your own private facts and schema-compatible configuration outside Git.
5. Review exact-model sources, assets, and commercial decisions; create fresh approvals.
6. Log in yourself in the available browser and confirm the real platform module.
7. Perform only the supported visible review steps. Stop if the production adapter is unimplemented or a gate cannot be verified.

Expected: a reproducible local environment without borrowed credentials. Missing dependencies, data, approvals, or browser capability remain visible blockers. Never move cookies, storage, tokens, browser profiles, private CRM exports, old approvals, or private run evidence in the public package.

---

# 平台与流程

## 中文

工具箱提供本地准备与人工审核的操作流程。提到某个平台，只说明预期操作对象，不代表该账号、额度、页面或 API 已经接通。

## 实际平台

| 平台 | 工具箱中的作用 | 应检查什么 |
| --- | --- | --- |
| Codex Desktop 与可用的应用内浏览器 | 读取项目技能、运行本地工具，检查受支持的可见流程 | 正确的项目/技能路径、浏览器工具、自己的登录 |
| [Alibaba.com](https://www.alibaba.com/) / MyAlibaba | 卖家商品与 RFQ 入口背景 | 正确卖家账号与模块；仓库不提供账号权限 |
| [商品发布](https://post.alibaba.com/) | 类似产品表单和随类目变化的字段 | 当前类目、标签、角色、约束与模板残留；内置在线适配器仍阻断 |
| 阿里巴巴 RFQ | 阅读单个买家需求、比对事实、准备保守报价 | 需求有效性、结构适配、确切型号、数量单位、独立批准的商务条件 |
| [阿里巴巴客户通](https://alicrm.alibaba.com/) | EDM 计划与可见活动流程 | EDM 自己的渠道状态、平台维护客群、权限、额度与发送边界 |
| [OKKI](https://crm.xiaoman.cn/) | 仅作为历史独立 CRM 集成背景 | 独立账号、数据、权限与额度；不包含 OKKI 连接器或客户迁移 |

能进入 MyAlibaba 不代表客户通 EDM 已开通。RFQ 报价限制、商品发布限制、EDM 额度和 OKKI 额度不能混用。应查看对应模块的实际状态，不能用另一个平台的额度推断剩余次数。

## 商品准备：一个可审核候选

需要从自己的获准事实与素材准备类似产品候选、对应当前字段，并把证据和审批绑定到确切字段值时，使用 `$alibaba-similar-draft`。

本地工具可以校验来源、字段约束、审批与离线状态流转。通用归档辅助可整理已有公开/商品记录；可选检索与视觉工作可辅助审核。这些功能不会提供当前商务批准、证明某项产品声明，也不代表完整自动发布器。

当前预期结果：本地候选清楚列出批准字段、阻断字段和验证结果。内置生产适配器缺少经审核的浏览器后台真实性验证及必要执行能力，因此保持 `LOCAL_CONTRACT_ONLY / BLOCKED_LIVE_ATTESTATION`。不能通过它在在线阿里巴巴页面导航、填写、上传或保存。

即使未来另行实现并审核了适配器，仍需要自己的登录、当前账号/类目字段、准确型号/模板绑定、新审批、可见回读与已验证保存结果。填完表单不等于保存草稿，草稿不等于发布商品。离线结果不能授权最终发布。

## RFQ：先比对，再准备报价

用 `$alibaba-rfq-light` 把一个实际买家需求与已核实本地产品事实比对。Python 辅助是有限规则的候选工具，不是通用语义解析器，更不是自动报价引擎。

预期结果：`HEURISTIC_SHORTLIST_ONLY`，列出近似/准确、定制、待审核或拒绝的候选及明确冲突。未知必需事实、尚未选择的选项保持未解决。高分不能选择选项、批准材料改变或略过其他强制要求。定制许可必须覆盖每个不匹配项；只允许改尺寸，不代表允许改其他结构。

不适合的需求返回 `SKIP_RFQ` 可以是正确结果。`PREPARE_AND_REVIEW` 表示不能自动填。`FILL_TO_SUBMIT` 只是技能规则内的建议，不证明报价已经填入或提交。

受支持的可见表单操作前，由人确认买家需求、确切型号、单位、价格、MOQ、贸易条款、港口与未获证实的声明。使用实际型号的获准数据，不借另一型号的价格、图片、交期或证书。停在最终报价提交之前；价格或必需事实未解决时，不填猜测值。

## EDM：校验小计划，保持客群边界

阿里巴巴客户通的小范围活动计划使用 `$alibaba-edm-light`。本地辅助校验计划结构、类型与隐私约束。浏览器流程使用获准的阿里巴巴平台维护客群，不是客户名单导出或抓取流程。

预期结果：计划通过校验，或明确列出阻断项。本地计划通过不代表账号有 EDM 渠道、足够额度、已配置发件人或可用模板。受支持的表单操作前，必须在实际客户通页面检查这些条件。

回读目标客群、发件人、主题、链接、模板与已核实声明。停在测试发送、排期、发布、最终发送或付费开通之前。不能替换成 OKKI 客户名单、上传私有收件人，也不能因按钮可点击就推断获得授权。

## 换机器使用

迁移公开仓库，不迁移会话包：

1. 建干净副本，完成[本地配置](GETTING_STARTED.md)。
2. 接入业务数据前，先跑合成检查。
3. 把副本作为 Codex 项目打开，确认技能实际路径。
4. 在 Git 之外准备自己的私有事实与兼容配置。
5. 审核确切型号来源、素材和商务决定，创建新审批。
6. 在可用浏览器中自己登录，确认实际平台模块。
7. 只执行受支持的可见审核；生产适配器未实现或守门项无法核实，就停止对应流程。

预期结果：获得可复现的本地环境，而不是借来的凭据。依赖、数据、审批或浏览器能力缺失，都应作为明确阻断项。公开包绝不能包含 Cookie、Storage、令牌、浏览器配置、私有 CRM 导出、旧审批或私有执行证据。
