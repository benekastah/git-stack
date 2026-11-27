"""Metadata and recoverable builds for integration roots."""

import hashlib
import json
import pathlib
import time
import unicodedata
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
    group: NotRequired[str]
    retired_sources: NotRequired[dict[str, RetiredSource]]
    # Each selected group whose work above the integration a sync or pull
    # untracked without publishing the integration, with its remote, so the
    # group still selects the integration until a sync publishes it.
    retry_groups: NotRequired[dict[str, str]]
    automatic: NotRequired[bool]
    tip: str


class Build(TypedDict):
    integration: Integration
    expected: str | None
    observed: dict[str, str | None]
    step: int
    head: str
    result: str | None
    checkout: NotRequired[str]
    replay: NotRequired[bool]


class Integrations:
    """Build against pinned refs in a detached worktree, with a checkpoint
    for each step and the result journaled before moving the branch."""

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
        """Drop retry selectors whose surviving work was included in this push, and
        the retry groups that selected these integrations."""
        configs = {
            name: config
            for name in names
            if (config := self.read(name)) and (config.get("retired_sources") or config.get("retry_groups"))
        }
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
            changed = config.pop("retry_groups", None) is not None
            if remaining != retired:
                if remaining:
                    config["retired_sources"] = remaining
                else:
                    del config["retired_sources"]
                changed = True
            if changed:
                self.save(name, config)

    def add_retry_groups(self, retry_groups: dict[str, dict[str, str]]) -> None:
        """Record groups that must still select these integrations on a retry."""
        for name, groups in retry_groups.items():
            config = self.read(name)
            assert config is not None
            if not groups.items() <= config.get("retry_groups", {}).items():
                config["retry_groups"] = {**config.get("retry_groups", {}), **groups}
                self.save(name, config)

    def replace_group(self, new: str, old: str | None = None) -> None:
        """A new group can reuse a name from a retired group. Only a rename
        carries over the old group's pending integration selectors."""

        def update(config: Integration) -> bool:
            changed = old is not None and config.get("group") == old
            if changed:
                config["group"] = new
            changed = Integrations.replace_retry_group(config, new, old) or changed
            for entry in config.get("retired_sources", {}).values():
                if old is not None and entry["group"] == old:
                    entry["group"] = new
                    changed = True
                groups = entry.get("integration_groups", [])
                if new in groups or old in groups:
                    entry["integration_groups"] = [new if group == old else group for group in groups if group != new]
                    changed = True
            return changed

        pending_names = {b for b, key in self.git.branch_config() if key == PENDING_KEY.lower()}
        for name in self.names() | pending_names:
            config = self.read(name)
            if config and update(config):
                self.save(name, config)
            build = self.pending(name)
            if build and update(build["integration"]):
                self.journal(name, build)

    @staticmethod
    def replace_retry_group(config: Integration, new: str, old: str | None) -> bool:
        """Carry old's retry group over to new on a rename, and drop new's
        otherwise. Says whether config changed."""
        retry_groups = config.get("retry_groups", {})
        if new not in retry_groups and old not in retry_groups:
            return False
        retry_remote = retry_groups.pop(old, None) if old is not None else None
        retry_groups.pop(new, None)
        if retry_remote is not None:
            retry_groups[new] = retry_remote
        if not retry_groups:
            del config["retry_groups"]
        return True

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

    def replay_path(self, name: str) -> pathlib.Path:
        return self.path(name).with_suffix(".replay")

    def write_progress(self, name: str, build: Build) -> None:
        """Write a small checkpoint without rewriting the immutable build inputs."""
        path = self.progress_path(name)
        staging = path.with_suffix(".tmp")
        staging.write_text(f"{build['step']} {build['head']}\n", encoding="utf-8")
        staging.replace(path)

    def remove_build(self, name: str, *, keep_worktree: bool = False) -> None:
        if not keep_worktree:
            self.remove_worktree(name)
        self.git.config_unset(name, PENDING_KEY)
        self.progress_path(name).unlink(missing_ok=True)
        self.progress_path(name).with_suffix(".tmp").unlink(missing_ok=True)
        self.replay_path(name).unlink(missing_ok=True)

    def remove_worktree(self, name: str, *, force: bool = True) -> None:
        path = self.path(name)
        # Git can remove a registered worktree even if its files are gone.
        registered = self.git.run("worktree", "list", "--porcelain", "-z").split("\0")
        if any(path == pathlib.Path(entry[9:]) for entry in registered if entry.startswith("worktree ")):
            self.core.emit(events.RemovingIntegrationWorktree(name))
            flags = ["--force"] if force else []
            self.git.run("worktree", "remove", *flags, str(path))

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
        self.remove_build(name, keep_worktree="replay" in build)
        return True

    def abort(self, name: str) -> None:
        if self.pending(name) is None:
            if not (self.path(name) / ".git").is_file():
                raise StackError(f"{name} has no integration build to abort")
            self.remove_worktree(name, force=False)
            return
        if not self.finish_interrupted(name):
            self.remove_build(name)

    @staticmethod
    def source_names(config: Integration, stacks: Stacks) -> list[str]:
        """Resolve a live group selector, retaining the order of surviving inputs."""
        recorded = [source["name"] for source in config["sources"]]
        if (group := config.get("group")) is None:
            return recorded
        selected = stacks.group_leaves(group, config["root"])
        selected_names = set(selected)
        return list(dict.fromkeys([*(name for name in recorded if name in selected_names), *selected]))

    def source_dependencies(self, config: Integration, stacks: Stacks) -> list[str]:
        """Include recorded inputs and group leaves that would join at the next refresh."""
        recorded = [source["name"] for source in config["sources"]]
        return list(dict.fromkeys([*recorded, *self.source_names(config, stacks)]))

    def selected_sources(self, config: Integration, stacks: Stacks) -> list[Source]:
        if "group" not in config:
            return config["sources"]
        known = {name: retired["source"] for name, retired in config.get("retired_sources", {}).items()}
        known.update({source["name"]: source for source in config["sources"]})
        return [
            known[name] if name in known else self.source(name, self.git.tip(name))
            for name in self.source_names(config, stacks)
        ]

    def group_inputs_changed(self, config: Integration, stacks: Stacks) -> bool:
        """Whether locally known active inputs would change with the same root tip."""
        if (root_tip := config["root_tip"]) is None:
            return True
        active: list[tuple[str, str]] = []
        for recorded in self.selected_sources(config, stacks):
            name = recorded["name"]
            tip = self.git.rev(f"refs/heads/{name}")
            if tip is None:
                return True
            if not self.local_landing(self.source(name, tip, recorded), root_tip):
                active.append((name, tip))
        return active != self.active_sources(config)

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

    def ensure_independent(self, name: str, stacks: Stacks, original_stacks: Stacks) -> None:
        """A newly registered integration cannot be an input of an existing one."""
        for other in self.names() - {name}:
            config = self.read(other)
            assert config is not None
            if config["root"] == name or any(
                stacks.root(source) == name for source in self.source_dependencies(config, original_stacks)
            ):
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
        prs: bool = True,
        previous: Integration | None = None,
        group: str | None = None,
    ) -> tuple[Integration, dict[str, str | None]]:
        root_tip = self.git.tip(root)
        observed: dict[str, str | None] = {root: root_tip}
        same_root = bool(previous and previous["root_tip"] == root_tip and previous.get("evidence_remote") == remote)
        inputs: list[Source] = []
        unresolved: list[Source] = []
        retired = previous.get("retired_sources", {}) if previous else {}
        pending = self.pending(name) if retired else None
        pending_sources = {source["name"]: source for source in pending["integration"]["sources"]} if pending else {}
        # A retired group leaf is no longer an input, but its retry selectors
        # are needed until publication succeeds.
        still_retired = retired.copy() if group is not None else {}
        for source in sources:
            branch = source["name"]
            live = self.git.rev(f"refs/heads/{branch}")
            observed[branch] = live
            entry = retired.get(branch)
            recorded = entry["source"] if entry and live is None else source
            current = self.source(branch, live or recorded["tip"], recorded)
            if entry:
                current = self.retired_input(source, current, pending_sources.get(branch))
            still_retired.pop(branch, None)
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
        self.look_up_landings(unresolved, remote, root_tip, fetch, prs=prs)
        for current in inputs:
            if observed[current["name"]] is None and not current["landing"]:
                raise StackError(
                    f"source {current['name']} is missing and its included tip {current['tip']} "
                    f"hasn't been proven to land on {root}. Replace the sources with "
                    f"`git stack integrate {name} <source>...`."
                )
        desired = Integration(root=root, sources=inputs, root_tip=root_tip, evidence_remote=remote, tip=root_tip)
        if group is not None:
            desired["group"] = group
        self.keep_retry_selectors(desired, still_retired, previous)
        return desired, observed

    @staticmethod
    def keep_retry_selectors(
        desired: Integration, retired: dict[str, RetiredSource], previous: Integration | None
    ) -> None:
        """Keep what a retry needs to select the integration until it's published."""
        if retired:
            desired["retired_sources"] = retired
        if previous and "retry_groups" in previous:
            desired["retry_groups"] = previous["retry_groups"]

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

    def evidence_remote(self, root: str, fallback: str) -> str:
        upstream = self.git.config_get(root, "remote")
        return upstream if upstream and upstream != "." else fallback

    def check_source_deletion(
        self,
        name: str,
        root: str,
        source: Source,
        remote: str,
        deleting_locally: bool,
        *,
        recorded: Source | None = None,
    ) -> bool:
        """Require exact landing before deletion and say whether GitHub proved it."""
        if not deleting_locally:
            return False
        root_tip = self.git.tip(root)
        if self.local_landing(source, root_tip):
            return False
        candidates = [source]
        if recorded is not None and recorded["tip"] == source["tip"]:
            previous = recorded.copy()
            # The upstream can change without invalidating proof for this tip.
            if self.local_landing(previous, root_tip):
                source.update(previous)
                return False
            if (previous["remote"], previous["head"]) != (source["remote"], source["head"]):
                candidates.append(previous)
        self.look_up_landings(candidates, self.evidence_remote(root, remote), root_tip, fetch=True, prs=True)
        if landed := next((candidate for candidate in candidates if candidate["landing"]), None):
            source.update(landed)
            return True
        raise StackError(
            f"can't delete {source['name']}: its exact tip has not been proven to land on {root} "
            f"for integration {name}. Retry without local deletion, or use --prs with the GitHub adapter for PR evidence."
        )

    def check_unselected_source_deletion(
        self, branch: str, remote: str, deleting_locally: bool, retiring_in: Iterable[str] = ()
    ) -> None:
        """Check the recorded source that a fixed integration will use after deletion."""
        if not deleting_locally:
            return
        for name in sorted(self.names().difference(retiring_in)):
            config = self.read(name)
            if config is None or "group" in config:
                continue
            source = next((source for source in config["sources"] if source["name"] == branch), None)
            if source is None:
                continue
            retired = config.get("retired_sources", {}).get(branch)
            included = (self.retired_input(source, retired["source"], None) if retired else source).copy()
            self.save_source_deletion_proof(name, config, included, remote, retired)

    def save_source_deletion_proof(
        self, name: str, config: Integration, included: Source, remote: str, retired: RetiredSource | None
    ) -> None:
        branch = included["name"]
        current = (
            self.source(branch, included["tip"], included) if self.git.tip(branch) == included["tip"] else included
        )
        if self.check_source_deletion(name, config["root"], current, remote, True, recorded=included):
            # The saved sources still describe the generated tip. A refresh
            # must see the change in active inputs even if the root didn't move.
            entry = retired or RetiredSource(source=current, parent=None, group=None, members=[])
            entry["source"] = current
            config.setdefault("retired_sources", {})[branch] = entry
            self.save(name, config)

    @staticmethod
    def same_evidence(old: Source, source: Source, fetch: bool) -> bool:
        return (
            "checked" in old
            and (not fetch or old["checked"])
            and 0 <= time.time() - old.get("checked_at", 0) < EVIDENCE_RECHECK_SECONDS
            and all(old[key] == source[key] for key in ("tip", "remote", "head"))
        )

    def look_up_landings(self, sources: list[Source], remote: str, root_tip: str, fetch: bool, *, prs: bool) -> None:
        if not (prs and sources):
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
                or saved.get("group") != desired.get("group")
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
            if previous.get("automatic"):
                desired["automatic"] = True
            if desired != previous:
                self.save(name, desired)
            return {**observed, name: expected}
        else:
            self.ensure_available_worktree(name)
            self.core.emit(events.RebuildingIntegration(name))
            start, replay = self.replay_plan(name, root_tip, inputs, previous, expected)
            desired["automatic"] = True
            build = Build(
                integration=desired,
                expected=expected,
                observed=observed,
                step=0,
                head=start,
                result=None,
                replay=replay is not None,
            )
            self.progress_path(name).unlink(missing_ok=True)
            if replay is not None:
                # A large source history would not fit in a git config argument,
                # especially on Windows. Pin the ordered plan on disk first.
                path = self.replay_path(name)
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(replay), encoding="utf-8")
            self.journal(name, build)
        self.ensure_available_worktree(name)
        result = self.apply(name, build, inputs)
        self.publish(name, build, result)
        return {**observed, name: result}

    def replay_plan(
        self, name: str, root: str, inputs: list[tuple[str, str]], previous: Integration | None, expected: str | None
    ) -> tuple[str, list[tuple[str, str]] | None]:
        """Append new commits if the previous inputs still form a basis of this build."""
        if not previous or not previous.get("automatic") or previous["root_tip"] != root or expected != previous["tip"]:
            return root, None
        old = self.active_sources(previous)
        if len(old) != len(inputs) or any(
            old_name != name or not self.git.is_ancestor(old_tip, tip)
            for (old_name, old_tip), (name, tip) in zip(old, inputs, strict=True)
        ):
            return root, None
        path = self.path(name)
        # Respect the merge policy as seen by the detached build worktree.
        if not (path / ".git").is_file() or self.git.at(path).ok("config", "--get", "merge.verifySignatures"):
            return root, None
        if not self.disjoint_source_paths(root, inputs):
            return root, None
        picks = [
            (name, commit)
            for (_, old_tip), (name, tip) in zip(old, inputs, strict=True)
            if old_tip != tip
            for commit in self.git.run("rev-list", "--reverse", f"{old_tip}..{tip}").splitlines()
        ]
        return previous["tip"], picks

    def disjoint_source_paths(self, root: str, inputs: list[tuple[str, str]]) -> bool:
        """Only replay independent linear histories that touch separate top-level paths."""
        claimed: set[str] = set()
        spellings: dict[str, str] = {}
        for _, tip in inputs:
            history = f"{root}..{tip}"
            if not self.git.is_ancestor(root, tip) or self.git.run("rev-list", "--max-count=1", "--merges", history):
                return False
            paths = self.git.run(
                "log",
                "--format=",
                "--no-show-signature",
                "--no-relative",
                "--no-renames",
                "--ignore-submodules=none",
                "--name-status",
                "-z",
                history,
                "--",
            ).split("\0")[1::2]
            if any(path.rsplit("/", 1)[-1].casefold() == ".gitattributes" for path in paths):
                return False
            # Treat a directory and all its descendants as one unit so a
            # directory rename can't relocate another source's files.
            top_level = {self.portable_path(path.split("/", 1)[0]) for path in paths if path}
            if top_level & claimed or not self.record_path_spellings(spellings, paths):
                return False
            claimed.update(top_level)
        # Git objects distinguish spellings that may name the same file on disk.
        entries = self.git.run("ls-tree", "-r", "-z", root).split("\0")
        root_paths = (entry.partition("\t")[2] for entry in entries if entry)
        return self.record_path_spellings(
            spellings, (path for path in root_paths if self.portable_path(path.split("/", 1)[0]) in claimed)
        )

    @staticmethod
    def portable_path(path: str) -> str:
        return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())

    @classmethod
    def record_path_spellings(cls, spellings: dict[str, str], paths: Iterable[str]) -> bool:
        """Reject aliases, including alternative spellings of a directory component."""
        for path in paths:
            while path:
                key = cls.portable_path(path)
                if spellings.setdefault(key, path) != path:
                    return False
                path = path.rpartition("/")[0]
        return True

    @staticmethod
    def active_sources(integration: Integration) -> list[tuple[str, str]]:
        return [(source["name"], source["tip"]) for source in integration["sources"] if not source["landing"]]

    def ensure_available_worktree(self, name: str) -> None:
        if (path := self.git.other_worktree_of(name, fresh=True)) is not None:
            raise StackError(f"can't move integration {name}: checked out in another worktree at {path}")

    def apply(self, name: str, build: Build, inputs: list[tuple[str, str]]) -> str:
        if build["result"] is not None:
            return build["result"]
        cherry_pick = build.get("replay", False)
        try:
            steps = (
                cast(list[tuple[str, str]], json.loads(self.replay_path(name).read_text(encoding="utf-8")))
                if cherry_pick
                else inputs
            )
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise StackError(
                f"integration replay plan is missing or invalid; run `git stack integrate --abort {name}`"
            ) from error
        if not steps:
            return build["head"]
        worktree = self.merge_worktree(name, build)
        for index in range(build["step"], len(steps)):
            source, tip = steps[index]
            before = build["head"]
            if worktree.rev("HEAD") != before:
                self.verify_completed_step(name, worktree, before, tip, cherry_pick)
                # An interrupted step can have been completed by hand.
                if build["integration"].pop("automatic", False):
                    self.journal(name, build)
            elif cherry_pick:
                self.pick_step(name, worktree, source, tip)
            elif not worktree.is_ancestor(tip, before):
                self.merge_step(name, worktree, source, tip)
            build["head"] = worktree.run("rev-parse", "HEAD")
            build["step"] = index + 1
            self.write_progress(name, build)
        if worktree.rev("HEAD") != build["head"] or worktree.run("status", "--porcelain"):
            raise StackError(f"integration worktree changed unexpectedly; run `git stack integrate --abort {name}`")
        return build["head"]

    def verify_completed_step(self, name: str, worktree: GitClient, before: str, tip: str, cherry_pick: bool) -> None:
        """An interrupted command may have committed before recording its checkpoint."""
        parents = worktree.run("show", "-s", "--format=%P", "HEAD").split()
        if cherry_pick:
            valid = parents == [before] and worktree.run("show", "-s", "--format=%B", "HEAD").endswith(
                f"(cherry picked from commit {tip})"
            )
        else:
            valid = parents == [before, tip]
        if not valid:
            raise StackError(
                f"integration worktree HEAD changed unexpectedly; run `git stack integrate --abort {name}`"
            )

    def merge_step(self, name: str, worktree: GitClient, source: str, tip: str) -> None:
        try:
            worktree.run("merge", "--no-ff", "--no-edit", "-m", f"Merge {source} into {name}", tip)
        except StackError as error:
            if worktree.run("ls-files", "-u"):
                raise self.conflict(name, source) from None
            raise StackError(
                f"merging {source} into {name} failed; run `git stack integrate --abort {name}`: {error}"
            ) from error

    def pick_step(self, name: str, worktree: GitClient, source: str, commit: str) -> None:
        try:
            worktree.run("cherry-pick", "-x", "--allow-empty", commit)
        except StackError as error:
            if worktree.run("ls-files", "-u"):
                raise self.conflict(name, source) from None
            # Git 2.38 stops when a patch's changes are already present.
            if worktree.operation_in_progress() and worktree.ok("diff", "--cached", "--quiet", "HEAD", "--"):
                worktree.run("cherry-pick", "--skip")
                return
            raise StackError(
                f"applying {source} to {name} failed; run `git stack integrate --abort {name}`: {error}"
            ) from error

    def merge_worktree(self, name: str, build: Build) -> GitClient:
        """Prepare the cached detached worktree once, then reopen it on retries."""
        path = self.path(name)
        path.parent.mkdir(exist_ok=True)
        worktree = self.git.at(path)
        worktree.passthrough_stdout = self.git.passthrough_stdout
        cached = (path / ".git").is_file()
        if not cached:
            if build["step"]:
                raise StackError(f"integration worktree is missing; run `git stack integrate --abort {name}`")
            self.git.run("worktree", "add", "--quiet", "--detach", str(path), build["head"])
        if worktree.current_branch() is not None:
            raise StackError(f"integration worktree is no longer detached; run `git stack integrate --abort {name}`")
        if worktree.operation_in_progress():
            if build["integration"].pop("automatic", False):
                self.journal(name, build)
            command = "cherry-pick" if build.get("replay") else "merge"
            raise StackError(
                f"integration worktree has a stopped operation in {path}. "
                f'Run `git -C "{path}" {command} --continue` to keep its changes, '
                f"or discard it with `git stack integrate --abort {name}`."
            )
        if "replay" in build and not self.progress_path(name).exists():
            if cached and worktree.run("status", "--porcelain"):
                raise StackError(f"integration worktree changed unexpectedly in {path}")
            if worktree.rev("HEAD") != build["head"]:
                worktree.run("switch", "--quiet", "--detach", build["head"])
            self.write_progress(name, build)
        return worktree

    def preserve_retired_landings(self, name: str, build: Build) -> None:
        """Keep landing evidence for removed inputs even when a build cannot publish."""
        previous = self.read(name)
        if previous is None or not (retired := previous.get("retired_sources")):
            return
        sources = {source["name"]: source for source in build["integration"]["sources"]}
        changed = False
        for branch, record in retired.items():
            old = record["source"]
            source = self.retired_input(old, old, sources.get(branch))
            if source != old:
                record["source"] = source
                changed = True
        if changed:
            self.save(name, previous)

    def conflict(self, name: str, source: str) -> StackError:
        build = self.pending(name)
        against = "the integration root"
        if build:
            against = build["integration"]["root"]
            self.preserve_retired_landings(name, build)
        keep = build is not None and self.read(name) is not None and self.abort_conflicting_step(name, build)
        self.remove_build(name, keep_worktree=keep)
        return StackError(
            f"integration build for {name} aborted: changes from {source} conflict with {against} or another source. "
            f"Run `git stack restack {source} --onto <dependency>`, resolve the conflict there, and rerun this command."
        )

    def abort_conflicting_step(self, name: str, build: Build) -> bool:
        """Keep the registered integration's worktree only after a clean abort."""
        worktree = self.git.at(self.path(name))
        command = "cherry-pick" if build.get("replay") else "merge"
        try:
            return (
                worktree.ok(command, "--abort")
                and worktree.rev("HEAD") == build["head"]
                and worktree.current_branch() is None
                and not worktree.operation_in_progress()
                and not worktree.run("status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
            )
        except StackError:
            return False

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
        self.remove_build(name, keep_worktree="replay" in build)

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
