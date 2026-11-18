import argparse
from dataclasses import dataclass

from git_stack import skills
from git_stack.commands import Subparsers, add_json_argument
from git_stack.skills import AGENTS, InstallAction, Installed
from git_stack.stack import StackClient


class Args(argparse.Namespace):
    """The arguments register's parser produces."""

    agents: list[str]
    replace: bool
    json: bool


def register(subparsers: Subparsers) -> None:
    p = subparsers.add_parser(
        "install-skills",
        help="link git-stack's agent skills into the directories your coding agents read them from",
    )
    for agent in AGENTS:
        p.add_argument(
            f"--{agent.name}",
            dest="agents",
            action="append_const",
            const=agent.name,
            default=[],
            help=f"install for {agent.name} (even if it isn't found); without any such option, "
            "install for each agent found",
        )
    p.add_argument(
        "--replace",
        action="store_true",
        help="replace a skill's link to somewhere else (e.g. another clone's); a link that's already right is "
        "left alone, and a file or directory that isn't a link never replaced",
    )
    add_json_argument(p)
    p.set_defaults(run=run, show=show)


@dataclass(frozen=True)
class Result:
    installed: list[Installed]


def run(stack: StackClient, args: Args) -> Result:
    return Result(skills.install(args.agents, args.replace))


def show(stack: StackClient, args: Args, result: Result) -> None:
    for item in result.installed:
        verb = "already linked" if item.action is InstallAction.UNCHANGED else item.action.value
        print(f"{item.agent}: {verb} {item.path}")
