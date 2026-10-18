from git_stack.commands import add_delete_argument
from git_stack.git import DEFAULT_REMOTE


def register(subparsers):
    p = subparsers.add_parser("sync", help="fetch, untrack the current stack's merged branches, "
                                           "restack it, and pull in commits others pushed")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    add_delete_argument(p)
    p.set_defaults(func=run)


def run(stack, args):
    unpushed = stack.sync(remote=args.remote, delete=args.delete)
    if unpushed:
        print("note: run `git stack push` to update the remote branches and pull requests of:")
        for branch in unpushed:
            print(f"  {branch}")
