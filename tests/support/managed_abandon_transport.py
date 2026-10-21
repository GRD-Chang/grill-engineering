"""Add committed Issue timestamps to the original managed fake-GitHub transport."""
import json
from pathlib import Path


def install_currentness(directory: Path, state: Path) -> None:
    """Call after _control_environment, before any delivery or GitHub operation.

    This models the external service, never Kernel records. Each accepted write
    commits its timestamp with its actual comment/event; GET uses those stored
    values. Lost acknowledgements therefore retain the committed timestamp too.
    """
    document = json.loads(state.read_text())
    assert document["prs"] == [] and document["comments"] == [] and document["events"] == []
    document["operation_clock"] = 0
    for issue in (document["source_graph"]["parent"], *document["source_graph"]["issues"].values()):
        issue["updated_at"] = "2026-10-21T10:00:00Z"
    state.write_text(json.dumps(document))
    executable = directory / "control-bin" / "gh"
    program = executable.read_text()

    def replace(original: str, changed: str) -> None:
        nonlocal program
        assert program.count(original) == 1, original
        program = program.replace(original, changed)

    replace("d=json.loads(state.read_text())", """d=json.loads(state.read_text())
from datetime import datetime, timedelta, timezone
def tick():
    d['operation_clock'] += 1
    value = datetime(2026,10,21,10,0,0,tzinfo=timezone.utc) + timedelta(seconds=d['operation_clock'])
    return value.isoformat().replace('+00:00','Z')""")
    replace("'merge_commit_sha':None,", "'merge_commit_sha':None,'updated_at':tick(),")
    replace("'created_at':'2026-10-21T10:00:00Z'", "'created_at':tick()")
    replace("d['comments'].append(comment);comments.append(comment);result=comment",
            "d['comments'].append(comment);comments.append(comment);resource['updated_at']=comment['created_at'];result=comment")
    replace("'created_at':'2026-10-21T10:01:00Z'", "'created_at':tick()")
    replace(";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1])",
            ";d.setdefault('events_by_issue',{}).setdefault(str(number),[]).append(d['events'][-1]);pr['updated_at']=d['events'][-1]['created_at']")
    replace("at='2026-10-21T10:01:00Z' if event=='closed' else '2026-10-21T10:02:00Z'", "at=tick()")
    replace("d['events'].append(observed);events.append(observed);result={'state':body['state']}",
            "d['events'].append(observed);events.append(observed);resource['updated_at']=at;result={'state':body['state']}")
    replace("elif method=='PATCH':\n        resource['state']=body['state'].upper()", """elif method=='PATCH':
        if 'state' not in body:
            assert body and set(body) <= {'title','body'}
            resource.update(body)
            resource['updated_at']=tick()
            state.write_text(json.dumps(d))
            print(json.dumps(resource))
            raise SystemExit(0)
        resource['state']=body['state'].upper()""")
    replace("'state':resource.get('state','OPEN').lower()}",
            "'state':resource.get('state','OPEN').lower(),'updated_at':resource['updated_at']}")
    executable.write_text(program)
