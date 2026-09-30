"""Unit tests for dual-source codes (legacy exact-to + CF payload)."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from recovery_binder.codes import (
    Mailbox,
    create_alt_mailbox,
    create_legacy_mailbox,
    extract_code,
    pick_code_from_cf_payload,
    pick_code_from_payload,
    poll_verification_code,
    random_alt_email,
)
from recovery_binder.config import BindConfig, DomainEntry


class TestPickCodeExactTo(unittest.TestCase):
    def test_exact_to_match(self):
        payload = [
            {"to": "other@example.com", "code": "111111"},
            {"to": "target@example.com", "code": "222222"},
        ]
        self.assertEqual(
            pick_code_from_payload(payload, "target@example.com"),
            "222222",
        )

    def test_ignores_other_recipients(self):
        payload = [
            {"to": "a@example.org", "code": "333333"},
            {"to": "b@example.org", "code": "444444"},
        ]
        self.assertIsNone(pick_code_from_payload(payload, "missing@example.org"))

    def test_case_insensitive_to(self):
        payload = [{"to": "User@example.com", "code": "555555"}]
        self.assertEqual(
            pick_code_from_payload(payload, "user@example.com"),
            "555555",
        )

    def test_seen_codes_skipped(self):
        payload = [
            {"to": "x@example.com", "code": "666666"},
            {"to": "x@example.com", "code": "777777"},
        ]
        code = pick_code_from_payload(payload, "x@example.com", seen_codes={"777777"})
        self.assertEqual(code, "666666")

    def test_extract_code_from_nested(self):
        self.assertEqual(extract_code({"data": {"otp": "888888"}}), "888888")

    def test_random_alt_email_domain(self):
        cfg = BindConfig(
            domains=[
                DomainEntry("example.com", "http://a"),
                DomainEntry("example.org", "http://b"),
            ]
        )
        email = random_alt_email(config=cfg)
        self.assertTrue(email.endswith("@example.com") or email.endswith("@example.org"))


class TestPickCodeFromCfPayload(unittest.TestCase):
    def test_subject_code(self):
        payload = {
            "results": [
                {
                    "to": "user@mail3.example.org",
                    "subject": "Security code: 123456",
                    "text": "Your code is 123456",
                }
            ]
        }
        self.assertEqual(
            pick_code_from_cf_payload(payload, "user@mail3.example.org"),
            "123456",
        )

    def test_seen_codes_skipped(self):
        payload = [
            {"subject": "code 111111", "text": "111111"},
            {"subject": "code 222222", "text": "222222"},
        ]
        code = pick_code_from_cf_payload(payload, "a@x.com", seen_codes={"222222"})
        self.assertEqual(code, "111111")

    def test_html_blob(self):
        payload = [{"html": "<p>Your Microsoft code is 654321</p>"}]
        self.assertEqual(pick_code_from_cf_payload(payload, ""), "654321")

    def test_skips_unrelated_recipient_when_to_present(self):
        payload = [
            {
                "to": "other@mail3.example.org",
                "subject": "code 999999",
                "text": "999999",
            }
        ]
        self.assertIsNone(
            pick_code_from_cf_payload(payload, "want@mail3.example.org")
        )


class TestCreateMailboxMode(unittest.TestCase):
    def test_legacy_mailbox(self):
        cfg = BindConfig(mail_source_mode="legacy")
        mb = create_legacy_mailbox(config=cfg)
        self.assertEqual(mb.source, "legacy")
        self.assertIn("@", mb.email)
        self.assertIsNone(mb.jwt)
        self.assertTrue(mb.domain in cfg.alt_domains)

    def test_force_legacy_via_alt(self):
        cfg = BindConfig(mail_source_mode="cf", cf_enabled=True, legacy_enabled=True)
        mb = create_alt_mailbox(cfg, force_source="legacy", allow_fallback=False)
        self.assertEqual(mb.source, "legacy")

    def test_cf_only_mode_raises_without_network_mock(self):
        cfg = BindConfig(mail_source_mode="cf", cf_enabled=True, legacy_enabled=False)

        def boom(*_a, **_k):
            raise RuntimeError("network down")

        with patch("recovery_binder.codes.create_cf_mailbox", side_effect=boom):
            with self.assertRaises(RuntimeError):
                create_alt_mailbox(cfg, allow_fallback=True)

    def test_random_fallback_to_legacy_on_cf_fail(self):
        cfg = BindConfig(
            mail_source_mode="random",
            cf_enabled=True,
            legacy_enabled=True,
        )
        calls = {"n": 0}

        def fake_pick(config, force_source=None):
            return "cf" if force_source is None else force_source

        def fake_cf(_config):
            calls["n"] += 1
            raise RuntimeError("cf fail")

        with patch("recovery_binder.codes._pick_mail_source_name", side_effect=fake_pick):
            with patch("recovery_binder.codes.create_cf_mailbox", side_effect=fake_cf):
                mb = create_alt_mailbox(cfg, allow_fallback=True)
        self.assertEqual(mb.source, "legacy")
        self.assertEqual(calls["n"], 1)

    def test_create_cf_mailbox_parses_json(self):
        cfg = BindConfig(mail_source_mode="cf")
        fake = {
            "text": json.dumps({"address": "ab@mail3.example.org", "jwt": "tok123"}),
            "json": {"address": "ab@mail3.example.org", "jwt": "tok123"},
            "url": "https://email.example.org/api/new_address",
        }
        with patch("recovery_binder.codes._http_json", return_value=fake):
            mb = create_alt_mailbox(cfg, force_source="cf", allow_fallback=False)
        self.assertEqual(mb.source, "cf")
        self.assertEqual(mb.email, "ab@mail3.example.org")
        self.assertEqual(mb.jwt, "tok123")
        self.assertIn("mail3.example.org", cfg.alt_domains)


class TestPollWithMailbox(unittest.TestCase):
    def test_legacy_poll_uses_to_match(self):
        cfg = BindConfig()
        payload = [{"to": "t@example.com", "code": "112233"}]

        def fetch(_url, _proxy=None):
            return {"json": payload, "text": json.dumps(payload)}

        code = poll_verification_code(
            "t@example.com",
            config=cfg,
            timeout_ms=2000,
            interval_ms=100,
            fetch_once=fetch,
            mailbox=Mailbox(
                email="t@example.com",
                source="legacy",
                domain="example.com",
                code_api="http://x",
            ),
        )
        self.assertEqual(code, "112233")

    def test_cf_poll_uses_jwt_path(self):
        cfg = BindConfig()
        mb = Mailbox(
            email="c@mail3.example.org",
            source="cf",
            domain="mail3.example.org",
            jwt="j",
        )
        payload = {
            "results": [
                {
                    "to": "c@mail3.example.org",
                    "subject": "Your code 445566",
                    "text": "445566",
                }
            ]
        }

        def fetch_cf(_mb, _cfg):
            return {"json": payload, "text": json.dumps(payload)}

        code = poll_verification_code(
            mb.email,
            config=cfg,
            timeout_ms=2000,
            interval_ms=100,
            mailbox=mb,
            fetch_cf_once=fetch_cf,
        )
        self.assertEqual(code, "445566")


if __name__ == "__main__":
    unittest.main()
