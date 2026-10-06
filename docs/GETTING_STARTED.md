# Getting started

## English

Follow these steps in order. Start without private data or a browser login; add your own facts only after the local checks work.

## 1. Keep code and business data separate

Clone the public repository or extract its source archive into a clean project folder:

```bash
git clone <repository-url> seller-workflow-kit
cd seller-workflow-kit
```

Replace `<repository-url>` with this project's repository URL. Put your private business workspace in a separate sibling directory or another location outside the Git root. Do not place it in `docs`, `.agents`, a fixture folder, or the checkout's parent if that parent is itself a repository you upload.

Expected: the checkout contains public code, instructions, and synthetic fixtures only. If you copied an existing business folder, stop and start with a clean checkout; do not try to make private data safe merely by adding it to `.gitignore` after it was committed.

## 2. Create the Python environment

Use Python 3.13, the recommended version checked for this public release. The doctor accepts Python 3.11–3.13, but the other versions have not been re-tested for this release. Check `python --version` on Windows or `python3 --version` on Linux/macOS. You do not need to change PowerShell execution policy: call the environment's interpreter directly. Linux/macOS commands below are equivalents, not a claim that every operating-system combination was release-tested.

PowerShell:

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Expected: dependencies install into `.venv`, not into a global Python installation. If installation fails, read the first dependency error and confirm the selected interpreter version. This public release does not ship the legacy binary wheelhouse; use `requirements.txt`, not an old `verify.ps1` bootstrap.

## 3. Check the public package before using it

PowerShell:

```powershell
& '.venv/Scripts/python.exe' tools/check_public_release.py --root .
```

Linux/macOS:

```bash
.venv/bin/python tools/check_public_release.py --root .
```

Expected: the public-release check exits successfully. If it reports an unexpected file, private-looking content, or an incomplete package, stop and inspect that finding. Do not upload the checkout or continue with a live workflow while the package check is failing.

## 4. Run the synthetic examples and tests

PowerShell:

```powershell
$sellerkitDemo = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-demo-' + [guid]::NewGuid().ToString('N'))
& '.venv/Scripts/python.exe' tools/public_examples.py --output $sellerkitDemo
& '.venv/Scripts/python.exe' tools/verify.py
```

Linux/macOS:

```bash
sellerkit_demo="$(mktemp -d -t sellerkit-demo.XXXXXXXX)/examples"
.venv/bin/python tools/public_examples.py --output "$sellerkit_demo"
.venv/bin/python tools/verify.py
```

Expected: synthetic examples are generated outside the checkout, and local verification reports its actual outcome. Inspect failures, errors, and skipped tests. A deliberately blocked production adapter is an expected safety result, not evidence that Alibaba is unavailable. If a fixture is rejected, fix the fixture's required fields and types; do not weaken a validator to make the demo pass.

These commands do not install Node.js, an in-app browser, browser automation plugins, or connectors. Optional JavaScript/browser checks need their separately documented dependencies. Do not claim those checks passed when they were skipped.

## 5. Open this checkout in Codex

Open the repository root as the project's primary folder in Codex Desktop. The project skills are already under `.agents/skills`; do not create a second shadow copy of an older installed skill. Inspect the displayed skill path if identical names appear. Codex discovers repository skills and does not merge same-name copies; refresh or restart if a changed skill is not visible. See the [official skill documentation](https://developers.openai.com/codex/skills/).

Use a local-only request first:

```text
$alibaba-rfq-light
Use only the generated synthetic example files. Explain the shortlist and every unresolved product requirement. Do not open a browser or submit a quotation.
```

```text
$alibaba-edm-light
Validate the generated synthetic campaign plan locally. Explain each blocker. Do not open a customer list, fill a live form, send, schedule, or activate anything.
```

```text
$alibaba-similar-draft
Inspect the local synthetic product package and its approvals. Report offline checks and the production adapter block. Do not navigate, fill, upload, or save on live Alibaba.
```

Expected: Codex loads the skill from this checkout and works only on the explicit synthetic files. If a skill is absent or points elsewhere, fix project discovery before using business data. These skills do not install a browser connection themselves.

## 6. Check discovery without a private workspace

For a clean demo session, clear only this kit's process-level settings:

PowerShell:

```powershell
Remove-Item Env:SELLERKIT_ALIBABA_WORKSPACE -ErrorAction SilentlyContinue
Remove-Item Env:SELLERKIT_ALIBABA_CONFIG -ErrorAction SilentlyContinue
& '.venv/Scripts/python.exe' .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --pretty
```

Linux/macOS:

```bash
unset SELLERKIT_ALIBABA_WORKSPACE SELLERKIT_ALIBABA_CONFIG
.venv/bin/python .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --pretty
```

Expected in a clean checkout with no business markers in its ancestors: `PASS_WITHOUT_WORKSPACE`. That means package checks can run without private data; it does not mean a live workflow is enabled. If the doctor discovers a workspace unexpectedly, inspect environment settings and ancestor folders. If it reports missing public files or dependencies, repair the checkout/environment rather than borrowing files from an old private installation.

## 7. Configure your own private facts

Only do this when you intend to use your own business data. Keep the workspace outside Git, and point explicitly to it. Choose one configuration method; avoid conflicting settings.

PowerShell, using a sibling private directory you created:

```powershell
$env:SELLERKIT_ALIBABA_WORKSPACE = (Resolve-Path '../sellerkit-private').Path
& '.venv/Scripts/python.exe' .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --workspace $env:SELLERKIT_ALIBABA_WORKSPACE --strict-workspace --pretty
```

Linux/macOS:

```bash
export SELLERKIT_ALIBABA_WORKSPACE="$(cd ../sellerkit-private && pwd)"
.venv/bin/python .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --workspace "$SELLERKIT_ALIBABA_WORKSPACE" --strict-workspace --pretty
```

Resolution order for draft workspace discovery is explicit `--workspace`, workspace environment variable, a supported workspace value in the config environment variable, then recognized markers in current-directory ancestors. An empty folder is not a compatible business workspace.

Expected: strict doctor checks recognize your explicitly selected workspace or list its missing requirements. Inspect the skill's documented input layout and JSON schemas, then prepare your own current product facts, source references, allowed-use decisions, and exact-model assets. The neutral [example profile](../.agents/skills/alibaba-similar-draft/profiles/example-profile.v1.json) follows `sellerkit.alibaba-workspace-profile.v1`, but its placeholder hashes are not approved evidence and its write permissions are false. Replace the placeholders with your own verified files and decisions outside Git; do not reuse someone else's approved manifest.

### Optional local review/retrieval configuration

The optional review-ready workspace helpers use a separate, fuller configuration. Open [review-config.example.json](../src/review-ready-workspace/examples/review-config.example.json), save your own copy outside Git, and fill its values from your own local evidence. Its `approved=false` is intentional; set it to true only after human review of the actual configuration and evidence. A path-only JSON object is not a valid review configuration.

| Configuration item | What you supply locally |
| --- | --- |
| `schema_version` | Keep `sellerkit.review-config.v1` |
| `paths.project_root` | Your private review workspace root |
| `paths.website_replica_root` | Your local website mirror |
| `paths.website_candidate_root` | Your locally prepared website candidates |
| `paths.website_archive` / `paths.old_website_archive` | Your current and prior local website archives |
| `paths.alibaba_archive_root` | Your local Alibaba product archive |
| `paths.disposition_overlay_root` | Your record-use/disposition decisions |
| `expected` | Verified hashes and counts for your own files, not invented or copied values |
| `priority_models` | Your selected model identifiers, kept private |
| `allowed_website_hosts` | Hosts you actually intend to allow; not permission to crawl or upload |
| `website_release_id` | Your own local evidence-release identifier |

All seven `paths` entries must be absolute local paths. Point the review helpers to your private configuration:

PowerShell:

```powershell
$env:SELLERKIT_ALIBABA_CONFIG = (Resolve-Path '../sellerkit-private/review-config.json').Path
```

Linux/macOS:

```bash
export SELLERKIT_ALIBABA_CONFIG="$(cd ../sellerkit-private && pwd)/review-config.json"
```

Expected: missing, unapproved, or incompatible review configuration blocks business-data work rather than loading a hidden default. Keep the explicit draft workspace setting for draft commands; the two consumers have different input needs. Read any configuration error before continuing, and never make hashes/counts up merely to satisfy it.

If required evidence, schema-compatible settings, or approvals are missing, remain at local preparation. Retrieval/RAG and optional vision helpers can assist review, but do not turn an inferred feature or a similar photo into an approved product fact. Do not upload private files to a remote model without your organization's explicit decision to do so.

## 8. Choose the workflow and its stopping point

Read the appropriate skill before using a real page:

- [Product preparation](../.agents/skills/alibaba-similar-draft/SKILL.md): approve exact field values and sources; the bundled live adapter remains blocked. Do not use an offline receipt to unlock live writes.
- [RFQ](../.agents/skills/alibaba-rfq-light/SKILL.md): confirm the actual request, model, every conflict, and the commercial terms. A shortlist is not a quotation. Stop before final submission.
- [EDM](../.agents/skills/alibaba-edm-light/SKILL.md): validate the plan, use only the authorized maintained audience, and read the visible form back. Stop before any test send, scheduling, publish, final send, or paid activation.

For supported visible-page review, connect an available Codex in-app browser and log in yourself on that machine. Confirm the account and the actual platform module. Never install cookies, storage, browser profiles, tokens, or old approvals from another machine.

Expected: every action has a current target, exact approved values, and a visible readback. A missing browser tool, CAPTCHA, changed field, unknown price, or unimplemented production adapter is a stopping point—not a reason to call undocumented APIs or reuse an old session. See [Platforms and workflows](PLATFORMS_AND_WORKFLOWS.md) and [Troubleshooting](TROUBLESHOOTING.md).

---

# 入门指南

## 中文

请按顺序执行。先不接私有数据、不登录浏览器；本地检查完成后，再配置自己的业务事实。

## 1. 分开代码与业务数据

把公开仓库克隆或解压到干净项目目录：

```bash
git clone <repository-url> seller-workflow-kit
cd seller-workflow-kit
```

将 `<repository-url>` 换成项目仓库地址。私有业务工作区放在独立的同级目录或 Git 根目录之外的其他位置；不要放在 `docs`、`.agents`、测试材料目录里。如果上级目录本身也是会上传的仓库，同级目录也不够安全。

预期结果：仓库只包含公开代码、说明与合成测试材料。若复制的是已有业务文件夹，应重新建干净副本；私有数据已经提交后再加 `.gitignore`，不能消除泄露。

## 2. 创建 Python 环境

推荐 Python 3.13，本次公开发布已核验这个版本。doctor 接受 Python 3.11–3.13，但其他版本尚未针对本次发布复测。Windows 用 `python --version`，Linux/macOS 用 `python3 --version` 检查。不必修改 PowerShell 执行策略，直接调用虚拟环境解释器即可。下方 Linux/macOS 命令是对应写法，不代表所有操作系统组合都已完成发布验证。

PowerShell：

```powershell
python -m venv .venv
& '.venv/Scripts/python.exe' -m pip install -r requirements.txt
```

Linux/macOS：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

预期结果：依赖安装在 `.venv` 中，不修改全局 Python。安装失败时，先看第一个依赖报错并核对解释器版本。公开版不携带旧的二进制 wheelhouse；使用 `requirements.txt`，不要依赖旧 `verify.ps1` 的初始化逻辑。

## 3. 先检查公开包

PowerShell：

```powershell
& '.venv/Scripts/python.exe' tools/check_public_release.py --root .
```

Linux/macOS：

```bash
.venv/bin/python tools/check_public_release.py --root .
```

预期结果：公开包检查正常退出。如果发现多余文件、疑似私有内容或包不完整，停下来检查具体项；检查未通过时，不上传仓库，也不继续在线流程。

## 4. 跑合成演示与测试

PowerShell：

```powershell
$sellerkitDemo = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-demo-' + [guid]::NewGuid().ToString('N'))
& '.venv/Scripts/python.exe' tools/public_examples.py --output $sellerkitDemo
& '.venv/Scripts/python.exe' tools/verify.py
```

Linux/macOS：

```bash
sellerkit_demo="$(mktemp -d -t sellerkit-demo.XXXXXXXX)/examples"
.venv/bin/python tools/public_examples.py --output "$sellerkit_demo"
.venv/bin/python tools/verify.py
```

预期结果：合成演示生成在仓库外，本地验证输出实际结果。必须查看失败、错误与跳过项。生产适配器按设计阻断，是安全预期，不代表阿里巴巴不可用。若测试材料被拒绝，修复材料所需字段和类型，不降低校验条件。

这些命令不会安装 Node.js、应用内浏览器、自动化插件或连接器。可选的 JavaScript/浏览器检查需要其单独说明的依赖；被跳过的检查不能报告为通过。

## 5. 在 Codex 打开这个副本

在 Codex Desktop 中把仓库根目录设为项目主目录。技能已经放在 `.agents/skills`，不必再复制一套旧安装技能。同名技能出现时，查看实际显示的路径。Codex 会发现仓库技能，但不会合并同名副本；更新没有出现时可刷新或重启。机制见[官方技能说明](https://developers.openai.com/codex/skills/)。

先发只做本地工作的请求：

```text
$alibaba-rfq-light
只使用生成的合成演示文件，解释候选结果和每一项未解决的产品要求。不要打开浏览器，不提交报价。
```

```text
$alibaba-edm-light
只在本地校验生成的合成活动计划，解释每个阻断项。不要打开客户列表，不填在线表单，不发送、排期或开通。
```

```text
$alibaba-similar-draft
检查本地合成商品包和审批，报告离线检查与生产适配器阻断。不要在在线阿里巴巴页面导航、填写、上传或保存。
```

预期结果：Codex 从这个仓库副本加载技能，只处理指定的合成文件。技能缺失或指向别处时，先修复项目发现机制，再接业务数据。技能本身不会安装浏览器连接。

## 6. 不接私有工作区时检查发现机制

干净演示会话中，只清除工具箱自己的进程级设置：

PowerShell：

```powershell
Remove-Item Env:SELLERKIT_ALIBABA_WORKSPACE -ErrorAction SilentlyContinue
Remove-Item Env:SELLERKIT_ALIBABA_CONFIG -ErrorAction SilentlyContinue
& '.venv/Scripts/python.exe' .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --pretty
```

Linux/macOS：

```bash
unset SELLERKIT_ALIBABA_WORKSPACE SELLERKIT_ALIBABA_CONFIG
.venv/bin/python .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --pretty
```

预期结果：干净副本的上级目录也没有业务标记时，返回 `PASS_WITHOUT_WORKSPACE`。它只表示不接私有数据也能检查包，不代表在线执行已启用。意外发现工作区时，应检查环境变量和上级目录；缺少公开文件或依赖时，修复副本与环境，不从旧私有安装中随意借文件。

## 7. 配置自己的私有事实

只有计划使用自己的业务数据时才执行。工作区必须在 Git 之外，并明确指定。选择一种配置方法，避免多个设置互相冲突。

PowerShell，假设你已创建同级私有目录：

```powershell
$env:SELLERKIT_ALIBABA_WORKSPACE = (Resolve-Path '../sellerkit-private').Path
& '.venv/Scripts/python.exe' .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --workspace $env:SELLERKIT_ALIBABA_WORKSPACE --strict-workspace --pretty
```

Linux/macOS：

```bash
export SELLERKIT_ALIBABA_WORKSPACE="$(cd ../sellerkit-private && pwd)"
.venv/bin/python .agents/skills/alibaba-similar-draft/scripts/runtime_config.py doctor --skill-root .agents/skills/alibaba-similar-draft --workspace "$SELLERKIT_ALIBABA_WORKSPACE" --strict-workspace --pretty
```

草稿工作区发现的优先级依次是明确的 `--workspace`、工作区环境变量、配置环境变量中受支持的工作区值、当前目录的上级业务标记。一个空文件夹并不等于兼容的业务工作区。

预期结果：严格 doctor 识别到你明确指定的工作区，或列出缺失项。按技能说明的输入布局与 JSON 格式要求，准备自己的当前产品事实、来源、允许使用的记录与确切型号素材。中性的[示例 profile](../.agents/skills/alibaba-similar-draft/profiles/example-profile.v1.json) 遵循 `sellerkit.alibaba-workspace-profile.v1`，但占位哈希不是批准证据，写入权限全部为 false。应在 Git 之外用自己的核实文件与决定替换占位值，不重用别人的审批清单。

### 可选的本地审核/检索配置

可选的 review-ready 工作区辅助使用另一份内容更完整的配置。打开 [review-config.example.json](../src/review-ready-workspace/examples/review-config.example.json)，在 Git 外保存自己的副本，按自己的本地证据填写。示例 `approved=false` 是有意设置；只有人审核了实际配置与证据后才能改成 true。只有工作区路径的 JSON，不是有效的审核配置。

| 配置项 | 应提供的本地内容 |
| --- | --- |
| `schema_version` | 保持 `sellerkit.review-config.v1` |
| `paths.project_root` | 私有审核工作区根目录 |
| `paths.website_replica_root` | 本地网站镜像 |
| `paths.website_candidate_root` | 本地整理的网站候选 |
| `paths.website_archive` / `paths.old_website_archive` | 当前与此前的本地网站归档 |
| `paths.alibaba_archive_root` | 本地阿里巴巴商品归档 |
| `paths.disposition_overlay_root` | 记录允许使用/处置决定 |
| `expected` | 自己文件的已核实哈希与数量，不编造、不照抄 |
| `priority_models` | 自己选定的型号标识，保持私有 |
| `allowed_website_hosts` | 实际计划允许的域名，不代表获准抓取或上传 |
| `website_release_id` | 自己的本地证据版本标识 |

七个 `paths` 项都必须是本地绝对路径。把审核辅助指向私有配置：

PowerShell：

```powershell
$env:SELLERKIT_ALIBABA_CONFIG = (Resolve-Path '../sellerkit-private/review-config.json').Path
```

Linux/macOS：

```bash
export SELLERKIT_ALIBABA_CONFIG="$(cd ../sellerkit-private && pwd)/review-config.json"
```

预期结果：配置缺失、未批准或不兼容时，业务数据工作应阻断，不加载隐藏默认值。草稿命令继续保留明确工作区设置，因为两个消费者的输入需求不同。先读配置错误，再继续；不能为了通过检查而编造哈希或数量。

证据、兼容配置或审批缺失时，停在本地准备。检索/RAG 与可选视觉辅助可以帮助审核，但推测功能、相似图片不等于已批准产品事实。没有组织明确决定，不把私有文件上传给远程模型。

## 8. 选择流程与停止点

接真实页面前，先读对应技能：

- [商品准备](../.agents/skills/alibaba-similar-draft/SKILL.md)：批准确切字段值与来源，内置在线适配器仍阻断；离线回执不能解锁在线写入。
- [RFQ](../.agents/skills/alibaba-rfq-light/SKILL.md)：确认实际需求、型号、全部冲突和商务条款；候选清单不是报价，停在最终提交前。
- [EDM](../.agents/skills/alibaba-edm-light/SKILL.md)：校验计划，只使用获准的平台维护客群，回读可见表单；停在测试发送、排期、发布、最终发送或付费开通之前。

在受支持的可见页面审核流程中，连接可用的 Codex 应用内浏览器，并在当前机器上自己登录。确认账号和实际平台模块。不要安装或迁移另一台机器的 Cookie、Storage、浏览器配置、令牌或旧审批。

预期结果：每一步都有当前目标、准确批准值和可见回读。浏览器工具缺失、验证码、字段变化、未知价格或生产适配器未实现时，应停止对应操作，而不是调用未公开 API 或重用旧会话。详见[平台与流程](PLATFORMS_AND_WORKFLOWS.md)和[常见问题](TROUBLESHOOTING.md)。
