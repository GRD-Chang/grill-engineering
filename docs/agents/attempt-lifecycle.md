# Attempt 生命周期与恢复

三种交付共用原 Attempt、Invocation、输出和租约，不按交付方式创建恢复控制器。
正式推进入口仍是 Core 的选工与事务。物理进程退出与原始返回是两个独立事实：
原始返回先持久化，准确退出并释放原租约后才消费；未知退出不能被 Stop、
换线程或读取到返回替代。

## 责任与入口

- `core/attempt_continuation.py` 校验原 Attempt 的线程、固定输入与 Invocation 谱系，
  从实际 Invocation/output 历史读出各类计数和原绝对期限。
- `core/attempt_lifecycle.py` 拥有等待、异常恢复、语义未完成续接和只读输出修复的判定，
  以及保留期限的规则。输入是当前事务中的准确 Attempt/ContinuationFacts、
  物理线程绑定、租约事实和时钟；不接受完整 Kernel 或回调。
- `admit_preparation` 在调用方原事务内写入 Invocation、output、lease 并结算原准备席位。
  Core 必须先在同一事务核验原准备身份、当前工作与 Executor 授权。
- Core 保留唯一 FIFO/席位选择、当前性与需求/代次核验、预算、业务阶段和事件提交。
  物理 runtime 只报告原 Invocation 的返回、启动/退出与故障类别。

新线程和显式继续不先清除已有结果。保存成功结果后重开时优先消费原结果，
不能再次计费、重建 Attempt 或借新线程改变原用途与 Generation。
保存返回失败仍须记录实际退出；提交响应丢失按原 Invocation 读回，不凭调用异常重派。

## 各类续接的边界

| 类型 | 必需事实与额度 | 时间与输入 |
| --- | --- | --- |
| 异常 `ordinary` | 原进程准确退出、有 backend，原 Attempt 最多一次自动恢复 | 保留原用途/计数；若属于语义续接链，继承同一绝对期限 |
| 容量 `capacity` | 准确退出、实际 not-before 到达；计数与普通恢复分开 | 显式继续也先等待容量；语义链不续期 |
| 语义 `incomplete` | Development 正确返回、准确正常退出、有原线程；最多一次自动语义续接 | 同一 Attempt/Thread 与绝对期限；只读或格式修复结果不能成为可写 successor |
| 格式修复 | 准确正常退出、有 backend、最多两个输出修复 ordinal | 只读；原物理周期可复用时保留 Invocation 与期限，跨 Executor 时保留谱系 |
| 显式继续 | 原控制 Action；历史计数不退款 | 只在原授权允许时开启物理周期；旧只读输入仍只读 |

扩展一种 Worker 故障类别时，先检查 runtime 分类、`worker_failure_policy` 及
存储的 `failure_kind` 合法值约束；将物理类别映射到实际需要的有界恢复家族。
不得把格式、语义、容量和普通恢复混成一套通用 retry 计数。

## 行为验证

`test_kernel_attempt_continuation.py` 覆盖原输入、并发准入、未知退出、换线程、
格式周期与恢复计数；`test_kernel_semantic_incomplete.py` 覆盖语义续接的次数、
期限及只读边界；`test_kernel_execution.py` 与 `test_kernel_agent_release_boundary.py`
保留真实进程、返回保存失败、丢失回执和租约释放边界。
`test_run_scoped_scheduling.py` 覆盖满席时仍允许推进的工作。
