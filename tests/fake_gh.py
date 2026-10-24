#!/usr/bin/env python3
"""Stands in for the gh CLI in tests.

Keeps pull requests and the calls it got in the JSON file at $FAKE_GH_STATE,
and fails for commands whose first argument is listed in its "fail" key, and
for opening pull requests of the branches in its "fail_heads" key. A pull
request's "author" is "me", the gh user, unless it says otherwise.
"""

import json
import os
import re
import sys
from typing import Any

PR_FIELDS = ("number", "state", "isDraft", "isCrossRepository", "headRefOid", "baseRefName", "mergeCommit")


def opt(args: list[str], name: str) -> str:
    """The value of the `name value` option in args."""
    return args[args.index(name) + 1]


def opt_value(args: list[str], key: str) -> str:
    """The value of the -f key=value argument in args."""
    return next(a for a in args if a.startswith(f"{key}=")).removeprefix(f"{key}=")


def respond(args: list[str], prs: list[dict[str, Any]]) -> str | None:
    """Handle the gh command args, updating prs. Return what gh would print, or None."""
    if args[:2] == ["api", "graphql"]:
        query = opt_value(args, "query")
        if "search(" in query:
            return my_open_pull_requests(prs)
        if "pullRequest(number:" in query:
            return pull_requests_by_number(query, prs)
        return pull_requests_by_head(args, query, prs)
    if args[:2] == ["pr", "create"]:
        return create_pull_request(args, prs)
    if args[:2] == ["pr", "edit"]:
        edit_pull_request(args, prs)
        return None
    sys.exit(f"fake gh: unexpected {args}")


def my_open_pull_requests(prs: list[dict[str, Any]]) -> str:
    """The gh user's open pull requests, all on one page."""
    mine = [p for p in prs if p["state"] == "OPEN" and p.get("author", "me") == "me"]
    nodes = [{k: p[k] for k in ("number", "headRefName", "baseRefName", "isCrossRepository")} for p in mine]
    return json.dumps({"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}})


def pull_requests_by_number(query: str, prs: list[dict[str, Any]]) -> str:
    """The fields query asks for of the pull requests it names by number."""
    by_alias: dict[str, Any] = {}
    for alias, number, fields in re.findall(r"(\w+): pullRequest\(number: (\d+)\) \{ ([^}]*) \}", query):
        pr = next((p for p in prs if p["number"] == int(number)), None)
        by_alias[alias] = None if pr is None else {f: pr.get(f, "") for f in fields.split()}
    return json.dumps({"data": {"repository": by_alias}})


def pull_requests_by_head(args: list[str], query: str, prs: list[dict[str, Any]]) -> str:
    """The pull requests of the branches query names, and the default branch."""
    found: dict[str, Any] = {"defaultBranchRef": {"name": "main"}}
    for alias, variable in re.findall(r"(\w+): pullRequests\(headRefName: \$(\w+)", query):
        # Newest first, taking higher numbers as newer.
        matching = sorted(
            (p for p in prs if p["headRefName"] == opt_value(args, variable)), key=lambda p: p["number"], reverse=True
        )
        found[alias] = {"nodes": [{"isDraft": False, **{k: p[k] for k in PR_FIELDS if k in p}} for p in matching]}
    return json.dumps({"data": {"repository": found}})


def create_pull_request(args: list[str], prs: list[dict[str, Any]]) -> str:
    number = max((p["number"] for p in prs), default=0) + 1
    prs.append(
        {
            "number": number,
            "state": "OPEN",
            "isCrossRepository": False,
            "headRefName": opt(args, "--head"),
            "headRefOid": "",
            "baseRefName": opt(args, "--base"),
            "mergeCommit": None,
            "title": opt(args, "--title"),
            "body": sys.stdin.read(),
            "isDraft": "--draft" in args,
        }
    )
    return f"https://github.com/acme/widgets/pull/{number}"


def edit_pull_request(args: list[str], prs: list[dict[str, Any]]) -> None:
    pr = next(p for p in prs if p["number"] == int(args[2]))
    if "--base" in args:
        pr["baseRefName"] = opt(args, "--base")
    if "--body-file" in args:
        pr["body"] = sys.stdin.read()


def main() -> None:
    path = os.environ["FAKE_GH_STATE"]
    with open(path) as f:
        state = json.load(f)
    args = sys.argv[1:]
    state["calls"].append(args)
    failing = args[0] in state["fail"] or (
        args[:2] == ["pr", "create"] and args[args.index("--head") + 1] in state.get("fail_heads", [])
    )
    out = None if failing else respond(args, state["prs"])
    with open(path, "w") as f:
        json.dump(state, f)
    if failing:
        sys.exit("fake gh: failing as asked")
    if out is not None:
        print(out)


if __name__ == "__main__":
    main()
