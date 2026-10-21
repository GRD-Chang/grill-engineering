# Independent delivery kernel exploration

This package is an offline replacement experiment. It does not import the
existing `agent_run` implementation and is not connected to the installed
Runner, public CLI, live state, GitHub, or user repositories. It is not a
compatibility or migration layer.

The product baseline is `02b008e43386a73c4510250732a7216d7dba17a5`.
`b0a9c34` proved one durable-return slice (one independent test passed).
`2022f02` proved ten initial probes (all passed independently); later fixes and
additional probes require their own exact revision evidence.

## Responsibility and authority

| Module | Sole responsibility | Interface |
| --- | --- | --- |
| `kernel.py` | Authoritative business choices, transactions, ownership, queues, budgets, evidence applicability, control and effect intent | `create`, `claim_executor`, `advance`, `receive`, `control`, `dispatch`, `guard_effect`, `reconcile`, `status/history/scheduling` |
| `storage.py` | SQLite protocol, short transactions, immutable material, byte and query limits | SQL transaction with bounded rows/material |
| `model.py` | Immutable interface values and complete role wire validation | Values, role artifact parser |
| `effects.py` | Actual local Git mechanics on new synthetic data | Explicit Git verbs with per-write authorization guard |
| `offline.py` | Executable scripted Agent and synthetic GitHub adapter; real Git and separate SQLite remote facts | `Scenario`, trace export CLI |

`Kernel` directly reads current relational facts. There is no mutable Run
snapshot, generic Program/Stage interpreter, per-field persistence protocol,
or second adapter that chooses roles and successors. Storage does not decide
business transitions. Adapters execute explicit requests, with the same
read-only authority guard before successive real Git mutations.

Subject phase and gates are independent. Attempt, Invocation and backend Thread
identities are separate. Every Invocation references its actual immutable
input context and raw result. Development is charged at semantic allocation;
valid Review is charged once when its raw result is consumed. A returned
result commits before phase consumption. Each object owns its three budget
purposes; Parent repair does not move mutable quotas between records.

Agent leases, persistent FIFO and the integration seat are separate facts.
Program work can consume a stored result with `allow_agent=False`. CI waiting
does not allocate a lease. Global Stop retires the execution epoch permanently;
late raw can settle its exact Invocation, while a replacement requires exit
evidence, explicit Resume and a new claim. Local Stop leaves siblings intact.

Each effect has one immutable intent, one observed receipt and an independent
application. Unknown effects are read before new work. Target observations can
settle without mutation authority; prior observations require current authority
to retry. Third values and failed reads preserve the original intent. Old PR
narrative can be confirmed after a target append; an unexecuted stale write is
retired before reintegration. Stop can prevent application or the next effect
without preventing exact receipt settlement.

## Executable scope

The same interpreter handles Ticket, Parent-only and Final Run. Normal
Ticket/Parent-only starts with Development and Review. Ticket exhaustion has
an explicitly identified final Development/fallback; Parent-only cannot use
it. Final Run starts with the full review of synthetic completed-Ticket input;
its repairs update Run Branch directly and create no internal repair PR.
Integration failure returns through Development and Git checks. CI uses an
independent cumulative allowance and forces review for both Parent types.
The final merge is a single-parent squash for Ticket and a two-parent commit
for Parent. Explicit Parent approval binds PR/head/base, review, checks and
requirements. Approval remains noncancellable through closeout, with finite
check waiting and explicit continuation after timeout.

Run from the project directory on fresh data:

```sh
PYTHONPATH=src python -m agent_run_kernel.offline \
  --root /tmp/new-independent-kernel-fixture \
  --scenario ticket-review-repair --output /tmp/kernel-observation.json
```

The root must be new. Other scenarios are `ticket-normal`, `ticket-fallback`
(the default N=3), `parent-only-normal`, `full-parent-normal`, and
`full-parent-review-repair` (cut before final Publication). Trace export reads
actual calls, committed facts, effects and Git contents. It does not contain
expected baseline values or claim equivalence. Comparing Git hashes requires
identical initial bytes, edits, metadata and narrative on both sides; different
fixtures cannot be made equivalent by ignoring tree/content identities.

## Remaining work and explicit limits

- The actual Worker adapter, process-group cancellation, credential renewal,
  control capability isolation and protected Git metadata are not implemented.
  Thread logical/backend identities are modeled, but this is not a Codex E2E.
- Host launch/handshake, genuine exit-proof acquisition, Action admission under
  real process races, Runner usage/installation leases and management are not
  connected. `confirm_executor_exit` is a trusted Host seam, not a process probe.
- Full graph fetch/refresh/triage, external dependencies and automatic creation
  of Final Run from a complete multi-Ticket graph are not yet connected. Final
  scenarios start from explicit synthetic completed-Ticket facts. Basic
  dependency waiting, capacity, FIFO and single integration seat are executable.
- No public CLI/JSON, bilingual resources, static user Prompt snapshots,
  notification transport, status/history presentation or defaults configuration
  migration has been provided. Machine observations here are an experiment seam.
- Output repair and automatic Invocation recovery, requeue/revise/abandon,
  source/target resync allowances, full drift/revalidation and all closing-event
  ownership variants are incomplete. Unknown intent and same-result reuse are
  implemented for representative operations, not every external API variant.
- The exploration protocol refuses other database protocols. There is no
  migration, reverse migration, live-Run compatibility, rollback proof or
  installation qualification. Never point it at a user database.
- Storage has real bounded reads, digest checks, SQLite atomicity and physical
  limits. Production schema corruption validation, database inode replacement,
  full storage-fault cancellation and the original bounded write-coordination
  contract remain unqualified.
- Existing 33 functional domains / 18 failure families and original effective
  assertions remain the complete replacement ledger. These representative
  probes do not retire that ledger or authorize removal/integration.

The entire package, including the offline adapter, is counted as new cost;
its tests are counted separately. No legacy production block has been deleted
or proved fully replaceable, so temporary coexistence is not a line reduction.
Any final comparison must include the completed new implementation and all
necessary adapters against only the old responsibilities it actually replaces.

## Design evidence

The five required local skills were read before implementation, under
`/workspace/matt-skills/skills/<name>/SKILL.md`:

| Skill | SHA-256 |
| --- | --- |
| codebase-design | `2c20617f87ec8af6a434859f381b2f061a69b530444e74eb39e78bb016a6d1e2` |
| domain-modeling | `7b925d7b1e341a2eeae33ad68a8a8c0ab889a38ddd22a7598e4c240e5a7556a3` |
| implement | `483bdc4ccf4ac31d86ac7beeaa0d0db80313e71e120bb8c59b3196ae5decd53d` |
| tdd | `5505d25bbaa8fc14a79e4d20163936958992a2995b7f1dd1d6197edc9ed722e0` |
| code-review | `09eb147f793e4647949edd32dbad278f0554dfb56e0d46b699cd4c2c55bf0357` |

Both candidate `.agents/skills` and `/workspace/.agents/skills` were absent.
Root/project AGENTS, domain guide, CONTEXT, testing/test-support, state-storage
and ADRs 0012/0019/0020/0021/0022/0023/0024/0025/0026/0028/0029 were inspected.
The parent owns independent Standards/Spec/recovery review and CPU execution;
the candidate source has one writer. Test seams follow the user's already
authorized vertical behaviors and are the new public Kernel/effect interfaces.

Official references actually inspected: [SQLite atomic commits](https://sqlite.org/atomiccommit.html)
support one local atomic transaction; they do not cover remote mutations.
[Temporal activity idempotency](https://docs.temporal.io/activity-definition#idempotency)
motivates explicit effect intents and observations; no workflow platform or
exactly-once claim is imported. Actual platform model/effort routing is not
visible and has not been verified.
