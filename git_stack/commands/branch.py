import argparse

from git_stack.commands import Subparsers, add_json_argument, run_for_json
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    group: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("branch", help="create a new branch stacked on the current one")
    p.add_argument("name")
    p.add_argument(
        "--group",
        help="also put its stack in group GROUP (see `git stack group`), failing without "
        "creating it if the stack is in another group already",
    )
    add_json_argument(p)
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    def branch() -> str:
        return stack.branch(args.name, group=args.group)

    if args.json:
        run_for_json(stack, branch, lambda parent: {"branch": args.name, "parent": parent, "group": args.group})
        return
    parent = branch()
    print(f"created {args.name} on top of {parent}" + (f", in group {args.group}" if args.group else ""))
