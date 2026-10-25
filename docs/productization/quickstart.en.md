# First task with Agent Run

[简体中文](quickstart.md) · [Support and validation](support.md)

The first milestone is an installed Runner starting a real Worker on a small task.
Record download, dependency preparation, user authorization and model/CI waits separately;
this guide promises no fixed completion time.

## 1. Check your computer and accounts

The current baseline needs Linux, a reachable systemd user manager 254+ and working bwrap.
Executable files on PATH alone do not establish readiness. Follow the exact Python, SQLite,
Git, gh and Codex requirements in [installation](../install.en.md#missing-prerequisites).
A cloud environment without systemd is not ready on this baseline. Foreground compatibility
is a separate pending work package; there is no foreground flag in this guide.

You authorize Codex and the selected GitHub identity. Host mode reuses native gh login;
Runner PAT/App does not require an additional host login. Prepare the target project's own
build/test tools and the Matt Skills required by the project for the actual Worker user.
Runner's Python environment does not replace the project environment.

## 2. Install one exact version

Download the source archive from the maintainer's release entry, verify its published SHA256,
and use documentation from that same version. From the extracted source root:

```sh
./setup.sh
```

Read and confirm the plan. Missing administrator access and account login require the
appropriate person; Setup does not grant either. Installation includes a real Codex
compatibility call that consumes account quota. In a new login shell:

```sh
command -v agent-run
agent-run doctor --json
agent-run settings show --json
```

Read the report statuses: doctor can exit 0 while reporting issues. Its current gh check
covers host login, not the separately selected PAT/App repository access. Do not paste raw
configuration output into a public Issue. Use the existing [update/rollback/uninstall
commands](../install.en.md#update-rollback-and-uninstall).

## 3. Keep your chosen authentication method

Runner and Worker independently support host/PAT/App; both default to host.

| Method | What you supply | Existing configuration |
| --- | --- | --- |
| host | Native gh login for the same user and required repository permissions | `agent-run settings configure --runner-github-source host --worker-github-source host` |
| PAT | An authorized environment-variable or private-file reference, never the token in argv | [Identity settings](../user-defaults.md#github-身份) |
| App | Existing App/installation metadata and a private-key file reference | [App settings](../user-defaults.md#app-配置与迁移) |

The two sides may use different sources. Credentials determine Worker GitHub capabilities;
there is no local command allowlist. Write capability does not grant publication or approval
responsibility. A Run freezes its source/reference; resume does not silently switch identity.

## 4. Start a small task

Prepare your own copy of the [standard-library Python example](../../examples/first-ticket/README.md).
Creating a repository, Issues and CI requires your authorization. Push the baseline first;
check task labels/native dependencies, Runner permission to update managed branches, and
CI coverage of the default branch and `agent-run/**/run`. Replace placeholders:

```sh
agent-run run PARENT_NUMBER --repo OWNER/REPO
agent-run status --parent PARENT_NUMBER --repo OWNER/REPO --json
agent-run history --parent PARENT_NUMBER --repo OWNER/REPO --json
```

A receipt is acceptance of a request, not completed delivery. Runner uses an independent
clone and leaves your checkout unchanged. Development can edit working files; authoritative
Git metadata stays read-only, and Review's delivery checkout is read-only too. Workers use
the existing trusted-Agent model, not a general hostile-code sandbox. Confirm actual Worker
start through this attempt's Invocation/process facts, not help output or an install probe.

## 5. Stop, resume and approve explicitly

The existing background mode survives leaving the terminal; Ctrl-C ends observation only.
Use the exact `RUN_ID` returned by the query:

```sh
agent-run stop RUN_ID --repo OWNER/REPO
agent-run resume RUN_ID --repo OWNER/REPO
```

Confirm the actual result in status. Resume retains work and identity; ordinary resume does
not reset Attempts or budgets. Follow the current gate and [operation guide](../agent-guide.en.md)
when limits are exhausted. Unknown external writes require original-intent readback before replay.
At the final PR, inspect exact results, Review and checks. Only after explicit maintainer approval:

```sh
agent-run approve RUN_ID --repo OWNER/REPO
```

Approval does not authorize a later changed candidate. Pending/unknown checks are not passes.
Verify actual merge and completion afterwards. For a blocker, use the [safe report template](diagnostics.md);
do not delete Runs, edit the database, broaden credentials or repeatedly restart the task.
