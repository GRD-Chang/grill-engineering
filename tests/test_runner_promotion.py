from __future__ import annotations

import json
from pathlib import Path

from agent_run.runner_promotion import (
    PromotionVerification,
    _bounded_error,
    run_promotion_handshake,
)


class PassingBackend:
    def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
        assert checkout.name == "checkout"
        return ('{"result_kind":"human_blocker"}', "thread-42")


class SchemaRejectingBackend:
    def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
        raise RuntimeError("invalid_json_schema: root must be an object")


class NetworkFailingBackend:
    def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
        raise RuntimeError("network connection failed")


def verification() -> PromotionVerification:
    return PromotionVerification(
        runner_commit_sha="a" * 40,
        runner_python="/runner/bin/python",
        runner_module="/runner/lib/python/site-packages/agent_run/__init__.py",
        runner_package_sha256="sha256:runner-package",
    )


def test_promotion_handshake_writes_a_bounded_pass_record(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.json"
    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=audit_file,
        backend=PassingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "passed"
    assert result["credential_redaction"] == "passed"
    assert result["thread_id_present"] is True
    loaded = json.loads(audit_file.read_text(encoding="utf-8"))
    assert loaded == result
    assert "prompt" not in loaded
    assert "stdout" not in loaded


def test_promotion_handshake_fails_for_schema_rejection(tmp_path: Path) -> None:
    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=SchemaRejectingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "failed"
    assert result["credential_redaction"] == "failed"
    assert "invalid_json_schema" in str(result["bounded_error"])


def test_promotion_handshake_marks_network_failure_inconclusive(tmp_path: Path) -> None:
    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=NetworkFailingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "inconclusive"
    assert result["credential_redaction"] == "failed"


def test_promotion_handshake_marks_common_api_transport_failures_inconclusive(
    tmp_path: Path,
) -> None:
    class ApiFailingBackend:
        def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
            raise RuntimeError("429 Too Many Requests: TLS DNS ECONNREFUSED")

    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=ApiFailingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "inconclusive"
    assert result["credential_redaction"] == "failed"


def test_promotion_handshake_marks_common_transport_and_throttle_errors_inconclusive(
    tmp_path: Path,
) -> None:
    class ApiFailingBackend:
        def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
            raise RuntimeError("ECONNRESET: socket hang up; request throttled")

    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=ApiFailingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "inconclusive"


def test_promotion_audit_error_redacts_common_credential_forms() -> None:
    error = _bounded_error(
        "api_key=sekret-value secret=other Authorization: Bearer auth-value "
        "private-key=key-value"
    )

    assert "sekret-value" not in error
    assert "other" not in error
    assert "auth-value" not in error
    assert "key-value" not in error
    assert error.count("[REDACTED]") == 4


def test_promotion_audit_error_redacts_multiline_private_keys() -> None:
    error = _bounded_error(
        "private_key=-----BEGIN PRIVATE KEY-----\nMIIEsekret\n-----END PRIVATE KEY----- failed"
    )

    assert "MIIEsekret" not in error
    assert "[REDACTED]" in error


def test_promotion_audit_error_redacts_quoted_and_json_multiline_secrets() -> None:
    error = _bounded_error(
        '"private_key": "-----BEGIN PRIVATE KEY-----\nMIIE-json-secret\n'
        '-----END PRIVATE KEY-----" token="first secret fragment"'
    )

    assert "MIIE-json-secret" not in error
    assert "first secret fragment" not in error
    assert error.count("[REDACTED]") == 2


def test_promotion_handshake_marks_common_auth_errors_inconclusive(tmp_path: Path) -> None:
    class AuthFailingBackend:
        def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
            raise RuntimeError("invalid_api_key: forbidden; invalid token")

    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=AuthFailingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "inconclusive"


def test_promotion_handshake_refuses_to_overwrite_an_audit(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.json"
    audit_file.write_text("{}", encoding="utf-8")

    try:
        run_promotion_handshake(
            checkout=tmp_path / "checkout",
            audit_file=audit_file,
            backend=PassingBackend(),
            verification=verification(),
            codex_version="codex-cli test",
        )
    except ValueError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("expected an existing audit to be rejected")


def test_promotion_handshake_refuses_an_audit_inside_the_runner_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    try:
        run_promotion_handshake(
            checkout=checkout,
            audit_file=checkout / "audit.json",
            backend=PassingBackend(),
            verification=verification(),
            codex_version="codex-cli test",
        )
    except ValueError as error:
        assert "outside" in str(error)
    else:
        raise AssertionError("expected an in-checkout audit to be rejected")
