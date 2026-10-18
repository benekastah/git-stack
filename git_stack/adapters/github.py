import json
import re
import subprocess

from git_stack import events
from git_stack.adapters.git import GitAdapter
from git_stack.adapters.merge_state import MergeState
from git_stack.core import PR_KEY
from git_stack.errors import StackError

# Matches https://host/owner/repo, ssh://user@host:port/owner/repo and
# user@host:owner/repo, with or without .git.
REMOTE_URL = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
PR_FIELDS = "number state headRefOid baseRefName mergeCommit { oid }"


class GitHubAdapter(GitAdapter):
    """Asks GitHub, through the gh CLI, whether each branch's pull request was
    merged, and opens draft pull requests when branches are pushed.

    Branches without a merged pull request (for example, one merged into its
    parent locally) are checked like GitAdapter does, unless their parent is
    the repository's default branch, which we assume nobody merges into
    locally.
    """

    def __init__(self, core, host, owner, name):
        super().__init__(core)
        self.host, self.owner, self.name = host, owner, name
        self.repo = f"{host}/{owner}/{name}"
        self._pull_requests = None
        self._default_branch = None

    def gh(self, *args):
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise StackError(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
        return result.stdout.strip()

    def pull_requests(self):
        """{branch: pull request} for tracked branches with a recorded pull request.

        Looked up with one API call per command. If that fails, emits
        PullRequestLookupFailed and returns {}, so merges are only detected
        locally.
        """
        if self._pull_requests is None:
            self._pull_requests = {}
            numbers = {}
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
                found = json.loads(out)["data"]["repository"]
                self._pull_requests = {branch: found[f"pr{n}"] for branch, n in numbers.items()
                                       if found.get(f"pr{n}")}
                self._default_branch = (found.get("defaultBranchRef") or {}).get("name")
            except (StackError, ValueError, KeyError, TypeError) as e:
                self.core.emit(events.PullRequestLookupFailed(str(e)))
        return self._pull_requests

    def merge_state(self, branch, parent):
        git = self.git
        pr = self.pull_requests().get(branch)
        if pr and pr["state"] != "MERGED" and parent == self._default_branch:
            return MergeState.NOT_MERGED
        merge = pr and pr["state"] == "MERGED" and (pr.get("mergeCommit") or {}).get("oid")
        # Trust GitHub only once the merge is in the local parent, so children
        # aren't restacked onto a parent that lacks it, and only if we have the
        # merged commits, so we can tell whether anything came after them.
        if not (merge and git.rev(merge) and git.is_ancestor(merge, parent) and git.rev(pr["headRefOid"])):
            return super().merge_state(branch, parent)
        head = pr["headRefOid"]
        upstream_tip = git.rev(f"{branch}@{{upstream}}")
        if (self.unmerged_commits(f"refs/heads/{branch}", head, parent)
                or (upstream_tip and self.unmerged_commits(upstream_tip, head, parent))):
            return MergeState.MERGED_WITH_NEW_COMMITS
        return MergeState.MERGED

    def unmerged_commits(self, ref, merged_head, parent):
        """Non-merge commits on ref that aren't in parent and don't match a commit on merged_head."""
        unmatched = self.git.run("rev-list", "--cherry-pick", "--right-only", "--no-merges",
                                 f"{merged_head}...{ref}")
        return (set(unmatched.split())
                & set(self.git.run("rev-list", "--no-merges", ref, "--not", parent).split()))

    def after_push(self, branch, branches, parents):
        """Open a draft pull request for branch if it has none, and point the
        open pull requests of the pushed branches at their stack parents."""
        if branch in branches and not self.git.config_get(branch, PR_KEY):
            self.record_pull_request(branch, parents[branch])
        prs = self.pull_requests()
        for b in branches:
            pr = prs.get(b)
            if pr and pr["state"] == "OPEN" and pr["baseRefName"] != parents[b]:
                self.gh("pr", "edit", str(pr["number"]), "--repo", self.repo, "--base", parents[b])
                self.core.emit(events.PullRequestRetargeted(b, pr["number"], parents[b]))

    def record_pull_request(self, branch, parent):
        """Record branch's open pull request, opening a draft one if there is none."""
        found = json.loads(self.gh("pr", "list", "--repo", self.repo, "--head", branch,
                                   "--state", "open", "--json", "number"))
        if found:
            number = found[0]["number"]
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


def github_repo(git, remote):
    """Return (host, owner, name) of remote's repository, or None if its URL isn't one.

    Tries the URL as configured, then with url.<base>.insteadOf applied.
    """
    for url in (git.run("config", "--get", f"remote.{remote}.url", check=False),
                git.run("remote", "get-url", remote, check=False)):
        match = REMOTE_URL.match(url)
        if match:
            return match.groups()
    return None
