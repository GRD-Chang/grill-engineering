# agent-run

`agent-run` 是一个显式启动的本地 Codex 自动交付控制器。维护者以 GitHub Parent Issue
定义交付范围；Controller 依次驱动开发、独立验收、Required Checks、PR 发布和恢复，
最终是否进入默认分支仍由维护者通过 `approve` 决定。

完整状态机、权限边界和恢复语义见
[`docs/agent-run.md`](docs/agent-run.md)。

## 前置条件

- Python 3.11 或更高版本；
- 已登录的 `codex` 和 `gh` CLI；
- Git、OpenSSL 和 Linux `bubblewrap`；
- 安装在目标仓库上的专用 GitHub App，权限严格限定为
  `actions: read`、`checks: read`、`contents: read`、`issues: read`、
  `metadata: read`、`pull_requests: read` 和 `statuses: read`；
- Publisher 使用的宿主 `gh` 登录具有目标仓库写权限。

GitHub App 的私钥必须保存在仓库外。运行前注入：

```bash
export AGENT_RUN_GITHUB_APP_ID="<app-id>"
export AGENT_RUN_GITHUB_APP_INSTALLATION_ID="<installation-id>"
export AGENT_RUN_GITHUB_APP_PRIVATE_KEY="$(</secure/agent-run-app.pem)"
```

Controller 会为每个 Worker 创建短期只读 installation token；不要把 Publisher token
复用为 Worker token。

### 目标 GitHub 仓库配置

除本机依赖外，运行前还必须在**目标仓库**完成以下配置。缺失其中任一项时，自动交付可能
在检查或合并阶段停住。

1. 在 **Settings → General → Pull Requests** 启用 **Allow squash merging**。Ticket PR
   固定使用 squash merge 合入 Run Branch；仅开启 merge commit 或 rebase merge 不满足要求。
2. 先让仓库 CI 真实产出并成功一次名为 `quality` 的 Check；仅有 workflow 文件、但从未运行
   成功，不足以证明 Required Check 可用。
3. 在 **Settings → Rules → Rulesets** 创建并启用两条 *Active branch ruleset*：
   - 默认分支（仓库实际的 default branch，例如 `main` 或 `master`）：要求 Pull Request 和
     `quality`，并禁止 force push 与删除；
   - Run Branch：匹配 `refs/heads/agent-run/**/run`，要求 `quality`，但设置
     **Do not enforce on create**，并允许交付完成后的受控删除。
4. Required Check 选择 **Any source**，只填写 context `quality`，不要绑定 GitHub Actions
   App / integration。当前 Controller 对非空 `integration_id` 会安全拒绝继续；同时应限制
   仓库写权限，避免其他写入主体伪造同名 status。
5. 将前述专用 GitHub App 安装到该仓库；App 只用于 Worker 的短期只读 token。Publisher
   仍使用独立、具仓库写权限的宿主 `gh` 登录。

启用后，应读回 Ruleset 并对一张指向 Run Branch 的 PR 验证 Required Check：

```bash
gh api repos/OWNER/REPO/rulesets
gh pr checks <pr-number> --repo OWNER/REPO --required --json bucket,name
```

第二个命令必须能看到 `quality` 且为 `pass`，再启动正式 Run。更完整的 Ruleset 兼容性和
权限说明见 [`docs/agent-run.md`](docs/agent-run.md#自托管开发)。

## 开发

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m mypy src/agent_run
```

CI 在最低支持版本 Python 3.11 上执行同一组测试和类型检查，Required Check 名称为
`quality`。

## 使用

在需要交付的目标 Git 仓库中运行：

```bash
agent-run run <parent-issue> --repo OWNER/REPO
```

`run` 会创建或恢复同一 Parent Issue 的未完成 Delivery Run，自动推进到 Required
Checks、结构变化、Human Blocker 或最终人工批准等边界。使用返回的 `run_id` 查看状态：

```bash
agent-run status <run-id>
agent-run history <run-id>
```

检查最终 Run PR 后，只有下面的命令会把结果合入默认分支：

```bash
agent-run approve <run-id> --repo OWNER/REPO
```

## 用 agent-run 开发自身

自托管时必须让控制器版本与被开发版本分离：

1. 从已验证的 commit 创建一个非 editable、按 commit SHA 命名的独立 Python 环境；
2. 从专用干净 clone 启动 Delivery Run，不使用日常脏工作区；
3. 整个 Run 始终使用同一个 Runner 环境；
4. Run 完成并合入 `main` 后，才从新 commit 创建下一版 Runner；
5. 新版必须先通过一次真实 Structured Outputs promotion handshake；API schema rejection 为 failed，认证、网络或 rate limit 只算 inconclusive，均不得启动新的 self-hosting Run；
6. 新版完成 promotion 后，只保留当前版和上一版，清理更旧环境。

必须遵循 `docs/agent-run.md` 的 promotion gate：从干净 detached checkout 的完整 40 位 SHA 安装，
再运行一次 `promotion-handshake` 并保存新的 audit 文件。只有 audit verdict 为 `passed`，才允许在
专用干净 clone 中启动 Run；不可变 Runner 的 lifecycle 命令会机械校验其专属审计记录，缺失、失败或
身份不一致都会拒绝启动。

在首次 `run` 前必须完成并保存真实 promotion handshake 的脱敏审计记录。该记录要包含 Runner SHA、
Codex CLI 版本、Publication schema SHA256、时间、凭据脱敏结果及 `passed` / `failed` /
`inconclusive` verdict；它不能保存 token、私钥、Prompt、完整 stdout 或 transcript。完整的 immutable
SHA pin、`promotion-handshake` 命令、Output Repair、Resume 与 Requeue 操作边界见
[`docs/agent-run.md`](docs/agent-run.md)。

目标仓库应通过 GitHub Ruleset 将 `quality` 设为 Required Check，并覆盖默认分支及
Delivery Run 使用的 Run Branch。没有 Required Checks 时，`agent-run` 会按设计继续，
不能把这种状态误认为已有托管 CI 门禁。当前版本适用的 Ruleset 条件与兼容性约束见
[`docs/agent-run.md`](docs/agent-run.md#自托管开发)。

`bubblewrap` 只保护权威 Git metadata 和 Publisher 凭据，并不是抵抗恶意进程、宿主污染
或数据外泄的通用安全沙箱。只应在受信任的 Issue、代码和主机环境中运行。
