"""Names of bounded Job archives in an on-demand work projection.

Their current authority and history now live in explicit work relations.
The validator uses these names to distinguish unloaded sibling evidence.
"""
from __future__ import annotations

from agent_run.sqlite_change_job_schema import HISTORY_FIELDS, THREAD_FIELDS

FIELD = "semantic_attempt_history"
FIELDS = HISTORY_FIELDS | THREAD_FIELDS | {FIELD, "review_budget_history"}
