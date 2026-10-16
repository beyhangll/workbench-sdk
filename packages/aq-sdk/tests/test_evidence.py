"""Offline verification of fictional packs with synthetic public keys and TSA roots.

These prospective fixtures use disposable keys generated in memory with the
existing local test helpers. Only public key/certificate bytes and fictional
signed content are stored; historical source fixtures remain unchanged.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from aq_sdk import cli

FIXTURES = Path(__file__).parent / "fixtures"
PACK = FIXTURES / "evidence-pack-ess.json"
KEY = FIXTURES / "evidence-key.pem"
TSA = FIXTURES / "evidence-tsa-ess-root.pem"
FINGERPRINT = "c8a8bb375f1577d9d63f0b29261c7e885c55ca6aa922633089f6659211770cf3"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    return cli.main(["evidence", "verify", *argv], out=out, err=err), out.getvalue(), err.getvalue()


def test_the_fixed_pack_verifies_with_its_key_and_tsa():
    code, out, err = run(str(PACK), "--key-fingerprint", FINGERPRINT, "--tsa-cert", str(TSA))
    assert code == 0 and err == "", out
    assert "Evidence pack fixture-1 for Weather agent: decision release." in out
    assert "ok   timestamp: RFC 3161 time-stamp from CN=Test TSA at 2026-10-13T09:30:15+00:00." in out
    assert out.strip().endswith("Result: verified.")
    code, out, _ = run(str(PACK), "--public-key", str(KEY), "--json")
    assert code == 0 and json.loads(out)["key_pinned"] is True


def test_a_changed_pack_or_another_key_fails(tmp_path):
    env = json.loads(PACK.read_text())
    env["pack"]["decision"]["outcome"] = "do_not_release"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(env))
    code, out, _ = run(str(changed), "--key-fingerprint", FINGERPRINT)
    assert code == 1 and "FAIL content" in out and "Result: NOT verified." in out
    code, out, _ = run(str(PACK), "--key-fingerprint", "00" * 32)
    assert code == 1 and "FAIL key" in out


def test_a_valid_embedded_signer_without_an_independent_pin_is_refused():
    code, out, err = run(str(PACK))
    assert code == 1 and "expected signer is required" in out and err == ""
    assert "Result: NOT verified." in out


def test_usage_errors_exit_two(tmp_path):
    code, _, err = run(str(tmp_path / "missing.json"))
    assert code == 2 and "could not be read" in err
    code, _, err = run(str(PACK), "--tsa-cert", str(tmp_path / "missing.pem"))
    assert code == 2


def test_legacy_format_fixture_without_signed_tsa_binding_is_refused():
    code, out, _ = run(str(FIXTURES / "evidence-pack.json"), "--key-fingerprint", FINGERPRINT)
    assert code == 1 and "signed TSA certificate binding" in out


def test_a_repeated_key_is_refused_even_when_the_last_copy_is_the_signed_one(tmp_path):
    raw = PACK.read_text()
    # A fake decision first and the signed one last. A reader that keeps the first copy would show the fake one.
    forged = raw.replace('"pack": {', '"pack": {"decision": {"outcome": "do_not_release"}, ', 1)
    assert forged != raw
    path = tmp_path / "forged.json"
    path.write_text(forged)
    code, out, err = run(str(path), "--key-fingerprint", FINGERPRINT, "--tsa-cert", str(TSA))
    assert code == 2 and "repeats the key decision" in err and out == ""


def test_deeply_nested_json_is_refused_without_a_traceback(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000)
    code, _, err = run(str(path))
    assert code == 2 and "is not JSON" in err and "Traceback" not in err


def test_the_unsigned_signing_time_is_not_reported(tmp_path):
    env = json.loads(PACK.read_text())
    env["signature"]["signed_at"] = "1999-01-01T00:00:00+00:00"
    path = tmp_path / "dated.json"
    path.write_text(json.dumps(env))
    code, out, _ = run(str(path), "--key-fingerprint", FINGERPRINT, "--tsa-cert", str(TSA), "--json")
    report = json.loads(out)
    assert code == 0 and "signed_at" not in report and report["timestamped_at"] == "2026-10-13T09:30:15+00:00"
    assert "1999" not in out


@pytest.mark.parametrize("omit", [True, False])
def test_only_absent_or_null_timestamps_are_optional(tmp_path, omit):
    env = json.loads(PACK.read_text())
    if omit:
        env.pop("timestamp")
    else:
        env["timestamp"] = None
    path = tmp_path / "unstamped.json"
    path.write_text(json.dumps(env))
    code, _, _ = run(str(path), "--key-fingerprint", FINGERPRINT)
    assert code == 1
    code, _, _ = run(str(path), "--key-fingerprint", FINGERPRINT, "--allow-no-timestamp")
    assert code == 0


@pytest.mark.parametrize("stamp", ["malformed", "", [], ["malformed"], 0, False, True, {}])
def test_optional_timestamp_does_not_allow_malformed_present_values(tmp_path, stamp):
    from aq_sdk.evidence_verify import verify_envelope
    env = json.loads(PACK.read_text())
    env["timestamp"] = stamp
    report = verify_envelope(env, expected_fingerprint=FINGERPRINT, require_timestamp=False)
    assert not report.ok and any(c.name == "timestamp" and not c.ok for c in report.checks)
    path = tmp_path / "malformed-stamp.json"
    path.write_text(json.dumps(env))
    for options in ((), ("--allow-no-timestamp",)):
        code, out, err = run(str(path), "--key-fingerprint", FINGERPRINT, *options)
        assert code == 1 and "FAIL timestamp" in out and err == ""


def test_verification_needs_no_api_configuration_or_client(monkeypatch):
    for name in ("AQ_API_KEY", "AQ_ENDPOINT", "AQ_UI_URL", "AQ_PROJECT"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Offline verification must not configure or create an API client.")

    monkeypatch.setattr(cli, "build", forbidden)
    monkeypatch.setattr(cli, "Client", forbidden)
    code, out, err = run(str(PACK), "--public-key", str(KEY), "--tsa-cert", str(TSA), "--json")
    report = json.loads(out)
    assert code == 0 and err == "" and report["key_pinned"] and report["tsa_trusted"]


def test_missing_optional_cryptography_is_an_actionable_usage_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "cryptography", None)
    code, out, err = run(str(PACK), "--key-fingerprint", FINGERPRINT)
    assert code == 2 and out == ""
    assert "needs the cryptography package" in err and "aq-sdk[evidence]" in err
    # The optional dependency does not prevent normal evaluation argument parsing.
    args = cli.parser().parse_args(["eval", "run", "--dataset", "weather", "--target", "agent", "--budget", "1"])
    assert args.group == "eval" and args.target == "agent"


def test_a_timestamp_without_an_independent_tsa_anchor_reports_that_limit():
    code, out, err = run(str(PACK), "--key-fingerprint", FINGERPRINT, "--json")
    report = json.loads(out)
    assert code == 0 and err == "" and report["key_pinned"] and report["tsa_trusted"] is False
    code, out, err = run(str(PACK), "--key-fingerprint", FINGERPRINT)
    assert code == 0 and "time-stamp authority was not checked" in out and err == ""


def test_a_different_trusted_tsa_root_refuses_verification():
    code, out, err = run(str(PACK), "--public-key", str(KEY),
                         "--tsa-cert", str(FIXTURES / "evidence-tsa-root.pem"), "--json")
    report = json.loads(out)
    assert code == 1 and err == "" and report["key_pinned"] and not report["tsa_trusted"]
    assert any(c["name"] == "tsa" and not c["ok"] for c in report["checks"])


@pytest.mark.parametrize("token", ["MA==", "MIA=", "AAAA"])
def test_truncated_or_malformed_der_is_a_controlled_failure(tmp_path, token):
    env = json.loads(PACK.read_text())
    env["timestamp"]["token"] = token
    path = tmp_path / "malformed-der.json"
    path.write_text(json.dumps(env))
    code, out, err = run(str(path), "--key-fingerprint", FINGERPRINT)
    assert code == 1 and "FAIL timestamp" in out and err == ""


def test_copied_fixtures_are_fictional_and_contain_only_public_key_material():
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    serialization.load_pem_public_key(KEY.read_bytes())
    for name in ("evidence-tsa-ess-root.pem", "evidence-tsa-root.pem"):
        cert = x509.load_pem_x509_certificate((FIXTURES / name).read_bytes())
        assert cert.subject.rfc4514_string() == "CN=Test TSA Root"
    for path in FIXTURES.glob("evidence-*"):
        assert b"PRIVATE KEY" not in path.read_bytes()
    for name in ("evidence-pack.json", "evidence-pack-ess.json"):
        envelope = json.loads((FIXTURES / name).read_text())
        assert envelope["pack"]["id"] == "fixture-1"
        assert envelope["pack"]["agent"]["name"] == "Weather agent"
        assert "key_id" not in envelope["signature"]
        assert "tsa_url" not in envelope["timestamp"]
