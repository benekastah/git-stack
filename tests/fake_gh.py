#!/usr/bin/env python3
"""Stands in for the gh CLI in tests.

Keeps pull requests and the calls it got in the JSON file at $FAKE_GH_STATE,
and fails for commands whose first argument is listed in its "fail" key.
"""

import json
import os
import re
import sys

PR_FIELDS = ("number", "state", "headRefOid", "baseRefName", "mergeCommit")


def respond(args, prs):
    """Handle the gh command args, updating prs. Return what gh would print, or None."""

    def opt(name):
        return args[args.index(name) + 1]

    if args[:2] == ["api", "graphql"]:
        query = next(a for a in args if a.startswith("query="))
        found = {"defaultBranchRef": {"name": "main"}}
        for n in map(int, re.findall(r"pullRequest\(number: (\d+)\)", query)):
            pr = next((p for p in prs if p["number"] == n), None)
            found[f"pr{n}"] = pr and {k: pr[k] for k in PR_FIELDS}
        return json.dumps({"data": {"repository": found}})
    if args[:2] == ["pr", "list"]:
        return json.dumps([{"number": p["number"]} for p in prs
                           if p["headRefName"] == opt("--head") and p["state"] == "OPEN"])
    if args[:2] == ["pr", "create"]:
        number = max((p["number"] for p in prs), default=0) + 1
        prs.append({"number": number, "state": "OPEN", "isDraft": "--draft" in args,
                    "headRefName": opt("--head"), "headRefOid": None, "baseRefName": opt("--base"),
                    "mergeCommit": None})
        return f"https://github.com/acme/widgets/pull/{number}"
    if args[:2] == ["pr", "edit"]:
        next(p for p in prs if p["number"] == int(args[2]))["baseRefName"] = opt("--base")
        return None
    sys.exit(f"fake gh: unexpected {args}")


def main():
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
