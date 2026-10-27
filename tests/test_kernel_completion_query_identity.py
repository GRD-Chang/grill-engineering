"""Notification query commands select their exact Run, never a numeric Parent."""
import json
import shlex

import pytest
from markdown_it import MarkdownIt

from agent_run.adapters.workspace import ManagedWorkspace
from agent_run.cli.main import main
from agent_run.core.kernel import Kernel
from agent_run.model import Commit, Delivery, DeliveryKind, ParentIssue, Requirements, RunSpec
from agent_run.notifications.event_projection import completion_context, event_state
from agent_run.notifications.cards import card
from agent_run.notifications.envelope import event_envelope
from agent_run.queries.read_facts import read_boundaries
from support.offline import fixture_inputs


def _create_task(workspace, identity, parent, language="zh"):
    delivery = Delivery(f"parent:{parent}", DeliveryKind.PARENT_ONLY, parent, "requirement",
        Commit("base", "tree"), "refs/heads/source", "refs/heads/main")
    spec = RunSpec(identity, workspace.repository, parent, "parent", "graph", (delivery,),
                   repository_root=str(workspace.repository_root), language=language, inputs=fixture_inputs())
    return Kernel.create_task(workspace.state_root / "kernel.sqlite3", spec,
                              requirements=Requirements(ParentIssue(parent, "Parent", "Scope"), ()))


@pytest.mark.parametrize("run_id", ["parent-one", "a name; keep literal", "--literal-run"])
def test_completion_query_reaches_exact_run_among_numeric_and_other_parent_tasks(tmp_path, capsys, run_id):
    workspace = ManagedWorkspace.for_repository("example/project")
    workspace.repository_root.mkdir(parents=True)

    wanted = _create_task(workspace, run_id, 1)
    other = _create_task(workspace, "1", 7)
    with wanted.storage.transaction(write=False) as tx:
        event = completion_context(read_boundaries(tx, 0.0), event_state(tx))
    command = shlex.split(event["status_query"])
    assert command[0:2] == ["agent-run", "status"]
    # --json is inserted before the literal selector delimiter, if present.
    assert main([command[1], "--json", *command[2:]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["run_id"] == run_id
    assert result["parent"]["number"] == 1
    assert result["repository"] == workspace.repository
    assert other.storage.path != wanted.storage.path


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("stopped", [False, True], ids=["status", "stopped-guidance"])
@pytest.mark.parametrize("run_id", ["a  b", "a 'quote'; literal", "--literal-run", "parent-one",
                                   "a ``` [literal](not-a-link) <tag>", "x" * 240])
def test_displayed_completion_command_queries_the_exact_run(tmp_path, capsys, language, stopped, run_id):
    workspace = ManagedWorkspace.for_repository("example/project")
    workspace.repository_root.mkdir(parents=True)
    wanted = _create_task(workspace, run_id, 1, language)
    _create_task(workspace, "1", 7, language)
    _create_task(workspace, "a b", 8, language)
    if stopped:
        assert wanted.control("stop", "operator-stop") == "applied"
    with wanted.storage.transaction(write=False) as tx:
        state = event_state(tx)
        context = completion_context(read_boundaries(tx, 0.0), state)
    event = event_envelope(state, "ticket_completed", "original-completion", "Completed",
                           summary="Original completion summary. " * 12, **context)
    if stopped:
        assert event["next_step"] == event["status_query"]

    # Read the final card's Markdown as displayed/copyable text, not the
    # projection. Backslash escapes and fenced code have different semantics.
    commands, code, history = [], [], []
    for element in card(event)["body"]["elements"]:
        if element["tag"] != "markdown":
            continue
        for token in MarkdownIt().parse(element["content"]):
            if token.type == "fence":
                if token.content.startswith("agent-run status "):
                    code.append(token.content.removesuffix("\n"))
                if token.content.startswith("agent-run history "):
                    history.append(token.content.removesuffix("\n"))
                visible = token.content
            elif token.type == "inline":
                visible = "".join("\n" if child.type in {"softbreak", "hardbreak"} else child.content
                                  for child in token.children or [])
            else:
                continue
            for line in visible.splitlines():
                if "agent-run status " in line:
                    commands.append(line[line.index("agent-run status "):])
    assert len(commands) == 1
    for displayed in commands:
        argv = shlex.split(displayed)
        assert main([argv[1], "--json", *argv[2:]]) == 0
        actual = json.loads(capsys.readouterr().out)
        assert actual["run_id"] == run_id and actual["parent"]["number"] == 1
        assert actual["repository"] == workspace.repository
    assert code == commands  # Commands retain literal whitespace and markup.
    assert len(history) == 1
    for displayed in history:
        argv = shlex.split(displayed)
        assert main([argv[1], "--json", *argv[2:]]) == 0
        actual = json.loads(capsys.readouterr().out)
        assert actual["run_id"] == run_id and actual["parent"]["number"] == 1
    if len(run_id) == 240:
        assert len(commands[0]) > 260 and history


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("unsafe", ["a\x1b[2Jb", "a\u202eb", "a\n```\n[tag](https://example.invalid)", "x" * 40000])
def test_card_omits_unsafe_or_oversized_guidance_instead_of_altering_a_command(language, unsafe):
    event = event_envelope({"run_id": "safe", "repository": "example/project", "language": language},
        "ticket_completed", "completion", "Completed", status_query=unsafe, next_step=unsafe,
        summary="Ordinary  summary\ntext")
    output = card(event)
    markdown = "\n".join(element["content"] for element in output["body"]["elements"]
                         if element["tag"] == "markdown")
    assert all(token.content.startswith("agent-run history ") for token in MarkdownIt().parse(markdown)
               if token.type == "fence")
    assert unsafe not in markdown and "\x1b" not in markdown and "\u202e" not in markdown
    assert ("无法在卡片" if language == "zh" else "in this card" if len(unsafe) > 4096 else "no copyable command") in markdown
    assert "Ordinary summary text" in markdown
    assert len(json.dumps(output, ensure_ascii=False).encode()) < 30000


def test_card_shares_the_literal_guidance_budget_without_partial_commands():
    command = "agent-run status --repo example/project -- " + "x" * 2000
    event = event_envelope({"run_id": "safe", "repository": "example/project", "language": "en"},
        "ticket_completed", "completion", "Completed", status_query=command, next_step=command + "y",
        completion_actions=[{"issue": 1, "scope": "local", "reasons": ["Original blocker"],
                             "next_action": command + "z"}])
    output = card(event)
    markdown = "\n".join(element["content"] for element in output["body"]["elements"]
                         if element["tag"] == "markdown")
    commands = [token.content.removesuffix("\n") for token in MarkdownIt().parse(markdown)
                if token.type == "fence"]
    assert commands == [command + "z", event["query"]]
    assert markdown.count("Guidance is too long to display in full") == 2
    assert "…" not in "".join(commands)
    assert len(json.dumps(output, ensure_ascii=False).encode()) < 30000


@pytest.mark.parametrize("language", ["zh", "en"])
def test_oversized_card_rejects_without_silently_dropping_blockers(language):
    event = event_envelope({"run_id": "safe", "repository": "example/project", "language": language},
        "ticket_completed", "completion", "Completed",
        completion_actions=[{"issue": number, "scope": "local", "reasons": ["Original blocker " * 20],
                             "next_action": "agent-run status --repo example/project -- safe"}
                            for number in range(300)])
    before = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with pytest.raises(ValueError, match="未发送" if language == "zh" else "not sent"):
        card(event)
    assert json.dumps(event, ensure_ascii=False, sort_keys=True) == before
