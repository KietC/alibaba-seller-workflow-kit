---
name: alibaba-rfq-light
description: Match live Alibaba.com RFQs against SellerKit's local product facts, prepare a commercially cautious quote, fill the visible RFQ form, and stop before final submission. Use for Alibaba RFQ market scanning, RFQ-product matching, quotation drafting, unit correction, or browser-assisted RFQ form filling when the user wants a simple workflow rather than the full similar-product draft state machine.
---

# Alibaba RFQ Light

## English

Use one visible RFQ and one browser tab at a time. Default mode is `fill_to_submit`: fill the quotation and leave the final `Submit quotation / 提交报价` button untouched.

## Inputs

Accept an RFQ URL or scan the logged-in RFQ market. Optional inputs are `target_model`, `unit_price`, `MOQ`, `port`, and `language`.

Use these local sources when present:

- `sellerkit-alibaba-review-ready-v1/work/official_current.jsonl` for current product facts.
- `sellerkit-alibaba-review-ready-v1/work/website_main_images.jsonl` for exact-model assets.
- the latest `catalog_index.jsonl` for historical Alibaba titles, MOQ, and price references.
- the latest `record_disposition.jsonl` to permit historical references only when `record_disposition=HISTORICAL_REFERENCE_ONLY` and `allow_rag=true`. Missing/unknown disposition or missing/non-boolean reuse permission excludes the record; historical prices remain `REFERENCE_ONLY`.

For a quick local shortlist before opening the quote form, run:

```powershell
python scripts/rfq_match.py --rfq-text "<visible RFQ title and description>" --top 5
```

The helper output is `HEURISTIC_SHORTLIST_ONLY`. It may select a `CUSTOM` base product, but it must list every structural mismatch. It does not approve a price, image, attachment, certificate, lead time, or final submission. Re-check the selected model against the visible RFQ before filling.

## Workflow

1. Use `browser:control-in-app-browser` and the existing Alibaba login. Never export cookies or storage.
2. Read visible RFQ cards and reject expired, irrelevant, or internally inconsistent requests.
3. Score candidates by category/product type, material, installation or actuation, size, accessories, and application. Prefer exact matches. Allow a custom candidate only when the buyer explicitly welcomes customization; label it `CUSTOM`, state every mismatch, and never present the base model as identical.
4. Build a short quote from verified local facts. Correct obvious buyer unit errors such as `Acre/Acres` to `Pieces` before pricing.
5. Establish price using, in order: approved current commercial data; a recent valid internal comparable; then a current public B2B comparable. Historical or public prices are reference points, not factory cost. If no defensible range exists, leave price unresolved and stop.
6. Fill only the visible UI fields: product name, optional product number, product details, trade term, port, validity, currency, quantity unit, MOQ, unit price, and buyer message.
7. Do not upload an image or attachment unless it is an exact product asset and the user has approved that upload. Do not use another model's image.
8. Re-read the form and verify every filled value, RFQ ID, unit, price, and message. Report any unresolved commercial assumptions.
9. Stop with the final submit button visible. Do not call undocumented write APIs and do not click final Submit without an action-time user confirmation for that exact RFQ and quote.

## Matching Rule

Use a lightweight score:

- exact product family or intended application: `+4`
- material match: `+2`
- installation or actuation match: `+2`
- size/accessory match: `+1`
- explicit customization accepted: `+1`
- hard structural conflict not covered by customization: `-5`

At score `6+`, recommend filling only after the product-family and structural-conflict gates pass. `REVIEW` never permits filling, regardless of score. Foot/knee actuation and single/double bowl conflicts are checked in both directions; missing required material, installation, actuation or bowl-count facts remain conflicts. A mismatched base can be `CUSTOM` only when explicit permission covers **every current mismatch scope** (material, actuation, installation, dimensions, bowl count). A dimensions-only/custom-size request does not authorize other structural changes; `must remain`/`except` limits take precedence. Bare `customization` or unrelated `welcomed` wording is not permission.

The matcher recognizes only a limited set of common English exclusions such as `Not knee operated` and `SUS201 not acceptable`; it removes those concepts from positive requirements and excludes candidates containing them, even with generic customization permission. Other detected but unresolved negation on either the RFQ or candidate side requires `REVIEW`; candidate uncertainty cannot be covered by customization. Parse a finite scope list as the permission's subject, not all scope words later in the clause. Prefix/suffix `only` both constrain it; comma/`and`/`&` branches with known scope/fact terms and `required`, `mandatory`, `must`, `shall` or `need` are separate requirements, not customization permission, including a leading conjunction after the acceptance word. Explicit multi-scope lists such as `dimensions and actuation customization accepted` are supported; unknown or ambiguous qualification requires `REVIEW`, never generic permission. A trailing `only` must not delete following conditions such as `subject to design approval`, `provided`, `unless` or `only if`; unparsed conditions remain review-only, never assumed fulfilled. This is not a general semantic or multilingual parser. `not required` is not silently treated as `not acceptable`. Excluded candidates are reported separately and cannot displace eligible ones. At `4-5` or a high-scoring unresolved conflict, prepare the text but stop for review. Below `4`, skip the RFQ.

The complete prefix is part of the permission contract: `For dimensions only, customization accepted` remains dimensions-only. Normalize only supported punctuation and prepositions, then consume the whole scope subject. Do not take only the last comma branch or last known scope token. Only a completely recognized finite product-fact preface may be separated from a following scope subject; unknown prefix words, `only`, scope fragments or approval conditions require `REVIEW`, never a global-permission fallback.

Consume the complete remainder after `except` as a finite known scope list, optionally followed by a fully recognized separate product requirement. Pure exceptions prohibit only their listed scopes and preserve other explicit permission. Unconsumed words or conditions such as `subject to`, `provided`, `unless` or `if` anywhere in the exception remainder require `REVIEW`; recognizing an initial exception scope never means its following condition has been fulfilled.

Recognized measurement spans use the same finite syntax as `dimensions()`: two or three unsigned numbers, optional decimal fractions, `x`/`*`/`×` separators and optional `mm`/`cm`. Consume those spans before validating requirement/preface words; an isolated `x` or unknown surrounding words remain unresolved. Decimal points between digits do not end a permission clause. This compatibility rule does not turn measurements into customization permission or bypass unknown conditions and structural-conflict gates.

Permission context persists across semicolons, sentence punctuation and newlines: a separate `subject to`, `provided`, `unless`, `if` or approval clause before/after customization is still an unresolved qualification and requires `REVIEW`. Retain pending conditions across recognized independent product facts/requirements; neither such facts nor another generic permission fulfill them. Unqualified independent known facts, requirements and explicit global permissions remain supported. This is finite conservative context tracking, not a general semantic approval interpreter.

Detached clauses are fail-closed, not a qualification-keyword whitelist: every clause around an explicit permission must be fully consumed as a known product fact/requirement/restriction, another explicit permission, or a finite ordinary RFQ request (for example a catalogue/details/price request or thanks). Otherwise retain it as unresolved context before/after permission. `on condition that`, `pending confirmation` and other unrecognized qualifiers therefore require review. Ordinary-request patterns must match the complete clause; a known request/fact followed by an unknown condition is not exempt. Unsupported but potentially legitimate prose also requires human review rather than silently broadening this finite grammar.

Hard requirements are conjunctive across clauses, not a union of alternatives: every explicitly mandatory actuation, material, installation and bowl-count requirement must be matched. Only complete finite RFQ lists joined by explicit `or` (and the existing RFQ material 201/304 convention) are alternatives; mixed AND/OR, unknown notes or unconsumed relation words require `REVIEW`. Missing one of several mandatory values cannot be covered by a generic customization permission. A clearly conjunctive dual-actuation requirement may match verified dual-actuation facts. Candidate OR/options do not prove a definite current configuration, even for a single requested hard-domain value; material, installation, actuation and bowl count use the same review gate. No approval or option selection is inferred from the OR wording or generic customization permission. Known fixed facts and verified dual-AND facts remain supported; an explicit RFQ OR list can still match a known fixed candidate. Existing bowl-count uncertainty still requires review. Read every supported dimension span, not just the first: incompatible repeated requested dimensions require review even with customization. Repeated compatible dimensions preserve the existing measurement syntax, units and tolerance. This remains a finite structural-consistency check, not an interpreter for multi-product RFQs.

## Quote Style

An RFQ AND/`&` branch cannot disappear just because only one value is recognized: retain unknown mandatory values and shared-suffix operands such as `SUS304 and SUS316`, `foot and knee operated`, and a known measurement plus an unknown drawing as unresolved in their hard field. Complete known cross-field requirements, verified full dual-AND facts, ordinary requests and explicit customization scope lists retain their existing checks. Shared-suffix shorthand is not proof of a complete singleton requirement.

Candidate OR qualifications are consumed as complete fact text, not only recognized concept atoms. A recognized material/actuation/installation/bowl value followed or preceded by an unknown alternative is unresolved; typed hard-domain facts with two unknown options are also unresolved. For example, `SUS304 or SUS316`, `Foot operated or sensor operated` and `Wall mount or undermount` never prove an approved selection. Candidate dimension alternatives follow the same gate, including an otherwise matching measurement joined to an unknown size. Preserve typed fact labels when rechecking recommendations. Only requested hard domains are gated; unrelated packaging options and ignored descriptive metadata do not grant or invalidate structural permission. Vocabulary coverage, a high score and generic customization never approve an option selection.

Adjacent option modifiers do not erase an OR branch: `wall mount (standard) or undermount`, `foot operated (optional) or sensor operated`, and qualified dimension options remain unresolved in their actual requested field. Complete consumption of balanced modifiers is sufficient to bind an option, but failure to consume connector text is never permission to discard that OR. Unknown bare prose such as `preferred by customers`, unfinished parentheses/brackets, and an unrecognized opposite branch retain the nearest actual field's uncertainty. Bind only the actual operand section rather than every hard-domain word in a title; an explicit separate nonstructural packaging operand is not a structural option. A packing word in an earlier title preface or inside a structural qualifier is not evidence of a separate packing operand. Preserve the actual material/actuation/dimension facts inside a cross-field enclosure; its outer field anchor must not hide an inner hard-domain OR. A separate fixed attribute, whether consistent with or conflicting with candidate OR wording, is not an explicit option-selection record and does not resolve that uncertainty; removing the OR wording restores the existing definite-attribute checks. Unsupported option prose requires review, not inferred selection or filling. This remains a conservative field binder, not a general semantic option-selection parser.

Bind each actual OR operand independently: a recognized right-side field never consumes an unresolved left-side field with unknown connector text. Keep both actual field scopes, but gate only fields requested by the buyer. A generic field label inside a modifier does not override its actual enclosing/inner fact ownership. Carton, pallet, box and crate nouns use the same packing-operand ownership gate as packing labels: a completed structural modifier containing such a noun is not a separate packing choice; actual bare or jointly enclosed packing options remain unrelated controls.

The existing definite dual-actuation shorthand `Foot and Knee Operated` / `Knee and Foot Operated` (also `&`) is a narrow known-AND case: parse both known actuations on the RFQ and current-fact sides and require two mandatory values, never a single right-side value or an option union. A current fact explicitly asserting both can match; a Foot-only/Knee-only fact, unknown operand or unconsumed shared-suffix wording still requires review. This does not approve arbitrary shorthand, OR selection or customization.

Keep a leading conjunction attached to its preceding operand across comma/whitespace/newline separators; `Wall Mount (standard), or another option` is not a definite wall-mount selection. Candidate slash choices use the same hard-field ownership gate as OR, including a recognized value paired with an unknown value or qualified operand; unrelated packing choices and unrequested fields retain their existing behavior. A detected unsupported mandatory value in a separate clause (for example `Require SUS316`) retains its unresolved material scope even when another clause states known SUS304 facts or permits dimensions customization. Unknown vocabulary is not an approved material or a customization permission.

- Write in English unless the form requires another language; allow Alibaba's buyer-language translation when stated by the form.
- Lead with the buyer's requested configuration, not marketing adjectives.
- Mark preliminary custom pricing as subject to drawing/specification confirmation.
- Include material, size, operation method, accessories, MOQ, Incoterm, port, and the exact items still requiring confirmation.
- Never claim certificates, lead time, warranty, sample availability, or included accessories without a source.

## Stop Conditions

Stop immediately on CAPTCHA, login loss, expired RFQ, unknown unit, price with no support, conflicting target model, unexpected write request, or final-submit navigation. Preserve the filled page for user review.

## Invocation

```text
$alibaba-rfq-light mode=fill_to_submit rfq=<url-or-auto> target_model=<optional>
```

Matcher regression check:

```powershell
python scripts/test_rfq_match.py -v
```

## 中文

### 能力与平台

用于 Alibaba.com / MyAlibaba 的 RFQ：先把一个实际买家需求与自己的已核实本地产品事实比对，再准备可审核报价。在线步骤使用 Codex 中可用的应用内浏览器和操作者自己的登录，不是客户通 EDM，也不使用 OKKI 额度。Python 匹配器只返回 `HEURISTIC_SHORTLIST_ONLY`，不会自动发送报价或批准商务条件。

### 需要的输入

提供一个 RFQ 地址或当前可见标题/描述，以及自己的当前产品事实、确切型号素材和历史记录使用决定。可补充目标型号、获准单价、MOQ、港口和语言。私有文件留在仓库外；本地匹配命令应明确指定自己的 `--official-current`、`--catalog-index` 与 `--disposition`，避免误读默认或上级目录中的数据。历史记录只有符合上方英文复用规则才可作为参考；历史价不等于当前工厂批准价。

### 按顺序使用

1. 首次先用公开合成演示离线跑匹配和回归，确认工具可用，不接浏览器或私有数据。
2. 阅读实际 RFQ，确认有效性、产品类别、每项强制要求与明确允许改变的范围。
3. 用已核实事实生成候选，逐项查看模式、不匹配项与未解决作用域。高分不是结构冲突的豁免；AND 的每个强制要求都要满足，候选 OR 或斜杠选项不代表已选择配置。
4. `REVIEW` 只准备审核文本，不自动填；`SKIP_RFQ` 可以是正确的不适配结果。`CUSTOM` 必须获得覆盖全部不匹配项的明确许可，只允许改尺寸不能顺带改材质、安装或操作方式。
5. 人工确认实际型号、单位、价格、MOQ、贸易条件、港口和消息。明显单位错误先纠正；无可信价格或关键事实时保留未解决状态并停下。不借另一型号的价格、图片、证书或交期。
6. 只有流程获准、浏览器可用且所有守门项通过时，按上方英文流程填当前可见报价字段。素材上传另需批准且必须属于确切产品；回读每个值、RFQ 身份、单位、价格和消息。
7. 留在最终 `Submit quotation / 提交报价` 按钮前。这个技能的准备/填表过程不授权最终提交；另行动作需要当时针对该 RFQ 和确切报价的明确确认。

### 硬停止点与限制

遇到验证码、登录失效、需求过期、未知单位、无依据价格、目标型号冲突、意外写入请求或进入最终提交时立即停止，不调用未公开写入 API，不导出或迁移 Cookie/Storage。未知或附条件的定制许可、未识别强制材料、未解决否定、选项与重复冲突尺寸都不能靠分数绕过。保留页面供人工检查，但不把“建议填表”报告成“已报价”。

匹配器是有限英文规则，不是通用语义或多语言解析器。合理但未被支持的表述仍需人工审核；中文说明不能替代上方英文 `Matching Rule`、`Quote Style` 与停止条件的完整技术约束。
