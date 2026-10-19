import json
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Self, TypeVar

from git_stack import events
from git_stack.adapters.git import GitAdapter
from git_stack.adapters.merge_state import MergeState
from git_stack.core import BASE_KEY, Parents, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
PR_FIELDS = "number state isCrossRepository headRefOid baseRefName mergeCommit { oid }"
# How many of a branch's pull requests to look at, newest first. More than one
# only if its name was reused, or a pull request was closed and replaced.
PRS_PER_BRANCH = 10


T = TypeVar("T")


def parse_json(out: str) -> object:
    try:
        parsed: object = json.loads(out)
    except ValueError as e:
        raise StackError(f"unexpected response from GitHub: {e}")
    return parsed


def field(data: object, key: str, kind: type[T]) -> T:
    """data[key], checking that data is a JSON object and the value a kind."""
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, kind):
        raise StackError(f"unexpected response from GitHub: expected {key} to be a "
                         f"{kind.__name__} in {data!r}")
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
            cross_repository=field(data, "isCrossRepository", bool),
            head_oid=field(data, "headRefOid", str),
            base=field(data, "baseRefName", str),
            merge_oid=field(merge_commit, "oid", str) if merge_commit is not None else None,
        )


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
        self._default_branch: str | None = None

    def gh(self, *args: str) -> str:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
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
            raise StackError(f"couldn't look up pull requests on GitHub; check `gh auth status` (and, for "
                             f"an organization with SSO, that gh's token is authorized for it): {e}")

    def look_up_pull_requests(self) -> dict[str, PullRequest]:
        branches = [b for b in self.core.tracked_branches() if self.git.tracks_same_name(b, self.remote)]
        if not branches:
            return {}
        # Branch names are passed as variables, so they needn't be escaped.
        fields = " ".join(f"b{i}: pullRequests(headRefName: $b{i}, first: {PRS_PER_BRANCH}, "
                          f"orderBy: {{field: CREATED_AT, direction: DESC}}) {{ nodes {{ {PR_FIELDS} }} }}"
                          for i in range(len(branches)))
        variables = "".join(f", $b{i}: String!" for i in range(len(branches)))
        query = (f"query($owner: String!, $name: String!{variables}) {{ repository(owner: $owner, name: $name) "
                 f"{{ defaultBranchRef {{ name }} {fields} }} }}")
        args = ["api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}",
                "-f", f"name={self.name}", "-f", f"query={query}"]
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
        return next((pr for pr in merged if self.git.rev(pr.head_oid) and self.git.is_ancestor(base, pr.head_oid)),
                    None)

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

    def merge_state(self, branch: str, parent: str) -> MergeState:
        git = self.git
        pr = self.pull_requests().get(branch)
        if pr is None or pr.state != "MERGED":
            if pr is not None and parent == self._default_branch:
                return MergeState.NOT_MERGED
            return super().merge_state(branch, parent)
        merge, head = pr.merge_oid, pr.head_oid
        # Trust GitHub only once the merge is in the local parent, so children
        # aren't restacked onto a parent that lacks it, and only if we have the
        # merged commits, so we can tell whether anything came after them.
        if not (merge and git.rev(merge) and git.is_ancestor(merge, parent) and git.rev(head)):
            return super().merge_state(branch, parent)
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if (self.unmerged_commits(f"refs/heads/{branch}", head, parent)
                or (upstream_tip and self.unmerged_commits(upstream_tip, head, parent))):
            return MergeState.MERGED_WITH_NEW_COMMITS
        return MergeState.MERGED

    def unmerged_commits(self, ref: str, merged_head: str, parent: str) -> set[str]:
        """Non-merge commits on ref that aren't in parent and don't match a commit on merged_head."""
        unmatched = self.git.run("rev-list", "--cherry-pick", "--right-only", "--no-merges",
                                 f"{merged_head}...{ref}")
        return (set(unmatched.split())
                & set(self.git.run("rev-list", "--no-merges", ref, "--not", parent).split()))

    def after_push(self, pushed: list[str], parents: Parents) -> None:
        """Point the open pull requests of pushed branches at their stack parents,
        and give a link to open one for each pushed branch that has none."""
        prs = self.pull_requests()
        for b in pushed:
            pr = prs.get(b)
            if pr is None:
                self.core.emit(events.NoPullRequest(b, self.compare_url(parents[b], b)))
            elif pr.state == "OPEN" and pr.base != parents[b]:
                self.gh("pr", "edit", str(pr.number), "--repo", self.repo, "--base", parents[b])
                self.core.emit(events.PullRequestRetargeted(b, pr.number, parents[b]))

    def compare_url(self, base: str, head: str) -> str:
        """The page for opening a pull request of head into base, with the
        repository's pull request template filled in."""
        base, head = (urllib.parse.quote(ref, safe="/") for ref in (base, head))
        return f"https://{self.host}/{self.owner}/{self.name}/compare/{base}...{head}?expand=1"


def github_repo(git: GitClient, remote: str) -> tuple[str, str, str] | None:
    """Return (host, owner, name) of remote's repository, or None if its URL isn't one.

    Tries the URL as configured, then with url.<base>.insteadOf applied. For an
    SSH URL, host is the one ssh connects to, so a Host alias in ~/.ssh/config
    (e.g. github-work for github.com) is resolved.
    """
    for url in (git.run("config", "--get", f"remote.{remote}.url", check=False),
                git.run("remote", "get-url", remote, check=False)):
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
        result = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError:
        return host
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        key, _, value = line.partition(" ")
        if key == "hostname" and value:
            return value
    return host
