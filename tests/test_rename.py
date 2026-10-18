"""git stack rename."""

from tests.helpers import Repo


def test_rename_keeps_branch_and_children_in_stack(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr2")
    old_base = repo.config("pr2", "stackBase")

    repo.stack("rename", "middle")

    assert repo.git("branch", "--show-current") == "middle"
    assert repo.config("middle", "stackParent") == "pr1"
    assert repo.config("middle", "stackBase") == old_base
    assert repo.config("pr3", "stackParent") == "middle"
    assert repo.stack("info").stdout == (
        "main\n"
        "└── pr1\n"
        "    └── middle *\n"
        "        └── pr3\n")


def test_rename_other_branch_by_name(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr3")

    repo.stack("rename", "pr1", "first")

    assert repo.git("branch", "--show-current") == "pr3"
    assert repo.config("first", "stackParent") == "main"
    assert repo.config("pr2", "stackParent") == "first"


def test_rename_missing_branch_fails(repo: Repo) -> None:
    repo.make_stack()

    result = repo.stack("rename", "nope", "other", expect_success=False)

    assert "nope isn't a local branch" in result.stderr


def test_rename_pushed_branch_unsets_old_upstream(repo: Repo) -> None:
    repo.make_stack()
    repo.stack("push")
    repo.git("switch", "--quiet", "pr1")

    result = repo.stack("rename", "first")

    assert "origin/pr1 wasn't renamed" in result.stdout
    assert not repo.config_exists("first", "merge")
    repo.stack("push")
    assert repo.git("rev-parse", "--abbrev-ref", "first@{upstream}") == "origin/first"


def test_rename_forgets_pull_request(repo: Repo) -> None:
    repo.make_stack()
    repo.git("config", "branch.pr1.stackPr", "3")

    repo.stack("rename", "pr1", "first")

    assert not repo.config_exists("first", "stackPr")


def test_rename_to_existing_branch_changes_nothing(repo: Repo) -> None:
    repo.make_stack()
    repo.git("switch", "--quiet", "pr1")

    repo.stack("rename", "pr3", expect_success=False)

    assert repo.config("pr2", "stackParent") == "pr1"
    assert repo.git("branch", "--show-current") == "pr1"
