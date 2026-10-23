"""Exact Git resource retirement; the Kernel owns authorization and retry policy."""
from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from time import monotonic

from .effects import LocalGit
from .model import Effect, EffectResult, KernelError, ResourceInspection, ResourceWitness, resource_wire


def _result(disposition: str, reason: str, **facts: object) -> EffectResult:
    return EffectResult(disposition, raw={"reason": reason, **facts})


def _ref(effect: Effect, operation: str) -> tuple[str, str] | None:
    ref = effect.payload.get("branch_ref")
    expected = effect.payload.get("expected_head")
    if (effect.operation != operation
            or not isinstance(ref, str) or not ref.startswith("refs/heads/")
            or "\0" in ref or ref != effect.source_ref
            or not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected) is None):
        return None
    return ref, expected


def _read(git: LocalGit, *arguments: str, cwd: Path | None = None,
          check: bool = True) -> subprocess.CompletedProcess[str]:
    # LocalGit's broad write guard also covers `worktree list`. Inspection
    # must remain possible after the intent's execution epoch was retired.
    reader = LocalGit(git.root)
    reader.env = git.env.copy()
    return reader.run(*arguments, cwd=cwd, check=check)


def _authority(git: LocalGit) -> None:
    if git.authorize is None:
        raise KernelError("cleanup_authority_missing")
    git.authorize()


def inspect_local_ref(git: LocalGit, effect: Effect) -> EffectResult:
    """Observe only the original ref; a failed read never establishes absence."""
    identity = _ref(effect, "cleanup_local_ref")
    if identity is None:
        return _result("third", "invalid_ref_identity")
    ref, expected = identity
    try:
        result = _read(git, "for-each-ref", "--format=%(refname)%09%(objectname)", ref)
        matches = [line.partition("\t")[2] for line in result.stdout.splitlines()
                   if line.partition("\t")[0] == ref]
        if not matches:
            return _result("target", "ref_absent")
        if len(matches) != 1 or not matches[0]:
            return _result("unknown", "unreadable_ref")
        return _result("prior" if matches[0] == expected else "third", "ref_observed", head=matches[0])
    except (KernelError, OSError):
        return _result("unknown", "ref_read_failed")


def cleanup_local_ref(git: LocalGit, effect: Effect) -> EffectResult:
    """Delete with the original source SHA as Git's atomic compare-and-swap."""
    observed = inspect_local_ref(git, effect)
    if observed.disposition != "prior":
        return observed
    identity = _ref(effect, "cleanup_local_ref")
    assert identity is not None
    ref, expected = identity
    try:
        listed = _read(git, "worktree", "list", "--porcelain")
    except (KernelError, OSError):
        return _result("unknown", "worktree_registry_read_failed")
    if f"branch {ref}" in listed.stdout.splitlines():
        return _result("third", "branch_checked_out")
    _authority(git)
    try:
        git.run("update-ref", "--no-deref", "-d", ref, expected, check=False)
    except (OSError, subprocess.SubprocessError):
        pass  # A lost response is reconciled against the original intent.
    return inspect_local_ref(git, effect)


def _remote(effect: Effect) -> tuple[str, str, str] | None:
    identity = _ref(effect, "cleanup_remote_ref")
    remote = effect.payload.get("remote")
    if identity is None or not isinstance(remote, str) or not remote or "\0" in remote:
        return None
    return remote, *identity


def inspect_remote_ref(git: LocalGit, effect: Effect) -> EffectResult:
    """Read a remote independently of the local ref or an earlier response."""
    identity = _remote(effect)
    if identity is None:
        return _result("third", "invalid_remote_identity")
    remote, ref, expected = identity
    try:
        result = _read(git, "ls-remote", "--refs", "--", remote, ref)
        lines = [line.split("\t") for line in result.stdout.splitlines()]
        if not lines:
            return _result("target", "remote_ref_absent")
        if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref:
            return _result("unknown", "unreadable_remote_ref")
        observed = lines[0][0]
        return _result("prior" if observed == expected else "third", "remote_ref_observed", head=observed)
    except (KernelError, OSError, subprocess.SubprocessError):
        return _result("unknown", "remote_read_failed")


def cleanup_remote_ref(git: LocalGit, effect: Effect) -> EffectResult:
    """Attempt one exact lease deletion; inspect the original ref after any response."""
    observed = inspect_remote_ref(git, effect)
    if observed.disposition != "prior":
        return observed
    identity = _remote(effect)
    assert identity is not None
    remote, ref, expected = identity
    _authority(git)
    try:
        git.run("push", "--porcelain", f"--force-with-lease={ref}:{expected}",
                "--", remote, f":{ref}", check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    return inspect_remote_ref(git, effect)


def _worktree_paths(values: tuple[object, object, object]) -> tuple[Path, Path, Path] | None:
    if any(not isinstance(value, str) or not value or "\0" in value for value in values):
        return None
    checkout, owned, common = (Path(str(value)) for value in values)
    if (not all(path.is_absolute() for path in (checkout, owned, common))
            or not checkout.is_relative_to(owned) or checkout == owned
            or any(path.resolve() != path for path in (checkout, owned, common))):
        return None
    return checkout, owned, common


def _worktree(effect: Effect) -> tuple[Path, Path, Path, str, str] | None:
    if effect.operation == "abandon_worktree":
        resource = _abandon_resource(effect)
        if resource is None:
            return None
        paths = _worktree_paths((resource.checkout, resource.owned_root, resource.common_git_dir))
        return None if paths is None else (*paths, resource.source_ref, resource.head_sha)
    identity = _ref(effect, "validation_cleanup" if effect.operation == "validation_cleanup" else "cleanup_worktree")
    if identity is None:
        return None
    paths = _worktree_paths((effect.payload.get("checkout"), effect.payload.get("owned_root"),
                             effect.payload.get("common_git_dir")))
    return None if paths is None else (*paths, *identity)


def _valid_resource(resource: ResourceWitness) -> bool:
    return (isinstance(resource.subject, str) and bool(resource.subject)
            and type(resource.generation) is int and resource.generation >= 1
            and resource.kind in ("development", "validation")
            and isinstance(resource.source_ref, str) and resource.source_ref.startswith("refs/heads/")
            and "\0" not in resource.source_ref and isinstance(resource.head_sha, str)
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", resource.head_sha) is not None)


def _abandon_resource(effect: Effect) -> ResourceWitness | None:
    raw = effect.payload.get("resource")
    if not isinstance(raw, Mapping):
        return None
    try:
        resource = ResourceWitness(**raw)
    except TypeError:
        return None
    if (not _valid_resource(resource) or resource.subject != effect.subject
            or resource.source_ref != effect.source_ref or resource.head_sha != effect.before_sha
            or resource.source_ref == effect.target_ref
            or not isinstance(effect.payload.get("control_action"), str) or not effect.payload["control_action"]
            or type(effect.payload.get("discard_worktree")) is not bool):
        return None
    return resource


def _resource_worktree(resource: ResourceWitness) -> tuple[Path, Path, Path, str, str] | None:
    paths = _worktree_paths((resource.checkout, resource.owned_root, resource.common_git_dir))
    if paths is None:
        return None
    return (*paths, resource.source_ref, resource.head_sha)


def inspect_worktree(git: LocalGit, effect: Effect) -> EffectResult:
    """Require exact registration, Git identity, HEAD and a clean owned checkout."""
    try:
        discard = effect.operation == "abandon_worktree" and effect.payload.get("discard_worktree") is True
        resource = _abandon_resource(effect) if effect.operation == "abandon_worktree" else None
        identity = _worktree(effect)
        observed = _inspect_worktree(git, identity,
                                    check_dirty=effect.operation != "validation_cleanup" and not discard)
        if resource is not None:
            return replace(observed, raw={**observed.raw, "resource": resource_wire(resource)})
        return observed
    except KernelError as error:
        return _result("third" if error.code == "foreign_local_checkout" else "unknown", error.code)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return _result("unknown", "worktree_read_failed")


def inspect_resource(git: LocalGit, resource: ResourceWitness) -> ResourceInspection:
    """Read one original resource witness, including validation checkout dirt."""
    if not _valid_resource(resource):
        return ResourceInspection(resource, "foreign", reason="invalid_worktree_identity")
    try:
        identity = _resource_worktree(resource)
        if identity is None:
            return ResourceInspection(resource, "foreign", reason="invalid_worktree_identity")
    except KernelError as error:
        return ResourceInspection(resource, "foreign" if error.code == "foreign_local_checkout" else "unknown", reason=error.code)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return ResourceInspection(resource, "unknown", reason="worktree_read_failed")
    observed = _inspect_worktree(git, identity, check_dirty=True)
    reason = str(observed.raw["reason"])
    if observed.disposition == "prior":
        return ResourceInspection(resource, "clean", identity[-1], reason)
    if reason == "worktree_dirty":
        return ResourceInspection(resource, "dirty", identity[-1], reason)
    if observed.disposition == "target":
        return ResourceInspection(resource, "absent", reason=reason)
    return ResourceInspection(resource, "unknown" if observed.disposition == "unknown" else "foreign", reason=reason)


def _inspect_worktree(git: LocalGit, identity: tuple[Path, Path, Path, str, str] | None,
                      *, check_dirty: bool) -> EffectResult:
    try:
        if identity is None:
            return _result("third", "invalid_worktree_identity")
        checkout, _owned, common, ref, expected = identity
        actual_common = _read(git, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        if Path(actual_common).resolve() != common:
            return _result("third", "foreign_common_git_dir")
        listed = _read(git, "worktree", "list", "--porcelain", "-z").stdout
        registrations = [block.split("\0") for block in listed.split("\0\0") if block]
        registered = [fields for fields in registrations if fields[0] == f"worktree {checkout}"]
        if not checkout.exists():
            return _result("unknown", "registered_path_missing") if registered else _result("target", "worktree_absent")
        if not checkout.is_dir() or len(registered) != 1:
            return _result("third", "foreign_worktree_registration")
        fields = registered[0]
        if (f"HEAD {expected}" not in fields
                or not ("detached" in fields or f"branch {ref}" in fields)
                or any(field.startswith(("locked", "prunable")) for field in fields)):
            return _result("third", "worktree_registration_changed")
        own_common = _read(git, "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=checkout).stdout.strip()
        top = _read(git, "rev-parse", "--show-toplevel", cwd=checkout).stdout.strip()
        head = _read(git, "rev-parse", "--verify", "HEAD", cwd=checkout).stdout.strip()
        symbolic = _read(git, "symbolic-ref", "-q", "HEAD", cwd=checkout, check=False)
        if (Path(own_common).resolve() != common or Path(top).resolve() != checkout or head != expected
                or symbolic.returncode not in {0, 1}
                or (symbolic.returncode == 0 and symbolic.stdout.strip() != ref)):
            return _result("third", "worktree_identity_changed")
        if check_dirty and _read(git, "status", "--porcelain", "--untracked-files=all", cwd=checkout).stdout:
            return _result("third", "worktree_dirty")
        return _result("prior", "worktree_owned", attached=symbolic.returncode == 0)
    except (KernelError, OSError, RuntimeError, subprocess.SubprocessError):
        return _result("unknown", "worktree_read_failed")


def _signal_group(pid: int, signum: int) -> None:
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


@contextmanager
def _hold_ref(git: LocalGit, checkout: Path, ref: str, expected: str) -> Iterator[None]:
    # Prepared verification locks the original object without changing its value.
    # HEAD and its referent need separate transactions on supported Git versions.
    process = subprocess.Popen(["git", "update-ref", "--no-deref", "--stdin", "-z"],
                               cwd=checkout, env=git.env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(f"start\0verify {ref}\0{expected}\0prepare\0".encode())
        process.stdin.flush()
        deadline = monotonic() + 5.0
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while b"prepare: ok\n" not in output:
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise KernelError("cleanup_lock_timeout")
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk or len(output) + len(chunk) > 65536:
                    raise KernelError("cleanup_lock_failed")
                output.extend(chunk)
        yield
    finally:
        try:
            process.stdin.close()  # Abort; never commit the verification transaction.
        except BrokenPipeError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _signal_group(process.pid, signal.SIGKILL)
                process.wait()
        finally:
            _signal_group(process.pid, signal.SIGKILL)
            process.stdout.close()


def cleanup_worktree(git: LocalGit, effect: Effect) -> EffectResult:
    """Retain HEAD locks; only disposable Review validation may contain test outputs."""
    observed = inspect_worktree(git, effect)
    if observed.disposition != "prior":
        return observed
    identity = _worktree(effect)
    assert identity is not None
    checkout, _owned, _common, ref, expected = identity
    if "head" in observed.raw:
        expected = str(observed.raw["head"])
    _authority(git)
    # A stale authorization must propagate; lock contention is only a resource fact.
    try:
        with ExitStack() as locks:
            locks.enter_context(_hold_ref(git, checkout, "HEAD", expected))
            if observed.raw.get("attached"):
                locks.enter_context(_hold_ref(git, git.root, ref, expected))
            verified = inspect_worktree(git, effect)
            if verified.disposition != "prior":
                return verified
            if "head" in observed.raw and verified.raw.get("head") != expected:
                return _result("third", "worktree_identity_changed")
            _authority(git)
            discard = effect.operation == "abandon_worktree" and effect.payload.get("discard_worktree") is True
            options = ("--force",) if effect.operation == "validation_cleanup" or discard else ()
            git.run("worktree", "remove", *options, str(checkout), check=False)
    except KernelError as error:
        if error.code not in {"cleanup_lock_timeout", "cleanup_lock_failed"}:
            raise
        return _result("unknown", "worktree_lock_failed")
    except (OSError, subprocess.SubprocessError):
        pass
    return inspect_worktree(git, effect)
