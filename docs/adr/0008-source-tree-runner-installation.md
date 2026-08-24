---
status: accepted
---

# v0.1 由源码安装器固化 Runner，不引入常驻 Manager

v0.1 的公开安装入口是取得 Git 仓库源码，并在用户选择的 tag、branch 或本地修改目录中显式执行 `./install.sh`。该安装器从当前目录实际内容构建非 editable 的候选 Runner Snapshot；Snapshot identity 只覆盖安装后 `agent_run` runtime tree 的规范相对路径与内容，不包含环境生成文件、依赖、时间戳或 Git provenance。候选 identity 已经等于 Active current 时安装幂等成功，不重新执行 Compatibility Check、不创建 Snapshot 或 Generation，也不改变 previous；否则安装器执行一次只验证当前 Codex Structured Outputs schema 链路的小型真实调用，成功后先创建同时包含新 current 与可选旧 current-as-previous 的完整 Runner Generation，再原子切换本机 `agent-run` 命令，并固定只保留 Active Generation 引用的 current 与可选 previous Snapshot。构建、检查或切换失败时，旧 Generation 及其 current、previous 整体保持不变。安装器以可识别、幂等的受管配置把用户级命令目录加入 shell PATH，但不调用系统包管理器、不执行 `sudo`，也不自动安装或升级 Python、Git、`gh`、bubblewrap 等宿主软件。源码来源、Git clean 状态和历史不限制构建或生命周期权限，既有 Delivery Run 不绑定旧 Runner，也不获得状态迁移或兼容层。

安装器只存在于源码树并在用户显式调用时运行；安装完成后不驻留，不参与 Controller 生命周期，也不形成独立 Manager、Launcher、Python 包或版本。install、rollback 与 uninstall 在修改受管状态前必须取得同一个固定用户级非阻塞互斥锁；竞争者立即失败且不修改状态，uninstall 永不删除锁文件，避免替换持锁 inode 后出现第二把锁。官方更新由用户先通过 Git 选择或取得新源码再重新运行安装器，本地自开发使用同一入口。`./install.sh --rollback` 只通过新的完整 Generation 原子交换 current 与 previous，不重建、不调用 Codex、不判断状态兼容性，也不改变 Delivery Run。`./install.sh --uninstall` 删除全部受管 Snapshot、Generation、候选残留、内部 `active` 入口、公开命令 symlink 和安装器添加的 PATH 受管配置，但保留固定锁、GitHub App 配置、全局 Run 定位状态与仓库内 Delivery Run 数据；重复 uninstall 幂等成功，v0.1 不提供 purge 模式。v0.1 不要求 PyPI 或 pipx；以后增加 PyPI 只增加分发渠道，不改变 Snapshot 与显式安装语义。

## Considered Options

- PyPI wheel + pipx 作为首发主入口：普通安装命令更短，但第一版需要额外维护包名、发布身份和 release workflow，而预期用户已经使用 Git 与 GitHub，因此暂缓。
- 独立 Runner Manager 或常驻 Launcher：可以持有更新入口，但会增加第二个运行组件和更新边界；源码安装器已经能够在进程外完成候选检查与原子切换，因此拒绝。
- 直接 `pipx install --force .` 覆盖现有环境：实现更少，但不能保留已决定的候选检查失败不影响旧 Runner、原子切换和双快照行为，因此不采用为规范入口。

## Consequences

用户首次安装与后续重建都需要一份源码目录，并对自己选择的 Git revision 或本地修改负责。执行所选源码的安装器与 build backend 等同于授予该源码当前用户级代码执行权限；“只修改受管位置”的承诺只约束符合本项目合同的源码，不构成对不受信 fork 的 sandbox。项目只维护一个源码树和一套 Runner 版本；README 必须把 release tag 安装作为稳定使用路径，把 branch/fork 安装明确为开发者选择。安装器只管理自己的 XDG 安装目录、Active Runner 入口和唯一边界标记的 PATH 受管配置；同名非受管入口不覆盖、不备份，卸载时发现入口已被替换则保留用户内容并报告 operational error。宿主依赖缺失时返回准确错误，由用户自行安装，可选 `agent-run doctor` 只提供诊断。管理动作不协调已经运行的生命周期进程，因此清理或卸载旧 Snapshot 后不保证旧进程继续可用。

本 ADR 明确修订 ADR 0001 的 Worker GitHub 凭据来源子决策：临时 adapter、绑定当前 Repository identity 的固定只读请求、凭据不进入 Worker 与 Publisher 唯一 Mutation Authority 的边界保持不变；写请求、认证请求和其他仓库请求必须在宿主执行前拒绝。默认 provider 改为宿主已登录的 `gh`，只有操作者显式配置时才使用专用只读 GitHub App。App profile 存在但损坏、私钥不可读、签名或权限校验失败时必须有界失败，不得静默回退；App 模式继续使用短期 installation token 的有界续签与过期读取重试。GitHub 读取身份与源码安装互不耦合。
