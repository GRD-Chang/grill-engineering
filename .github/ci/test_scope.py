"""Current product test selections shared by Make and CI coverage receipts."""

from __future__ import annotations

import argparse


# These exercise installed delivery, Parent selection, readiness and raw history.
# Receipt unit doubles are supplemented by the real collection consistency test.
CONTRACT_TESTS = (
    "tests/test_kernel_installed_parent_only.py::test_installed_public_parent_only_reaches_approval_close_cleanup_and_normal_host_exit",
    "tests/test_kernel_query_cli.py::test_parent_selector_works_in_a_repo_and_across_directories",
    "tests/test_doctor_readiness.py::test_doctor_cli_separates_installation_and_execution_readiness",
    "tests/test_kernel_query_cli.py::test_history_keeps_raw_feedback_and_pages_without_losing_events",
)
TEST_GROUPS = {
    "policy": ("tests/test_delivery_policy.py", "tests/test_kernel_policy_values.py"),
    "state": ("tests/test_kernel_state_preflight.py", "tests/test_kernel_task_admission.py",
              "tests/test_error_safety.py"),
    "prompts": ("tests/test_prompt_settings.py", "tests/test_kernel_prompt_facts.py"),
    "locator": (
        CONTRACT_TESTS[1],
        "tests/test_kernel_query_cli.py::test_parent_selector_rejects_multiple_runs_and_exact_id_remains_available",
        "tests/test_kernel_query_cli.py::test_explicit_state_directory_selects_its_clone_without_scanning_other_roots",
        "tests/test_kernel_query_cli.py::test_runs_filters_repository_and_parent_without_creating_an_index",
        "tests/test_test_environment.py::test_pytest_isolates_each_case_and_cli_from_host_state",
    ),
    "github": ("tests/test_github_recovery.py", "tests/test_github_identity.py",
               "tests/test_github_checks_fixture.py", "tests/test_kernel_source_reading.py"),
    "delivery": ("tests/test_kernel_execution_delivery.py", "tests/test_kernel_publication_ancestry.py",
                 "tests/test_kernel_remote_effects.py", "tests/test_kernel_local_effects.py"),
    "run": ("tests/test_kernel_execution.py", "tests/test_kernel_exploration.py",
            "tests/test_kernel_abandon.py", "tests/test_kernel_final_requeue.py"),
    "executor": ("tests/test_kernel_executor_controls.py", "tests/test_kernel_executor_command.py",
                 "tests/test_kernel_host_adapter.py", "tests/test_runner_lease.py"),
}


def selection(group: str) -> tuple[str, ...]:
    groups = ("policy", "state", "prompts", "locator") if group == "fast" else (group,)
    return tuple(dict.fromkeys(target for name in groups for target in TEST_GROUPS[name]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("fast", *TEST_GROUPS))
    print("\n".join(selection(parser.parse_args().group)))
