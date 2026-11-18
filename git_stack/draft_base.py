"""The GitHub draft's managed ref points to the recorded replay boundary."""

from dataclasses import dataclass
from hashlib import sha256

from git_stack.errors import StackError
from git_stack.git import GitClient, is_object_id

KEY = "stackDraftBase"
PREFIX = "git-stack/draft-base/"


@dataclass(frozen=True)
class DraftBase:
    branch: str
    remote: str
    name: str
    previous: str
    tip: str

    @classmethod
    def owned(cls, git: GitClient, branch: str, remote: str) -> DraftBase | None:
        saved = (git.config_get(branch, KEY) or "").split()
        if not saved:
            return None
        if len(saved) not in (3, 4) or not is_object_id(saved[-1]):
            raise StackError(f"the recorded draft base for {branch} is invalid")
        return cls.plan(git, branch, remote, saved[-1])

    @classmethod
    def plan(cls, git: GitClient, branch: str, remote: str, tip: str) -> DraftBase:
        head = git.remote_branch_name(branch, remote)
        name = PREFIX + sha256(head.encode()).hexdigest()[:24]
        current = git.rev(f"refs/remotes/{remote}/{name}") or ""
        saved = (git.config_get(branch, KEY) or "").split()
        if saved:
            if len(saved) not in (3, 4):
                raise StackError(f"the recorded draft base for {branch} is invalid")
            old_remote, old_name, *expected = saved
            if old_remote != remote or old_name != name or not all(is_object_id(oid) for oid in expected):
                raise StackError(f"the recorded draft base for {branch} belongs to a different upstream")
            if current and current not in expected:
                raise StackError(f"{remote}/{name} changed outside git-stack; refusing to overwrite it")
        elif current:
            raise StackError(f"{remote}/{name} already exists; refusing to adopt it as a draft base")
        return cls(branch, remote, name, current, tip)

    @property
    def ref(self) -> str:
        return f"refs/heads/{self.name}"

    @property
    def lease(self) -> str:
        return f"--force-with-lease={self.ref}:{self.previous}"

    @property
    def refspec(self) -> str:
        return f"{self.tip}:{self.ref}"

    def record(self, git: GitClient) -> None:
        # Keep both expected tips until the push succeeds so a retry can
        # recover on either side of an interrupted push.
        tips = dict.fromkeys(filter(None, (self.previous, self.tip)))
        state = " ".join((self.remote, self.name, *tips))
        if git.config_get(self.branch, KEY) != state:
            git.config_set(self.branch, KEY, state)

    def confirm(self, git: GitClient) -> None:
        state = f"{self.remote} {self.name} {self.tip}"
        if git.config_get(self.branch, KEY) != state:
            git.config_set(self.branch, KEY, state)

    def publish(self, git: GitClient, no_verify: bool = False) -> None:
        if self.previous != self.tip:
            self.record(git)
            verify_options = ["--no-verify"] if no_verify else []
            if not git.passthrough("push", *verify_options, self.lease, self.remote, self.refspec):
                raise StackError(f"couldn't push the draft base for {self.branch}; retry after checking the remote")
        self.confirm(git)
