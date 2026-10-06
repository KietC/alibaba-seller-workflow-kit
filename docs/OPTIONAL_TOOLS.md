# Optional tools / 可选工具

## English

The root Python setup is sufficient for the offline RFQ/EDM demos and local contract tests. Do not install all optional tools at once. This repository's source is MIT; Codex and Alibaba are external proprietary platforms, not included open-source services. Optional dependencies keep their own licenses.

### Node.js and the synthetic browser harness

Install Node.js 22 separately. Check `node --version` and `npm --version`. The browser harness is an optional Playwright test against a temporary `127.0.0.1` fixture, not the real Alibaba website or Codex browser.

From the repository root, in PowerShell:

```powershell
Push-Location '.agents/skills/alibaba-similar-draft/tests/offline-iab-harness'
npm install --no-save --package-lock=false playwright
npx playwright install chromium
$env:SELLERKIT_HARNESS_OUTPUT = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-browser-' + [guid]::NewGuid().ToString('N'))
node harness.spec.mjs
Pop-Location
```

Linux/macOS equivalent:

```bash
cd .agents/skills/alibaba-similar-draft/tests/offline-iab-harness
npm install --no-save --package-lock=false playwright
npx playwright install chromium
SELLERKIT_HARNESS_OUTPUT="$(mktemp -d -t sellerkit-browser.XXXXXXXX)" node harness.spec.mjs
```

Expected after a successful run: `FINAL_REVIEW_READY_LOCAL_CONTRACT`, `browser_live_attested=false`, and `serverFinalWrites=0`. Read the receipt rather than assuming success from a launched browser. Installation downloads a dependency and browser executable; the fixture test is loopback-only. These optional installation/browser commands are not part of the release's Python test result. Never copy real browser profiles or cookies into this harness. See the [harness reference](../.agents/skills/alibaba-similar-draft/references/offline-iab-harness.md).

### Workbook bridges

Some preserved candidate-workbook scripts call Codex's `@oai/artifact-tool` bridge. That SDK is not redistributed here and is not installed by `npm install`. Those scripts require an available Codex-provided spreadsheet runtime; they are not standalone npm exporters. If the runtime is absent, use the JSON review material and mark workbook generation unavailable. Do not replace a missing runtime with an invented output or claim a workbook test passed when it skipped.

### Local images, OCR, and similarity

These source helpers are optional: `src/review-ready-workspace/scripts/vision_openclip_worker.py`, `vision_ocr_worker.py`, and `src/review-ready-plugin/mcp/review_ready/vision.py`. They use separate subprocesses for Torch/OpenCLIP and OCR engines. Model weights, GPU drivers, private images, and model environments are not bundled.

Configuration order:

1. Finish the root offline setup first.
2. Prepare an approved external review configuration and exact-model local assets, following [Getting started](GETTING_STARTED.md).
3. Inspect each worker's `--help` with your selected Python. Set up a separate environment for the chosen engine using its official installation instructions: [PyTorch](https://pytorch.org/get-started/locally/), [OpenCLIP](https://github.com/mlfoundations/open_clip), or [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR). Do not mix incompatible CUDA/Paddle/Torch environments.
4. Supply explicit local paths and record model revision, weight SHA-256, coverage, and errors. Do not put assets or weights in this repository.
5. Compare the same fixture set with one worker and multiple workers before trusting optimized results. A similar image is a ranking hint, not proof of identical specifications or an approved commercial claim.

No GPU/OCR/model inference is claimed as release-tested. Missing model dependencies must remain a visible degraded or blocked condition.

## 中文

根目录 Python 配置已经足够跑 RFQ/EDM 离线示例和本地约束测试，不用一口气装所有可选工具。本仓库源码使用 MIT；Codex、Alibaba 是外部专有平台，不是本仓库附带的开源服务。可选依赖保留各自许可证。

### Node.js 与模拟浏览器测试

单独安装 Node.js 22，先检查 `node --version`、`npm --version`。Playwright 测试只访问临时 `127.0.0.1` 模拟页面，不操作真实 Alibaba，也不证明 Codex 浏览器已认证。

在仓库根目录用 PowerShell 执行：

```powershell
Push-Location '.agents/skills/alibaba-similar-draft/tests/offline-iab-harness'
npm install --no-save --package-lock=false playwright
npx playwright install chromium
$env:SELLERKIT_HARNESS_OUTPUT = Join-Path ([System.IO.Path]::GetTempPath()) ('sellerkit-browser-' + [guid]::NewGuid().ToString('N'))
node harness.spec.mjs
Pop-Location
```

Linux/macOS 对应命令：

```bash
cd .agents/skills/alibaba-similar-draft/tests/offline-iab-harness
npm install --no-save --package-lock=false playwright
npx playwright install chromium
SELLERKIT_HARNESS_OUTPUT="$(mktemp -d -t sellerkit-browser.XXXXXXXX)" node harness.spec.mjs
```

成功时应有 `FINAL_REVIEW_READY_LOCAL_CONTRACT`、`browser_live_attested=false` 和 `serverFinalWrites=0`。启动了浏览器不等于通过，要看回执。安装步骤会下载依赖和浏览器，模拟测试只访问回环地址。这些可选安装与浏览器命令不算在本次 Python 测试结果里。不要向模拟环境复制真实浏览器资料或 Cookie，详见[模拟测试说明](../.agents/skills/alibaba-similar-draft/references/offline-iab-harness.md)。

### 审核表格桥接

保留的部分审核表脚本调用 Codex 的 `@oai/artifact-tool` 桥接。该 SDK 不随仓库分发，普通 `npm install` 也不会安装它，需要当前 Codex 提供可用的电子表格运行时。因此它不是独立 npm 表格导出器。没有该运行时时，先使用 JSON 审核材料，并明确说明表格生成功能不可用；不能伪造文件或把跳过测试说成通过。

### 本地图片、OCR 与相似度

可选源码是 `src/review-ready-workspace/scripts/vision_openclip_worker.py`、`vision_ocr_worker.py` 和 `src/review-ready-plugin/mcp/review_ready/vision.py`。Torch/OpenCLIP 与 OCR 分进程运行。模型权重、显卡驱动、私有图片和推理环境均不打包。

配置顺序：

1. 先跑通根目录离线环境。
2. 按[入门指南](GETTING_STARTED.md)准备仓库外的已审核配置和确切型号素材。
3. 用选定 Python 查看各 worker 的 `--help`。按官方说明为选定引擎单独建环境：[PyTorch](https://pytorch.org/get-started/locally/)、[OpenCLIP](https://github.com/mlfoundations/open_clip)、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)。不要混装不兼容的 CUDA/Paddle/Torch 环境。
4. 显式指定本地路径，记录模型版本、权重 SHA-256、覆盖率和错误。素材和权重不要放仓库。
5. 同一批虚构测试数据先比较单 worker 与多 worker 结果，再采用优化结果。识图相似只用于排序，不能证明规格相同或批准商业宣称。

本次发布没有宣称 GPU/OCR/模型推理已验收。缺少依赖必须明确显示降级或阻断。
