from git_stack.commands import add_delete_argument
from git_stack.git import DEFAULT_REMOTE


def register(subparsers):
    p = subparsers.add_parser("clean", help="fetch and untrack merged branches in every stack, "
                                            "without restacking")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    p.set_defaults(func=run)


def run(stack, args):
    stack.clean(remote=args.remote, delete=args.delete)
