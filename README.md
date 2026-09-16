# grill-engineering

English | [简体中文](README.zh-CN.md)

![One command starts automated ticket development, verification, repair, and full acceptance before your approval.](docs/images/automation-overview.png)

After [Matt Pocock Skills](https://github.com/mattpocock/skills) breaks your work into GitHub tickets,
grill-engineering runs Codex to implement and verify them, with less manual coordination between tasks.

[Get started](#get-started) · [Screenshots](#screenshots) · [Feishu notifications](#feishu-notifications) · [Agent guide](docs/agent-guide.en.md)

## Why I built this

Matt's `grill-me` and `grill-with-docs` help work through an idea. `to-spec` and `to-tickets`
turn it into a spec and tickets on GitHub. During development, two problems remain:

1. Someone still has to keep the work moving: start the next ticket, follow up on fixes, and handle PRs and merges.
2. An agent can report completion while leaving bugs, missing requirements, or breaking existing behavior. Its work needs to be checked.

## How grill-engineering works

The `agent-run` command runs Codex to implement the tickets in dependency order. A separate Codex agent
checks the results against the requirements and sends any problems back to the development agent.
Implementation, verification, and fixes run in a loop, managed by the program.

Once the requirements, tickets, and environment are ready, run this from any directory:

```bash
agent-run run <parent-issue> --repo OWNER/REPO
```

Runner develops in an independent clone of the remote and leaves your local checkout unchanged. You can continue the same repository and Parent task from different directories.

The main workflow:

```mermaid
flowchart TD
    A[Select a ready ticket] --> B[Codex development agent]
    B -->|Submit work| C{Independent Codex verification}
    C -->|Send problems back for fixes| B
    C -->|Pass| D[Ticket PR, checks, and merge]
    D --> E{All tickets completed?}
    E -->|No| A
    E -->|Yes| F{Independent verification of the whole feature}
    F -->|Send problems back for fixes| G[Codex development agent]
    G -->|Submit fixes for verification| F
    F -->|Pass| H[Final PR, checks, and human approval]
    H --> I[Merge into the default branch]
```

Individual tickets can look correct while the combined feature still fails. After all tickets are
integrated, another agent checks the whole feature against the original requirements. Any problems
go back to a development agent for fixes and another verification pass.

When human input is needed, the run pauses and saves its work. The final PR is merged into the default
branch after the maintainer reviews and approves it. See the [run reference](docs/agent-run.md) for
execution and recovery details.

## Screenshots

`status` shows what is running and whether anything needs attention. Here is an example task:

![agent-run status showing development progress, model, and remaining budget](docs/images/status.en.png)

`history` shows the time spent on each development and verification round, along with PR checks and merges.

<details>
<summary>View the history of a real test run</summary>

![agent-run history showing development, verification, PR checks, human approval, and the final merge](docs/images/history.en.png)

</details>

The run keeps going after the terminal closes. Use `stop` to pause it and preserve the work,
and `resume` to continue later.

## Feishu notifications

Follow progress without keeping a terminal open. Once enabled, Feishu sends you a message when a ticket
finishes, your input or approval is needed, or the whole task is complete.

Concise mode is the default, covering key progress and anything that needs your attention. Switch to
detailed mode to follow each development, verification, and repair round. Cards include available work
summaries and execution times, with links to the relevant issue or PR.

<p align="center">
  <img src="docs/images/feishu-delivery.en.jpg" width="460" alt="Feishu cards requesting PR approval and reporting completion, with Agent execution time and elapsed time">
</p>

<details>
<summary>View development, verification, and repair notifications in detailed mode</summary>

Detailed mode shows when each development round starts, what it delivers, and how long it takes.

<p align="center">
  <img src="docs/images/feishu-development.en.jpg" width="460" alt="Feishu cards showing a development round starting and finishing, with a work summary and execution time">
</p>

When verification finds problems, it also shows the findings and the automatic fixes that follow.

<p align="center">
  <img src="docs/images/feishu-verification.en.jpg" width="460" alt="Feishu cards showing a verification issue queued for automatic repair, followed by ticket completion">
</p>

</details>

Feishu notifications are optional and off by default; see the
[notification guide (Chinese)](docs/notifications.md) for setup.

## Get started

Give your agent the following prompt with your repository path and parent issue URL.
The [agent guide](docs/agent-guide.en.md) covers installation, setup, and running tasks.

```text
Read https://github.com/GRD-Chang/grill-engineering/blob/main/docs/agent-guide.en.md.
Install agent-run, check that this repository and its GitHub tickets are ready, then start the run.
Repository path: <local path>
Parent issue: <GitHub issue URL>
Let me know when you need input or approval for the final PR.
```

The current backend is Codex CLI with GitHub, running on Linux. Your agent can follow the guide to
install and configure it. Runs use your Codex account quota; models and repair limits can be adjusted
through [user settings](docs/user-defaults.md).

For Matt's workflow, see the [upstream guide](https://www.aihero.dev/skills).

## Acknowledgments

This project draws on ideas from [ClawSweeper](https://github.com/openclaw/clawsweeper). Thanks to its contributors for sharing their work.

## License

[MIT](LICENSE).
