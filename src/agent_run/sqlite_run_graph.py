"""Separate fixed graph membership/edges from immutable raw observations."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from agent_run.sqlite_query_rows import bounded_query
from agent_run.sqlite_run_inputs import identity, input_version_for_revision, owner
from agent_run.sqlite_work_evidence import get_evidence, put_evidence

MAX_GRAPH_TICKETS = 4096


def _number(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Invalid Run graph Ticket identity")
    return value


def _parts(value: Mapping[str, Any]) -> tuple[list[int], list[tuple[int, int]], dict[str, Any], list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    if value.keys() - {"revision", "ordered_ticket_numbers", "tickets", "order_source"}:
        raise ValueError("Unsupported Run graph input fields")
    order = value.get("ordered_ticket_numbers", [])
    tickets = value.get("tickets", {})
    if (not isinstance(order, list) or not isinstance(tickets, Mapping)
            or max(len(order), len(tickets)) > MAX_GRAPH_TICKETS):
        raise ValueError("Invalid or oversized Run graph input")
    order = [_number(number) for number in order]
    if len(set(order)) != len(order):
        raise ValueError("Duplicate Run graph scope member")
    report: dict[str, Any] = {"fields": {key: item for key, item in value.items()
                                         if key not in {"revision", "ordered_ticket_numbers", "tickets"}},
                              "revision_present": "revision" in value,
                              "order_present": "ordered_ticket_numbers" in value,
                              "tickets_present": "tickets" in value, "tickets": []}
    ticket_slots, blocker_slots, edges = [], [], set()
    for position, (key, ticket) in enumerate(tickets.items()):
        if not isinstance(key, str) or not key.isdecimal() or key != str(int(key)):
            raise ValueError("Invalid Run graph Ticket key")
        number = _number(int(key))
        if not isinstance(ticket, Mapping) or "number" in ticket and ticket["number"] != number:
            raise ValueError("Invalid Run graph Ticket observation")
        observed = {field: item for field, item in ticket.items() if field not in {"number", "blocked_by"}}
        ticket_slots.append((position, number, int("number" in ticket)))
        if "blocked_by" in ticket:
            blockers = ticket["blocked_by"]
            if not isinstance(blockers, list) or len(blockers) > MAX_GRAPH_TICKETS:
                raise ValueError("Invalid Run graph dependency observations")
            observed["blocked_by"] = []
            for index, blocker in enumerate(blockers):
                if not isinstance(blocker, Mapping):
                    raise ValueError("Invalid Run graph dependency")
                blocker_number = _number(blocker.get("number"))
                edge_id = identity((number, blocker_number))
                edges.add((number, blocker_number))
                blocker_slots.append((position, index, edge_id, 1))
                observed["blocked_by"].append({field: item for field, item in blocker.items() if field != "number"})
        report["tickets"].append(observed)
    return order, sorted(edges), report, ticket_slots, blocker_slots


def save_graph(c: sqlite3.Connection, run_id: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Run graph input must be an object")
    revision = value.get("revision")
    if revision is not None and not isinstance(revision, str):
        raise ValueError("Invalid Run graph revision")
    order, edges, report, ticket_slots, blocker_slots = _parts(value)
    version = identity((revision, sorted(order), edges))
    if revision is not None:
        previous = input_version_for_revision(c, run_id, "graph", revision)
        if previous is not None and previous != version:
            raise ValueError("One graph revision identifies different fixed structures")
    row = bounded_query(c, "SELECT revision FROM run_graph_versions WHERE run_id=? AND version_id=?", (run_id, version)).fetchone()
    if row is None:
        c.execute("INSERT INTO run_graph_versions VALUES (?,?,?)", (run_id, version, revision))
        c.executemany("INSERT INTO run_graph_members VALUES (?,?,?)", ((run_id, version, number) for number in sorted(order)))
        c.executemany("INSERT INTO run_graph_edges VALUES (?,?,?,?,?)",
                      ((run_id, version, identity(edge), *edge) for edge in edges))
    elif row != (revision,):
        raise ValueError("Immutable Run graph version was changed")
    else:
        _structure(c, run_id, version)
    evidence_id = put_evidence(c, run_id, owner(run_id), "graph.observation", report)
    snapshot = identity(value)
    old = bounded_query(c, "SELECT version_id,evidence_id FROM run_graph_snapshots WHERE run_id=? AND snapshot_id=?",
                    (run_id, snapshot)).fetchone()
    if old is not None:
        if old != (version, evidence_id) or load_graph(c, run_id, snapshot) != value:
            raise ValueError("Immutable Run graph observation was changed")
        return snapshot
    c.execute("INSERT INTO run_graph_snapshots VALUES (?,?,?,?)", (run_id, snapshot, version, evidence_id))
    c.executemany("INSERT INTO run_graph_scope_order VALUES (?,?,?,?)",
                  ((run_id, snapshot, index, number) for index, number in enumerate(order)))
    c.executemany("INSERT INTO run_graph_ticket_slots VALUES (?,?,?,?,?)", ((run_id, snapshot, *item) for item in ticket_slots))
    c.executemany("INSERT INTO run_graph_blocker_slots VALUES (?,?,?,?,?,?)", ((run_id, snapshot, *item) for item in blocker_slots))
    return snapshot


def _structure(c: sqlite3.Connection, run_id: str, version: str) -> tuple[Any, set[int], dict[str, tuple[int, int]]]:
    row = bounded_query(c, "SELECT revision FROM run_graph_versions WHERE run_id=? AND version_id=?", (run_id, version)).fetchone()
    if row is None:
        raise ValueError("Missing fixed Run graph version")
    members = [item[0] for item in bounded_query(c, "SELECT ticket_number FROM run_graph_members WHERE run_id=? AND version_id=? ORDER BY ticket_number",
                                          (run_id, version))]
    edges = {edge_id: (source, target) for edge_id, source, target in bounded_query(c, "SELECT edge_id,ticket_number,blocker_number FROM run_graph_edges WHERE run_id=? AND version_id=?", (run_id, version))}
    if (len(members) > MAX_GRAPH_TICKETS or any(type(number) is not int or number < 1 for number in members)
            or any(edge_id != identity(edge) for edge_id, edge in edges.items())
            or version != identity((row[0], members, sorted(edges.values())))):
        raise ValueError("Invalid fixed Run graph structure")
    return row[0], set(members), edges


def load_graph(c: sqlite3.Connection, run_id: str, snapshot: str) -> dict[str, Any]:
    row = bounded_query(c, "SELECT version_id,evidence_id FROM run_graph_snapshots WHERE run_id=? AND snapshot_id=?",
                    (run_id, snapshot)).fetchone()
    if row is None:
        raise ValueError("Missing immutable Run graph observation")
    revision, members, edges = _structure(c, run_id, row[0])
    report = get_evidence(c, run_id, row[1], owner_id=owner(run_id), kind="graph.observation")
    result = dict(report["fields"])
    if report["revision_present"]:
        result["revision"] = revision
    order = []
    for expected, (position, number) in enumerate(bounded_query(c, "SELECT position,ticket_number FROM run_graph_scope_order WHERE run_id=? AND snapshot_id=? ORDER BY position", (run_id, snapshot))):
        if position != expected or number not in members:
            raise ValueError("Invalid Run graph order reference")
        order.append(number)
    if set(order) != members or len(order) != len(members):
        raise ValueError("Missing Run graph scope member")
    if report["order_present"]:
        result["ordered_ticket_numbers"] = order
    tickets = {}
    for expected, (position, number, number_present) in enumerate(bounded_query(c, "SELECT position,ticket_number,number_present FROM run_graph_ticket_slots WHERE run_id=? AND snapshot_id=? ORDER BY position",
        (run_id, snapshot))):
        if position != expected or not 0 <= position < len(report["tickets"]) or number_present not in {0, 1}:
            raise ValueError("Invalid Run graph Ticket observation reference")
        ticket = report["tickets"][position]
        if number_present:
            ticket["number"] = number
        blocker_rows = bounded_query(c, "SELECT position,edge_id,number_present FROM run_graph_blocker_slots "
                                "WHERE run_id=? AND snapshot_id=? AND ticket_position=? ORDER BY position", (run_id, snapshot, position)).fetchall()
        blockers = ticket.get("blocked_by", [])
        if len(blockers) != len(blocker_rows):
            raise ValueError("Missing Run graph dependency observation")
        for index, (blocker_position, edge_id, present) in enumerate(blocker_rows):
            edge = edges.get(edge_id)
            if blocker_position != index or edge is None or edge[0] != number or present != 1:
                raise ValueError("Invalid Run graph dependency reference")
            blockers[index]["number"] = edge[1]
        tickets[str(number)] = ticket
    if len(tickets) != len(report["tickets"]):
        raise ValueError("Missing Run graph Ticket observation")
    if report["tickets_present"]:
        result["tickets"] = tickets
    if identity(result) != snapshot:
        raise ValueError("Invalid immutable Run graph observation digest")
    return result
