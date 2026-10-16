"""``aq evidence verify``: check a signed evidence pack from AQ, offline.

    aq evidence verify evidence-pack.json --key-fingerprint 3f1c... --tsa-cert tsa-chain.pem

The checks are in ``evidence_verify.py``, the same file AQ runs after it
signs a pack. Nothing is sent anywhere. Pass the key you expect, as a PEM
public key (``--public-key``) or its SHA-256 fingerprint
(``--key-fingerprint``, obtained through a trusted channel). A missing expected
signer fails verification. Pass your time-stamp authority's certificates
(``--tsa-cert``) to check who stamped it.

Exit codes: 0 every check passed, 1 a check failed, 2 the command or a
file is wrong. Needs the ``cryptography`` package
(``pip install 'aq-sdk[evidence]'``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TextIO

OK, FAILED, USAGE = 0, 1, 2


def add_parser(sub: Any) -> None:
    ev = sub.add_parser("evidence", help="Work with signed evidence packs.").add_subparsers(dest="command", required=True)
    v = ev.add_parser("verify", help="Check a signed evidence pack offline.",
                      description="Checks the pack's hash, its signature and its RFC 3161 time-stamp. Sends nothing.")
    v.add_argument("file", help="The evidence pack JSON from AQ.")
    key = v.add_mutually_exclusive_group()
    key.add_argument("--public-key", help="PEM file of the public key the pack must be signed with.")
    key.add_argument("--key-fingerprint", help="SHA-256 of the expected public key (DER SubjectPublicKeyInfo), in hex.")
    v.add_argument("--tsa-cert", help="PEM file with the time-stamp authority's certificates you trust.")
    v.add_argument("--allow-no-timestamp", action="store_true", help="Pass a pack that has no time-stamp.")
    v.add_argument("--json", action="store_true", help="Print one JSON result instead of text.")


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def _read(path: str, what: str, err: TextIO) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        print(f"Error: the {what} {path} could not be read: {exc.strerror}.", file=err)
        return None


def run(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        from . import evidence_verify
        import cryptography  # noqa: F401
    except ImportError:
        print("Error: aq evidence verify needs the cryptography package. Run: pip install 'aq-sdk[evidence]'", file=err)
        return USAGE
    raw = _read(args.file, "pack", err)
    if raw is None:
        return USAGE
    try:
        envelope = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKey as exc:
        # A repeated key could show one value to this check and another to a reader that keeps the first.
        print(f"Error: {args.file} repeats the key {exc}, so it is not a pack AQ wrote.", file=err)
        return USAGE
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        print(f"Error: {args.file} is not JSON that a pack can be read from.", file=err)
        return USAGE
    public_key = tsa = None
    if args.public_key:
        data = _read(args.public_key, "public key", err)
        if data is None:
            return USAGE
        public_key = data.decode("ascii", errors="replace")
    if args.tsa_cert:
        tsa = _read(args.tsa_cert, "TSA certificate file", err)
        if tsa is None:
            return USAGE
    try:
        report = evidence_verify.verify_envelope(envelope, expected_public_key_pem=public_key,
                                                 expected_fingerprint=args.key_fingerprint,
                                                 tsa_certificates_pem=tsa,
                                                 require_timestamp=not args.allow_no_timestamp)
    except ValueError as exc:
        print(f"Error: the expected key could not be read: {exc}.", file=err)
        return USAGE
    if args.json:
        print(json.dumps(report.as_dict(), indent=2), file=out)
    else:
        pack = envelope.get("pack") if isinstance(envelope, dict) else None
        if report.ok and isinstance(pack, dict):
            agent = pack.get("agent") if isinstance(pack.get("agent"), dict) else {}
            decision = pack.get("decision") if isinstance(pack.get("decision"), dict) else {}
            decision = decision.get("outcome")
            print(f"Evidence pack {pack.get('id')} for {agent.get('name') or 'an agent'}: decision {decision}.", file=out)
        for c in report.checks:
            print(f"{'ok  ' if c.ok else 'FAIL'} {c.name}: {c.detail}", file=out)
        if report.ok and args.tsa_cert is None and report.timestamped_at:
            print("Note: no trusted TSA certificate was given, so the time-stamp authority was not checked.", file=out)
        print("Result: verified." if report.ok else "Result: NOT verified.", file=out)
    return OK if report.ok else FAILED
