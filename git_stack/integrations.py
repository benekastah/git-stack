"""Metadata and recoverable native merges for integration roots."""

import hashlib
import json
import pathlib
import time
from collections.abc import Iterable, Sequence
from typing import NotRequired, TypedDict, cast

from git_stack import events
from git_stack.adapters import make_adapter
from git_stack.adapters.github import GitHubAdapter
from git_stack.core import INTEGRATION_KEY, RemoteDeletion, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id, same_file
from git_stack.stacks import Stacks

PENDING_KEY = "stackIntegrationBuild"
CHECKOUT_KEY = "stackIntegrationCheckout"
PUBLISH_KEY = "stackIntegrationPublish"
DELETE_PREFIX = "stackIntegrationDelete"
# GitHub can attach merge metadata after the commit becomes visible on the root.
EVIDENCE_RECHECK_SECONDS = 300


class Source(TypedDict):
    name: str
    tip: str
    remote: str | None
    head: str
    landing: str | None
    checked: NotRequired[bool]
    checked_at: NotRequired[float]


class RetiredSource(TypedDict):
    source: Source
    group: str | None
    parent: str | None
    members: NotRequired[list[str]]
    integration_groups: NotRequired[list[str]]
    refresh_remote: NotRequired[str]


class Integration(TypedDict):
    root: str
    sources: list[Source]
    root_tip: str | None
    evidence_remote: NotRequired[str]
    retired_sources: NotRequired[dict[str, RetiredSource]]
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
        self.adapters: dict[tuple[str, bool], GitHubAdapter | None] = {}

    def names(self) -> set[str]:
        return {b for b, key in self.git.branch_config() if key == INTEGRATION_KEY.lower()}

    def read(self, name: str) -> Integration | None:
        value = self.git.config_get(name, INTEGRATION_KEY)
        return cast(Integration, json.loads(value)) if value else None

    def pending(self, name: str) -> Build | None:
        value = self.git.config_get(name, PENDING_KEY)
        if not value:
            return None
        build = cast(Build, json.loads(value))
        try:
            step, head = self.progress_path(name).read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return build
        if int(step) > build["step"]:
            build["step"], build["head"] = int(step), head
        return build

    def save(self, name: str, integration: Integration) -> None:
        self.git.config_set(name, INTEGRATION_KEY, json.dumps(integration))

    def clear_retired_sources(self, names: Iterable[str], stacks: Stacks, scope: frozenset[str]) -> None:
        """Drop retry selectors whose surviving work was included in this push."""
        configs = {name: config for name in names if (config := self.read(name)) and config.get("retired_sources")}
        if not configs:
            return
        outside = {stacks.bottom(branch) for branch in stacks if branch not in scope}
        groups_outside = {stacks.group(bottom) for bottom in outside} - {None}

        def has_outside_work(entry: RetiredSource) -> bool:
            if "members" in entry:
                return any(member in stacks and member not in scope for member in entry["members"])
            group, parent = entry["group"], entry["parent"]
            if group is None and parent in stacks:
                assert parent is not None
                return stacks.bottom(parent) in outside
            return (group or parent) in groups_outside

        for name, config in configs.items():
            retired = config.get("retired_sources", {})
            # Publication completes group recovery even if a retired source still
            # has children that require their own retry.
            remaining: dict[str, RetiredSource] = {
                branch: {**entry, "integration_groups": []} if "integration_groups" in entry else entry
                for branch, entry in retired.items()
                if has_outside_work(entry)
            }
            if remaining != retired:
                if remaining:
                    config["retired_sources"] = remaining
                else:
                    del config["retired_sources"]
                self.save(name, config)

    def replace_group(self, new: str, old: str | None = None) -> None:
        """A new group can reuse a name from a retired group. Only a rename
        carries over the old group's pending integration selectors."""

        def update(config: Integration) -> bool:
            changed = False
            for entry in config.get("retired_sources", {}).values():
                if old is not None and entry["group"] == old:
                    entry["group"] = new
                    changed = True
                groups = entry.get("integration_groups", [])
                if new in groups or old in groups:
                    entry["integration_groups"] = [new if group == old else group for group in groups if group != new]
                    changed = True
            return changed

        for name in self.names():
            config = self.read(name)
            if config and update(config):
                self.save(name, config)
            build = self.pending(name)
            if build and update(build["integration"]):
                self.journal(name, build)

    def renamed_inputs(self, old: str, new: str) -> dict[str, Integration]:  # noqa: C901
        """Prepare integration metadata for a local source or root rename."""
        self.check_pending_rename(old, new)
        updated: dict[str, Integration] = {}
        for name in self.names():
            config = self.read(name)
            assert config is not None
            source_names = {source["name"] for source in config["sources"]}
            retired = config.get("retired_sources", {})
            continuations = [entry for entry in retired.values() if entry["parent"] == old]
            members = [entry for entry in retired.values() if old in entry.get("members", [])]
            if config["root"] != old and old not in source_names and not continuations and not members:
                continue
            if old in source_names and new in source_names:
                raise StackError(f"{new} is already a source for integration {name}")
            if self.pending(name):
                raise StackError(
                    f"{name} has a pending integration build; run `git stack integrate --abort {name}` first"
                )
            if config["root"] == old:
                config["root"] = new
            for source in config["sources"]:
                if source["name"] == old:
                    source["name"] = new
            if old in retired:
                retired[new] = retired.pop(old)
                retired[new]["source"]["name"] = new
            for entry in continuations:
                entry["parent"] = new
            for entry in members:
                entry["members"] = [new if member == old else member for member in entry.get("members", [])]
            updated[name] = config
        return updated

    def check_pending_rename(self, old: str, new: str) -> None:
        """Protect the branch and inputs pinned by an in-progress build."""
        for name, key in self.git.branch_config():
            if (
                key == PENDING_KEY.lower()
                and (build := self.pending(name))
                and ({old, new}.intersection(build["observed"]) or name in (old, new))
            ):
                raise StackError(
                    f"{name} has a pending integration build; run `git stack integrate --abort {name}` first"
                )

    def journal(self, name: str, build: Build) -> None:
        self.git.config_set(name, PENDING_KEY, json.dumps(build))

    def remember_first_push(self, name: str, remote: str, target: str, tip: str) -> None:
        self.git.config_set(name, PUBLISH_KEY, json.dumps([remote, target, tip]))

    def first_push(self, name: str) -> tuple[str, str, str] | None:
        value = self.git.config_get(name, PUBLISH_KEY)
        return cast(tuple[str, str, str], json.loads(value)) if value else None

    def forget_first_push(self, name: str) -> None:
        if self.git.config_get(name, PUBLISH_KEY):
            self.git.config_unset(name, PUBLISH_KEY)

    def deletion_key(self, deletion: RemoteDeletion) -> str:
        target = f"{deletion.remote} {deletion.ref}".encode()
        return DELETE_PREFIX + hashlib.sha256(target).hexdigest()

    def remember_deletion(self, name: str, deletion: RemoteDeletion) -> None:
        self.git.config_set(
            name,
            self.deletion_key(deletion),
            json.dumps([deletion.remote, deletion.ref, deletion.tip, deletion.children]),
        )

    def forget_deletion(self, name: str, deletion: RemoteDeletion) -> None:
        self.git.config_unset(name, self.deletion_key(deletion))

    def pending_deletions(self, names: set[str], remote: str) -> list[tuple[str, RemoteDeletion]]:
        return [
            (name, deletion)
            for (name, key), value in self.git.branch_config().items()
            if name in names
            and key.startswith(DELETE_PREFIX.lower())
            and (deletion := self.read_deletion(value)).remote == remote
        ]

    @staticmethod
    def read_deletion(value: str) -> RemoteDeletion:
        record = cast(
            tuple[str, str, str] | tuple[str, str, str, list[tuple[str, str | None]] | None], json.loads(value)
        )
        remote, ref, tip = record[:3]
        children = record[3] if len(record) == 4 else None
        return RemoteDeletion(remote, ref, tip, None if children is None else tuple((b, r) for b, r in children))

    def path(self, name: str) -> pathlib.Path:
        key = hashlib.sha256(name.encode()).hexdigest()[:24]
        return self.git.common_dir() / "stack-integration-builds" / key

    def progress_path(self, name: str) -> pathlib.Path:
        return self.path(name).with_suffix(".progress")

    def write_progress(self, name: str, build: Build) -> None:
        """Write a small checkpoint without rewriting the immutable build inputs."""
        path = self.progress_path(name)
        staging = path.with_suffix(".tmp")
        staging.write_text(f"{build['step']} {build['head']}\n", encoding="utf-8")
        staging.replace(path)

    def remove_build(self, name: str) -> None:
        path = self.path(name)
        # Git can remove a registered worktree even if its files are gone.
        registered = self.git.run("worktree", "list", "--porcelain", "-z").split("\0")
        if any(path == pathlib.Path(entry[9:]) for entry in registered if entry.startswith("worktree ")):
            self.core.emit(events.RemovingIntegrationWorktree(name))
            self.git.run("worktree", "remove", "--force", str(path))
        self.git.config_unset(name, PENDING_KEY)
        self.progress_path(name).unlink(missing_ok=True)
        self.progress_path(name).with_suffix(".tmp").unlink(missing_ok=True)

    def restore_pending_checkout(self) -> None:
        """Restore the integration if an interruption left this worktree detached."""
        if self.git.current_branch() is not None:
            return
        head = self.git.rev("HEAD")
        for name in sorted(self.names()):
            marker = self.git.config_get(name, CHECKOUT_KEY)
            if marker:
                checkout, result = cast(tuple[str, str], json.loads(marker))
            else:
                # A build stopped by an earlier version recorded this in its journal.
                build = self.pending(name)
                checkout, result = (build.get("checkout"), build["result"]) if build else (None, None)
            if checkout and result == head and same_file(pathlib.Path(checkout), self.git.toplevel()):
                self.git.switch(name)
                self.git.config_unset(name, CHECKOUT_KEY)
                break

    def finish_interrupted(self, name: str) -> bool:
        """Complete metadata and cleanup if the ref update already happened."""
        build = self.pending(name)
        if build is None or (result := build["result"]) is None or result != self.git.rev(f"refs/heads/{name}"):
            return False
        if result == build["expected"] and self.read(name) != build["integration"]:
            return False
        self.save(name, build["integration"])
        if (checkout := build.get("checkout")) and not self.git.config_get(name, CHECKOUT_KEY):
            self.git.config_set(name, CHECKOUT_KEY, json.dumps([checkout, result]))
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
            if remote and self.git.config_get(name, "remote") == remote
            else (previous["head"] if previous else name)
        )
        return Source(
            name=name,
            tip=tip,
            remote=remote,
            head=head,
            landing=previous["landing"] if previous and tip == previous["tip"] else None,
        )

    def validate(
        self,
        name: str,
        root: str,
        sources: Sequence[str],
        stacks: Stacks,
        tip: str | None,
        *,
        names: set[str] | None = None,
    ) -> None:
        if name in stacks:
            raise StackError(f"{name} is tracked; untrack it before registering it as an integration")
        integrations = self.names() | {name} if names is None else names
        if root in stacks or root in integrations:
            raise StackError(f"{root} must be an ordinary untracked root")
        root_tip = self.git.tip(root)
        generated = tip if tip and not self.git.is_ancestor(tip, root_tip) else None
        for source in sources:
            if stacks.root(source) in integrations:
                raise StackError(f"{source} is an integration or depends on one")
            source_tip = self.git.rev(f"refs/heads/{source}")
            if generated and source_tip and self.git.is_ancestor(generated, source_tip):
                raise StackError(f"{source} contains the generated tip of {name} and depends on it")

    def ensure_independent(self, name: str, stacks: Stacks) -> None:
        """A newly registered integration cannot be an input of an existing one."""
        for other in self.names() - {name}:
            config = self.read(other)
            assert config is not None
            if config["root"] == name or any(stacks.root(src["name"]) == name for src in config["sources"]):
                raise StackError(f"{name} is an input of integration {other}")

    def preflight(self, name: str, *, force: bool = False) -> Integration:
        self.finish_interrupted(name)
        integration = self.read(name)
        if integration is None:
            raise StackError(f"{name} isn't a registered integration")
        recorded = integration["tip"]
        current = self.git.tip(name)
        if current != recorded and not force:
            extra = self.git.run("log", "--format=%h %s", f"{recorded}..{current}") if self.git.rev(recorded) else ""
            detail = extra or f"Recorded tip: {recorded}\nCurrent tip:  {current}"
            raise StackError(
                f"{name} differs from its last generated tip:\n{detail}\n"
                f"Run `git stack restack {name} --force` to replace the tip and remove any extra commits "
                f"from {name}."
            )
        return integration

    def inputs(
        self,
        name: str,
        root: str,
        sources: Sequence[Source],
        remote: str,
        fetch: bool,
        *,
        previous: Integration | None = None,
    ) -> tuple[Integration, dict[str, str | None]]:
        root_tip = self.git.tip(root)
        observed: dict[str, str | None] = {root: root_tip}
        same_root = bool(previous and previous["root_tip"] == root_tip and previous.get("evidence_remote") == remote)
        inputs: list[Source] = []
        unresolved: list[Source] = []
        retired = previous.get("retired_sources", {}) if previous else {}
        pending = self.pending(name) if retired else None
        pending_sources = {source["name"]: source for source in pending["integration"]["sources"]} if pending else {}
        still_retired: dict[str, RetiredSource] = {}
        for source in sources:
            branch = source["name"]
            live = self.git.rev(f"refs/heads/{branch}")
            observed[branch] = live
            entry = retired.get(branch)
            recorded = entry["source"] if entry and live is None else source
            current = self.source(branch, live or recorded["tip"], recorded)
            if entry:
                current = self.retired_input(source, current, pending_sources.get(branch))
            if entry and live in (None, entry["source"]["tip"]):
                still_retired[branch] = {**entry, "source": current}
            if self.local_landing(current, root_tip):
                pass
            elif live and same_root and self.same_evidence(source, current, fetch):
                current["checked"] = source.get("checked", False)
                current["checked_at"] = source.get("checked_at", 0)
            else:
                unresolved.append(current)
            inputs.append(current)
        self.look_up_landings(unresolved, remote, root_tip, fetch)
        for current in inputs:
            if observed[current["name"]] is None and not current["landing"]:
                raise StackError(
                    f"source {current['name']} is missing and its included tip {current['tip']} "
                    f"hasn't been proven to land on {root}. Replace the sources with "
                    f"`git stack integrate {name} <source>...`."
                )
        desired = Integration(root=root, sources=inputs, root_tip=root_tip, evidence_remote=remote, tip=root_tip)
        if still_retired:
            desired["retired_sources"] = still_retired
        return desired, observed

    @staticmethod
    def retired_input(source: Source, captured: Source, pending: Source | None) -> Source:
        """Use existing landing proof only for the exact captured source identity."""
        for candidate in (pending, source):
            if (
                candidate
                and candidate["landing"]
                and all(candidate[k] == captured[k] for k in ("tip", "remote", "head"))
            ):
                return candidate
        return captured

    def local_landing(self, source: Source, root_tip: str) -> bool:
        """Keep a recorded proof only while its commit is still on the root."""
        landing = source["landing"]
        if landing and self.git.rev(landing) and self.git.is_ancestor(landing, root_tip):
            return True
        tip = source["tip"]
        source["landing"] = tip if self.git.rev(tip) and self.git.is_ancestor(tip, root_tip) else None
        return source["landing"] is not None

    @staticmethod
    def same_evidence(old: Source, source: Source, fetch: bool) -> bool:
        return (
            "checked" in old
            and (not fetch or old["checked"])
            and 0 <= time.time() - old.get("checked_at", 0) < EVIDENCE_RECHECK_SECONDS
            and all(old[key] == source[key] for key in ("tip", "remote", "head"))
        )

    def look_up_landings(self, sources: list[Source], remote: str, root_tip: str, fetch: bool) -> None:
        if not sources:
            return
        self.check_landings(sources, remote, root_tip, fetch)
        # A source can also have landed via a PR in another GitHub repository
        # whose merge commit was subsequently brought onto the root.
        others: dict[str, list[Source]] = {}
        for source in sources:
            if not source["landing"] and (other := source["remote"]) and other != remote:
                others.setdefault(other, []).append(source)
        for other, candidates in others.items():
            self.check_landings(candidates, other, root_tip, fetch, optional=True)

    def check_landings(
        self, sources: list[Source], remote: str, root_tip: str, fetch: bool, *, optional: bool = False
    ) -> None:
        key = remote, fetch
        try:
            if key not in self.adapters:
                adapter = make_adapter(self.core, remote, fetch=fetch)
                self.adapters[key] = adapter if isinstance(adapter, GitHubAdapter) else None
            if github := self.adapters[key]:
                landed, complete = github.source_landings({(s["head"], s["tip"]) for s in sources}, root_tip)
                for source in sources:
                    identity = source["head"], source["tip"]
                    source["landing"] = landed.get(identity)
                    if identity in complete:
                        source["checked"] = fetch
                        source["checked_at"] = time.time()
                    else:
                        source.pop("checked", None)
                        source.pop("checked_at", None)
        except StackError as exc:
            if fetch and not optional:
                raise
            for source in sources:
                source.pop("checked", None)
                source.pop("checked_at", None)
            self.core.emit(events.PullRequestLookupFailed(str(exc)))

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
                or [(s["name"], s["tip"]) for s in saved["sources"]]
                != [(s["name"], s["tip"]) for s in desired["sources"]]
                or self.active_sources(saved) != inputs
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
            self.core.emit(events.RebuildingIntegration(name))
            build = Build(integration=desired, expected=expected, observed=observed, step=0, head=root_tip, result=None)
            self.progress_path(name).unlink(missing_ok=True)
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
            self.write_progress(name, build)
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
            self.git.config_set(name, CHECKOUT_KEY, json.dumps([str(self.git.toplevel()), result]))
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
                self.git.config_unset(name, CHECKOUT_KEY)
        self.remove_build(name)

    def verify(self, refs: dict[str, str | None], update: tuple[str, str] | None = None) -> None:
        """Verify inputs together, with the update in the same ref transaction."""
        # NUL-delimited fields let Git validate the names in the same
        # transaction. An empty old value for verify requires an absent ref.
        commands: list[str] = []
        for branch, tip in refs.items():
            if "\0" in branch or (tip and not is_object_id(tip)):
                raise StackError(f"invalid integration input ref or tip for {branch}")
            commands.append("option no-deref")
            if update and branch == update[0]:
                commands.extend((f"update refs/heads/{branch}", update[1], tip or "0" * len(update[1])))
            else:
                commands.extend((f"verify refs/heads/{branch}", tip or ""))
        try:
            self.git.ref_transaction(commands, publish=update is not None)
        except StackError as e:
            raise StackError(f"integration refs changed or could not be locked; retry or abort the build:\n{e}") from e
