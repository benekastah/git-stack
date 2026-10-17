"""End-to-end tests for git-stack, run against throwaway repositories."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "git-stack"


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
                                capture_output=True, text=True)
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

    def squash_merge_on_forge(self, branch):
        """Squash-merge origin/<branch> into origin/main and delete the branch."""
        self.git("pull", "--quiet", cwd=self.forge)
        self.git("merge", "--quiet", "--squash", f"origin/{branch}", cwd=self.forge)
        self.git("commit", "--quiet", "-m", f"{branch} (squashed)", cwd=self.forge)
        self.git("push", "--quiet", "origin", "main", f":{branch}", cwd=self.forge)

    def config(self, branch, key):
        return self.git("config", f"branch.{branch}.{key}")

    def own_commits(self, branch, parent):
        return self.git("log", "--format=%s", f"{parent}..{branch}").splitlines()

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


class SyncTest(StackTestCase):
    def test_sync_after_parent_squash_merged(self):
        self.make_stack()
        self.stack("push")
        self.squash_merge_on_forge("pr1")
        self.git("switch", "--quiet", "pr1")

        self.stack("sync")

        self.assertEqual(self.git("branch", "--list", "pr1"), "")
        self.assertEqual(self.git("rev-parse", "main"), self.git("rev-parse", "origin/main"))
        self.assert_stacked("pr2", "main", ["pr2 c1"])
        self.assert_stacked("pr3", "pr2", ["pr3 c1"])
        # We were on pr1, which no longer exists, so we land on its parent.
        self.assertEqual(self.git("branch", "--show-current"), "main")

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

        self.assertEqual(self.git("branch", "--list", "pr1", "pr2"), "")
        self.assert_stacked("pr3", "main", ["pr3 c1"])
        self.assertEqual(self.git("branch", "--show-current"), "pr3")

    def test_sync_keeps_branch_deleted_without_merging(self):
        self.make_stack()
        self.stack("push")
        self.git("push", "--quiet", "origin", ":pr1", cwd=self.forge)

        result = self.stack("sync")

        self.assertIn("pr1's remote branch is gone but its changes aren't in main", result.stderr)
        self.assert_stacked("pr2", "pr1", ["pr2 c1"])

    def test_push_pushes_every_branch_in_stack(self):
        self.make_stack()
        self.git("switch", "--quiet", "pr2")

        self.stack("push")

        for branch in ("pr1", "pr2", "pr3"):
            self.assertEqual(self.git("rev-parse", f"origin/{branch}"), self.git("rev-parse", branch))
            self.assertEqual(self.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"),
                             f"origin/{branch}")


if __name__ == "__main__":
    unittest.main()
