# Runner 安装与管理

安装、激活和回滚的领域合同。首次接入与故障处理见[安装指南](../install.md)，源码归属见[当前架构](../architecture.md#模块责任)。

[按任务查阅术语与合同](../../GLOSSARY-MAP.md)

## 安装对象

**Runner Snapshot（Runner 固化快照）**:
本机用于执行 Controller 的不可变代码快照。其 content identity 只对非 editable 安装后 `agent_run` runtime tree 的规范相对路径和文件内容计算 SHA-256；console entry、shebang、virtualenv 路径、Python 与系统库、缓存、日志、时间戳和 Runner Provenance 不进入该身份。源码可以来自正式发布、Git revision 或包含未提交修改的本地开发目录；来源不限制其生命周期权限。源目录后续变化不会影响既有快照，只有用户显式构建并激活另一快照才会改变调用使用的 Runner。
_Avoid_: 可编辑源码环境、Git commit、当前源码目录

**Runner Provenance（Runner 来源）**:
说明一个 Runner Snapshot 的源码来自正式发布、Git revision 或本地源码快照的审计事实。它帮助用户理解和追溯 Runner，但不替代内容身份，也不单独决定该 Runner 能否执行生命周期命令。
_Avoid_: Runner Snapshot、Runner 内容身份、Delivery Run currentness

**Runner Generation（Runner 世代）**:
一次管理动作准备并作为整体激活的完整指针集合，包含 current Runner Snapshot 与可选 previous Runner Snapshot。安装器必须先在 Active Runner 之外创建完整 Generation，再以同一文件系统上的原子切换选为 active；current、previous 或命令入口不得分别原地更新。构建、Compatibility Check 或切换失败时，旧 Generation 及其 current、previous 整体保持不变。
_Avoid_: 分步更新 current 与 previous、半完成安装、可配置历史列表、每-Run Runner 绑定

**Active Runner（当前 Runner）**:
本机命令入口为 Controller 调用选择的完整 Runner Generation，并直接执行其中的 current Runner Snapshot。Runner 管理只能在 Runner Management Quiescence 中改变 Active Runner，因此不会替换活跃 Run Executor Session 正在使用的 Runner；后续重新推进既有 Delivery Run 时使用届时的 Active Runner，系统不绑定旧 Snapshot、不迁移旧状态，也不承诺跨 Runner 状态兼容。
_Avoid_: 当前 Git checkout、自动跟随源码、运行中热更新、每-Run Runner 绑定、状态兼容层

**Runner Build（Runner 构建）**:
用户在自己选择的源码目录中显式运行该目录的源码安装器，将目录当前实际内容冻结为候选 Runner Snapshot 的动作。构建忠实使用该目录内容，不审查其 Git 状态、可信等级或与既有 Delivery Run 的兼容性；执行所选源码的安装器与 build backend 等同于授予该源码当前用户级代码执行权限，安装位置与清理承诺只约束符合本项目合同的源码。
_Avoid_: 自动跟随源码、来源审批、状态迁移、自动触发构建

**Runner Compatibility Check（Runner 兼容性检查）**:
源码安装器在激活候选 Snapshot 前发起的一次小型真实 Codex 调用，只确认当前调用链接受 Runner 使用的结构化输出 schema。结果不绑定 Codex 版本、不证明源码可信、不授权未来兼容，也不会在环境版本变化后自动重新检查；检查失败时保留原 Active Runner。
_Avoid_: Promotion Audit、Codex 版本门禁、源码验收、状态兼容检查、长期信任证明

## 安装与执行就绪

**Runner Installation（Runner 安装）**:
用户从所选源码显式安装并激活 Runner 的管理动作。首次安装、切换 tag、拉取修改和从本地改动安装都经过同一条路径与 Runner Management Quiescence。依次构建候选、核对 content identity、执行 Runner Compatibility Check、准备完整 Runner Generation 并原子激活。候选 identity 与当前 Snapshot 相同时直接幂等成功，保留已激活的 current、previous 和兼容性检查结果。

激活后保留新 current 与可选旧 current，清理更早的世代和快照。激活失败时清理候选，整个原 Generation 继续可用。激活后的清理失败给出有界提示，交下次管理动作重试。

安装只创建缺少的 `~/.local/bin/agent-run` 或替换指向受管 XDG 根目录的自有 symlink。非受管入口保留原样并报错。Shell PATH 使用带唯一边界的幂等受管块。安装仅修改 Runner 及其入口，原 Delivery Run 状态和宿主软件保持原样。
_Avoid_: 自动更新、重复 PATH 配置、系统包管理、`sudo`、源码热加载、可配置保留策略、无限快照历史、状态迁移、每-Run Runner 绑定

**Runner Setup（Runner 环境准备）**:
源码树 `./setup.sh` 提供的、无需预先安装 Python 或 Git 的一次性公开入口；在用户开始使用 Runner 前，经集中检查和统一确认后补齐宿主依赖、委托 Source Runner Installer 完成本体安装并获取手动待办清单，区别于 Runner 本体安装、目标仓库准备和实际执行就绪判定。不在自动补装范围内的依赖转为手动待办，不因发行版缺少自动适配而中断所有可继续步骤，也不把尚缺关键条件的环境称为已就绪；CI、任务标签与 Skills 由用户自行准备。
_Avoid_: 安装成功即能运行、自动初始化目标仓库、自动配置 CI、自动管理 Skills、非适配发行版一律拒绝

**Source Runner Installer（源码 Runner 安装器）**:
随源码提供的单次 `./install.sh`，是 Runner Installation 的执行入口。Runner Setup 委托同一安装器，已有宿主也可直接调用它。其检查范围为 Installation Readiness，宿主执行能力由 Setup、`doctor` 和生命周期准入按各自职责检查。安装器以普通用户权限运行并在本次管理动作结束时退出。
_Avoid_: Runner Manager、常驻 Launcher、editable install、独立发布物、PyPI 前置条件

**Runner Installation Readiness（Runner 安装就绪度）**:
Source Runner Installer 为构建并激活候选 Snapshot 强制检查的本机条件，包括可用的受支持 CPython、venv/pip、源码与 build backend、受管路径写入与冲突边界、Runner Compatibility Check 以及 Runner 管理租约。它不包含 systemd、linger、目标 Repository、GitHub 登录或 lifecycle mutation 所需能力；`doctor` 可以只读报告它，但只有 Installer 在实际安装时作权威判定。
_Avoid_: Executor Host Availability、GitHub 执行资格、目标仓库、把 doctor 当作安装授权

**Runner Execution Readiness（Runner 执行就绪度）**:
一次公开 lifecycle mutation 在提交 Action 前按本次动作实际需要检查、并由 Executor 在真实宿主环境中通过启动握手最终确认的运行条件，包括有效 Active Runner、准确 Local Delivery Workspace/Delivery Task、Linux Executor Host Availability 以及本次 Agent、Git、GitHub、Publisher 或 sandbox 所需工具和认证。缺失时不得持久化新 Action或启动前台 fallback；`doctor` 只读报告，不替代动作时检查。`status` 与 `history` 不要求该就绪度。
_Avoid_: Runner Installation Readiness、一次全局永久判定、阻止只读查询、Setup 报告替代动作时检查

## 租约与维护命令

**Runner Management Lock（Runner 管理锁）**:
Source Runner Installer 与 Run Executor Session 共享的固定用户级非阻塞锁。Executor 从 Active Runner binding 确定前到 Session 退出或启动收口期间持共享 Runner Usage Lease；install、rollback 与 uninstall 必须取得独占 Runner Management Lease。多个 Executor 可以并行持有共享租约，任何共享或独占租约存在时竞争的独占管理动作立即失败且不修改状态；租约随持有进程退出由操作系统释放。uninstall 永不删除锁文件，避免替换持锁 inode 后出现第二把锁。
_Avoid_: 等待锁、每种动作一把锁、删除并重建锁文件、systemd 查询、自动停止活跃 Executor、启动交接空窗

**Runner Usage Lease（Runner 使用租约）**:
Run Executor Session 对当前 Active Runner 的主机级共享使用权。Active Runner 选择、binding claim 与租约从启动者到 Executor 的接管必须连续，不能让 Runner 管理动作在 systemd 已接受启动但 Executor 尚未握手的窗口穿插；Executor 或启动者退出时由操作系统自动释放。它只保护受管 Runner 内容，不扩大 Delivery Task 唯一性，也不保存 Run 业务状态。
_Avoid_: per-Run Snapshot 固定、引用计数、systemd unit 枚举、裸 PID、跨主机租约

**Runner Management Quiescence（Runner 管理静默状态）**:
本机不存在任何活跃 Run Executor Session 时形成的 Runner 管理前置状态。Runner Installation、Runner Rollback 与 Runner Uninstall 只在该状态下开始；任一活跃 Executor 存在时，管理动作立即拒绝，不等待、不停止 Delivery Run，也不改变 Active Runner。它保护的是当前用户共享的 Runner 生命周期，不扩大 Local Delivery Workspace 中 Delivery Task 的唯一性边界。
_Avoid_: 等待 Executor、自动 stop、运行中热更新、每-Run Snapshot 固定、跨工作区 Delivery Task 锁

**Runner Rollback（Runner 回退）**:
用户在任一包含源码安装器的目录显式执行 `./install.sh --rollback`，并在 Runner Management Quiescence 中创建交换 current 与 previous 的完整 Runner Generation、原子切换 Active Runner 的动作。若存在活跃 Run Executor Session，回退立即拒绝且不修改受管状态。它不重新构建、不调用 Codex、不判断 Runner 或 Delivery Run 状态兼容性，也不修改任何 Delivery Run；没有 previous 时明确失败且旧 Generation 整体不变。
_Avoid_: 状态迁移、兼容性检查、自动回退、下载历史版本、任意历史选择

**Runner Uninstall（Runner 卸载）**:
用户在任一包含源码安装器的目录显式执行 `./install.sh --uninstall`，并在 Runner Management Quiescence 中删除全部受管 Runner Snapshot、Runner Generation、候选残留、内部 `active` 入口、本机受管 `agent-run` symlink 以及安装器添加的 PATH 受管配置。若存在活跃 Run Executor Session，卸载立即拒绝且不修改受管状态。若用户已把公开入口替换为非受管路径，卸载保留该内容并以有界 operational error 报告清理未完成。它保留固定 Runner Management Lock、GitHub App 持久配置、Run 定位状态、Runner 独立仓库及 Delivery Run 数据；v0.1 不提供连带删除用户配置或运行数据的 purge 模式。
_Avoid_: 删除 Delivery Run、删除 GitHub App 配置、删除非受管 shell 配置、`--purge`
