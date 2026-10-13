"""One bounded SQLite statement for a small, related work projection."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any
from agent_run.sqlite_job_history import CURRENT_WORK_PREDICATE

# A shared bounded relation must not be flattened into each aggregate and
# dependency lookup. Older SQLite versions retain the original valid query.
_MATERIALIZED = "MATERIALIZED " if sqlite3.sqlite_version_info >= (3, 35, 0) else ""

_NATIVE_WORK = f"""
WITH raw_core AS (
 SELECT object_path, kind, value_json FROM run_objects INDEXED BY current_work_objects WHERE run_id=:run_id AND object_path<:history_low AND {CURRENT_WORK_PREDICATE}
 UNION ALL SELECT object_path, kind, value_json FROM run_objects INDEXED BY current_work_objects WHERE run_id=:run_id AND object_path>=:history_high AND object_path<:canonical_low AND {CURRENT_WORK_PREDICATE}
 UNION ALL SELECT object_path, kind, value_json FROM run_objects INDEXED BY current_work_objects WHERE run_id=:run_id AND object_path>=:canonical_high AND {CURRENT_WORK_PREDICATE}
), core AS {_MATERIALIZED}(
 SELECT * FROM raw_core WHERE object_path NOT IN (SELECT object_path FROM run_job_history WHERE run_id=:run_id)
), active AS (
 SELECT COALESCE(json_extract(m.value,'$.ticket_number'),json_extract(m.value,'$[1].ticket_number')) AS number
 FROM core o JOIN run_materials m ON m.material_id=json_extract(o.value_json,'$')
 WHERE o.object_path='["active_ticket_job"]' AND o.kind='snapshot'
 AND m.byte_length=LENGTH(CAST(m.value AS BLOB)) AND m.byte_length<=:limit_bytes AND json_valid(m.value)
), current AS (
 SELECT COALESCE(json_extract(m.value,'$.work_subject'),json_extract(m.value,'$[1].work_subject')) AS subject,
 COALESCE(json_extract(m.value,'$.started_at'),json_extract(m.value,'$[1].started_at')) AS started
 FROM run_objects o JOIN run_materials m ON m.material_id=json_extract(o.value_json,'$')
 WHERE o.run_id=:run_id AND o.object_path=:ticket_path AND o.kind='snapshot'
 AND m.byte_length=LENGTH(CAST(m.value AS BLOB)) AND m.byte_length<=:limit_bytes AND json_valid(m.value)
), retry_owners AS (
 SELECT o.object_path, COALESCE(json_extract(m.value,'$.publication_attempts'),json_extract(m.value,'$[1].publication_attempts')) AS ordinal
 FROM core o JOIN run_materials m ON m.material_id=json_extract(o.value_json,'$')
 WHERE o.kind='snapshot' AND (o.object_path='["active_ticket_job"]' OR
 (json_extract(o.object_path,'$[0]')='ticket_jobs' AND json_array_length(o.object_path)=2))
 AND m.byte_length=LENGTH(CAST(m.value AS BLOB)) AND m.byte_length<=:limit_bytes AND json_valid(m.value)
 AND COALESCE(json_type(m.value,'$.publication_operation_retry'),json_type(m.value,'$[1].publication_operation_retry'),'null')!='null'
 AND COALESCE(json_extract(m.value,'$.pending_semantic_attempt.role'),
 json_extract(m.value,'$[1].pending_semantic_attempt.role'),json_extract(m.value,'$[1].pending_semantic_attempt[1].role'),'')!='publication'
), resumes AS (
 SELECT json_extract(o.value_json,'$') AS identity FROM core o
 CROSS JOIN run_objects s ON s.run_id=:run_id AND s.object_path=json_set(o.object_path,'$[3]','successor_invocation_started_at')
 WHERE json_extract(o.object_path,'$[0]')='resume_audit' AND json_extract(o.object_path,'$[1]')='history'
 AND json_extract(o.object_path,'$[3]')='resume_id' AND o.kind='scalar' AND s.kind='scalar' AND s.value_json!='null'
), count AS (
 SELECT COALESCE(json_extract(value_json,'$'),0) AS total FROM core WHERE object_path=:history_root
), positions AS (
 SELECT position FROM run_history WHERE run_id=:run_id AND work_subject=(SELECT subject FROM current) AND started_at=(SELECT started FROM current)
 UNION SELECT position FROM run_history WHERE run_id=:run_id AND resume_id IN (SELECT identity FROM resumes)
 UNION SELECT position FROM run_history WHERE run_id=:run_id AND detailed=1 AND :ticket_number IS NOT NULL
 AND position<MAX(COALESCE((SELECT total FROM count),0)-255,0)
), history AS (
 SELECT h.* FROM positions p CROSS JOIN run_history h WHERE h.run_id=:run_id AND h.position=p.position
), canonical AS (
 SELECT :ticket_path AS path WHERE :ticket_number IS NOT NULL
 UNION SELECT json_array('ticket_agent_invocations',CAST(number AS TEXT)) FROM active WHERE number IS NOT NULL
 UNION SELECT json_array('ticket_agent_invocations',SUBSTR(work_subject,8)) FROM history WHERE work_subject LIKE 'ticket:%'
), selected_archives AS (
 SELECT object_path FROM run_job_history WHERE run_id=:run_id AND
 ((json_extract(object_path,'$[0]')='ticket_jobs' AND json_extract(object_path,'$[1]')=:job_number) OR
 (json_extract(object_path,'$[0]')='active_ticket_job' AND :ticket_number=(SELECT number FROM active)))
), related AS {_MATERIALIZED}(
 SELECT a.* FROM retry_owners r CROSS JOIN run_job_attempts a
 WHERE a.run_id=:run_id AND a.parent_path=json_insert(r.object_path,'$[#]','semantic_attempt_history')
 AND a.parent_path NOT IN (SELECT object_path FROM selected_archives)
 AND a.role='publication' AND a.status='completed' AND a.ordinal=r.ordinal
 AND a.position=(SELECT MAX(position) FROM run_job_attempts last WHERE last.run_id=:run_id AND last.parent_path=a.parent_path
 AND last.role='publication' AND last.status='completed' AND last.ordinal=r.ordinal)
), related_objects AS (
 SELECT o.object_path,o.kind,o.value_json FROM related r CROSS JOIN run_objects o
 WHERE o.run_id=:run_id AND o.run_id=r.run_id AND o.object_path=r.object_path
), objects AS {_MATERIALIZED}(
 SELECT * FROM core
 UNION ALL SELECT o.object_path,o.kind,o.value_json FROM selected_archives j CROSS JOIN run_objects o
 WHERE o.run_id=:run_id AND o.object_path=j.object_path
 UNION ALL SELECT o.object_path,o.kind,o.value_json FROM selected_archives j CROSS JOIN run_objects o
 WHERE o.run_id=:run_id AND o.object_path>=SUBSTR(j.object_path,1,LENGTH(j.object_path)-1)||','
 AND o.object_path<SUBSTR(j.object_path,1,LENGTH(j.object_path)-1)||'-'
 UNION ALL SELECT o.object_path,o.kind,o.value_json FROM positions p CROSS JOIN run_objects o
 WHERE o.run_id=:run_id AND o.object_path=json_array('agent_invocation_history',p.position)
 UNION ALL SELECT o.object_path,o.kind,o.value_json FROM canonical c CROSS JOIN run_objects o
 WHERE o.run_id=:run_id AND o.object_path=c.path
), all_objects AS (
 SELECT * FROM objects UNION ALL SELECT * FROM related_objects
), refs AS {_MATERIALIZED}(
 SELECT r.object_path,r.material_id FROM all_objects o CROSS JOIN run_object_material_refs r
 WHERE r.run_id=:run_id AND r.object_path=o.object_path AND o.kind IN ('material','snapshot')
), materials AS {_MATERIALIZED}(
 -- Drive exact references first: older planners otherwise scan all Runs' materials.
 SELECT m.material_id,
 CASE WHEN LENGTH(CAST(m.value AS BLOB))<=:aggregate_bytes THEN m.value END AS value,
 m.byte_length,LENGTH(CAST(m.value AS BLOB)) AS actual_length,
 rr.material_id AS linked FROM (SELECT DISTINCT material_id FROM refs) ids
 CROSS JOIN run_materials m ON m.material_id=ids.material_id
 LEFT JOIN run_material_refs rr ON rr.run_id=:run_id AND rr.material_id=m.material_id
), sizes AS (
 SELECT (SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+LENGTH(CAST(value_json AS BLOB))),0) FROM all_objects)
 +(SELECT COALESCE(SUM(actual_length),0) FROM materials)
 +(SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+LENGTH(material_id)+32),0) FROM refs) AS physical
)
SELECT (SELECT logical_bytes FROM run_sizes WHERE run_id=:run_id),
(SELECT canonical_count FROM run_sizes WHERE run_id=:run_id), COALESCE((SELECT total FROM count),0), physical,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(object_path,kind,value_json)) FROM objects) END,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(material_id,value,byte_length,actual_length,linked)) FROM materials) END,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(object_path,material_id)) FROM refs) END,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(run_id,position,ticket_number,work_subject,started_at,resume_id,logical_bytes,detailed)) FROM history) END,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(object_path,kind,value_json)) FROM related_objects) END,
CASE WHEN physical<=:aggregate_bytes THEN (SELECT json_group_array(json_array(object_path,parent_path,position,role,status,ordinal)) FROM related) END,
CASE WHEN (SELECT COALESCE(SUM(LENGTH(CAST(object_path AS BLOB))+64),0) FROM run_job_history WHERE run_id=:run_id)<=:aggregate_bytes THEN
 (SELECT json_group_array(json_array(j.object_path,j.logical_bytes,j.item_count,o.kind)) FROM run_job_history j
 LEFT JOIN run_objects o ON o.run_id=j.run_id AND o.object_path=j.object_path WHERE j.run_id=:run_id) END
FROM sizes
"""



def read_work(connection: sqlite3.Connection, parameters: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Return raw snapshot facts, or use the caller's non-JSON fallback."""
    try:
        row: tuple[Any, ...] | None = connection.execute(_NATIVE_WORK, parameters).fetchone()
        return row
    except sqlite3.OperationalError as error:
        if "no such function" not in str(error):
            raise
        return None
