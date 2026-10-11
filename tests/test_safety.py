import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken.match import play_match
from janken.safety import scan_source

from test_engine import write_bot


def violations_of(body):
    return scan_source(textwrap.dedent(body))


class ScanTest(unittest.TestCase):
    def assert_flagged(self, body, fragment):
        violations = violations_of(body)
        self.assertTrue(
            any(fragment in str(v) for v in violations),
            "expected a violation mentioning %r, got %r" % (fragment, violations),
        )

    def test_clean_bot_passes(self):
        self.assertEqual(violations_of("""
            import random
            from collections import Counter

            def play(my_moves, opp_moves):
                if not opp_moves:
                    return random.choice(('rock', 'paper', 'scissors'))
                favourite = Counter(opp_moves).most_common(1)[0][0]
                return {'rock': 'paper', 'paper': 'scissors',
                        'scissors': 'rock'}.get(favourite, 'rock')

            if __name__ == '__main__':
                print(play([], []))
            """), [])

    def test_disallowed_imports(self):
        for statement in (
            "import os",
            "import subprocess",
            "import socket",
            "from pathlib import Path",
            "import os.path",
            "from urllib import request",
            "import pickle",
            "import ctypes",
            "import importlib",
        ):
            self.assert_flagged(statement + "\ndef play(m, o):\n    return 'rock'",
                                "not allowed")

    def test_import_inside_function_is_caught(self):
        self.assert_flagged("""
            def play(m, o):
                import os
                return 'rock'
            """, "import of 'os'")

    def test_banned_builtins(self):
        self.assert_flagged("def play(m, o):\n    open('/etc/passwd')", "'open'")
        self.assert_flagged("def play(m, o):\n    return eval('1')", "'eval'")
        self.assert_flagged("def play(m, o):\n    exec('x = 1')", "'exec'")
        self.assert_flagged("def play(m, o):\n    __import__('os')", "'__import__'")
        self.assert_flagged("def play(m, o):\n    return input()", "'input'")

    def test_banned_even_without_a_call(self):
        # Aliasing the builtin must be caught too: f = open; f(path)
        self.assert_flagged("f = open\ndef play(m, o):\n    return 'rock'", "'open'")

    def test_introspection_escapes(self):
        self.assert_flagged(
            "def play(m, o):\n    return getattr(o, 'pop')()", "'getattr'")
        self.assert_flagged(
            "def play(m, o):\n    ().__class__.__bases__\n    return 'rock'",
            "dunder attribute",
        )
        self.assert_flagged(
            "def play(m, o):\n    play.__globals__\n    return 'rock'",
            "__globals__",
        )
        self.assert_flagged("x = __builtins__\ndef play(m, o):\n    return 'rock'",
                            "'__builtins__'")

    def test_operator_is_banned(self):
        # operator.attrgetter("__globals__")(fn) reaches __builtins__.__import__
        # while passing every attribute check, so the module is off the allowlist.
        self.assert_flagged("import operator\ndef play(m, o):\n    return 'rock'",
                            "not allowed")
        self.assert_flagged(
            "from operator import attrgetter\ndef play(m, o):\n    return 'rock'",
            "not allowed")

    def test_dunder_string_literal_escapes_blocked(self):
        # The confirmed escape: dunder smuggled as a string past the AST checks.
        self.assert_flagged("""
            import operator
            def play(m, o):
                return operator.attrgetter("__globals__")(play)
            """, "__globals__")
        # str.format attribute walk — dunder lives inside the format string.
        self.assert_flagged("""
            def play(m, o):
                return "{0.__class__.__mro__}".format(())
            """, "__class__")
        # A bare dunder string is enough on its own.
        self.assert_flagged("x = '__builtins__'\ndef play(m, o):\n    return 'rock'",
                            "__builtins__")

    def test_main_string_literal_still_allowed(self):
        # "__main__" is the one dunder string that must pass.
        self.assertEqual(violations_of("""
            def play(m, o):
                return 'rock'
            if __name__ == '__main__':
                print('debug')
            """), [])

    def test_help_is_banned(self):
        self.assert_flagged("def play(m, o):\n    help()\n    return 'rock'", "'help'")

    def test_main_guard_is_allowed(self):
        self.assertEqual(violations_of("""
            def play(m, o):
                return 'rock'
            if __name__ == '__main__':
                pass
            """), [])

    def test_syntax_error_reported(self):
        self.assert_flagged("def play(:\n", "syntax error")


class EnforcementTest(unittest.TestCase):
    def test_wrapper_refuses_unsafe_bot(self):
        with tempfile.TemporaryDirectory() as d:
            sneaky = write_bot(d, "sneaky", """
                import os
                def play(m, o):
                    return 'rock'
                """)
            rock = write_bot(d, "rock", "def play(m, o):\n    return 'rock'\n")
            result = play_match(sneaky, rock, rounds=5)
            self.assertIn("failed to start", result.forfeit_a)
            self.assertEqual(result.winner, "rock")

    def test_shipped_example_bots_pass_screening(self):
        bots_dir = os.path.join(os.path.dirname(__file__), "..", "bots")
        for filename in sorted(os.listdir(bots_dir)):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(bots_dir, filename)) as f:
                self.assertEqual(
                    scan_source(f.read()), [], "%s should be safe" % filename
                )


if __name__ == "__main__":
    unittest.main()
