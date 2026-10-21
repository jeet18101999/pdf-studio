"""Certificate-based digital signatures (PAdES) with pyHanko: sign, and verify signatures.

Signing writes an incremental update, so the document must not be modified afterwards
(any later change would invalidate the signature)."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path

import pymupdf

try:
    from asn1crypto import x509 as asn1_x509
    from cryptography.hazmat.primitives import serialization
    from pyhanko.pdf_utils.images import PdfImage
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign import fields, signers, validation
    from pyhanko.stamp import TextStampStyle
    from pyhanko_certvalidator import ValidationContext
    AVAILABLE = True
except Exception:          # pragma: no cover - pyHanko missing
    AVAILABLE = False

import logging

from . import digitalid

for _name in ("pyhanko", "pyhanko_certvalidator"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

ID_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PDFStudio" / "digital_ids"


@dataclass
class SignatureReport:
    field: str
    signer: str
    signed_at: str
    intact: bool          # document bytes covered by the signature are unchanged
    valid: bool           # cryptographically valid
    trusted: bool         # certificate chains to a trusted identity
    modified_after: bool  # document was changed after signing (beyond allowed form filling)
    summary: str


def _require():
    if not AVAILABLE:
        raise RuntimeError("Digital signatures need the 'pyhanko' package (pip install pyhanko).")


def signature_fields(pdf_bytes: bytes) -> list[tuple[str, bool]]:
    """(field name, already signed) for every signature field."""
    out = []
    with pymupdf.open("pdf", pdf_bytes) as doc:
        for page in doc:
            for w in page.widgets():
                if w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                    out.append((w.field_name, bool(w.is_signed)))
    return out


def sign_pdf(pdf_bytes: bytes, output_path: str, pfx_path: str, password: str, *,
             field_name: str | None = None, page: int = 0, rect: pymupdf.Rect | None = None,
             reason: str = "", location: str = "", contact: str = "", signature_image=None,
             certify: bool = False, show_details: bool = True) -> str:
    """Sign `pdf_bytes` and write the signed file.

    field_name: sign this (empty) signature field; if it doesn't exist a new field is created on
                `page` at `rect` (PyMuPDF unrotated coordinates, top-left origin).
    signature_image: optional PIL image (e.g. drawn signature) shown in the appearance."""
    _require()
    try:
        signer = signers.SimpleSigner.load_pkcs12(pfx_file=pfx_path, passphrase=password.encode("utf-8") if password else None)
    except Exception as ex:
        raise ValueError("Could not open the Digital ID - check the password.") from ex
    if signer is None:
        raise ValueError("Could not open the Digital ID - check the password.")

    existing = dict(signature_fields(pdf_bytes))
    new_spec = None
    if not field_name or field_name not in existing:
        field_name = field_name or _next_field_name(existing)
        if rect is None:
            rect = pymupdf.Rect(36, 36, 236, 96)
        with pymupdf.open("pdf", pdf_bytes) as doc:
            mb = doc[page].mediabox
        # PDF user space: origin bottom-left
        box = (int(rect.x0 + mb.x0), int(mb.y1 - rect.y1), int(rect.x1 + mb.x0), int(mb.y1 - rect.y0))
        new_spec = fields.SigFieldSpec(sig_field_name=field_name, on_page=page, box=box)
    elif existing[field_name]:
        raise ValueError(f"Field “{field_name}” is already signed.")

    stamp_text = "Digitally signed by %(signer)s\nDate: %(ts)s"
    if reason:
        stamp_text += f"\nReason: {reason}"
    if location:
        stamp_text += f"\nLocation: {location}"
    style_kw = dict(stamp_text=stamp_text if show_details else " ", border_width=0)
    if signature_image is not None:
        style_kw.update(background=PdfImage(signature_image.convert("RGBA")), background_opacity=1.0)
    style = TextStampStyle(**style_kw)

    meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason or None, location=location or None,
                                        contact_info=contact or None, certify=certify, md_algorithm="sha256")
    writer = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False)
    pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style, new_field_spec=new_spec)
    buf = io.BytesIO()
    pdf_signer.sign_pdf(writer, output=buf)
    Path(output_path).write_bytes(buf.getvalue())
    return output_path


def _next_field_name(existing) -> str:
    i = 1
    while f"Signature{i}" in existing:
        i += 1
    return f"Signature{i}"


def trusted_certificates(extra_pfx: list[tuple[str, str]] | None = None) -> list:
    """Certificates the user trusts: every Digital ID created in PDF Studio (public .cer files)."""
    _require()
    certs = []
    if ID_DIR.is_dir():
        for cer in ID_DIR.glob("*.cer"):
            try:
                certs.append(asn1_x509.Certificate.load(_der(cer.read_bytes())))
            except Exception:
                pass
    for path, pw in extra_pfx or []:
        try:
            _, cert, _ = digitalid.load_digital_id(path, pw)
            certs.append(asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)))
        except Exception:
            pass
    return certs


def _der(data: bytes) -> bytes:
    if data.lstrip().startswith(b"-----BEGIN"):
        from cryptography import x509
        return x509.load_pem_x509_certificate(data).public_bytes(serialization.Encoding.DER)
    return data


def verify(pdf_bytes: bytes, trusted: list | None = None) -> list[SignatureReport]:
    _require()
    reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
    reports = []
    for sig in reader.embedded_signatures:
        vc = ValidationContext(trust_roots=trusted or [], allow_fetching=False)
        try:
            diff_ok = True
            try:
                st = validation.validate_pdf_signature(sig, vc)
            except Exception:
                # change analysis can fail on unusual form structures; integrity and validity are still checked
                diff_ok = False
                st = validation.validate_pdf_signature(sig, ValidationContext(trust_roots=trusted or [],
                                                                             allow_fetching=False), skip_diff=True)
            cert = sig.signer_cert
            signer = cert.subject.native.get("common_name", "Unknown")
            when = sig.self_reported_timestamp.strftime("%Y-%m-%d %H:%M") if sig.self_reported_timestamp else ""
            modified = getattr(st, "modification_level", None)
            mod_after = modified is not None and "FORM_FILLING" not in str(modified) and "NONE" not in str(modified)
            if not st.intact:
                summary = "INVALID – the document was altered after signing"
            elif not st.valid:
                summary = "INVALID – signature is corrupt"
            elif not st.trusted:
                summary = "Valid, but the signer's identity is not trusted"
            else:
                summary = "Valid and trusted"
            if not diff_ok:
                summary += " (later changes could not be analysed)"
            reports.append(SignatureReport(sig.field_name, signer, when, bool(st.intact), bool(st.valid),
                                           bool(st.trusted), mod_after, summary))
        except Exception as ex:
            reports.append(SignatureReport(sig.field_name, "?", "", False, False, False, True, f"Could not verify: {ex}"))
    return reports


def register_identity(pfx_path: str, password: str) -> Path:
    """Store the public certificate of a Digital ID so signatures made with it are trusted."""
    _, cert, _ = digitalid.load_digital_id(pfx_path, password)
    ID_DIR.mkdir(parents=True, exist_ok=True)
    out = ID_DIR / (Path(pfx_path).stem + ".cer")
    out.write_bytes(cert.public_bytes(serialization.Encoding.DER))
    return out
