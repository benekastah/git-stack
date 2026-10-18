"""git stack run as a separate process, the way users run it, to check what
the mocked CLI tests can't: the entry point, and printing what StackClient
reports."""

from tests.helpers import FakeGitHub, Repo


def test_stack_through_squash_merge(repo: Repo) -> None:
    repo.stack("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.stack("branch", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.stack("push")
    repo.squash_merge_on_forge("pr1")

    result = repo.stack("sync", "-d", "local")

    assert "pr1 was merged into main; deleted it" in result.stdout
    assert "restacking pr2 onto main" in result.stdout
    assert result.stdout.endswith("pull requests of:\n  pr2\n")
    assert repo.stack("info").stdout == "main\n└── pr2 *\n"


def test_warnings_go_to_stderr(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "main")
    repo.git("merge", "--quiet", "--squash", "pr1")
    repo.git("commit", "--quiet", "-m", "pr1 (squashed)")
    repo.git("switch", "--quiet", "pr2")

    result = repo.stack("restack")

    assert "warning: pr1 looks merged into main" in result.stderr
    assert "looks merged" not in result.stdout


def test_failure_exits_nonzero_with_message(repo: Repo) -> None:
    result = repo.stack("restack", expect_success=False)

    assert result.returncode == 1
    assert result.stderr == "git stack: main isn't in a stack; switch to a stacked branch to restack its stack\n"


def test_push_opens_draft_pull_request(repo: Repo, gh: FakeGitHub) -> None:
    repo.stack("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")

    result = repo.stack("push")

    assert "opened draft pull request #1 for pr1: https://github.com/acme/widgets/pull/1" in result.stdout
