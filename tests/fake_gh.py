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

A pull request's "baseChanges" are its timeline's base changes, as GraphQL
gives them. Changing its base adds one, a "previousRefName" and a
"createdAt" from the "clock" key, a count of the base changes so far, as a
timestamp. A merged pull request's "mergedAt" is earlier than any of them
unless it says otherwise.
"""

import json
import os
import re
import sys
from typing import Any

PR_FIELDS = (
    "number",
    "state",
    "isDraft",
    "isCrossRepository",
    "headRefOid",
    "baseRefName",
    "mergeCommit",
    "mergedAt",
)


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
        if "pullRequests(baseRefName: $base, states: MERGED" in query:
            base = opt_value(args, "base")
            nodes = [{"number": p["number"]} for p in prs if p["baseRefName"] == base and p["state"] == "MERGED"]
            return json.dumps({"data": {"repository": {"pullRequests": {"nodes": nodes[:1]}}}})
        if "search(" in query:
            return open_pull_requests(opt_value(args, "q"), prs)
        if "pullRequest(number: $number)" in query:
            return pull_request_author(int(opt_value(args, "number")), prs)
        if "pullRequest(number:" in query:
            if " stack {" in query and "stacks" not in state and state.get("no_stack_field"):
                raise GhError("gh: Field 'stack' doesn't exist on type 'PullRequest'")
            return pull_requests_by_number(query, state)
        return pull_requests_by_head(args, query, prs)
    path = next((a for a in args if "/stacks" in a), None)
    if args[0] == "api" and path is not None:
        return stacks_api(path, state)
    if args[:2] == ["pr", "create"]:
        return create_pull_request(args, prs)
    if args[:2] == ["pr", "edit"]:
        edit_pull_request(args, state)
        return None
    sys.exit(f"fake gh: unexpected {args}")


def open_pull_requests(q: str, prs: list[dict[str, Any]]) -> str:
    """The open pull requests of the author the search q names ("@me" is
    "me", the gh user), all on one page."""
    author = re.search(r"author:(\S+)", q)
    login = "me" if author is None or author[1] == "@me" else author[1]
    theirs = [p for p in prs if p["state"] == "OPEN" and p.get("author", "me") == login]
    nodes = [
        {**{k: p[k] for k in ("number", "headRefName", "baseRefName", "isCrossRepository")}, "author": {"login": login}}
        for p in theirs
    ]
    search = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
    repository = {"defaultBranchRef": {"name": "main"}}
    return json.dumps({"data": {"viewer": {"login": "me"}, "repository": repository, "search": search}})


def pull_request_author(number: int, prs: list[dict[str, Any]]) -> str:
    """The state and author of pull request number, or GraphQL's error if
    there's none."""
    pr = next((p for p in prs if p["number"] == number), None)
    if pr is None:
        raise GhError(f"gh: Could not resolve to a PullRequest with the number of {number}.")
    node = {"state": pr["state"], "author": {"login": pr.get("author", "me")}}
    return json.dumps({"data": {"repository": {"pullRequest": node}}})


def pull_requests_by_number(query: str, state: dict[str, Any]) -> str:
    """The fields query asks for of the pull requests it names by number. Of
    a stack, all of them, whichever query asks for."""
    prs: list[dict[str, Any]] = state["prs"]
    by_alias: dict[str, Any] = {}
    for alias, number, fields in re.findall(
        r"(\w+): pullRequest\(number: (\d+)\) \{ (.*?) \}(?= \w+: pullRequest| \} \}$)", query
    ):
        pr = next((p for p in prs if p["number"] == int(number)), None)
        by_alias[alias] = None if pr is None else {f: field_of(pr, f, state) for f in top_level(fields)}
    return json.dumps({"data": {"repository": by_alias}})


def top_level(fields: str) -> list[str]:
    """The names of the fields in fields, without their arguments or
    subfields."""
    depth = 0
    flat = ""
    for c in fields:
        depth += c in "({["
        if depth == 0:
            flat += c
        depth -= c in ")}]"
    return flat.split()


def field_of(pr: dict[str, Any], name: str, state: dict[str, Any]) -> object:
    """pr's field name, as GraphQL gives it."""
    if name == "stack":
        return stack_of(pr["number"], state)
    if name == "timelineItems":
        return {"nodes": pr.get("baseChanges", [])}
    return pr.get(name, "")


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


def stacks_api(path: str, state: dict[str, Any]) -> str | None:
    """Handle a call to the stacks REST API's path."""
    if "stacks" not in state:
        raise GhError("gh: Not Found (HTTP 404)")
    if "?pull_request=" in path:
        return stacks_of(int(path.rpartition("=")[2]), state)
    if path.endswith("/unstack"):
        unstack(int(path.split("/")[-2]), state)
        return None
    return link_stack(path, json.loads(sys.stdin.read())["pull_requests"], state)


def stacks_of(number: int, state: dict[str, Any]) -> str:
    """The stacks pull request number is in, as the REST API lists them."""
    stacks = [stack for stack in state["stacks"] if number in stack["prs"]]
    return json.dumps(
        [{"number": stack["number"], "pull_requests": [{"number": n} for n in stack["prs"]]} for stack in stacks]
    )


def unstack(number: int, state: dict[str, Any]) -> None:
    """Take the open pull requests out of stack number, dropping it if that
    leaves none."""
    stack = next(stack for stack in state["stacks"] if stack["number"] == number)
    merged = {p["number"] for p in state["prs"] if p["state"] == "MERGED"}
    stack["prs"] = [n for n in stack["prs"] if n in merged]
    state["stacks"] = [stack for stack in state["stacks"] if stack["prs"]]


def link_stack(path: str, numbers: list[int], state: dict[str, Any]) -> str:
    """Make a stack of numbers (POST .../stacks), or add them to the top of
    one (POST .../stacks/<number>/add), checking what GitHub would."""
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
            (
                p
                for p in prs
                if p["headRefName"] == opt_value(args, variable)
                and (
                    f"{alias}: pullRequests(headRefName: ${variable}, states: MERGED" not in query
                    or p["state"] == "MERGED"
                )
            ),
            key=lambda p: p["number"],
            reverse=True,
        )
        if "pageInfo { hasNextPage endCursor }" in query:
            start = int(next((a.removeprefix("after=") for a in args if a.startswith("after=")), "0"))
            end = start + 10
            found[alias] = {
                "nodes": [
                    {"isDraft": False, "mergedAt": "", **{k: p[k] for k in PR_FIELDS if k in p}}
                    for p in matching[start:end]
                ],
                "pageInfo": {"hasNextPage": end < len(matching), "endCursor": str(end)},
            }
        else:
            found[alias] = {
                "nodes": [
                    {"isDraft": False, "mergedAt": "", **{k: p[k] for k in PR_FIELDS if k in p}} for p in matching
                ]
            }
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


def edit_pull_request(args: list[str], state: dict[str, Any]) -> None:
    pr = next(p for p in state["prs"] if p["number"] == int(args[2]))
    if "--base" in args:
        if any(pr["number"] in stack["prs"] for stack in state.get("stacks", [])):
            raise GhError(
                "GraphQL: Cannot change the base branch because the pull request is part of a stack. "
                "(updatePullRequest)"
            )
        state["clock"] = state.get("clock", 0) + 1
        pr.setdefault("baseChanges", []).append(
            {"previousRefName": pr["baseRefName"], "createdAt": timestamp(state["clock"])}
        )
        pr["baseRefName"] = opt(args, "--base")
    if "--body-file" in args:
        pr["body"] = sys.stdin.read()


def timestamp(tick: int) -> str:
    """A GitHub timestamp, tick seconds into the fake's day."""
    return f"2026-01-01T{tick // 3600:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}Z"


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
