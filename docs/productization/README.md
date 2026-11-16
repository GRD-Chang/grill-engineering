# 安装、首次任务与发行验证

[中文快速开始](quickstart.md) · [English quickstart](quickstart.en.md) ·
[支持与验证范围](support.md) · [发行验收](release-acceptance.md) ·
[无秘密问题报告](diagnostics.md) · [最小示例](../../examples/first-ticket/README.md)

本目录把现有安装与交付入口串成首次使用路径，不改变 Core、Git 只读、
host/PAT/App 身份、后台运行、人工批准或恢复合同。
版本下载入口应链接**同一版本的文档和固定产品归档**，并提供归档 SHA256；
不要把安装了旧版本的用户导向 main 上尚未交付的命令。

本轮在 e014 基线上整合固定产品包、分阶段 doctor 和首次任务说明。
归档先用 `./install.sh --verify-release` 校验；安装后先 `doctor --local`，
需要认证和仓库读取时再显式 `doctor --online --repo OWNER/REPO`，见[准备说明](../preparation.md)。
这些材料的静态检查、示例测试和记录检查器不代表真实发行验收完成。
只有 [支持矩阵](support.md) 中有同版实际证据的条目才能标为已验证。
Windows 的 WSL2 与 macOS 的 Linux VM 是待逐机验证的 Linux Guest 路径，
不是原生后端；先按[Guest 布局与诊断步骤](support.md#linux-guest-路径与只读诊断)核对，
不把 Docker 内工具存在、`uname` 或 doctor 就绪报告当作跨平台启动验收。
无 systemd 的前台候选已接入显式入口，仍待物理 Host 与真实交付验收；
能力预检不代表业务通过，见[运行模式](../agent-run.md#运行模式与前台候选验收)。

维护者接入一版发行时：固定归档、文档和安装身份，执行真实验收，记录失败及未运行项，
再更新支持矩阵。公开发布、真实建库与最终批准分别需要对应授权；本目录不执行它们。
