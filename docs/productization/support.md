# 支持矩阵与版本匹配

本表是发行验证清单，不是平台支持证明。本页对应显式前台 Host 候选；每版以准确提交重验，接线不代表已通过验收。
原1836项产品回归不能证明发行归档、真实登录、systemd、模型或 GitHub 交付可用。

| 环境 | 已知实现/探针 | 真实发行验收 | 当前判定 |
| --- | --- | --- | --- |
| Ubuntu 24.04 / Debian 13，x86_64、aarch64 | Setup 有受限 APT 准备策略；仍逐项检查依赖 | 每个发行版与架构组合均待干净机记录 | 未验证，不能把 APT 分支当通过 |
| 其他 Linux | 有缺项说明及可行安装步骤，无统一自动准备承诺 | 待逐机验证完整运行前提 | 未验证 |
| 父助手实际云：Debian 13.6、Linux 6.18.55、x86_64、PID1 codex | Python 3.12.14、SQLite 3.53.1；doctor 的基础 bwrap 探针正常权限 exit0，pidfd/WNOWAIT 可用；无 user bus/cgroup 挂载/容器引擎/KVM | 真实 Codex 初始化被平台遮蔽阻塞；跨工具 observer 锚点不同，准确退出/恢复与 Host/E2E 未通过 | 阻塞此次“父云可安装配置并使用”的发行目标 |
| 新 savedcloud 验证环境 | 与父助手原云不同，须独立核对进程观察与执行前置 | 验证进行中，未确认为通过 | 不替代父云目标 |
| Windows 原生 / WSL1 | 不提供原生 Worker，WSL1 不属于 Linux namespace 执行路径 | 未运行 | 不支持 |
| Windows + WSL2 Linux Guest | 整套 Linux 工具链在 Guest 内运行；前台/后台分别满足下述前置 | Worker 启动、Stop、Resume 均待真实记录 | 可评估，未验证 |
| macOS 原生 | 不提供原生 Worker sandbox | 未运行 | 不支持 |
| macOS + Linux VM Guest | 在 Guest 内运行 Linux Runner 和 Worker；不依赖 macOS 原生 bwrap | Worker 启动、Stop、Resume 均待真实记录 | 可评估，未验证 |
| 普通 Docker/其他容器 | 安装 bwrap 不等于具有所需 namespace、挂载和退出证明 | 未证明等价边界 | 非默认支持路径 |

父云事实来自维护者提供的该环境探针，不是本工作者 savedcloud 的实测；
基础 namespace 成功不证明完整挂载、退出、恢复或交付成功。
云环境兼容必须作为独立工作包验证，不能在安装脚本中跳过 systemd 检查、
换 fake manager、禁用隔离或用进程组冒充原 cgroup 保证。
现有 systemd 后台能力保留；前台候选已接入 `--foreground`，启动失败/中断 drain 的退出留证修复已独立准入，
normal/stop-resume 两项已在明确批准的 savedcloud 执行上下文合成通过，原默认权限2F保留；
后继 `854f0764` 的真实 namespace 合成2P与unknown负例1P另账记录，不外推最终组合全通过；
真实 Codex 与父助手原云交付尚未验收，详见[统一运行模式说明](../agent-run.md#运行模式与前台候选验收)。
doctor 前台预检报告 `ready/unavailable` 和 `business_execution=not_verified`，不代表业务验收通过。

父云先前因 `files.pythonhosted.org` 网络策略缺依赖的轮次仍保留 blocked 原账；
父原云 product5 正式包 `verify-release` 通过；真实安装报告 `codex_sandbox_initialization_failed`，
未激活，仍为 blocked，不能以另一环境的合成通过替代。
固定产品包可携带四个已锁定运行依赖，构建者仍须通过获准来源取得精确 wheel。

每版矩阵补充：归档 SHA256、源码 commit、安装内容身份、工具版本、实际运行模式、
验收日期和本轮证据索引。工具可执行/安装成功/Worker 启动/完整交付分别记录。
同版文档放在同一 tag 下；当前数据合同为 v21，明确拒绝 v20 及更旧协议，无迁移或兼容续接；旧数据和匹配的历史 Runner 保留。
权限不足、分支保护拒绝、CI 未触发与工具不兼容分别报告，不统称安装失败。

正式声称此次产品化目标完成前，既要有正常 Linux 后台路径的真实记录，
也要有父助手实际云的真实安装到交付记录；不能用另一台云机器或单元测试代替。

## Linux Guest 路径与只读诊断

这里补充 [#104](https://github.com/GRD-Chang/grill-engineer/issues/104) 的使用路径，
不宣称已完成其 Windows/macOS 受控启动验收，也不新增原生后端。
Windows 用户在 PowerShell 中执行 `wsl --list --verbose`，确认目标发行版为版本 2，
再用 `wsl --distribution <发行版名>` 进入已有 Guest；这些命令见
[微软官方说明](https://learn.microsoft.com/en-us/windows/wsl/basic-commands)。
WSL 安装、升级或 systemd 配置由用户另行决定，本工具不执行。
macOS 用户进入自己已准备的 Linux VM 终端；macOS 主机上有 Python/Git 不代表 Guest 就绪。

下面的命令全部在**同一个 Guest、同一个实际运行用户**下执行；不是在宿主机上代查：

```sh
uname -s
python3 --version
git --version
command -v codex gh openssl bwrap
agent-run doctor --local --mode foreground --json
agent-run doctor --local --mode background --json
```

`uname -s` 应为 Linux，但它不能识别或证明 WSL2/VM 品牌及完整安全能力。
doctor 的 `checks` 定位 Python/SQLite、工具及配置缺项；
`execution_readiness` 对应所选模式，`modes` 分列两个后端。
前台仍要求真实 bwrap、namespace、pidfd 与准确退出观察；
后台还要求现有 systemd user manager 254+、可达 user bus、cgroup 与 FD store。
可在该 Guest 的实际登录会话执行 `systemctl --user show-environment` 检查后台连接，
但不要公开其完整输出。WSL 的 systemd 可用性与 Guest 内 systemd 版本是两项条件，
见[微软 systemd 说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)。
能力缺失时停止在准确诊断，不改权限、关闭隔离、使用 fake manager 或假造退出证明。

| 内容 | 建议 Guest 内位置/边界 |
| --- | --- |
| 项目及 Git worktree | 如 `~/src/project` 的 Guest Linux 文件系统；不用 `/mnt/c`、macOS 共享目录承担未验证的锁/权限语义。微软也建议 Linux 命令使用 [WSL 文件系统](https://learn.microsoft.com/en-us/windows/wsl/filesystems)。 |
| Runner 安装与 Run 数据 | 使用现有安装入口；默认数据在 `~/.local/share/agent-run/`，配置在 `~/.config/agent-run/`；不要把同一可写 Run 交给宿主与 Guest 两套进程。 |
| Unix socket/运行控制 | 使用程序既有 `$XDG_RUNTIME_DIR/agent-run`，未设置时为 `/tmp/agent-run-<uid>`；Worker 临时文件由 Linux `TMPDIR`/默认临时目录管理。不把 socket 放到 Windows/macOS 共享目录，也不复制旧控制 socket。 |
| Codex / gh / PAT / App | 在 Guest 内复用所选身份的既有配置和引用；两个身份仍独立。PAT 环境引用须对实际 Guest 进程可用，文件引用/App 私钥留在 Guest 中可读且符合现行权限规则的位置。不要把宿主整个 HOME、认证目录或私钥额外挂入 Worker。 |

`doctor --local` 不登录、不读 PAT 内容或签发 App token，也不调用模型。
本地缺项补齐后，用户明确允许认证读取时再执行
`agent-run doctor --online --repo OWNER/REPO --json`：分别核验所选 Runner/Worker 来源，
可能签发短期 App token，仍不调用模型；仓库可读不证明全部写权限。
不要因 PAT/App 失败而切换 host。模型兼容性探针及首次任务另行授权。

每个 WSL2/VM 组合仍需记录准确版本、受控 Worker 的只读 Git metadata/凭据边界、
停止及同 Run 恢复；后台还须分别核验断终端、注销、Guest 停止和重启情形。
单次启动成功、doctor ready 或普通 Linux 合成测试均不能填这些验收格。
本页仅给可执行诊断及现有布局，不改变开发、Review、CI、批准或 unknown 对账流程。
