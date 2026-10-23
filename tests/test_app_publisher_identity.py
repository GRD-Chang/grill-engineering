"""Reject foreign/malformed App actor metadata through the live authentication leaf."""
import io
import json

import pytest

from agent_run.github_auth import GitHubCredentialError, resolve_app_login
from agent_run.github_auth_profile import GitHubAppProfile


@pytest.mark.parametrize("app", [{"id": 999, "slug": "delivery"}, {"id": 123},
                                  {"id": 123, "slug": "bad/slug"}])
def test_app_actor_rejects_wrong_or_missing_metadata(tmp_path, monkeypatch, app):
    monkeypatch.setattr("agent_run.github_auth._create_app_jwt",
                        lambda *_args, process_registry=None: "synthetic-jwt")
    monkeypatch.setattr("agent_run.github_auth.urllib.request.urlopen",
                        lambda *_a, **_kw: io.BytesIO(json.dumps(app).encode()))
    key = tmp_path / "synthetic-app-key"
    key.write_text("test-key")
    key.chmod(0o600)
    profile = GitHubAppProfile("123", "456", key)
    with pytest.raises(GitHubCredentialError, match="identity is invalid"):
        resolve_app_login(profile)
