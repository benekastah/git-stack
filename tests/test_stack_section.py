"""git_stack.stack_section's tree handling, which test_prs.py doesn't reach."""

from git_stack import stack_section


def test_render_nests_siblings_pull_requests_first() -> None:
    assert stack_section.render("main", {"pr2b": 1, 3: 1, 1: "main"}, "pr2b").splitlines()[4:-1] == [
        "- `main`",
        "  - #1",
        "    - #3",
        "    - `pr2b` 👈",
    ]
