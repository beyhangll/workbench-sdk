"""Verify a signed evidence pack (#492). Standard library and ``cryptography`` only.

This file is shipped twice, byte for byte: as ``aq_api/evidence/verify.py``
in the API, which checks every pack right after signing it, and as
``aq_sdk/evidence_verify.py`` in the SDK, behind ``aq evidence verify``. A
test fails when the two copies differ. A customer can read this one file and
check a pack without trusting AQ's servers.

What is checked, in order:

1. ``pack_sha256`` is the SHA-256 of the canonical JSON of ``pack``: keys
   sorted, no spaces, UTF-8, non-ASCII kept as is.
2. ``signature.value`` is a valid signature over that digest by
   ``signature.public_key``, with the algorithm named in
   ``signature.algorithm`` (the AWS KMS names). When the caller passes the
   key it expects, or its SHA-256 fingerprint, the embedded key must be that
   key. An expected signer is required for successful verification.
3. ``timestamp.token`` is an RFC 3161 time-stamp token over the SHA-256 of
   the signature bytes. The token's CMS signature is checked against the
   certificate inside the token, and the certificate must be marked for time
   stamping. When the caller passes trusted TSA certificates, the token's
   certificate must be one of them or be issued by one of them, and be valid
   at the stamped time.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ENVELOPE_FORMAT = "aq.signed-evidence/1"
SIGNING_ALGORITHMS = ("ECDSA_SHA_256", "RSASSA_PSS_SHA_256", "RSASSA_PKCS1_V1_5_SHA_256")

OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_SHA384 = "2.16.840.1.101.3.4.2.2"
OID_SHA512 = "2.16.840.1.101.3.4.2.3"
OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SIGNING_CERTIFICATE = "1.2.840.113549.1.9.16.2.12"
OID_SIGNING_CERTIFICATE_V2 = "1.2.840.113549.1.9.16.2.47"
OID_MGF1 = "1.2.840.113549.1.1.8"
OID_RSA = "1.2.840.113549.1.1.1"
OID_RSA_PSS = "1.2.840.113549.1.1.10"
OID_RSA_SHA256 = "1.2.840.113549.1.1.11"
OID_RSA_SHA384 = "1.2.840.113549.1.1.12"
OID_RSA_SHA512 = "1.2.840.113549.1.1.13"
OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
OID_ECDSA_SHA384 = "1.2.840.10045.4.3.3"
OID_ECDSA_SHA512 = "1.2.840.10045.4.3.4"
OID_EKU_TIME_STAMPING = "1.3.6.1.5.5.7.3.8"


class EvidenceFormatError(ValueError):
    """The input is not a signed evidence pack, or a part of it cannot be read."""


# ── Canonical JSON ─────────────────────────────────────────────────────────

def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def pack_digest(pack: Any) -> bytes:
    return hashlib.sha256(canonical_bytes(pack)).digest()


# ── DER reading ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Der:
    """One DER element: its tag byte, its content and its full encoding."""

    tag: int
    content: bytes
    raw: bytes

    def children(self) -> list["Der"]:
        return read_all(self.content)


def read_one(data: bytes, pos: int = 0) -> tuple[Der, int]:
    if pos + 2 > len(data):
        raise EvidenceFormatError("DER: truncated element")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise EvidenceFormatError("DER: multi-byte tags are not supported")
    first = data[pos + 1]
    head = 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4:
            raise EvidenceFormatError("DER: indefinite or oversized length")
        if pos + 2 + count > len(data):
            raise EvidenceFormatError("DER: truncated length")
        length = int.from_bytes(data[pos + 2:pos + 2 + count], "big")
        head += count
    end = pos + head + length
    if end > len(data):
        raise EvidenceFormatError("DER: element runs past the end")
    return Der(tag, data[pos + head:end], data[pos:end]), end


def read_all(data: bytes) -> list[Der]:
    out, pos = [], 0
    while pos < len(data):
        el, pos = read_one(data, pos)
        out.append(el)
    return out


def parse(data: bytes) -> Der:
    el, end = read_one(data)
    if end != len(data):
        raise EvidenceFormatError("DER: trailing bytes")
    return el


def oid(el: Der) -> str:
    if el.tag != 0x06 or not el.content:
        raise EvidenceFormatError("DER: expected an object identifier")
    first = el.content[0]
    parts = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    value = 0
    for b in el.content[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(value)
            value = 0
    return ".".join(str(p) for p in parts)


def integer(el: Der) -> int:
    if el.tag != 0x02:
        raise EvidenceFormatError("DER: expected an integer")
    return int.from_bytes(el.content, "big", signed=True)


def generalized_time(el: Der) -> datetime:
    if el.tag != 0x18:
        raise EvidenceFormatError("DER: expected a GeneralizedTime")
    text = el.content.decode("ascii")
    if not text.endswith("Z"):
        raise EvidenceFormatError("DER: GeneralizedTime must be UTC")
    main, _, frac = text[:-1].partition(".")
    stamp = datetime.strptime(main, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    if frac:
        stamp = stamp.replace(microsecond=int((frac + "000000")[:6]))
    return stamp


# ── RFC 3161 tokens ────────────────────────────────────────────────────────

_HASHES = {OID_SHA256: "sha256", OID_SHA384: "sha384", OID_SHA512: "sha512"}


@dataclass(frozen=True)
class TstInfo:
    policy: str
    hash_oid: str
    imprint: bytes
    serial: int
    gen_time: datetime
    nonce: int | None


@dataclass(frozen=True)
class Token:
    tst: TstInfo
    tst_der: bytes
    certificates: tuple[bytes, ...]
    signer: Der


def parse_tst_info(der: bytes) -> TstInfo:
    root = parse(der)
    parts = root.children()
    if root.tag != 0x30 or len(parts) < 5:
        raise EvidenceFormatError("TSTInfo: too few fields")
    if integer(parts[0]) != 1:
        raise EvidenceFormatError("Unsupported TSTInfo version: expected version 1")
    imprint = parts[2].children()
    name = _hash_name(oid(imprint[0].children()[0]))
    if imprint[1].tag != 0x04 or len(imprint[1].content) != hashlib.new(name).digest_size:
        raise EvidenceFormatError("The time-stamp imprint has the wrong digest length")
    nonce = None
    for el in parts[5:]:
        if el.tag == 0x02:
            nonce = integer(el)
    return TstInfo(policy=oid(parts[1]), hash_oid=oid(imprint[0].children()[0]), imprint=imprint[1].content,
                   serial=integer(parts[3]), gen_time=generalized_time(parts[4]), nonce=nonce)


def parse_token(der: bytes) -> Token:
    """Read a TimeStampToken: a CMS ContentInfo holding SignedData over a TSTInfo."""
    info = parse(der).children()
    if len(info) != 2 or oid(info[0]) != OID_SIGNED_DATA or info[1].tag != 0xA0:
        raise EvidenceFormatError("The time-stamp token is not CMS SignedData")
    signed = info[1].children()[0].children()
    encap = signed[2].children()
    if oid(encap[0]) != OID_TST_INFO:
        raise EvidenceFormatError("The time-stamp token does not hold a TSTInfo")
    tst_der = encap[1].children()[0].content
    certs: tuple[bytes, ...] = ()
    signer_infos = None
    for el in signed[3:]:
        if el.tag == 0xA0:
            certs = tuple(c.raw for c in el.children() if c.tag == 0x30)
        elif el.tag == 0x31:
            signer_infos = el.children()
    if not signer_infos or len(signer_infos) != 1:
        raise EvidenceFormatError("The time-stamp token must have exactly one signer")
    return Token(parse_tst_info(tst_der), tst_der, certs, signer_infos[0])


def _hash_name(hash_oid: str) -> str:
    name = _HASHES.get(hash_oid)
    if name is None:
        raise EvidenceFormatError(f"Unsupported hash algorithm {hash_oid}")
    return name


def _crypto_hash(name: str) -> Any:
    from cryptography.hazmat.primitives import hashes
    return {"sha256": hashes.SHA256(), "sha384": hashes.SHA384(), "sha512": hashes.SHA512()}[name]


def _signer_cert(token: Token, sid: Der) -> Any:
    from cryptography import x509
    certs = [x509.load_der_x509_certificate(c) for c in token.certificates]
    for cert in certs:
        if sid.tag == 0x30:
            issuer, serial = sid.children()
            if cert.serial_number == integer(serial) and cert.issuer.public_bytes() == issuer.raw:
                return cert
        elif sid.tag == 0x80:
            try:
                ski = cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
            except x509.ExtensionNotFound:
                continue
            if ski == sid.content:
                return cert
    raise EvidenceFormatError("The time-stamp token does not include its signing certificate")


def _pss_parameters(params: Der | None, digest: str) -> tuple[str, int]:
    """Read supported RSA-PSS declarations. Omitted fields have the RFC 4055 defaults."""
    if params is None or params.tag != 0x30:
        raise EvidenceFormatError("RSA-PSS time-stamp signatures need explicit parameters")
    name, mgf_name, salt_length, trailer = "sha1", "sha1", 20, 1
    seen = set()
    for field in params.children():
        if field.tag in seen or field.tag not in (0xA0, 0xA1, 0xA2, 0xA3):
            raise EvidenceFormatError("Invalid RSA-PSS parameters")
        seen.add(field.tag)
        value = field.children()[0]
        if field.tag == 0xA0:
            name = _hash_name(oid(value.children()[0]))
        elif field.tag == 0xA1:
            mgf = value.children()
            if len(mgf) != 2 or oid(mgf[0]) != OID_MGF1:
                raise EvidenceFormatError("Unsupported RSA-PSS mask generation")
            mgf_name = _hash_name(oid(mgf[1].children()[0]))
        elif field.tag == 0xA2:
            salt_length = integer(value)
        else:
            trailer = integer(value)
    if name != digest or mgf_name != digest or salt_length < 0 or trailer != 1:
        raise EvidenceFormatError("RSA-PSS parameters conflict with the signed digest")
    return mgf_name, salt_length


def _verify_raw(public_key: Any, signature: bytes, data: bytes, sig_oid: str, hash_name: str,
                params: Der | None = None) -> None:
    """Verify a CMS signer signature using its declared algorithm and parameters."""
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    implied = {OID_ECDSA_SHA256: "sha256", OID_ECDSA_SHA384: "sha384", OID_ECDSA_SHA512: "sha512",
               OID_RSA_SHA256: "sha256", OID_RSA_SHA384: "sha384", OID_RSA_SHA512: "sha512"}
    if sig_oid in implied and implied[sig_oid] != hash_name:
        raise EvidenceFormatError("The time-stamp signature algorithm conflicts with its digest")
    algo = _crypto_hash(hash_name)
    if sig_oid in (OID_ECDSA_SHA256, OID_ECDSA_SHA384, OID_ECDSA_SHA512):
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or params is not None:
            raise EvidenceFormatError("Invalid ECDSA time-stamp algorithm")
        public_key.verify(signature, data, ec.ECDSA(algo))
    elif sig_oid in (OID_RSA, OID_RSA_SHA256, OID_RSA_SHA384, OID_RSA_SHA512):
        if not isinstance(public_key, rsa.RSAPublicKey) or (params is not None and params.tag != 0x05):
            raise EvidenceFormatError("Invalid RSA time-stamp algorithm")
        public_key.verify(signature, data, padding.PKCS1v15(), algo)
    elif sig_oid == OID_RSA_PSS:
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise EvidenceFormatError("RSA-PSS signature with a non-RSA certificate")
        mgf_name, salt_length = _pss_parameters(params, hash_name)
        public_key.verify(signature, data, padding.PSS(mgf=padding.MGF1(_crypto_hash(mgf_name)),
                                                      salt_length=salt_length), algo)
    else:
        raise EvidenceFormatError(f"Unsupported time-stamp signature algorithm {sig_oid}")


def _certificate_binding(attr: Der, attr_oid: str, cert: Any) -> None:
    """The first ESS certificate ID binds the selected TSA certificate (RFC 3161/5816)."""
    from cryptography.hazmat.primitives import serialization
    values = attr.children()
    if len(values) != 1:
        raise EvidenceFormatError("Invalid TSA signing-certificate attribute")
    cert_ids = values[0].children()[0].children()
    fields = cert_ids[0].children()
    name = "sha1" if attr_oid == OID_SIGNING_CERTIFICATE else "sha256"
    if attr_oid == OID_SIGNING_CERTIFICATE_V2 and fields[0].tag == 0x30:
        name = _hash_name(oid(fields.pop(0).children()[0]))
    raw = cert.public_bytes(serialization.Encoding.DER)
    if fields[0].tag != 0x04 or fields[0].content != hashlib.new(name, raw).digest():
        raise EvidenceFormatError("The signed TSA certificate hash does not match its signing certificate")
    if len(fields) > 1:
        issuer = fields[1].children()
        if (len(fields) != 2 or len(issuer) != 2 or integer(issuer[1]) != cert.serial_number
                or not any(n.tag == 0xA4 and n.content == cert.issuer.public_bytes()
                           for n in issuer[0].children())):
            raise EvidenceFormatError("The signed TSA issuer/serial does not match its signing certificate")


def _supported_extensions(cert: Any) -> bool:
    """Refuse critical constraints this limited offline verifier does not process."""
    from cryptography import x509
    handled = (x509.BasicConstraints, x509.KeyUsage, x509.ExtendedKeyUsage)
    return all(not ext.critical or isinstance(ext.value, handled) for ext in cert.extensions)


def verify_token(token: Token) -> Any:
    """Check the token's CMS signature. Returns the TSA certificate."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    fields = token.signer.children()
    sid, digest_alg = fields[1], fields[2]
    signed_attrs = next((f for f in fields[3:] if f.tag == 0xA0), None)
    rest = [f for f in fields[3:] if f.tag != 0xA0]
    if signed_attrs is None or len(rest) < 2:
        raise EvidenceFormatError("The time-stamp signer has no signed attributes")
    sig_alg, signature = rest[0], rest[1]
    if sig_alg.tag != 0x30 or len(sig_alg.children()) not in (1, 2):
        raise EvidenceFormatError("Invalid time-stamp signature AlgorithmIdentifier")
    hash_name = _hash_name(oid(digest_alg.children()[0]))
    content_type = message_digest = None
    attributes = {}
    for attr in signed_attrs.children():
        kind, values = attr.children()
        kind_oid = oid(kind)
        if kind_oid in attributes:
            raise EvidenceFormatError("Duplicate time-stamp signed attribute")
        attributes[kind_oid] = values
        if kind_oid == OID_CONTENT_TYPE:
            content_type = oid(values.children()[0])
        elif kind_oid == OID_MESSAGE_DIGEST:
            message_digest = values.children()[0].content
    if content_type != OID_TST_INFO:
        raise EvidenceFormatError("The time-stamp signer does not sign a TSTInfo")
    if message_digest != hashlib.new(hash_name, token.tst_der).digest():
        raise EvidenceFormatError("The time-stamp signature does not cover this TSTInfo")
    cert = _signer_cert(token, sid)
    if not cert.not_valid_before_utc <= token.tst.gen_time <= cert.not_valid_after_utc:
        raise EvidenceFormatError("The time-stamp certificate was not valid at the stamped time")
    try:
        extension = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        raise EvidenceFormatError("The time-stamp certificate is not marked for time stamping") from None
    if not extension.critical or list(extension.value) != [x509.ObjectIdentifier(OID_EKU_TIME_STAMPING)]:
        raise EvidenceFormatError("The time-stamp certificate needs a critical, timestamp-only extended key usage")
    if not _supported_extensions(cert):
        raise EvidenceFormatError("Unsupported critical time-stamp certificate extension")
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not usage.digital_signature and not usage.content_commitment:
            raise EvidenceFormatError("The TSA certificate does not permit signing")
    except x509.ExtensionNotFound:
        pass
    bindings = [k for k in (OID_SIGNING_CERTIFICATE, OID_SIGNING_CERTIFICATE_V2) if k in attributes]
    if not bindings:
        raise EvidenceFormatError("The time-stamp signature has no signed TSA certificate binding")
    for kind in bindings:
        _certificate_binding(attributes[kind], kind, cert)
    # The signature covers the attributes encoded as a SET, not as the [0] they are stored as.
    to_verify = b"\x31" + signed_attrs.raw[1:]
    try:
        _verify_raw(cert.public_key(), signature.content, to_verify, oid(sig_alg.children()[0]), hash_name,
                    sig_alg.children()[1] if len(sig_alg.children()) == 2 else None)
    except InvalidSignature:
        raise EvidenceFormatError("The time-stamp signature is not valid") from None
    return cert


def _trusted(cert: Any, token: Token, anchors: list[Any], at: datetime) -> bool:
    """Follow authorized CA paths, or an explicitly pinned TSA leaf, valid at stamped time."""
    from cryptography import x509
    pool = [x509.load_der_x509_certificate(c) for c in token.certificates]
    remaining = 128  # Bound total issuance checks, not only per-path depth.

    def valid(c: Any) -> bool:
        return c.not_valid_before_utc <= at <= c.not_valid_after_utc and _supported_extensions(c)

    def ca(c: Any, below: int) -> bool:
        try:
            constraints = c.extensions.get_extension_for_class(x509.BasicConstraints).value
        except x509.ExtensionNotFound:
            return False
        if not constraints.ca or (constraints.path_length is not None and below > constraints.path_length):
            return False
        try:
            if not c.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
                return False
        except x509.ExtensionNotFound:
            pass
        try:
            eku = c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if (x509.ObjectIdentifier(OID_EKU_TIME_STAMPING) not in eku
                    and x509.ObjectIdentifier("2.5.29.37.0") not in eku):
                return False
        except x509.ExtensionNotFound:
            pass
        return valid(c)

    def walk(current: Any, path: tuple[Any, ...], below: int) -> bool:
        nonlocal remaining
        if current in path or len(path) >= 8 or not valid(current):
            return False
        if current in anchors:
            return True
        try:
            is_ca = current.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        except x509.ExtensionNotFound:
            is_ca = False
        below += int(is_ca and current.subject != current.issuer)
        for parent in [*anchors, *pool]:
            if parent == current or parent.subject != current.issuer or not ca(parent, below):
                continue
            if remaining <= 0:
                return False
            remaining -= 1
            try:
                current.verify_directly_issued_by(parent)
            except Exception:
                continue
            if walk(parent, (*path, current), below):
                return True
        return False

    return walk(cert, (), 0)


# ── Pack signatures ────────────────────────────────────────────────────────

def public_key_fingerprint(public_key_pem: str) -> str:
    """SHA-256 of the key's DER SubjectPublicKeyInfo, as hex."""
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def verify_signature(algorithm: str, public_key_pem: str, digest: bytes, signature: bytes) -> bool:
    """Whether ``signature`` signs the SHA-256 ``digest`` with the named KMS algorithm."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
    key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    pre = utils.Prehashed(hashes.SHA256())
    try:
        if algorithm == "ECDSA_SHA_256" and isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, digest, ec.ECDSA(pre))
        elif algorithm == "RSASSA_PSS_SHA_256" and isinstance(key, rsa.RSAPublicKey):
            # KMS uses a salt as long as the digest.
            key.verify(signature, digest, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), pre)
        elif algorithm == "RSASSA_PKCS1_V1_5_SHA_256" and isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, digest, padding.PKCS1v15(), pre)
        else:
            return False
    except InvalidSignature:
        return False
    return True


# ── The whole envelope ─────────────────────────────────────────────────────

@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    timestamped_at: str | None = None
    key_fingerprint: str | None = None
    key_pinned: bool = False
    tsa_trusted: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, ok, detail))
        return ok

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "key_fingerprint": self.key_fingerprint, "key_pinned": self.key_pinned,
                "tsa_trusted": self.tsa_trusted, "timestamped_at": self.timestamped_at,
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks]}


def _b64(value: Any, what: str) -> bytes:
    try:
        return base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        raise EvidenceFormatError(f"{what} is not base64") from None


def verify_envelope(envelope: Any, *, expected_public_key_pem: str | None = None,
                    expected_fingerprint: str | None = None, tsa_certificates_pem: bytes | None = None,
                    require_timestamp: bool = True) -> Report:
    """Run every check on a signed evidence pack and report each one. Never raises on a bad pack."""
    report = Report()
    if not isinstance(envelope, dict) or envelope.get("format") != ENVELOPE_FORMAT:
        report.add("format", False, f"Not a signed evidence pack: format must be {ENVELOPE_FORMAT}.")
        return report
    pack = envelope.get("pack")
    try:
        digest = pack_digest(pack)
    except (TypeError, ValueError) as exc:
        report.add("content", False, f"The pack cannot be read as canonical JSON: {exc}.")
        return report
    report.add("content", digest.hex() == envelope.get("pack_sha256"),
               "The pack matches its SHA-256." if digest.hex() == envelope.get("pack_sha256")
               else "The pack does not match pack_sha256. It was changed after it was signed.")

    sig = envelope.get("signature")
    sig_bytes = b""
    if not isinstance(sig, dict):
        report.add("signature", False, "The pack has no signature.")
    else:
        # signature.signed_at is not covered by the signature, so it is never reported. Only the time-stamp
        # token's time is signed.
        algorithm, pem = sig.get("algorithm"), sig.get("public_key")
        try:
            sig_bytes = _b64(sig.get("value"), "signature.value")
            fingerprint = public_key_fingerprint(str(pem))
        except Exception as exc:
            report.add("signature", False, f"The signature or its key cannot be read: {exc}.")
        else:
            report.key_fingerprint = fingerprint
            if algorithm not in SIGNING_ALGORITHMS:
                report.add("signature", False, f"Unsupported signing algorithm {algorithm}.")
            else:
                valid = verify_signature(str(algorithm), str(pem), digest, sig_bytes)
                report.add("signature", valid, f"Valid {algorithm} signature by key {fingerprint[:16]}…"
                           if valid else "The signature does not match the pack.")
            pinned = None
            if expected_public_key_pem is not None:
                pinned = public_key_fingerprint(expected_public_key_pem)
            elif expected_fingerprint is not None:
                pinned = expected_fingerprint.strip().lower().replace(":", "")
            if pinned is None:
                report.add("key", False, "An expected signer is required. Obtain its public key or fingerprint independently.")
            elif re.fullmatch(r"[0-9a-f]{64}", pinned) is None:
                report.add("key", False, "The expected signer fingerprint must be a SHA-256 hex digest.")
            else:
                report.key_pinned = report.add(
                    "key", fingerprint == pinned,
                    "The pack was signed with the expected key." if fingerprint == pinned
                    else f"The pack was signed with key {fingerprint}, not the expected key {pinned}.")
            if sig.get("public_key_sha256") not in (None, fingerprint):
                report.add("key", False, "signature.public_key_sha256 does not match signature.public_key.")

    stamp = envelope.get("timestamp")
    if stamp is None:
        if require_timestamp:
            report.add("timestamp", False, "The pack has no RFC 3161 time-stamp.")
        return report
    if not isinstance(stamp, dict):
        report.add("timestamp", False, "The time-stamp must be an object when present.")
        return report
    try:
        token = parse_token(_b64(stamp.get("token"), "timestamp.token"))
        cert = verify_token(token)
        expected = hashlib.new(_hash_name(token.tst.hash_oid), sig_bytes).digest()
    except EvidenceFormatError as exc:
        report.add("timestamp", False, str(exc))
        return report
    except Exception as exc:
        report.add("timestamp", False, f"The time-stamp token cannot be read: {type(exc).__name__}.")
        return report
    covers = bool(sig_bytes) and token.tst.imprint == expected
    report.timestamped_at = token.tst.gen_time.isoformat()
    report.add("timestamp", covers,
               f"RFC 3161 time-stamp from {cert.subject.rfc4514_string()} at {report.timestamped_at}."
               if covers else "The time-stamp does not cover this signature.")
    if tsa_certificates_pem is not None:
        from cryptography import x509
        try:
            anchors = x509.load_pem_x509_certificates(tsa_certificates_pem)
        except ValueError:
            report.add("tsa", False, "The trusted TSA certificate file cannot be read.")
            return report
        try:
            trusted = _trusted(cert, token, anchors, token.tst.gen_time)
        except Exception:
            # Malformed certificate bags, including duplicate extensions, are untrusted input.
            trusted = False
        report.tsa_trusted = report.add(
            "tsa", trusted,
            "The time-stamp authority is one you trust." if trusted
            else "The time-stamp certificate does not chain to a trusted TSA certificate at the stamped time.")
    return report
