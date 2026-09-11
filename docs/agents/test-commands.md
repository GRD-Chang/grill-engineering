# 测试入口与选测范围

先激活项目专用 Python venv，在仓库根目录运行 `make test-bootstrap`：按
`tests/dev-requirements.txt` 的版本与哈希安装开发工具及传递依赖，用已固定的构建工具安装当前源码，
再执行 `make test-prepare` 下载真实安装测试所需的 wheel。CI 使用同一准备入口；开发时无需每次重新准备，
依赖变化或需要验证新候选的已安装 CLI 时重新执行。不要在系统 Python 中执行该入口。
仅需补充构建 wheel 时执行 `make test-prepare`，版本与哈希来自 `tests/build-requirements.txt`。
pytest 不自动联网补包，缺少或损坏时明确失败并提示准备命令。wheel 默认缓存在 `.test-wheels/`，
不提交 Git；更新构建依赖时同步 pins/hash 并重新准备，可清理旧缓存。
自定义目录用 `make test-prepare TEST_WHEELHOUSE=/专用目录`，全量使用同一 `TEST_WHEELHOUSE`；
直接运行 pytest 时通过 `AGENT_RUN_TEST_WHEELHOUSE` 指定该目录。准备耗时单列，不计入本地全量五分钟预算。

下列入口是相关测试的起点；
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

本地最终验收使用 `make test-full`：先检查 pip 和 Linux `os.memfd_create`、`os.pidfd_open` 能力，再执行完整套件，默认固定六个 pytest worker；缺少关键能力时拒绝全量入口，不以跳过进程边界测试代替验收。`make typecheck` 执行完整类型检查。

提交前优先使用 CI 相同的 Python 3.11 与锁定开发依赖；通过环境报告比较 Git、系统能力及依赖版本。Git 测试只使用公开支持的命令构造状态，例如通过本地 fetch 生成 `FETCH_HEAD`，不直接将伪引用交给 `update-ref`。不同版本本地通过不代表 CI 已通过，不应固定旧版 Git 来规避兼容性缺陷。
六 worker 用于重叠 Git、文件和子进程等待，同时会增加 CPU 与内存占用。资源较少或主机繁忙时用 `make test-full TEST_WORKERS=2`；CI 显式使用这个双 worker 命令，runner 规格和 job 数量不变。
本地默认等价命令为 `python -m pytest -q -n 6 --dist worksteal`；直接运行 `pytest` 仍收集完整套件，没有隐含的慢测试过滤。
调试顺序问题用 `make test-full TEST_WORKERS=0`；临时追加过滤或诊断参数，例如
`make test-policy PYTEST_ARGS='-q --durations=10'`。不要把带 `-k`、`--lf` 等过滤的结果记为全量通过。

普通 pytest 自动按用例隔离 HOME/XDG；共享准备与 CLI 子进程使用同一环境。
Git 初始模板每个 worker 只创建一次，每个用例复制独立文件，禁止修改共享模板或改为共享可写仓库。
默认删除通过用例的临时目录，只保留最近一次失败现场；排障完成后清理本次保留路径。
若使用 `--basetemp`，只能指定本次新建的专用目录，并在取证后自行清理。
并行保持固定上限，不使用 `-n auto`；需要测量其他并发数时同时比较时间、内存与临时存储。

## CI 对齐与诊断

CI 的 `Run tests` 与本地 `make test-report TEST_WORKERS=2` 共用完整测试入口，不过滤集成测试；
本地六 worker 的对应诊断入口是 `make test-report`。已安装 CLI 的 `agent-run --help` 和
`make typecheck` 仍需单独检查。快速回归与全量的验证范围不同，不能用 `make test` 通过推断 CI 必须通过。

报告包含环境版本与系统能力、JUnit、最慢用例各阶段耗时、测试输出和 GNU time 资源记录。
默认写入 `.test-results/`，下一次覆盖；需要比较时用独立的 `TEST_RESULTS`。CI 无论成功或失败均上传报告，
保留七天。JUnit 的用例时间包含 setup/call/teardown；并行用例时间求和不等于墙钟，GNU time 最大 RSS
也不等于并行进程树内存之和。诊断后清理本次本地报告。

排查本地与 CI 的分歧时，先对齐 commit、完整命令与依赖锁，再对照报告中的 Python/Git、OS、CPU、内存及
bubblewrap 能力。CI 的 Python 与 runner 选择以 workflow 为准；主机内核与 Git 可能随 runner 镜像更新，
相同命令不代表任意主机环境完全相同。与环境有关的回归必须在受影响环境验证，不能靠本地重复通过结案。
不要通过跳过集成测试、自动重试或隐去失败来制造一致。

开发依赖变更后，从 `pyproject.toml` 与 `tests/build-requirements.txt` 重新生成锁文件，命令在
`tests/dev-requirements.txt` 文件头；检查变更并运行共同准备入口，保留完整验证证据。
