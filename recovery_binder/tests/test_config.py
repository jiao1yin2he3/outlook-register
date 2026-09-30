"""Unit tests for BindConfig target_count and dual mail-source mapping."""
from __future__ import annotations

import unittest

from recovery_binder.config import MAX_TARGET_COUNT, BindConfig, DEFAULT_CF_API_BASE


class TestBindTargetCount(unittest.TestCase):
    def test_from_host_one(self):
        cfg = BindConfig.from_host({"bind_target_count": 1})
        self.assertEqual(cfg.target_count, 1)

    def test_from_host_two(self):
        cfg = BindConfig.from_host({"bind_target_count": 2})
        self.assertEqual(cfg.target_count, 2)

    def test_default_two(self):
        self.assertEqual(BindConfig().target_count, 2)
        self.assertEqual(BindConfig.from_host({}).target_count, 2)

    def test_clamp_zero_and_negative(self):
        self.assertEqual(BindConfig.from_host({"bind_target_count": 0}).target_count, 1)
        self.assertEqual(BindConfig.from_host({"bind_target_count": -3}).target_count, 1)

    def test_invalid_falls_back(self):
        self.assertEqual(
            BindConfig.from_host({"bind_target_count": "nope"}).target_count, 2
        )
        self.assertEqual(
            BindConfig.from_host({"bind_target_count": None}).target_count, 2
        )

    def test_cap_extreme(self):
        cfg = BindConfig.from_host({"bind_target_count": 99})
        self.assertEqual(cfg.target_count, MAX_TARGET_COUNT)

    def test_loop_bounds_use_target(self):
        for n in (1, 2):
            steps = list(
                range(
                    1,
                    BindConfig.from_host({"bind_target_count": n}).target_count + 1,
                )
            )
            self.assertEqual(len(steps), n)
            self.assertEqual(steps[-1], n)


class TestMailSourceConfig(unittest.TestCase):
    def test_default_mode_random(self):
        cfg = BindConfig.from_host({})
        self.assertEqual(cfg.mail_source_mode, "random")
        self.assertTrue(cfg.cf_enabled)
        self.assertTrue(cfg.legacy_enabled)
        self.assertTrue(cfg.cf_http_direct)
        self.assertEqual(cfg.cf_api_base, DEFAULT_CF_API_BASE.rstrip("/"))

    def test_flat_bind_mail_source_cf(self):
        cfg = BindConfig.from_host({"bind_mail_source": "cf", "bind_cf_enabled": True})
        self.assertEqual(cfg.mail_source_mode, "cf")
        self.assertIn("cf", cfg.enabled_mail_sources())

    def test_nested_mode_and_cf_domains(self):
        cfg = BindConfig.from_host(
            {
                "bind_mail_sources": {
                    "mode": "legacy",
                    "sources": {
                        "legacy": {"enabled": True},
                        "cf": {
                            "enabled": True,
                            "apiBase": "https://email.example.org",
                            "domains": ["mail3.example.org", "mail4.example.org"],
                            "httpDirect": True,
                        },
                    },
                }
            }
        )
        self.assertEqual(cfg.mail_source_mode, "legacy")
        self.assertEqual(cfg.cf_domains[:2], ["mail3.example.org", "mail4.example.org"])
        self.assertTrue(cfg.cf_http_direct)

    def test_alt_domains_merges_legacy_and_cf(self):
        cfg = BindConfig.from_host({})
        alts = cfg.alt_domains
        self.assertIn("example.com", alts)
        self.assertIn("example.org", alts)
        self.assertIn("mail3.example.org", alts)

    def test_register_extra_domain(self):
        cfg = BindConfig()
        cfg.register_extra_domain("runtime.example")
        self.assertIn("runtime.example", cfg.alt_domains)

    def test_invalid_mode_falls_to_random(self):
        cfg = BindConfig.from_host({"bind_mail_source": "nope"})
        self.assertEqual(cfg.mail_source_mode, "random")

    def test_force_cf_only_flags(self):
        cfg = BindConfig.from_host(
            {
                "bind_mail_source": "cf",
                "bind_cf_enabled": True,
                "bind_legacy_enabled": False,
            }
        )
        self.assertEqual(cfg.mail_source_mode, "cf")
        self.assertEqual(cfg.enabled_mail_sources(), ["cf"])


if __name__ == "__main__":
    unittest.main()
