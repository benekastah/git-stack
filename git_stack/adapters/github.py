# pyright: reportUnknownVariableType=false
import enum
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Self

from git_stack import events, stack_section
from git_stack.adapters.git import GitAdapter, PullRequestLink, PullRequestState, PushRetargets
from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.performance import measure_gh
from git_stack.stacks import Stacks

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
# Only unambiguous SSH repository paths, with GitHub's explicit login user.
SSH_REPO_URL = re.compile(
    r"(?:(?P<scheme>ssh://))?(?P<destination>git@[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?(scheme)(?::(?P<port>[0-9]+))?/|:)"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*)"
)
GITHUB_SSH_ENDPOINTS = {("github.com", "22"), ("ssh.github.com", "443")}
SSH_REDIRECT_SETTINGS = (
    "proxycommand",
    "proxyjump",
    "remotecommand",
    "hostkeyalias",
    "controlpath",
    "knownhostscommand",
)
# What GitHub allows in a username, so one can't add to a search query.
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
# A pull request's number, and the path of its page (or one of its tabs, e.g.
# /files) on GitHub.
NUMBER = re.compile(r"[0-9]+")
PULL_REQUEST_PATH = re.compile(r"/(?P<owner>[^/]+)/(?P<name>[^/]+)/pull/(?P<number>[0-9]+)(?:/.*)?")
PR_FIELDS = "number state isDraft isCrossRepository headRefOid baseRefName mergeCommit { oid }"
# How many of a branch's pull requests to look at, newest first. More than one
# only if its name was reused, or a pull request was closed and replaced.
PRS_PER_BRANCH = 10
QUEUE_FIELDS = "number state isInMergeQueue"
REVIEW_FIELDS = (
    "isDraft baseRefName reviewDecision mergeStateStatus "
    "latestOpinionatedReviews(first: 20) { nodes { state } pageInfo { hasNextPage } }"
)
DESCRIPTION_BATCH_SIZE = 10
# Where GitHub looks for a repository's pull request template, matching names
# case-insensitively. GitHub doesn't say which wins if there are several.
TEMPLATE_DIRECTORIES = (".github", "", "docs")
TEMPLATE_NAMES = ("pull_request_template.md", "pull_request_template.txt")
# Whether prs links each linear stack's pull requests into a stack on GitHub,
# or adds to one that's still a path through a stack that branches out
# (default: true), where the repository has GitHub's stacked pull requests.
GITHUB_STACKS_SETTING = "stack.githubStacks"
DEPENDENTS_FIELD = "dependents: pullRequests(baseRefName: $base, states: OPEN, first: 1) { nodes { number } }"
# A pull request's stack on GitHub, as GraphQL gives it. GitHub only allows a
# handful of pull requests in one.
STACK_FIELDS = "stack { number entries(first: 100) { nodes { position pullRequest { number state } } } }"
# The bases a pull request's base was changed from, oldest first: by GitHub,
# when the pull request it was stacked on merged and its branch was deleted
# (oldBase), or by hand or `git stack push` (previousRefName). Only the last
# few matter, for the merged pull request it was last stacked on.
BASE_CHANGES = (
    f"timelineItems(last: {PRS_PER_BRANCH}, itemTypes: [BASE_REF_CHANGED_EVENT, AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT]) "
    "{ nodes { ... on BaseRefChangedEvent { previousRefName createdAt } "
    "... on AutomaticBaseChangeSucceededEvent { oldBase createdAt } } }"
)
# What GitHub says when asked to change the base of a pull request in a stack.
STACKED_BASE_ERROR = "Cannot change the base branch because the pull request is part of a stack"


def parse_json(out: str) -> object:
    try:
        parsed: object = json.loads(out)
    except ValueError as e:
        raise StackError(f"unexpected response from GitHub: {e}") from e
    return parsed


def field[T](data: object, key: str, kind: type[T]) -> T:
    """data[key], checking that data is a JSON object and the value a kind."""
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, kind):
        raise StackError(f"unexpected response from GitHub: expected {key} to be a {kind.__name__} in {data!r}")
    return value


def optional_field[T](data: object, key: str, kind: type[T]) -> T | None:
    """Like field, but a missing or null value is None."""
    if isinstance(data, dict) and data.get(key) is None:
        return None
    return field(data, key, kind)


class GitHubState(enum.Enum):
    """A pull request's state, as GitHub's API gives it. (PullRequestState is
    what info shows, which tells drafts apart.)"""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


def github_state(data: object) -> GitHubState:
    """The "state" field of data, a pull request from GitHub's API."""
    value = field(data, "state", str)
    try:
        return GitHubState(value)
    except ValueError:
        raise StackError(f"GitHub gave a pull request state git-stack doesn't know: {value}") from None


@dataclass(frozen=True)
class PullRequest:
    """A pull request, with the fields in PR_FIELDS."""

    number: int
    state: GitHubState
    draft: bool
    cross_repository: bool
    head_oid: str
    base: str
    merge_oid: str | None

    @classmethod
    def from_json(cls, data: object) -> Self:
        merge_commit = optional_field(data, "mergeCommit", dict)
        return cls(
            number=field(data, "number", int),
            state=github_state(data),
            draft=field(data, "isDraft", bool),
            cross_repository=field(data, "isCrossRepository", bool),
            head_oid=field(data, "headRefOid", str),
            base=field(data, "baseRefName", str),
            merge_oid=field(merge_commit, "oid", str) if merge_commit is not None else None,
        )


@dataclass(frozen=True)
class GitHubStack:
    """A stack of pull requests on GitHub, as STACK_FIELDS gives it."""

    number: int
    # (number, state) of each of its pull requests, bottom first.
    entries: tuple[tuple[int, GitHubState], ...]

    def matches_after_closures(self, expected: GitHubStack, closed: set[int]) -> bool:
        """GitHub can retain or remove closed entries from the stack."""
        return (
            self.number == expected.number
            and all(state is GitHubState.CLOSED for n, state in self.entries if n in closed)
            and tuple(entry for entry in self.entries if entry[0] not in closed)
            == tuple(entry for entry in expected.entries if entry[0] not in closed)
        )

    @classmethod
    def from_json(cls, data: object) -> Self:
        nodes = sorted(field(field(data, "entries", dict), "nodes", list), key=lambda n: field(n, "position", int))
        prs = [field(node, "pullRequest", dict) for node in nodes]
        return cls(
            field(data, "number", int),
            tuple((field(pr, "number", int), github_state(pr)) for pr in prs),
        )


@dataclass(frozen=True)
class OrphanedPullRequest:
    """The identity and refs shown before confirming a draft closure."""

    number: int
    stack: GitHubStack
    url: str
    head: str
    base: str
    head_oid: str

    @property
    def description(self) -> str:
        return f"#{self.number} {self.url} ({self.head} → {self.base})"


type ConfirmOrphanClosures = Callable[[list[OrphanedPullRequest]], bool]


@dataclass(frozen=True)
class OpenPullRequest:
    """An open (or draft) pull request of head, a branch in the repository,
    into base."""

    number: int
    head: str
    base: str
    # Its author's login, or None if their account was deleted.
    author: str | None = None
    # Whether it's from a fork rather than a branch in the repository.
    fork: bool = False


def login_of(user: object) -> str | None:
    """The login of a GraphQL user (an author), or None for a deleted account."""
    return None if user is None else field(user, "login", str)


def check_github_login(login: str) -> None:
    """Raise StackError if login can't be a GitHub username. It's checked
    before anything is asked of GitHub, so the error isn't taken for gh's."""
    if not GITHUB_LOGIN.fullmatch(login):
        raise StackError(f"{login} isn't a GitHub username")


@dataclass(frozen=True)
class PRProtection:
    """Consent for changes to existing pull requests on this invocation."""

    force: bool = False
    protect: bool = False
    allowed: frozenset[int | str] = frozenset()

    @property
    def fields(self) -> str:
        return f"{QUEUE_FIELDS} {REVIEW_FIELDS}" if self.protect else QUEUE_FIELDS

    def check(self, pr: object, action: str, *, repo: str, new_base: str | None = None, foreign: bool = False) -> None:
        if field(pr, "state", str) != GitHubState.OPEN.value:
            return
        number = field(pr, "number", int)
        queued = field(pr, "isInMergeQueue", bool)
        on_main = self.protect and not field(pr, "isDraft", bool) and field(pr, "baseRefName", str) == "main"
        ready = on_main and field(pr, "mergeStateStatus", str) in ("CLEAN", "HAS_HOOKS")
        if on_main and (ready or queued) and new_base is not None and new_base != "main":
            reason = "in the merge queue" if queued else "ready to merge"
            raise StackError(f"can't retarget PR #{number} away from main: it is {reason}")
        qualified = f"{repo.casefold()}#{number}"
        approval = qualified if foreign else str(number)
        label = f"{repo}#{number}" if foreign else f"#{number}"
        allowed = qualified in self.allowed or (number in self.allowed and not foreign)
        consent = self.protect and not allowed
        if queued and (not self.force or consent):
            options = f"--force --allow-pr-update {approval}" if consent else "--force"
            raise StackError(f"can't {action} PR {label}: it is in the merge queue; retry with {options} if authorized")
        if consent:
            status = "approved" if self.has_approvals(pr) else "ready to merge" if ready else None
            if status is not None:
                raise StackError(
                    f"can't {action} PR {label}: it is {status} and the change may clear reviews; "
                    f"get permission, then pass --allow-pr-update {approval}"
                )

    @staticmethod
    def has_approvals(pr: object) -> bool:
        if optional_field(pr, "reviewDecision", str) == "APPROVED":
            return True
        reviews = field(pr, "latestOpinionatedReviews", dict)
        if any(field(review, "state", str) == "APPROVED" for review in field(reviews, "nodes", list)):
            return True
        # A long review history can hide an approval outside this page.
        return field(field(reviews, "pageInfo", dict), "hasNextPage", bool)


class GitHubAdapter(GitAdapter):
    """Asks GitHub, through the gh CLI, whether each branch's pull request was
    merged, and keeps pull requests' bases in step with the stack when
    branches are pushed.

    Branches without a merged pull request (for example, one merged into its
    parent locally) are checked like GitAdapter does, unless their parent is
    the repository's default branch, which we assume nobody merges into
    locally.
    """

    def __init__(
        self,
        core: StackCore,
        remote: str,
        host: str,
        owner: str,
        name: str,
        fetch: bool,
        lookup_branches: Iterable[str] | None = None,
    ) -> None:
        super().__init__(core)
        self.remote = remote
        self.fetch = fetch
        self._lookup_branches = tuple(lookup_branches) if lookup_branches is not None else None
        self.host, self.owner, self.name = host, owner, name
        self.repo = f"{host}/{owner}/{name}"
        self._pull_requests: dict[str, PullRequest] | None = None
        self._source_pages: dict[tuple[str, str | None], tuple[list[PullRequest], str | None]] = {}
        # So branches checked in parallel (as info does) look them up only once.
        self._pull_requests_lock = threading.Lock()
        self._default_branch: str | None = None
        self._viewer: str | None = None
        self.protection = PRProtection()
        self.confirm_orphan_closures: ConfirmOrphanClosures | None = None

    def gh(self, *args: str, input: str | None = None, partial_errors: list[str] | None = None) -> str:
        # As bytes, so Windows doesn't turn the \n in input (e.g. a pull
        # request's description) into \r\n.
        with measure_gh(args):
            result = subprocess.run(
                ["gh", *args], capture_output=True, input=None if input is None else input.encode("utf-8"), check=False
            )
        if result.returncode:
            error = result.stderr.decode("utf-8", "replace").strip()
            if partial_errors is None or not result.stdout:
                raise StackError(f"gh {' '.join(args)} failed:\n{error}")
            if error:
                partial_errors.append(error)
        return result.stdout.decode("utf-8", "replace" if result.returncode else "strict").strip()

    def protect_ref_updates(self, refs: Iterable[str], *, allow_number: bool = True) -> None:
        """Check open PRs whose head or base ref the push would change."""
        if self.protection.force and not self.protection.protect:
            return
        targets = [(ref, side) for ref in sorted(set(refs)) for side in ("head", "base")]
        for start in range(0, len(targets), PRS_PER_BRANCH):
            self.protect_ref_batch(targets[start : start + PRS_PER_BRANCH], allow_number=allow_number)

    def protect_ref_batch(self, targets: list[tuple[str, str]], *, allow_number: bool) -> None:
        """Check a bounded batch of ref connections, following their independent cursors."""
        pending: dict[tuple[str, str], str | None] = dict.fromkeys(targets)
        seen: set[tuple[str, str, str]] = set()
        while pending:
            variables = ["$owner: String!", "$name: String!"]
            args = ["-f", f"owner={self.owner}", "-f", f"name={self.name}"]
            connections: list[str] = []
            for i, ((ref, side), after) in enumerate(pending.items()):
                alias = f"ref{i}"
                variables.append(f"${alias}: String!")
                name = f"refs/heads/{ref}" if side == "head" else ref
                args.extend(("-f", f"{alias}={name}"))
                cursor = ""
                if after is not None:
                    variables.append(f"$cursor{i}: String!")
                    args.extend(("-f", f"cursor{i}={after}"))
                    cursor = f", after: $cursor{i}"
                pagination = f"states: OPEN, first: {PRS_PER_BRANCH}{cursor}"
                selection = (
                    f"{{ nodes {{ {self.protection.fields} repository {{ nameWithOwner }} }} "
                    "pageInfo { hasNextPage endCursor } }"
                )
                if side == "head":
                    connections.append(
                        f"{alias}: ref(qualifiedName: ${alias}) {{ associatedPullRequests({pagination}) {selection} }}"
                    )
                else:
                    connections.append(f"{alias}: pullRequests(baseRefName: ${alias}, {pagination}) {selection}")
            query = (
                f"query({', '.join(variables)}) {{ repository(owner: $owner, name: $name) "
                f"{{ {' '.join(connections)} }} }}"
            )
            found = field(
                field(
                    parse_json(self.gh("api", "graphql", "--hostname", self.host, *args, "-f", f"query={query}")),
                    "data",
                    dict,
                ),
                "repository",
                dict,
            )
            remaining: dict[tuple[str, str], str | None] = {}
            for i, (ref, side) in enumerate(pending):
                node = optional_field(found, f"ref{i}", dict)
                if side == "head" and node is None:
                    continue  # The destination ref was deleted since the fetch.
                page = field(node, "associatedPullRequests", dict) if side == "head" else field(found, f"ref{i}", dict)
                for pr in field(page, "nodes", list):
                    repo = field(field(pr, "repository", dict), "nameWithOwner", str)
                    own_repo = repo.casefold() == f"{self.owner}/{self.name}".casefold()
                    self.protection.check(
                        pr,
                        f"update {self.remote}/{ref}, the {side} of",
                        repo=repo,
                        foreign=not (own_repo and allow_number),
                    )
                info = field(page, "pageInfo", dict)
                if field(info, "hasNextPage", bool):
                    after = field(info, "endCursor", str)
                    if (ref, side, after) in seen:
                        raise StackError(f"GitHub repeated a pull request page for {side} {ref}")
                    seen.add((ref, side, after))
                    remaining[ref, side] = after
            pending = remaining

    def protect_edits(self, changes: dict[int, str | None], action: str) -> None:
        """Check the live state of PRs about to be edited. A value is the new base, if any."""
        if self.protection.force and not self.protection.protect:
            return
        for number, pr in self.pull_request_fields(changes, self.protection.fields).items():
            self.protection.check(pr, action, repo=f"{self.owner}/{self.name}", new_base=changes[number])

    def protect_retargets(self, branches: Iterable[str], stacks: Stacks) -> None:
        self.protect_edits(self.retarget_edits(branches, stacks), "retarget")

    def retarget_edits(self, branches: Iterable[str], stacks: Stacks) -> dict[int, str | None]:
        prs = self.pull_requests()
        changes: dict[int, str | None] = {}
        for branch in branches:
            if (pr := prs.get(branch)) is not None and pr.state is GitHubState.OPEN:
                base = self.git.remote_branch_name(stacks.parent(branch), self.remote)
                if base != pr.base:
                    changes[pr.number] = base
        return changes

    def protect_pr_opening(self, branches: list[str], opening: Iterable[str], stacks: Stacks) -> None:
        """New PRs can change the stack sections or membership of existing ones."""
        prs = self.pull_requests()
        affected = {member for b in opening for member in stacks.stack_of(b)}
        self.protect_edits(
            {prs[b].number: None for b in branches if b in affected and b in prs and prs[b].state is GitHubState.OPEN},
            "change the stack of",
        )

    def pull_requests(self) -> dict[str, PullRequest]:
        """{branch: pull request} for tracked branches with a pull request.

        Only branches whose configured upstream is on this remote can have one.
        Pull requests are looked up by the upstream's branch name.

        Looked up with one API call per command. If that fails, or GitHub's
        answer isn't what we asked for, emits PullRequestLookupFailed and
        returns {}, so merges are only detected locally, unless
        ensure_available was called first.
        """
        with self._pull_requests_lock:
            if self._pull_requests is None:
                self._pull_requests = {}
                try:
                    self._pull_requests = self.look_up_pull_requests()
                except StackError as e:
                    self.core.emit(events.PullRequestLookupFailed(str(e)))
            return self._pull_requests

    def ensure_available(self) -> None:
        """Look up pull requests now, failing if GitHub can't be asked (e.g. gh
        isn't logged in, or its token isn't authorized for the organization's
        SSO). Without them, merges detected locally can be wrong both ways."""
        if self._pull_requests is not None:
            return
        try:
            self._pull_requests = self.look_up_pull_requests()
        except StackError as e:
            raise StackError(
                f"couldn't look up pull requests on GitHub; check `gh auth status` (and, for "
                f"an organization with SSO, that gh's token is authorized for it): {e}"
            ) from e

    def look_up_pull_requests(self) -> dict[str, PullRequest]:
        candidates = self.core.stacks() if self._lookup_branches is None else self._lookup_branches
        branches = [b for b in candidates if self.git.config_get(b, "remote") == self.remote]
        if not branches:
            return {}
        # Branch names are passed as variables, so they needn't be escaped.
        fields = " ".join(
            f"b{i}: pullRequests(headRefName: $b{i}, first: {PRS_PER_BRANCH}, "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}) "
            f"{{ nodes {{ {PR_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }}"
            for i in range(len(branches))
        )
        variables = "".join(f", $b{i}: String!" for i in range(len(branches)))
        query = (
            f"query($owner: String!, $name: String!{variables}) {{ repository(owner: $owner, name: $name) "
            f"{{ defaultBranchRef {{ name }} {fields} }} }}"
        )
        args = [
            "api",
            "graphql",
            "--hostname",
            self.host,
            "-f",
            f"owner={self.owner}",
            "-f",
            f"name={self.name}",
            "-f",
            f"query={query}",
        ]
        for i, branch in enumerate(branches):
            args += ["-f", f"b{i}={self.git.remote_branch_name(branch, self.remote)}"]
        repository = field(field(parse_json(self.gh(*args)), "data", dict), "repository", dict)
        default_branch = optional_field(repository, "defaultBranchRef", dict)
        default_name = field(default_branch, "name", str) if default_branch else None
        pull_requests = {}
        for i, branch in enumerate(branches):
            if self.git.remote_branch_name(branch, self.remote) == default_name:
                continue
            page = field(repository, f"b{i}", dict)
            prs = [PullRequest.from_json(node) for node in field(page, "nodes", list)]
            cursors: set[str] = set()
            while not any(pr.state is GitHubState.OPEN and not pr.cross_repository for pr in prs):
                page_info = field(page, "pageInfo", dict)
                if not field(page_info, "hasNextPage", bool):
                    break
                after = field(page_info, "endCursor", str)
                if after in cursors:
                    raise StackError(f"GitHub repeated a pull request page for {branch}")
                cursors.add(after)
                query = (
                    "query($owner: String!, $name: String!, $b0: String!, $after: String!) "
                    "{ repository(owner: $owner, name: $name) "
                    f"{{ b0: pullRequests(headRefName: $b0, first: {PRS_PER_BRANCH}, after: $after, "
                    f"orderBy: {{field: CREATED_AT, direction: DESC}}) "
                    f"{{ nodes {{ {PR_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }} }} }}"
                )
                response = parse_json(
                    self.gh(
                        "api",
                        "graphql",
                        "--hostname",
                        self.host,
                        "-f",
                        f"owner={self.owner}",
                        "-f",
                        f"name={self.name}",
                        "-f",
                        f"b0={self.git.remote_branch_name(branch, self.remote)}",
                        "-f",
                        f"after={after}",
                        "-f",
                        f"query={query}",
                    )
                )
                page = field(field(field(response, "data", dict), "repository", dict), "b0", dict)
                prs.extend(PullRequest.from_json(node) for node in field(page, "nodes", list))
            pr = self.pick_pull_request(branch, prs)
            if pr is not None:
                pull_requests[branch] = pr
        self._default_branch = default_name
        return pull_requests

    def pick_pull_request(self, branch: str, prs: list[PullRequest]) -> PullRequest | None:
        """branch's pull request among prs (newest first): its open one, or else
        its newest merged one.

        Closed pull requests don't count, nor do ones from forks, which only
        share the branch's name. Nor do merged ones whose head doesn't descend
        from the branch's base, which are most likely for an earlier branch of
        the same name.
        """
        prs = [pr for pr in prs if not pr.cross_repository]
        open_pr = next((pr for pr in prs if pr.state is GitHubState.OPEN), None)
        if open_pr is not None:
            return open_pr
        base = self.git.config_get(branch, BASE_KEY)
        if base is None:
            return None
        merged = [pr for pr in prs if pr.state is GitHubState.MERGED]
        self.fetch_missing_heads((pr.number, pr.head_oid) for pr in merged)
        return next(
            (pr for pr in merged if self.git.rev(pr.head_oid) and self.git.is_ancestor(base, pr.head_oid)), None
        )

    def fetch_missing_heads(self, heads: Iterable[tuple[int, str]]) -> None:
        """Fetch the head commits of pull requests, heads (number, head commit),
        that we don't have, which GitHub keeps after their branches are
        deleted.

        A pull request's last commit can be made on GitHub (e.g. by "Update
        branch") right before it's merged and its branch deleted, so we never
        fetch it. Without it, we can't tell the pull request is the branch's.
        If the fetch fails, or the adapter may not fetch, those pull requests
        are just ignored.
        """
        if not self.fetch:
            return
        refs = [f"refs/pull/{number}/head" for number, oid in heads if not self.git.rev(oid)]
        if refs:
            self.git.run("fetch", "--quiet", "--no-write-fetch-head", self.remote, *refs, check=False)

    @property
    def default_branch(self) -> str | None:
        """The repository's default branch, once pull requests were looked up
        (by open_pull_requests, open_pull_request_by_number or pull_requests),
        or None."""
        return self._default_branch

    def default_branch_for_push(self) -> str | None:
        """The API's default branch when Git fetches and pushes to that repository."""
        if not self.default_branch or self.host != "github.com":
            return None
        url = self.git.direct_remote_url(self.remote)
        expected = f"https://{self.repo}"
        if url in (expected, f"{expected}.git") or url and self.matches_direct_ssh(url):
            return self.default_branch
        return None

    def matches_direct_ssh(self, url: str) -> bool:
        match = SSH_REPO_URL.fullmatch(url)
        if match is None or not self.uses_openssh():
            return False
        destination = match["destination"]
        repo = match["repo"]
        if repo.removesuffix(".git").casefold() != f"{self.owner}/{self.name}".casefold():
            return False
        protocol = self.git.config_value("protocol.version") or "2"
        if protocol not in ("0", "1", "2"):
            return False
        port_args = ("-p", match["port"]) if match["port"] else ()
        path = f"/{repo}" if match["scheme"] else repo
        env = self.git_ssh_environment()
        if env is None:
            return False
        cwd = self.git.toplevel()
        program = git_ssh_program(cwd, env["PATH"])
        if program is None:
            return False
        # OpenSSH Match blocks can select settings by service or environment.
        # Git downgrades protocol v2 to v0 for receive-pack.
        return self.github_ssh_service(program, destination, port_args, path, "upload", protocol, cwd, env) and (
            self.github_ssh_service(
                program, destination, port_args, path, "receive", "0" if protocol == "2" else protocol, cwd, env
            )
        )

    def git_ssh_environment(self) -> dict[str, str] | None:
        """Use Git's executable lookup and the environment it passes to SSH."""
        env = {**os.environ}
        if "PATH" not in env or "GIT_TEST_PROTOCOL_VERSION" in env:
            return None
        try:
            exec_path = self.git.run("--exec-path")
            if not pathlib.Path(exec_path).is_absolute():
                return None
            local_names = self.git.run("rev-parse", "--local-env-vars")
        except StackError:
            return None
        for key in local_names.splitlines():
            env.pop(key, None)
        env["GIT_EXEC_PATH"] = exec_path
        env["PATH"] = os.pathsep.join((exec_path, env["PATH"]))
        return env

    @staticmethod
    def github_ssh_service(
        program: pathlib.Path,
        destination: str,
        port_args: tuple[str, ...],
        path: str,
        service: str,
        protocol: str,
        cwd: pathlib.Path,
        env: dict[str, str],
    ) -> bool:
        options = ("-o", "SendEnv=GIT_PROTOCOL") if protocol != "0" else ()
        environment = {**env, "GIT_PROTOCOL": f"version={protocol}"} if options else env
        config = ssh_configuration(
            *options, *port_args, destination, f"git-{service}-pack '{path}'", cwd=cwd, env=environment, program=program
        )
        return direct_github_ssh(config)

    def uses_openssh(self) -> bool:
        variant = os.environ.get("GIT_SSH_VARIANT") or self.git.config_value("ssh.variant") or "auto"
        overrides = (
            "core.sshCommand",
            *(f"remote.{self.remote}.{key}" for key in ("vcs", "uploadpack", "receivepack")),
        )
        # Git for Windows can adjust PATH inside its bundled runtime.
        return (
            sys.platform != "win32"
            and variant in ("auto", "ssh")
            and not {"GIT_SSH", "GIT_SSH_COMMAND"} & os.environ.keys()
            and all(self.git.config_value(key) is None for key in overrides)
        )

    @property
    def viewer(self) -> str | None:
        """The gh user's login, once open_pull_requests or
        open_pull_request_by_number looked it up, or None."""
        return self._viewer

    def has_merged_pull_requests_into(self, branch: str) -> bool:
        """Whether a branch has served as the base of a merged pull request."""
        query = (
            "query($owner: String!, $name: String!, $base: String!) "
            "{ repository(owner: $owner, name: $name) "
            "{ pullRequests(baseRefName: $base, states: MERGED, first: 1) { nodes { number } } } }"
        )
        args = [
            "api",
            "graphql",
            "--hostname",
            self.host,
            "-f",
            f"owner={self.owner}",
            "-f",
            f"name={self.name}",
            "-f",
            f"base={branch}",
            "-f",
            f"query={query}",
        ]
        repository = field(field(parse_json(self.gh(*args)), "data", dict), "repository", dict)
        return bool(field(field(repository, "pullRequests", dict), "nodes", list))

    def open_pull_requests(self, author: str = "@me") -> list[OpenPullRequest]:
        """author's open pull requests (by login; default: the gh user's),
        drafts included, from branches in the repository (not forks). Also
        looks up the default branch and the gh user's login."""
        query = (
            "query($q: String!, $after: String, $owner: String!, $name: String!) "
            "{ viewer { login } repository(owner: $owner, name: $name) { defaultBranchRef { name } } "
            "search(query: $q, type: ISSUE, first: 100, after: $after) "
            "{ pageInfo { hasNextPage endCursor } "
            "nodes { ... on PullRequest { number headRefName baseRefName isCrossRepository author { login } } } } }"
        )
        prs: list[OpenPullRequest] = []
        after = None
        while True:
            args = [
                "api",
                "graphql",
                "--hostname",
                self.host,
                "-f",
                f"q=repo:{self.owner}/{self.name} is:pr is:open author:{author}",
                "-f",
                f"owner={self.owner}",
                "-f",
                f"name={self.name}",
                "-f",
                f"query={query}",
            ]
            if after is not None:
                args += ["-f", f"after={after}"]
            data = field(parse_json(self.gh(*args)), "data", dict)
            self.read_viewer_and_default_branch(data)
            search = field(data, "search", dict)
            prs.extend(
                OpenPullRequest(
                    field(node, "number", int),
                    field(node, "headRefName", str),
                    field(node, "baseRefName", str),
                    login_of(optional_field(node, "author", dict)),
                )
                for node in field(search, "nodes", list)
                if not field(node, "isCrossRepository", bool)
            )
            page = field(search, "pageInfo", dict)
            if not field(page, "hasNextPage", bool):
                return prs
            after = field(page, "endCursor", str)

    def read_viewer_and_default_branch(self, data: dict[str, object]) -> None:
        """Keep the gh user's login and the default branch, from the data of a
        query that asks for them."""
        self._viewer = field(field(data, "viewer", dict), "login", str)
        default_branch = optional_field(field(data, "repository", dict), "defaultBranchRef", dict)
        self._default_branch = field(default_branch, "name", str) if default_branch else None

    def pull_request_number(self, pr: str) -> int:
        """The number of the pull request pr names: its number, or its URL on
        GitHub (e.g. https://github.com/acme/widgets/pull/12/files). Raises
        StackError for anything else, including another repository's URL."""
        if NUMBER.fullmatch(pr):
            return int(pr)
        url = urllib.parse.urlsplit(pr)
        match = PULL_REQUEST_PATH.fullmatch(url.path)
        if (
            url.scheme in ("http", "https")
            and url.hostname == self.host.lower()
            and match is not None
            and (match["owner"].lower(), match["name"].lower()) == (self.owner.lower(), self.name.lower())
        ):
            return int(match["number"])
        raise StackError(f"{pr} isn't a pull request number, or the URL of a pull request in {self.repo}")

    def open_pull_request_by_number(self, number: int) -> OpenPullRequest | None:
        """Pull request number, or None if it isn't open (or doesn't exist).
        Also looks up the default branch and the gh user's login."""
        query = (
            "query($owner: String!, $name: String!, $number: Int!) "
            "{ viewer { login } repository(owner: $owner, name: $name) { defaultBranchRef { name } "
            "pullRequest(number: $number) "
            "{ number state headRefName baseRefName isCrossRepository author { login } } } }"
        )
        args = ["api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        args += ["-F", f"number={number}", "-f", f"query={query}"]
        try:
            output = self.gh(*args)
        except StackError as e:
            # GraphQL answers a number that isn't a pull request (e.g. an
            # issue's) with an error.
            if "Could not resolve to a PullRequest" in str(e):
                return None
            raise
        data = field(parse_json(output), "data", dict)
        self.read_viewer_and_default_branch(data)
        pr = optional_field(field(data, "repository", dict), "pullRequest", dict)
        if pr is None or github_state(pr) is not GitHubState.OPEN:
            return None
        return OpenPullRequest(
            field(pr, "number", int),
            field(pr, "headRefName", str),
            field(pr, "baseRefName", str),
            login_of(optional_field(pr, "author", dict)),
            fork=field(pr, "isCrossRepository", bool),
        )

    def prefetch(self) -> None:
        self.pull_requests()

    def pull_request_link(self, branch: str) -> PullRequestLink | None:
        pr = self.pull_requests().get(branch)
        if pr is None:
            return None
        state = (
            PullRequestState.MERGED
            if pr.state is GitHubState.MERGED
            else PullRequestState.DRAFT
            if pr.draft
            else PullRequestState.OPEN
        )
        return PullRequestLink(pr.number, f"https://{self.host}/{self.owner}/{self.name}/pull/{pr.number}", state)

    def deleted_parent_merged_into(self, branch: str, parent: str) -> str | None:
        """The local branch of the one branch's pull request merges into now,
        if parent's pull request into it was merged: GitHub retargets the pull
        requests on a merged branch when it's deleted. A pull request
        retargeted by hand, over a parent that wasn't merged, doesn't count,
        nor does a merged pull request of an earlier branch of parent's name:
        its head must contain branch's base, the parent's commit branch was
        built on. A head we never fetched (e.g. a commit "Update branch" made
        right before the merge) is fetched first."""
        git = self.git
        pr = self.pull_requests().get(branch)
        base = git.config_get(branch, BASE_KEY)
        if pr is None or pr.base == parent or base is None:
            return None
        try:
            merged = self.merged_heads(parent, pr.base)
        except StackError:
            return None
        self.fetch_missing_heads(merged)
        if not any(git.rev(head) and git.is_ancestor(base, head) for _, head in merged):
            return None
        return self.local_branch_of(pr.base)

    def merged_heads(self, head: str, base: str) -> list[tuple[int, str]]:
        """The numbers and head commits of the pull requests from head, a
        branch in the repository, merged into base."""
        query = (
            "query($owner: String!, $name: String!, $h0: String!) { repository(owner: $owner, name: $name) "
            f"{{ h0: pullRequests(headRefName: $h0, states: MERGED, first: {PRS_PER_BRANCH}, "
            "orderBy: {field: UPDATED_AT, direction: DESC}) "
            "{ nodes { number baseRefName headRefOid isCrossRepository } } } }"
        )
        args = ["api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        args += ["-f", f"h0={head}", "-f", f"query={query}"]
        repository = field(field(parse_json(self.gh(*args)), "data", dict), "repository", dict)
        return [
            (field(pr, "number", int), field(pr, "headRefOid", str))
            for pr in field(field(repository, "h0", dict), "nodes", list)
            if not field(pr, "isCrossRepository", bool) and field(pr, "baseRefName", str) == base
        ]

    def source_landings(
        self, sources: set[tuple[str, str]], root_tip: str
    ) -> tuple[dict[tuple[str, str], str], set[tuple[str, str]]]:
        """Exact landing proof and the sources whose PR scan was conclusive."""
        remaining: dict[str, set[str]] = {}
        for head, tip in sorted(sources):
            if self.git.rev(tip):
                remaining.setdefault(head, set()).add(tip)
        observed: set[tuple[str, str | None]] = set()
        cursors: dict[str, str | None] = dict.fromkeys(remaining)
        landed: dict[tuple[str, str], str] = {}
        incomplete: set[str] = set()
        while cursors:
            self.load_source_pages(list(cursors.items()))
            candidates = {
                head: self.root_merges(self._source_pages[head, cursor][0], root_tip)
                for head, cursor in cursors.items()
            }
            self.fetch_missing_heads((pr.number, pr.head_oid) for prs in candidates.values() for pr in prs)
            next_page: dict[str, str | None] = {}
            for head, cursor in cursors.items():
                observed.add((head, cursor))
                for pr in candidates[head]:
                    if self.git.rev(pr.head_oid) is None:
                        incomplete.add(head)
                        continue
                    matched = {tip for tip in remaining[head] if self.git.is_ancestor(tip, pr.head_oid)}
                    assert pr.merge_oid is not None
                    landed.update(dict.fromkeys(((head, tip) for tip in matched), pr.merge_oid))
                    remaining[head].difference_update(matched)
                after = self._source_pages[head, cursor][1]
                if after and remaining[head]:
                    if (head, after) in observed:
                        raise StackError(f"GitHub repeated a pull request page for {head}")
                    next_page[head] = after
            cursors = next_page
        return landed, {(h, t) for h, t in sources if h in remaining and (h not in incomplete or (h, t) in landed)}

    def root_merges(self, prs: list[PullRequest], root_tip: str) -> list[PullRequest]:
        return [
            pr
            for pr in prs
            if pr.state is GitHubState.MERGED
            and pr.merge_oid
            and self.git.rev(pr.merge_oid)
            and self.git.is_ancestor(pr.merge_oid, root_tip)
        ]

    def load_source_pages(self, pages: list[tuple[str, str | None]]) -> None:
        """Look up missing pages together, keeping each request small enough for the command line."""
        missing = [page for page in pages if page not in self._source_pages]
        for start in range(0, len(missing), 20):
            batch = missing[start : start + 20]
            args = ["api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}", "-f", f"name={self.name}"]
            variables = ["$owner: String!", "$name: String!"]
            fields: list[str] = []
            for index, (head, cursor) in enumerate(batch):
                variables.extend((f"$h{index}: String!", f"$after{index}: String"))
                args.extend(("-f", f"h{index}={head}"))
                if cursor:
                    args.extend(("-f", f"after{index}={cursor}"))
                fields.append(
                    f"h{index}: pullRequests(headRefName: $h{index}, states: MERGED, first: {PRS_PER_BRANCH}, "
                    f"after: $after{index}, orderBy: {{field: UPDATED_AT, direction: DESC}}) "
                    f"{{ nodes {{ {PR_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }}"
                )
            query = (
                f"query({', '.join(variables)}) {{ repository(owner: $owner, name: $name) {{ {' '.join(fields)} }} }}"
            )
            repository = field(
                field(parse_json(self.gh(*args, "-f", f"query={query}")), "data", dict), "repository", dict
            )
            for index, key in enumerate(batch):
                page = field(repository, f"h{index}", dict)
                info = field(page, "pageInfo", dict)
                after = field(info, "endCursor", str) if field(info, "hasNextPage", bool) else None
                self._source_pages[key] = [PullRequest.from_json(pr) for pr in field(page, "nodes", list)], after

    def local_branch_of(self, name: str) -> str | None:
        """The local branch of name, a branch on remote: the one whose
        upstream it is (name itself, if several are), or else a local branch
        called name that tracks nothing else on remote. None if there's none."""
        git = self.git
        tracking = sorted(
            b
            for b in git.local_branches()
            if git.has_branch_upstream(b, self.remote) and git.remote_branch_name(b, self.remote) == name
        )
        if tracking:
            return name if name in tracking else tracking[0] if len(tracking) == 1 else None
        if git.local_branch_exists(name) and git.remote_branch_name(name, self.remote) == name:
            return name
        return None

    def merge_state(self, branch: str, parent: str, ignore_revert: bool = False) -> MergeState:
        git = self.git
        pr = self.pull_requests().get(branch)
        if (
            pr is not None
            and pr.state is GitHubState.OPEN
            and self.git.remote_branch_name(parent, self.remote) == self._default_branch
        ):
            return MergeState.NOT_MERGED
        merged = self.trusted_merge(branch, parent)
        if merged is None:
            return super().merge_state(branch, parent, ignore_revert)
        if not ignore_revert and self.reverted(branch, parent):
            return MergeState.REVERTED
        head = merged.head_oid
        upstream_tip = git.rev(f"{branch}@{{upstream}}")

        def has_new_work(ref: str) -> bool:
            # Commits made since that the parent has the changes of anyway
            # (e.g. a merge of it) leave nothing behind.
            return bool(self.unmerged_commits(ref, head, parent)) and not git.contained_in(ref, f"refs/heads/{parent}")

        if has_new_work(f"refs/heads/{branch}") or (upstream_tip and has_new_work(upstream_tip)):
            return MergeState.MERGED_WITH_NEW_COMMITS
        return MergeState.MERGED

    def trusted_merge(self, branch: str, parent: str) -> PullRequest | None:
        """branch's merged pull request, if it's merged into the local parent.

        Trust GitHub only once the merge is in the local parent, so children
        aren't restacked onto a parent that lacks it, and only if we have the
        merged commits, so we can tell whether anything came after them.
        """
        git = self.git
        pr = self.pull_requests().get(branch)
        if pr is None or pr.state is not GitHubState.MERGED:
            return None
        merge = pr.merge_oid
        if not (merge and git.rev(merge) and git.is_ancestor(merge, f"refs/heads/{parent}") and git.rev(pr.head_oid)):
            return None
        return pr

    def merge_confirmed(self, branch: str, parent: str) -> bool:
        return self.trusted_merge(branch, parent) is not None or super().merge_confirmed(branch, parent)

    def landing_merges(self, branch: str, parent: str) -> tuple[str, ...]:
        pr = self.trusted_merge(branch, parent)
        return (pr.merge_oid,) if pr is not None and pr.merge_oid else ()

    def unmerged_commits(self, ref: str, merged_head: str, parent: str) -> set[str]:
        """Non-merge commits on ref that aren't in parent and don't match a commit
        on merged_head, and merges on ref, in neither, with changes of their own
        (see GitClient.merges_with_own_changes)."""
        git = self.git
        unmatched = git.run("rev-list", "--cherry-pick", "--right-only", "--no-merges", f"{merged_head}...{ref}")
        commits = set(unmatched.split()) & set(
            git.run("rev-list", "--no-merges", ref, "--not", f"refs/heads/{parent}").split()
        )
        return commits | set(git.merges_with_own_changes(ref, "--not", merged_head, f"refs/heads/{parent}"))

    def ensure_pr_parent_remote(self, branch: str, parent: str) -> None:
        base_remote = self.git.config_get(parent, "remote")
        if base_remote and base_remote not in (".", self.remote):
            raise StackError(f"can't retarget {branch}'s pull request: its parent {parent} tracks {base_remote}")

    def ensure_pr_parent_remotes(
        self, branches: list[str], stacks: Stacks, prs: dict[str, PullRequest], opening: bool
    ) -> None:
        for branch in branches:
            if (branch in prs and prs[branch].state is GitHubState.OPEN) or (opening and branch not in prs):
                self.ensure_pr_parent_remote(branch, stacks.parent(branch))

    def prepare_push_retargets(
        self,
        to_push: list[str],
        stacks: Stacks,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
        opening: bool = False,
    ) -> PushRetargets | None:
        """Validate PR changes and return a callback to retarget open PRs
        whose new bases are already on the remote.

        Pushing first would briefly compare a rebased branch with its old base
        (e.g. its parent's branch, after the parent was merged), so its pull
        request would show every commit the new base gained since. The other
        way round, it shows at most the old parent's commits.
        """
        # A new parent is not on the remote yet, so its PR cannot be retargeted
        # until after the push. Check its stack now to avoid pushing first and
        # only then discovering that unstacking requires consent.
        prs = self.pull_requests()
        self.ensure_pr_parent_remotes(to_push, stacks, prs, opening)
        self.protect_retargets(to_push, stacks)
        allowed = {
            prs[b].number
            for b in (scope if scope is not None else to_push)
            if b in prs and prs[b].state is GitHubState.OPEN
        }
        ready: list[tuple[str, str]] = []
        pending: dict[int, tuple[str, str]] = {}
        for branch in to_push:
            pr = prs.get(branch)
            if pr is None or pr.state is not GitHubState.OPEN:
                continue
            parent = stacks.parent(branch)
            base = self.git.remote_branch_name(parent, self.remote)
            if pr.base == base:
                continue
            parent_published = self.git.rev(f"refs/remotes/{self.remote}/{base}") is not None
            if parent_published:
                ready.append((branch, parent))
            if rebuild_stack or not parent_published:
                pending[pr.number] = (pr.base, base)
        closures = self.validate_deferred_retargets(pending, rebuild_stack, allowed) if pending else []
        # Collect consent before the dry push. A ref could change while the
        # user reads the prompt, so check the push leases after they answer.
        self.confirm_orphans(closures)

        def retarget() -> None:
            self.close_orphans(
                closures, confirmed=True, edits=self.retarget_edits(to_push, stacks) if closures else None
            )
            for branch, parent in ready:
                # A new prompt here would run after the push lease check.
                self.retarget(branch, parent, rebuild_stack, allowed, allow_closure_prompt=False)

        return PushRetargets(retarget, bool(closures)) if ready or closures else None

    def validate_deferred_retargets(
        self, pending: dict[int, tuple[str, str]], rebuild_stack: bool, allowed: set[int]
    ) -> list[OrphanedPullRequest]:
        """Check whether retargeting to parents published by this push is allowed."""
        try:
            fields = self.pull_request_fields(pending, STACK_FIELDS)
        except StackError as e:
            if "Field 'stack' doesn't exist on type 'PullRequest'" not in str(e):
                raise StackError(
                    "couldn't check whether pull requests need unstacking before pushing; nothing was pushed"
                ) from e
            return []
        stacks = {
            number: stack for number, pr in fields.items() if (stack := optional_field(pr, "stack", dict)) is not None
        }
        existing = [GitHubStack.from_json(stacks[number]) for number in sorted(stacks)]
        if existing and not rebuild_stack:
            changes = ", ".join(f"#{n}: {pending[n][0]} → {pending[n][1]}" for n in sorted(stacks))
            raise StackError(self.rebuild_hint(existing, allowed, f"change PR bases ({changes})"))
        closures = self.plan_orphan_closures(existing, allowed)
        members = {n for stack in existing for n, state in stack.entries if state is GitHubState.OPEN}
        self.protect_edits(dict.fromkeys(members - {pr.number for pr in closures}), "unstack")
        return closures

    def after_push(
        self,
        pushed: list[str],
        stacks: Stacks,
        opening: bool = False,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
    ) -> None:
        """Point the open pull requests of pushed branches at their stack parents,
        if the parent wasn't on the remote before the push, and
        say which pushed branches have none, unless they're opened next."""
        prs = self.pull_requests()
        allowed = {
            prs[b].number
            for b in (scope if scope is not None else pushed)
            if b in prs and prs[b].state is GitHubState.OPEN
        }
        for b in pushed:
            if b in prs:
                self.retarget(b, stacks.parent(b), rebuild_stack, allowed, allow_closure_prompt=False)
        without = tuple(b for b in pushed if b not in prs)
        if without and not opening:
            self.core.emit(events.NoPullRequests(without))

    def retarget(
        self,
        branch: str,
        base: str,
        rebuild_stack: bool = False,
        allowed: set[int] | None = None,
        *,
        allow_closure_prompt: bool = True,
    ) -> None:
        """Point branch's pull request at base, if it's open and isn't already."""
        prs = self.pull_requests()
        pr = prs.get(branch)
        if pr is None or pr.state is not GitHubState.OPEN:
            return
        self.ensure_pr_parent_remote(branch, base)
        base = self.git.remote_branch_name(base, self.remote)
        if pr.base == base:
            return
        self.protect_edits({pr.number: base}, "retarget")
        edit = ("pr", "edit", str(pr.number), "--repo", self.repo, "--base", base)
        try:
            self.gh(*edit)
        except StackError as e:
            # GitHub only changes a stack's bases itself, when one of its pull
            # requests merges, so a stack that's been reshaped (e.g. a branch
            # was put in the middle of it) has to be unstacked first.
            if STACKED_BASE_ERROR not in str(e):
                raise
            if not rebuild_stack:
                fields = self.pull_request_fields([pr.number], STACK_FIELDS)
                stack = optional_field(fields[pr.number], "stack", dict)
                if stack is None:
                    raise StackError(f"GitHub no longer reports #{pr.number} in a stack; retry the command") from e
                raise StackError(
                    self.rebuild_hint(
                        [GitHubStack.from_json(stack)],
                        allowed or set(),
                        f"change the PR base (#{pr.number}: {pr.base} → {base})",
                    )
                ) from e
            self.unstack(pr.number, allowed or set(), new_base=base, allow_closure_prompt=allow_closure_prompt)
            self.gh(*edit)
        self.changes.append(
            f"#{pr.number}'s base was changed from {pr.base} to {base} "
            f"(undo with `gh pr edit {pr.number} --repo {self.repo} --base {pr.base}`)"
        )
        prs[branch] = replace(pr, base=base)
        self.core.emit(events.PullRequestRetargeted(branch, pr.number, base))

    @staticmethod
    def orphan_blockers(stacks: Iterable[GitHubStack], allowed: set[int]) -> dict[int, GitHubStack]:
        return {
            n: stack for stack in stacks for n, state in stack.entries if state is GitHubState.OPEN and n not in allowed
        }

    def rebuild_hint(self, stacks: list[GitHubStack], allowed: set[int], action: str) -> str:
        ids = sorted({stack.number for stack in stacks})
        noun = "stacks" if len(ids) > 1 else "stack"
        steps = f"unstack GitHub {noun} {', '.join(map(str, ids))} (merged PRs stay linked), then {action}"
        blockers = self.orphan_blockers(stacks, allowed)
        if not blockers:
            return f"Retry with --pr-rebuild to {steps}."
        refusals: list[str] = []
        try:
            candidates = self.orphan_details(blockers, refusals=refusals)
        except StackError as e:
            return f"Rebuilding would {steps}, but couldn't check the blockers: {e}"
        draft_list = "\n".join(f"- {pr.description}" for pr in candidates)
        if refusals:
            problems = "\n".join(refusals)
            eligible = f"\nEligible drafts:\n{draft_list}" if candidates else ""
            return (
                f"Rebuilding would {steps}. No drafts would be closed while any blockers remain.\n{problems}{eligible}"
            )
        return (
            f"Retry with --pr-rebuild to {steps}.\n"
            f"The rebuild would first ask you to confirm closing these draft PRs, keeping their branches:\n{draft_list}"
        )

    def plan_orphan_closures(self, stacks: Iterable[GitHubStack], allowed: set[int]) -> list[OrphanedPullRequest]:
        blockers = self.orphan_blockers(stacks, allowed)
        if not blockers:
            return []
        message = "can't rebuild GitHub stacks containing other open pull requests: " + ", ".join(
            f"#{n}" for n in sorted(blockers)
        )
        if self.confirm_orphan_closures is None:
            raise StackError(message)
        try:
            return self.orphan_details(blockers)
        except StackError as e:
            raise StackError(f"{message}\n{e}") from e

    def orphan_details(
        self,
        blockers: dict[int, GitHubStack],
        *,
        already_closed: set[int] | None = None,
        expected: OrphanedPullRequest | None = None,
        refusals: list[str] | None = None,
    ) -> list[OrphanedPullRequest]:
        viewer = self.gh("api", "--hostname", self.host, "user", "--jq", ".login")
        check_github_login(viewer)
        stacks = self.core.stacks()
        # Check configured heads across all remotes, including aliases for
        # this repository. An ambiguous match is enough to keep the draft.
        tracked_heads = set(stacks) | {
            ref.removeprefix("refs/heads/")
            for branch in stacks
            if (ref := self.git.config_get(branch, "merge")) and ref.startswith("refs/heads/")
        }
        fields = (
            f"{QUEUE_FIELDS} {REVIEW_FIELDS} isCrossRepository headRefName headRefOid author {{ login }} {STACK_FIELDS}"
        )
        if expected is None:
            details = self.pull_request_fields(blockers, fields)
            dependents = None
        else:
            # Use one final GitHub read for the draft and its dependents.
            repo = self.repository_fields(
                f"p{expected.number}: pullRequest(number: {expected.number}) {{ {fields} }} {DEPENDENTS_FIELD}",
                base=expected.head,
            )
            details = {expected.number: repo.get(f"p{expected.number}")}
            dependents = field(field(repo, "dependents", dict), "nodes", list)
        candidates = []
        for number, data in details.items():
            try:
                head = field(data, "headRefName", str)
                stack = optional_field(data, "stack", dict)
                if (
                    github_state(data) is not GitHubState.OPEN
                    or not field(data, "isDraft", bool)
                    or field(data, "isCrossRepository", bool)
                    or (login_of(optional_field(data, "author", dict)) or "").casefold() != viewer.casefold()
                    or head in tracked_heads
                    or stack is None
                    or not GitHubStack.from_json(stack).matches_after_closures(
                        blockers[number], already_closed or set()
                    )
                    or field(data, "isInMergeQueue", bool)
                    or PRProtection.has_approvals(data)
                ):
                    raise StackError(
                        f"can't close blocking PR #{number}: "
                        "it must still be your own untracked draft in this stack, with no approval or queue entry"
                    )
                if expected is not None and head != expected.head:
                    raise StackError(f"blocking pull request #{number} changed during the rebuild; retry the command")
                has_dependents = bool(dependents) if dependents is not None else self.has_open_pr_based_on(head)
                if has_dependents:
                    raise StackError(f"can't close blocking PR #{number}: open pull requests target its head {head}")
                candidates.append(
                    OrphanedPullRequest(
                        number,
                        blockers[number],
                        f"https://{self.repo}/pull/{number}",
                        head,
                        field(data, "baseRefName", str),
                        field(data, "headRefOid", str),
                    )
                )
            except StackError as e:
                if refusals is None:
                    raise
                refusals.append(str(e))
        return candidates

    def has_open_pr_based_on(self, head: str) -> bool:
        repo = self.repository_fields(DEPENDENTS_FIELD, base=head)
        return bool(field(field(repo, "dependents", dict), "nodes", list))

    def confirm_orphans(self, candidates: list[OrphanedPullRequest]) -> None:
        if candidates:
            assert self.confirm_orphan_closures is not None
            if not self.confirm_orphan_closures(candidates):
                raise StackError("rebuilding stopped because closing the blocking drafts wasn't confirmed")

    def close_orphans(
        self,
        candidates: list[OrphanedPullRequest],
        *,
        dry_run: bool = False,
        confirmed: bool = False,
        edits: dict[int, str | None] | None = None,
    ) -> None:
        if not candidates:
            return
        if dry_run:
            for pr in candidates:
                self.core.emit(events.OrphanedPullRequestClosure(pr.number, pr.url, pr.head, pr.base, closed=False))
            return
        if not confirmed:
            self.confirm_orphans(candidates)
        if self.orphan_details({pr.number: pr.stack for pr in candidates}) != candidates:
            raise StackError("a blocking pull request changed during confirmation; retry the command")
        closing = {pr.number for pr in candidates}
        affected = dict.fromkeys(
            n for pr in candidates for n, state in pr.stack.entries if state is GitHubState.OPEN and n not in closing
        )
        closed = set[int]()
        for pr in candidates:
            self.protect_edits({**affected, **(edits or {})}, "rebuild the stack of")
            if self.orphan_details({pr.number: pr.stack}, already_closed=closed, expected=pr) != [pr]:
                raise StackError(f"blocking pull request #{pr.number} changed during the rebuild; retry the command")
            self.gh("pr", "close", str(pr.number), "--repo", self.repo)
            closed.add(pr.number)
            self.changes.append(
                f"closed draft PR #{pr.number} ({pr.url}), keeping its branch "
                f"(reopen with `gh pr reopen {pr.number} --repo {self.repo}`, then relink if needed)"
            )
            self.core.emit(events.OrphanedPullRequestClosure(pr.number, pr.url, pr.head, pr.base, closed=True))

    def unstack(
        self,
        number: int,
        allowed: set[int],
        *,
        new_base: str | None = None,
        after_closures: bool = False,
        allow_closure_prompt: bool = True,
    ) -> None:
        """Take the unmerged pull requests of pull request number's stack on
        GitHub out of it. Merged ones stay in it. Closed ones outside allowed are
        taken out too, and this doesn't link them in again."""
        fields = self.pull_request_fields([number], STACK_FIELDS)
        stack_field = optional_field(fields[number], "stack", dict)
        if stack_field is None:
            if after_closures:
                return
            raise StackError(f"GitHub no longer reports #{number} in a stack; retry the command")
        github_stack = GitHubStack.from_json(stack_field)
        if (after_closures or not allow_closure_prompt) and self.orphan_blockers([github_stack], allowed):
            raise StackError("the GitHub stack gained other open pull requests; retry the command")
        closures = self.plan_orphan_closures([github_stack], allowed)
        if closures:
            self.protect_edits(
                {n: None for n, state in github_stack.entries if n in allowed and state is GitHubState.OPEN}, "unstack"
            )
            self.close_orphans(closures, edits={number: new_base})
            self.unstack(number, allowed, after_closures=True)
            return
        found = parse_json(self.gh("api", "--hostname", self.host, f"{self.stacks_path()}?pull_request={number}"))
        if not isinstance(found, list) or not found:
            raise StackError(f"GitHub says #{number} is in a stack, but doesn't say which: {found!r}")
        stack = field(found[0], "number", int)
        if stack != github_stack.number:
            raise StackError(f"#{number}'s GitHub stack changed while checking it; retry the command")
        current = {field(pr, "number", int) for pr in field(found[0], "pull_requests", list)}
        if current != {n for n, _ in github_stack.entries}:
            raise StackError(f"GitHub stack {stack} changed while checking it; retry the command")
        self.protect_edits({n: None for n, state in github_stack.entries if state is GitHubState.OPEN}, "unstack")
        self.gh("api", "--hostname", self.host, "-X", "POST", f"{self.stacks_path()}/{stack}/unstack")
        self.changes.append(
            f"GitHub stack {stack} was unstacked (once they're pushed, `git stack prs` links a linear stack's "
            "pull requests into one again; one that branches out is left unlinked, with description sections)"
        )
        self.core.emit(events.GitHubStackUnstacked(stack, number))

    def stacks_path(self) -> str:
        return f"repos/{self.owner}/{self.name}/stacks"

    def pull_request_template(self) -> str | None:
        """The repository's pull request template, as GitHub fills it in: from
        the remote's default branch, ignoring the named templates in a
        PULL_REQUEST_TEMPLATE directory. None if it has none."""
        self.ensure_available()
        if self._default_branch is None:
            return None
        ref = f"refs/remotes/{self.remote}/{self._default_branch}"
        for directory in TEMPLATE_DIRECTORIES:
            listing = self.git.run("ls-tree", "-z", f"{ref}:{directory}", check=False)
            for entry in filter(None, listing.split("\0")):
                info, _, name = entry.partition("\t")
                _, kind, oid = info.split()
                if kind == "blob" and name.lower() in TEMPLATE_NAMES:
                    return self.git.run("cat-file", "blob", oid)
        return None

    def open_pull_request(self, branch: str, base: str, title: str, body: str) -> tuple[int, str]:
        """Open a draft pull request of branch into base. Returns its number and URL."""
        url = self.gh(
            "pr",
            "create",
            "--repo",
            self.repo,
            "--draft",
            "--base",
            self.git.remote_branch_name(base, self.remote),
            "--head",
            self.git.remote_branch_name(branch, self.remote),
            "--title",
            title,
            "--body-file",
            "-",
            input=body,
        ).splitlines()[-1]
        match = re.search(r"/pull/(\d+)$", url)
        if not match:
            raise StackError(f"couldn't tell which pull request gh opened for {branch} from {url!r}")
        number = int(match.group(1))
        self.pull_requests()[branch] = PullRequest(
            number,
            GitHubState.OPEN,
            True,
            False,
            self.git.tip(branch),
            self.git.remote_branch_name(base, self.remote),
            None,
        )
        return number, url

    def pull_request_fields(self, numbers: Iterable[int], fields: str) -> dict[int, object]:
        """{number: its pull request's fields, a JSON object or None if there's
        no such pull request} for the pull requests numbers."""
        numbers = sorted(set(numbers))
        if not numbers:
            return {}
        prs = " ".join(f"p{n}: pullRequest(number: {n}) {{ {fields} }}" for n in numbers)
        repository = self.repository_fields(prs)
        return {n: repository.get(f"p{n}") for n in numbers}

    def repository_fields(self, fields: str, *, base: str | None = None) -> dict[str, object]:
        declarations = "$owner: String!, $name: String!"
        variables = ["-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        if base is not None:
            declarations += ", $base: String!"
            variables.extend(["-f", f"base={base}"])
        query = f"query({declarations}) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        data = parse_json(self.gh("api", "graphql", "--hostname", self.host, *variables, "-f", f"query={query}"))
        return field(field(data, "data", dict), "repository", dict)

    def update_stack_sections(
        self, branches: list[str], stacks: Stacks, rebuild_stack: bool = False, dry_run: bool = False
    ) -> None:
        """Make the stack section at the end of the description of each open
        pull request of branches, a stack parent-before-child, show the stack,
        marking that pull request (see git_stack.stack_section).

        Merged pull requests aren't shown, except that each pull request on
        root is shown on the merged one it was last stacked on (see
        merged_parents), which links to the pull requests before it. A stack
        with fewer than two pull requests, counting those, gets no section, and
        one it has is removed.

        The paths of open pull requests to have in stacks on GitHub (see
        github_stack_paths) are linked into them first (see
        link_github_stack). The pull requests in such a stack (all of a
        path's, or its bottom ones when linking is off), which GitHub shows
        on each pull request itself, get no section, and the ones they have
        are removed for ungrouped stacks. For groups with several stacks,
        each updated pull request's section shows the full group across its
        roots, including when GitHub shows a linked stack.
        """
        prs = self.pull_requests()
        to_update = {b: prs[b].number for b in branches if b in prs and prs[b].state is GitHubState.OPEN}
        history_roots = self.stack_section_history_roots(branches, stacks, prs, to_update)
        numbers = sorted(set(to_update.values()) | history_roots.keys())
        try:
            fields = self.pull_request_fields(numbers, f"id body {BASE_CHANGES} {STACK_FIELDS}")
        except StackError:
            # E.g. GitHub Enterprise without stacked pull requests, which
            # doesn't know the field.
            fields = self.pull_request_fields(numbers, f"id body {BASE_CHANGES}")
        merged = self.merged_parents({n: fields[n] for n in history_roots}, history_roots)
        for root in sorted({stacks.root(b) for b in branches}):
            on_root = [b for b in branches if stacks.root(b) == root]
            self.update_stack_sections_on_root(
                on_root,
                stacks,
                prs,
                fields,
                {b: to_update[b] for b in on_root if b in to_update},
                merged,
                rebuild_stack,
                dry_run,
            )

    @staticmethod
    def stack_section_history_roots(
        branches: list[str], stacks: Stacks, prs: dict[str, PullRequest], to_update: dict[str, int]
    ) -> dict[int, str]:
        """Roots of PRs whose previous bases can appear in a stack section."""
        selected = set(branches)
        bottoms = {b for b in to_update if stacks.parent(b) not in selected}
        for bottom in {stacks.bottom(b) for b in to_update}:
            bottoms.update(stacks.group_bottoms(bottom))
        return {prs[b].number: stacks.root(b) for b in bottoms if b in prs and prs[b].state is GitHubState.OPEN}

    def update_stack_sections_on_root(
        self,
        branches: list[str],
        stacks: Stacks,
        prs: dict[str, PullRequest],
        fields: dict[int, object],
        to_update: dict[str, int],
        group_history: dict[int, int],
        rebuild_stack: bool,
        dry_run: bool,
    ) -> None:
        root = stacks.root(branches[0])
        selected = set(branches)

        def node(branch: str) -> stack_section.Node:
            return prs[branch].number if branch in prs else branch

        current = {node(b): node(stacks.parent(b)) if stacks.parent(b) in selected else root for b in branches}
        merged = {
            n: group_history[n] for b, n in to_update.items() if n in group_history and stacks.parent(b) not in selected
        }
        # Each pull request on root goes on the merged one it was on, on root.
        tree = dict(current)
        for number, parent in merged.items():
            tree[number] = parent
            tree[parent] = root
        shown = sum(isinstance(n, int) for n in tree) >= 2
        github_stacks = {
            n: GitHubStack.from_json(stack) if (stack := optional_field(fields[n], "stack", dict)) is not None else None
            for n in to_update.values()
        }
        paths = github_stack_paths(branches, stacks, root, to_update, github_stacks)
        # Only a linear stack is linked again once a retarget unstacks it, as
        # nothing else is left to say which path to link.
        chain = [to_update[b] for b in branches] if paths == [branches] else []
        if dry_run and rebuild_stack and self.plan_retargets(to_update, prs, stacks, github_stacks, chain):
            return
        linked = (
            self.link_github_stacks(
                [[to_update[b] for b in path] for path in paths], github_stacks, rebuild_stack, dry_run
            )
            if shown
            else set[int]()
        )
        if dry_run:
            return
        edits: dict[int, str] = {}
        for branch, number in to_update.items():
            body = field(fields[number], "body", str)
            if len(stacks.group_bottoms(stacks.bottom(branch))) > 1:
                section = group_stack_section(branch, stacks, prs, group_history)
                new_body = stack_section.replace(body, section)
            elif number in linked or not shown:
                new_body = stack_section.remove(body)
            else:
                section = stack_section.render({root: tree}, set(merged.values()), number)
                new_body = stack_section.replace(body, section)
            if new_body != body:
                edits[number] = new_body
        self.update_descriptions(edits, fields)

    def update_descriptions(self, edits: dict[int, str], fields: dict[int, object]) -> None:
        """Batch body-only changes and verify each mutation's result."""
        self.protect_edits(dict.fromkeys(edits), "edit the description of")
        numbers = list(edits)
        if len(numbers) == 1:
            number = numbers[0]
            self.gh("pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", input=edits[number])
            return
        for start in range(0, len(numbers), DESCRIPTION_BATCH_SIZE):
            batch = numbers[start : start + DESCRIPTION_BATCH_SIZE]
            inputs = {f"p{n}": {"pullRequestId": field(fields[n], "id", str), "body": edits[n]} for n in batch}
            declarations = ", ".join(f"${alias}: UpdatePullRequestInput!" for alias in inputs)
            mutations = " ".join(
                f"{alias}: updatePullRequest(input: ${alias}) {{ pullRequest {{ number }} }}" for alias in inputs
            )
            # The descriptions can be long and belong on stdin, outside the
            # command line and performance log.
            request = json.dumps({"query": f"mutation({declarations}) {{ {mutations} }}", "variables": inputs})
            errors: list[str] = []
            raw_response = self.gh(
                "api", "graphql", "--hostname", self.host, "--input", "-", input=request, partial_errors=errors
            )
            try:
                response = parse_json(raw_response)
                data = optional_field(response, "data", dict) or {}
            except StackError as error:
                if errors:
                    raise StackError("\n".join(errors)) from error
                raise
            failed = [
                n
                for n in batch
                if not isinstance(entry := data.get(f"p{n}"), dict) or entry.get("pullRequest") != {"number": n}
            ]
            if failed:
                names = ", ".join(f"#{n}" for n in failed)
                # A partial response can omit or garble these diagnostics.
                with suppress(StackError):
                    for item in optional_field(response, "errors", list) or []:
                        with suppress(StackError):
                            message = optional_field(item, "message", str)
                            if message and not any(message in error for error in errors):
                                errors.append(message)
                details = "".join(f"\n{error}" for error in errors)
                raise StackError(
                    f"couldn't update descriptions for {names}; rerun to update the remaining pull requests{details}"
                )

    def link_github_stacks(
        self, chains: list[list[int]], stacks: dict[int, GitHubStack | None], rebuild_stack: bool, dry_run: bool
    ) -> set[int]:
        """Link each of chains of two or more pull requests into a stack on
        GitHub (see link_github_stack). Returns the pull requests that are in
        a stack on GitHub of their path's bottom ones (see
        link_github_stack)."""
        linked = set[int]()
        for chain in chains:
            if len(chain) > 1:
                linked.update(self.link_github_stack(chain, {n: stacks[n] for n in chain}, rebuild_stack, dry_run))
        return linked

    def plan_retargets(
        self,
        to_update: dict[str, int],
        prs: dict[str, PullRequest],
        stacks: Stacks,
        github_stacks: dict[int, GitHubStack | None],
        chain: list[int],
    ) -> bool:
        """Report unstacking needed for retargets in a dry run, if any."""
        affected = [
            n
            for b, n in to_update.items()
            if prs[b].base != self.git.remote_branch_name(stacks.parent(b), self.remote)
            and github_stacks[n] is not None
        ]
        if not affected:
            return False
        can_link = len(chain) > 1 and self.github_stacks_enabled()
        found = {
            stack.number: stack
            for n, stack in github_stacks.items()
            if stack is not None and (n in affected or can_link)
        }
        self.close_orphans(self.plan_orphan_closures(found.values(), set(to_update.values())), dry_run=True)
        if can_link:
            self.core.emit(events.GitHubStackRebuildPlanned(tuple(sorted(found)), tuple(chain)))
        else:
            self.core.emit(events.GitHubStackUnstackPlanned(tuple(sorted(found)), tuple(sorted(affected))))
        return True

    def merged_parents(self, prs: dict[int, object], roots: dict[int, str]) -> dict[int, int]:
        """The merged pull request each is last stacked on, given its fields
        with BASE_CHANGES and its current root.

        That's the head of the first of the bases each was changed from,
        newest first, to have a pull request merged before the change (the
        newest, if several were, as the name may have been reused). Pull
        requests whose base never changed, or only from root, from branches
        with an open pull request, or from branches without a merged one, are
        left out.
        """
        changes = {
            n: [
                (
                    optional_field(change, "previousRefName", str) or field(change, "oldBase", str),
                    field(change, "createdAt", str),
                )
                for change in reversed(field(field(pr, "timelineItems", dict), "nodes", list))
            ]
            for n, pr in prs.items()
        }
        open_heads = {b for b, pr in self.pull_requests().items() if pr.state is GitHubState.OPEN}
        heads = {head for n, c in changes.items() for head, _ in c if head != roots[n]} - open_heads
        merges = self.merged_pull_requests(sorted(heads))
        found = {}
        for n, c in changes.items():
            # GitHub's timestamps are all UTC, in the same format, so they
            # compare as strings.
            parent = next(
                (m for head, at in c if head != roots[n] for merged_at, m in merges.get(head, []) if merged_at <= at),
                None,
            )
            if parent is not None:
                found[n] = parent
        return found

    def merged_pull_requests(self, heads: list[str]) -> dict[str, list[tuple[str, int]]]:
        """{head: (when it merged, number) of each of its merged pull requests
        in the repository, newest first} for the branches heads."""
        if not heads:
            return {}
        # Branch names are passed as variables, so they needn't be escaped.
        fields = " ".join(
            f"h{i}: pullRequests(headRefName: $h{i}, states: MERGED, first: {PRS_PER_BRANCH}, "
            f"orderBy: {{field: UPDATED_AT, direction: DESC}}) {{ nodes {{ number mergedAt isCrossRepository }} }}"
            for i in range(len(heads))
        )
        variables = "".join(f", $h{i}: String!" for i in range(len(heads)))
        query = f"query($owner: String!, $name: String!{variables}) {{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
        args = ["api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        args += ["-f", f"query={query}"]
        for i, head in enumerate(heads):
            args += ["-f", f"h{i}={head}"]
        repository = field(field(parse_json(self.gh(*args)), "data", dict), "repository", dict)
        return {
            head: sorted(
                (
                    (field(pr, "mergedAt", str), field(pr, "number", int))
                    for pr in field(field(repository, f"h{i}", dict), "nodes", list)
                    if not field(pr, "isCrossRepository", bool)
                ),
                reverse=True,
            )
            for i, head in enumerate(heads)
        }

    def link_github_stack(
        self,
        chain: list[int],
        stacks: dict[int, GitHubStack | None],
        rebuild_stack: bool = False,
        dry_run: bool = False,
    ) -> list[int]:
        """Put chain, open pull requests each on the one before, bottom first,
        in one stack on GitHub, unless GITHUB_STACKS_SETTING turns that off.
        stacks is each one's stack on GitHub, if it's in one. Returns those of
        chain that are in one stack whose open pull requests are chain's
        bottom ones: chain, once they're all in one, or the bottom ones,
        if the rest weren't added, e.g. as linking is off.

        They're all in one if its open pull requests are chain, in order.
        Otherwise the rest are added to the top of the one whose open pull
        requests are chain's bottom ones (and whose last pull request is
        open), or their stack is made, if none is in one. Pull requests can't
        be taken out of a stack, so if they're in several, or in one that
        doesn't fit, they're left alone, with a warning, unless rebuild_stack
        is set, which unstacks those stacks and makes chain's. A repository
        without stacked pull requests is left alone too.
        """
        found = {stack.number: stack for stack in stacks.values() if stack is not None}
        (stack,) = found.values() if len(found) == 1 else (None,)
        bottom = [n for n, state in stack.entries if state is GitHubState.OPEN] if stack is not None else []
        fits = (
            stack is not None
            and bottom == chain[: len(bottom)]
            and bool(stack.entries)
            and stack.entries[-1][1] is GitHubState.OPEN
        )
        if bottom == chain:
            return chain
        kept = bottom if fits else []
        if not self.github_stacks_enabled():
            return kept
        if stack is not None and fits:
            if dry_run:
                self.core.emit(events.GitHubStackAddPlanned(stack.number, tuple(chain[len(bottom) :])))
                return kept
            return chain if self.stacks_api(f"/{stack.number}/add", chain[len(bottom) :], bottom) else kept
        if dry_run and not found:
            self.core.emit(events.GitHubStackRebuildPlanned((), tuple(chain)))
            return []
        if rebuild_stack and found:
            return chain if self.rebuild_github_stack(chain, found, dry_run) else []
        if found:
            hint = self.rebuild_hint(
                list(found.values()), set(chain), f"link {', '.join(f'#{n}' for n in chain)} in a new stack"
            )
            self.core.emit(events.GitHubStackMismatch(tuple(chain), tuple(sorted(found)), hint))
            return []
        return chain if self.stacks_api("", chain) else []

    def rebuild_github_stack(self, chain: list[int], found: dict[int, GitHubStack], dry_run: bool) -> bool:
        """Unstack the stacks on GitHub found, {number: stack}, which don't fit
        chain, then link chain into a new one (see link_github_stack)."""
        allowed = set(chain)
        closures = self.plan_orphan_closures(found.values(), allowed)
        if dry_run:
            self.close_orphans(closures, dry_run=True)
            self.core.emit(events.GitHubStackRebuildPlanned(tuple(sorted(found)), tuple(chain)))
            return False
        edits = dict.fromkeys(chain)
        self.protect_edits(edits, "rebuild the stack of")
        self.close_orphans(closures, edits=edits)
        for existing in found.values():
            representative = next(n for n, state in existing.entries if state is GitHubState.OPEN and n in allowed)
            self.unstack(representative, allowed, after_closures=bool(closures))
        return self.stacks_api("", chain)

    def github_stacks_enabled(self) -> bool:
        setting = self.git.run("config", "--type=bool", "--get", GITHUB_STACKS_SETTING, check=False)
        return setting != "false"

    def stacks_api(self, path: str, numbers: list[int], existing: Iterable[int] = ()) -> bool:
        """POST numbers to path in the repository's stacks API. Returns
        False if the repository doesn't have stacked pull requests."""
        self.protect_edits(dict.fromkeys([*existing, *numbers]), "change the stack of")
        args = ("api", "--hostname", self.host, "-X", "POST", f"{self.stacks_path()}{path}", "--input", "-")
        with measure_gh(args):
            result = subprocess.run(
                ["gh", *args],
                capture_output=True,
                encoding="utf-8",
                input=json.dumps({"pull_requests": numbers}),
                check=False,
            )
        if result.returncode == 0:
            self.core.emit(events.GitHubStackLinked(tuple(numbers)))
            return True
        if "(HTTP 404)" in result.stderr:
            return False
        raise StackError(
            f"linking {', '.join(f'#{n}' for n in numbers)} into a stack on GitHub failed:\n{result.stderr.strip()}"
        )


def group_stack_section(branch: str, stacks: Stacks, prs: dict[str, PullRequest], merged: dict[int, int]) -> str:
    """Describe all of branch's group, including its last merged parents."""
    group = stacks.restricted(stacks.stack_of(branch))

    def node(name: str) -> stack_section.Node:
        return prs[name].number if name in prs else name

    trees: dict[str, stack_section.Tree] = {root: {} for root in group.roots()}
    for member in group:
        parent = group.parent(member)
        trees[group.root(member)][node(member)] = node(parent) if parent in group else parent
    group_merged = {prs[b].number for b in group if b in prs and prs[b].state is GitHubState.MERGED}
    for tree in trees.values():
        for child, parent in merged.items():
            if child in tree:
                tree.setdefault(parent, tree[child])
                tree[child] = parent
                group_merged.add(parent)
    return stack_section.render(trees, group_merged, node(branch))


def github_stack_paths(
    branches: list[str],
    stacks: Stacks,
    root: str,
    open_prs: dict[str, int],
    github_stacks: dict[int, GitHubStack | None],
) -> list[list[str]]:
    """The paths of branches, parent-before-child on root, to have in stacks
    on GitHub (see link_github_stack): each a list of branches with open pull
    requests (open_prs), each stacked on the one before, the first on root.

    If branches are such a path, it's the only one, whichever stacks on
    GitHub their pull requests are in. Otherwise, each stack on GitHub
    (github_stacks, each open pull request's, if it's in one) whose open pull
    requests are such a path still fits, and gives one, followed by the
    branches that can join its top: each the only one of branches stacked on
    the one before, with an open pull request. A tree with no such stack has
    no path, since nothing says which of its paths to link.
    """

    def is_path(path: list[str]) -> bool:
        return all(b in open_prs and stacks.parent(b) == (path[i - 1] if i else root) for i, b in enumerate(path))

    if is_path(branches):
        return [branches]
    branch_of = {n: b for b, n in open_prs.items()}
    paths: list[list[str]] = []
    for stack in {s.number: s for s in github_stacks.values() if s is not None}.values():
        numbers = [n for n, state in stack.entries if state is GitHubState.OPEN]
        if not numbers or any(n not in branch_of for n in numbers):
            continue
        path = [branch_of[n] for n in numbers]
        if not is_path(path):
            continue
        while len(above := [b for b in stacks.children(path[-1]) if b in branches]) == 1 and above[0] in open_prs:
            path.append(above[0])
        paths.append(path)
    return paths


def github_repo(git: GitClient, remote: str) -> tuple[str, str, str] | None:
    """The configured repository, or the fetch URL after applying insteadOf."""
    return github_repo_from_url(git.config_value(f"remote.{remote}.url") or "") or github_repo_from_url(
        git.run("remote", "get-url", remote, check=False)
    )


def github_repo_from_url(url: str) -> tuple[str, str, str] | None:
    """The repository and host, resolving SSH aliases."""
    match = REMOTE_URL.fullmatch(url)
    if match is None:
        return None
    host, owner, name = match.groups()
    if "://" not in url or url.startswith(("ssh://", "git+ssh://", "ssh+git://")):
        host = ssh_hostname(host).lower()
        if host == "ssh.github.com":
            host = "github.com"
    return host.lower(), owner, name


def ssh_hostname(host: str) -> str:
    """The host ssh connects to after resolving aliases."""
    return ssh_configuration(host).get("hostname", host)


def git_ssh_program(cwd: pathlib.Path, path: str) -> pathlib.Path | None:
    """Find the first executable Git would try on POSIX."""
    for entry in path.split(os.pathsep):
        candidate = cwd / entry / "ssh"
        try:
            mode = candidate.stat().st_mode
        except OSError:
            continue
        if stat.S_ISREG(mode) and mode & stat.S_IXUSR:
            return candidate
    return None


def ssh_configuration(
    *args: str,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
    program: pathlib.Path | None = None,
) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["ssh", "-G", *args],
            # Python can skip a text executable that Git would run with sh.
            executable=program,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            check=False,
            cwd=cwd,
            env=env,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    return {fields[0]: fields[1] for line in result.stdout.splitlines() if len(fields := line.split(maxsplit=1)) == 2}


def direct_github_ssh(config: dict[str, str]) -> bool:
    """Confirm that effective SSH settings use one of GitHub's endpoints."""
    endpoint = config.get("hostname", "").lower(), config.get("port", "")
    return (
        endpoint in GITHUB_SSH_ENDPOINTS
        and config.get("user") == "git"
        and config.get("stricthostkeychecking") not in ("no", "off", "false")
        and all(config.get(key) in (None, "none") for key in SSH_REDIRECT_SETTINGS)
    )
