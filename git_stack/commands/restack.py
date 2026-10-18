def register(subparsers):
    p = subparsers.add_parser("restack", help="rebase every branch in the current stack onto its parent")
    p.add_argument("--onto", help="first change the current branch's parent to ONTO")
    p.add_argument("--squash", action="store_true",
                   help="first squash each branch in the stack into a single commit")
    p.set_defaults(func=run)


def run(stack, args):
    stack.restack(onto=args.onto, squash=args.squash)
