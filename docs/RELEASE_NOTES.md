# Public source release notes / 公开源码发布说明

## English

This first public edition includes the three core Codex skills, local validators, JSON schemas, state/approval contracts, synthetic regression fixtures, source packaging/privacy tools, and optional local evidence/matching/vision source. The code is MIT-licensed. External platforms and optional SDKs are not re-licensed or bundled.

The Windows/Python 3.13 offline validation ran 700 test methods: 694 passed and 6 business-workspace-dependent methods skipped. A separate local-data integration test class skipped before running any methods because no approved private configuration was provided. RFQ (207 methods), EDM (63), and the archive chain (44) had no failures or errors. The draft group ran 333 methods and release-tool tests ran 53. RFQ and EDM synthetic demo CLIs also completed successfully.

The first public test run exposed a missing HTML-parser dependency and a filename-boundary error introduced during anonymization. Both were fixed in the public copy, the complete archive group was re-run, and the draft group was re-run after removing private-wheelhouse requirements from the public doctor. Initial failure logs are preserved locally, not shipped. The resolved test groups were not represented as a new formal review or a fresh single-run aggregate.

Known parser fixes include retaining comma/slash option branches and unsupported mandatory requirements in RFQ review gates, retaining split credential aliases in EDM prose, and respecting correctly typed metadata keys. These are finite rules with synthetic regressions, not general language understanding or a universal privacy guarantee.

Not release-tested: real Alibaba writes, draft saving, RFQ submission, email sending, GPU/OCR inference, and the optional Chromium harness. The production draft adapter remains `BLOCKED_LIVE_ATTESTATION`, `production_execution_ready=false`, with final writes permanently disabled. No production account, catalog, customer information, login state, or private review logs are included.

## 中文

首个公开版包含三个核心 Codex Skill、本地校验器、JSON schema、状态与审批约束、虚构回归测试、源码打包与隐私检查工具，以及可选的本地证据/匹配/识图源码。源码使用 MIT；外部平台及可选 SDK 不因此改变许可证，也不随仓库打包。

Windows/Python 3.13 离线验证共执行 700 个测试方法：694 个通过，6 个依赖业务工作区的方法跳过。另一个本地数据集成测试类因没有已批准私有配置，在执行方法前整体跳过。RFQ 207 项、EDM 63 项、归档链路 44 项均无失败或错误；商品草稿组执行 333 项，发布工具测试 53 项。RFQ 和 EDM 虚构示例 CLI 也运行成功。

首次公开版测试发现缺少 HTML 解析依赖，以及匿名化文件名破坏型号边界。两处均只在公开副本修复，并重跑完整归档组；公开 doctor 去掉私有 wheelhouse 要求后又重跑商品草稿组。初始失败日志留在本地，不随包上传。修正后的分组结果没有冒充新一轮正式代码审查，也没有冒充一次全新单轮总测试。

已有修复包括：RFQ 不丢弃逗号/斜杠选项及不支持的强制要求；EDM 不漏掉被普通文案分隔的凭据别名，并尊重合法元数据的真实类型。这些是有限规则及虚构回归，不是通用语义理解，也不是绝对隐私保证。

本次未验证真实 Alibaba 写入、保存草稿、提交 RFQ、发送邮件、GPU/OCR 推理及可选 Chromium 模拟测试。生产商品适配器仍为 `BLOCKED_LIVE_ATTESTATION`、`production_execution_ready=false`，最终写入永久关闭。仓库不含生产账号、实际商品库、客户信息、登录状态或私密审查日志。
