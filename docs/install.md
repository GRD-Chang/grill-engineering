# 安装与仓库接入

[English](install.en.md) | 简体中文

按本页完成安装和目标仓库检查；运行任务见 [Agent 操作指南](agent-guide.md#运行任务)。
工具源码目录用于安装，目标仓库目录用于交付，两者可以不同。

## 首次安装

1. 取得用户选择的源码版本并记录 ref/commit；未指定时可使用默认分支，已有源码可复用：

   ```bash
   git clone https://github.com/GRD-Chang/grill-engineering.git
   cd grill-engineering
   ./setup.sh
   ```

   没有 Git 时，下载所选 ref 的 GitHub Source code 归档并解压，从源码根目录执行 `./setup.sh`。
   branch、fork 和包含本地修改的源码也可安装；记录实际来源，不要求干净工作树。

2. 核对 Setup 展示的变更计划。已有授权覆盖计划时可用 `./setup.sh --yes`；
   它只确认计划，不提供管理员权限或账号认证。需要用户登录、提权或调整宿主会话时，报告实际缺项。
   补齐后在同一源码目录重跑。

   | 退出码 | 结果 |
   | --- | --- |
   | `0` | 安装及执行条件满足，继续检查目标仓库 |
   | `1` | 安装失败或新版未激活 |
   | `2` | Runner 已安装，执行环境未就绪 |

3. 在新登录 shell 中验证入口和环境：

   ```bash
   command -v agent-run
   agent-run doctor --json
   ```

   入口应为 `~/.local/bin/agent-run`，指向所选版本的已激活 Runner；安装器通过 `~/.profile` 设置 PATH。
   安装包含真实 Codex 兼容性调用，会使用账号额度。旧版本仍能运行，不代表本次安装成功。

完成条件：所选版本已激活，执行环境检查满足；否则报告具体缺项和修复动作。
生产运行使用安装后的 `agent-run`，不使用源码或 editable 环境代替。

### 环境缺项

Setup 仅在 Linux 运行，Ubuntu/Debian 自动补齐已有 APT 源中的缺失或不兼容依赖；
不添加软件源、不升级整个系统、不替用户安装或登录 Codex。其他 Linux 需自行补齐未适配项。
目前尚未完成 Ubuntu/Debian 发行版、版本与架构组合的完整真实宿主验证；自动依赖准备适配不等于平台已经通过验证。

| 缺项 | 处理与复查 |
| --- | --- |
| Python / Git | 准备 CPython 3.11+、venv/pip、构建后端和 Git 2.40+；以 Setup 报告核对兼容性 |
| GitHub CLI | 按 [gh Linux 安装说明](https://github.com/cli/cli/blob/trunk/docs/install_linux.md)准备兼容版本；执行 `gh auth status` |
| Codex | 按 [Codex 安装说明](https://github.com/openai/codex)安装并登录；核对 `codex exec --help`、`codex exec resume --help` 和 `codex login status` |
| user systemd / bubblewrap | 在真实用户登录会话检查 `systemctl --user show-environment`，定位 namespace 或权限问题；修复后重跑 Setup 的执行探针 |

Setup 成功后的短命 systemd 探针用于检查真实执行能力。不要通过关闭全机防护、替换 init 或绕过隔离来通过检查。
`doctor` 只读检查，不安装或修复；也不证明 Skills 可用、项目测试通过或全部仓库权限满足。

## 目标仓库接入

进入目标仓库并读取其 `AGENTS.md`。Setup 不配置以下内容，按用户授权补齐并逐项验证：

| 检查项 | 完成条件 |
| --- | --- |
| 认证与仓库身份 | `codex login status`、`gh auth status` 有效；Git 身份和 remote 正确，宿主 gh 有目标仓库发布所需的写权限 |
| Skills | 实际运行 Codex 的用户可发现并读取 `implement`、`code-review`、它们引用的 Skills 及项目要求的其他 Skills。Runner 不附带它们；缺失时按 [Matt Skills](https://github.com/mattpocock/skills) 的当前说明准备，已有同名内容先检查并复用 |
| 项目环境 | 编译、测试和运行依赖已就绪；从配置好项目 PATH/虚拟环境的终端启动，Runner 的 Python 环境不能替代它 |
| 忽略规则 | 根 `.gitignore` 包含 `/.agent-run/`，`git check-ignore .agent-run/runs/probe` 成功；若已有跟踪文件，先妥善处理 |
| CI | 已有 PR workflow 时，核对其 base 分支覆盖默认分支和 `agent-run/**`，并检查两类分支的必需检查规则；确认无必需检查也可运行，但不报告为 CI 通过 |

### GitHub 认证：默认无需配置 App

默认使用本机已登录的 `gh`。发布操作需要该账号能推送代码、创建和合并 PR、更新 Issue；
Worker 的 GitHub 请求由程序限制为只读，不向 Worker 提供发布凭据。
已有有效登录直接复用，缺失时用 `gh auth login` 登录，再核对目标仓库权限。

只有需要为 Worker 单独配置只读身份时，才配置 GitHub App；App 不替代发布操作所需的宿主 gh 登录。
先用 `agent-run auth status` 查看当前身份来源。已有 App 配置时会使用 App，配置损坏不会自动回退到 gh。
App 所需只读权限和配置命令见[GitHub App 配置](agent-run.md#github-只读身份)及[权限边界](agent-run.md#权限边界)。

执行 `agent-run doctor --json` 和 `agent-run settings show`，核对环境及所选模型、推理强度和运行额度。
配置调整见[个人运行配置](user-defaults.md)；模型实际可调用性由执行验证。

完成条件：表中各项有验证结果，缺项有处理方式。仅安装时到此报告结果；
用户要求运行时，再按[运行任务](agent-guide.md#运行任务)核验 Issue 和任务关系。

### CI 自动修复

需要让 Runner 自动修复 CI 中的代码失败时，在目标 `pyproject.toml` 声明允许修复的步骤：

```toml
[tool.agent-run.required-checks]
code-failure-steps = [
  "CI::quality::Run tests",
  "CI::quality::Run type checks",
]
```

名称须匹配实际 `workflow::job显示名::step`。只有当前 PR head 的已完成 Actions job 明确失败，
且所有失败步骤都在声明内，才触发代码修复。检查未触发、仍在等待、取消、证据不明或平台故障继续按监督流程处理。
该配置不创建必需检查，也不改变验收或修复额度；运行规则见[运行说明](agent-run.md)。

## 更新、回滚与卸载

以下命令在所选工具源码根目录执行；宿主缺依赖时先重跑 `./setup.sh`。

| 操作 | 命令与验证 |
| --- | --- |
| 更新 | `./install.sh` 构建并激活当前源码；随后用 `agent-run doctor --json` 核对。源码修改不会自动改变已安装版本 |
| 回滚 | `./install.sh --rollback` 交换当前和上一个版本，不重建、不调用 Codex；随后核对已激活版本 |
| 卸载 | `./install.sh --uninstall` 删除 Runner、受管入口和 PATH 块；检查清理结果，并在新登录 shell 中核对入口 |

安装只保留当前和上一个版本。重复安装仍构建候选；与当前版本内容相同时复用该版本，跳过兼容性调用。
构建或激活失败保留原版本。
安装、回滚和卸载均不修改已有交付任务。卸载保留安装锁、Run 定位记录、App 配置、私钥和各目标仓库的 `.agent-run`；
用户替换过的命令入口会保留并报告清理未完成。若任务状态不兼容，按错误处理，不手工迁移状态。
