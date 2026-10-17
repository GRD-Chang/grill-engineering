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

`make test` 在一次 pytest 中运行下表前四组快速回归的用例并集，重叠的 StateStore/Locator 用例只执行一次。
Policy、State、Prompts 按完整模块选择，Locator 沿用原有关键词筛选；各组独立入口和 `make test-report` 继续可用。
交付、Run、Executor、安装器等重型测试需显式选择模块或运行 `make test-full`。
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

本地最终验收使用 `make test-full`：先检查 pip，并真实创建、关闭 Linux memfd/pidfd，再执行完整套件，默认固定六个 pytest worker；缺少接口或内核拒绝调用时拒绝全量入口，不以跳过进程边界测试代替验收。`make typecheck` 执行完整类型检查。

提交前优先使用 CI 相同的 CPython 3.11.16 与锁定开发依赖；通过环境报告比较 Git、系统能力及依赖版本。Python 版本号本身不能证明构建支持 memfd/pidfd，部分便携构建缺少这些接口。先激活所选 venv，并确认 `python` 与 `python3` 都来自该环境；仅指定 Make 的 `PYTHON` 不会改变所有外部工具的解释器选择。Git 测试只使用公开支持的命令构造状态，例如通过本地 fetch 生成 `FETCH_HEAD`，不直接将伪引用交给 `update-ref`。不同版本本地通过不代表 CI 已通过，不应固定旧版 Git 来规避兼容性缺陷。
六 worker 用于重叠 Git、文件和子进程等待，同时会增加 CPU 与内存占用。资源较少或主机繁忙时用 `make test-full TEST_WORKERS=2`。
CI 使用两个独立的产品测试分片，每台 runner 仍固定两个 worker；不提高单机并发或产品测试超时。分片 1 复用已安装环境，在 pytest 前执行 CI 规则回归、实际安装冒烟和类型检查，独立检查无需等待产品测试结束。每个 worker 完整收集用例后，按排序后的 nodeid 轮流分配至分片，两个分片互斥且并集为完整套件。参数错误、重复 nodeid 或空分片明确失败。默认本地入口不分片。
本地默认等价命令为 `python -m pytest -q -n 6 --dist worksteal`；直接运行 `pytest` 仍收集完整套件，没有隐含的慢测试过滤。
`make test-full` 未收到显式 `TMPDIR` 时，在标准且可写的 `/run/user/<uid>` tmpfs 中放置测试临时文件；其他环境沿用 `/tmp`。
该路径对受控 Worker 子进程可见，避免 `/dev/shm` 在 bubblewrap 的 `/dev` 挂载下不可见；需要对照磁盘临时目录时可显式设置 `TMPDIR=/tmp`。
调试顺序问题用 `make test-full TEST_WORKERS=0`；临时追加过滤或诊断参数，例如
`make test-policy PYTEST_ARGS='-q --durations=10'`。不要把带 `-k`、`--lf` 等过滤的结果记为全量通过。

普通 pytest 自动按用例隔离 HOME/XDG，并将当前测试解释器所在目录放在 PATH 首位；共享准备与 CLI 子进程使用同一环境。继承的 Git 仓库位置和 `GIT_CONFIG*` 覆盖会被清除，系统 Git 配置被禁用，避免本地宿主设置进入 CI 场景；用例仍可在隔离 HOME 中创建 `.gitconfig` 或显式构造配置覆盖。Git Trace2 等诊断变量保留。
Git 初始模板每个 worker 只创建一次，每个用例复制独立文件，禁止修改共享模板或改为共享可写仓库。
默认删除通过用例的临时目录，只保留最近一次失败现场；排障完成后清理本次保留路径。
若使用 `--basetemp`，只能指定本次新建的专用目录，并在取证后自行清理。
并行保持固定上限，不使用 `-n auto`；需要测量其他并发数时同时比较时间、内存与临时存储。

## CI 对齐与诊断

CI 每个分片的 `Run tests` 使用 `make test-report TEST_WORKERS=2 PYTEST_ARGS='-v --test-shard-count=2 --test-shard-index=0'`（另一分片为 index 1）。
必需的 `quality` 只汇总选择、作业结果及两个分片的完整证据，不重复安装环境或执行测试。独立检查有明确的结果核验，失败、取消或意外跳过均不能使汇总通过。分片各自上传独立诊断，仍保留全部集成测试；单个分片的通过不代表全量通过。

成功分片通过 `.github/ci/ci_evidence.py record` 生成 `evidence.json`：只重新收集 nodeid，不重新执行测试，逐项核对该分片的 JUnit 与完整集合应分配的范围。证据绑定仓库、准确提交、workflow run/attempt、workflow 身份、依赖文件以及原始 JUnit/环境报告。`quality` 只接受本次 workflow run 的两个完整分片，并核验相同收集范围和环境配置、互斥覆盖及全部成功结果；缺失、重复、过滤、不一致或跳过的结果明确失败。证据不跨 PR 或 workflow run 复用。同一 run 重跑失败作业时，可保留较早 attempt 的成功分片，仍核验相同候选和覆盖并保留原 attempt；不接受未来 attempt，附件上传覆盖同名旧结果。

锁定的 pytest 9 将成功 subtest 计入 suite 的 `tests`，并复用父 `testcase`。
`support.sharding` 在 JUnit controller 从真实报告统计成功 subtests，写入版本化属性
`agent-run-pytest-passed-subtests-v1`；解析器要求 `tests = testcase + 显式成功 subtest 数`。
没有属性的普通报告仍要求两者相等，未知差额不能当作成功。父 nodeid 继续决定分片覆盖，
成功 subtest 不能补足缺失的父测试；失败、错误、跳过、缺失或重复结果仍拒绝。
旧 subtest 报告需要重新运行生成属性，不手工补写计数。

CI 规则的定向回归为 `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s .github/ci -p 'test_*.py' -q`。
该回归使用六个微型真实 pytest 父测试，覆盖串行和 xdist 分片、参数地址、fixture/unittest
subtests，另验证实际失败、跳过和 teardown error 报告，以及缺失、重复和错误分片拒绝。
pytest 升级时须复验这些实际报告的兼容性。
本地六 worker 的对应诊断入口是 `make test-report`。已安装 CLI 的 `agent-run --help` 和
`make typecheck` 仍需单独检查。快速回归与全量的验证范围不同，不能用 `make test` 通过推断 CI 必须通过。

报告包含 Python、SQLite 等环境版本与系统能力、JUnit、最慢用例各阶段耗时、测试输出和 GNU time 资源记录。
CI 使用 `PYTEST_ARGS='-v'`，逐例记录 nodeid 与结果；即使 job 到时取消、JUnit 和
资源汇总尚未生成，已有失败也能在上传的测试日志中定位。
`quality` 的 `Run tests` 是分片结果和覆盖证据汇总；排查实际 pytest 失败或取消时，读取同一 run 的
`product-tests` 作业日志和 `test-diagnostics-shard-0` / `test-diagnostics-shard-1` 附件。
安装冒烟或类型检查失败时，读取 `product-tests (1)` 中对应步骤。测试资源报告只包含 `make test-full`；前置检查、依赖准备与完整 CI 关键路径需另按作业和步骤计量，不能用测试资源报告代表完整 CI 成本。
默认写入 `.test-results/`，下一次覆盖；需要比较时用独立的 `TEST_RESULTS`。CI 无论成功或失败均上传报告，
保留七天。JUnit 的用例时间包含 setup/call/teardown；并行用例时间求和不等于墙钟，GNU time 最大 RSS
也不等于并行进程树内存之和。诊断后清理本次本地报告。

排查本地与 CI 的分歧时，先对齐 commit、完整命令与依赖锁，再对照报告中的 Python/Git、OS、CPU、内存及
bubblewrap 能力。CI 的 Python 与 runner 选择以 workflow 为准；主机内核与 Git 可能随 runner 镜像更新，
相同命令不代表任意主机环境完全相同。与环境有关的回归必须在受影响环境验证，不能靠本地重复通过结案。
不要通过跳过集成测试、自动重试或隐去失败来制造一致。

开发依赖变更后，从 `pyproject.toml` 与 `tests/build-requirements.txt` 重新生成锁文件，命令在
`tests/dev-requirements.txt` 文件头；检查变更并运行共同准备入口，保留完整验证证据。
