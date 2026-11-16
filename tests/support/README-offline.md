# 合成外部世界

`support.offline` 提供 Scenario、GraphScenario、OfflinePort 与物理端口；
`offline_api` 保存合成 GitHub 原始记录，`offline_git` 操作私有 bare Git。
这些模块仅用于测试和实验。

业务选择、授权、预算和恢复由真实 Kernel、OperationRunner、RemoteEffects 执行。
夹具只提供 Agent 返回与外部对象，执行真实 Git 并记录轨迹；预期结果由独立断言判断。

从项目目录选择本任务的新目录运行：

```sh
PYTHONPATH=src:tests python -m support.offline \
  --root /tmp/new-kernel-synthetic-fixture \
  --scenario parent-only-normal --output /tmp/kernel-observation.json
```

场景见 `--help`。创建时独占新 `authority`，可重新打开自己创建的合成世界。
夹具仅支持其生成的固定布局与小型配置；不导入任意仓库，也不模拟元数据遭恶意并发替换的环境。
每次操作检查私有根、顶层 Git 路径、配置、commondir/alternates 和环境重定向；
不递归遍历整个对象树。Git hooks 与非本地传输被禁用。
新建的客户端与 bare 接收端都持久禁用自动维护；本地 push 传输不保证接收端继承客户端的命令行配置。
夹具返回前不留下后台维护，完整文件快照仍检查锁文件，不通过忽略临时锁来绕过竞态。
每次观察仍新读 refs、commit 与祖先关系，写入继续使用真实 CAS、直接 ref、锁、
原授权和丢回复后的读回，不能缓存事实或授权。
产品 ManagedWorkspace、安全检查和原生生命周期由各自产品测试负责。

故障注入位于物理 I/O；测试不使用真实账号、凭据或服务。
原始数据和独立断言是结果依据，夹具不推导预期业务状态。
