# 支持矩阵与版本匹配

本表是发行验证清单，不是平台支持证明。本页对应显式前台 Host 候选；每版以准确提交重验，接线不代表已通过验收。
原1836项产品回归不能证明发行归档、真实登录、systemd、模型或 GitHub 交付可用。

| 环境 | 已知实现/探针 | 真实发行验收 | 当前判定 |
| --- | --- | --- | --- |
| Ubuntu 24.04 / Debian 13，x86_64、aarch64 | Setup 有受限 APT 准备策略；仍逐项检查依赖 | 每个发行版与架构组合均待干净机记录 | 未验证，不能把 APT 分支当通过 |
| 其他 Linux | 有缺项说明及可行安装步骤，无统一自动准备承诺 | 待逐机验证完整运行前提 | 未验证 |
| 父助手实际云：Debian 13.6、Linux 6.18.55、x86_64、PID1 codex | Python 3.12.14、SQLite 3.53.1；doctor 的基础 bwrap 探针正常权限 exit0，pidfd/WNOWAIT 可用；无 user bus/cgroup 挂载/容器引擎/KVM | 真实 Codex 初始化被平台遮蔽阻塞；跨工具 observer 锚点不同，准确退出/恢复与 Host/E2E 未通过 | 阻塞此次“父云可安装配置并使用”的发行目标 |
| 新 savedcloud 验证环境 | 与父助手原云不同，须独立核对进程观察与执行前置 | 验证进行中，未确认为通过 | 不替代父云目标 |
| Windows / macOS | 本轮不提供原生安装路径 | 未运行 | 不在本轮范围 |

父云事实来自维护者提供的该环境探针，不是本工作者 savedcloud 的实测；
基础 namespace 成功不证明完整挂载、退出、恢复或交付成功。
云环境兼容必须作为独立工作包验证，不能在安装脚本中跳过 systemd 检查、
换 fake manager、禁用隔离或用进程组冒充原 cgroup 保证。
现有 systemd 后台能力保留；前台候选已接入 `--foreground`，正常取消/退出收口仍待修复，
normal/stop-resume 两项已在明确批准的 savedcloud 执行上下文合成通过，原默认权限2F保留；
整体前台仍有 P1 待修复，真实 Codex 尚未验收，详见[统一运行模式说明](../agent-run.md#运行模式与前台候选验收)。
doctor 前台预检报告 `ready/unavailable` 和 `business_execution=not_verified`，不代表业务验收通过。

父云先前因 `files.pythonhosted.org` 网络策略缺依赖的轮次仍保留 blocked 原账；
父原云 product5 正式包 `verify-release` 通过；真实安装报告 `codex_sandbox_initialization_failed`，
未激活，仍为 blocked，不能以另一环境的合成通过替代。
固定产品包可携带四个已锁定运行依赖，构建者仍须通过获准来源取得精确 wheel。

每版矩阵补充：归档 SHA256、源码 commit、安装内容身份、工具版本、实际运行模式、
验收日期和本轮证据索引。工具可执行/安装成功/Worker 启动/完整交付分别记录。
同版文档放在同一 tag 下；升级说明明确配置影响和 v20 数据合同，明确拒绝 v19 及更旧协议，无迁移或兼容续接。
权限不足、分支保护拒绝、CI 未触发与工具不兼容分别报告，不统称安装失败。

正式声称此次产品化目标完成前，既要有正常 Linux 后台路径的真实记录，
也要有父助手实际云的真实安装到交付记录；不能用另一台云机器或单元测试代替。
