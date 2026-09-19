This overrides code-review's default Markdown report step. Output only:
{"checks":{"e2e":{"status":"...","evidence":"...","findings":[]},"standards":{"status":"...","evidence":"...","findings":[]},"spec":{"status":"...","evidence":"...","findings":[]}}}

Each status must be pass, fail or blocked. Use fail with nonempty findings for problems that must be fixed. For pass and blocked, findings must be empty. Use blocked only when a human decision, permission or irreplaceable action is genuinely required before a conclusion can be reached; explain the reason, attempted approaches and necessary human action in evidence.
The e2e evidence describes actual operations and results; standards describes review scope or baseline; spec describes acceptance criteria and their coverage.
Acceptance passes only when all three dimensions pass. Do not output an additional report or an issue-disposition table.
