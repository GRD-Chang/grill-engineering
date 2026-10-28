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
PROMPT_CONTRACT_TESTS = ("tests/test_prompt_settings.py", "tests/test_show_prompts_catalog.py")
# Fail early on unresolved native boundaries; these remain in the full suite.
# A successful boundary stage is not complete product acceptance.
BOUNDARY_TESTS = (
    'tests/test_kernel_cleanup_prepare_timeout.py::test_cleanup_prepare_timeout_preserves_complete_delivery_and_reenters',
    'tests/test_kernel_public_abandon_completed_ticket.py::test_public_abandon_recovers_its_completed_ticket_and_closes_unapproved_final_pr',
    *(f'tests/test_kernel_managed_entry_end_to_end.py::{name}' for name in (
        'test_public_run_reaches_ticket_close_through_managed_host_after_lost_merge_reply',
        'test_public_parallel_review_and_publication_keep_each_threads_binding',
        'test_public_resume_after_exact_foreground_host_loss_preserves_parallel_work',
        'test_public_ticket_stop_keeps_late_raw_unconsumed_then_resumes_in_the_original_managed_host',
        'test_public_revise_repairs_the_real_final_pr_from_original_feedback_in_the_same_managed_host',
        'test_public_requeue_delivers_fresh_ticket_and_final_generations_in_the_original_managed_host',
        'test_public_ticket_repair_deletes_an_earlier_candidate_file_without_a_human_blocker',
        'test_public_local_worker_failure_preserves_completed_and_independent_tickets',
        'test_controlled_native_commit_failure_reaps_both_workers_before_disarm_and_public_resume')),
    'tests/test_kernel_control_operation_observation.py::test_public_resume_timeout_reaps_exact_guardian_without_touching_unrelated_child',
    'tests/test_kernel_operation_integration.py::test_real_core_n1_ci_poll_advances_during_independent_long_agent',
    'tests/test_kernel_notification_sender_facts.py::test_sender_compresses_real_development_to_review_and_preserves_gate_budget_and_return_order',
)
TEST_GROUPS = {
    'boundary': BOUNDARY_TESTS,
    "policy": ("tests/test_delivery_policy.py", "tests/test_kernel_policy_values.py"),
    "state": ("tests/test_kernel_state_preflight.py", "tests/test_kernel_task_admission.py",
              "tests/test_error_safety.py"),
    "prompts": (*PROMPT_CONTRACT_TESTS, "tests/test_kernel_prompt_facts.py"),
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
    if group == "fast":
        # Prompt allocation/repair uses full real delivery preconditions. Keep
        # it in test-prompts/full; quick exercises the actual renderer/catalog.
        return tuple(dict.fromkeys((*TEST_GROUPS["policy"], *TEST_GROUPS["state"],
            *PROMPT_CONTRACT_TESTS, *TEST_GROUPS["locator"])))
    return TEST_GROUPS[group]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("fast", *TEST_GROUPS))
    print("\n".join(selection(parser.parse_args().group)))
