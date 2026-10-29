#!/usr/bin/env python3
"""Stands in for the gh CLI in tests.

Keeps pull requests and the calls it got in the JSON file at $FAKE_GH_STATE,
and fails for commands whose first argument is listed in its "fail" key, and
for opening pull requests of the branches in its "fail_heads" key. A pull
request's "author" is "me", the gh user, unless it says otherwise.

GitHub's stacks of pull requests are in its "stacks" key, each a "number" and
the "prs" in it, bottom first. Without a "stacks" key, the repository doesn't
have them, so the stacks API is Not Found, and with a true "no_stack_field"
key, GraphQL doesn't even know pull requests' stack field, like GitHub
Enterprise without them.
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


class GhError(Exception):
    """gh fails, printing the message."""


def respond(args: list[str], state: dict[str, Any]) -> str | None:
    """Handle the gh command args, updating state. Return what gh would print, or None."""
    prs: list[dict[str, Any]] = state["prs"]
    if args[:2] == ["api", "graphql"]:
        query = opt_value(args, "query")
        if "search(" in query:
            return my_open_pull_requests(prs)
        if "pullRequest(number:" in query:
            if " stack {" in query and "stacks" not in state and state.get("no_stack_field"):
                raise GhError("gh: Field 'stack' doesn't exist on type 'PullRequest'")
            return pull_requests_by_number(query, state)
        return pull_requests_by_head(args, query, prs)
    path = next((a for a in args if "/stacks" in a), None)
    if args[0] == "api" and path is not None:
        return link_stack(path, json.loads(sys.stdin.read())["pull_requests"], state)
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


def pull_requests_by_number(query: str, state: dict[str, Any]) -> str:
    """The fields query asks for of the pull requests it names by number. Of
    a stack, all of them, whichever query asks for."""
    prs: list[dict[str, Any]] = state["prs"]
    by_alias: dict[str, Any] = {}
    for alias, number, fields in re.findall(
        r"(\w+): pullRequest\(number: (\d+)\) \{ (.*?) \}(?= \w+: pullRequest| \} \}$)", query
    ):
        pr = next((p for p in prs if p["number"] == int(number)), None)
        names = re.sub(r"\{.*\}", "", fields).split()
        by_alias[alias] = (
            None if pr is None else {f: stack_of(pr["number"], state) if f == "stack" else pr.get(f, "") for f in names}
        )
    return json.dumps({"data": {"repository": by_alias}})


def stack_of(number: int, state: dict[str, Any]) -> object:
    """The stack that pull request number is in, as GraphQL gives it, or None."""
    for stack in state.get("stacks", []):
        if number in stack["prs"]:
            states = {p["number"]: p["state"] for p in state["prs"]}
            nodes = [
                {"position": i + 1, "pullRequest": {"number": n, "state": states[n]}}
                for i, n in enumerate(stack["prs"])
            ]
            return {"number": stack["number"], "entries": {"nodes": nodes}}
    return None


def link_stack(path: str, numbers: list[int], state: dict[str, Any]) -> str:
    """Make a stack of numbers (POST .../stacks), or add them to the top of
    one (POST .../stacks/<number>/add), checking what GitHub would."""
    if "stacks" not in state:
        raise GhError("gh: Not Found (HTTP 404)")
    if any(n in stack["prs"] for stack in state["stacks"] for n in numbers):
        raise GhError("gh: Validation Failed (HTTP 422): a pull request is already in a stack")
    match = re.search(r"/stacks/(\d+)/add$", path)
    if match is None:
        if len(numbers) < 2:
            raise GhError("gh: Validation Failed (HTTP 422): a stack needs two pull requests")
        state["stacks"].append({"number": 1000 + len(state["stacks"]), "prs": numbers})
    else:
        next(stack for stack in state["stacks"] if stack["number"] == int(match.group(1)))["prs"].extend(numbers)
    return json.dumps({"pull_requests": [{"number": n} for n in numbers]})


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
    error = "fake gh: failing as asked" if failing else None
    out = None
    if not failing:
        try:
            out = respond(args, state)
        except GhError as e:
            error = str(e)
    with open(path, "w") as f:
        json.dump(state, f)
    if error is not None:
        sys.exit(error)
    if out is not None:
        print(out)


if __name__ == "__main__":
    main()
