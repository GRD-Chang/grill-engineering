"""Finite Parent repair drivers and read-only observations for shared journeys."""

def _advance_wait(scenario, step):
    if step.kind == "waiting":
        retry = step.retry_at or scenario.kernel.operation_state().wake_at
        assert retry is not None, step
        scenario.kernel.clock = lambda: retry + 0.01


def _run(scenario, *, approve=False):
    for _ in range(20):
        step = scenario.run(approve=approve)
        if step.kind != "waiting":
            return step
        _advance_wait(scenario, step)
    raise AssertionError(scenario.kernel.status(scenario.delivery.key))


def _until(scenario, predicate, limit=160):
    for _ in range(limit):
        state = scenario.kernel.status(scenario.delivery.key)
        if predicate(state):
            return state
        step = scenario.step()
        assert step.kind not in {"paused", "complete", "approval"}, (step, state)
        _advance_wait(scenario, step)
    raise AssertionError(scenario.kernel.status(scenario.delivery.key))


def _repairs(scenario):
    # Exclude historical Ticket PRs by their recorded numbers, not naming rules.
    historical = {row["pr_number"] for row in scenario.kernel.source_status()["completions"]}
    return [pr for pr in scenario.port.objects.pulls()
            if "refs/heads/" + pr["base"]["ref"] == scenario.delivery.source_ref
            and pr["number"] not in historical]


def _requests(scenario, method, suffix):
    return [row for row in scenario.port.objects.snapshot()["api_requests"]
            if row["method"] == method and row["endpoint"].endswith(suffix)]
