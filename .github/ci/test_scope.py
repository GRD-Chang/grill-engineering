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
# Small component contracts precede long lifecycle stories; all remain in full coverage.
# DecisionClock fault tests are storage decisions, not native clock evidence.
NATIVE_CONTRACT_TESTS = (
    'tests/test_repository_save_fifo.py::test_native_calling_task_clock_uses_linux_top_level_offsets',
    'tests/test_repository_save_fifo.py::test_native_nonleader_thread_clock_uses_its_own_top_level_offsets',
    'tests/test_storage_save_fifo.py::test_earlier_save_cannot_be_overtaken_after_preflight[separate]',
    'tests/test_storage_save_fifo.py::test_queue_wait_and_sqlite_busy_share_one_second',
    'tests/test_repository_save_fifo.py::test_registration_commit_unknown_reads_only_original_uuid_and_never_reinserts',
    'tests/test_repository_save_fifo.py::test_business_commit_unknown_keeps_actual_write_and_never_replays_body',
    'tests/test_repository_save_fifo.py::test_registration_commit_known_failure_keeps_category_and_never_registers_again',
    'tests/test_repository_save_fifo.py::test_shared_owner_poison_rolls_back_whole_body_without_reentry',
    'tests/test_repository_save_fifo.py::test_readonly_and_exact_control_bypass_do_not_capture_clock_or_touch_tickets',
    'tests/test_kernel_remote_effects.py::test_comment_collections_keep_distinct_issue_pr_and_foreign_identity',
    'tests/test_kernel_remote_effects.py::test_close_uses_original_comment_watermark_and_rejects_reopened_or_foreign_issue',
    'tests/test_kernel_ticket_recovery_transport.py::test_recovery_comment_has_one_exact_marker_and_independent_fresh_authority[lost_ack]',
    'tests/test_kernel_ticket_recovery_transport.py::test_recovery_comment_has_one_exact_marker_and_independent_fresh_authority[equivalent_timestamp]',
    'tests/test_kernel_ticket_recovery_transport.py::test_recovery_comment_has_one_exact_marker_and_independent_fresh_authority[metadata]',
    'tests/test_kernel_control_github_fixture.py::test_managed_control_github_keeps_parent_and_ticket_rest_facts_separate',
    'tests/test_kernel_control_github_fixture.py::test_comment_handler_commits_canonical_rows_and_mirrors_without_read_ticks[False]',
    'tests/test_kernel_control_github_fixture.py::test_comment_handler_commits_canonical_rows_and_mirrors_without_read_ticks[True]',
)
FIXED_REPAIR_REGRESSION_TESTS = (
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[parent-human-zh]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[parent-human-en]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[parent-json-zh]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[parent-json-en]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[exact-run-human-zh]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[exact-run-human-en]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[exact-run-json-zh]',
    'tests/test_cli_command_diagnostics.py::test_stop_receipt_uses_frozen_language_and_keeps_internal_identity_only_in_json[exact-run-json-en]',
    'tests/test_kernel_sqlite_faults.py::test_public_stop_reports_real_commit_interruption_without_partial_action',
    'tests/test_local_cleanup_intent_core.py::test_first_local_intents_need_no_source_or_delivery_read',
    'tests/test_local_cleanup_intent_core.py::test_real_intent_is_not_authority_after_revocation[stop]',
    'tests/test_local_cleanup_intent_core.py::test_real_intent_is_not_authority_after_revocation[owner]',
    'tests/test_local_cleanup_intent_core.py::test_worktree_command_preparation_keeps_fresh_core_and_resource_guards[stop]',
    'tests/test_local_cleanup_intent_core.py::test_worktree_command_preparation_keeps_fresh_core_and_resource_guards[owner]',
    'tests/test_kernel_cleanup_adapter.py::test_worktree_cleanup_authorizes_before_locks_and_at_command_start[revoked]',
    'tests/test_kernel_cleanup_observation_projection.py::test_projection_cannot_be_rebound_to_other_effects_or_observation_types',
    'tests/test_kernel_control_policy.py::test_ticket_policy_window_does_not_change_a_sibling_call_or_its_allowances',
    'tests/test_kernel_control_policy.py::test_checkpoint_thread_policy_is_creation_frozen_and_legal_exceptions_stay_independent[control-False-new-per-attempt]',
    'tests/test_kernel_control_policy.py::test_checkpoint_thread_policy_is_creation_frozen_and_legal_exceptions_stay_independent[control-True-reuse]',
    'tests/test_kernel_control_policy.py::test_checkpoint_thread_policy_is_creation_frozen_and_legal_exceptions_stay_independent[control_host-False-new-per-attempt]',
    'tests/test_kernel_control_policy.py::test_checkpoint_thread_policy_is_creation_frozen_and_legal_exceptions_stay_independent[control_host-True-reuse]',
    'tests/test_repository_query_adapters.py::test_real_current_database_uncheckpointed_wal_materials_and_budget_survive_new_layout_refusal',
    'tests/test_kernel_host_exit_sender.py::test_temporary_unsent_claim_does_not_label_later_success_unsent',
)

TEST_GROUPS = {
    'fixed-repair-regression': FIXED_REPAIR_REGRESSION_TESTS,
    'native-contract': NATIVE_CONTRACT_TESTS,
    "policy": ("tests/test_delivery_policy.py", "tests/test_kernel_policy_values.py", "tests/test_budget_policy.py"),
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
    "delivery": ("tests/test_kernel_managed_entry_end_to_end.py::test_public_run_reaches_ticket_close_through_managed_host_after_lost_merge_reply", "tests/test_kernel_publication_ancestry.py",
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
