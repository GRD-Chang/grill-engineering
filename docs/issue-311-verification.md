# Issue #311 开发验证记录

本记录对应当前工作树的开发自测，最终验收由独立验收工程师完成。测试使用项目现有 Python 3.11.16 虚拟环境、固定 6 个 pytest worker、离线 wheelhouse；没有调用付费 Agent 或线上 GitHub 写入。

## 独立审查与处理

按 Standards、Spec 和架构方向对当前未提交及未跟踪文件进行一轮只读审查，发现并处理：

- 整批等待阻塞已就绪 Ticket 的集成：改为 lane 完成即尝试推进空闲集成席位；事件测试验证另一 Ticket 仍在开发时首张 Ticket 已进入 `waiting_checks` 并持席。
- 并发集成读取完整状态后可能覆盖另一 lane 的归并：同一 Executor 内的集成读写与 lane 归并共用状态锁；竞争测试在集成读取和保存之间让另一 lane 尝试写入，验证两张 Ticket 结果均保留。
- 持席 Ticket 局部暂停后直接恢复 `accepted`/`waiting_checks` 会丢失席位：改为先以原 phase 排队，取得席位后恢复；真实 Ticket 发布至等待检查与暂停恢复用例通过。
- 已阻塞 Ticket 的暂停请求无法被消费：拒绝该请求，避免正常 Human Blocker 恢复后再次被旧请求暂停。

共享交付 Engine、Ticket Adapter、Publisher 与 StateStore 仍承担原职责；新增 Ticket 局部状态投影只归并对应 Job、Invocation 和历史，不复制交付循环。状态投影与 StateStore 的私有事务接口仍有耦合，后续若调整状态端口需同步审视这条边界。

## 实际验证与资源

- `make typecheck`：172 个源码文件无类型错误；`git diff --check` 通过。
- 受影响的并发、进程控制、编排、范围修订、崩溃恢复及 CLI 交付用例：`python -m pytest -q tests/test_ticket_parallel_development.py tests/test_multi_worker_control.py tests/test_run_orchestration.py tests/test_scope_revision.py tests/test_run_crash_recovery.py tests/test_cli_delivery.py`，231 passed，205.89 秒。
- 首次 `make test-full`：3420 passed、6 failed、1 error，墙钟 307.25 秒。6 个失败包括 Reviewer 恢复 phase 校验和构造器参数断言；安装器 error 是本 checkout 缺离线 wheel。
- 使用已有离线 wheelhouse 复验 `make test-full TEST_WHEELHOUSE=/home/changcy/public/grill-engineer/.test-wheels PYTEST_ARGS='--durations=15'`：3428 passed、3 failed，墙钟 323.32 秒，pytest 322.92 秒；3 个失败均为旧范围修订用例的顺序 Fixture Agent 与新默认并发不匹配。该用例显式设置 1 个开发 worker 后 3 个参数分支定向复验通过。最慢用例是安装器 smoke，35.94 秒；`/usr/bin/time` 报告的主进程最大 RSS 为 102284 KB，不代表整个 6 worker 进程树。
- 本轮完整套件尚无收口后全量通过记录，且上述两次均超过 300 秒预算；不能据定向通过声称完整验收或预算达标。超时主要落在现有安装器 smoke 和完整套件总运行时间，未取得同条件修改前基线，不能将差额归因于本次新增用例。新并发用例单文件 4 秒左右；未增加重型测试设施。

## 独立验收后修复

独立验收在提交 `c8f858a` 的准确副本上记录 `make test-report` 为 3431 passed、墙钟 358.67 秒；同配置基准为 3415 passed、317.81 秒。验收的真实调用链探针发现：Publication Agent 等待期间整轮状态锁阻塞其他 Ticket 的 Dev/Review 保存；末次失败 CI Reviewer 期间的暂停请求会覆盖不可由普通 `resume --ticket` 恢复的预算升级阶段。

本次把 Controller 刷新保留在短锁内，并使集成每次状态保存从最新持久状态归并仍运行的 Ticket lane，不在 Agent 或外部交付期间持有该锁。真实 Controller/Engine/StateStore 事件用例验证 Publication Agent 挂起时另一 Ticket 完成 Review、保存队列与 Invocation，释放后双方状态仍完整。预算升级阶段不消费暂停请求为 `ticket_paused`；预算门禁落盘时清除该请求。事件用例在 CI 第二轮末次失败 Review 期间请求暂停，验证结果保持 `ci_inner_budget_exhausted`、普通恢复不刷新额度。

本次 `make typecheck` 通过；`python -m pytest -q tests/test_ticket_parallel_development.py tests/test_run_orchestration.py tests/test_state_store.py tests/test_resume_intent.py tests/test_ticket_191_operator_gates.py` 为 161 passed、59.45 秒。`python -m pytest -q tests/test_scope_revision.py tests/test_run_crash_recovery.py tests/test_cli_delivery.py` 为 133 passed、173.13 秒。针对性审查后将状态归并上下文绑定 Run ID，并增加跨 Run 隔离用例；最终 `make typecheck` 通过（172 个源文件），`python -m pytest -q tests/test_state_store.py tests/test_ticket_parallel_development.py` 为 36 passed、7.93 秒。完整套件与 300 秒预算由后续独立验收再次核验；上段结果不代表本次修改后的全量通过。

## 第二次独立验收后修复

独立验收在提交 `0bd0bf5` 的准确副本上记录 `make test-report` 为 3434 passed、墙钟 325.84 秒；同配置基准为 3415 passed、318.90 秒，均超过 300 秒预算。真实调用链探针发现：Ticket 2 Publication 等待期间，Ticket 3 已排队并立即暂停，后续旧快照保存却把 Ticket 3 恢复为 `ready_for_integration`，且在无显式恢复时调用了其 Publication。另有真实 CLI 探针发现 `stop --help` 的 `--ticket` 说明未随个人语言变化，暂停回执也未遵循 Run 固定语言。

本轮在每次共享 Ticket 交付保存时从最新状态归并其他 Ticket 的 Job、Invocation、历史、暂停请求和队列位置，不再只保护仍运行的开发 lane。事件测试先复现旧实现的暂停丢失，再验证另一票 Publication 期间通过独立 StateStore 暂停已排队 Ticket；后续保存、非显式调度和重启均保留 `ticket_paused`，直到显式 `resume --ticket`。CLI 的 `stop`、`resume --ticket` 帮助与两种暂停回执改用中英文短文案资源；测试验证个人语言与 Run 语言相反时，帮助采用个人语言，回执采用冻结 Run 语言，JSON 字段保持原样。

本轮使用 Python 3.11.16 的已安装开发环境；`python -m pytest -q tests/test_ticket_parallel_development.py tests/test_run_orchestration.py tests/test_state_store.py tests/test_delivery.py tests/test_cli_user_defaults.py tests/test_cli_delivery.py` 为 407 passed、259.18 秒。补充真实 CLI 帮助断言后的聚焦复验为 5 passed、4.66 秒；把排队顺序断言改为按实际队列选择并整理帮助断言后，相关用例分别为 3 passed、4.14 秒及 2 passed、0.80 秒。`make typecheck` 通过（172 个源文件），中英文文案 catalog 均为 1039 个键且键集一致，`git diff --check` 通过。本轮未运行修改后完整套件，不能将 407 个选测用例或其耗时当作完整验收及五分钟预算结果。

## 第三次独立验收后修复

独立验收在提交 `3390e41` 的准确副本上记录 `make test-report` 为 3437 passed、墙钟 333.94 秒；其真实调用链探针发现，Controller 在 GitHub 图读取期间持有旧 Run 快照，此时另一 StateStore 立即暂停已排队 Ticket，Controller 随后的整状态保存将暂停覆盖并再次发布该 Ticket。上述全量结果属于验收副本，不代表本节修复后的完整测试结果。

Controller 刷新现以读到的 Run 快照作为持久化前置条件：GitHub 读取保持无锁，每次保存只在 StateStore 短事务中核对最新状态；冲突时普通刷新重读并重新选择 Ticket，显式操作要求重试。返回前再次核对调度和控制字段，防止保存后的全局 Stop 被旧返回值掩盖。若其他 Ticket 已完成而剩余 Ticket 局部阻塞，图投影保留人工等待类型，使 Run 可重启并定向恢复。事件测试覆盖图读取期间立即暂停队列首尾 Ticket、局部 Human Blocker 不阻塞兄弟 Ticket、图读取与保存后发生的全局 Stop，以及其他 Ticket 完成后的局部失败恢复。

本轮在 Python 3.11.16 开发环境运行受影响的并发 Ticket、StateStore、Controller、Run 编排、控制门禁、恢复意图、RunDriver、CLI 交付、范围修订、崩溃恢复与多 Worker 控制共 11 个测试文件：`python -m pytest -q tests/test_ticket_parallel_development.py tests/test_state_store.py tests/test_controller.py tests/test_run_orchestration.py tests/test_ticket_191_operator_gates.py tests/test_resume_intent.py tests/test_run_driver.py tests/test_cli_delivery.py tests/test_scope_revision.py tests/test_run_crash_recovery.py tests/test_multi_worker_control.py`，353 passed、244.92 秒。此后收紧返回前整状态核对并完成聚焦复验：上述新增竞态与状态事务 7 passed、3.52 秒；`make typecheck` 对 172 个源文件通过，`git diff --check` 通过。未在最终修改后运行全量 `make test-full`，不将本轮选测时间用于判断完整套件的 300 秒目标。
