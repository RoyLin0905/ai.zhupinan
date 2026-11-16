"""Upload checks and privacy handling for documents and proofs (S02).

Rules this module enforces:
- Size, type and page limits are checked on the bytes, never trusted from the file name.
- The original never persists: bytes stay in memory; a scan is written to a private temp folder only for the
  duration of the model call and removed in `finally`, also on errors.
- Text goes through privacy.mask before any model call; the masked text is returned as the preview.
- Images (and scanned PDFs) cannot be masked locally, so only the team's synthetic samples, recognised by
  SHA-256, may be sent to the model. Anything else is refused before any outbound call.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MAX_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 10
EXT_KIND = {".txt": "text", ".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}

# Folders holding the team's synthetic documents. Only these images may be sent to the model.
SYNTHETIC_DIRS = [ROOT / "demo" / "web" / "samples", ROOT / "validation" / "docs", ROOT / "validation" / "docs_holiday"]


def _digest_dir(dirs: list[Path]) -> set[str]:
    out = set()
    for d in dirs:
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in EXT_KIND:
                    out.add(hashlib.sha256(f.read_bytes()).hexdigest())
    return out


SYNTHETIC_DIGESTS = _digest_dir(SYNTHETIC_DIRS)

REAL_IMAGE_MSG = ("為保護個資，決賽示範版只接受合成範例的掃描圖。真實圖片無法先在本機遮蔽個資，"
                  "因此不會送給 AI。請改用文字檔或可選取文字的 PDF。")


class Refused(HTTPException):
    """HTTPException with a machine-readable code (the UI shows a dedicated stop screen for real images)."""

    def __init__(self, status: int, detail: str, code: str):
        super().__init__(status, detail)
        self.code = code


def tmp_root() -> Path:
    p = Path(os.environ.get("ZP_TMP_DIR") or HERE / "tmp" / "ingest")
    p.mkdir(parents=True, exist_ok=True)
    return p


def sniff(raw: bytes) -> str | None:
    if raw.startswith(b"%PDF-"):
        return "pdf"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if b"\x00" in raw[:8192]:
        return None
    try:
        raw.decode("utf-8-sig")
        return "text"
    except UnicodeDecodeError:
        return None


@dataclass
class Upload:
    filename: str
    digest: str
    kind: str                       # text | pdf_text | image | pdf_scan
    synthetic_sample: bool
    text: str | None = None
    image_bytes: bytes | None = None
    image_suffix: str = ".png"
    notes: list[str] = field(default_factory=list)

    @property
    def is_image(self) -> bool:
        return self.image_bytes is not None


def inspect(raw: bytes, filename: str) -> Upload:
    """Validate an upload entirely in memory. Raises HTTPException with a readable message."""
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "檔案太大（上限 15 MB）")
    if not raw:
        raise HTTPException(422, "檔案是空的")
    ext = Path(filename or "").suffix.lower()
    claimed = EXT_KIND.get(ext)
    if claimed is None:
        raise HTTPException(415, "只接受 JPG、PNG、PDF 或 TXT 檔案")
    actual = sniff(raw)
    if actual != claimed:
        raise HTTPException(415, "檔案內容與副檔名不符，請確認檔案格式（只接受 JPG、PNG、PDF、TXT）")
    digest = hashlib.sha256(raw).hexdigest()
    synthetic = digest in SYNTHETIC_DIGESTS
    up = Upload(filename=filename, digest=digest, kind="text", synthetic_sample=synthetic)
    if actual == "text":
        up.text = raw.decode("utf-8-sig")
        return up
    if actual in ("png", "jpeg"):
        up.kind, up.image_bytes, up.image_suffix = "image", raw, ".png" if actual == "png" else ".jpg"
        return _require_synthetic(up)
    import fitz  # PyMuPDF; opened from memory, never from disk
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as e:  # noqa: BLE001 - any parser failure means an unusable file
        raise HTTPException(422, "PDF 檔案損毀或無法開啟") from e
    try:
        if doc.needs_pass:
            raise HTTPException(422, "PDF 有密碼保護，無法讀取")
        if doc.page_count > MAX_PDF_PAGES:
            raise HTTPException(413, f"PDF 超過 {MAX_PDF_PAGES} 頁，請只上傳相關頁面")
        if doc.page_count == 0:
            raise HTTPException(422, "PDF 檔案損毀或無法開啟")
        text = "\n".join(p.get_text() for p in doc)
        if len(text.strip()) > 50:
            up.kind, up.text = "pdf_text", text
            return up
        up.kind = "pdf_scan"
        if not synthetic:
            return _require_synthetic(up)
        up.image_bytes = doc[0].get_pixmap(dpi=150).tobytes("png")
        up.notes.append("掃描 PDF 只讀取第 1 頁")
        return up
    finally:
        doc.close()


def _require_synthetic(up: Upload) -> Upload:
    if not up.synthetic_sample:
        raise Refused(422, REAL_IMAGE_MSG, "real_image_blocked")
    return up


@contextmanager
def image_file(up: Upload):
    """A scan written to a private temp folder for the model call only; always removed afterwards."""
    folder = Path(tempfile.mkdtemp(prefix="zp-", dir=tmp_root()))
    try:
        path = folder / f"{uuid.uuid4().hex}{up.image_suffix}"
        path.write_bytes(up.image_bytes or b"")
        yield path
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- warnings per date field
CN_DIGITS = "〇零一二三四五六七八九十"


def field_warning(excerpt: str | None, is_image: bool) -> str | None:
    """Plain-language reasons to double-check a date. No confidence scores."""
    reasons = []
    s = excerpt or ""
    if is_image:
        reasons.append("掃描文件，請對照原件核對")
    if any(c in s for c in CN_DIGITS):
        reasons.append("國字日期，請核對換算")
    elif "年" in s and any(ch.isdigit() for ch in s.split("年")[0]):
        year = "".join(ch for ch in s.split("年")[0] if ch.isdigit())
        if year and int(year) < 1911:
            reasons.append("民國年已換算為西元，請核對")
    if not s:
        reasons.append("找不到原文片段，請核對")
    return "；".join(reasons) or None


# ---------------------------------------------------------------- pending extractions (memory only)
PENDING_TTL = 30 * 60


@dataclass
class Pending:
    account_id: int
    created: float
    document_kind: str
    filename: str
    digest: str
    flagged: dict[str, str | None]   # "worker_index:field" -> value as extracted


_pending: dict[str, Pending] = {}


def remember(account_id: int, kind: str, up: Upload, flagged: dict[str, str | None]) -> str:
    _gc()
    xid = uuid.uuid4().hex
    _pending[xid] = Pending(account_id, time.monotonic(), kind, up.filename, up.digest, flagged)
    return xid


def take(xid: str, account_id: int, consume: bool = True) -> Pending:
    _gc()
    p = _pending.get(xid or "")
    if p is None or p.account_id != account_id:
        raise HTTPException(404, "找不到這次的辨識結果（可能已逾時或已確認），請重新上傳")
    if consume:
        _pending.pop(xid, None)
    return p


def forget_account(account_id: int | None = None) -> None:
    for k in [k for k, p in _pending.items() if account_id is None or p.account_id == account_id]:
        _pending.pop(k, None)


def _gc() -> None:
    now = time.monotonic()
    for k in [k for k, p in _pending.items() if now - p.created > PENDING_TTL]:
        _pending.pop(k, None)
