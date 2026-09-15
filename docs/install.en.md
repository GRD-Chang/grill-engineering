# Installation and repository setup

English | [简体中文](install.md)

Complete installation and the target repository checks on this page. To run a task, follow the [agent guide](agent-guide.en.md#run-a-task).
The tool's source directory is used for installation; the target repository is where delivery runs. They may be different directories.

## First installation

1. Obtain the source version requested by the user and record its ref/commit. Use the default branch if none is specified; reuse an existing checkout when appropriate:

   ```bash
   git clone https://github.com/GRD-Chang/grill-engineering.git
   cd grill-engineering
   ./setup.sh
   ```

   If Git is unavailable, download and extract the GitHub source archive for the selected ref, then run `./setup.sh` from its root.
   Branches, forks, and locally modified checkouts are supported. Record the actual source; a clean working tree is not required.

2. Check the changes proposed by Setup. Use `./setup.sh --yes` when existing authorization covers the plan.
   This confirms the plan; it does not grant administrator privileges or authenticate accounts. Report any required login, privilege escalation, or host-session changes. After resolving them, rerun Setup from the same source directory.

   | Exit code | Result |
   | --- | --- |
   | `0` | Installation and execution prerequisites are satisfied; check the target repository next |
   | `1` | Installation failed or the new version was not activated |
   | `2` | Runner is installed, but the execution environment is not ready |

3. Verify the entry point and environment in a new login shell:

   ```bash
   command -v agent-run
   agent-run doctor --json
   ```

   The entry point should be `~/.local/bin/agent-run`, pointing to the activated Runner for the selected version. The installer configures PATH through `~/.profile`.
   Installation includes a real Codex compatibility call that uses account quota. An older version still working does not prove this installation succeeded.

Completion: the selected version is active and execution prerequisites are satisfied. Otherwise, report the specific missing requirements and recovery steps.
Use the installed `agent-run` for delivery, rather than a source or editable environment.

### Missing prerequisites

Setup runs on Linux only. On Ubuntu/Debian, it installs missing or incompatible dependencies from existing APT sources.
It does not add package sources, upgrade the whole system, or install or log in to Codex. On other Linux distributions, resolve unsupported prerequisites separately.
Complete real-host validation of Ubuntu/Debian release and architecture combinations has not been performed; dependency setup support does not establish platform validation.

| Missing requirement | Action and verification |
| --- | --- |
| Python / Git | Provide CPython 3.11+, venv/pip, the build backend, and Git 2.40+; use Setup's report to check compatibility |
| GitHub CLI | Install a compatible version using the [gh Linux instructions](https://github.com/cli/cli/blob/trunk/docs/install_linux.md); run `gh auth status` |
| Codex | Follow the [Codex installation instructions](https://github.com/openai/codex) and log in; check `codex exec --help`, `codex exec resume --help`, and `codex login status` |
| User systemd / bubblewrap | Check `systemctl --user show-environment` in a real user login session; diagnose namespace or permission failures, then rerun Setup's execution probe |

Setup's short-lived systemd probe checks actual execution capability. Preserve host protections and isolation when resolving failures; do not replace init or disable system-wide protections to pass the probe.
`doctor` is read-only: it neither installs nor repairs anything, and does not establish that Skills work, project tests pass, or all repository permissions are sufficient.

## Target repository setup

Enter the target repository and read its `AGENTS.md`. Setup does not configure the items below. Resolve them within the user's authorization and verify each one:

| Check | Completion criterion |
| --- | --- |
| Authentication and repository identity | `codex login status` and `gh auth status` succeed; Git identity and remotes are correct; host gh has the write permissions needed for publication |
| Skills | The user running Codex can discover and read `implement`, `code-review`, their referenced Skills, and any other Skills required by the project. Runner does not bundle them. Follow the current [Matt Skills instructions](https://github.com/mattpocock/skills) when missing; inspect and reuse existing Skills with the same name |
| Project environment | Build, test, and runtime dependencies are ready. Start from a shell with the project's PATH/virtual environment configured; Runner's Python environment does not replace it |
| Ignore rules | Root `.gitignore` includes `/.agent-run/`, and `git check-ignore .agent-run/runs/probe` succeeds. Handle any already-tracked runtime files first |
| CI | If PR workflows exist, verify that their base-branch filters cover the default branch and `agent-run/**`; check required-check rules for both. Runs may proceed with no required checks, but do not describe that as CI passing |

### GitHub authentication: no App required by default

By default, Runner uses the host's authenticated `gh`. That account needs permission to push code, create and merge PRs, and update Issues.
Worker GitHub requests are restricted to read-only operations by the program; publication credentials are not exposed to Workers.
Reuse a valid login, or run `gh auth login` and verify access to the target repository.

Configure a GitHub App only when a separate read-only identity is needed for Workers. It does not replace the host gh login used for publication.
Run `agent-run auth status` to inspect the current identity source. An existing App profile selects App authentication; a broken profile does not silently fall back to gh.
For permissions and commands, read [GitHub App configuration](agent-run.md#github-只读身份) and [permission boundaries](agent-run.md#权限边界).

Run `agent-run doctor --json` and `agent-run settings show` to check the environment, selected models, reasoning effort, and run limits.
For changes, read [user defaults](user-defaults.md). Model availability is established by actual execution.

Completion: each table item has a verification result, with a resolution for any missing requirement. For installation-only requests, report the result here.
If the user requested a run, continue with [task checks](agent-guide.en.md#run-a-task).

### Automatic CI repair

To allow Runner to repair code failures in CI, declare the eligible steps in the target repository's `pyproject.toml`:

```toml
[tool.agent-run.required-checks]
code-failure-steps = [
  "CI::quality::Run tests",
  "CI::quality::Run type checks",
]
```

Names must match the actual `workflow::job display name::step`. Code repair requires a completed, failed Actions job for the current PR head, with every failed step declared above.
Missing, pending, canceled, ambiguous, or platform-related failures remain under supervision.
This configuration does not create required checks or change acceptance or repair limits. See the [operation reference](agent-run.md).

## Update, rollback, and uninstall

Run these commands from the selected tool source root. Rerun `./setup.sh` first if host dependencies are missing.

| Operation | Command and verification |
| --- | --- |
| Update | `./install.sh` builds and activates the current source; then check `agent-run doctor --json`. Source edits do not automatically update the installed version |
| Rollback | `./install.sh --rollback` swaps the current and previous versions without rebuilding or calling Codex; verify the activated version |
| Uninstall | `./install.sh --uninstall` removes Runner, the managed entry point, and the PATH block; check cleanup results and command resolution in a new login shell |

Installation retains only the current and previous versions. Reinstalling still builds a candidate; if its content matches the current version, that version is reused and the compatibility call is skipped.
Build or activation failure preserves the previous active version.

Installation, rollback, and uninstall do not alter existing delivery tasks. Uninstall preserves the installation lock, Run locator records, App configuration, private keys, and each target repository's `.agent-run` directory.
A user-replaced command entry point is preserved and reported as incomplete cleanup. If task state is incompatible, follow the reported error rather than manually migrating it.
