# Synthetic external world

`support.offline` provides Scenario, GraphScenario, OfflinePort and the physical
ports used by their tests. `support.offline_api` owns raw fake API records and
SyntheticGitHub; `support.offline_git` owns the private bare Git world.
These are test/experiment support, not a product offline entry.

All workflow decisions, authorization, budgets and recovery remain in the real
Kernel/OperationRunner/RemoteEffects. The harness supplies Agent outputs and
external objects, executes actual private Git operations, and projects traces;
it must not calculate expected business outcomes from the production engine.

From the project directory, use a new task-private directory:

```sh
PYTHONPATH=src:tests python -m support.offline \
  --root /tmp/new-kernel-synthetic-fixture \
  --scenario parent-only-normal --output /tmp/kernel-observation.json
```

Other scenario names are listed by `--help`. The builder accepts only current
raw API world files and self-contained private Git repositories. Unsupported
formats are refused without upgrading or modifying their original files.
Synthetic fault injection stays at physical I/O. No real accounts, credentials
or services are needed. Original data and independent test assertions remain
the oracle; the fake's protocol classification is not an expected-value source.
