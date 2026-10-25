# First task with Agent Run

[简体中文](quickstart.md) · [Support and validation](support.md)

The first milestone is an installed Runner starting a real Worker on a small task.
Record download, dependency preparation, user authorization and model/CI waits separately;
this guide promises no fixed completion time.

## 1. Check your computer and accounts

Background mode needs Linux, a reachable systemd user manager 254+ and working bwrap.
Executable files on PATH alone do not establish readiness. Follow the exact Python, SQLite,
Git, gh and Codex requirements in [installation](../install.en.md#missing-prerequisites).
An explicit foreground candidate is wired but still awaits physical Host and real delivery validation.
Lack of systemd alone does not establish foreground support; do not disable isolation or use a fake manager.

You authorize Codex and the selected GitHub identity. Host mode reuses native gh login;
Runner PAT/App does not require an additional host login. Prepare the target project's own
build/test tools and the Matt Skills required by the project for the actual Worker user.
Runner's Python environment does not replace the project environment.

## 2. Install one exact version

Obtain a fixed product archive through an authorized maintainer channel, independently verify
its SHA256, and use documentation from that same version. From the extracted package root:

```sh
./install.sh --verify-release
./setup.sh
```

The first command checks the manifest, complete file inventory, wheels and hashed dependency
lock without creating installation state or calling a model. The package contains the product
wheel and four dependency wheels for offline candidate installation; Python, system tools and
Skills are not included. Verification requires CPython 3.11+; setup also requires timeout.
A checksum alongside the package does not establish publisher identity. Full documentation
and examples come from the maintainer's matching-version documentation entry; the package
currently includes only a short installation note. A private build is not a public release.

Read and confirm the plan. Missing administrator access and account login require the
appropriate person; Setup does not grant either. Installation includes a real Codex
compatibility call that consumes account quota. In a new login shell:

```sh
command -v agent-run
agent-run doctor --local --json
agent-run settings show --json
```

Read the report statuses: doctor can exit 0 while reporting issues. Local checks inspect tools
and identity references without checking account authentication. Do not paste raw
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

Explicitly run `agent-run doctor --online --repo OWNER/REPO --json` when ready to check access.
It uses the selected host/PAT/App identities for Runner/Worker authentication and Runner
repository reads; App mode may issue a short-lived token. It calls no model and does not prove
write permissions or authentication inside the actual Worker sandbox. See the
[staged preparation contract](../preparation.md). `doctor --local --mode foreground` now reports
`ready` or `unavailable`, with `business_execution=not_verified`; this preflight is not a business run.

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

The example above uses background mode. The foreground candidate accepts
`agent-run run PARENT_NUMBER --repo OWNER/REPO --foreground` for explicitly authorized candidate validation.
Two normal physical Host checks remain unpassed; real delivery in the parent assistant cloud is blocked.
See the [support matrix](support.md), not a release claim. This candidate accepts only v20 state;
v19 and older Runs are rejected and preserved without migration or compatible resume.

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

For a new foreground execution use `agent-run resume RUN_ID --repo OWNER/REPO --foreground`.
Its normal-cancellation contract requests stop and waits for cleanup; that closure still needs repair and validation.
It does not promise continued execution after terminal closure, logout or session termination.
Final approval and its recovery remain non-cancellable. An active Host retains its recorded backend;
there is no silent fallback from systemd or replacement of an active Host. See the
[shared mode contract](../agent-run.md#运行模式与前台候选验收).

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
