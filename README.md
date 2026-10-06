# Alibaba Seller Workflow Kit

## English

Local review tools and Codex skills for preparing Alibaba seller work: product drafts, RFQ matching, and small EDM campaign plans.

Use it to organize your own product evidence, compare a buyer request with verified facts, and make a reviewable plan before touching a live form. It is not an unattended publisher, email sender, customer scraper, or production-ready browser adapter.

## What you can do

| Workflow | Available now | Live boundary |
| --- | --- | --- |
| Product preparation | Build and validate an evidence-backed candidate, field mapping, approval, and offline draft contract | The bundled production adapter is `BLOCKED_LIVE_ATTESTATION`; it does not enable live navigation, filling, uploading, or saving |
| RFQ matching | Generate a finite, conservative shortlist and flag missing or conflicting product facts | A person confirms product fit, commercial terms, and the visible quote; no automatic quotation submission |
| EDM planning | Validate a small campaign plan, privacy rules, and field types | The visible workflow stops before test send, scheduling, publishing, final send, or paid activation |

The skills are in `.agents/skills/`: `alibaba-similar-draft`, `alibaba-rfq-light`, and `alibaba-edm-light`. Optional archive, evidence, retrieval, and vision helpers can support local review; they are not a complete publisher or a customer-data collection service.

## Start with the offline demo

Use Python 3.13, the recommended release-checked version. Other versions accepted by the doctor have not been re-tested for this public release. Run from this repository's root. The demo uses synthetic inputs and does not need an Alibaba login, browser connection, private product files, or API key.

PowerShell:

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
& '.venv/Scripts/python.exe' tools/check_public_release.py --root .
$sellerkitDemo = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-demo-' + [guid]::NewGuid().ToString('N'))
& '.venv/Scripts/python.exe' tools/public_examples.py --output $sellerkitDemo
& '.venv/Scripts/python.exe' tools/verify.py
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/check_public_release.py --root .
sellerkit_demo="$(mktemp -d -t sellerkit-demo.XXXXXXXX)/examples"
.venv/bin/python tools/public_examples.py --output "$sellerkit_demo"
.venv/bin/python tools/verify.py
```

Expected: local checks finish successfully, synthetic outputs stay outside the repository, and the production draft adapter stays blocked. Read the reported failures and skipped tests; a skipped browser test is not a live pass. These commands do not install Node.js, browser tools, plugins, or login sessions.

## Use it with your own workflow

Follow [Getting started](docs/GETTING_STARTED.md) in order: offline checks → open the project in Codex → choose a skill → configure private facts outside Git → review exact values → inspect the live page manually where supported.

The intended browser surface is Codex Desktop's available in-app browser. Alibaba.com/MyAlibaba product publishing and RFQ are separate from Alibaba Customer CRM EDM. Historical OKKI integration context is not an included connector. See [Platforms and workflows](docs/PLATFORMS_AND_WORKFLOWS.md).

Keep private workspaces, customer data, approvals, screenshots, browser profiles, cookies, tokens, and generated business outputs outside this repository. Log in yourself on every machine; never copy another machine's session. Local hash checks prove file consistency, not live browser authenticity or permission to publish.

## Documentation

- [Ordered setup and expected results](docs/GETTING_STARTED.md)
- [Platforms, use cases, and stopping points](docs/PLATFORMS_AND_WORKFLOWS.md)
- [Common failures and next steps](docs/TROUBLESHOOTING.md)
- [Optional browser, workbook, and vision tools](docs/OPTIONAL_TOOLS.md)
- [Security and private-data handling](SECURITY.md), [Contributing](CONTRIBUTING.md), [Documentation design references](docs/DESIGN_INSPIRATION.md)
- [Product skill](.agents/skills/alibaba-similar-draft/SKILL.md), [RFQ skill](.agents/skills/alibaba-rfq-light/SKILL.md), [EDM skill](.agents/skills/alibaba-edm-light/SKILL.md)

Contributions should use synthetic fixtures and include a reproducible check. Do not attach customer exports, credentials, or private run evidence to issues or pull requests. See [LICENSE](LICENSE) for license terms. This is an independent project, not an official Alibaba or OpenAI product.

---

# 阿里巴巴卖家流程工具箱

## 中文

一套本地审核工具和 Codex 技能，用于准备商品草稿、比对 RFQ，以及整理小范围 EDM 营销计划。

你可以用它整理自己的产品证据，把买家需求与已核实的产品事实逐项比对，并在操作在线表单前生成可审核的方案。它不是无人值守发布器、邮件发送器或客户抓取工具，也没有可直接生产执行的浏览器适配器。

## 能做什么

| 流程 | 现在可用 | 在线边界 |
| --- | --- | --- |
| 商品准备 | 整理有证据的候选内容、字段对应、审批与离线草稿约束，并做本地校验 | 内置生产适配器为 `BLOCKED_LIVE_ATTESTATION`，不能据此在线导航、填写、上传或保存 |
| RFQ 比对 | 生成有限规则下的保守候选，并标出缺失或冲突的产品事实 | 人工确认产品适配、商务条件和可见报价；不会自动提交报价 |
| EDM 计划 | 校验小范围活动计划、隐私边界与字段类型 | 可见页面流程停在测试发送、排期、发布、最终发送或付费开通之前 |

三个技能位于 `.agents/skills/`：`alibaba-similar-draft`、`alibaba-rfq-light`、`alibaba-edm-light`。可选的归档、证据、检索与视觉辅助适合本地审核，不代表完整发布系统或客户数据采集服务。

## 先跑离线演示

推荐 Python 3.13，这也是公开发布已核验的版本。doctor 接受的其他版本尚未针对本次公开发布复测。在仓库根目录运行；演示只使用合成数据，不需要阿里巴巴登录、浏览器连接、私有产品文件或 API key。

PowerShell：

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
& '.venv/Scripts/python.exe' tools/check_public_release.py --root .
$sellerkitDemo = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-demo-' + [guid]::NewGuid().ToString('N'))
& '.venv/Scripts/python.exe' tools/public_examples.py --output $sellerkitDemo
& '.venv/Scripts/python.exe' tools/verify.py
```

Linux/macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tools/check_public_release.py --root .
sellerkit_demo="$(mktemp -d -t sellerkit-demo.XXXXXXXX)/examples"
.venv/bin/python tools/public_examples.py --output "$sellerkit_demo"
.venv/bin/python tools/verify.py
```

预期结果：本地检查完成，合成输出位于仓库外，生产草稿适配器仍保持阻断。必须查看失败项和跳过项；浏览器测试被跳过不等于在线通过。这些命令不会安装 Node.js、浏览器工具、插件，也不会安装或迁移登录会话。

## 接入自己的业务流程

按[入门指南](docs/GETTING_STARTED.md)执行：离线检查 → 在 Codex 打开项目 → 选择技能 → 在 Git 仓库外配置私有事实 → 审核确切字段值 → 在支持的流程中人工检查在线页面。

预期浏览器入口是 Codex Desktop 中可用的应用内浏览器。Alibaba.com/MyAlibaba 的商品发布、RFQ，与阿里巴巴客户通 EDM 是不同模块；历史 OKKI 集成背景不代表仓库包含其连接器。详见[平台与流程](docs/PLATFORMS_AND_WORKFLOWS.md)。

私有工作区、客户数据、审批、截图、浏览器配置、Cookie、令牌及业务输出必须放在本仓库之外。每台机器自己登录，不迁移另一台机器的会话。本地哈希检查只能证明文件一致，不能证明浏览器证据真实，更不能授权发布。

## 文档

- [按顺序配置与预期结果](docs/GETTING_STARTED.md)
- [实际平台、适用场景与停止点](docs/PLATFORMS_AND_WORKFLOWS.md)
- [常见失败与处理办法](docs/TROUBLESHOOTING.md)
- [可选浏览器、表格和识图工具](docs/OPTIONAL_TOOLS.md)
- [安全与私有数据处理](SECURITY.md)、[贡献指南](CONTRIBUTING.md)、[文档结构参考](docs/DESIGN_INSPIRATION.md)
- [商品技能](.agents/skills/alibaba-similar-draft/SKILL.md)、[RFQ 技能](.agents/skills/alibaba-rfq-light/SKILL.md)、[EDM 技能](.agents/skills/alibaba-edm-light/SKILL.md)

贡献请使用合成测试材料并附可复现检查。不要把客户导出、凭据或私有执行证据放进 issue、PR。许可条款见 [LICENSE](LICENSE)。这是独立项目，不是阿里巴巴或 OpenAI 官方产品。
