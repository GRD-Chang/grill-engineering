Output only:
{"result_kind":"publication","commit_message":"...","pr_title":"...","pr_body_markdown":"...","human_blockers":null}

Only when a human must provide a decision, permission, credentials or an irreplaceable action, return:
{"result_kind":"human_blocker","commit_message":null,"pr_title":null,"pr_body_markdown":null,"human_blockers":["What happened; what was tried; what the human must do"]}
