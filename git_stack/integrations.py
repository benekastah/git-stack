"""Metadata and recoverable native merges for integration roots."""

import hashlib
import json
import pathlib
from collections.abc import Sequence
from typing import NotRequired, TypedDict, cast

from git_stack import events
from git_stack.adapters import make_adapter
from git_stack.adapters.github import GitHubAdapter
from git_stack.core import INTEGRATION_KEY, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id, same_file
from git_stack.stacks import Stacks

PENDING_KEY = "stackIntegrationBuild"


class Source(TypedDict):
    name: str
    tip: str
    remote: str | None
    head: str
    landing: str | None


class Integration(TypedDict):
    root: str
    sources: list[Source]
    root_tip: str | None
    tip: str


class Build(TypedDict):
    integration: Integration
    expected: str | None
    observed: dict[str, str | None]
    step: int
    head: str
    result: str | None
    checkout: NotRequired[str]


class Integrations:
    """Build against pinned refs in a detached worktree. Journal the next
    merge before starting it and the result before moving the branch."""

    def __init__(self, core: StackCore) -> None:
        self.core = core
        self.git = core.git

    def names(self) -> set[str]:
        return {b for b, key in self.git.branch_config() if key == INTEGRATION_KEY.lower()}

    def read(self, name: str) -> Integration | None:
        value = self.git.config_get(name, INTEGRATION_KEY)
        return cast(Integration, json.loads(value)) if value else None

    def pending(self, name: str) -> Build | None:
        value = self.git.config_get(name, PENDING_KEY)
        return cast(Build, json.loads(value)) if value else None

    def save(self, name: str, integration: Integration) -> None:
        self.git.config_set(name, INTEGRATION_KEY, json.dumps(integration))

    def journal(self, name: str, build: Build) -> None:
        self.git.config_set(name, PENDING_KEY, json.dumps(build))

    def path(self, name: str) -> pathlib.Path:
        key = hashlib.sha256(name.encode()).hexdigest()[:24]
        return self.git.common_dir() / "stack-integration-builds" / key

    def remove_build(self, name: str) -> None:
        path = self.path(name)
        # Git can remove a registered worktree even if its files are gone.
        registered = self.git.run("worktree", "list", "--porcelain", "-z").split("\0")
        if any(path == pathlib.Path(entry[9:]) for entry in registered if entry.startswith("worktree ")):
            self.git.run("worktree", "remove", "--force", str(path))
        self.git.config_unset(name, PENDING_KEY)

    def restore_pending_checkout(self) -> None:
        """Restore the integration if an interruption left this worktree detached."""
        if self.git.current_branch() is not None:
            return
        head = self.git.rev("HEAD")
        for name in sorted(self.names()):
            build = self.pending(name)
            if (
                build
                and build["result"] == head
                and (checkout := build.get("checkout"))
                and same_file(pathlib.Path(checkout), self.git.toplevel())
            ):
                self.git.switch(name)
                break

    def finish_interrupted(self, name: str) -> bool:
        """Complete metadata and cleanup if the ref update already happened."""
        build = self.pending(name)
        if build is None or (result := build["result"]) is None or result != self.git.rev(f"refs/heads/{name}"):
            return False
        if result == build["expected"] and self.read(name) != build["integration"]:
            return False
        self.save(name, build["integration"])
        self.remove_build(name)
        return True

    def abort(self, name: str) -> None:
        if self.pending(name) is None:
            raise StackError(f"{name} has no integration build to abort")
        if not self.finish_interrupted(name):
            self.remove_build(name)

    def source(self, name: str, tip: str, previous: Source | None = None) -> Source:
        remote = self.git.config_get(name, "remote") or (previous and previous["remote"])
        if remote == ".":
            remote = None
            previous = None
        head = (
            self.git.remote_branch_name(name, remote)
            if remote and self.git.has_branch_upstream(name, remote)
            else (previous["head"] if previous else name)
        )
        return Source(
            name=name,
            tip=tip,
            remote=remote,
            head=head,
            landing=previous["landing"] if previous and tip == previous["tip"] else None,
        )

    def validate(self, name: str, root: str, sources: Sequence[str], stacks: Stacks, tip: str | None) -> None:
        if name in stacks:
            raise StackError(f"{name} is tracked; untrack it before registering it as an integration")
        integrations = self.names() | {name}
        if root in stacks or root in integrations:
            raise StackError(f"{root} must be an ordinary untracked root")
        root_tip = self.git.tip(root)
        for source in sources:
            if stacks.root(source) in integrations:
                raise StackError(f"{source} is an integration or depends on one")
            source_tip = self.git.rev(f"refs/heads/{source}")
            if (
                tip
                and source_tip
                and tip != source_tip
                and not self.git.is_ancestor(tip, root_tip)
                and self.git.is_ancestor(tip, source_tip)
            ):
                raise StackError(f"{source} contains the generated tip of {name} and depends on it")

    def ensure_independent(self, name: str, stacks: Stacks) -> None:
        """A newly registered integration cannot be an input of an existing one."""
        for other in self.names() - {name}:
            config = self.read(other)
            assert config is not None
            if config["root"] == name or any(stacks.root(src["name"]) == name for src in config["sources"]):
                raise StackError(f"{name} is an input of integration {other}")

    def preflight(self, name: str) -> Integration:
        self.finish_interrupted(name)
        integration = self.read(name)
        if integration is None:
            raise StackError(f"{name} isn't a registered integration")
        recorded = integration["tip"]
        current = self.git.tip(name)
        if current != recorded:
            extra = self.git.run("log", "--format=%h %s", f"{recorded}..{current}") if self.git.rev(recorded) else ""
            detail = extra or f"Recorded tip: {recorded}\nCurrent tip:  {current}"
            raise StackError(
                f"{name} differs from its last generated tip:\n{detail}\n"
                f"Restore {name} to {recorded} before rebuilding it."
            )
        return integration

    def inputs(
        self,
        name: str,
        root: str,
        sources: Sequence[Source],
        remote: str,
        fetch: bool,
    ) -> tuple[Integration, dict[str, str | None]]:
        root_tip = self.git.tip(root)
        observed: dict[str, str | None] = {root: root_tip}
        inputs: list[Source] = []
        for source in sources:
            branch = source["name"]
            live = self.git.rev(f"refs/heads/{branch}")
            observed[branch] = live
            tip = live or source["tip"]
            current = self.source(branch, tip, source)
            landing = source["landing"] if source["tip"] == tip else None
            if landing and not self.git.is_ancestor(landing, root_tip):
                landing = None
            if landing is None and self.git.rev(tip) and self.git.is_ancestor(tip, root_tip):
                landing = tip
            if landing is None:
                adapter = make_adapter(self.core, current["remote"] or remote, fetch=fetch)
                if isinstance(adapter, GitHubAdapter):
                    try:
                        landing = adapter.source_landing(current["head"], tip, root_tip)
                    except StackError as exc:
                        if fetch:
                            raise
                        self.core.emit(events.PullRequestLookupFailed(str(exc)))
            if live is None and landing is None:
                raise StackError(
                    f"source {branch} is missing and its included tip {tip} hasn't been proven to land on {root}. "
                    f"Replace the sources with `git stack integrate {name} <source>...`."
                )
            current["landing"] = landing
            inputs.append(current)
        return Integration(root=root, sources=inputs, root_tip=root_tip, tip=root_tip), observed

    def build(
        self,
        name: str,
        desired: Integration,
        observed: dict[str, str | None],
        previous: Integration | None,
        expected: str | None,
    ) -> dict[str, str | None]:
        self.verify({**observed, name: expected})
        root = desired["root"]
        root_tip = observed[root]
        assert root_tip is not None
        inputs = self.active_sources(desired)
        build = self.pending(name)
        if build:
            saved = build["integration"]
            if (
                build["expected"] != expected
                or build["observed"] != observed
                or saved["root"] != root
                or saved["sources"] != desired["sources"]
            ):
                raise StackError(
                    f"inputs or tip of {name} changed during its integration build. "
                    f"Run `git stack integrate --abort {name}` and retry."
                )
        elif (
            previous
            and expected == previous["tip"]
            and previous["root_tip"] == root_tip
            and self.active_sources(previous) == inputs
        ):
            desired["tip"] = previous["tip"]
            if desired != previous:
                self.save(name, desired)
            return {**observed, name: expected}
        else:
            self.ensure_available_worktree(name)
            build = Build(integration=desired, expected=expected, observed=observed, step=0, head=root_tip, result=None)
            self.journal(name, build)
        self.ensure_available_worktree(name)
        result = self.merge(name, build, inputs)
        self.publish(name, build, result)
        return {**observed, name: result}

    @staticmethod
    def active_sources(integration: Integration) -> list[tuple[str, str]]:
        return [(source["name"], source["tip"]) for source in integration["sources"] if not source["landing"]]

    def ensure_available_worktree(self, name: str) -> None:
        if (path := self.git.other_worktree_of(name, fresh=True)) is not None:
            raise StackError(f"can't move integration {name}: checked out in another worktree at {path}")

    def merge(self, name: str, build: Build, inputs: list[tuple[str, str]]) -> str:
        if build["result"] is not None:
            return build["result"]
        if not inputs:
            return build["head"]
        worktree = self.merge_worktree(name, build)
        for index in range(build["step"], len(inputs)):
            source, tip = inputs[index]
            before, head = build["head"], worktree.rev("HEAD")
            if head != before:
                # The merge completed before its next step was journaled,
                # possibly with a conflict resolution made by the user.
                parents = worktree.run("show", "-s", "--format=%P", "HEAD").split()
                if parents != [before, tip]:
                    raise StackError(
                        f"integration worktree HEAD changed unexpectedly; run `git stack integrate --abort {name}`"
                    )
            elif not worktree.is_ancestor(tip, before) and not worktree.passthrough(
                "merge", "--no-ff", "--no-edit", "-m", f"Merge {source} into {name}", tip
            ):
                if worktree.operation_in_progress():
                    raise self.stopped(name)
                raise StackError(f"merging {source} into {name} failed; run `git stack integrate --abort {name}`")
            build["head"] = worktree.run("rev-parse", "HEAD")
            build["step"] = index + 1
            self.journal(name, build)
        if worktree.rev("HEAD") != build["head"] or worktree.run("status", "--porcelain"):
            raise StackError(f"integration worktree changed unexpectedly; run `git stack integrate --abort {name}`")
        return build["head"]

    def merge_worktree(self, name: str, build: Build) -> GitClient:
        """Open the detached worktree, creating it for the first merge."""
        path = self.path(name)
        path.parent.mkdir(exist_ok=True)
        worktree = GitClient(path)
        worktree.passthrough_stdout = self.git.passthrough_stdout
        if not (path / ".git").is_file():
            if build["step"]:
                raise StackError(f"integration worktree is missing; run `git stack integrate --abort {name}`")
            self.git.run("worktree", "add", "--quiet", "--detach", str(path), build["head"])
        if worktree.current_branch() is not None:
            raise StackError(f"integration worktree is no longer detached; run `git stack integrate --abort {name}`")
        if worktree.operation_in_progress():
            raise self.stopped(name)
        return worktree

    def stopped(self, name: str) -> StackError:
        path = self.path(name)
        return StackError(
            f"integration merge stopped in {path}. Resolve and stage the conflicts, then run:\n"
            f'  git -C "{path}" merge --continue\n'
            f'Or back out with:\n  git -C "{path}" merge --abort\n'
            f"Then rerun the original git-stack command, or discard the build with "
            f"`git stack integrate --abort {name}`."
        )

    def publish(self, name: str, build: Build, result: str) -> None:
        self.ensure_available_worktree(name)
        # Journal the result first: if interrupted after updating the ref,
        # the next invocation can distinguish that update from a user edit.
        build["result"] = build["integration"]["tip"] = result
        expected = build["expected"]
        checked_out = self.git.current_branch() == name and result != expected
        if checked_out:
            self.core.ensure_clean_worktree()
            build["checkout"] = str(self.git.toplevel())
        self.journal(name, build)
        if checked_out:
            # Check out the result before moving the ref. Git can then refuse
            # an untracked-file collision while the integration is still intact.
            self.git.run("switch", "--quiet", "--detach", result)
        try:
            self.ensure_available_worktree(name)
            self.verify({**build["observed"], name: expected}, update=(name, result))
            self.save(name, build["integration"])
        finally:
            if checked_out:
                self.git.switch(name)
        self.remove_build(name)

    def verify(self, refs: dict[str, str | None], update: tuple[str, str] | None = None) -> None:
        """Verify inputs together, with the update in the same ref transaction."""
        # An omitted old value in a verify means the ref must not exist.
        commands = ["start"]
        for branch, tip in refs.items():
            if not self.git.ok("check-ref-format", f"refs/heads/{branch}") or (tip and not is_object_id(tip)):
                raise StackError(f"invalid integration input ref or tip for {branch}")
            commands.append("option no-deref")
            old = f" {tip}" if tip else ""
            if update and branch == update[0]:
                commands.append(f"update refs/heads/{branch} {update[1]}{old or ' ' + '0' * len(update[1])}")
            else:
                commands.append(f"verify refs/heads/{branch}{old}")
        commands.extend(("prepare", "commit" if update else "abort", ""))
        try:
            self.git.run("update-ref", "--stdin", input="\n".join(commands))
        except StackError as e:
            raise StackError(f"integration refs changed or could not be locked; retry or abort the build:\n{e}") from e
