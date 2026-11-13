import json
import re
import subprocess
import threading
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Self, TypeVar

from git_stack import events, stack_section
from git_stack.adapters.git import GitAdapter, PullRequestLink, PullRequestState
from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, Parents, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
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
# Whether prs links each linear stack's pull requests into a stack on GitHub
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


T = TypeVar("T")


def parse_json(out: str) -> object:
    try:
        parsed: object = json.loads(out)
    except ValueError as e:
        raise StackError(f"unexpected response from GitHub: {e}") from e
    return parsed


def field(data: object, key: str, kind: type[T]) -> T:
    """data[key], checking that data is a JSON object and the value a kind."""
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, kind):
        raise StackError(f"unexpected response from GitHub: expected {key} to be a {kind.__name__} in {data!r}")
    return value


def optional_field(data: object, key: str, kind: type[T]) -> T | None:
    """Like field, but a missing or null value is None."""
    if isinstance(data, dict) and data.get(key) is None:
        return None
    return field(data, key, kind)


@dataclass(frozen=True)
class PullRequest:
    """A pull request, with the fields in PR_FIELDS."""

    number: int
    state: str
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
            state=field(data, "state", str),
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
    entries: tuple[tuple[int, str], ...]

    @classmethod
    def from_json(cls, data: object) -> Self:
        nodes = sorted(field(field(data, "entries", dict), "nodes", list), key=lambda n: field(n, "position", int))
        prs = [field(node, "pullRequest", dict) for node in nodes]
        return cls(field(data, "number", int), tuple((field(pr, "number", int), field(pr, "state", str)) for pr in prs))


@dataclass(frozen=True)
class OpenPullRequest:
    """An open (or draft) pull request of head, a branch in the repository,
    into base."""

    number: int
    head: str
    base: str
    # Its author's login, or None if their account was deleted.
    author: str | None = None


def login_of(user: object) -> str | None:
    """The login of a GraphQL user (an author), or None for a deleted account."""
    return None if user is None else field(user, "login", str)


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
        result = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8", input=input, check=False
        )
        if result.returncode != 0:
            raise StackError(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def pull_requests(self) -> dict[str, PullRequest]:
        """{branch: pull request} for tracked branches with a pull request.

        Only branches whose upstream is the remote branch of the same name can
        have one: a remote branch the branch doesn't track (or a pull request
        from one) is most likely someone else's that happens to share its name.

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
        branches = [b for b in self.core.tracked_branches() if self.git.tracks_same_name(b, self.remote)]
        if not branches:
            return {}
        # Branch names are passed as variables, so they needn't be escaped.
        fields = " ".join(
            f"b{i}: pullRequests(headRefName: $b{i}, first: {PRS_PER_BRANCH}, "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}) {{ nodes {{ {PR_FIELDS} }} }}"
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
            args += ["-f", f"b{i}={branch}"]
        repository = field(field(parse_json(self.gh(*args)), "data", dict), "repository", dict)
        default_branch = optional_field(repository, "defaultBranchRef", dict)
        pull_requests = {}
        for i, branch in enumerate(branches):
            nodes = field(field(repository, f"b{i}", dict), "nodes", list)
            pr = self.pick_pull_request(branch, [PullRequest.from_json(node) for node in nodes])
            if pr is not None:
                pull_requests[branch] = pr
        self._default_branch = field(default_branch, "name", str) if default_branch else None
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
        open_pr = next((pr for pr in prs if pr.state == "OPEN"), None)
        if open_pr is not None:
            return open_pr
        base = self.git.config_get(branch, BASE_KEY)
        if base is None:
            return None
        merged = [pr for pr in prs if pr.state == "MERGED"]
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
        (by open_pull_requests or pull_requests), or None."""
        return self._default_branch

    @property
    def viewer(self) -> str | None:
        """The gh user's login, once open_pull_requests looked it up, or None."""
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
            self._viewer = field(field(data, "viewer", dict), "login", str)
            default_branch = optional_field(field(data, "repository", dict), "defaultBranchRef", dict)
            self._default_branch = field(default_branch, "name", str) if default_branch else None
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

    def open_pull_request_author(self, number: int) -> str | None:
        """The login of the author of pull request number, or None if it isn't
        open (or doesn't exist, or its author's account was deleted)."""
        query = (
            "query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) "
            "{ pullRequest(number: $number) { state author { login } } } }"
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
        repository = field(field(parse_json(output), "data", dict), "repository", dict)
        pr = optional_field(repository, "pullRequest", dict)
        if pr is None or field(pr, "state", str) != "OPEN":
            return None
        return login_of(optional_field(pr, "author", dict))

    def prefetch(self) -> None:
        self.pull_requests()

    def pull_request_link(self, branch: str) -> PullRequestLink | None:
        pr = self.pull_requests().get(branch)
        if pr is None:
            return None
        state = (
            PullRequestState.MERGED
            if pr.state == "MERGED"
            else PullRequestState.DRAFT
            if pr.draft
            else PullRequestState.OPEN
        )
        return PullRequestLink(pr.number, f"https://{self.host}/{self.owner}/{self.name}/pull/{pr.number}", state)

    def merge_state(self, branch: str, parent: str, ignore_revert: bool = False) -> MergeState:
        git = self.git
        pr = self.pull_requests().get(branch)
        if pr is not None and pr.state == "OPEN" and parent == self._default_branch:
            return MergeState.NOT_MERGED
        merged = self.trusted_merge(branch, parent)
        if merged is None:
            return super().merge_state(branch, parent, ignore_revert)
        if not ignore_revert and self.reverted(branch, parent):
            return MergeState.REVERTED
        head = merged.head_oid
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if self.unmerged_commits(f"refs/heads/{branch}", head, parent) or (
            upstream_tip and self.unmerged_commits(upstream_tip, head, parent)
        ):
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
        if pr is None or pr.state != "MERGED":
            return None
        merge = pr.merge_oid
        if not (merge and git.rev(merge) and git.is_ancestor(merge, f"refs/heads/{parent}") and git.rev(pr.head_oid)):
            return None
        return pr

    def landing_merges(self, branch: str, parent: str) -> tuple[str, ...]:
        pr = self.trusted_merge(branch, parent)
        return (pr.merge_oid,) if pr is not None and pr.merge_oid else ()

    def unmerged_commits(self, ref: str, merged_head: str, parent: str) -> set[str]:
        """Non-merge commits on ref that aren't in parent and don't match a commit on merged_head."""
        unmatched = self.git.run("rev-list", "--cherry-pick", "--right-only", "--no-merges", f"{merged_head}...{ref}")
        return set(unmatched.split()) & set(
            self.git.run("rev-list", "--no-merges", ref, "--not", f"refs/heads/{parent}").split()
        )

    def before_push(
        self, to_push: list[str], parents: Parents, rebuild_stack: bool = False, scope: list[str] | None = None
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
        allowed = {
            prs[b].number for b in (scope if scope is not None else to_push) if b in prs and prs[b].state == "OPEN"
        }
        pending = {
            prs[b].number
            for b in to_push
            if b in prs
            and prs[b].state == "OPEN"
            and prs[b].base != parents[b]
            and self.git.rev(f"refs/remotes/{self.remote}/{parents[b]}") is None
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
            if self.git.rev(f"refs/remotes/{self.remote}/{parents[b]}") is not None:
                self.retarget(b, parents[b], rebuild_stack, allowed)

    def after_push(
        self,
        pushed: list[str],
        parents: Parents,
        opening: bool = False,
        rebuild_stack: bool = False,
        scope: list[str] | None = None,
    ) -> None:
        """Point the open pull requests of pushed branches at their stack parents,
        if before_push couldn't because the parent wasn't on the remote yet, and
        say which pushed branches have none, unless they're opened next."""
        prs = self.pull_requests()
        allowed = {
            prs[b].number for b in (scope if scope is not None else pushed) if b in prs and prs[b].state == "OPEN"
        }
        for b in pushed:
            if b in prs:
                self.retarget(b, parents[b], rebuild_stack, allowed)
        without = tuple(b for b in pushed if b not in prs)
        if without and not opening:
            self.core.emit(events.NoPullRequests(without))

    def retarget(self, branch: str, base: str, rebuild_stack: bool = False, allowed: set[int] | None = None) -> None:
        """Point branch's pull request at base, if it's open and isn't already."""
        prs = self.pull_requests()
        pr = prs.get(branch)
        if pr is None or pr.state != "OPEN" or pr.base == base:
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
        prs[branch] = replace(pr, base=base)
        self.core.emit(events.PullRequestRetargeted(branch, pr.number, base))

    def ensure_stack_scope(self, stack: GitHubStack, allowed: set[int]) -> None:
        """Refuse to unstack PRs outside the selected local stack."""
        unrelated = sorted(n for n, state in stack.entries if state != "MERGED" and n not in allowed)
        if unrelated:
            raise StackError(
                "can't rebuild GitHub stacks containing other unmerged pull requests: "
                + ", ".join(f"#{n}" for n in unrelated)
            )

    def unstack(self, number: int, allowed: set[int]) -> None:
        """Take the open pull requests of pull request number's stack on GitHub
        out of it. Merged ones stay in it."""
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
            base,
            "--head",
            branch,
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
        self.pull_requests()[branch] = PullRequest(number, "OPEN", True, False, self.git.tip(branch), base, None)
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
        self, branches: list[str], parents: Parents, rebuild_stack: bool = False, dry_run: bool = False
    ) -> None:
        """Make the stack section at the end of the description of each open
        pull request of branches, a stack parent-before-child, show the stack,
        marking that pull request (see git_stack.stack_section).

        Merged pull requests aren't shown, except that each pull request on
        root is shown on the merged one it was last stacked on (see
        merged_parents), which links to the pull requests before it. A stack
        with fewer than two pull requests, counting those, gets no section, and
        one it has is removed.

        If branches are a chain of open pull requests, each on the one before,
        they're linked into a stack on GitHub first (see link_github_stack).
        Once they're all in one, which GitHub shows on each pull request
        itself, they get no section, and the ones they have are removed.
        """
        prs = self.pull_requests()
        root = parents[branches[0]]

        def node(branch: str) -> stack_section.Node:
            return prs[branch].number if branch in prs else branch

        current = {node(b): node(parents[b]) if parents[b] in branches else root for b in branches}
        to_update = {b: prs[b].number for b in branches if b in prs and prs[b].state == "OPEN"}
        try:
            fields = self.pull_request_fields(to_update.values(), f"body {BASE_CHANGES} {STACK_FIELDS}")
        except StackError:
            # E.g. GitHub Enterprise without stacked pull requests, which
            # doesn't know the field.
            fields = self.pull_request_fields(to_update.values(), f"body {BASE_CHANGES}")
        bodies = {n: field(pr, "body", str) for n, pr in fields.items()}
        on_root = [n for b, n in to_update.items() if parents[b] not in branches]
        merged = self.merged_parents({n: fields[n] for n in on_root}, root)
        # Each pull request on root goes on the merged one it was on, on root.
        tree = dict(current)
        for number, parent in merged.items():
            tree[number] = parent
            tree[parent] = root
        shown = sum(isinstance(n, int) for n in tree) >= 2
        chain = [to_update[b] for b in branches] if is_chain(branches, parents, root, to_update) else []
        stacks = {
            n: GitHubStack.from_json(stack) if (stack := optional_field(pr, "stack", dict)) is not None else None
            for n, pr in fields.items()
        }
        if dry_run and rebuild_stack and self.plan_retargets(to_update, prs, parents, stacks, chain):
            return
        linked = shown and len(chain) > 1 and self.link_github_stack(chain, stacks, rebuild_stack, dry_run)
        if dry_run:
            return
        for number in to_update.values():
            body = bodies[number]
            if linked or not shown:
                new_body = stack_section.remove(body)
            else:
                new_body = stack_section.replace(body, stack_section.render(root, tree, set(merged.values()), number))
            if new_body != body:
                self.gh("pr", "edit", str(number), "--repo", self.repo, "--body-file", "-", input=new_body)

    def plan_retargets(
        self,
        to_update: dict[str, int],
        prs: dict[str, PullRequest],
        parents: Parents,
        stacks: dict[int, GitHubStack | None],
        chain: list[int],
    ) -> bool:
        """Report unstacking needed for retargets in a dry run, if any."""
        affected = [n for b, n in to_update.items() if prs[b].base != parents[b] and stacks[n] is not None]
        if not affected:
            return False
        can_link = len(chain) > 1 and self.github_stacks_enabled()
        found = {
            stack.number: stack for n, stack in stacks.items() if stack is not None and (n in affected or can_link)
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
        open_heads = {b for b, pr in self.pull_requests().items() if pr.state == "OPEN"}
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
    ) -> bool:
        """Put chain, open pull requests each on the one before, bottom first,
        in one stack on GitHub, unless GITHUB_STACKS_SETTING turns that off.
        stacks is each one's stack on GitHub, if it's in one. Returns whether
        they're all in one.

        They're all in one if its open pull requests are chain, in order.
        Otherwise their stack is made, if none is in one, or the rest are
        added to the top of the one whose open pull requests are chain's
        bottom ones. Pull requests can't be taken out of a stack, so if
        they're in several, or in one that doesn't fit, they're left alone,
        with a warning. So is a repository without stacked pull requests.
        """
        found = {stack.number: stack for stack in stacks.values() if stack is not None}
        (stack,) = found.values() if len(found) == 1 else (None,)
        bottom = [n for n, state in stack.entries if state == "OPEN"] if stack is not None else []
        if bottom == chain:
            return True
        if not self.github_stacks_enabled():
            return False
        if dry_run and not found:
            self.core.emit(events.GitHubStackRebuildPlanned((), tuple(chain)))
            return False
        if rebuild_stack and found:
            unrelated = sorted(
                n
                for existing in found.values()
                for n, state in existing.entries
                if state != "MERGED" and n not in chain
            )
            if unrelated:
                raise StackError(
                    "can't rebuild GitHub stacks containing other unmerged pull requests: "
                    + ", ".join(f"#{n}" for n in unrelated)
                )
            if dry_run:
                self.core.emit(events.GitHubStackRebuildPlanned(tuple(sorted(found)), tuple(chain)))
                return False
            for existing in found.values():
                representative = next(n for n, state in existing.entries if state == "OPEN" and n in chain)
                self.unstack(representative, set(chain))
            return self.stacks_api("", chain)
        if (
            len(found) > 1
            or bottom != chain[: len(bottom)]
            or (stack is not None and stack.entries and stack.entries[-1][1] != "OPEN")
        ):
            self.core.emit(events.GitHubStackMismatch(tuple(chain), tuple(sorted(found))))
            return False
        if stack is None:
            return self.stacks_api("", chain)
        return self.stacks_api(f"/{stack.number}/add", chain[len(bottom) :])

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
            text=True,
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


def is_chain(branches: list[str], parents: Parents, root: str, open_prs: dict[str, int]) -> bool:
    """Whether branches, parent-before-child, each have an open pull request
    (in open_prs) and are each stacked on the one before, the first on root."""
    return all(b in open_prs and parents[b] == (branches[i - 1] if i else root) for i, b in enumerate(branches))


def github_repo(git: GitClient, remote: str) -> tuple[str, str, str] | None:
    """Return (host, owner, name) of remote's repository, or None if its URL isn't one.

    Tries the URL as configured, then with url.<base>.insteadOf applied. For an
    SSH URL, host is the one ssh connects to, so a Host alias in ~/.ssh/config
    (e.g. github-work for github.com) is resolved.
    """
    for url in (
        git.run("config", "--get", f"remote.{remote}.url", check=False),
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
            ["ssh", "-G", host], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False
        )
    except OSError:
        return host
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        key, _, value = line.partition(" ")
        if key == "hostname" and value:
            return value
    return host
