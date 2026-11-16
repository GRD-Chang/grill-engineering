# 无秘密诊断与问题模板

使用 [JSON 模板](diagnostic-report.template.json) 手动填写公开摘要，保留字段白名单。
这不是新的 doctor 或自动采集器，不读取环境变量、账号文件或 Run 数据，也不自动上传。
先在本地查看 `agent-run doctor --local --json` 及安装回执；任务问题再在本地看 status/history。
明确需要认证/仓库读取时才运行 `doctor --online --repo OWNER/REPO --json`；
此检查使用所选身份，App 可能签发短期 token，不调用模型。报告中仍有未验证的能力。
**不要原样附上这些输出**：其中可能有路径、仓库身份、引用或任务正文。

只填写：准确 Runner commit/安装内容 SHA256、OS/架构、纯工具版本号、运行模式、
两端身份来源类型，以及能力状态/失败阶段/稳定错误码。未知填 `null` 或 `unknown`，
未检查填 `unrun`；能力状态只用 `pass/fail/blocked/unrun`。
identity_sources 只用 `host/pat/app/unknown`；mode 只用 `systemd-background/unknown`，
前台适配交付后再扩展。只复制短错误码，不复制任意异常消息或原始 stderr。

不填 HOME、绝对路径、用户名、仓库/Issue/PR 标识、环境变量及其值、PAT 引用位置、
私钥路径/内容、hosts.yml、auth.json、cookies、完整命令、HTTP headers/body、任务正文或日志。
此模板不是任意文本的脱敏保证：发布前由用户预览并自行决定分享。
原始证据留在仓库外的私有现场；公开摘要不足时与维护者另行约定必要材料，避免公开泄露。

## 安装 / 环境问题（复制到 Issue）

```text
标题：[安装] 简短现象（不含路径、账号或仓库名）
所选发行版本 / 归档 SHA256：
预期阶段：下载 / 准备依赖 / 安装 / 激活 / 执行就绪
实际阶段与稳定错误码：
是否发生真实 Worker 启动：是 / 否 / 未确认
附：已逐字段检查的 diagnostic-report 摘要
```

## 任务运行问题（复制到 Issue）

```text
标题：[运行] 简短现象（不含任务正文）
Runner 版本与运行模式：
类别：开发 / Review / CI / 批准 / 停止 / 恢复 / 其他
实际现象：用不含私密材料的一两句描述
最近公开操作种类：run / stop / resume / approve / query
是否已确认实际停止或完成：是 / 否 / 未确认
附：已逐字段检查的 diagnostic-report 摘要
```

权限不足、分支规则拒绝、CI 没有触发、pending/unknown 和程序失败分别描述。
缺 systemd 不要通过换 fake manager、关闭隔离或改安全设置“修复”；修复引用后公开 resume，
不删 Task、Run 或成果，不编辑数据库，不盲目重放未知外部写。
