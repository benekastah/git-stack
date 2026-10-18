"""git stack info."""

from tests.helpers import Repo


def test_info_shows_every_stack_as_a_tree(repo: Repo) -> None:
    repo.stack("branch", "pr1")
    repo.commit("a", "pr1\n", "pr1 c1")
    repo.stack("branch", "pr2")
    repo.commit("b", "pr2\n", "pr2 c1")
    repo.git("switch", "--quiet", "pr1")
    repo.stack("branch", "pr2b")
    repo.commit("c", "pr2b\n", "pr2b c1")
    repo.git("switch", "--quiet", "pr1")
    repo.commit("a", "pr1 more\n", "pr1 c2")
    repo.git("switch", "--quiet", "main")
    repo.stack("branch", "solo")

    assert repo.stack("info").stdout == (
        "main\n"
        "├── pr1\n"
        "│   ├── pr2 (needs restack)\n"
        "│   └── pr2b (needs restack)\n"
        "└── solo *\n")
