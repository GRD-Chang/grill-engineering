# 首次接入：保留配置，分阶段验证

这是安装后的短路径；安装、更新和回滚仍使用[安装指南](install.md)。
已有登录、PAT/App 配置、个人 Prompt 和 Skills 都应复用。以下命令不安装服务、
不修改 GitHub 授权，也不启动开发。最终真实交付仍需[运行任务](agent-guide.md#运行任务)。

1. **只查本地准备情况**：`agent-run doctor --local`，机器读取可加 `--json`。
   检查实际解释器的 SQLite WAL-reset 支持、工具选项、bubblewrap 和 user systemd
   预检，以及所选身份的引用。不会读取 PAT 内容、签名私钥或调用 GitHub 认证服务，
   不创建业务 Run；不将文件可读等同账号或仓库权限有效。
2. **保留已有设置**：`agent-run settings show`。无自定义时使用内置默认；
   Runner/Worker 均可独立选择 host/PAT/App，默认 host，详情见[个人配置](user-defaults.md#github-身份)。
   PAT/App 不要求额外登录宿主 gh。需要改动时才执行 `settings configure`，
   配置仅保存非秘密引用；修改新任务默认不改变已有 Run。
3. **检查开发准备**：doctor 报告当前用户 `.agents/skills` 和所选 Codex home 的
   `skills` 下 `implement` / `code-review` 入口是否可读，不自动安装或覆盖。
   这不证明引用的其他 Skills、实际 Worker 的发现路径或项目 `AGENTS.md` 要求已满足，
   须按实际执行环境继续核实。项目编译/测试工具链另行准备。
   默认无需 `prompts init`，个人定制继续保留；需要比较时用 `agent-run prompts diff`。
4. **明确发起在线检查**：`agent-run doctor --online --repo OWNER/REPO`。
   复用当前所选身份，可能签发短期 App token；不调用模型、不执行仓库写操作。
   分别报告 Runner/Worker 认证及 Runner 仓库/默认分支读取。
   成功不证明写权限、Required Checks 全部权限或 sandbox 内 Worker 认证。
   缺少兼容 gh（例如没有 `--slurp`）时不执行在线认证，按报告升级工具。
5. **确认后运行**：模型兼容性调用与上述诊断分开，可能消耗账号额度。
   源码和固定产品包安装仍包含真实模型兼容性探针；没有跳过探针的安装选项。
   固定包可先运行 `./install.sh --verify-release` 只读核验，此动作不安装或调用模型。
   准备完成后使用 `agent-run run PARENT --repo OWNER/REPO`，再按查询给出的下一步处理。
   保留后台运行、明确批准、原意图 unknown 对账和显式 resume。

## 结果与现有入口

`doctor` 继续返回退出码 0 表示报告生成成功；不能据此判断环境就绪。
保留 `status`、`checks`、`installation_readiness` 和 `execution_readiness`，
新增 `configuration`、`identities`、`verification`、`modes`、`skills` 和 `next_steps`。
`status=ready` 仍是本次所列依赖/预检的结果，不是完整交付证明；按 `verification`
区分本地引用、认证、仓库读取、未验证的写权限和实际 Worker 环境。

未带 `--local/--online` 的原 doctor 保留历史宿主登录检查，仅在所选身份使用 host 时进行；
首次接入推荐显式 `--local`，在线检查明确使用 `--online`。

已有 Run 可用 `agent-run doctor --local --run RUN_ID --repo OWNER/REPO`；
必要时提供现有 `--state-dir`。该入口复用只读 Run 选择和 settings 查询，
使用冻结身份，不从新默认补齐或改写 Run。更换引用内容后仍按原身份恢复，不能自动换源。

模式默认 `--mode background`，使用现有 systemd manager 预检。
`--mode foreground` 已调用前台 Host 预检，报告 `ready` 或 `unavailable`，
并明确 `business_execution=not_verified`；它不启动业务，也不证明完整 namespace 启动、收尾或恢复可用。
前台命令及待验收限制见[运行模式](agent-run.md#运行模式与前台候选验收)。
缺 Host 能力时保留安装和配置、报告宿主缺项，不修改 OS 安全设置。

Runner App 长命令共享续签 scope 的生产接线缺口仍单独登记；
这里复用既有 provider，没有新增 token manager，也没有声称已验证该长期行为。

兼容性探针若报告 `codex_sandbox_initialization_failed`（Codex沙箱/执行环境初始化失败），
表示 Codex 报告其 app-server socket 目录不满足所属用户和 `0700` 权限要求。
请联系环境管理员检查该执行环境；受平台只读挂载或掩蔽限制时，不要自行解除限制或绕过沙箱。
安装仍失败、候选不会因此获得兼容性通过；此分类不证明 Worker 已能运行。
未知 stderr 继续只显示通用失败，不输出原始日志、路径或凭据；探针不会自动重试模型调用。
