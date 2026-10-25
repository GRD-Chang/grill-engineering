# 发行验收：从归档到实际交付

本页是发行维护者的记录与断言脚手架，不是第二套 Runner，不发 GitHub 请求、
不启动模型、不批准任务。需要对应授权时才在真实环境执行。
先固定归档 SHA256、源码 commit、同版文档与实际安装内容身份；每轮单独保存原账，
失败/恢复后的结果不得追溯覆盖首次失败，也不能跨版本拼绿。

## 三条实际旅程

1. 干净用户从发行归档安装，完成真实账号授权及必要 Skills/项目工具准备，
   从任意目录启动小任务，并观察真实 Worker Invocation 与进程事实。
2. 两个依赖 Ticket 完成开发、Review、集成、CI 修复及整体验收；
   正确最终 PR 等待明确人工批准，批准后核对实际合并、Issue 关闭和 Runner 完成。
3. 终端离开后后台继续，公开 stop 确认停止，再公开 resume 保留原工作推进；
   另在父助手实际云完成安装到交付，并验证前台适配声明的退出/恢复语义。
   当前前台适配未交付，此项保持 blocked，不套用后台语义。

同一任务的正常停止/恢复与故障恢复分别记录；普通 resume 不重置预算。
记录下载、准备依赖、人工授权、Worker 启动、模型/CI 等待和清理各自耗时；
约15分钟首次启动仅作体验目标，不是任意网络或模型下的保证。

| 记录项 | 必须人工核对的断言 |
| --- | --- |
| `archive_install` | 真正从所记归档解压安装，实际激活身份与同版文档一致；没有使用 editable 或旧安装冒充 |
| `identity_access` | Runner/Worker 真实子进程可用；host/PAT/App 各自有选择与访问证据，无静默 fallback；不保留秘密 |
| `real_worker` | 新任务真实 Codex 调用开始并返回，Git metadata/Review 只读合同仍成立；help/兼容探针不足 |
| `ticket_delivery` | 正确原生依赖、两 Ticket 和 Parent 的真实成果及独立 Review；最终默认分支函数/测试正确 |
| `ci_repair` | 准确 PR/head 的真实失败、Runner 修复及新 head 完整 checks；只一次绿 CI 或手工修复不足 |
| `approval_boundary` | 批准前未合并；显式授权绑定当前 PR/head/base/验收事实；之后真实合并/关闭，不只命令回执 |
| `stop_resume` | stop 后无旧执行重叠，原工作/身份保留；resume 正确推进；unknown 写先原意图读回，不重复外部写 |
| `background_delivery` | 真实 user manager 上终端离开仍推进，完成及后代退出/必要清理有证明，不是 fake manager |
| `parent_cloud_delivery` | 在父助手自身实际云从同版归档安装并真实交付；完整隔离、崩溃/取消/退出和恢复准入获独立验证 |

没有触发 CI 修复填 unrun；没有权限或 Host 条件不满足填 blocked；真实失败填 fail。
旧1836项回归、APT 分支、bwrap 短探针或另一台 savedcloud 不能替代这些断言。
父云项是本次目标版本的发布阻断项，不能从清单删去或挪到“以后支持”。

## 记录方法

复制 [acceptance-record.template.json](acceptance-record.template.json) 到仓库外私有现场。
记录中的 evidence 只是该现场的证据编号与文件 SHA256，不放路径、账号、凭据或正文。
原始来源、命令、退出码、时间和各安装内容身份保存在编号对应的私有记录；
审核人独立核对同一发行身份、真实执行与证据内容。不要把真实日志提交到示例仓库。
原失败轮保留；恢复或重测创建新记录，声明它与原轮的关系，不覆盖原件。

```sh
python3 check_acceptance.py /path/to/private/acceptance-record.json
```

命令在本目录执行。检查器只校验字段、冻结身份格式、九项状态和证据摘要是否齐备；
exit0 只表示**填写完整**，不验证证据真实性，也不授予发布/批准许可。
原模板必须非0，任何 fail/blocked/unrun 必须非0。未知/多余/重复字段不能被静默忽略。
诊断摘要另用 [白名单模板](diagnostics.md)，本记录不是任意日志的脱敏工具。

私有维护者可复用仓库外层 `tools/real-e2e/README.md` 的已有 single/multi 准备与观察能力，
不要另造调度器或拷贝私有源码到公开分发。其真实 run 会建仓、建 Issue 并执行最终批准，
只有授权覆盖这些动作时才使用，不能把它当作无副作用 smoke test。
它不自动证明本页新增的 CI repair、stop/resume 或父云兼容覆盖，仍需逐项证据。
本页九项也不替代冻结集成后的产品回归与独立 review。
