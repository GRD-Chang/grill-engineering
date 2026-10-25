# 第一次使用 Agent Run

[English](quickstart.en.md) · [支持矩阵](support.md)

目标：安装后启动一个真实任务，能看到 Worker 工作并知道何时需要自己批准。
下载、人工登录、依赖准备和模型/CI 等待分别计时，不承诺固定时间完成开发。

## 1. 确认电脑和账号

当前基线使用 Linux、可达的 systemd user manager 254+ 和可工作的 bwrap；
仅装好可执行文件不够。Python、SQLite、Git、gh、Codex 的准确要求及补齐方式见
[安装指南](../install.md#环境缺项)。无 systemd 的云环境当前尚未就绪，
不要通过关闭隔离或伪造 manager 继续。

需要你完成 Codex 登录及选定 GitHub 身份的授权。普通 host 模式可复用已有 gh 登录；
Runner PAT/App 不要求额外登录 host gh。目标项目的测试/构建工具仍需准备，
Runner 自己的 Python 环境不代替项目环境。示例只用 Python 标准库。
实际 Codex 子进程还必须能发现项目要求的 Matt Skills，见安装指南。

## 2. 安装同一版本

从已授权的维护者入口取得所选版本的固定产品包，独立核对归档 SHA256，
解压后先在包根目录只读校验，再启动安装：

```sh
./install.sh --verify-release
./setup.sh
```

`--verify-release` 核验 manifest、完整文件清单、wheel 和哈希依赖锁，不创建安装状态或调用模型。
产品包含产品及四个依赖 wheel，候选 Python 环境离线安装；不含 Python、系统工具或 Skills。
校验前须有 CPython 3.11+，setup 还需 timeout；同包 checksum 不能代替可信来源。
同版完整文档和示例从维护者的同版文档入口取得，当前产品包只附简短安装说明。
私有候选构建成功不代表已获公开发布授权或已建立公开下载入口。

阅读计划后确认；缺管理员权限由管理员补齐，登录由你完成。Setup 不授予权限或创建账号。
安装包含一次真实 Codex 兼容调用，会消耗账号额度。
从新登录 shell 检查：

```sh
command -v agent-run
agent-run doctor --local --json
agent-run settings show --json
```

doctor 的退出码 0 不代表全部就绪，要看报告状态；`--local` 不检查账号认证，
只检查工具和身份引用。不要直接把原始配置输出贴到公开 Issue。
更新、回滚和卸载继续用 [现有安装生命周期](../install.md#更新回滚与卸载)。

## 3. 选择已有身份方式

Runner 和 Worker 各自支持 host/PAT/App，默认都是 host；无需为了首次使用创建 App。

| 方式 | 你需要准备 | 设置入口 |
| --- | --- | --- |
| host | 同一用户的原生 gh 登录，以及所需仓库权限 | `agent-run settings configure --runner-github-source host --worker-github-source host` |
| PAT | 已授权 token 的环境变量或私密文件引用，不把值放进命令或 Run | [PAT 设置](../user-defaults.md#github-身份) |
| App | 已有 App/installation 与私钥文件引用；签发留在 Runner | [App 设置](../user-defaults.md#app-配置与迁移) |

可以混合选择两端身份。Worker 的 GitHub 能力由凭据决定，不由本地命令白名单限制；
拥有写权限不授予自我发布或批准职责。新任务固定来源和引用，恢复不静默切换身份。

需要验证时明确执行 `agent-run doctor --online --repo OWNER/REPO --json`。
这会按所选 host/PAT/App 检查 Runner/Worker 认证和 Runner 仓库读取，App 可能签发短期 token；
不调用模型，不证明写权限或实际 sandbox 内的 Worker 认证。详细边界见[分阶段准备](../preparation.md)。
`doctor --local --mode foreground` 只诊断并报告 `not_integrated`，不是前台启动命令。

## 4. 启动一个小任务

先按 [最小示例](../../examples/first-ticket/README.md) 准备自己的仓库副本和 Parent；
真实建库、Issue 和 CI 操作由你授权执行。确认内容已推送、任务标签/依赖正确，
所选 Runner 能更新受管分支，CI 覆盖默认分支和 `agent-run/**/run`。
将下面的占位值换成实际值：

```sh
agent-run run PARENT_NUMBER --repo OWNER/REPO
agent-run status --parent PARENT_NUMBER --repo OWNER/REPO --json
agent-run history --parent PARENT_NUMBER --repo OWNER/REPO --json
```

命令回执只是已受理，不是已完成。Runner 在独立 clone 开发，不修改你的原 checkout；
Development 可编辑工作文件，权威 Git metadata 只读，Review 的交付 checkout 也只读。
Worker 仍按受信 Agent 模型运行，这不是对恶意代码的通用沙箱承诺。
真实 Worker 开始应由本轮 Invocation/进程事实确认，不能用 help 或安装探针代替。

## 5. 保留工作，按当前门禁继续

当前后台模式在终端离开后继续，Ctrl-C 只退出观察。使用查询返回的准确 `RUN_ID` 暂停和继续：

```sh
agent-run stop RUN_ID --repo OWNER/REPO
agent-run resume RUN_ID --repo OWNER/REPO
```

核对 status 中的实际停止/继续结果，不只看命令回执。恢复保留原工作和身份；
普通恢复不重置 Attempt 或预算，额度耗尽按 [操作指南](../agent-guide.md) 的当前门禁处理。
未知外部写先按原意图读回，不能直接重放。
最终 PR 就绪后，先检查准确成果、Review 和 CI，再由维护者明确批准后执行：

```sh
agent-run approve RUN_ID --repo OWNER/REPO
```

批准不适用于之后改变的候选；pending/unknown checks 不算通过。最后确认实际合并和完成状态。
卡住时用 [无秘密报告模板](diagnostics.md)，不要删除 Run、改数据库、扩大 token 权限或反复重跑。
