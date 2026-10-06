# README design notes / README 结构研究

## English

The README structure was researched on 2026-10-06 using public, high-star repositories. Their layout informed this kit; their source code and text were not copied.

| Reference | Useful pattern | Applied here |
| --- | --- | --- |
| [Playwright](https://github.com/microsoft/playwright) | Explain capabilities before installation; link deeper guides | Capability table, platform names, separate workflow guide |
| [uv](https://github.com/astral-sh/uv) | Short quick start followed by task-specific documentation | Copyable setup order, offline demo, troubleshooting |
| [Agent Skills](https://github.com/anthropics/skills) | Show skill folders, entry points, and concrete usage | `.agents/skills`, named skills, supported scenarios and limits |
| [GitHub README guidance](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) | State what the project does, why it helps, and how to start | Plain-language introduction and clear first-run commands |

A popular README is not evidence that its software or our software is production-ready. We explicitly separate offline validation, visible browser procedures, unimplemented live adapters, and final actions. Every guide presents a complete English section first and a complete Chinese section second; translated headings point to the same executable commands.

This publication contains generic source and fictional fixtures. It deliberately excludes operational datasets, account state, historical company-specific profiles, production screenshots, private review transcripts, and model weights. Optional browser and vision packages are installed separately, not bundled.

## 中文

2026-10-06 调研了以下 GitHub 高星公开仓库。这里只借鉴组织方式，没有复制其源码或原文。

| 参考 | 值得借鉴的方式 | 本仓库采用的方式 |
| --- | --- | --- |
| [Playwright](https://github.com/microsoft/playwright) | 先说明能做什么，再讲安装，深入内容另设指南 | 能力表、真实平台名称、独立流程指南 |
| [uv](https://github.com/astral-sh/uv) | 简短快速开始，加上按任务划分的详细文档 | 可复制配置顺序、离线示例、踩坑手册 |
| [Agent Skills](https://github.com/anthropics/skills) | 展示 Skill 目录、入口和使用方式 | `.agents/skills`、Skill 名称、适用场景和限制 |
| [GitHub README 指南](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) | 说清项目是什么、为什么有用、如何开始 | 直白介绍和首次运行命令 |

高星 README 并不证明参考项目或本项目已经能用于生产。本项目明确分开离线校验、可见浏览器操作流程、尚未实现的实时适配器和最终动作。各指南先给完整英文版，再给完整中文版；两种语言使用相同的可执行命令。

公开内容是通用源码和虚构测试。真实业务数据、登录状态、历史企业配置、生产截图、私密审查对话和模型权重均不随仓库发布。可选浏览器与识图依赖需另行安装，不捆绑上传。
