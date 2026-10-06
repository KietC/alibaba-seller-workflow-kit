# Contributing / 参与贡献

## English

1. Fork or clone the repository. Install Python 3.13 and Node.js 22 if you need JavaScript tooling.
2. Create a virtual environment and install `requirements.txt` following [Getting started](docs/GETTING_STARTED.md).
3. Make a focused change. Use fictional fixtures, reserved example domains, and English-first/Chinese-second explanations for non-obvious safety behavior.
4. Run `python tools/check_public_release.py --root .`, `python tools/public_examples.py --output <outside-repo-directory>`, and `python tools/verify.py`.
5. Describe the problem, changed behavior, actual test results, skips, and limitations. Do not label offline tests as production validation.

Contributions are licensed under MIT. Dependency packages retain their own licenses. Do not copy third-party source-available components into the repository without confirming their license permits redistribution.

## 中文

1. Fork 或克隆仓库。安装 Python 3.13；需要 JavaScript 工具时安装 Node.js 22。
2. 按[配置指南](docs/GETTING_STARTED.md)创建虚拟环境，安装 `requirements.txt`。
3. 每次改动保持集中。测试只用虚构数据和保留示例域名；不直观的安全逻辑应有英文在前、中文在后的说明。
4. 执行 `python tools/check_public_release.py --root .`、`python tools/public_examples.py --output <仓库外目录>` 和 `python tools/verify.py`。
5. 说明问题、行为变化、实际测试结果、跳过项和限制。不要把离线测试描述成生产验证。

贡献代码采用 MIT 许可证；依赖包保留各自许可证。第三方“源码可见”组件不一定开源，未确认可再分发之前不要复制进来。
