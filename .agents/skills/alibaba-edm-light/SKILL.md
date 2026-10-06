---
name: alibaba-edm-light
description: Prepare one small, targeted Alibaba Customer CRM EDM campaign in the signed-in Codex in-app browser, using an Alibaba-maintained system audience, verified product claims, and visible UI readback. Use for Alibaba 客户通客户营销, EDM营销, 邮件模板, 客群选择, canary campaigns, or continuing an existing EDM draft. Validate a local campaign plan, fill only when a live EDM channel is ready, and always stop before 发布活动, final send, scheduling, test send, or any paid activation.
---

# Alibaba EDM Light

## English

Prepare one focused campaign without exporting contacts or automating a bulk blast. Use the signed-in Codex in-app browser for all Alibaba interaction.

## Inputs

Require:

- one product/topic with locally verified claims;
- one Alibaba system audience and its current live count;
- English subject, plain-text body, and one CTA;
- campaign plan JSON accepted by `scripts/validate_campaign.py`.

Never place customer names, emails, IDs, raw CRM rows, SMTP passwords, cookies, or tokens in the plan or outputs.

URL userinfo (username/password in the authority) is rejected in every plan string, including nested notes and copy. Preserve the raw lexical URL boundaries and inspect the original plus every stage of the existing two-layer percent/HTML decoding; encoded authority whitespace or angle brackets must not truncate a candidate before its `@`. Check both explicit-scheme and protocol-relative (`//host/...`) URLs, including references after decoded text enclosures/field separators and query/fragment markers. Double slashes within an existing URL path or ordinary word are not a new authority; retain the innermost parent URI context before accepting a nested reference. This separate authority check does not change raw form-query parsing.

The local validator scans every plan field, including nested `notes` and CTA text/URLs, for contact/credential fields and high-confidence secret/address patterns. Complete compound URL-query and text-assignment keys use the same separator/CamelCase normalization as object keys, but only a full lexical contract can take precedence over embedded colon assignments. Explicit UI namespaces must contain no credential/metadata span before the typed key; trailing boolean/count/hash/policy aliases cannot hide a preceding credential value. Decoded query keys are checked even when they resemble sensitive aliases or have no equals value; only correctly typed full metadata or an explicit nonempty policy key/value keeps internal colons whole. URL form queries distinguish literal `+` from percent-encoded `+`; decoded paths, query flags/values and fragments are also checked for embedded assignments. Boolean availability flags, nonnegative counts and SHA-256 metadata are permitted; this is not a semantic guarantee that arbitrary prose contains no private names. CTA URLs require a parsed HTTPS hostname and no embedded credentials; validation does not contact the URL or prove that it is reachable. Invalid plan data produces a blocked receipt without copying the rejected values into errors.

Whole-key namespace binding normalizes each lexical part once and checks only bounded suffixes derived from the finite credential/metadata/policy vocabulary. Long ordinary namespaces do not gain an exemption by reaching a length limit; sensitive namespace prefixes still reject whole-key precedence. Local timing regressions are finite synthetic checks, not a universal input-size or runtime guarantee.

Ambiguous colon keys keep the same finite alias-sized word window at every internal delimiter. Their nonempty colon-bearing values never qualify as typed metadata; do not repeatedly normalize growing namespace prefixes or concatenate the remaining value. Complete assignment values retain the existing boolean/count/SHA-256 and quoted-value rules.

Dictionary keys use the same finite-vocabulary-sized suffix window, preserving longest metadata precedence without materializing all namespace suffixes. Prose assignment scanning lexes each complete key token once; colon-only namespaces must not trigger overlapping equals retries. Surrounding ASCII and common Unicode quotation marks bound a URL reference, but escaped quotes and internal quoted assignment values remain subject to credential checks. None of these boundaries change raw form-query `+`/`%2B` decoding or the independent raw/intermediate URL-userinfo scan.

The authority boundary check recognizes the same supported Unicode enclosures in the raw token and every existing decode stage, including protocol-relative references; an enclosure inside a parent URI path still does not create a new authority. Email and explicit-scheme discovery consume each maximal lexical candidate once rather than retrying dot-separated suffixes. Preserve the existing email/scheme word-boundary semantics, metadata precedence, and form-query decoding; performance checks remain finite synthetic checks with their unchanged per-CLI deadline.

Non-overlapping prose tokens retain a finite alias-sized word tail across adjacent whitespace chunks, so benign leading copy cannot split a compound credential alias. A dictionary metadata key stays bound to its actual boolean/count/SHA-256 value during text scanning; suppress isolated colon reinterpretation only for a valid complete typed contract, never for a credential-bearing namespace prefix.

## Workflow

1. Read and follow the installed `browser:control-in-app-browser` Skill. Use only the in-app browser for Alibaba.
2. Open `https://alicrm.alibaba.com/#mail-config` and inspect configuration state without reading or printing field values:
   - `form.user` is the DirectMail sender field;
   - `form.password` is the SMTP password field;
   - `form.replayAddr` and `form.nickName` configure replies;
   - output only booleans such as `hasValue`, never the values.
3. Open `https://alicrm.alibaba.com/#custom-marketing-editor` and read the current channel labels, quota message, audience names/counts, enabled state, and final button label.
4. If the Alibaba Cloud quick-setup URL redirects to `account.aliyun.com/login`, preserve that tab and ask the user to sign in there. Alibaba.com login does not imply Alibaba Cloud login. Do not inspect or handle the user's login credentials.
5. Fail closed when both EDM channels are disabled, quota is exhausted, DirectMail is unopened/unconfigured, Alibaba Cloud login is absent, or CAPTCHA appears. Report `BLOCKED_EDM_CHANNEL_CONFIGURATION`; do not substitute station messaging or another service.
6. Select exactly one Alibaba-maintained audience. Default canary order:
   - `近30天询盘买家`;
   - `近30天TM买家`;
   - `活跃RFQ客户`;
   - `近30天访问买家`;
   - `店铺兴趣客户`.
7. Prefer a live count of 1-20. If the best audience exceeds 20, stop for explicit scope approval instead of silently widening the campaign.
8. Build a campaign plan and run:

   ```powershell
   python scripts/validate_campaign.py --input <campaign-plan.json> --receipt <new-receipt.json>
   ```

9. Fill the visible form using current labels/roles, never fixed screen coordinates. Re-read each field after entry. Do not create a custom audience from the local public-sea archive unless the user separately approves the exact segment logic.
10. If an existing published template is suitable, preview it and verify every claim before reuse. Otherwise prepare a new template but do not click its publish/save action if that action makes it active without a separate review.
11. Use `计算可营销人数` only after the selected audience and channel are visible and correct. Re-read the calculated count and stop if it differs materially from the plan.
12. Stop immediately before `发布活动`, `发送`, `定时发送`, test send, paid activation, recharge, or any equivalent final action. Ask for action-time confirmation with the exact audience, count, subject, channel, schedule, and cost status.

## Hard boundaries

- `final_send_allowed` must remain `false`; this Skill has no sent state.
- Do not enter SMTP credentials, reply-email addresses, or recipient data without action-time confirmation.
- Do not publish, schedule, test-send, recharge, activate paid DirectMail, or accept a bill automatically.
- Do not use the any local historical customer index as a mailing list. It is historical analysis data, contains masked contact fields, and is not the platform suppression/consent source of truth.
- Honor Alibaba/DirectMail unsubscribe, invalid-address, bounce, suppression, and quota handling. Never remove a legitimate unsubscribe to increase reach.
- Do not use unpublished internal write APIs. Visible UI is the write surface; official DirectMail documentation is the rule source.

## Current UI reference

Read `references/current-ui.md` before a live run. Treat its counts and readiness as observations, not permanent truth; refresh them from the current page.

## Completion states

- `PASS_EDM_DRAFT_PLAN`: local plan validates; no browser write implied.
- `FINAL_REVIEW_READY`: form is filled and read back, with zero final-send actions.
- `BLOCKED_EDM_CHANNEL_CONFIGURATION`: no usable EDM channel.
- `BLOCKED_RECIPIENT_SCOPE`: audience is missing, empty, too large, or changed.
- `BLOCKED_CLAIM_REVIEW`: campaign copy contains unapproved claims.

Never report `SENT` from this Skill.

## 中文

### 能力与实际平台

用于阿里巴巴客户通 `alicrm.alibaba.com` 的小范围 EDM 活动计划、本地隐私/类型校验与获准的可见页面准备。它不是批量群发、客户名单抓取或导出工具；MyAlibaba 登录、RFQ 额度和 OKKI 额度不代表客户通 EDM 已开通。阿里云 DirectMail 登录也独立，需要操作者自己完成。

### 需要的输入

准备一个有本地核实声明的产品/主题、一个阿里巴巴平台维护客群及当前实际人数、英文主题、纯文本正文、一个 CTA，以及 `scripts/validate_campaign.py` 接受的计划 JSON。私有计划放在公开仓库外。任何字段，包括嵌套 notes、文案和链接，都不能含客户姓名、邮箱、ID、原始 CRM 行、SMTP 密码、Cookie 或令牌。

CTA 必须是可解析的 HTTPS 链接且不含登录凭据。计划通过只证明有限本地校验通过，不证明链接可访问、任意文案无私人姓名、客群已获同意或渠道已经可发送。布尔状态、人数和哈希必须使用正确类型；无效材料应修材料，不降低验证条件。上方英文段完整规定词法边界、解码与类型规则，中文说明不改变它们。

### 按顺序使用

1. 先用公开合成演示做本地计划验证；不连接真实客户列表，不发送邮件。
2. 实际运行前阅读应用内浏览器技能与 `references/current-ui.md`，然后在当前页面重新核实状态；参考文件里的观察不代表永久状态。
3. 检查客户通发件配置时只记录“有无值”等布尔状态，不读取或输出发件账号、SMTP 密码与回复地址。需要填写这些值时必须另有当时确认，不让助手接管凭据。
4. 检查当前 EDM 渠道、额度、客群名字/人数、启用状态与最终按钮。阿里云页面要求登录时保留标签页，由用户自己登录；不能用另一个渠道代替未开通的 EDM。
5. 按上方英文优先顺序选择且只选择一个平台维护客群。试运行优先少量人数；超过英文规定的范围时停下来取得具体范围批准，不静默扩大。
6. 运行 `validate_campaign.py --input <campaign-plan.json> --receipt <new-receipt.json>`，逐项解决阻断。只在渠道可用、范围获准且声明核实后，按当前标签/角色填写可见表单，禁止固定坐标猜控件。
7. 预览模板并验证每项声明。会使模板立即生效的发布/保存动作不能顺带执行；自建客群也需对确切分段逻辑另行批准。计算可营销人数前先确认客群与渠道，计算后回读，与计划明显不同则停止。
8. 回读客群、渠道、发件状态、主题、正文、CTA 与人数，停在发布、发送、定时发送、测试发送、充值或付费开通之前。

### 硬停止点与结果

两渠道不可用、额度不足、DirectMail 未开通/未配置、阿里云登录缺失或验证码出现时，返回 `BLOCKED_EDM_CHANNEL_CONFIGURATION`。客群缺失、空、过大或发生变化时阻断范围；声明未批准时阻断文案。不得用历史本地客户索引当邮件名单，不移除退订、抑制、无效地址或退信规则以扩大人数，不调用内部写入 API。

`final_send_allowed` 永远保持 `false`。`PASS_EDM_DRAFT_PLAN` 是本地计划校验，不代表浏览器写入；`FINAL_REVIEW_READY` 仅表示填表回读完成且最终发送为零。本技能没有 `SENT` 状态，不自动发布、排期、测试发送、付费开通或接受账单。要讨论下一动作，必须按英文规则列出确切客群、人数、主题、渠道、时间与费用状态供当时确认，不能把该确认当成这个技能已发送。
