# Troubleshooting

## English

Start with the failed check and the actual command. Preserve a small synthetic reproduction; do not paste credentials, customer exports, private product records, or browser sessions into an issue.

## Python or dependency setup fails

Expected: use the release-checked Python 3.13 and install dependencies in `.venv` from the repository's `requirements.txt`. The doctor accepts 3.11–3.13; other accepted versions have not been re-tested for this public release.

Next step: confirm the interpreter version and use that environment's interpreter for both installation and verification. Read the first pip error rather than repeatedly running an old bootstrap. The legacy `verify.ps1` depends on a wheelhouse that the public release does not ship. Use `tools/verify.py`; do not assume it downloads Node.js, plugins, browser binaries, or account access.

## A clean demo discovers private data

Expected: synthetic demos use explicit files outside Git; a core-only doctor can return `PASS_WITHOUT_WORKSPACE`.

Next step: inspect `SELLERKIT_ALIBABA_WORKSPACE`, `SELLERKIT_ALIBABA_CONFIG`, and recognized workspace markers in the current directory's ancestors. Explicit arguments take precedence. Start from a neutral checkout if an ancestor belongs to a business workspace. Installed skill locations are code, not a source of business facts. Clearing environment variables alone does not remove ancestor discovery. Never solve this by copying private data into the public repository.

## A private workspace or profile is rejected

Expected: strict discovery validates the required files, and the profile follows the supplied JSON schema and binds the selected evidence.

Next step: use the skill's documented file layout and validate your own profile. A directory's existence, a renamed sample, or a source hash copied from another machine is insufficient. Relative paths in a config file are relative to that file. Keep credentials, customer files, approvals, and outputs outside Git. A local doctor pass does not enable the production browser adapter.

## A synthetic fixture fails on a suffix or missing field

Expected: the fixture follows the declared input format, file suffix, required fields, and strict value types.

Next step: regenerate with `tools/public_examples.py` into a fresh external directory, or correct your synthetic fixture according to the validator error. JSON and JSONL are not interchangeable. Renaming a suffix does not convert a file. A string such as `"true"` is not a boolean. Do not remove a type, privacy, or required-field check merely to accept an invalid example.

## Codex loads an old or duplicate skill

Expected: the selected skill path is inside this checkout's `.agents/skills`.

Next step: open the repository as the project primary folder and inspect the visible skill path. Repository and user-installed copies are not merged. Avoid shadow copies, refresh discovery, and restart if needed. Do not change unrelated global settings just to hide a mismatch. See the [official skill documentation](https://developers.openai.com/codex/skills/).

## Product publishing fields or locators changed

Expected: field targets match the current account/category page, including the actual visible label, role, locale, and constraints—not an old CSS selector alone.

Next step: stop and capture the current page read-only where the workflow permits. Review the field mapping, required fields, options, and dynamic schema again. A stale locator, different category, translated label, unknown rule, or expired page evidence requires a fresh review. Do not guess an equivalent control, force-fill by position, or switch to undocumented write APIs. The bundled production adapter remains blocked regardless of a locally valid mapping.

## Similar-product copying retains another item's content

Expected: every retained title, model, attribute, image, video, attachment, and claim belongs to the reviewed candidate.

Next step: compare the complete visible form with the candidate's approved fields. Template text and inherited videos can survive a copy even when the new title looks correct. Remove or replace them only in an authorized, supported workflow; otherwise record the blocker and stop. Another model's image or video is not a suitable default. Do not infer that a copied price, MOQ, certificate, or delivery promise is approved.

## The form is filled, but saving is unproven

Expected: a save claim has a visible save result and a verified reload/readback for the same draft. A filled form alone proves neither saving nor publishing.

Next step: do not report a saved draft from an in-memory page or an attractive screenshot. In a separately enabled workflow, verify the save result, draft identity, and post-refresh fields; report unknown inventory counts as unknown. Do not press Save repeatedly without resolving the previous outcome. With the bundled production adapter, live saving is not implemented and the correct result remains blocked.

## A product count or live inventory result is unknown

Expected: live counts come from a current, authenticated, supported observation, not from an archive or a local manifest.

Next step: label the value unknown and state the missing observation. Local file counts, historical archives, and synthetic inventory tests do not establish today's account inventory. Do not turn an unreadable counter into zero or a stale count into a successful save claim.

## RFQ returns REVIEW or SKIP

Expected: the matcher is a shortlist tool. Unsupported requirements, incomplete facts, conflicting mandatory clauses, and unselected candidate alternatives stop automatic filling. `SKIP_RFQ` is often the correct business result, not a test failure.

Next step: inspect the reported mismatch and unresolved scopes. Confirm whether the buyer really permits each proposed change and whether the current candidate facts prove a definite configuration. A high score, broad customization wording, an option list, or a known half of an unknown alternative cannot supply missing approval. The parser uses finite English rules, not unlimited semantic understanding. Unsupported legitimate prose needs human review.

## Price, MOQ, assets, or claims have no approved source

Expected: each commercial value and product claim has its own suitable source for the actual model.

Next step: leave the value unresolved and stop the corresponding action. Historical/public comparables are references, not current approved factory terms. Do not borrow another model's price or MOQ, carry a copied template's terms forward, use a similar image as exact evidence, or invent a certificate, warranty, lead time, or included accessory. Passing a structural match does not approve a quotation.

## EDM is unavailable or its quota looks inconsistent

Expected: Alibaba Customer CRM's own EDM page determines channel availability, sender readiness, audience, and quota.

Next step: inspect that module's visible status without consuming credits or enabling a paid service. MyAlibaba publishing access, RFQ limits, and OKKI credits do not prove EDM entitlement. An inquiry button or another platform's quota is not the needed evidence. Do not substitute a private recipient export for the maintained audience. Stop before test send, schedule, publish, final send, and paid activation.

## Tests pass with skips, or local hashes look convincing

Expected: verification reports executed tests separately from failures, errors, and skips. Local hashes show exact file consistency and drift, not browser origin, live permission, or a completed business action.

Next step: read the entire summary and state what did not run. An all-tests-discovered command is not an all-tests-passed claim when it skips browser-dependent coverage. `PASS_ZERO_WRITE` only confirms no write for the checked probe; it does not unlock filling. A `LOCAL_CONTRACT` receipt cannot authenticate the live browser. `BLOCKED_LIVE_ATTESTATION` is expected with the shipped production adapter; do not edit a readiness flag to bypass it.

## CAPTCHA, login loss, or missing browser tool

Expected: the operator logs in on the current machine and the selected browser tool is actually available.

Next step: stop live operations, keep local preparation results, and resolve login or tool availability yourself. Do not migrate cookies, storage, browser profiles, tokens, or sessions; do not bypass CAPTCHA. Python package installation does not configure the desktop browser connection.

---

# 常见问题

## 中文

先看失败检查和实际命令。保留一个小型合成复现，不把凭据、客户导出、私有产品记录或浏览器会话贴到 issue。

## Python 或依赖安装失败

预期结果：使用发布已核验的 Python 3.13，把依赖从仓库 `requirements.txt` 安装到 `.venv`。doctor 接受 3.11–3.13，其他接受版本尚未针对本次公开发布复测。

下一步：核对解释器版本，安装与验证都使用同一个虚拟环境解释器。先看第一个 pip 错误，不反复跑旧初始化脚本。旧 `verify.ps1` 依赖公开版不携带的 wheelhouse；请用 `tools/verify.py`，不要假设它会下载 Node.js、插件、浏览器或账号权限。

## 干净演示意外发现私有数据

预期结果：合成演示明确使用 Git 之外的文件；只检查包的 doctor 可以返回 `PASS_WITHOUT_WORKSPACE`。

下一步：检查 `SELLERKIT_ALIBABA_WORKSPACE`、`SELLERKIT_ALIBABA_CONFIG`，以及当前目录各级上级中的业务标记。明确参数优先。若上级属于业务工作区，应换到中性副本执行。技能安装目录是代码，不是业务事实来源。只清环境变量不能消除上级目录自动发现，不能靠把私有数据复制进公开仓库解决。

## 私有工作区或 profile 被拒绝

预期结果：严格发现检查所需文件；profile 遵循随包 JSON 格式，并绑定所选证据。

下一步：按技能的文件布局校验自己的 profile。目录存在、示例改名或复制另一机器的哈希，都不够。配置文件中的相对路径以该文件为基准。凭据、客户文件、审批与输出留在 Git 之外。本地 doctor 通过不能启用生产浏览器适配器。

## 合成材料因后缀或缺字段失败

预期结果：材料符合声明的输入格式、后缀、必需字段与严格类型。

下一步：用 `tools/public_examples.py` 在新的外部目录重新生成，或按报错修正自己的合成材料。JSON 与 JSONL 不能互换，改后缀不是格式转换。字符串 `"true"` 不是布尔值。不要为接纳无效示例删除类型、隐私或必需字段检查。

## Codex 加载了旧技能或重复技能

预期结果：所选技能实际路径位于当前副本的 `.agents/skills`。

下一步：把仓库作为项目主目录打开，查看技能显示路径。仓库技能与用户安装副本不会合并。避免影子副本，刷新发现，必要时重启；不为了掩盖路径不一致改无关全局设置。机制见[官方技能说明](https://developers.openai.com/codex/skills/)。

## 商品发布字段或定位方式变化

预期结果：字段对应当前账号/类目页面的实际标签、角色、语言与约束，而不是只匹配旧 CSS 选择器。

下一步：停止操作，在允许的流程中只读采集当前页面，再审核字段对应、必填项、选项和动态表单要求。旧定位、类目变化、翻译标签、未知规则或过期页面证据，都需要重新审核。不猜等价控件、不按位置强填、不转向未公开写入 API。即使本地字段映射有效，内置生产适配器仍然阻断。

## 类似产品复制残留了别的商品内容

预期结果：保留的标题、型号、属性、图片、视频、附件与声明，都属于审核过的候选。

下一步：把整个可见表单与候选批准字段比对。新标题正确，不代表复制模板中的文案、视频已清理。只有在获准且受支持的流程中才删除或替换；否则记录阻断项并停止。别的型号图片或视频不是合理默认值，复制的价格、MOQ、证书与交期承诺也不等于获得批准。

## 表单填好了，但无法证明已保存

预期结果：保存声明要有可见保存结果，以及同一草稿重新加载后的回读。填完表单既不证明保存，也不证明发布。

下一步：不根据页面内存状态或好看的截图报告已保存。另行启用的流程中，应核实保存结果、草稿身份与刷新后的字段；商品数量不知道就报告未知。上一结果未确认前，不重复点击保存。内置生产适配器没有实现在线保存，正确结果仍为阻断。

## 商品数量或在线库存结果未知

预期结果：在线数量来自当前、已登录且受支持的观察，不来自归档或本地清单。

下一步：标为未知，说明缺少哪项观察。本地文件数量、历史归档与合成库存测试，不能证明当前账号商品数量。读不到计数不能当成零，旧数量不能变成保存成功的证据。

## RFQ 返回 REVIEW 或 SKIP

预期结果：匹配器是候选工具；不支持的要求、事实缺失、强制条款冲突和候选未选择选项，都应阻止自动填。`SKIP_RFQ` 往往是正确业务结果，不是测试失败。

下一步：看不匹配项与未解决作用域。确认买家是否真的允许每项改变，以及候选当前事实是否证明确定配置。高分、泛泛的定制措辞、选项列表，或未知选项中的已知半句，都不能补出审批。解析器使用有限英文规则，不具备无限语义理解；合理但不支持的表述需要人工审核。

## 价格、MOQ、素材或声明没有批准来源

预期结果：每个商务值与产品声明，都有针对实际型号的独立适当来源。

下一步：保留未解决状态，停止对应动作。历史/公开对比价只是参考，不是当前工厂批准条件。不借另一个型号的价格或 MOQ，不沿用复制模板的条件，不把相似图片当确切证据，不编造证书、保修、交期或所含附件。结构匹配通过不能批准报价。

## EDM 不可用，或额度看起来不一致

预期结果：阿里巴巴客户通自己的 EDM 页面决定渠道、发件人、客群与额度状态。

下一步：只读检查该模块，不消耗额度，不开通付费服务。MyAlibaba 发布权限、RFQ 限制、OKKI 额度都不能证明 EDM 已获权限。询盘按钮或另一个平台的额度不是所需证据。不能用私有收件人导出替代维护客群。停在测试发送、排期、发布、最终发送或付费开通之前。

## 测试有跳过项，或本地哈希看起来很完整

预期结果：验证分别报告已执行测试、失败、错误与跳过项。本地哈希只证明文件字节一致与漂移，不证明浏览器来源、在线权限或业务动作已完成。

下一步：读完整总结，明确哪些没跑。全量发现测试不等于全量通过，尤其是跳过了浏览器覆盖时。`PASS_ZERO_WRITE` 只证明检查的探测没有写入，不解锁填表。`LOCAL_CONTRACT` 回执不能认证在线浏览器。当前生产适配器返回 `BLOCKED_LIVE_ATTESTATION` 是预期，不通过修改就绪标志绕开。

## 验证码、登录失效或浏览器工具缺失

预期结果：操作者在当前机器自己登录，所选浏览器工具确实可用。

下一步：停止在线动作，保留本地准备结果，自己处理登录或工具可用性。不迁移 Cookie、Storage、浏览器配置、令牌或会话，不绕过验证码。安装 Python 依赖不会配置桌面浏览器连接。
