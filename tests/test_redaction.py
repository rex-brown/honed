"""The secrets and personal-data scan (`core.redaction`): every kind it is meant to catch, and the look-alikes it
must leave alone (documentation ranges and domains, noreply addresses, private addresses, placeholders, code)."""

from __future__ import annotations

import pytest

from honed.core.redaction import KINDS, RULES_VERSION, redact, redact_value, sha256

# Synthetic values in the shape of real ones; none of them is a live credential. Each is split in the source so secret
# scanners (GitHub push protection among them) don't take this file for a leak.
AWS_KEY = "AKIA" + "Q2J4K5L6M7N8P9R3"
POSITIVES = [
    ("aws_access_key", f"the key {AWS_KEY} leaked"),
    ("aws_secret_key", "aws_secret_access_key = '" + "Qz9pL2xW8vN4mK7j" + "H3gF6dS1aZ5cX0bV2nM8qW4e'"),
    ("github_token", "token ghp_" + "a1B2c3D4e5" * 4),
    ("github_token", "gho_" + "Z9y8X7w6V5" * 4),
    ("github_token", "github_pat_11ABCDEFG0" + "x1Y2z3" * 5),
    ("slack_token", "xoxb-" + "2048-4096-abcdefABCDEF1234"),
    ("slack_token", "https://hooks.slack.com/services/" + "T0ABC123/B0DEF456/aBcDeFgHiJkLmNoP"),
    ("stripe_key", "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"),
    ("stripe_key", "rk_test_" + "51H8sKlm2Q3r4S5t6U7v8W9x"),
    ("google_key", "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"[:35]),
    ("anthropic_key", "export ANTHROPIC_API_KEY=sk-ant-api03-" + "Ab1Cd2Ef3G" * 3),
    ("openai_key", "sk-proj-" + "Ab1Cd2Ef3G" * 4),
    ("private_key", "-----BEGIN RSA PRIVATE " + "KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"),
    ("private_key", "-----BEGIN OPENSSH PRIVATE " + "KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA (truncated)"),
    ("jwt", "Bearer eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFU"),
    ("secret_assignment", 'api_key = "a1b2c3d4e5f6g7h8i9"'),
    ("secret_assignment", "CLIENT_SECRET=Zx81kQp02mWn55vB"),
    ("secret_assignment", '{"password": "Tr0ub4dor-and-3"}'),
    ("email", "ping jane.doe@acme-corp.io about it"),
    ("phone", "call me on +1 415 555 0132"),
    ("phone", "or (415) 555-0132"),
    ("phone", "or 415-555-0132"),
    ("phone", "+44 20 7946 0958"),
    ("ipv4", "the server at 52.14.200.7 refused"),
]


@pytest.mark.parametrize(("kind", "text"), POSITIVES)
def test_each_kind_is_found_and_replaced_by_its_mark(kind, text):
    done = redact(text)
    assert [h.kind for h in done.hits] == [kind]
    assert f"[redacted:{kind}]" in done.text
    hit = done.hits[0]
    secret = text[hit.start : hit.end]
    assert secret and secret not in done.text  # the value is gone; only its kind and place remain


NEGATIVES = [
    "docs use 192.0.2.10, 198.51.100.7 and 203.0.113.99",  # RFC 5737 documentation ranges
    "bind 127.0.0.1, 0.0.0.0, 10.1.2.3, 172.16.0.1, 192.168.1.1, 169.254.0.1, 100.64.0.1",  # not globally routable
    "Version=4.0.1.2 and v8.1.3.7 and 1.2.3.4.5 and 52.14.200.0",  # versions and a network address
    "resolvers 8.8.8.8 and 1.1.1.1",
    "Co-Authored-By: Bot <12345+bot@users.noreply.github.com>",
    "from noreply@github.com and no-reply@accounts.google.com",
    "clone git@github.com:o/r.git",
    "write to user@example.com or admin@mail.example.org or a@b.test",
    "AWS's sample key AKIAIOSFODNN7EXAMPLE",
    'api_key = os.environ["API_KEY"]',
    "secret_key: str = Field(...)",
    "password = get_password(1234567890123)",
    'token = "your-api-key-here-123"',
    "API_KEY=${{ secrets.API_KEY }}",
    "created 2026-10-01, lines 100-200, PR #12345678, commit 3f2a9c1",
    "pkg@1.2.3 and @types/node@20.1.0",
]


@pytest.mark.parametrize("text", NEGATIVES)
def test_look_alikes_are_left_alone(text):
    done = redact(text)
    assert done.hits == () and done.text == text


def test_a_private_key_block_is_one_hit_even_with_secrets_inside():
    text = "-----BEGIN PRIVATE " + "KEY-----\napi_key=a1b2c3d4e5f6g7h8i9 jane@acme.io\n-----END PRIVATE KEY-----\nafter"
    done = redact(text)
    assert [h.kind for h in done.hits] == ["private_key"] and done.text == "[redacted:private_key]\nafter"


def test_several_hits_keep_their_offsets_in_the_original_text():
    text = "mail jane@acme.io, then call 415-555-0132"
    done = redact(text)
    assert [(h.kind, text[h.start : h.end]) for h in done.hits] == [("email", "jane@acme.io"),
                                                                    ("phone", "415-555-0132")]  # fmt: skip
    assert done.text == "mail [redacted:email], then call [redacted:phone]"


def test_structured_answers_are_redacted_leaf_by_leaf_with_their_paths():
    value = {"issues": [{"description": f"key {AWS_KEY} in config", "line": 3}], "ok": True}
    out, hits = redact_value(value)
    assert out == {"issues": [{"description": "key [redacted:aws_access_key] in config", "line": 3}], "ok": True}
    assert [(path, h.kind) for path, h in hits] == [("issues[0].description", "aws_access_key")]
    assert redact_value(None) == (None, [])


def test_the_rule_set_is_versioned_and_the_hash_is_of_utf8_text():
    assert len(RULES_VERSION) == 12 and len(KINDS) == len(set(KINDS))
    assert sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
