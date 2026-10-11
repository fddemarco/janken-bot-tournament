"""Containment tests for the bubblewrap sandbox in janken.match.

The network/mask/read-only cases run code the static scanner would reject, so
they go straight through the sandbox prefix rather than through play_match, and
skip where bwrap can't sandbox (macOS, hardened kernels, CI without userns).
The policy test needs no bwrap and runs everywhere.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken import match


@unittest.skipUnless(match._sandbox_available(), "bwrap sandbox unavailable here")
class SandboxContainmentTest(unittest.TestCase):
    def _run(self, code):
        prefix = match._bwrap_prefix(match.WRAPPER)
        return subprocess.run(
            prefix + [match.INTERPRETER, "-c", code],
            capture_output=True, text=True, timeout=15,
        )

    def test_network_is_blocked(self):
        # No net namespace at all -> even the AWS metadata IP is unreachable.
        r = self._run("import socket; socket.create_connection(('169.254.169.254', 80), 3)")
        self.assertNotEqual(r.returncode, 0, "sandbox should have no network")

    def test_home_files_are_masked(self):
        # A real host file under $HOME that the sandbox does not re-expose must
        # be invisible inside it (this is where SSH keys / cloud creds live).
        readme = os.path.realpath(os.path.join(os.path.dirname(match.WRAPPER), "..", "README.md"))
        home = os.path.realpath(os.path.expanduser("~"))
        if not (os.path.exists(readme) and readme.startswith(home + os.sep)):
            self.skipTest("project README not under $HOME on this host")
        r = self._run("import os, sys; sys.exit(1 if os.path.exists(%r) else 0)" % readme)
        self.assertEqual(r.returncode, 0, "%s should be masked inside the sandbox" % readme)

    def test_filesystem_is_read_only(self):
        # The interpreter and this package are re-exposed read-only: a write to
        # one of them must fail (the real files can't be tampered with).
        r = self._run("open(%r, 'a')" % match.WRAPPER)
        self.assertNotEqual(r.returncode, 0, "re-exposed paths must be read-only")


class ReexposePathsTest(unittest.TestCase):
    """What gets re-bound over the masked home. Needs no bwrap."""

    def _covered(self, path, paths):
        return any(path == p or path.startswith(p + os.sep) for p in paths)

    def test_the_exec_target_is_exposed(self):
        # This is the one that matters: bwrap dies with "execvp ...: No such file
        # or directory" — reported as "sandbox unavailable", which stops every bot
        # on the host — unless the exact path BotProcess execs is bound.
        paths = match._reexpose_paths(match.WRAPPER)
        self.assertTrue(self._covered(match.INTERPRETER, paths),
                        "%s is not covered by %r" % (match.INTERPRETER, paths))

    def test_interpreter_is_resolved_so_no_symlink_hop_can_dangle(self):
        # A venv's bin/python3 traverses hops that may live under the masked
        # $HOME (uv's versionless "cpython-3.14" alias directory, say). Exec'ing
        # the resolved path sidesteps every one of them.
        self.assertEqual(match.INTERPRETER, os.path.realpath(match.INTERPRETER))
        self.assertTrue(os.path.exists(match.INTERPRETER))

    def test_both_forms_of_each_path_are_considered(self):
        paths = match._reexpose_paths(match.WRAPPER)
        for path in (os.path.abspath(sys.executable), os.path.realpath(sys.executable),
                     os.path.abspath(sys.prefix), os.path.realpath(sys.prefix)):
            self.assertTrue(self._covered(path, paths),
                            "%s is not covered by %r" % (path, paths))

    def test_no_path_nests_inside_another(self):
        # Binding inside an already-bound (read-only) path makes bwrap fail with
        # "Can't create file at ...", so the list has to stay top-most only.
        paths = match._reexpose_paths(match.WRAPPER)
        for path in paths:
            for other in paths:
                if path != other:
                    self.assertFalse(
                        path.startswith(other + os.sep),
                        "%s nests inside %s" % (path, other))

    def test_sources_all_exist(self):
        # A missing bind source is a hard bwrap error.
        for path in match._reexpose_paths(match.WRAPPER):
            self.assertTrue(os.path.exists(path), path)


class SandboxPolicyTest(unittest.TestCase):
    def test_require_sandbox_raises_when_unavailable(self):
        saved = dict(os.environ)
        match._sandbox_available.cache_clear()
        try:
            os.environ["JANKEN_NO_SANDBOX"] = "1"      # force it unavailable
            os.environ["JANKEN_REQUIRE_SANDBOX"] = "1"  # but demand it
            with self.assertRaises(RuntimeError):
                match._enforce_sandbox_policy()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            match._sandbox_available.cache_clear()

    def test_require_sandbox_zero_disables(self):
        # "0" must read as off — the string "0" is otherwise truthy in Python.
        saved = dict(os.environ)
        match._sandbox_available.cache_clear()
        try:
            os.environ["JANKEN_NO_SANDBOX"] = "1"       # unavailable
            os.environ["JANKEN_REQUIRE_SANDBOX"] = "0"   # explicitly not required
            match._enforce_sandbox_policy()  # must NOT raise
        finally:
            os.environ.clear()
            os.environ.update(saved)
            match._sandbox_available.cache_clear()


if __name__ == "__main__":
    unittest.main()
