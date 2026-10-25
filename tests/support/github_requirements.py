"""Normal GraphQL source facts shared by isolated gh executable fixtures."""
from copy import deepcopy


def source_issue(value, repository="example/project", parent_number=1, *, totals=None):
    issue = deepcopy(value)
    repo = {"id": "repository-" + repository, "nameWithOwner": repository}

    def identity(number):
        return {"id": f"issue-node-{number}", "number": number, "repository": repo}

    issue = {**identity(issue["number"]), **issue}
    if "subIssues" not in issue:
        issue.setdefault("parent", identity(parent_number))
    for field in ("subIssues", "labels", "blockedBy"):
        if not isinstance(issue.get(field), dict):
            continue
        connection = issue[field]
        connection.setdefault("totalCount", (totals or {}).get(field, len(connection["nodes"])))
        for index, node in enumerate(connection["nodes"]):
            if field == "labels":
                connection["nodes"][index] = {"id": "label-" + node["name"], **node}
            else:
                member = {**identity(node["number"]), **node}
                if field == "subIssues":
                    member.setdefault("parent", identity(issue["number"]))
                connection["nodes"][index] = member
    return issue


def source_response(issue, repository="example/project"):
    return {"data": {"repository": {"id": "repository-" + repository,
            "nameWithOwner": repository, "issue": issue}}}
