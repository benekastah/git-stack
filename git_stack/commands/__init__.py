"""One module per `git stack` subcommand.

Each module has register(subparsers), which adds its parser and sets func to
its run(stack, args).
"""

DELETE_HELP = ("besides untracking merged branches, delete their local branches, their remote "
               "branches, or both")


def add_delete_argument(parser):
    parser.add_argument("-d", "--delete", choices=("local", "remote", "both"), help=DELETE_HELP)
