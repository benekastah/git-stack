"""git_stack.stack_section's tree handling, which test_prs.py doesn't reach."""

from git_stack import stack_section


def test_find_reads_back_a_branching_tree() -> None:
    tree: stack_section.Tree = {1: "main", 2: 1, 3: 2, "pr2b": 1}

    assert stack_section.find(f"text\n\n{stack_section.render('main', tree, {1}, 3)}") == tree


def test_render_nests_siblings_pull_requests_first() -> None:
    assert stack_section.render("main", {"pr2b": 1, 3: 1, 1: "main"}, set(), "pr2b").splitlines()[4:-1] == [
        "- `main`",
        "  - #1",
        "    - #3",
        "    - `pr2b` 👈",
    ]


def test_lay_out_puts_merged_pull_requests_back_on_their_old_parents() -> None:
    old: stack_section.Tree = {1: "main", 2: 1, 3: 2, 5: 1}

    # 2 stays on main, where it is now, rather than going back under 1; merged
    # 5 goes back on 1.
    assert stack_section.lay_out("main", {2: "main", 3: 2}, old, {1, 5}) == {1: "main", 5: 1, 2: "main", 3: 2}


def test_lay_out_puts_a_merged_pull_request_with_an_unknown_parent_on_the_root() -> None:
    assert stack_section.lay_out("main", {3: "main"}, {2: 1, 3: 2}, {2}) == {2: "main", 3: "main"}
