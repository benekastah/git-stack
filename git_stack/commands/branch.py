import argparse
from dataclasses import dataclass

from git_stack.commands import Subparsers, add_json_argument
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    name: str
    group: str | None
    from_branch: str | None
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "create", aliases=["branch"], help="create a new branch stacked on the current one (or --from another)"
    )
    p.add_argument("name")
    p.add_argument(
        "--from",
        dest="from_branch",
        metavar="branch",
        help="stack it on this local branch instead of the current one (and switch to it)",
    )
    p.add_argument(
        "--group",
        help="also put its stack in group GROUP (see `git stack group`), failing without "
        "creating it if the stack is in another group already",
    )
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Created:
    branch: str
    parent: str
    group: str | None


def run(stack: StackClient, args: Args) -> Created:
    return Created(args.name, stack.branch(args.name, group=args.group, from_branch=args.from_branch), args.group)


def show(stack: StackClient, args: Args, created: Created) -> None:
    print(
        f"created {created.branch} on top of {created.parent}"
        + (f", in group {created.group}" if created.group else "")
    )
