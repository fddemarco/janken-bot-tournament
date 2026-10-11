import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from janken.auth import (
    DEFAULT_DOMAINS,
    DOMAINS_ENV,
    allowed_domains,
    email_domain,
    parse_domains,
    rejection_reason,
)

MUTT = ("muttdata.ai",)


class ParseDomainsTest(unittest.TestCase):
    def test_commas_whitespace_and_at_signs(self):
        self.assertEqual(
            parse_domains(" @muttdata.ai, example.com  other.co "),
            ("muttdata.ai", "example.com", "other.co"),
        )

    def test_lowercases_and_dedupes(self):
        self.assertEqual(parse_domains("MuttData.ai muttdata.AI"), MUTT)

    def test_empty_falls_back_to_default(self):
        for raw in ("", "   ", None):
            self.assertEqual(parse_domains(raw), DEFAULT_DOMAINS)

    def test_junk_without_a_dot_falls_back_rather_than_opening_up(self):
        # A bare "muttdata" would otherwise match nothing and lock everyone out,
        # or worse, be read as "no restriction".
        self.assertEqual(parse_domains("muttdata"), DEFAULT_DOMAINS)

    def test_explicit_default_is_honoured(self):
        self.assertEqual(parse_domains(None, default=["x.dev"]), ("x.dev",))


class AllowedDomainsTest(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get(DOMAINS_ENV)

    def tearDown(self):
        if self.saved is None:
            os.environ.pop(DOMAINS_ENV, None)
        else:
            os.environ[DOMAINS_ENV] = self.saved

    def test_defaults_to_muttdata(self):
        os.environ.pop(DOMAINS_ENV, None)
        self.assertEqual(allowed_domains(), MUTT)

    def test_env_override(self):
        os.environ[DOMAINS_ENV] = "muttdata.ai,contractor.dev"
        self.assertEqual(allowed_domains(), ("muttdata.ai", "contractor.dev"))


class EmailDomainTest(unittest.TestCase):
    def test_extracts_and_lowercases(self):
        self.assertEqual(email_domain("  Yisus@MuttData.ai "), "muttdata.ai")

    def test_rejects_non_addresses(self):
        for bad in ("", "yisus", "@muttdata.ai", "a@b@muttdata.ai",
                    "yisus@localhost", None, 42, ["yisus@muttdata.ai"]):
            self.assertIsNone(email_domain(bad), bad)


class RejectionReasonTest(unittest.TestCase):
    def allow(self, email, verified=True):
        return rejection_reason(email, verified=verified, domains=MUTT)

    def test_verified_muttdata_address_is_allowed(self):
        self.assertIsNone(self.allow("ivan.terzano@muttdata.ai"))

    def test_case_and_padding_are_ignored(self):
        self.assertIsNone(self.allow(" Ivan.Terzano@MuttData.ai "))

    def test_other_domain_is_rejected(self):
        reason = self.allow("someone@gmail.com")
        self.assertIn("only @muttdata.ai accounts", reason)
        self.assertIn("someone@gmail.com", reason)

    def test_lookalike_domains_are_rejected(self):
        for bad in ("a@notmuttdata.ai", "a@muttdata.ai.evil.com",
                    "a@sub.muttdata.ai", "a@muttdata.io"):
            self.assertIsNotNone(self.allow(bad), bad)

    def test_unverified_address_is_rejected(self):
        # Fails closed: a missing email_verified claim is not a pass.
        for verified in (False, None, "true", 1):
            self.assertIn("isn't a verified address",
                          self.allow("ivan.terzano@muttdata.ai", verified=verified))

    def test_missing_email_is_rejected(self):
        for missing in (None, "", "   ", "not-an-email"):
            self.assertIn("no usable email address", self.allow(missing))

    def test_multiple_allowed_domains(self):
        domains = ("muttdata.ai", "contractor.dev")
        self.assertIsNone(rejection_reason("a@contractor.dev", verified=True,
                                           domains=domains))
        reason = rejection_reason("a@gmail.com", verified=True, domains=domains)
        self.assertIn("@muttdata.ai or @contractor.dev", reason)

    def test_falls_back_to_env_allowlist_when_domains_omitted(self):
        saved = os.environ.pop(DOMAINS_ENV, None)
        try:
            self.assertIsNone(rejection_reason("a@muttdata.ai", verified=True))
            self.assertIsNotNone(rejection_reason("a@gmail.com", verified=True))
        finally:
            if saved is not None:
                os.environ[DOMAINS_ENV] = saved


if __name__ == "__main__":
    unittest.main()
