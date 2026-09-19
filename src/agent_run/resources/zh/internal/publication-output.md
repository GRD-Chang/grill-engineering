最后只输出：
{"result_kind":"publication","commit_message":"...","pr_title":"...","pr_body_markdown":"...","human_blockers":null}

只有确实需要人提供决定、权限、凭据或不可替代操作时，返回：
{"result_kind":"human_blocker","commit_message":null,"pr_title":null,"pr_body_markdown":null,"human_blockers":["发生了什么；已尝试什么；人必须做什么"]}
