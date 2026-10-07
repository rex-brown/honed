"""The secrets and personal-data scan a bundle export runs over quoted text (ARCHITECTURE.md section 8, DATASET.md).
Pure functions.

`redact(text)` finds API keys and tokens, private-key blocks, JSON Web Tokens, generic `api_key = ...` style
assignments, email addresses, phone numbers and public IPv4 addresses, and replaces each hit with
`[redacted:<kind>]`. A hit's location (its kind and offset) can be reported; its value never leaves this module.

What is not a hit: noreply email addresses (`...@users.noreply.github.com`, `noreply@...`, `no-reply@...`), `git@`
remotes and addresses at the reserved example domains (RFC 2606); IPv4 addresses in the documentation ranges
(RFC 5737) or not globally routable (private, loopback, link-local, shared, reserved); AWS's own documentation keys
(`...EXAMPLE`); and assignments whose value doesn't look like a secret (no digit, a placeholder, a short word).

Rules are tried in order and a span claimed by an earlier rule is not matched again, so a private-key block is
one hit, not a key block plus whatever it contains. `RULES_VERSION` hashes the rule set: a bundle records it, and a
text hash made under other rules may not verify.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

MARK = "[redacted:{kind}]"


@dataclass(frozen=True)
class Hit:
    kind: str
    start: int  # offset in the original text
    end: int


@dataclass(frozen=True)
class Redacted:
    text: str  # the text with every hit replaced by its mark
    hits: tuple[Hit, ...] = ()


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    group: str = "value"  # the named group replaced; the whole match when the pattern has no such group
    keep: Callable[[re.Match[str]], bool] = lambda m: True  # False: not a hit after all


# ---- validators ---------------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"(?i)example|your|changeme|placeholder|dummy|redacted|sample|xxxx|\*\*\*|<|>|\.\.\.")


def _not_example(match: re.Match[str]) -> bool:
    return "EXAMPLE" not in match.group(0).upper()


def _secret_like(match: re.Match[str]) -> bool:
    """An assignment's value looks like a secret: letters and digits, not a placeholder, not one repeated char."""
    value = match.group("value")
    mixed = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return mixed and len(set(value)) > 4 and not _PLACEHOLDER.search(value)


_NOREPLY = re.compile(r"(?i)(^|[.+_-])no-?reply([.+_-]|$)")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost")


def _personal_email(match: re.Match[str]) -> bool:
    local, _, domain = match.group(0).lower().rpartition("@")
    if (
        local == "git"
        or not any(c not in "+- " for c in local)
        or _NOREPLY.search(local)
        or domain.endswith("noreply.github.com")
        or "noreply" in domain
    ):
        return False
    reserved = domain in _RESERVED_DOMAINS or domain.endswith(tuple(f".{d}" for d in _RESERVED_DOMAINS))
    return not reserved and not domain.endswith(_RESERVED_TLDS)


_DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))
_PUBLIC_RESOLVERS = frozenset({"8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9", "149.112.112.112"})
_VERSION_BEFORE = re.compile(r"(?i)(?:version|ver)\s*[=:]?\s*[\"']?$")


def _public_ipv4(match: re.Match[str]) -> bool:
    """A globally routable address that could point at someone: not a documentation, private, loopback, link-local or
    reserved one, not a well-known public resolver, not a network address (`.0`), and not a version number
    (`Version=4.0.1.2`)."""
    text = match.group(0)
    try:
        address = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        return False
    if not address.is_global or any(address in net for net in _DOCUMENTATION_NETS):
        return False
    before = match.string[max(0, match.start() - 12) : match.start()]
    return text not in _PUBLIC_RESOLVERS and not text.endswith(".0") and not _VERSION_BEFORE.search(before)


def _phone_digits(match: re.Match[str]) -> bool:
    return 9 <= sum(c.isdigit() for c in match.group(0)) <= 15


# ---- rules --------------------------------------------------------------------------------------------------------

_KEYWORD = (
    r"(?:api[_-]?key|apikey|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?token|auth[_-]?token"
    r"|private[_-]?token|password|passwd)"
)
_PRIVATE_KEY = (
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----|\Z)"
)
_AWS_SECRET = (
    r"(?i)aws_?secret_?(?:access_?)?key[\"']?\s*(?:=|:|=>)\s*[\"']?"
    r"(?P<value>[A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])"
)
_GOOGLE = r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])|\bGOCSPX-[A-Za-z0-9_-]{20,}|\bya29\.[0-9A-Za-z_-]{20,}"
_SLACK = r"\bxox[abposr]-[A-Za-z0-9-]{10,}|https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"
_ASSIGNMENT = (
    rf"(?i)\b[\w-]*{_KEYWORD}[\w-]*[\"']?\s*(?:=|:=|:|=>)\s*(?P<q>[\"'`]?)"
    r"(?P<value>[A-Za-z0-9_\-+/=]{12,})(?P=q)(?![A-Za-z0-9_\-+/=(.\[])"
)
_EMAIL = r"(?<![\w.+-])[+-]*[A-Za-z0-9._%][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
_PHONE = (
    r"(?<![\w+.-])(?:\+\d{1,3}[ .-]?(?:\(\d{1,4}\)[ .-]?)?\d{1,4}(?:[ .-]\d{2,4}){1,4}"
    r"|\(\d{3}\) ?\d{3}[ .-]\d{4}|\d{3}([.-])\d{3}\1\d{4})(?![\w.-]?\d)"
)
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IPV4 = rf"(?<![\w.])(?:{_OCTET}\.){{3}}{_OCTET}(?![\w.]?\d)"

RULES: tuple[_Rule, ...] = (
    _Rule("private_key", re.compile(_PRIVATE_KEY, re.S)),
    _Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"), keep=_not_example),
    _Rule("aws_secret_key", re.compile(_AWS_SECRET), keep=_not_example),
    _Rule("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")),
    _Rule("slack_token", re.compile(_SLACK)),
    _Rule("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    _Rule("google_key", re.compile(_GOOGLE)),
    _Rule("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    _Rule("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    _Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    _Rule("secret_assignment", re.compile(_ASSIGNMENT), keep=_secret_like),
    _Rule("email", re.compile(_EMAIL), keep=_personal_email),
    _Rule("phone", re.compile(_PHONE), keep=_phone_digits),
    _Rule("ipv4", re.compile(_IPV4), keep=_public_ipv4),
)

KINDS = tuple(rule.kind for rule in RULES)
RULES_VERSION = hashlib.sha256("\n".join(f"{r.kind}\t{r.pattern.pattern}" for r in RULES).encode()).hexdigest()[:12]


# ---- redaction ----------------------------------------------------------------------------------------------------


def _spans(text: str) -> Iterator[Hit]:
    taken: list[tuple[int, int]] = []
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            if not rule.keep(match):
                continue
            group = rule.group if rule.group in match.re.groupindex else 0
            start, end = match.span(group)
            if start == end or any(start < b and a < end for a, b in taken):
                continue
            taken.append((start, end))
            yield Hit(rule.kind, start, end)


def redact(text: str) -> Redacted:
    """`text` with every hit replaced by `[redacted:<kind>]`, and the hits in order of position."""
    if not text:
        return Redacted(text)
    hits = tuple(sorted(_spans(text), key=lambda h: h.start))
    if not hits:
        return Redacted(text)
    out, at = [], 0
    for hit in hits:
        out += [text[at : hit.start], MARK.format(kind=hit.kind)]
        at = hit.end
    out.append(text[at:])
    return Redacted("".join(out), hits)


def redact_value(value: Any, path: str = "") -> tuple[Any, list[tuple[str, Hit]]]:
    """A JSON-like value with every string in it redacted, and each hit with the path of its string
    (`issues[0].description`)."""
    if isinstance(value, str):
        done = redact(value)
        return done.text, [(path, h) for h in done.hits]
    if isinstance(value, Mapping):
        out, hits = {}, []
        for k, v in value.items():
            out[k], found = redact_value(v, f"{path}.{k}" if path else str(k))
            hits += found
        return out, hits
    if isinstance(value, (list, tuple)):
        items, hits = [], []
        for n, v in enumerate(value):
            item, found = redact_value(v, f"{path}[{n}]")
            items.append(item)
            hits += found
        return items, hits
    return value, []


def sha256(text: str) -> str:
    """The hex SHA-256 of `text` as UTF-8: a bundle's `text_sha256`."""
    return hashlib.sha256(text.encode()).hexdigest()
