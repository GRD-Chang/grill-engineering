# Agent operation guide

English | [简体中文](agent-guide.md)

You are the engineer responsible for installing and running `agent-run` for the target repository's maintainer.
Deliver actual results, verification evidence, unmet requirements, and next steps. For installation-only requests, stop after setup. For run requests, follow the task to completion or a boundary requiring human action.

Follow the user's instructions and the target repository's `AGENTS.md`. Use current Issue/PR data, the installed version's `--help`, and query results as evidence.
Read documentation matching the selected source version. Record the tool source directory, target `OWNER/REPO`, and user checkout directory if used.

## Choose an entry point

| Task | Steps |
| --- | --- |
| First installation or environment not ready | [Install](install.en.md#first-installation), then [set up the target repository](install.en.md#target-repository-setup) |
| Installed, adding a new repository | [Target repository setup](install.en.md#target-repository-setup) |
| Start or follow a task | “Run a task” below; complete installation and repository checks before the first run |
| Update, rollback, or uninstall | [Installation lifecycle](install.en.md#update-rollback-and-uninstall) |
| Enable or adjust Feishu notifications | [Feishu setup](install.en.md#feishu-notifications-optional), then configure the recipient and notification mode in user defaults |
| Change models, reasoning effort, or limits | [User defaults](user-defaults.md); check which changes apply to new versus existing tasks |

Read only the relevant branch and its required references. Detailed operation references linked below are currently in Chinese.

## Run a task

1. Read the Parent Issue's current body and comments to establish scope and acceptance criteria.
   When subtasks exist, verify native GitHub sub-issues and `blockedBy` relationships; body lists do not define the task graph.
   With no subtasks, use Parent-only delivery. Eligible tasks must be open, have `ready-for-agent`, and have none of `needs-triage`, `needs-info`, or `ready-for-human`. Unresolved blockers prevent selection.

2. Locate and start the task from a shell with the project environment configured. With `--repo`, commands work from any directory; inside a user checkout, omit it to discover the remote without modifying that checkout. Replace placeholders with actual values:

   ```bash
   agent-run runs --repo OWNER/REPO
   agent-run run <parent-issue> --repo OWNER/REPO
   agent-run status --parent <parent-issue> --repo OWNER/REPO --json
   ```

   Runner clones the remote independently and leaves the user checkout unchanged. Only pushed content reaches the Agent; users update their own files, index, and branches.
   On first creation, the managed clone inherits only the effective Git `user.name` and `user.email` from the launch environment, without copying other repository-local Git settings.
   For the same local user, `run` creates or attaches to one unfinished task for the same Repository/Parent, regardless of the launch directory. Follow query instructions when human action is pending.
   If multiple candidates exist, resolve repository and Parent identity rather than choosing the most recent. A command returning does not mean delivery is complete.

3. Use `status` for the current work and next action. For process evidence, read
   `agent-run history --parent <parent-issue> --repo OWNER/REPO --json`.
   Space queries according to development or CI wait times; avoid frequent empty polling.

Completion: delivery is confirmed complete, or the current human boundary and required action are clear.
Report status, PR links if any, actual acceptance/required-check results, and outstanding work. Claim completed delivery only when task state and GitHub evidence confirm final merge, Issue closeout, and required cleanup.

## Human action and recovery

Before changing run state, read the [command boundary table](agent-run.md#命令边界与状态轮转) and select the action for the current state.
Run mutation commands from any directory with the actual Parent and `--repo OWNER/REPO`.

| Situation | Action |
| --- | --- |
| Running or waiting for checks | Continue observing; no restart is needed |
| Execution or command failure, unknown pause reason, manual stop, or supervision timeout | Verify and resolve the cause, then `resume` the original work; add context with `--message`, or use `--new-thread` for an unavailable Agent session |
| Human Blocker needs an answer | Show the original question; pass the user's answer unchanged through `resume --message` |
| `requeue_required` | Check the changed requirements, then `requeue` |
| Final PR awaits approval | Show the PR, acceptance, and check results; execute `approve` only with explicit approval for that final delivery |
| Feedback on the overall result | Pass the original feedback through `revise --message` in an allowed state |
| Pause | Use `stop`; Ctrl-C or closing the terminal only ends observation, while the background task continues |
| Abandon | Follow `abandon` boundaries; preserve uncommitted work by default and require explicit authorization for forced discard |

For exhausted limits, changed scope, or incompatible state, report the decision needed. Recover through the public CLI; never edit persistent run state to bypass gates. See [local state and cleanup](agent-run.md#本地状态与清理) for directory ownership and retention.

For a multi-ticket Parent, Findings and final-PR CI repairs cover the complete Parent and preserve completed Tickets. The program updates the Run Branch after applicable integration and acceptance; only the final Run PR has publication copy, Hosted CI and human approval. There is no internal Repair PR. Ticket PRs and Parent-only delivery retain their own checks and delivery gates.

New Runs use `branch_authority_protocol=3`; old Runs are rejected without migration or deletion of their results. A denied Run Branch update preserves the repair checkout, candidate and write intent and pauses for human action. After the missing permission or repository condition is resolved, continue through `resume`. An unknown push result is reconciled first. Repository write permission alone does not prove that branch rules allow a new head; the Runner does not change rules, force-push or restore an internal PR fallback.
