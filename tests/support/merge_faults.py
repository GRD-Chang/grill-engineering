from __future__ import annotations

from pathlib import Path

from agent_run.git import GitRepository


class CandidateSaveCrashGit(GitRepository):
    def commit_merge_resolution_candidate(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        attempt: int,
        squash_candidate_sha: str | None = None,
        candidate_intent: dict[str, object] | None = None,
        expected_conflict_paths: tuple[str, ...] = (),
    ) -> str:
        candidate = super().commit_merge_resolution_candidate(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            attempt=attempt,
            squash_candidate_sha=squash_candidate_sha,
            candidate_intent=candidate_intent,
            expected_conflict_paths=expected_conflict_paths,
        )
        raise RuntimeError(f"crash after creating {candidate}")


class CleanReprepareCrashGit(GitRepository):
    def commit_merge_resolution_candidate(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        attempt: int,
        squash_candidate_sha: str | None = None,
        candidate_intent: dict[str, object] | None = None,
        expected_conflict_paths: tuple[str, ...] = (),
    ) -> str | None:
        raise RuntimeError("crash before creating clean reprepare Candidate")


class FindingSnapshotCrashGit(GitRepository):
    def create_integration_repair_snapshot(
        self,
        checkout: Path,
        *,
        candidate_sha: str,
        attempt: int,
    ) -> str | None:
        snapshot = super().create_integration_repair_snapshot(
            checkout,
            candidate_sha=candidate_sha,
            attempt=attempt,
        )
        raise RuntimeError(f"crash after preserving finding repair {snapshot}")


class FindingReplayCrashGit(GitRepository):
    def _replay_finding_delta(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
        snapshot_sha: str,
    ) -> None:
        raise RuntimeError("crash before replaying finding delta")


class FindingReplaySaveCrashGit(GitRepository):
    def _replay_finding_delta(
        self,
        checkout: Path,
        *,
        run_head_sha: str,
        default_head_sha: str,
        candidate_sha: str,
        snapshot_sha: str,
    ) -> None:
        super()._replay_finding_delta(
            checkout,
            run_head_sha=run_head_sha,
            default_head_sha=default_head_sha,
            candidate_sha=candidate_sha,
            snapshot_sha=snapshot_sha,
        )
        raise RuntimeError("crash after replaying finding delta")
