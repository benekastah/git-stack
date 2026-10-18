from git_stack.git import DEFAULT_REMOTE


def register(subparsers):
    p = subparsers.add_parser("push", help="force-push (with lease) every branch in the current stack, "
                                           "refusing if that would drop commits on the remote")
    p.add_argument("--remote", default=DEFAULT_REMOTE)
    p.set_defaults(func=run)


def run(stack, args):
    if not stack.push(remote=args.remote):
        print(f"nothing to push: every branch in {stack.git.current_branch()}'s stack looks merged")
