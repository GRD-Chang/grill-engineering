"""Synthetic comment API objects for Status tests and their real child processes."""
from functools import partial
from pathlib import Path

from agent_run.contracts.status_comment import StatusProjection, StatusTarget
from agent_run.core.kernel import Kernel
from agent_run.core.operation_contract import OperationBinding
from agent_run.model import CreationInputs
from agent_run.notifications.status_comment import StatusComments
from agent_run.queries.status_comment import project_status


def core_status_comments(kernel: Kernel) -> StatusComments:
    """Wire real read-only Core facts for the retained integration stories."""
    return StatusComments(kernel.storage.path.parent,
        read_projection=partial(project_status, kernel.storage.path, storage=kernel.storage),
        read_binding=kernel.operation_binding)


def display_inputs(root: Path) -> tuple[StatusProjection, OperationBinding]:
    """Recorded display inputs, without claiming a Core publication happened."""
    target = StatusTarget('example/project', 'frozen-run', 1, 'parent:1', 1, 1, 7,
        'offline-publisher', 'publication', 'receipt', 'refs/heads/work', 'refs/heads/main')
    inputs = CreationInputs(profiles={}, prompt_resources={}, worker_identity={},
        runner_identity={'source': 'host'}, notifications={}, provenance={})
    return StatusProjection(target, 'original'), OperationBinding(root, target.repository, 1, inputs)


class CommentsAPI:
    """Independent remote objects; successful writes may lose their response."""
    def __init__(self, target):
        self.target = target
        self.comments = []
        self.commands = []
        self.reads = 0
        self.lose = False
        self.hidden = False
        self.before_write = lambda: None

    def object(self, endpoint):
        self.reads += 1
        if endpoint == "user":
            return {"login": "offline-publisher"}
        assert endpoint == f"repos/{self.target.repository}/pulls/{self.target.pr_number}"
        return {"number": self.target.pr_number, "user": {"login": "offline-publisher"},
                "head": {"ref": self.target.source_ref.removeprefix("refs/heads/"),
                         "repo": {"full_name": self.target.repository}},
                "base": {"ref": self.target.target_ref.removeprefix("refs/heads/"),
                         "repo": {"full_name": self.target.repository}}}

    def list(self, endpoint):
        self.reads += 1
        assert endpoint == f"repos/{self.target.repository}/issues/{self.target.pr_number}/comments?per_page=100"
        return [] if self.hidden else [dict(row) for row in self.comments]

    def write(self, endpoint, method, body):
        import subprocess
        self.before_write()
        self.commands.append((endpoint, method, dict(body)))
        if method == "POST":
            assert endpoint == f"repos/{self.target.repository}/issues/{self.target.pr_number}/comments"
            self.comments.append({"id": 100 + len(self.comments), "user": {"login": "offline-publisher"},
                                  "body": body["body"],
                                  "issue_url": f"https://api.github.com/repos/{self.target.repository}/issues/{self.target.pr_number}"})
        else:
            assert method == "PATCH"
            row, = [row for row in self.comments if endpoint == f"repos/{self.target.repository}/issues/comments/{row['id']}"]
            row["body"] = body["body"]
        if self.lose:
            raise OSError("lost reply; private diagnostic must not enter journal")
        return subprocess.CompletedProcess([], 0, "", "")

