from __future__ import annotations

import hashlib
import json

from agent_run.models import DeliveryGraph


class TicketGraphDriftError(RuntimeError):
    pass


def fingerprint(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def effective_revision(
    *,
    ticket_revision: str,
    parent_revision: str,
    graph_revision: str,
) -> str:
    return fingerprint(
        {
            "ticket": ticket_revision,
            "parent": parent_revision,
            "graph": graph_revision,
        }
    )


def ticket_graph_revision(graph: DeliveryGraph) -> str:
    """Fingerprint delivery scope and dependencies, never scheduling order."""
    ticket_numbers = sorted(set(graph.parent.sub_issue_numbers))
    return fingerprint(
        {
            "tickets": ticket_numbers,
            "dependencies": {
                str(number): sorted(
                    blocker.number
                    for blocker in graph.issues[number].blocked_by
                )
                for number in ticket_numbers
                if number in graph.issues
            },
        }
    )


def effective_revision_from_graph(
    graph: DeliveryGraph, ticket_number: int
) -> str:
    graph_revision = ticket_graph_revision(graph)
    if (
        ticket_number not in graph.parent.sub_issue_numbers
        or ticket_number not in graph.issues
    ):
        raise TicketGraphDriftError(
            f"Ticket #{ticket_number} is no longer present in the live graph"
        )
    ticket = graph.issues[ticket_number]
    return effective_revision(
        ticket_revision=fingerprint(
            {"title": ticket.title, "body": ticket.body}
        ),
        parent_revision=fingerprint(
            {"title": graph.parent.title, "body": graph.parent.body}
        ),
        graph_revision=graph_revision,
    )
