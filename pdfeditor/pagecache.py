"""Per-page caches for expensive, read-only page analysis.

The editor keeps asking the same questions about a page - which images it holds, which of them are part of
a scan, what its editable text blocks are - on every click, scroll and commit. Some answers are costly:
PyMuPDF MD5-hashes every image's pixels just to report image xrefs. The answers only change when the page's
content changes, so they are cached against a cheap fingerprint of the page:

  * its content-stream xrefs and a hash of their raw (still compressed) bytes - a hash, not a length,
    because replacing "12" with "34" leaves the length unchanged;
  * the document's object count - inserting an image, a font or OCR metadata adds objects;
  * rotation and mediabox.

Cached values are shared: callers must treat them as read-only.
"""
from __future__ import annotations

import weakref

_CACHE: dict = {}
_LIMIT = 1024


def fingerprint(page):
    """A value that changes whenever the page's content does (None if it cannot be computed)."""
    try:
        doc = page.parent
        contents = tuple(page.get_contents())
        digest = hash(b"".join(doc.xref_stream_raw(x) or b"" for x in contents))
        return contents, digest, doc.xref_length(), page.rotation, tuple(page.mediabox)
    except Exception:
        return None


def get(page, kind: str, compute):
    """Cached `compute()` for this page, recomputed only after the page changes."""
    doc = page.parent
    token = fingerprint(page)
    key = (id(doc), page.number, kind)
    if token is not None:
        hit = _CACHE.get(key)
        if hit is not None and hit[0]() is doc and hit[1] == token:
            return hit[2]
    value = compute()
    if token is not None:
        try:
            ref = weakref.ref(doc)
        except TypeError:
            return value
        if len(_CACHE) >= _LIMIT:
            _CACHE.clear()
        _CACHE[key] = (ref, token, value)
    return value


def image_infos(page) -> list[dict]:
    """page.get_image_info(xrefs=True), cached - and without hashing pixels whenever possible."""
    return get(page, "image_info", lambda: _image_infos(page))


def _image_infos(page) -> list[dict]:
    """To tell which image object each placement shows, PyMuPDF MD5-hashes every image's pixels - seconds
    on a scan carrying many edit patches. But a placement reports its pixel size, and so does every image
    object on the page: when each size belongs to exactly one object, the size alone settles it. Only
    when two objects share a size (or anything is unexpected) does it fall back to hashing."""
    try:
        by_size: dict = {}
        for item in page.get_images(full=True):
            by_size.setdefault((item[2], item[3]), set()).add(item[0])
        if all(len(xrefs) == 1 for xrefs in by_size.values()):
            infos = page.get_image_info()                  # no xrefs requested: no hashing
            if all((i.get("width"), i.get("height")) in by_size for i in infos):
                for i in infos:
                    i["xref"] = next(iter(by_size[(i["width"], i["height"])]))
                return infos
    except Exception:
        pass
    return page.get_image_info(xrefs=True)


def clear():
    _CACHE.clear()
