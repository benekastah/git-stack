# pyright: reportUnknownVariableType=false
import enum
import json
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Self

from git_stack import events, stack_section
from git_stack.adapters.git import GitAdapter, PullRequestLink, PullRequestState
from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient
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

    @classmethod
    def from_json(cls, data: object) -> Self:
        nodes = sorted(field(field(data, "entries", dict), "nodes", list), key=lambda n: field(n, "position", int))
        prs = [field(node, "pullRequest", dict) for node in nodes]
        return cls(
            field(data, "number", int),
            tuple((field(pr, "number", int), github_state(pr)) for pr in prs),
        )


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


class GitHubAdapter(GitAdapter):
    """Asks GitHub, through the gh CLI, whether each branch's pull request was
    merged, and keeps pull requests' bases in step with the stack when
    branches are pushed.

    Branches without a merged pull request (for example, one merged into its
    parent locally) are checked like GitAdapter does, unless their parent is
    the repository's default branch, which we assume nobody merges into
    locally.
    """

    def __init__(self, core: StackCore, remote: str, host: str, owner: str, name: str, fetch: bool) -> None:
        super().__init__(core)
        self.remote = remote
        self.fetch = fetch
        self.host, self.owner, self.name = host, owner, name
        self.repo = f"{host}/{owner}/{name}"
        self._pull_requests: dict[str, PullRequest] | None = None
        # So branches checked in parallel (as info does) look them up only once.
        self._pull_requests_lock = threading.Lock()
        self._default_branch: str | None = None
        self._viewer: str | None = None

    def gh(self, *args: str, input: str | None = None) -> str:
        # As bytes, so Windows doesn't turn the \n in input (e.g. a pull
        # request's description) into \r\n.
        result = subprocess.run(
            ["gh", *args], capture_output=True, input=None if input is None else input.encode("utf-8"), check=False
        )
        if result.returncode != 0:
            raise StackError(f"gh {' '.join(args)} failed:\n{result.stderr.decode('utf-8', 'replace').strip()}")
        return result.stdout.decode("utf-8").strip()

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
        branches = [b for b in self.core.stacks() if self.git.config_get(b, "remote") == self.remote]
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
        self.fetch_missing_heads(merged)
        return next(
            (pr for pr in merged if self.git.rev(pr.head_oid) and self.git.is_ancestor(base, pr.head_oid)), None
        )

    def fetch_missing_heads(self, prs: list[PullRequest]) -> None:
        """Fetch the head commits of prs that we don't have, which GitHub keeps
        after their branches are deleted.

        A pull request's last commit can be made on GitHub (e.g. by "Update
        branch") right before it's merged and its branch deleted, so we never
        fetch it. Without it, we can't tell the pull request is the branch's.
        If the fetch fails, or the adapter may not fetch, those pull requests
        are just ignored.
        """
        if not self.fetch:
            return
        refs = [f"refs/pull/{pr.number}/head" for pr in prs if not self.git.rev(pr.head_oid)]
        if refs:
            self.git.run("fetch", "--quiet", "--no-write-fetch-head", self.remote, *refs, check=False)

    @property
    def default_branch(self) -> str | None:
        """The repository's default branch, once pull requests were looked up
        (by open_pull_requests, open_pull_request_by_number or pull_requests),
        or None."""
        return self._default_branch

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

    def before_push(
        self,
        to_push: list[str],
        stacks: Stacks,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
        opening: bool = False,
    ) -> None:
        """Point the open pull requests of branches about to be pushed at their
        stack parents, where the parent is already on the remote.

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
        pending = {
            prs[b].number
            for b in to_push
            if b in prs
            and prs[b].state is GitHubState.OPEN
            and prs[b].base != self.git.remote_branch_name(stacks.parent(b), self.remote)
            and self.git.rev(f"refs/remotes/{self.remote}/{self.git.remote_branch_name(stacks.parent(b), self.remote)}")
            is None
        }
        if pending:
            try:
                fields = self.pull_request_fields(pending, STACK_FIELDS)
            except StackError as e:
                if "Field 'stack' doesn't exist on type 'PullRequest'" not in str(e):
                    raise StackError(
                        "couldn't check whether pull requests need unstacking before pushing; nothing was pushed"
                    ) from e
                fields = {}
            stacked = sorted(n for n, pr in fields.items() if optional_field(pr, "stack", dict) is not None)
            if stacked and not rebuild_stack:
                raise StackError(
                    f"changing the base of {', '.join(f'#{n}' for n in stacked)} requires unstacking its "
                    "GitHub stack; rerun with --rebuild-stack to allow it"
                )
            if rebuild_stack:
                for n in stacked:
                    stack = optional_field(fields[n], "stack", dict)
                    if stack is not None:
                        self.ensure_stack_scope(GitHubStack.from_json(stack), allowed)
        for b in to_push:
            if (
                self.git.rev(f"refs/remotes/{self.remote}/{self.git.remote_branch_name(stacks.parent(b), self.remote)}")
                is not None
            ):
                self.retarget(b, stacks.parent(b), rebuild_stack, allowed)

    def after_push(
        self,
        pushed: list[str],
        stacks: Stacks,
        opening: bool = False,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
    ) -> None:
        """Point the open pull requests of pushed branches at their stack parents,
        if before_push couldn't because the parent wasn't on the remote yet, and
        say which pushed branches have none, unless they're opened next."""
        prs = self.pull_requests()
        allowed = {
            prs[b].number
            for b in (scope if scope is not None else pushed)
            if b in prs and prs[b].state is GitHubState.OPEN
        }
        for b in pushed:
            if b in prs:
                self.retarget(b, stacks.parent(b), rebuild_stack, allowed)
        without = tuple(b for b in pushed if b not in prs)
        if without and not opening:
            self.core.emit(events.NoPullRequests(without))

    def retarget(self, branch: str, base: str, rebuild_stack: bool = False, allowed: set[int] | None = None) -> None:
        """Point branch's pull request at base, if it's open and isn't already."""
        prs = self.pull_requests()
        pr = prs.get(branch)
        if pr is None or pr.state is not GitHubState.OPEN:
            return
        self.ensure_pr_parent_remote(branch, base)
        base = self.git.remote_branch_name(base, self.remote)
        if pr.base == base:
            return
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
                raise StackError(
                    f"changing #{pr.number}'s base requires unstacking its GitHub stack; "
                    "rerun this command with --rebuild-stack to allow it"
                ) from e
            self.unstack(pr.number, allowed or set())
            self.gh(*edit)
        self.changes.append(
            f"#{pr.number}'s base was changed from {pr.base} to {base} "
            f"(undo with `gh pr edit {pr.number} --repo {self.repo} --base {pr.base}`)"
        )
        prs[branch] = replace(pr, base=base)
        self.core.emit(events.PullRequestRetargeted(branch, pr.number, base))

    def ensure_stack_scope(self, stack: GitHubStack, allowed: set[int]) -> None:
        """Refuse to unstack open PRs outside the selected local stack; closed and
        merged ones don't stop it."""
        unrelated = sorted(n for n, state in stack.entries if state is GitHubState.OPEN and n not in allowed)
        if unrelated:
            raise StackError(
                "can't rebuild GitHub stacks containing other open pull requests: "
                + ", ".join(f"#{n}" for n in unrelated)
            )

    def unstack(self, number: int, allowed: set[int]) -> None:
        """Take the unmerged pull requests of pull request number's stack on
        GitHub out of it. Merged ones stay in it. Closed ones outside allowed are
        taken out too, and this doesn't link them in again."""
        fields = self.pull_request_fields([number], STACK_FIELDS)
        stack_field = optional_field(fields[number], "stack", dict)
        if stack_field is None:
            raise StackError(f"GitHub no longer reports #{number} in a stack; retry the command")
        github_stack = GitHubStack.from_json(stack_field)
        self.ensure_stack_scope(github_stack, allowed)
        found = parse_json(self.gh("api", "--hostname", self.host, f"{self.stacks_path()}?pull_request={number}"))
        if not isinstance(found, list) or not found:
            raise StackError(f"GitHub says #{number} is in a stack, but doesn't say which: {found!r}")
        stack = field(found[0], "number", int)
        if stack != github_stack.number:
            raise StackError(f"#{number}'s GitHub stack changed while checking it; retry the command")
        current = {field(pr, "number", int) for pr in field(found[0], "pull_requests", list)}
        if current != {n for n, _ in github_stack.entries}:
            raise StackError(f"GitHub stack {stack} changed while checking it; retry the command")
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
        query = f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {prs} }} }}"
        repository = field(
            field(
                parse_json(
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
                        f"query={query}",
                    )
                ),
                "data",
                dict,
            ),
            "repository",
            dict,
        )
        return {n: repository.get(f"p{n}") for n in numbers}

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
        link_github_stack). Once a path's pull requests are all in one,
        which GitHub shows on each pull request itself, they get no section,
        and the ones they have are removed. The other pull requests' sections
        show the whole stack.
        """
        prs = self.pull_requests()
        root = stacks.parent(branches[0])

        def node(branch: str) -> stack_section.Node:
            return prs[branch].number if branch in prs else branch

        current = {node(b): node(stacks.parent(b)) if stacks.parent(b) in branches else root for b in branches}
        to_update = {b: prs[b].number for b in branches if b in prs and prs[b].state is GitHubState.OPEN}
        try:
            fields = self.pull_request_fields(to_update.values(), f"body {BASE_CHANGES} {STACK_FIELDS}")
        except StackError:
            # E.g. GitHub Enterprise without stacked pull requests, which
            # doesn't know the field.
            fields = self.pull_request_fields(to_update.values(), f"body {BASE_CHANGES}")
        bodies = {n: field(pr, "body", str) for n, pr in fields.items()}
        on_root = [n for b, n in to_update.items() if stacks.parent(b) not in branches]
        merged = self.merged_parents({n: fields[n] for n in on_root}, root)
        # Each pull request on root goes on the merged one it was on, on root.
        tree = dict(current)
        for number, parent in merged.items():
            tree[number] = parent
            tree[parent] = root
        shown = sum(isinstance(n, int) for n in tree) >= 2
        github_stacks = {
            n: GitHubStack.from_json(stack) if (stack := optional_field(pr, "stack", dict)) is not None else None
            for n, pr in fields.items()
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
        for number in to_update.values():
            body = bodies[number]
            if number in linked or not shown:
                new_body = stack_section.remove(body)
            else:
                new_body = stack_section.replace(body, stack_section.render(root, tree, set(merged.values()), number))
            if new_body != body:
                self.gh("pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", input=new_body)

    def link_github_stacks(
        self, chains: list[list[int]], stacks: dict[int, GitHubStack | None], rebuild_stack: bool, dry_run: bool
    ) -> set[int]:
        """Link each of chains of two or more pull requests into a stack on
        GitHub (see link_github_stack). Returns the pull requests in those
        that are all in one."""
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
        for stack in found.values():
            self.ensure_stack_scope(stack, set(to_update.values()))
        if can_link:
            self.core.emit(events.GitHubStackRebuildPlanned(tuple(sorted(found)), tuple(chain)))
        else:
            self.core.emit(events.GitHubStackUnstackPlanned(tuple(sorted(found)), tuple(sorted(affected))))
        return True

    def merged_parents(self, prs: dict[int, object], root: str) -> dict[int, int]:
        """{number: the merged pull request it was last stacked on} for the
        pull requests prs, {number: its fields, with BASE_CHANGES}, on root.

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
        heads = {head for c in changes.values() for head, _ in c} - {root} - open_heads
        merges = self.merged_pull_requests(sorted(heads))
        found = {}
        for n, c in changes.items():
            # GitHub's timestamps are all UTC, in the same format, so they
            # compare as strings.
            parent = next((m for head, at in c for merged_at, m in merges.get(head, []) if merged_at <= at), None)
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
        requests are chain's bottom ones (and which doesn't end in a merged
        one), or their stack is made, if none is in one. Pull requests can't
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
            return chain if self.stacks_api(f"/{stack.number}/add", chain[len(bottom) :]) else kept
        if dry_run and not found:
            self.core.emit(events.GitHubStackRebuildPlanned((), tuple(chain)))
            return []
        if rebuild_stack and found:
            return chain if self.rebuild_github_stack(chain, found, dry_run) else []
        if found:
            self.core.emit(events.GitHubStackMismatch(tuple(chain), tuple(sorted(found))))
            return []
        return chain if self.stacks_api("", chain) else []

    def rebuild_github_stack(self, chain: list[int], found: dict[int, GitHubStack], dry_run: bool) -> bool:
        """Unstack the stacks on GitHub found, {number: stack}, which don't fit
        chain, then link chain into a new one (see link_github_stack)."""
        for existing in found.values():
            self.ensure_stack_scope(existing, set(chain))
        if dry_run:
            self.core.emit(events.GitHubStackRebuildPlanned(tuple(sorted(found)), tuple(chain)))
            return False
        for existing in found.values():
            representative = next(n for n, state in existing.entries if state is GitHubState.OPEN and n in chain)
            self.unstack(representative, set(chain))
        return self.stacks_api("", chain)

    def github_stacks_enabled(self) -> bool:
        setting = self.git.run("config", "--type=bool", "--get", GITHUB_STACKS_SETTING, check=False)
        return setting != "false"

    def stacks_api(self, path: str, numbers: list[int]) -> bool:
        """POST numbers to path in the repository's stacks API. Returns
        False if the repository doesn't have stacked pull requests."""
        result = subprocess.run(
            [
                "gh",
                "api",
                "--hostname",
                self.host,
                "-X",
                "POST",
                f"{self.stacks_path()}{path}",
                "--input",
                "-",
            ],
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
    """Return (host, owner, name) of remote's repository, or None if its URL isn't one.

    Tries the URL as configured, then with url.<base>.insteadOf applied. For an
    SSH URL, host is the one ssh connects to, so a Host alias in ~/.ssh/config
    (e.g. github-work for github.com) is resolved.
    """
    for url in (
        git.config_value(f"remote.{remote}.url") or "",
        git.run("remote", "get-url", remote, check=False),
    ):
        match = REMOTE_URL.match(url)
        if match:
            host, owner, name = match.groups()
            if "://" not in url or url.startswith(("ssh://", "git+ssh://", "ssh+git://")):
                host = ssh_hostname(host)
            return host, owner, name
    return None


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
