def register(subparsers):
    p = subparsers.add_parser("branch", help="create a new branch stacked on the current one")
    p.add_argument("name")
    p.set_defaults(func=run)


def run(stack, args):
    parent = stack.branch(args.name)
    print(f"created {args.name} on top of {parent}")
