"""Digital IDs: create a self-signed signing certificate (PKCS#12 / .pfx) and inspect existing ones."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

DOCUMENT_SIGNING = ObjectIdentifier("1.3.6.1.4.1.311.10.3.12")


@dataclass
class DigitalIdInfo:
    name: str
    email: str
    organization: str
    issuer: str
    valid_from: dt.datetime
    valid_to: dt.datetime
    self_signed: bool

    @property
    def expired(self) -> bool:
        return dt.datetime.now(dt.timezone.utc) > self.valid_to


def create_digital_id(path: str, name: str, password: str, email: str = "", organization: str = "",
                      country: str = "", years: int = 5) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if organization:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    if country and len(country) == 2:
        attrs.append(x509.NameAttribute(NameOID.COUNTRY_NAME, country.upper()))
    if email:
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    subject = x509.Name(attrs)
    now = dt.datetime.now(dt.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(subject).issuer_name(subject)
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - dt.timedelta(minutes=5))
               .not_valid_after(now + dt.timedelta(days=365 * years))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([DOCUMENT_SIGNING, ExtendedKeyUsageOID.EMAIL_PROTECTION]),
                              critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if email:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    data = pkcs12.serialize_key_and_certificates(
        name.encode("utf-8"), key, cert, None,
        serialization.BestAvailableEncryption(password.encode("utf-8")) if password else serialization.NoEncryption())
    Path(path).write_bytes(data)
    return path


def load_digital_id(path: str, password: str):
    """Returns (private_key, certificate, additional_certs); raises ValueError on a wrong password."""
    return pkcs12.load_key_and_certificates(Path(path).read_bytes(), password.encode("utf-8") if password else None)


def describe(cert: x509.Certificate) -> DigitalIdInfo:
    def get(n, oid):
        v = n.get_attributes_for_oid(oid)
        return v[0].value if v else ""
    subj, iss = cert.subject, cert.issuer
    return DigitalIdInfo(name=get(subj, NameOID.COMMON_NAME), email=get(subj, NameOID.EMAIL_ADDRESS),
                         organization=get(subj, NameOID.ORGANIZATION_NAME),
                         issuer=get(iss, NameOID.COMMON_NAME) or iss.rfc4514_string(),
                         valid_from=cert.not_valid_before_utc, valid_to=cert.not_valid_after_utc,
                         self_signed=subj == iss)
