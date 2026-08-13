from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_run.runner_promotion import (
    PromotionVerification,
    require_promotion_audit,
    run_promotion_handshake,
)
from agent_run.error_safety import bounded_error
from agent_run.state import StateStore


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
    assert result["bounded_error"] == "Codex rejected the canonical Structured Outputs schema"


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
    assert result["bounded_error"] == (
        "Codex handshake did not complete because of an external condition"
    )


def test_promotion_handshake_audits_an_unavailable_codex_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_file = tmp_path / "audit.json"
    monkeypatch.setattr("agent_run.runner_promotion.codex_cli_version", lambda: None)

    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=audit_file,
        backend=PassingBackend(),
        verification=verification(),
    )

    assert result["codex_cli_version"] == "unavailable"
    assert result["handshake_verdict"] == "failed"
    assert result["sandbox"] == "not_started"
    assert json.loads(audit_file.read_text(encoding="utf-8")) == result


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
    error = bounded_error(
        "api_key=sekret-value secret=other Authorization: Bearer auth-value "
        "private-key=key-value"
    )

    assert "sekret-value" not in error
    assert "other" not in error
    assert "auth-value" not in error
    assert "key-value" not in error
    assert error.count("[REDACTED]") == 4


def test_promotion_audit_error_redacts_multiline_private_keys() -> None:
    error = bounded_error(
        "private_key=-----BEGIN PRIVATE KEY-----\nMIIEsekret\n-----END PRIVATE KEY----- failed"
    )

    assert "MIIEsekret" not in error
    assert "[REDACTED]" in error


def test_promotion_audit_error_redacts_quoted_and_json_multiline_secrets() -> None:
    error = bounded_error(
        '"private_key": "-----BEGIN PRIVATE KEY-----\nMIIE-json-secret\n'
        '-----END PRIVATE KEY-----" token="first secret fragment"'
    )

    assert "MIIE-json-secret" not in error
    assert "first secret fragment" not in error
    assert error.count("[REDACTED]") == 2


def test_promotion_audit_error_redacts_unquoted_multiline_secrets() -> None:
    error = bounded_error("token=first-secret-fragment\nSECOND_SECRET_FRAGMENT")

    assert "first-secret-fragment" not in error
    assert "SECOND_SECRET_FRAGMENT" not in error
    assert error == "token=[REDACTED]"


def test_promotion_audit_error_redacts_quoted_authorization_and_escaped_json() -> None:
    error = bounded_error(
        'Authorization: Bearer "first secret fragment" '
        'token="foo\\",LEAK"'
    )

    assert "first secret fragment" not in error
    assert "foo" not in error
    assert "LEAK" not in error
    assert error.count("[REDACTED]") == 2


def test_promotion_audit_error_redacts_json_escaped_keys_and_bearer_tokens() -> None:
    json_error = bounded_error('{"tok\\u0065n":"json-secret"}')
    bearer_error = bounded_error("token=Bearer bearer-secret")

    assert json_error == '{"token":"[REDACTED]"}'
    assert "json-secret" not in json_error
    assert bearer_error == "token=[REDACTED]"
    assert "bearer-secret" not in bearer_error


def test_promotion_audit_error_redacts_embedded_json_credentials() -> None:
    error = bounded_error('upstream: {"tok\\u0065n":"embedded-secret"} failed')

    assert error == 'upstream: {"token":"[REDACTED]"} failed'
    assert "embedded-secret" not in error


def test_state_store_redacts_all_durable_error_fields(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    store.save_run(
        "run-1",
        {
            "error": 'upstream: {"tok\\u0065n":"error-secret"}',
            "last_publication_error": "https://user:password@proxy.invalid",
            "last_error": "token=cleanup-secret",
            "api_error": "access_token=durable-access-token client_secret=durable-client-secret",
            "diagnostics": [
                {"code": "failed", "message": "Authorization: Bearer auth-secret"}
            ],
        },
    )

    loaded = store.load_run("run-1")
    assert loaded is not None
    assert "error-secret" not in repr(loaded)
    assert "password" not in repr(loaded)
    assert "auth-secret" not in repr(loaded)
    assert "cleanup-secret" not in repr(loaded)
    assert "durable-access-token" not in repr(loaded)
    assert "durable-client-secret" not in repr(loaded)


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


def test_promotion_audit_must_authorize_the_exact_runner(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.json"

    with pytest.raises(ValueError, match="no promotion audit"):
        require_promotion_audit(verification(), audit_file, "codex-cli test")

    run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=audit_file,
        backend=PassingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )
    require_promotion_audit(verification(), audit_file, "codex-cli test")

    record = json.loads(audit_file.read_text(encoding="utf-8"))
    record["publication_schema_sha256"] = "sha256:another-schema"
    audit_file.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="does not authorize"):
        require_promotion_audit(verification(), audit_file, "codex-cli test")


def test_promotion_audit_does_not_persist_raw_backend_credentials(tmp_path: Path) -> None:
    class CredentialFailingBackend:
        def publication_schema_handshake(self, checkout: Path) -> tuple[str, str]:
            raise RuntimeError("https://user:password@proxy.invalid sk-secret-value")

    result = run_promotion_handshake(
        checkout=tmp_path / "checkout",
        audit_file=tmp_path / "audit.json",
        backend=CredentialFailingBackend(),
        verification=verification(),
        codex_version="codex-cli test",
    )

    assert result["handshake_verdict"] == "failed"
    assert result["bounded_error"] == (
        "Codex handshake failed before schema acceptance was confirmed"
    )
