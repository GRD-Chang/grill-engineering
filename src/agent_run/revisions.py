from __future__ import annotations

import hashlib
import json

from agent_run.models import DeliveryGraph


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


def effective_revision_from_graph(
    graph: DeliveryGraph, ticket_number: int
) -> str:
    order = list(graph.parent.sub_issue_numbers)
    if not graph.parent.sub_issue_order_reliable:
        order.sort()
    graph_revision = fingerprint(
        {
            "tickets": order,
            "dependencies": {
                str(number): sorted(
                    blocker.number
                    for blocker in graph.issues[number].blocked_by
                )
                for number in order
                if number in graph.issues
            },
        }
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
