# 测试入口与选测范围

先激活安装了 `.[dev]` 的 Python 环境，在仓库根目录运行。下列入口是相关测试的起点；
不能仅凭改动文件名判断已经覆盖所有影响。单个用例仍可直接运行
`python -m pytest tests/文件.py::用例名 -q`。

`make test` 默认依次运行下表前四组快速回归；交付、Run、Executor、安装器等重型测试需显式选择模块或运行 `make test-full`。
快速回归通过仅代表这四组通过；修改重型模块时仍需运行相关用例，不能仅凭默认入口判定修改正确。

| 入口 | 验证范围 | 需要补充的直接影响 |
| --- | --- | --- |
| `make test-policy` | Delivery Policy、Review Budget | 策略改变时补 Ticket / Parent-only / Run Repair 的实际调用路径 |
| `make test-state` | StateStore、Scope Revision | 状态结构或持久化语义改变时补 Controller、恢复及相关生命周期 |
| `make test-prompts` | 最终模型可见的 Worker Prompt 合同 | 请求结构、执行方式改变时补 Codex backend 和实际调用方 |
| `make test-locator` | Locator、Human Run Selector 与其持久化回归 | 登记时机改变时补 Controller；修改共享选择逻辑时补相关 CLI 动作 |
| `make test-github` | GitHub adapter、Required Checks、外部监督 | 外部契约改变时补 Delivery / Publication 调用方 |
| `make test-delivery` | Ticket、Parent-only、公开交付流程 | 共享 Engine 变化时补 Run Repair 和恢复路径 |
| `make test-run` | Run 生命周期、Acceptance、Repair、Publication、任务控制 | Controller / StateStore / CLI 等直接调用方 |
| `make test-executor` | CLI 监督、Run Executor、lease、systemd | 进程执行边界变化时补 Codex backend、安装及恢复 |

前四项适合相应模块的局部反馈；后四项覆盖较广，允许超过一分钟。
CLI、安装器等未单列的模块直接选择其测试文件。公共接口、共享状态、生命周期、依赖和测试基础设施
变化应扩大到直接调用方、同族场景及历史回归；无法界定影响范围时运行完整套件。

CI 和最终验收使用 `make test-full`：先检查 pip 和 Linux `os.memfd_create` 能力，再执行完整套件，固定两个 pytest worker；`make typecheck` 执行完整类型检查。
等价的全量测试命令为 `python -m pytest -q -n 2 --dist worksteal`；直接运行 `pytest` 仍收集完整套件，没有隐含的慢测试过滤。
调试顺序问题用 `make test-full PYTEST_ARGS='-q -n 0'`；临时追加过滤或诊断参数，例如
`make test-policy PYTEST_ARGS='-q --durations=10'`。不要把带 `-k`、`--lf` 等过滤的结果记为全量通过。

普通 pytest 自动按用例隔离 HOME/XDG；共享准备与 CLI 子进程使用同一环境。
Git 初始模板每个 worker 只创建一次，每个用例复制独立文件，禁止修改共享模板或改为共享可写仓库。
默认删除通过用例的临时目录，只保留最近一次失败现场；排障完成后清理本次保留路径。
若使用 `--basetemp`，只能指定本次新建的专用目录，并在取证后自行清理。
并行保持固定上限，不使用 `-n auto`；需要测量其他并发数时同时比较时间、内存与临时存储。
