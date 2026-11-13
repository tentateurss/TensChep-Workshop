"""Screenshot import (beta): text recognition through the OCR.space web API, standard library only.

Nothing is sent anywhere unless the user configures it: set POECRAFT_OCR_SPACE_KEY to an OCR.space API key
(https://ocr.space/ocrapi). Without a key the planner says OCR is unavailable and asks for a text paste instead.
The screenshot is uploaded to OCR.space only when the user clicks the upload button; the UI names the service.
The recognised text then goes through the same parser as a paste (poecraft.engine.itemtext), with an extra warning
that OCR can misread numbers. No local OCR: the standard library cannot read text from an image.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .messages import M

ENDPOINT = "https://api.ocr.space/parse/image"
SERVICE = "ocr.space"
LANGS = {"en": ("eng", "2"), "ru": ("rus", "1"), "de": ("ger", "2")}     # OCR.space language code, engine
MAX_BYTES = 1024 * 1024                                                   # the free tier's file size limit


class OcrError(Exception):
    pass


def status() -> dict:
    return {"available": bool(os.environ.get("POECRAFT_OCR_SPACE_KEY")), "service": SERVICE,
            "max_bytes": MAX_BYTES}


def image_to_text(data_url: str, lang: str = "en", timeout: float = 45.0) -> str:
    """data_url: 'data:image/png;base64,...' as the browser's FileReader gives it. Returns the recognised text."""
    key = os.environ.get("POECRAFT_OCR_SPACE_KEY")
    if not key:
        raise OcrError(M("m.ocr.not_configured"))
    if not data_url.startswith("data:image/"):
        raise OcrError(M("m.ocr.not_image"))
    if len(data_url) * 3 // 4 > MAX_BYTES:
        raise OcrError(M("m.ocr.too_large", kb=MAX_BYTES // 1024))
    code, engine = LANGS.get(lang, LANGS["en"])
    body = urllib.parse.urlencode({"base64Image": data_url, "language": code, "OCREngine": engine,
                                   "scale": "true", "isTable": "false"}).encode()
    req = urllib.request.Request(ENDPOINT, body, {"apikey": key, "Content-Type": "application/x-www-form-urlencoded",
                                                  "User-Agent": config.user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise OcrError(str(e)) from e
    if res.get("IsErroredOnProcessing"):
        msg = res.get("ErrorMessage")
        raise OcrError("; ".join(msg) if isinstance(msg, list) else str(msg))
    parts = [p.get("ParsedText", "") for p in res.get("ParsedResults") or []]
    text = "\n".join(parts).strip()
    if not text:
        raise OcrError(M("m.ocr.no_text"))
    return text
