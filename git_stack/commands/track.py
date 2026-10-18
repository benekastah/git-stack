def register(subparsers):
    p = subparsers.add_parser("track", help="track existing branches as a stack on PARENT, each on the "
                                            "one before it (default: the current branch)")
    p.add_argument("parent")
    p.add_argument("branches", nargs="*", metavar="branch",
                   help="branches to stack, bottom first (default: the current branch)")
    p.add_argument("--base", help="commit the first branch was built on (default: merge-base with "
                                  "PARENT). Needed when PARENT has already been squash-merged or "
                                  "rewritten")
    p.set_defaults(func=run)


def run(stack, args):
    for tracked in stack.track(args.parent, args.branches, base=args.base):
        print(f"tracking {tracked.branch} on top of {tracked.parent} (base {tracked.base[:12]})")
