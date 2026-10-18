"""End-to-end tests for git-stack, run against throwaway repositories."""

import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "git-stack"
FAKE_GH = pathlib.Path(__file__).resolve().parent / "fake_gh.py"


def load_git_stack():
    """Import the git-stack script as a module."""
    loader = importlib.machinery.SourceFileLoader("git_stack", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class StackTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self.tmp.name)
        global_config = tmp / "gitconfig"
        global_config.write_text("[init]\n\tdefaultBranch = main\n")
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_EDITOR": "true",
        }
        self.origin = tmp / "origin.git"
        self.repo = tmp / "local"
        # A second clone standing in for GitHub's merge button.
        self.forge = tmp / "forge"
        subprocess.run(["git", "init", "--quiet", "--bare", self.origin], check=True, env=self.env)
        subprocess.run(["git", "clone", "--quiet", self.origin, self.repo],
                       check=True, env=self.env, capture_output=True)
        for name in ("a", "b", "c"):
            self.write(name, "base\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "init")
        self.git("push", "--quiet", "--set-upstream", "origin", "main")
        subprocess.run(["git", "clone", "--quiet", self.origin, self.forge],
                       check=True, env=self.env, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, env=self.env,
                                capture_output=True, text=True)
        if result.returncode != 0:
            self.fail(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def stack(self, *args, expect_success=True):
        result = subprocess.run([sys.executable, SCRIPT, *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=60)
        if expect_success and result.returncode != 0:
            self.fail(f"git stack {' '.join(args)} failed:\n{result.stdout}{result.stderr}")
        if not expect_success and result.returncode == 0:
            self.fail(f"git stack {' '.join(args)} unexpectedly succeeded:\n{result.stdout}")
        return result

    def write(self, name, content):
        (self.repo / name).write_text(content)

    def commit(self, name, content, message):
        self.write(name, content)
        self.git("commit", "--quiet", "-am", message)

    def make_stack(self):
        """main <- pr1 (two commits to a) <- pr2 (b) <- pr3 (c)."""
        self.stack("branch", "pr1")
        self.commit("a", "pr1 first\n", "pr1 c1")
        self.commit("a", "pr1 second\n", "pr1 c2")
        self.stack("branch", "pr2")
        self.commit("b", "pr2\n", "pr2 c1")
        self.stack("branch", "pr3")
        self.commit("c", "pr3\n", "pr3 c1")

    def squash_merge_on_forge(self, branch, delete_branch=True):
        """Squash-merge origin/<branch> into origin/main and (by default) delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", f"origin/{branch}", cwd=self.forge)
        self.git("commit", "--quiet", "-m", f"{branch} (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", *([f":{branch}"] if delete_branch else []),
                 cwd=self.forge)

    def commit_on_forge(self, branch, name, content, message):
        """Push a commit to origin/<branch>, as a collaborator would."""
        self.git("fetch", "--quiet", cwd=self.forge)
        self.git("switch", "--quiet", "-C", branch, f"origin/{branch}", cwd=self.forge)
        (self.forge / name).write_text(content)
        self.git("add", name, cwd=self.forge)
        self.git("commit", "--quiet", "-m", message, cwd=self.forge)
        self.git("push", "--quiet", "origin", branch, cwd=self.forge)

    def remote_subject(self, branch):
        """Subject of the commit at <branch> on the remote itself."""
        return self.git("log", "-1", "--format=%s", branch, cwd=self.origin)

    def config(self, branch, key):
        return self.git("config", f"branch.{branch}.{key}")

    def config_exists(self, branch, key):
        return subprocess.run(["git", "config", f"branch.{branch}.{key}"], cwd=self.repo,
                              env=self.env, capture_output=True).returncode == 0

    def own_commits(self, branch, parent):
        return self.git("log", "--format=%s", f"{parent}..{branch}").splitlines()

    def assert_untracked(self, branch):
        """branch still exists but is no longer in a stack."""
        self.assertTrue(self.git("branch", "--list", branch))
        self.assertFalse(self.config_exists(branch, "stackParent"))
        self.assertFalse(self.config_exists(branch, "stackBase"))

    def assert_stacked(self, branch, parent, subjects):
        """branch sits directly on parent's tip with exactly the given commits."""
        self.assertEqual(self.config(branch, "stackParent"), parent)
        self.assertEqual(self.config(branch, "stackBase"), self.git("rev-parse", parent))
        self.assertEqual(self.own_commits(branch, parent), subjects)


class RestackTest(StackTestCase):
    def test_restack_moves_whole_stack_after_parent_changes(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 review fix\n", "pr1 review fix")
        self.git("switch", "--quiet", "pr3")

        self.stack("restack")

        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        self.assertEqual(self.git("branch", "--show-current"), "pr3")
        self.assertNotIn("needs restack", self.stack("info").stdout)

    def test_restack_onto_retargets_after_local_squash_merge(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("switch", "--quiet", "pr2")

        self.stack("restack", "--onto", "main")

        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_restack_skips_branch_merged_locally(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")

        self.assertIn("pr1 (merged)", self.stack("info").stdout)
        result = self.stack("restack")

        self.assertIn("pr1 looks merged into main", result.stderr)
        self.assertEqual(self.git("branch", "--list", "pr1"), "pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_conflict_resolved_with_rebase_continue_records_new_base(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.commit("b", "main change\n", "main touches b")
        self.git("switch", "--quiet", "pr1")

        # pr1 restacks cleanly; pr2 conflicts and pr3 is left for later.
        result = self.stack("restack", expect_success=False)
        self.assertIn("restacking pr2 stopped on a conflict", result.stderr)

        self.write("b", "resolved\n")
        self.git("add", "b")
        self.git("rebase", "--continue")
        self.assertIn("pr2 *\n", self.stack("info").stdout)
        self.stack("restack")

        self.assert_stacked("pr1", "main", ["pr1 c2", "pr1 c1"])
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_aborted_restack_keeps_previous_base(self):
        self.make_stack()
        old_base = self.config("pr1", "stackBase")
        self.git("switch", "--quiet", "main")
        self.commit("a", "main change\n", "main touches a")
        self.git("switch", "--quiet", "pr1")

        self.stack("restack", expect_success=False)
        self.git("rebase", "--abort")

        self.assertIn("pr1 * (needs restack)", self.stack("info").stdout)
        self.assertEqual(self.config("pr1", "stackBase"), old_base)

    def test_aborted_restack_onto_keeps_previous_parent(self):
        self.make_stack()
        old_base = self.config("pr2", "stackBase")
        self.git("switch", "--quiet", "main")
        self.commit("b", "main change\n", "main touches b")
        self.git("switch", "--quiet", "pr2")

        self.stack("restack", "--onto", "main", expect_success=False)
        self.git("rebase", "--abort")

        self.assertEqual(self.config("pr2", "stackParent"), "pr1")
        self.assertEqual(self.config("pr2", "stackBase"), old_base)

    def test_restack_onto_rejects_cycle(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr1")

        for target in ("pr3", "pr1"):
            with self.subTest(target=target):
                result = self.stack("restack", "--onto", target, expect_success=False)
                self.assertIn("would create a cycle", result.stderr)
                self.assertEqual(self.config("pr1", "stackParent"), "main")

    def test_restack_failure_without_conflict_is_not_reported_as_conflict(self):
        self.make_stack()
        self.git("worktree", "add", "--quiet", pathlib.Path(self.tmp.name) / "wt", "pr2")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 review fix\n", "pr1 review fix")

        result = self.stack("restack", expect_success=False)

        self.assertIn("restacking pr2 onto pr1 failed", result.stderr)
        self.assertNotIn("conflict", result.stderr)
        self.assertFalse(self.config_exists("pr2", "stackPendingParent"))

    def test_track_rejects_cycle(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr1")

        result = self.stack("track", "pr3", expect_success=False)

        self.assertIn("would create a cycle", result.stderr)
        self.assertEqual(self.config("pr1", "stackParent"), "main")

    def test_track_stacks_several_existing_branches(self):
        self.git("switch", "--quiet", "-c", "pr1")
        self.commit("a", "pr1\n", "pr1 c1")
        self.git("switch", "--quiet", "-c", "pr2")
        self.commit("b", "pr2\n", "pr2 c1")
        self.git("switch", "--quiet", "-c", "pr3")
        self.commit("c", "pr3\n", "pr3 c1")
        self.git("switch", "--quiet", "main")

        self.stack("track", "main", "pr1", "pr2", "pr3")

        self.assert_stacked("pr1", "main", ["pr1 c1"])
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_track_several_branches_writes_nothing_if_one_is_bad(self):
        self.git("switch", "--quiet", "-c", "pr1")
        self.commit("a", "pr1\n", "pr1 c1")
        self.git("switch", "--quiet", "main")

        result = self.stack("track", "main", "pr1", "missing", expect_success=False)

        self.assertIn("missing isn't a local branch", result.stderr)
        self.assert_untracked("pr1")

    def test_track_several_branches_rejects_cycle(self):
        self.make_stack()

        result = self.stack("track", "pr2", "pr3", "pr1", expect_success=False)

        self.assertIn("would create a cycle", result.stderr)
        self.assertEqual(self.config("pr3", "stackParent"), "pr2")
        self.assertEqual(self.config("pr1", "stackParent"), "main")

    def test_track_rejects_base_outside_branch_history(self):
        self.git("switch", "--quiet", "-c", "pr1")
        self.commit("a", "pr1\n", "pr1 c1")
        self.git("switch", "--quiet", "main")
        self.commit("b", "main change\n", "main touches b")
        self.git("switch", "--quiet", "pr1")

        result = self.stack("track", "main", "--base", "main", expect_success=False)

        self.assertIn("isn't in pr1's history", result.stderr)
        self.assertFalse(self.config_exists("pr1", "stackParent"))

    def test_restack_squash_squashes_each_branch_in_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")
        self.commit("b", "pr2 second\n", "pr2 c2")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 third\n", "pr1 c3")
        self.git("switch", "--quiet", "pr2")

        self.stack("restack", "--squash")

        self.assert_stacked("pr1", "main", ["pr1 c1"])
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        self.assertEqual(self.git("log", "-1", "--format=%B", "pr1"), "pr1 c1\n\npr1 c2\n\npr1 c3")
        self.assertEqual(self.git("log", "-1", "--format=%B", "pr2"), "pr2 c1\n\npr2 c2")
        self.assertEqual([self.git("show", f"pr3:{name}") for name in "abc"],
                         ["pr1 third", "pr2 second", "pr3"])
        self.assertEqual(self.git("branch", "--show-current"), "pr2")

    def test_restack_squash_resolves_a_line_edited_by_every_commit_once(self):
        self.stack("branch", "pr1")
        self.commit("a", "function(A, B)\n", "pr1 c1")
        self.stack("branch", "pr2")
        self.commit("a", "function(A, B, D)\n", "pr2 c1")
        self.commit("a", "function(A, B, E)\n", "pr2 c2")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "function(A, F)\n", "pr1 c2")
        self.git("switch", "--quiet", "pr2")

        result = self.stack("restack", "--squash", expect_success=False)
        self.assertIn("restacking pr2 stopped on a conflict", result.stderr)
        self.write("a", "function(A, F, E)\n")
        self.git("add", "a")
        # The only conflict: continuing finishes the rebase.
        self.git("rebase", "--continue")
        self.stack("restack")

        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assertEqual(self.git("show", "pr2:a"), "function(A, F, E)")

    def test_restack_squash_refuses_outside_a_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")

        result = self.stack("restack", "--squash", expect_success=False)

        self.assertIn("main isn't in a stack", result.stderr)
        self.assertEqual(self.own_commits("pr1", "main"), ["pr1 c2", "pr1 c1"])

    def test_track_existing_branch_after_parent_was_squash_merged(self):
        self.git("switch", "--quiet", "-c", "pr1")
        self.commit("a", "pr1 first\n", "pr1 c1")
        self.commit("a", "pr1 second\n", "pr1 c2")
        old_pr1_tip = self.git("rev-parse", "HEAD")
        self.git("switch", "--quiet", "-c", "pr2")
        self.commit("b", "pr2\n", "pr2 c1")
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("branch", "--quiet", "-D", "pr1")
        self.git("switch", "--quiet", "pr2")

        self.stack("track", "main", "--base", old_pr1_tip)
        self.stack("restack")

        self.assert_stacked("pr2", "main", ["pr2 c1"])


class RenameTest(StackTestCase):
    def test_rename_keeps_branch_and_children_in_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")
        old_base = self.config("pr2", "stackBase")

        self.stack("rename", "middle")

        self.assertEqual(self.git("branch", "--show-current"), "middle")
        self.assertEqual(self.config("middle", "stackParent"), "pr1")
        self.assertEqual(self.config("middle", "stackBase"), old_base)
        self.assertEqual(self.config("pr3", "stackParent"), "middle")
        self.assertEqual(self.stack("info").stdout, (
            "main\n"
            "└── pr1\n"
            "    └── middle *\n"
            "        └── pr3\n"))

    def test_rename_other_branch_by_name(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr3")

        self.stack("rename", "pr1", "first")

        self.assertEqual(self.git("branch", "--show-current"), "pr3")
        self.assertEqual(self.config("first", "stackParent"), "main")
        self.assertEqual(self.config("pr2", "stackParent"), "first")

    def test_rename_missing_branch_fails(self):
        self.make_stack()

        result = self.stack("rename", "nope", "other", expect_success=False)

        self.assertIn("nope isn't a local branch", result.stderr)

    def test_rename_pushed_branch_unsets_old_upstream(self):
        self.make_stack()
        self.stack("push")
        self.git("switch", "--quiet", "pr1")

        result = self.stack("rename", "first")

        self.assertIn("origin/pr1 wasn't renamed", result.stdout)
        self.assertFalse(self.config_exists("first", "merge"))
        self.stack("push")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "first@{upstream}"), "origin/first")

    def test_rename_forgets_pull_request(self):
        self.make_stack()
        self.git("config", "branch.pr1.stackPr", "3")

        self.stack("rename", "pr1", "first")

        self.assertFalse(self.config_exists("first", "stackPr"))

    def test_rename_to_existing_branch_changes_nothing(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr1")

        self.stack("rename", "pr3", expect_success=False)

        self.assertEqual(self.config("pr2", "stackParent"), "pr1")
        self.assertEqual(self.git("branch", "--show-current"), "pr1")


class InfoTest(StackTestCase):
    def test_info_shows_every_stack_as_a_tree(self):
        self.stack("branch", "pr1")
        self.commit("a", "pr1\n", "pr1 c1")
        self.stack("branch", "pr2")
        self.commit("b", "pr2\n", "pr2 c1")
        self.git("switch", "--quiet", "pr1")
        self.stack("branch", "pr2b")
        self.commit("c", "pr2b\n", "pr2b c1")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 more\n", "pr1 c2")
        self.git("switch", "--quiet", "main")
        self.stack("branch", "solo")

        self.assertEqual(self.stack("info").stdout, (
            "main\n"
            "├── pr1\n"
            "│   ├── pr2 (needs restack)\n"
            "│   └── pr2b (needs restack)\n"
            "└── solo *\n"))


class SyncTest(StackTestCase):
    def test_sync_after_parent_squash_merged(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.git("switch", "--quiet", "pr1")

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "origin/main"))
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        self.assertEqual(self.git("branch", "--show-current"), "pr1")

    def test_sync_delete_local_deletes_merged_branch(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.git("switch", "--quiet", "pr1")

        self.stack("sync", "-d", "local")

        self.assertEqual(self.git("branch", "--list", "pr1"), "")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        # We were on pr1, which no longer exists, so we land on its parent.
        self.assertEqual(self.git("branch", "--show-current"), "main")

    def test_sync_after_parent_squash_merged_with_remote_branch_kept(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1", delete_branch=False)

        result = self.stack("sync")

        self.assertIn("origin/pr1 still exists", result.stdout)
        self.assert_untracked("pr1")
        self.assertEqual(self.remote_subject("pr1"), "pr1 c2")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_sync_delete_remote_deletes_only_remote_branch(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1", delete_branch=False)

        self.stack("sync", "--delete", "remote")

        self.assert_untracked("pr1")
        self.assertEqual(self.git("branch", "--list", "pr1", cwd=self.origin), "")
        self.assertEqual(self.git("branch", "--remotes", "--list", "origin/pr1"), "")
        self.assert_stacked("pr2", "main", ["pr2 c1"])

    def test_sync_delete_both_deletes_local_and_remote_branches(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1", delete_branch=False)

        self.stack("sync", "-d", "both")

        self.assertEqual(self.git("branch", "--list", "pr1"), "")
        self.assertEqual(self.git("branch", "--list", "pr1", cwd=self.origin), "")
        self.assert_stacked("pr2", "main", ["pr2 c1"])

    def test_sync_after_local_squash_merge_pushed(self):
        self.make_stack()
        self.stack("push")
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("push", "--quiet")

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_sync_after_local_fast_forward_merge_of_unpushed_branch(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--ff-only", "pr1")
        self.git("push", "--quiet")

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])

    def check_sync_after_squash_merge_followed_by_edit_to_same_lines(self, delete_branch):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1", delete_branch=delete_branch)
        self.commit_on_forge("main", "a", "later\n", "main edits a again")

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_sync_after_squash_merge_followed_by_edit_to_same_lines(self):
        self.check_sync_after_squash_merge_followed_by_edit_to_same_lines(delete_branch=True)

    def test_sync_after_squash_merge_with_remote_branch_kept_followed_by_edit_to_same_lines(self):
        self.check_sync_after_squash_merge_followed_by_edit_to_same_lines(delete_branch=False)

    def test_sync_after_rebase_merge_followed_by_edit_to_same_lines(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("main", "d", "main\n", "main moves on")
        # Rebase-merge pr1, so main gets copies of its commits, then edit
        # the same line again.
        self.git("switch", "--quiet", "-C", "pr1", "origin/pr1", cwd=self.forge)
        self.git("rebase", "--quiet", "main", cwd=self.forge)
        self.git("switch", "--quiet", "main", cwd=self.forge)
        self.git("merge", "--quiet", "--ff-only", "pr1", cwd=self.forge)
        (self.forge / "a").write_text("later\n")
        self.git("commit", "--quiet", "-am", "main edits a again", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", ":pr1", cwd=self.forge)

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])

    def test_sync_skips_merged_branch_with_commits_pushed_after_merge(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1", delete_branch=False)
        self.commit_on_forge("pr1", "d", "late\n", "pushed after merge")

        result = self.stack("sync")

        self.assertIn("skipping pr1: it was merged into main, but it or its remote branch has commits",
                      result.stderr)
        self.assertIn("skipping pr2 because its parent pr1 was skipped", result.stderr)
        self.assertEqual(self.config("pr2", "stackParent"), "pr1")

    def test_sync_keeps_new_branch_without_commits(self):
        self.stack("branch", "pr1")
        self.stack("push")

        self.stack("sync")

        self.assertEqual(self.git("branch", "--list", "pr1"), "* pr1")
        self.assertEqual(self.config("pr1", "stackParent"), "main")

    def test_sync_after_two_levels_squash_merged(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.git("pull", "--quiet", cwd=self.forge)
        # GitHub retargets pr2 onto main; merging it then squashes pr1's
        # original commits and pr2's together.
        self.git("merge", "--quiet", "--squash", "origin/pr2", cwd=self.forge)
        self.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", ":pr2", cwd=self.forge)
        self.git("switch", "--quiet", "pr3")

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_untracked("pr2")
        self.assert_stacked("pr3", "main", ["pr3 c1"])
        self.assertEqual(self.git("branch", "--show-current"), "pr3")

    def test_sync_keeps_branch_deleted_without_merging(self):
        self.make_stack()
        self.stack("push")
        self.git("push", "--quiet", "origin", ":pr1", cwd=self.forge)

        result = self.stack("sync")

        self.assertIn("pr1's remote branch is gone but its changes aren't in main", result.stderr)
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])

    def test_sync_after_child_squash_merged_into_parent(self):
        self.make_stack()
        self.stack("push")
        self.git("fetch", "--quiet", cwd=self.forge)
        self.git("switch", "--quiet", "-c", "pr1", "origin/pr1", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", "origin/pr2", cwd=self.forge)
        self.git("commit", "--quiet", "-m", "pr2 (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "pr1", ":pr2", cwd=self.forge)

        self.stack("sync")

        self.assert_untracked("pr2")
        self.assertEqual(self.git("rev-parse", "pr1"), self.git("rev-parse", "origin/pr1"))
        self.assert_stacked("pr3", "pr1", ["pr3 c1"])

    def test_sync_fast_forwards_branch_to_commits_pushed_by_others(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("pr1", "d", "collab\n", "collab")

        self.stack("sync")

        self.assertEqual(self.git("rev-parse", "pr1"), self.git("rev-parse", "origin/pr1"))
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])

    def test_sync_pulls_commits_pushed_by_others_into_diverged_branch(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("pr1", "d", "collab\n", "collab")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 third\n", "pr1 c3")

        self.stack("sync")
        self.stack("sync")

        self.assertEqual(self.own_commits("pr1", "main"), ["collab", "pr1 c3", "pr1 c2", "pr1 c1"])
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])
        self.stack("push")
        self.assertEqual(self.remote_subject("pr1"), "collab")

    def test_sync_pull_conflict_resolved_with_cherry_pick_continue(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("pr1", "a", "collab\n", "collab")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 third\n", "pr1 c3")

        result = self.stack("sync", expect_success=False)
        self.assertIn("pulling upstream commits into pr1 stopped on a conflict", result.stderr)
        self.write("a", "resolved\n")
        self.git("add", "a")
        self.git("cherry-pick", "--continue")
        self.stack("sync")

        self.assertEqual(self.own_commits("pr1", "main"), ["collab", "pr1 c3", "pr1 c2", "pr1 c1"])
        self.assertEqual((self.repo / "a").read_text(), "resolved\n")
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])

    def test_sync_skips_broken_stack_but_syncs_the_rest(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.stack("branch", "solo")
        self.commit("a", "solo\n", "solo c1")
        self.git("switch", "--quiet", "main")
        self.git("branch", "--quiet", "-D", "pr1")
        self.commit_on_forge("main", "d", "upstream\n", "main moved")

        result = self.stack("sync")

        self.assertIn("skipping pr2: pr2's parent pr1 doesn't exist", result.stderr)
        self.assertIn("skipping pr3 because its parent pr2 was skipped", result.stderr)
        self.assert_stacked("solo", "main", ["solo c1"])

    def test_sync_leaves_diverged_trunk_alone(self):
        self.commit_on_forge("main", "d", "upstream\n", "upstream change")
        self.commit("a", "local\n", "local change")
        self.stack("branch", "pr1")
        local_main = self.git("rev-parse", "main")

        result = self.stack("sync")

        self.assertIn("main has diverged from origin/main", result.stderr)
        self.assertEqual(self.git("rev-parse", "main"), local_main)

    def test_push_refuses_to_drop_unpulled_commits(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("pr1", "d", "collab\n", "collab")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 third\n", "pr1 c3")
        self.git("fetch", "--quiet")

        result = self.stack("push", expect_success=False)

        self.assertIn("origin/pr1 has commits that pr1 doesn't", result.stderr)
        self.assertEqual(self.remote_subject("pr1"), "collab")

    def test_push_refuses_when_remote_moved_since_last_fetch(self):
        self.make_stack()
        self.stack("push")
        self.commit_on_forge("pr1", "d", "collab\n", "collab")
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "pr1 third\n", "pr1 c3")

        self.stack("push", expect_success=False)

        self.assertEqual(self.remote_subject("pr1"), "collab")

    def test_push_refuses_outside_a_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")

        result = self.stack("push", expect_success=False)

        self.assertIn("main isn't in a stack", result.stderr)
        self.assertEqual(self.git("branch", "--list", cwd=self.origin), "* main")

    def test_push_skips_branch_merged_locally(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("switch", "--quiet", "pr2")

        result = self.stack("push")

        self.assertIn("pr1 looks merged into main", result.stderr)
        self.assertEqual(self.git("branch", "--list", "--format=%(refname:short)", cwd=self.origin),
                         "main\npr2\npr3")

    def test_push_refuses_to_recreate_deleted_remote_branch(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.git("fetch", "--quiet", "--prune")
        self.git("switch", "--quiet", "pr2")
        self.commit("b", "pr2 again\n", "pr2 c2")

        result = self.stack("push", expect_success=False)

        self.assertIn("pr1's remote branch was deleted", result.stderr)
        self.assertEqual(self.git("branch", "--list", "pr1", cwd=self.origin), "")
        self.assertEqual(self.remote_subject("pr2"), "pr2 c1")

    def test_push_pushes_every_branch_in_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")

        self.stack("push")

        for branch in ("pr1", "pr2", "pr3"):
            self.assertEqual(self.git("rev-parse", f"origin/{branch}"), self.git("rev-parse", branch))
            self.assertEqual(self.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"),
                             f"origin/{branch}")


class GitHubTest(StackTestCase):
    def setUp(self):
        super().setUp()
        tmp = pathlib.Path(self.tmp.name)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").symlink_to(FAKE_GH)
        self.gh_state = tmp / "gh-state.json"
        self.save_gh_state({"prs": [], "calls": [], "fail": []})
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"
        self.env["FAKE_GH_STATE"] = str(self.gh_state)
        # Look like a GitHub repository while still using the local origin.
        url = "https://github.com/acme/widgets.git"
        self.git("config", f"url.{self.origin}.insteadOf", url)
        self.git("config", "remote.origin.url", url)

    def load_gh_state(self):
        return json.loads(self.gh_state.read_text())

    def save_gh_state(self, state):
        self.gh_state.write_text(json.dumps(state))

    def add_pr(self, **pr):
        state = self.load_gh_state()
        state["prs"].append(pr)
        self.save_gh_state(state)

    def gh_calls(self, *prefix):
        return [call for call in self.load_gh_state()["calls"] if call[:len(prefix)] == list(prefix)]

    def merge_pr1_on_forge_with_a_tweak(self):
        """Squash-merge pr1 with an extra change, so the merge doesn't match pr1's changes."""
        self.stack("push")
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", "origin/pr1", cwd=self.forge)
        (self.forge / "a").write_text("tweaked by the maintainer\n")
        self.git("commit", "--quiet", "-am", "pr1 (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", ":pr1", cwd=self.forge)
        self.git("config", "branch.pr1.stackPr", "5")
        self.add_pr(number=5, state="MERGED", headRefName="pr1", headRefOid=self.git("rev-parse", "pr1"),
                    baseRefName="main", mergeCommit={"oid": self.git("rev-parse", "HEAD", cwd=self.forge)})
        state = self.load_gh_state()
        state["calls"] = []
        self.save_gh_state(state)

    def test_push_opens_draft_pr_for_current_branch_only(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")

        self.stack("push")
        self.stack("push")

        prs = self.load_gh_state()["prs"]
        self.assertEqual([(p["headRefName"], p["baseRefName"], p["isDraft"]) for p in prs],
                         [("pr2", "pr1", True)])
        self.assertEqual(self.config("pr2", "stackPr"), "1")
        self.assertFalse(self.config_exists("pr1", "stackPr"))
        self.assertFalse(self.config_exists("pr3", "stackPr"))
        self.assertEqual(len(self.gh_calls("pr", "create")), 1)
        self.assertTrue(all("github.com/acme/widgets" in call for call in self.gh_calls("pr")))

    def test_push_from_merged_branch_opens_no_pr(self):
        self.make_stack()
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("switch", "--quiet", "pr1")

        self.stack("push")

        self.assertEqual(self.gh_calls("pr", "create"), [])
        self.assertEqual(self.git("branch", "--list", "pr1", cwd=self.origin), "")

    def test_push_records_existing_pr(self):
        self.make_stack()
        self.add_pr(number=7, state="OPEN", headRefName="pr1", headRefOid=None, baseRefName="main",
                    mergeCommit=None)
        self.git("switch", "--quiet", "pr1")

        self.stack("push")

        self.assertEqual(self.config("pr1", "stackPr"), "7")
        self.assertEqual(self.gh_calls("pr", "create"), [])

    def test_push_retargets_pr_whose_parent_changed(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.stack("sync")

        result = self.stack("push")

        self.assertIn("changed the base of pr2's pull request #1 to main", result.stdout)
        self.assertEqual(self.load_gh_state()["prs"][0]["baseRefName"], "main")

    def test_sync_trusts_merged_pr_whose_merge_differs_from_branch(self):
        self.make_stack()
        self.merge_pr1_on_forge_with_a_tweak()

        self.stack("sync")

        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        self.assertEqual(len(self.gh_calls("api", "graphql")), 1)

    def test_restack_ignores_merged_pr_until_parent_has_the_merge(self):
        self.make_stack()
        self.merge_pr1_on_forge_with_a_tweak()
        self.git("fetch", "--quiet")
        self.git("switch", "--quiet", "pr2")

        result = self.stack("restack")

        self.assertNotIn("looks merged", result.stderr)
        self.assertEqual(self.config("pr1", "stackParent"), "main")
        self.assertEqual(self.config("pr2", "stackParent"), "pr1")

    def test_sync_skips_merged_pr_with_local_commits_after_merge(self):
        self.make_stack()
        self.merge_pr1_on_forge_with_a_tweak()
        self.git("switch", "--quiet", "pr1")
        self.commit("a", "after merge\n", "pr1 after merge")

        result = self.stack("sync")

        self.assertIn("skipping pr1: it was merged into main, but it or its remote branch has commits",
                      result.stderr)
        self.assertEqual(self.config("pr1", "stackParent"), "main")

    def test_open_pr_on_default_branch_overrides_local_detection(self):
        self.make_stack()
        self.stack("push")
        self.git("config", "branch.pr1.stackPr", "5")
        self.add_pr(number=5, state="OPEN", headRefName="pr1", headRefOid=self.git("rev-parse", "pr1"),
                    baseRefName="main", mergeCommit=None)
        self.git("switch", "--quiet", "main")
        self.git("merge", "--quiet", "--squash", "pr1")
        self.git("commit", "--quiet", "-m", "pr1 (squashed)")
        self.git("push", "--quiet")

        self.assertIn("└── pr1 (needs restack)\n", self.stack("info").stdout)

    def test_open_pr_on_stacked_parent_falls_back_to_local_detection(self):
        self.make_stack()
        self.stack("push")
        self.git("config", "branch.pr2.stackPr", "6")
        self.add_pr(number=6, state="OPEN", headRefName="pr2", headRefOid=self.git("rev-parse", "pr2"),
                    baseRefName="pr1", mergeCommit=None)
        self.git("switch", "--quiet", "pr1")
        self.git("merge", "--quiet", "--squash", "pr2")
        self.git("commit", "--quiet", "-m", "pr2 (squashed)")
        self.git("push", "--quiet")

        self.stack("sync")

        self.assert_untracked("pr2")
        self.assert_stacked("pr3", "pr1", ["pr3 c1"])

    def test_failed_pr_lookup_falls_back_to_local_detection(self):
        self.make_stack()
        self.stack("push")
        self.git("config", "branch.pr1.stackPr", "5")
        self.squash_merge_on_forge("pr1")
        state = self.load_gh_state()
        state["fail"] = ["api"]
        self.save_gh_state(state)

        result = self.stack("sync")

        self.assertIn("couldn't look up pull requests on GitHub", result.stderr)
        self.assert_untracked("pr1")
        self.assert_stacked("pr2", "main", ["pr2 c1"])

    def test_git_adapter_setting_leaves_github_alone(self):
        self.make_stack()
        self.git("config", "stack.adapter", "git")

        self.stack("push")
        self.stack("sync")

        self.assertEqual(self.load_gh_state()["calls"], [])
        self.assertFalse(self.config_exists("pr3", "stackPr"))


class UpgradeTest(unittest.TestCase):
    OLD = "#!/usr/bin/env python3\nprint('old')\n"
    NEW = "#!/usr/bin/env python3\nprint('new')\n"

    def setUp(self):
        self.git_stack = load_git_stack()
        self.tmp = tempfile.TemporaryDirectory()
        tmp = pathlib.Path(self.tmp.name)
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.installed = self.bin / "git-stack"
        self.installed.write_text(self.OLD)
        self.installed.chmod(0o755)
        self.release = tmp / "release"

    def tearDown(self):
        self.tmp.cleanup()

    def upgrade(self, release_content):
        self.release.write_text(release_content)
        return self.git_stack.upgrade_file(str(self.installed), self.release.as_uri())

    def test_upgrade_replaces_script_and_keeps_it_executable(self):
        self.assertTrue(self.upgrade(self.NEW))

        self.assertEqual(self.installed.read_text(), self.NEW)
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.listdir(self.bin), ["git-stack"])

    def test_upgrade_reports_when_already_up_to_date(self):
        self.assertFalse(self.upgrade(self.OLD))

    def test_upgrade_refuses_invalid_download(self):
        with self.assertRaisesRegex(self.git_stack.StackError, "isn't a valid git-stack script"):
            self.upgrade("<html>Service Unavailable</html>\n")

        self.assertEqual(self.installed.read_text(), self.OLD)
        self.assertEqual(os.listdir(self.bin), ["git-stack"])

    def test_upgrade_refuses_script_tracked_by_git(self):
        subprocess.run(["git", "init", "--quiet", self.bin], check=True)
        subprocess.run(["git", "add", "git-stack"], cwd=self.bin, check=True)

        with self.assertRaisesRegex(self.git_stack.StackError, "tracked by git"):
            self.git_stack.ensure_upgradable(str(self.installed))

    def test_untracked_script_is_upgradable(self):
        self.git_stack.ensure_upgradable(str(self.installed))

    def test_resolve_commit_returns_sha(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.release.write_text(sha + "\n")

        self.assertEqual(self.git_stack.resolve_commit(self.release.as_uri(), "main"), sha)

    def test_resolve_commit_rejects_non_sha_response(self):
        self.release.write_text('{"message": "Not Found"}')

        with self.assertRaisesRegex(self.git_stack.StackError, "didn't return a commit"):
            self.git_stack.resolve_commit(self.release.as_uri(), "main")

    def test_resolve_commit_reports_missing_ref(self):
        for status in (404, 422):
            with self.subTest(status=status), self.assertRaisesRegex(
                    self.git_stack.StackError, "^couldn't find a branch, tag or commit named nope on GitHub$"):
                self.git_stack.resolve_commit(self.serve_status(status), "nope")

    def serve_status(self, status):
        """Start a local HTTP server that answers every request with status; return its URL."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_error(status)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/"


if __name__ == "__main__":
    unittest.main()
