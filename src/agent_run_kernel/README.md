# Offline delivery kernel exploration

This is an executable replacement experiment on synthetic data, isolated from
the installed Runner and existing `agent_run` implementation. It is not a
supported live Runner and has no live-state migration or rollback guarantee.

`Kernel` owns business transitions, authorization, immutable attempts/results,
budget purposes, FIFO and the integration seat in short SQLite transactions.
`Storage` provides bounded durable facts; it does not choose successors.
Adapters execute one explicit effect and call the Kernel's read-only guard
before successive actual Git writes. Reviewers receive separate, exact Git
validation checkouts; their temporary files never enter a Candidate.

Run one scenario from the project directory:

```sh
PYTHONPATH=src python -m agent_run_kernel.offline \
  --root /tmp/new-kernel-synthetic-fixture \
  --scenario ticket-review-repair --output /tmp/kernel-observation.json
```

The root must be new. Available scenarios also include `ticket-normal`,
`ticket-fallback` (N=3), `parent-only-normal`, `full-parent-normal`, and
`full-parent-review-repair` (cut before final Publication). The adapter uses
real local Git and SQLite, with scripted Agents and synthetic remote PRs.

The public experiment interfaces are `create`, `claim_executor`, `advance`,
`receive`, `control`, `dispatch`, `guard_effect`, `reconcile`, and read-only
`status`, `history`, `scheduling`. Roles retain the original wire contracts.

Implemented representative paths include all three delivery kinds, initial
and integration review/development loops, CI repairs, explicit approval,
Stop/Resume with permanent epoch retirement, raw return reuse, independent
budget renewal and exact unknown-effect observations. Final Parent repair
updates Run Branch without an internal Repair PR.

Actual Worker/process isolation, credentials, Host/installation, public CLI,
configuration/languages/notifications, complete graph scheduling and all
currentness/abandonment/resync variants remain incomplete. Final scenarios use
synthetic already-completed Ticket input. Host exit confirmation is a trusted
interface with no genuine process-proof adapter here. No complete legacy
responsibility has been retired or proven behaviorally equivalent.

Use only newly created test data. The experiment refuses other database
protocols; it must never be opened on a user database. All new code and adapters
count toward replacement cost, even during temporary coexistence.
