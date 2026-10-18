import json
import re
import subprocess
from dataclasses import dataclass
from typing import Self, TypeVar

from git_stack import events
from git_stack.adapters.git import GitAdapter
from git_stack.adapters.merge_state import MergeState
from git_stack.core import PR_KEY, Parents, StackCore
from git_stack.errors import StackError
from git_stack.git import GitClient

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
PR_FIELDS = "number state headRefOid baseRefName mergeCommit { oid }"


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
    head_oid: str
    base: str
    merge_oid: str | None

    @classmethod
    def from_json(cls, data: object) -> Self:
        merge_commit = optional_field(data, "mergeCommit", dict)
        return cls(
            number=field(data, "number", int),
            state=field(data, "state", str),
            head_oid=field(data, "headRefOid", str),
            base=field(data, "baseRefName", str),
            merge_oid=field(merge_commit, "oid", str) if merge_commit is not None else None,
        )


class GitHubAdapter(GitAdapter):
    """Asks GitHub, through the gh CLI, whether each branch's pull request was
    merged, and opens draft pull requests when branches are pushed.

    Branches without a merged pull request (for example, one merged into its
    parent locally) are checked like GitAdapter does, unless their parent is
    the repository's default branch, which we assume nobody merges into
    locally.
    """

    def __init__(self, core: StackCore, host: str, owner: str, name: str) -> None:
        super().__init__(core)
        self.host, self.owner, self.name = host, owner, name
        self.repo = f"{host}/{owner}/{name}"
        self._pull_requests: dict[str, PullRequest] | None = None
        self._default_branch: str | None = None

    def gh(self, *args: str) -> str:
        # In the repository, since `gh pr create --fill` reads its commits.
        result = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=self.git.directory)
        if result.returncode != 0:
            raise StackError(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def pull_requests(self) -> dict[str, PullRequest]:
        """{branch: pull request} for tracked branches with a recorded pull request.

        Looked up with one API call per command. If that fails, or GitHub's
        answer isn't what we asked for, emits PullRequestLookupFailed and
        returns {}, so merges are only detected locally.
        """
        if self._pull_requests is None:
            self._pull_requests = {}
            numbers: dict[str, int] = {}
            for branch in self.core.tracked_branches():
                number = self.git.config_get(branch, PR_KEY)
                if number and number.isdigit():
                    numbers[branch] = int(number)
            if not numbers:
                return self._pull_requests
            fields = " ".join(f"pr{n}: pullRequest(number: {n}) {{ {PR_FIELDS} }}"
                              for n in sorted(set(numbers.values())))
            query = (f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) "
                     f"{{ defaultBranchRef {{ name }} {fields} }} }}")
            try:
                out = self.gh("api", "graphql", "--hostname", self.host, "-f", f"owner={self.owner}",
                              "-f", f"name={self.name}", "-f", f"query={query}")
                repository = field(field(parse_json(out), "data", dict), "repository", dict)
                default_branch = optional_field(repository, "defaultBranchRef", dict)
                pull_requests = {}
                for branch, n in numbers.items():
                    # null if there's no such pull request.
                    data = optional_field(repository, f"pr{n}", dict)
                    if data is not None:
                        pull_requests[branch] = PullRequest.from_json(data)
                self._pull_requests = pull_requests
                self._default_branch = field(default_branch, "name", str) if default_branch else None
            except StackError as e:
                self.core.emit(events.PullRequestLookupFailed(str(e)))
        return self._pull_requests

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

    def after_push(self, branch: str, branches: list[str], parents: Parents) -> None:
        """Open a draft pull request for branch if it has none, and point the
        open pull requests of the pushed branches at their stack parents."""
        if branch in branches and not self.git.config_get(branch, PR_KEY):
            self.record_pull_request(branch, parents[branch])
        prs = self.pull_requests()
        for b in branches:
            pr = prs.get(b)
            if pr and pr.state == "OPEN" and pr.base != parents[b]:
                self.gh("pr", "edit", str(pr.number), "--repo", self.repo, "--base", parents[b])
                self.core.emit(events.PullRequestRetargeted(b, pr.number, parents[b]))

    def record_pull_request(self, branch: str, parent: str) -> None:
        """Record branch's open pull request, opening a draft one if there is none."""
        found = parse_json(self.gh("pr", "list", "--repo", self.repo, "--head", branch,
                                   "--state", "open", "--json", "number"))
        if not isinstance(found, list):
            raise StackError(f"unexpected response from GitHub: expected a list of pull requests, not {found!r}")
        if found:
            number = field(found[0], "number", int)
            self.core.emit(events.PullRequestFound(branch, number))
        else:
            url = self.gh("pr", "create", "--repo", self.repo, "--draft", "--base", parent,
                          "--head", branch, "--fill").splitlines()[-1]
            match = re.search(r"/pull/(\d+)$", url)
            if not match:
                raise StackError(f"couldn't tell which pull request gh opened for {branch} from {url!r}")
            number = int(match.group(1))
            self.core.emit(events.PullRequestOpened(branch, number, url))
        self.git.config_set(branch, PR_KEY, str(number))


def github_repo(git: GitClient, remote: str) -> tuple[str, str, str] | None:
    """Return (host, owner, name) of remote's repository, or None if its URL isn't one.

    Tries the URL as configured, then with url.<base>.insteadOf applied.
    """
    for url in (git.run("config", "--get", f"remote.{remote}.url", check=False),
                git.run("remote", "get-url", remote, check=False)):
        match = REMOTE_URL.match(url)
        if match:
            host, owner, name = match.groups()
            return host, owner, name
    return None
