Output only complete JSON at the end:
`{"result_kind":"development","summary":"Actual changes, actual validation and known limitations","human_blockers":null}`
The summary must briefly record actual commands, results, validation targets and necessary environment. Do not present checks that were not run, or passes on old code, as results for this delivery. Do not provide a separate issue-by-issue disposition table.
Only when a human must provide a product decision, permission, credentials or an irreplaceable external action, return:
`{"result_kind":"human_blocker","summary":null,"human_blockers":["What happened; what was tried; what the human must do"]}`
Continue addressing problems that can be resolved within your responsibilities.
