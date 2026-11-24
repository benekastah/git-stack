# pyright: reportUnknownVariableType=false
import enum
import json
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from itertools import batched
from typing import Self

from git_stack import events, stack_section
from git_stack.adapters.git import ApplyPushRetargets, GitAdapter, PullRequestLink, PullRequestState
from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient
from git_stack.performance import measure_gh
from git_stack.stacks import Stacks

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
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
REF_PROTECTION_BATCH_SIZE = 20
QUEUE_FIELDS = "number state isInMergeQueue"
REVIEW_FIELDS = (
    "isDraft baseRefName reviewDecision mergeStateStatus "
    "latestOpinionatedReviews(first: 20) { nodes { state } pageInfo { hasNextPage } }"
)
PR_UPDATE_BATCH_SIZE = 10
# Where GitHub looks for a repository's pull request template, matching names
# case-insensitively. GitHub doesn't say which wins if there are several.
TEMPLATE_DIRECTORIES = (".github", "", "docs")
TEMPLATE_NAMES = ("pull_request_template.md", "pull_request_template.txt")
# Whether prs links each linear stack's pull requests into a stack on GitHub,
# or adds to one that's still a path through a stack that branches out
# (default: true), where the repository has GitHub's stacked pull requests.
GITHUB_STACKS_SETTING = "stack.githubStacks"
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
    """The identity and refs of a draft eligible for closure."""

    number: int
    stack: GitHubStack
    url: str
    head: str
    base: str
    head_oid: str


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
        self._repository_id: str | None = None
        self._stack_creation_available = True
        self._viewer: str | None = None
        self.protection = PRProtection()
        self.close_orphaned_prs = False

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
        for start in range(0, len(targets), REF_PROTECTION_BATCH_SIZE):
            self.protect_ref_batch(targets[start : start + REF_PROTECTION_BATCH_SIZE], allow_number=allow_number)

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

    def look_up_pull_requests(
        self, candidates: Iterable[str] | None = None, *, open_only: bool = False
    ) -> dict[str, PullRequest]:
        if candidates is None:
            candidates = self.core.stacks() if self._lookup_branches is None else self._lookup_branches
        branches = [b for b in candidates if self.git.config_get(b, "remote") == self.remote]
        if not branches:
            return {}
        states = "states: OPEN, " if open_only else ""
        # Branch names are passed as variables, so they needn't be escaped.
        fields = " ".join(
            f"b{i}: pullRequests(headRefName: $b{i}, {states}first: {PRS_PER_BRANCH}, "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}) "
            f"{{ nodes {{ {PR_FIELDS} }} pageInfo {{ hasNextPage endCursor }} }}"
            for i in range(len(branches))
        )
        variables = "".join(f", $b{i}: String!" for i in range(len(branches)))
        query = (
            f"query($owner: String!, $name: String!{variables}) {{ repository(owner: $owner, name: $name) "
            f"{{ id defaultBranchRef {{ name }} {fields} }} }}"
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
        self._repository_id = optional_field(repository, "id", str)
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
                    f"{{ b0: pullRequests(headRefName: $b0, {states}first: {PRS_PER_BRANCH}, after: $after, "
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
        """The API's default branch when Git fetches and pushes to that
        repository directly over HTTPS, so the API answers for the same
        server. Any other remote, ssh included, needs a live probe of its
        HEAD: which server ssh reaches depends on the ssh that Git runs and
        its configuration."""
        if not self.default_branch or self.host != "github.com":
            return None
        url = self.git.direct_remote_url(self.remote)
        expected = f"https://{self.repo}"
        return self.default_branch if url in (expected, f"{expected}.git") else None

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
    ) -> ApplyPushRetargets | None:
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
        allowed = {
            prs[b].number
            for b in (scope if scope is not None else to_push)
            if b in prs and prs[b].state is GitHubState.OPEN
        }
        ready: list[tuple[str, str]] = []
        pending: dict[int, str] = {}
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
                pending[pr.number] = base
        batch_ready = len(ready) > 1
        if batch_ready and not rebuild_stack:
            pending.update(
                (prs[branch].number, self.git.remote_branch_name(parent, self.remote)) for branch, parent in ready
            )
        closures, membership = self.validate_push_retargets(pending, rebuild_stack, allowed) if pending else ([], {})
        self.protect_retargets(to_push, stacks)

        def retarget(protect_refs: Callable[[], None]) -> None:
            check_refs = protect_refs if closures else lambda: None
            check_refs()
            self.close_orphans(closures, edits=self.retarget_edits(to_push, stacks) if closures else None)
            if rebuild_stack or batch_ready:
                self.apply_ready_retargets(ready, pending, membership, allowed, {pr.number for pr in closures})
            else:
                for branch, parent in ready:
                    self.retarget(branch, parent, allowed=allowed)

            check_refs()

        return retarget if ready or closures else None

    def validate_push_retargets(
        self, pending: dict[int, str], rebuild_stack: bool, allowed: set[int]
    ) -> tuple[list[OrphanedPullRequest], dict[int, GitHubStack]]:
        """Check pending base changes and retain the stack membership seen in preflight."""
        try:
            fields = self.pull_request_fields(pending, STACK_FIELDS)
        except StackError as e:
            if "Field 'stack' doesn't exist on type 'PullRequest'" not in str(e):
                raise StackError(
                    "couldn't check whether pull requests need unstacking before pushing; nothing was pushed"
                ) from e
            return [], {}
        stacks = {
            number: stack for number, pr in fields.items() if (stack := optional_field(pr, "stack", dict)) is not None
        }
        membership = {number: GitHubStack.from_json(stacks[number]) for number in sorted(stacks)}
        existing = list(membership.values())
        if existing and not rebuild_stack:
            changes = [f"Retarget #{n} to {pending[n]}" for n in sorted(stacks)]
            raise StackError(self.rebuild_hint(existing, allowed, changes))
        closures = self.plan_orphan_closures(existing, allowed)
        members = {n for stack in existing for n, state in stack.entries if state is GitHubState.OPEN}
        self.protect_edits(dict.fromkeys(members - {pr.number for pr in closures}), "unstack")
        return closures, membership

    def apply_ready_retargets(
        self,
        ready: list[tuple[str, str]],
        pending: dict[int, str],
        membership: dict[int, GitHubStack],
        allowed: set[int],
        closed: set[int],
    ) -> None:
        """Unstack known stacks, then retarget to bases that already exist."""
        prs = self.pull_requests()
        changes = {prs[branch].number: branch for branch, _ in ready}
        unstacked: set[int] = set()
        for number in changes:
            if (stack := membership.get(number)) is not None and stack.number not in unstacked:
                self.unstack(number, allowed, expected=stack, closed=closed, allow_new_closures=False)
                unstacked.add(stack.number)
        if len(ready) == 1:
            branch, parent = ready[0]
            self.retarget(branch, parent, True, allowed, allow_new_closures=False)
        elif changes:
            self.batch_retargets(changes, pending)

    def batch_retargets(self, changes: dict[int, str], targets: dict[int, str]) -> None:
        for numbers in batched(changes, PR_UPDATE_BATCH_SIZE, strict=False):
            self.retarget_batch({n: changes[n] for n in numbers}, targets)

    def retarget_batch(self, changes: dict[int, str], targets: dict[int, str]) -> None:
        prs = self.pull_requests()
        fields = f"{self.protection.fields} id"
        if not self.protection.protect:
            fields += " baseRefName"
        inputs: dict[int, dict[str, str]] = {}
        for number, live in self.pull_request_fields(changes, fields).items():
            base = targets[number]
            pr = prs[changes[number]]
            current = field(live, "baseRefName", str)
            if github_state(live) is not GitHubState.OPEN or current not in (pr.base, base):
                raise StackError(f"PR #{number} changed while preparing to retarget it; retry the command")
            if current == base:
                prs[changes[number]] = replace(pr, base=base)
                continue
            self.protection.check(live, "retarget", repo=f"{self.owner}/{self.name}", new_base=base)
            inputs[number] = {"pullRequestId": field(live, "id", str), "baseRefName": base}

        if inputs:
            for number in self.apply_pr_update_batch(inputs, "retarget", result_field="baseRefName"):
                branch = changes[number]
                self.record_retarget(branch, prs[branch], targets[number])

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
        edits = {number: base for number, base in self.retarget_edits(pushed, stacks).items() if base is not None}
        if len(edits) > 1 and not rebuild_stack:
            changes = {prs[b].number: b for b in pushed if b in prs and prs[b].number in edits}
            self.batch_retargets(changes, edits)
        else:
            for b in pushed:
                if b in prs:
                    self.retarget(b, stacks.parent(b), rebuild_stack, allowed, allow_new_closures=False)
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
        allow_new_closures: bool = True,
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
                        [f"Retarget #{pr.number} to {base}"],
                    )
                ) from e
            self.unstack(pr.number, allowed or set(), new_base=base, allow_new_closures=allow_new_closures)
            self.gh(*edit)
        self.record_retarget(branch, pr, base)

    def record_retarget(self, branch: str, pr: PullRequest, base: str) -> None:
        self.changes.append(
            f"#{pr.number}'s base was changed from {pr.base} to {base} "
            f"(undo with `gh pr edit {pr.number} --repo {self.repo} --base {pr.base}`)"
        )
        self.pull_requests()[branch] = replace(pr, base=base)
        self.core.emit(events.PullRequestRetargeted(branch, pr.number, base))

    @staticmethod
    def orphan_blockers(stacks: Iterable[GitHubStack], allowed: set[int]) -> dict[int, GitHubStack]:
        return {
            n: stack for stack in stacks for n, state in stack.entries if state is GitHubState.OPEN and n not in allowed
        }

    def rebuild_hint(self, stacks: list[GitHubStack], allowed: set[int], actions: list[str]) -> str:
        ids = sorted({stack.number for stack in stacks})
        noun = "stacks" if len(ids) > 1 else "stack"
        unstack = f"Unstack GitHub {noun} {', '.join(map(str, ids))} (keep merged PRs linked)"
        steps = "\n".join(f"- {step}" for step in [unstack, *actions])
        blockers = self.orphan_blockers(stacks, allowed)
        if not blockers:
            return f"Retry with --pr-rebuild.\n\n{steps}"
        refusals: list[str] = []
        try:
            candidates = self.orphan_details(blockers, refusals=refusals)
        except StackError as e:
            return f"Couldn't check rebuild blockers.\n{e}\n\n{steps}"
        draft_list = "\n".join(f"- {pr.url}" for pr in candidates)
        if refusals:
            problems = "\n".join(refusals)
            eligible = f"\n\nEligible drafts\n{draft_list}" if candidates else ""
            return f"Rebuild blocked. No drafts will close.\n{problems}{eligible}\n\n{steps}"
        return f"Retry with --pr-rebuild.\n\nDrafts to close (keep branches)\n{draft_list}\n\n{steps}"

    def plan_orphan_closures(self, stacks: Iterable[GitHubStack], allowed: set[int]) -> list[OrphanedPullRequest]:
        blockers = self.orphan_blockers(stacks, allowed)
        if not blockers:
            return []
        message = "can't rebuild GitHub stacks containing other open pull requests: " + ", ".join(
            f"#{n}" for n in sorted(blockers)
        )
        if not self.close_orphaned_prs:
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
        prs = self.pr_selections(sorted(blockers), fields)
        heads = {expected.number: expected.head} if expected is not None else {}
        # A final closure check already knows the head. Check its live
        # details, viewer and dependents in one request immediately before closing.
        query = self.repository_query(f"{prs} {self.dependent_selections(heads)}", heads, viewer=True)
        repo = field(query, "repository", dict)
        viewer = field(field(query, "viewer", dict), "login", str)
        check_github_login(viewer)
        candidates = []
        for number in sorted(blockers):
            try:
                data = repo.get(f"p{number}")
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
        if not candidates:
            return []
        if expected is None:
            heads = {pr.number: pr.head for pr in candidates}
            query = self.repository_query(self.dependent_selections(heads), heads)
            repo = field(query, "repository", dict)
        return self.filter_orphan_dependents(candidates, repo, refusals)

    @staticmethod
    def filter_orphan_dependents(
        candidates: list[OrphanedPullRequest], repo: dict[str, object], refusals: list[str] | None
    ) -> list[OrphanedPullRequest]:
        eligible = []
        for pr in candidates:
            dependents = field(field(repo, f"d{pr.number}", dict), "nodes", list)
            if dependents:
                message = f"can't close blocking PR #{pr.number}: open pull requests target its head {pr.head}"
                if refusals is None:
                    raise StackError(message)
                refusals.append(message)
            else:
                eligible.append(pr)
        return eligible

    @staticmethod
    def dependent_selections(heads: dict[int, str]) -> str:
        return " ".join(
            f"d{n}: pullRequests(baseRefName: $base{n}, states: OPEN, first: 1) {{ nodes {{ number }} }}" for n in heads
        )

    def close_orphans(
        self,
        candidates: list[OrphanedPullRequest],
        *,
        dry_run: bool = False,
        edits: dict[int, str | None] | None = None,
    ) -> None:
        if not candidates:
            return
        if dry_run:
            for pr in candidates:
                self.core.emit(events.OrphanedPullRequestClosure(pr.number, pr.url, pr.head, pr.base, closed=False))
            return
        if self.orphan_details({pr.number: pr.stack for pr in candidates}) != candidates:
            raise StackError("a blocking pull request changed before closure; retry the command")
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
        allow_new_closures: bool = True,
        expected: GitHubStack | None = None,
        closed: set[int] | None = None,
    ) -> None:
        """Take the unmerged pull requests of pull request number's stack on
        GitHub out of it. Merged ones stay in it. Closed ones outside allowed are
        taken out too, and this doesn't link them in again."""
        fields = self.pull_request_fields([number], STACK_FIELDS)
        stack_field = optional_field(fields[number], "stack", dict)
        if stack_field is None:
            if after_closures or expected is not None:
                return
            raise StackError(f"GitHub no longer reports #{number} in a stack; retry the command")
        github_stack = GitHubStack.from_json(stack_field)
        if (after_closures or not allow_new_closures) and self.orphan_blockers([github_stack], allowed):
            raise StackError("the GitHub stack gained other open pull requests; retry the command")
        if expected is not None and not github_stack.matches_after_closures(expected, closed or set()):
            raise StackError(f"#{number}'s GitHub stack changed while checking it; retry the command")
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
        return self.git.first_named_blob(ref, TEMPLATE_DIRECTORIES, TEMPLATE_NAMES)

    def create_draft_pull_requests(self, requests: list[tuple[str, str, str]]) -> Iterator[tuple[str, int, str]]:
        """Create drafts after publication and report all confirmed creations in a failed batch."""
        if not requests:
            return
        repository_id = self._repository_id
        if repository_id is None:
            # On a first push, discovery had no configured upstreams to look up.
            data = field(self.repository_query("id defaultBranchRef { name }"), "repository", dict)
            repository_id = self._repository_id = field(data, "id", str)
            default = optional_field(data, "defaultBranchRef", dict)
            self._default_branch = field(default, "name", str) if default else None
        body = self.pull_request_template() or ""
        prs = self.pull_requests()
        opened: list[str] = []
        for batch in batched(requests, PR_UPDATE_BATCH_SIZE, strict=False):
            failed: list[str] = []
            errors: list[str] = []
            try:
                data = self.create_draft_batch(batch, repository_id, body, errors)
            except StackError as error:
                errors.append(str(error))
                data = {}
            for i, (branch, base, _) in enumerate(batch):
                try:
                    result = field(data.get(f"p{i}"), "pullRequest", dict)
                    number, url = field(result, "number", int), field(result, "url", str)
                except StackError:
                    failed.append(branch)
                    continue
                prs[branch] = PullRequest(
                    number,
                    GitHubState.OPEN,
                    True,
                    False,
                    self.git.tip(branch),
                    self.git.remote_branch_name(base, self.remote),
                    None,
                )
                opened.append(f"#{number} for {branch}")
                yield branch, number, url
            if failed:
                succeeded = f" (opened {', '.join(opened)})" if opened else ""
                detail = "\n".join(errors) or "GitHub did not confirm creation"
                raise StackError(f"opening a pull request for {', '.join(failed)} failed{succeeded}: {detail}")

    def create_draft_batch(
        self, batch: tuple[tuple[str, str, str], ...], repository_id: str, body: str, errors: list[str]
    ) -> dict[str, object]:
        variables = {
            f"p{i}": {
                "repositoryId": repository_id,
                "baseRefName": self.git.remote_branch_name(base, self.remote),
                "headRefName": self.git.remote_branch_name(branch, self.remote),
                "title": title,
                "body": body,
                "draft": True,
                "maintainerCanModify": True,
            }
            for i, (branch, base, title) in enumerate(batch)
        }
        declarations = ", ".join(f"${alias}: CreatePullRequestInput!" for alias in variables)
        mutations = " ".join(
            f"{alias}: createPullRequest(input: ${alias}) {{ pullRequest {{ number url }} }}" for alias in variables
        )
        request = json.dumps({"query": f"mutation({declarations}) {{ {mutations} }}", "variables": variables})
        raw = self.gh("api", "graphql", "--hostname", self.host, "--input", "-", input=request, partial_errors=errors)
        response = parse_json(raw)
        with suppress(StackError):
            for error in optional_field(response, "errors", list) or []:
                with suppress(StackError):
                    message = optional_field(error, "message", str)
                    if message and not any(message in existing for existing in errors):
                        errors.append(message)
        return optional_field(response, "data", dict) or {}

    def pull_request_fields(self, numbers: Iterable[int], fields: str) -> dict[int, object]:
        """{number: its pull request's fields, a JSON object or None if there's
        no such pull request} for the pull requests numbers."""
        numbers = sorted(set(numbers))
        if not numbers:
            return {}
        query = self.repository_query(self.pr_selections(numbers, fields))
        repository = field(query, "repository", dict)
        return {n: repository.get(f"p{n}") for n in numbers}

    @staticmethod
    def pr_selections(numbers: Iterable[int], fields: str) -> str:
        return " ".join(f"p{n}: pullRequest(number: {n}) {{ {fields} }}" for n in numbers)

    def repository_query(
        self, fields: str, heads: dict[int, str] | None = None, *, viewer: bool = False
    ) -> dict[str, object]:
        declarations = ["$owner: String!", "$name: String!"]
        variables = ["-f", f"owner={self.owner}", "-f", f"name={self.name}"]
        for number, head in (heads or {}).items():
            declarations.append(f"$base{number}: String!")
            variables.extend(["-f", f"base{number}={head}"])
        viewer_fields = "viewer { login } " if viewer else ""
        query = (
            f"query({', '.join(declarations)}) {{ {viewer_fields}"
            f"repository(owner: $owner, name: $name) {{ {fields.strip()} }} }}"
        )
        data = parse_json(self.gh("api", "graphql", "--hostname", self.host, *variables, "-f", f"query={query}"))
        return field(data, "data", dict)

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
        edits: dict[int, str] = {}
        for root in sorted({stacks.root(b) for b in branches}):
            on_root = [b for b in branches if stacks.root(b) == root]
            edits.update(
                self.plan_stack_sections_on_root(
                    on_root,
                    stacks,
                    prs,
                    fields,
                    {b: to_update[b] for b in on_root if b in to_update},
                    merged,
                    rebuild_stack,
                    dry_run,
                )
            )
        if edits:
            self.update_descriptions(edits, fields)

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

    def plan_stack_sections_on_root(
        self,
        branches: list[str],
        stacks: Stacks,
        prs: dict[str, PullRequest],
        fields: dict[int, object],
        to_update: dict[str, int],
        group_history: dict[int, int],
        rebuild_stack: bool,
        dry_run: bool,
    ) -> dict[int, str]:
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
            return {}
        linked = (
            self.link_github_stacks(
                [[to_update[b] for b in path] for path in paths], github_stacks, rebuild_stack, dry_run
            )
            if shown
            else set[int]()
        )
        if dry_run:
            return {}
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
        return edits

    def update_descriptions(self, edits: dict[int, str], fields: dict[int, object]) -> None:
        """Batch body-only changes and verify each mutation's result."""
        if len(edits) == 1:
            number = next(iter(edits))
            self.protect_edits({number: None}, "edit the description of")
            self.gh("pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", input=edits[number])
            return
        for batch in batched(edits, PR_UPDATE_BATCH_SIZE, strict=False):
            self.protect_edits(dict.fromkeys(batch), "edit the description of")
            inputs = {n: {"pullRequestId": field(fields[n], "id", str), "body": edits[n]} for n in batch}
            for _ in self.apply_pr_update_batch(inputs, "update descriptions for"):
                pass

    def apply_pr_update_batch(
        self, updates: dict[int, dict[str, str]], action: str, *, result_field: str = ""
    ) -> Iterator[int]:
        """Yield confirmed changes before raising any error from a partial result."""
        inputs = {f"p{n}": update for n, update in updates.items()}
        declarations = ", ".join(f"${alias}: UpdatePullRequestInput!" for alias in inputs)
        mutations = " ".join(
            f"{alias}: updatePullRequest(input: ${alias}) {{ pullRequest {{ number {result_field} }} }}"
            for alias in inputs
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
        failed: list[int] = []
        for n in updates:
            entry = data.get(f"p{n}")
            expected: dict[str, object] = {"number": n}
            if result_field:
                expected[result_field] = updates[n][result_field]
            if isinstance(entry, dict) and entry.get("pullRequest") == expected:
                yield n
            else:
                failed.append(n)
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
            raise StackError(f"couldn't {action} {names}; rerun to update the remaining pull requests{details}")

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
        if self._lookup_branches is not None:
            # A former base can still have an open PR outside the selected
            # stacks. Consult it only when its merge history would be shown.
            unqueried = {
                head
                for previous in changes.values()
                for head, at in previous
                if (history := merges.get(head)) and history[-1][0] <= at
            } - set(self._lookup_branches)
            if unqueried:
                stacks = self.core.stacks()
                open_former_bases = self.look_up_pull_requests(
                    (head for head in sorted(unqueried) if head in stacks), open_only=True
                )
                for head in open_former_bases:
                    del merges[head]
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
                list(found.values()), set(chain), [f"Link {', '.join(f'#{n}' for n in chain)} in a new stack"]
            )
            self.core.emit(events.GitHubStackMismatch(tuple(chain), tuple(sorted(found)), hint))
            return []
        return chain if self._stack_creation_available and self.stacks_api("", chain) else []

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
        value = self.git.config_value(GITHUB_STACKS_SETTING)
        if value is None:
            return True
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
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
            if not path:
                self._stack_creation_available = False
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
    """The host ssh connects to for host, after ~/.ssh/config's Host aliases, or
    host itself if ssh can't tell."""
    try:
        result = subprocess.run(
            ["ssh", "-G", host], capture_output=True, encoding="utf-8", stdin=subprocess.DEVNULL, check=False
        )
    except OSError:
        return host
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        key, _, value = line.partition(" ")
        if key == "hostname" and value:
            return value
    return host
