"""Decode original graph identities from their declared relation keys."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .migration import MigrationRefused
from .model import Blocker, Issue, ParentIssue, Requirements
from .kernel import Kernel


@dataclass(frozen=True)
class DecodedGraph:
    requirements: Requirements
    observations: Mapping[int, Mapping[str, Any]]


def read_graph_requirements(db: sqlite3.Connection, *, run_id: str, snapshot_id: str,
                            version_id: str, parent_number: int, title: Any, body: Any,
                            observation: Any) -> DecodedGraph:
    scope_rows = db.execute('SELECT position,ticket_number FROM run_graph_scope_order '
                                        'WHERE run_id=? AND snapshot_id=? ORDER BY position',
                                        (run_id, snapshot_id)).fetchall()
    order = [row['ticket_number'] for row in scope_rows]
    ticket_slots = db.execute('SELECT * FROM run_graph_ticket_slots WHERE run_id=? AND snapshot_id=? '
                              'ORDER BY position', (run_id, snapshot_id)).fetchall()
    members = {row[0] for row in db.execute('SELECT ticket_number FROM run_graph_members '
                                           'WHERE run_id=? AND version_id=?', (run_id, version_id))}
    if (not isinstance(title, str) or not isinstance(body, str) or not isinstance(observation, dict)
            or not order or len(order) != len(set(order)) or set(order) != members
            or [row['position'] for row in scope_rows] != list(range(len(order)))
            or [row['position'] for row in ticket_slots] != list(range(len(order)))
            or {row['ticket_number'] for row in ticket_slots} != members
            or any(row['number_present'] != 1 for row in ticket_slots)
            or not isinstance(observation.get('tickets'), list) or len(observation['tickets']) != len(order)
            or observation.get('fields', {}).get('order_source') != 'github_sub_issues'):
        raise MigrationRefused('source_requirements')
    blocker_slots = db.execute('SELECT * FROM run_graph_blocker_slots WHERE run_id=? AND snapshot_id=? '
                               'ORDER BY ticket_position,position', (run_id, snapshot_id)).fetchall()
    edges = db.execute('SELECT * FROM run_graph_edges WHERE run_id=? AND version_id=?',
                       (run_id, version_id)).fetchall()
    edge_by_id = {row['edge_id']: row for row in edges}
    try:
        issues: dict[int, Issue] = {}
        observations: dict[int, Mapping[str, Any]] = {}
        used_edges: set[str] = set()
        for ticket_slot in ticket_slots:
            position, number = ticket_slot['position'], ticket_slot['ticket_number']
            item = observation['tickets'][position]
            matching = [slot for slot in blocker_slots if slot['ticket_position'] == position]
            if ([slot['position'] for slot in matching] != list(range(len(matching)))
                    or len(matching) != len(item['blocked_by'])):
                raise MigrationRefused('source_requirements')
            blockers = []
            for slot, value in zip(matching, item['blocked_by'], strict=True):
                edge = edge_by_id[slot['edge_id']]
                if (slot['number_present'] != 1 or edge['ticket_number'] != number
                        or edge['edge_id'] in used_edges):
                    raise MigrationRefused('source_requirements')
                used_edges.add(edge['edge_id'])
                blockers.append(Blocker(edge['blocker_number'], value['state']))
            issue = Issue(number, item['title'], item['body'], item['state'], frozenset(item['labels']), tuple(blockers))
            if item['content_revision'] != Kernel._fingerprint({'title': issue.title, 'body': issue.body}):
                raise MigrationRefused('source_revision')
            issues[number] = issue
            observations[number] = item
        if (len(edge_by_id) != len(edges) or used_edges != set(edge_by_id)
                or len(blocker_slots) != len(used_edges)):
            raise MigrationRefused('source_requirements')
        return DecodedGraph(Requirements(ParentIssue(parent_number, title, body, tuple(order), True),
                                        tuple(issues[number] for number in order)), observations)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise MigrationRefused('source_requirements') from error
