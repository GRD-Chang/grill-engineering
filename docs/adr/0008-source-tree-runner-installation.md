---
status: accepted
---

# v0.1 由源码安装器固化 Runner，不引入常驻 Manager

## 后续有限修订：一次性宿主准备（#226 / #227）

首次公开接入改为源码目录 `./setup.sh`：最外层不要求 Python 或 Git，集中展示检查、依赖
来源/版本/架构、拟修改项和人工待办，统一确认后在 Ubuntu/Debian 准备必要缺项，再调用
既有 `./install.sh`。其他 Linux 跳过未适配自动步骤，继续可行工作；缺关键构建或激活条件
仍拒绝安装。确认不代替系统提权或账号认证，不升级整个系统。

以下关于不调用包管理器、不执行 sudo 的限制继续约束底层 Source Runner Installer，
不再禁止这个外层一次性 Setup。Setup 可以报告执行能力及认证缺项，明确区分本体未激活、
已安装未就绪和安装及执行条件满足，不以命令存在代替生产能力检查。
Skills、项目工具链、目标仓库忽略规则、标签、CI 与保护规则仍由维护者准备。
不复制外部凭据，不关闭全机安全机制、替换 init、自动启用 linger 或增加常驻组件。
候选构建、Compatibility Check、原子激活、幂等、current/previous、回滚及 ADR 0010
管理租约继续由同一底层安装生命周期负责。平台支持只列实际完成宿主验收的组合。

## 原始决策及保留边界

v0.1 的公开安装入口是取得 Git 仓库源码，并在用户选择的 tag、branch 或本地修改目录中显式执行 `./install.sh`。该安装器从当前目录实际内容构建非 editable 的候选 Runner Snapshot；Snapshot identity 只覆盖安装后 `agent_run` runtime tree 的规范相对路径与内容，不包含环境生成文件、依赖、时间戳或 Git provenance。候选 identity 已经等于 Active current 时安装幂等成功，不重新执行 Compatibility Check、不创建 Snapshot 或 Generation，也不改变 previous；否则安装器执行一次只验证当前 Codex Structured Outputs schema 链路的小型真实调用，成功后先创建同时包含新 current 与可选旧 current-as-previous 的完整 Runner Generation，再原子切换本机 `agent-run` 命令，并固定只保留 Active Generation 引用的 current 与可选 previous Snapshot。构建、检查或切换失败时，旧 Generation 及其 current、previous 整体保持不变。安装器以可识别、幂等的受管配置把用户级命令目录加入 shell PATH，但不调用系统包管理器、不执行 `sudo`，也不自动安装或升级 Python、Git、`gh`、bubblewrap 等宿主软件。源码来源、Git clean 状态和历史不限制构建或生命周期权限，既有 Delivery Run 不绑定旧 Runner，也不获得状态迁移或兼容层。

安装器只存在于源码树并在用户显式调用时运行；安装完成后不驻留，不参与 Controller 生命周期，也不形成独立 Manager、Launcher、Python 包或版本。install、rollback 与 uninstall 在修改受管状态前必须取得同一个固定用户级非阻塞互斥锁；竞争者立即失败且不修改状态，uninstall 永不删除锁文件，避免替换持锁 inode 后出现第二把锁。官方更新由用户先通过 Git 选择或取得新源码再重新运行安装器，本地自开发使用同一入口。`./install.sh --rollback` 只通过新的完整 Generation 原子交换 current 与 previous，不重建、不调用 Codex、不判断状态兼容性，也不改变 Delivery Run。`./install.sh --uninstall` 删除全部受管 Snapshot、Generation、候选残留、内部 `active` 入口、公开命令 symlink 和安装器添加的 PATH 受管配置，但保留固定锁、GitHub App 配置、全局 Run 定位状态与仓库内 Delivery Run 数据；重复 uninstall 幂等成功，v0.1 不提供 purge 模式。v0.1 不要求 PyPI 或 pipx；以后增加 PyPI 只增加分发渠道，不改变 Snapshot 与显式安装语义。

生产生命周期的规范入口是安装后得到的 Active Runner；直接从 source 或 editable checkout 运行生产生命周期不受支持。这个入口边界不构成旧 promotion gate：Active Runner 不会因 branch、fork、dirty source 或非官方 provenance 被拒绝。

## Considered Options

- PyPI wheel + pipx 作为首发主入口：普通安装命令更短，但第一版需要额外维护包名、发布身份和 release workflow，而预期用户已经使用 Git 与 GitHub，因此暂缓。
- 独立 Runner Manager 或常驻 Launcher：可以持有更新入口，但会增加第二个运行组件和更新边界；源码安装器已经能够在进程外完成候选检查与原子切换，因此拒绝。
- 直接 `pipx install --force .` 覆盖现有环境：实现更少，但不能保留已决定的候选检查失败不影响旧 Runner、原子切换和双快照行为，因此不采用为规范入口。

## Consequences

用户首次安装与后续重建都需要一份源码目录，并对自己选择的 Git revision 或本地修改负责。执行所选源码的安装器与 build backend 等同于授予该源码当前用户级代码执行权限；“只修改受管位置”的承诺只约束符合本项目合同的源码，不构成对不受信 fork 的 sandbox。项目只维护一个源码树和一套 Runner 版本；README 必须把 release tag 安装作为稳定使用路径，把 branch/fork 安装明确为开发者选择。安装器只管理自己的 XDG 安装目录、Active Runner 入口和唯一边界标记的 PATH 受管配置；同名非受管入口不覆盖、不备份，卸载时发现入口已被替换则保留用户内容并报告 operational error。宿主依赖缺失时返回准确错误，由用户自行安装，可选 `agent-run doctor` 只提供诊断。管理动作不协调已经运行的生命周期进程，因此清理或卸载旧 Snapshot 后不保证旧进程继续可用。

Worker 身份支持 host/PAT/App 原生 `gh`，统一由个人 `user-defaults.json` 和 `settings show/configure` 选择；无配置默认 host。PAT 只保存环境变量名或文件路径引用，二选一；App 保存 App ID、Installation ID 与私钥路径引用，私钥不主动下发 Worker。缺失、空值或不可读明确失败，不回退宿主身份。新 Run 固定非秘密来源、引用与范围，单次覆盖不改变个人默认；恢复从同一引用取得更新值，个人默认变化不改变已有 Run。查询不签发 token、不显示秘密值。Runner 程序端本阶段继续使用 host，其 App 接入由后续任务完成。

App 默认采用安装已授权的权限和仓库范围，不再固定七项 read；只读与当前仓库是独立收窄选项，不能扩大安装授权。Worker 收窄不改变程序端授权。不同范围的凭据实例与交付目录隔离，签发及续签复用不解析 gh 请求的凭据实现；同一 Agent 的后续原生 gh 子进程读取原子更新后的短期 token。旧代理、可执行文件挂载替换、socket 协议和命令白名单不再保留。

旧独立 App 元数据通过 settings 明确写入路径原子迁入统一配置，保持 App 来源并采用新默认范围；显式新值优先、保留其他设置。查询只报告迁移状态，不写入或签发。迁移失败保留有效数据，成功后旧配置不再是新运行的持续来源；不删除私钥或活动执行器引用的材料。个人迁移不重解释旧活动 Run 的授权；缺少身份与范围绑定的旧 Run 明确不兼容并保留数据，不从个人默认猜测补齐。

host/PAT 不适用 App 收窄选项，错误组合明确诊断。GitHub 凭据授权不替代角色职责、权威 Git 保护、Controller 正式批准与 Publisher 发布权威。GitHub 身份与源码安装互不耦合。普通测试使用受控服务和工具替身验证配置、范围、续签、恢复及回收，不代表真实 gh 或 Git HTTP 兼容性已验证。
