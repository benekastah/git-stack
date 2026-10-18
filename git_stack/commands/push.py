import argparse

from git_stack.commands import Subparsers
from git_stack.git import DEFAULT_REMOTE
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""
    branch: str | None
    remote: str
    force: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser("push", help="force-push (with lease) every branch in the current stack (or "
                                           "BRANCH's), refusing if that would drop commits on the remote")
    p.add_argument("branch", nargs="?", help="push BRANCH's stack instead of the current branch's")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.add_argument("--force", action="store_true",
                   help="push even if that drops commits on a remote branch that the local one doesn't "
                        "have. Still fails if a remote branch changed since the last fetch")
    p.set_defaults(func=run)


def run(stack: StackClient, args: Args) -> None:
    if not stack.push(args.branch, remote=args.remote, force=args.force):
        branch = args.branch or stack.git.current_branch()
        print(f"nothing to push: every branch in {branch}'s stack looks merged")
