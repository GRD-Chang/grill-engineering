# agent-run

`agent-run` 是一个显式启动的本地 Codex 自动交付控制器。维护者以 GitHub Parent Issue
定义交付范围；Controller 依次驱动开发、独立验收、Required Checks、PR 发布和恢复，
最终是否进入默认分支仍由维护者通过 `approve` 决定。

## 首次安装

Linux 用户在 GitHub release 页下载所选 release tag 的 **Source code** 归档，解压并进入源码目录，
执行唯一首次准备入口。下载和解压可以使用浏览器及系统归档工具，无需预先安装 Python 或 Git：

```bash
./setup.sh
```

已有 Git 的开发者也可以 clone 并选择 tag、branch 或本地修改，然后执行同一入口。
先审阅所选源码；安装器与构建后端拥有当前用户的代码执行权限。

Setup 集中显示依赖检查、拟执行变更和人工待办，再统一确认一次。兼容工具直接复用；
确认前执行可行的只读认证、bubblewrap namespace 和 user systemd 会话检查；构建前置
不满足或安装失败时仍重新汇总这些检查。认证与会话环境的原始输出不展示。
创建短命 systemd unit 及受管 Runner 的 doctor 检查明确后置到确认且安装成功之后。
Ubuntu/Debian 自动准备仅补齐缺失或不兼容的 Runner 依赖，不升级整个系统。
确认不代替管理员授权或账号认证；拒绝或缺少权限时不执行相应修改。其他 Linux 跳过
未适配步骤，继续可行检查和安装，缺关键构建或激活条件时不绕过门禁。

人工补齐后，在同一源码目录重跑 `./setup.sh`。报告区分“Runner 未安装或新版本未激活”、
“Runner 已安装但执行环境未就绪”和“安装及执行条件均满足”。旧 Active 仍可用不代表新版
已激活，宿主就绪也不代表目标仓库已配置完成。实际来源、版本、架构和缺项原因见本次检查计划。
退出码 `0` 表示安装及执行条件满足，`1` 表示安装失败或未激活，`2` 表示已安装但未就绪。
外层使用 POSIX shell 和系统 coreutils（含 `timeout`）；系统补装还需 util-linux 的 `flock`。
Ubuntu/Debian 只使用已有 APT 软件源，以 `apt-cache policy` 展示候选，再通过
在同一 Runner 管理租约下刷新已有源索引，再以 `apt-get install --no-install-recommends` 补齐缺项，不添加源。Codex 和不可用的 user systemd
转为人工待办；仓库候选版本达不到基线也需人工处理。`./setup.sh --yes` 可预先确认计划，
但不提供提权；没有可用管理员权限时跳过系统变更，操作者完成授权后重跑。
Linux 以外平台不在本轮范围；Linux 仍须实际具备 user systemd 和 bubblewrap 能力。
平台证据与显式 opt-in 方法见[一次性平台验收](docs/runner-setup-validation.md)。

如果已有软件源仍提供不兼容版本，按缺项处理后重跑：

| 缺项 | 人工处理与再次验证 |
| --- | --- |
| CPython / Git 太旧 | 由管理员选择提供 CPython 3.11+、Git 2.40+ 的系统版本或软件源；检查 `python3 --version`、`git --version`，重跑 Setup |
| gh 缺少 `api --slurp` 等选项 | 由管理员按 [GitHub CLI 官方 Linux 安装说明](https://github.com/cli/cli/blob/trunk/docs/install_linux.md)选择来源；检查 `gh api --help`、`gh auth status` |
| Codex 缺失或调用选项不兼容 | 按 [Codex 官方安装说明](https://github.com/openai/codex)选择适合架构的发行物或 npm 安装；自行登录，再检查 `codex exec --help`、`codex exec resume --help` 和 `codex login status` |
| user systemd / bubblewrap 不可用 | 在真实 Linux 用户登录会话检查 `systemctl --user show-environment`；由管理员定位 namespace/安全策略拒绝原因，重跑 Setup 的短命 unit 与 bubblewrap 探针，不用关闭全机防护来通过 |

Setup 不替用户选择或添加第三方软件源，不修改已有 Codex 安装；因此这类缺项会保留为人工待办。

底层 `./install.sh` 保留不可变 Runner 安装生命周期，不自动准备宿主，要求 CPython 3.11+、
`venv`、`pip`、构建后端和能完成 Compatibility Check 的 Codex CLI。Git metadata 不是构建前置；
构建或激活失败保留旧 Active Runner。

branch、fork 和包含未提交修改的本地目录也使用同一个命令。没有 Git metadata 的源码目录只要
包含有效的 Python build backend，同样可以安装；Git ref、commit 和 dirty 状态只写入有界
Runner Provenance，不授予权限或拒绝生命周期命令。

安装器会把当前源码 non-editable 安装到用户级 Snapshot，执行一次独立的最小 Codex
Structured Outputs Compatibility Check，成功后原子激活 Runner。`agent-run` 入口位于
`~/.local/bin`，并由 `~/.profile` 中唯一、幂等的受管 PATH 块加入新登录 shell；重新打开
登录 shell 后可从任意目标仓库调用：

```bash
cd /path/to/delivery-repository
agent-run run <parent-issue> --repo OWNER/REPO
```

`agent-run` 不要求工具源码目录和目标交付仓库相同。源码修改后，已安装 Snapshot 的行为不变；
要使用新源码，回到所选源码目录再次运行 `./install.sh`。系统只保留当前和紧邻上一个 Snapshot，
相同内容重复安装不会重新 probe 或创建重复 Snapshot：

`run` 会在目标仓库创建和维护受管 Run Branch；该交付状态与 Runner Snapshot 相互独立。

```bash
./install.sh --rollback
./install.sh --uninstall
```

rollback 不重建、不调用 Codex，也不读取或修改 Delivery Run。uninstall 只删除 Runner、入口和
安装器写入的 PATH 块，保留 GitHub App 配置、Run locator、私钥文件以及所有目标仓库的
`.agent-run`；重复卸载安全。若用户替换了 `~/.local/bin/agent-run`，安装器会保留该用户内容并
报告清理未完成。

安装器不是常驻 Manager、Launcher、daemon 或独立包；它只在用户显式执行时运行。它不审查 source
trust，不要求 clean checkout、detached SHA、`origin/main` ancestor 或 promotion audit，也不
提供旧 promotion 兼容入口。规范生命周期入口是安装后得到的 Active Runner；直接从 source 或
editable checkout 运行生产生命周期不受支持，但 Active Runner 不会因 branch、fork、dirty source
或非官方 provenance 被旧 gate 拒绝。

### 首次运行、更新与诊断

源码目录和目标交付仓库是两个不同角色。首次安装完成并重新打开登录 shell 后，从任意目录检查
本机状态，再进入目标仓库运行：

```bash
agent-run doctor
cd /path/to/delivery-repository
agent-run doctor
agent-run run <parent-issue> --repo OWNER/REPO
```

`doctor` 可以从任意目录调用，只读报告 Python 版本、Git、Codex、宿主 `gh` 登录、OpenSSL、Linux
`bubblewrap`、Active Runner、PATH 和当前 Worker read provider。缺少依赖只会显示为问题，不会
安装、修复、触发生命周期或改写任何仓库和用户配置；需要脚本消费时使用 `agent-run doctor --json`。

要重复构建，回到源码目录再次执行 `./install.sh`。安装 B 后 Active 是 B、previous 是 A；再安装
C 后只保留 C/B。相同源码重复执行是幂等的，候选构建或 Compatibility Check 失败会保留原 Active。
需要回退时在源码目录执行 `./install.sh --rollback`；它只交换 Active 与 previous，不调用 Codex，
也不读取或修改 Delivery Run。确认不再使用本机 Runner 后执行 `./install.sh --uninstall`；它保留
固定安装锁、Run locator、App profile、私钥文件和目标仓库 `.agent-run`。卸载后重新打开登录 shell
以取得 PATH 变化，重复卸载安全成功。

## 目标仓库人工接入

Setup 不管理 Skills、项目工具链，不修改目标仓库忽略规则、标签、Issue 状态、CI 或保护规则。
首次任务前由维护者完成：

1. **认证和权限**：自行 `codex login`、`gh auth login`，用 `codex login status`、
   `gh auth status` 验证，已有有效登录直接复用；确认 Git 身份及远端读写可用。
   Publisher 需要宿主 gh 的仓库写权限。Worker 只读请求需要 `actions: read`、`checks: read`、
   `contents: read`、`issues: read`、`metadata: read`、`pull_requests: read`、`statuses: read`。
   默认不要求 GitHub App，也不复制外部认证材料。
2. **Skills**：自行在 Codex 可发现的位置准备 `implement`、`code-review` 及目标 `AGENTS.md`
   引用的其他 Skills，确认当前用户能加载；安装 Runner 不会同步或覆盖它们。
3. **项目工具链**：自行安装项目语言、编译器、包管理器、数据库及测试依赖，从配置好项目
   PATH/虚拟环境的终端启动任务。Runner 的 Python 环境不代替项目环境。
4. **忽略规则**：自行在目标仓库根 `.gitignore` 加入 `/.agent-run/`，用
   `git check-ignore .agent-run/runs/probe` 核对；已跟踪内容由维护者妥善处理。
   此目录包含 Run 状态和受管工作区，不应进入交付提交。
5. **任务资格**：自行创建 [triage 标签](docs/agents/triage-labels.md)。可领取的 open 任务
   要有 `ready-for-agent`，且不能同时有 `needs-triage`、`needs-info`、`ready-for-human`。
   Ticket 集合来自 Parent 的 GitHub 原生 sub-issues，依赖来自原生 `blockedBy`；未解除的
   blocker 阻止领取，正文列表和标签搜索不增加任务。标签变化不会自动启动 Run。
6. **执行能力**：CPython 3.11+、Git 2.40+（或经过验证的等价功能）、Codex、gh、Linux
   bubblewrap 和真实 user systemd 应满足生产调用；App 模式另需 OpenSSL。
   `agent-run doctor` 只读报告能力，不代替实际任务检查。安装器不会关闭全机安全机制、
   替换 init、自动启用 linger 或降级为前台执行。

### CI 接入

中间 Ticket PR、Run Repair PR 的目标是受管 Run Branch（`agent-run/<run-id>/run`）；
最终 Run PR 与 Parent-only PR 的目标是默认分支。维护者自行让 workflow 的
`pull_request.branches` 覆盖相应 base，例如 `main` 和 `agent-run/**`，并核对针对两类
目标的 Ruleset/branch protection；安装器不会创建这些配置。

| GitHub 必需检查事实 | 行为 |
| --- | --- |
| 确认无必需检查 | 继续既有开发、独立审核和发布门禁，不宣称 CI 运行或通过 |
| 存在必需检查 | 当前 PR head 的所有必需检查通过才继续，未触发仍需等待 |
| 可选检查 | 不属于自动门禁，由维护者决定是否提升为必需 |
| 必需检查失败或读取未知 | 保留失败或监督语义，不作为“无 CI” |

可在目标 `pyproject.toml` 声明允许自动修复的稳定 `workflow::job显示名::step`，例如：

```toml
[tool.agent-run.required-checks]
code-failure-steps = [
  "CI::quality::Run tests",
  "CI::quality::Run type checks",
]
```

只有 GitHub 给出失败结论、completed Actions job 准确绑定当前 PR head，且全部失败步骤
都在声明内，才进入既有有界代码修复。pending、cancelled、未配置步骤、缺失或矛盾证据、
runner/网络/平台问题继续由 Controller 监督，不触发无依据的代码修复。
声明不会创建必需检查、增加预算、绕过独立审核或授权任意 CI 改造。
完整生命周期、权限边界和恢复语义见 [`docs/agent-run.md`](docs/agent-run.md)。

Worker 默认复用宿主已经登录的 `gh` 进行固定只读请求。需要独立只读身份时，可从任意目录一次性配置
GitHub App；配置只保存元数据和仓库外私钥路径，不复制私钥或 installation token：

```bash
agent-run auth status
agent-run auth app configure \
  --app-id <app-id> \
  --installation-id <installation-id> \
  --private-key /secure/agent-run-app.pem
agent-run auth app remove
```

没有 App profile 时默认使用宿主 `gh`；有效 App profile 存在时明确使用 App provider，损坏或不可读
时不会静默回退。`agent-run auth status` 只显示 provider 和非敏感状态，`auth app remove` 删除
profile 并恢复 host `gh`，不会删除私钥文件。Worker 永远不会看到宿主 token、App 私钥或 Publisher
凭据。

自动准备路径面向 Ubuntu/Debian；未取得一次性完整宿主验收证据的发行版/版本/架构组合
不列为已支持，普通容器或模拟测试不能证明真实执行能力。Windows、macOS、系统级/多用户安装、
PyPI/pipx、常驻 Manager/Launcher 和自动更新不在本轮范围。

## 开发

以下环境仅用于本仓库开发和测试，普通用户使用上述安装器：

```bash
python -m venv .venv
source .venv/bin/activate
make test-bootstrap # 首次或依赖变化：固定开发依赖、安装源码、准备离线 wheel
make test-policy  # 示例：策略模块的局部反馈
make test         # 本地快速回归，再补测受影响模块
make test-full    # 完整测试，CI 与最终验收使用
make typecheck
```

修改后按影响选择[测试入口](docs/agents/test-commands.md)；测试编写、扩大验证和隔离要求见
[测试指南](docs/agents/testing.md)。CI 保留完整测试、类型检查与安装后 CLI 检查；
需要比较 CI 时使用 `make test-report TEST_WORKERS=2`，详见测试入口中的诊断说明。

## 常用命令

```bash
agent-run run <parent-issue> --repo OWNER/REPO
agent-run status [--parent <parent-issue>]
agent-run history [--parent <parent-issue>]
agent-run runs [--repo OWNER/REPO]
agent-run resume <parent-issue> --repo OWNER/REPO
agent-run doctor [--json]
agent-run approve <parent-issue> --repo OWNER/REPO
agent-run revise <parent-issue> --message '维护者反馈' --repo OWNER/REPO
agent-run requeue <parent-issue> --repo OWNER/REPO
```

`run` 是创建或继续 Delivery Run 的唯一普通入口；当前仓库只有一个进行中 Run 时，
`status`、`history` 可省略选择参数，也可以使用 `--parent`；在任意目录用 `--repo` 时必须同时
提供 `--parent`。`runs` 用于发现候选。普通 mutation 和 Run-scoped `configure` 都用 Parent
Issue 定位唯一 Run：零匹配、多匹配或仓库不匹配时拒绝猜测；完整 Run ID 与显式 `--state-dir`
只保留给自动化和精确排障。`resume` 不创建新 Run，`approve` 是进入默认分支前的显式人工批准。
Lifecycle mutation 默认输出不含 Action ID、Run ID、PID 或 digest 的人类回执；需要稳定机器审计事实时显式使用 `--json`。安装、更新、rollback 和
uninstall 都不迁移、修改或绑定既有 Delivery Run；不兼容 state 仍返回
`incompatible_run_state`。
