# 本项目开发

以下环境仅用于本仓库开发和测试，普通用户按 [Agent 操作指南](agent-guide.md)安装：

```bash
python -m venv .venv
source .venv/bin/activate
make test-bootstrap # 首次或依赖变化：固定开发依赖、安装源码、准备离线 wheel
make test-policy  # 示例：策略模块的局部反馈
make test         # 本地快速回归，再补测受影响模块
make test-full    # 完整测试，CI 与最终验收使用
make typecheck
```

修改后按影响选择[测试入口](agents/test-commands.md)；测试编写、扩大验证和隔离要求见
[测试指南](agents/testing.md)。CI 保留完整测试、类型检查与安装后 CLI 检查；
需要比较 CI 时使用 `make test-report TEST_WORKERS=2`，详见测试入口中的诊断说明。
