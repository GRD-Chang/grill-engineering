"""Bounded development dispatch; program supervision has its own thread."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from threading import Lock
from time import monotonic
from typing import Any, Callable, TYPE_CHECKING

from agent_run.work_facts import ticket_program_ready
from agent_run.executor_admission import close_admission_on_failure, close_executor_admission
from agent_run.external_supervision import ExternalSupervisor
from agent_run.runner_credentials import runner_credential_scope
from agent_run.state import ConcurrentRunStateError
from agent_run.revisions import TicketGraphDriftError
from agent_run.task_control import TaskControlBusyError
from agent_run.ticket_queue import DEVELOPMENT_WORKER_PHASES
from agent_run.ticket_state import RUN_STOP_STATUSES
from agent_run.ticket_supervision import development_wait_due, supervision_ownership
from agent_run.ticket_work import TicketWorkAdmission, capacity

if TYPE_CHECKING:
    from agent_run.delivery import TicketDeliveryEngine


def develop_frontier(
    engine: TicketDeliveryEngine, run_id: str, state: dict[str, Any], *,
    after_lane: Callable[[frozenset[int]], None] | None,
    supervise: Callable[[frozenset[int]], None] | None,
    refresh_graph: Callable[[], None] | None = None,
) -> bool:
    from agent_run.delivery import SUPERVISION_POLL_SECONDS, TicketDeliveryEngine

    limit = capacity(state)
    admission = TicketWorkAdmission(engine.states, run_id)

    def candidates(current: dict[str, Any], active: frozenset[int]) -> tuple[list[int], bool]:
        ready: list[int] = []
        waiting = False
        if current["status"] in RUN_STOP_STATUSES:
            return ready, waiting
        for number in current["frontier"]:
            if number in active:
                continue
            job = current["ticket_jobs"].get(str(number), {})
            if current["ticket_integration_holder"] == number or job.get("phase") not in {
                None, *DEVELOPMENT_WORKER_PHASES,
            }:
                continue
            if isinstance(job.get("external_wait"), dict):
                queued = any(item["ticket_number"] == number for item in current["ticket_work_queue"])
                due = development_wait_due(engine, run_id, number, already_ready=queued)
                if due is not True:
                    waiting |= due is False
                    continue
            ready.append(number)
        return ready, waiting

    if not (supervise is not None and (
        state["ticket_integration_holder"] is not None or state["ticket_integration_queue"]
    )) and not any(
        state["ticket_integration_holder"] != number
        and state["ticket_jobs"].get(str(number), {}).get("phase") in {None, *DEVELOPMENT_WORKER_PHASES}
        for number in state["frontier"]
    ):
        return False

    def advance(number: int) -> None:
        with runner_credential_scope():
            TicketDeliveryEngine(
                git=engine.git, states=engine.states,
                github=engine.github, agents=engine.agents, development_only=True,
            ).deliver_ticket(run_id, number, complete=False)

    observer_ticket: int | None = None
    observer_lock = Lock()

    def claim_observed_ticket(number: int) -> None:
        nonlocal observer_ticket
        with observer_lock:
            observer_ticket = number

    def observe(lanes: frozenset[int]) -> None:
        assert supervise is not None
        with runner_credential_scope(), supervision_ownership(claim_observed_ticket):
            try:
                supervise(lanes)
            except ConcurrentRunStateError:
                # A live Resume or lane save invalidated a refresh snapshot.
                # The next supervision pass rereads; Agent work keeps running.
                return

    def consume_result(future: Future[None]) -> bool:
        if not isinstance(future.exception(), TicketGraphDriftError) or refresh_graph is None:
            future.result()
            return True
        try:
            refresh_graph()
        except (ConcurrentRunStateError, TaskControlBusyError) as error:
            if isinstance(error, TaskControlBusyError) and not isinstance(error.__cause__, ConcurrentRunStateError):
                raise
            # Keep this finished future until its graph refresh commits.
            # Only the read/validate refresh is retried, never Agent work.
            ExternalSupervisor().sleeper(min(0.05, SUPERVISION_POLL_SECONDS))
            return False
        return True

    # Agent capacity comes only from durable leases. Keep bounded room for
    # returned Agents' program closeout (e.g. a slow currentness read), plus
    # one reserved supervision thread. Program futures are not Agent permits.
    pipeline_limit = 2 * limit
    with (
        engine.states.current_run_reader(run_id, work_only=True) as read_current,
        ThreadPoolExecutor(max_workers=pipeline_limit + 1, thread_name_prefix="ticket") as pool,
        close_admission_on_failure(),
    ):
        futures: dict[Future[None], int] = {}
        observer: Future[None] | None = None
        next_observation = 0.0
        notify_completion = False
        final_observation = False
        while True:
            current = read_current()
            if current is None:
                raise ValueError("Delivery Run disappeared during dispatch")
            if current["status"] in RUN_STOP_STATUSES:
                close_executor_admission()
                return True
            active = frozenset(futures.values())
            with observer_lock:
                owned = active | ({observer_ticket} if observer_ticket is not None else set())
            ready, pending_wait = candidates(current, frozenset(owned))
            seeded = admission.seed([
                number for number in ready if number not in active
                and not ticket_program_ready(current["ticket_jobs"].get(str(number), {}))
            ], current=current)
            if seeded:
                current = read_current()
                assert current is not None
            if notify_completion and after_lane is not None and (
                (not futures and not ready) or any(item["lease_id"] is not None for item in current["ticket_work_queue"])
            ):
                after_lane(frozenset(futures.values()))
                notify_completion = False
            waiting = [item["ticket_number"] for item in current["ticket_work_queue"] if item["lease_id"] is None]
            occupied = sum(
                item["lease_id"] is not None or (
                    item["ticket_number"] in active
                    and not ticket_program_ready(current["ticket_jobs"].get(str(item["ticket_number"]), {}))
                )
                for item in current["ticket_work_queue"]
            )
            for number in ready:
                if len(futures) >= pipeline_limit:
                    break
                job = current["ticket_jobs"].get(str(number), {})
                # A returned result or Candidate commit is program work. It
                # can reach the next Agent boundary without a free lease;
                # that boundary still competes through the durable FIFO.
                closeout = ticket_program_ready(job)
                if number not in active and (closeout or str(number) in current["ticket_pause_requests"]):
                    futures[pool.submit(copy_context().run, advance, number)] = number
            # Queue order, not role or a new thread's startup order, owns priority.
            for number in waiting:
                if occupied >= limit or len(futures) >= pipeline_limit:
                    break
                if number in futures.values():
                    continue
                if number not in ready or number in active:
                    break
                futures[pool.submit(copy_context().run, advance, number)] = number
                occupied += 1
            if observer is not None and observer.done() and consume_result(observer):
                observer = None
                with observer_lock:
                    observer_ticket = None
                next_observation = monotonic() + SUPERVISION_POLL_SECONDS
                # Supervision may have refreshed the frontier or accepted a
                # concurrent Resume. Decide from a fresh snapshot next pass.
                continue
            # Supervision may still own a long Agent call or external I/O.
            # Keep dispatch and global controls live until it has returned.
            if not futures and not ready and not pending_wait:
                if observer is None:
                    if supervise is None or final_observation:
                        break
                    observer = pool.submit(copy_context().run, observe, frozenset())
                    final_observation = True
            else:
                final_observation = False
            if supervise is not None and observer is None and monotonic() >= next_observation:
                observer = pool.submit(copy_context().run, observe, frozenset(futures.values()))
            watched = set(futures)
            if observer is not None:
                watched.add(observer)
            if watched:
                done, _ = wait(watched, timeout=min(0.05, SUPERVISION_POLL_SECONDS), return_when=FIRST_COMPLETED)
            else:
                done = set()
            for future in done:
                if future is observer:
                    continue
                if not consume_result(future):
                    continue
                number = futures.pop(future)
                latest = read_current()
                queued = latest is not None and any(
                    item["ticket_number"] == number for item in latest["ticket_work_queue"]
                )
                notify_completion |= not queued
            if not futures and pending_wait and not ready and (observer is None or observer.done()):
                supervisor = ExternalSupervisor()
                now = supervisor.now()
                deadlines = [
                    window[key] - now
                    for job in current["ticket_jobs"].values()
                    if isinstance(external_wait := job.get("external_wait"), dict)
                    if isinstance(window := external_wait.get("supervision_window"), dict)
                    for key in ("next_retry_at", "deadline")
                    if isinstance(window.get(key), (int, float)) and window[key] > now
                ]
                supervisor.sleeper(min([SUPERVISION_POLL_SECONDS, *deadlines]))
    return True
