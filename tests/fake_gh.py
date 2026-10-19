#!/usr/bin/env python3
"""Stands in for the gh CLI in tests.

Keeps pull requests and the calls it got in the JSON file at $FAKE_GH_STATE,
and fails for commands whose first argument is listed in its "fail" key. A pull
request's "author" is "me", the gh user, unless it says otherwise.
"""

import json
import os
import re
import sys
from typing import Any

PR_FIELDS = ("number", "state", "isCrossRepository", "headRefOid", "baseRefName", "mergeCommit")


def respond(args: list[str], prs: list[dict[str, Any]]) -> str | None:
    """Handle the gh command args, updating prs. Return what gh would print, or None."""

    def opt(name: str) -> str:
        return args[args.index(name) + 1]

    def opt_value(key: str) -> str:
        """The value of the -f key=value argument."""
        return next(a for a in args if a.startswith(f"{key}=")).removeprefix(f"{key}=")

    if args[:2] == ["api", "graphql"] and "search(" in opt_value("query"):
        # The gh user's open pull requests, all on one page.
        mine = [p for p in prs if p["state"] == "OPEN" and p.get("author", "me") == "me"]
        nodes = [{k: p[k] for k in ("number", "headRefName", "baseRefName", "isCrossRepository")} for p in mine]
        return json.dumps({"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                               "nodes": nodes}}})
    if args[:2] == ["api", "graphql"]:
        query = opt_value("query")
        found: dict[str, Any] = {"defaultBranchRef": {"name": "main"}}
        for alias, variable in re.findall(r"(\w+): pullRequests\(headRefName: \$(\w+)", query):
            # Newest first, taking higher numbers as newer.
            matching = sorted((p for p in prs if p["headRefName"] == opt_value(variable)),
                              key=lambda p: p["number"], reverse=True)
            found[alias] = {"nodes": [{k: p[k] for k in PR_FIELDS} for p in matching]}
        return json.dumps({"data": {"repository": found}})
    if args[:2] == ["pr", "edit"]:
        next(p for p in prs if p["number"] == int(args[2]))["baseRefName"] = opt("--base")
        return None
    sys.exit(f"fake gh: unexpected {args}")


def main() -> None:
    path = os.environ["FAKE_GH_STATE"]
    with open(path) as f:
        state = json.load(f)
    args = sys.argv[1:]
    state["calls"].append(args)
    failing = args[0] in state["fail"]
    out = None if failing else respond(args, state["prs"])
    with open(path, "w") as f:
        json.dump(state, f)
    if failing:
        sys.exit("fake gh: failing as asked")
    if out is not None:
        print(out)


if __name__ == "__main__":
    main()
