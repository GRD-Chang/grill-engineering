本次覆盖 code-review 默认的 Markdown 报告步骤，最后只输出：
{"checks":{"e2e":{"status":"...","evidence":"...","findings":[]},"standards":{"status":"...","evidence":"...","findings":[]},"spec":{"status":"...","evidence":"...","findings":[]}}}

每个 status 只取 pass、fail 或 blocked：有必须修复的问题时为 fail 且 findings 非空；
pass 与 blocked 的 findings 为空。确需人提供决定、权限或不可替代操作而无法形成结论时用 blocked，
在 evidence 中说明原因、已尝试的办法和人必须做什么。
e2e 的 evidence 说明实际操作及结果，standards 说明审查范围或基准，spec 说明验收条件及覆盖情况。
只有三个维度均 pass 才通过；不输出额外报告或问题处理对照表。
