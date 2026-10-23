# Native delivery runtime

The installed `agent-run` command and `python -m agent_run_kernel` both route to
`query_cli.main`. Their command contract is defined by that parser and its
registered native lifecycle adapters; neither entry launches a synthetic world.

`Kernel` owns transitions, authorization, immutable execution facts, budgets,
FIFO and the integration seat. `OperationRunner` coordinates bounded physical
operations. `RemoteEffects`, `LocalEffects` and `LocalGit` implement real I/O
while preserving the Kernel's write guards, exact readback and cleanup duties.
Query readers expose committed facts without reconstructing another Run engine.

Synthetic GitHub, scripted Agents and isolated scenario builders belong to
`tests/support`. See [the harness guide](../../tests/support/README-offline.md).
They call the production runner and replace physical I/O only; they are not an
installed product offline mode. No source compatibility shim exposes them.

The [current architecture overview](../../docs/architecture.md) is the single
overview of these responsibilities, the formal call chain and integration status.
Only `independent-delivery-kernel-v19` state is supported; old protocols are
refused without conversion, upgrade or deletion of existing work.
