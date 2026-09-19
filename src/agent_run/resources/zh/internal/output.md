最后只输出完整 JSON：
`{"result_kind":"development","summary":"实际改动、实际验证及已知限制","human_blockers":null}`
summary 简要记录实际命令、结果、验证对象与必要环境，不把未执行或旧代码的通过当作本次结果，不另交逐条问题处理表。
只有确实需要人提供产品决定、权限、凭据或不可替代的外部操作时，返回：
`{"result_kind":"human_blocker","summary":null,"human_blockers":["发生了什么；已尝试什么；人必须做什么"]}`
可在当前职责内解决的问题继续处理。
