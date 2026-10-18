from git_stack.errors import StackError
from git_stack.stack import BASE_KEY, PARENT_KEY


def register(subparsers):
    p = subparsers.add_parser("branch", help="create a new branch stacked on the current one")
    p.add_argument("name")
    p.set_defaults(func=run)


def run(stack, args):
    git = stack.git
    parent = git.current_branch()
    if parent is None:
        raise StackError("not on a branch")
    git.run("switch", "--quiet", "--create", args.name)
    git.config_set(args.name, PARENT_KEY, parent)
    git.config_set(args.name, BASE_KEY, git.rev("HEAD"))
    print(f"created {args.name} on top of {parent}")
