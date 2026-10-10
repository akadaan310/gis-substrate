"""Extraction pipeline: drain pending extract_jobs and populate extracted_chunk.

Runs extractors based on artifact mime/extension:
  text-like -> utf-8 decoded, ~120-line chunks
  .zip      -> entry listing, one chunk
  images    -> PIL dims/EXIF into artifact meta + 320px thumbnail as derived artifact
  .pdf      -> pdftotext per-page chunks, else status 'unavailable'
  audio/video -> done, zero chunks, honest 'transcription unavailable' meta note
  unknown   -> done, zero chunks

The original blob is never modified or deleted. Extraction failure only
affects the job row (status/last_error); the artifact stays valid.
After extraction, search_fts is synced for the artifact (title + first ~50k
chars of extracted text). Chunks are application-synced (no DB triggers).
"""
import io
import json
import os
import shutil
import subprocess
import time
import zipfile

try:
    from PIL import Image, ExifTags
    _PIL = True
except ImportError:  # PIL may not be installed on every deployment
    Image = None
    ExifTags = None
    _PIL = False

from . import db as _db
from . import store
from . import model as _model

LINES_PER_CHUNK = 120
FTS_BODY_CHARS = 50000
MAX_CHUNKS = 2000  # hard bound so pathological files cannot blow up the DB
MAX_ATTEMPTS = 5   # automatic retries stop here; manual retry still allowed

TEXT_EXTS = frozenset([
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml",
    ".html", ".htm", ".log", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".sh", ".bash", ".sql", ".rb", ".php", ".swift",
    ".kt", ".scala", ".r", ".lua", ".pl", ".ps1",
])
IMAGE_EXTS = frozenset([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                        ".tif", ".tiff", ".heic"])
AV_EXTS = frozenset([".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac",
                     ".mp4", ".mov", ".webm", ".mkv", ".avi"])


def _classify(mime, filename):
    mime = (mime or "").lower()
    ext = os.path.splitext((filename or "").lower())[1]
    if ext == ".zip" or mime in ("application/zip", "application/x-zip-compressed"):
        return "zip"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext == ".docx" or mime == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return "docx"
    if mime.startswith("image/") or ext in IMAGE_EXTS:
        return "image"
    if (mime.startswith("audio/") or mime.startswith("video/")
            or ext in AV_EXTS):
        return "av"
    if mime.startswith("text/") or ext in TEXT_EXTS:
        return "text"
    return "unknown"


def _merge_meta(con, artifact_id, updates):
    row = con.execute("SELECT meta_json FROM artifact WHERE id=?",
                      (artifact_id,)).fetchone()
    try:
        meta = json.loads(row[0]) if row and row[0] else {}
    except (ValueError, TypeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update(updates)
    with con:
        con.execute("UPDATE artifact SET meta_json=? WHERE id=?",
                    (json.dumps(meta), artifact_id))


def _store_chunks(con, artifact_id, chunks, provenance=None):
    """chunks: list of (page_no, text) or (page_no, text, prov_dict).
    Replaces any previous chunks.

    provenance: default dict with revision_id, backend, method,
    source_sha256, confidence. Per-chunk prov_dict overrides the defaults,
    so hybrid extractions (e.g. embedded text + OCR pages) can record
    each chunk's true origin.
    """
    prov = provenance or {}
    now = time.time()
    with con:
        con.execute("DELETE FROM extracted_chunk WHERE artifact_id=?",
                    (artifact_id,))
        for i, chunk in enumerate(chunks[:MAX_CHUNKS]):
            if len(chunk) == 3:
                page_no, text, override = chunk
                p = dict(prov); p.update(override or {})
            else:
                page_no, text = chunk
                p = prov
            con.execute(
                "INSERT INTO extracted_chunk(artifact_id, chunk_no, page_no, text,"
                " revision_id, backend, method, source_sha256, extracted_at,"
                " confidence) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, i, page_no or "", text or "",
                 p.get("revision_id"), p.get("backend", ""),
                 p.get("method", ""), p.get("source_sha256", ""),
                 now, p.get("confidence")))


def _chunk_text_lines(text):
    lines = text.splitlines()
    chunks = []
    for i in range(0, len(lines), LINES_PER_CHUNK):
        piece = lines[i:i + LINES_PER_CHUNK]
        chunks.append(("L%d-%d" % (i + 1, i + len(piece)), "\n".join(piece)))
    return chunks


def _sync_fts(con, artifact_id):
    row = con.execute("SELECT filename FROM artifact WHERE id=?",
                      (artifact_id,)).fetchone()
    title = row[0] if row else ""
    rows = con.execute(
        "SELECT text FROM extracted_chunk WHERE artifact_id=? ORDER BY chunk_no",
        (artifact_id,)).fetchall()
    body = "\n\n".join(r[0] for r in rows)[:FTS_BODY_CHARS]
    con.execute("DELETE FROM search_fts WHERE doc_type='artifact' AND doc_id=?",
                (artifact_id,))
    con.execute("INSERT INTO search_fts(doc_type, doc_id, title, body)"
                " VALUES(?,?,?,?)", ("artifact", artifact_id, title or "", body))


def _as_temp_file(con, data_dir, artifact):
    """Return a real filesystem path for the bytes.

    Legacy artifacts already live at source_ref; blob-store bytes go to a
    temp file. Returns (path, cleanup_fn). Never modifies the original.
    """
    ref = artifact.get("source_ref")
    if artifact.get("source") == "legacy" and ref and os.path.isfile(ref):
        return ref, (lambda: None)
    data = store.read_artifact_bytes(con, data_dir, artifact)
    import tempfile
    fd, path = tempfile.mkstemp(prefix="substrate-extract-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path, (lambda p=path: os.path.exists(p) and os.remove(p))


def _provenance_for(con, artifact, backend, method):
    """Build the provenance record for chunks extracted from this artifact."""
    rev = con.execute(
        "SELECT id FROM artifact_revision WHERE artifact_id=? ORDER BY rev_no DESC LIMIT 1",
        (artifact["id"],)).fetchone()
    return {"revision_id": rev[0] if rev else None,
            "backend": backend,
            "method": method,
            "source_sha256": artifact.get("blob_sha256", "")}


# ---------------------------------------------------------------- extractors

def _extract_text(con, data_dir, artifact):
    data = store.read_artifact_bytes(con, data_dir, artifact)
    text = data.decode("utf-8", errors="replace")
    _store_chunks(con, artifact["id"], _chunk_text_lines(text),
                  _provenance_for(con, artifact, "substrate-text", "plain"))
    _sync_fts(con, artifact["id"])
    return "done", ""


def _extract_zip(con, data_dir, artifact):
    # bound: don't materialize enormous archives into memory
    if (artifact.get("size") or 0) > 500 * 1024 * 1024:
        return "failed", "zip exceeds 500MB extraction bound"
    data = store.read_artifact_bytes(con, data_dir, artifact)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return "failed", "not a valid zip: %s" % e
    lines = []
    total_uncompressed = 0
    for info in zf.infolist():
        # zip-bomb guard: bound total uncompressed size we will enumerate
        total_uncompressed += info.file_size
        if total_uncompressed > 2 * 1024 * 1024 * 1024:
            lines.append("... [truncated: archive exceeds 2GB uncompressed bound]")
            break
        lines.append("%s  (%d bytes%s)" % (
            info.filename, info.file_size,
            ", dir" if info.is_dir() else ""))
    if zf.comment:
        lines.insert(0, "zip comment: %s" % zf.comment.decode("utf-8", "replace"))
    _store_chunks(con, artifact["id"],
                  [("zip", "\n".join(lines) or "(empty archive)")],
                  _provenance_for(con, artifact, "substrate-zip", "listing"))
    _sync_fts(con, artifact["id"])
    return "done", ""


def _ocr_image_file(img_path):
    """OCR a single image file with tesseract. Returns (text, version, err)."""
    try:
        ver = subprocess.run(["tesseract", "--version"], capture_output=True,
                             timeout=10).stdout.decode(
                                 "utf-8", "replace").strip().split("\n")[0]
    except Exception:
        ver = "tesseract"
    try:
        proc = subprocess.run(
            ["tesseract", img_path, "stdout", "-l", "eng", "--psm", "3"],
            capture_output=True, timeout=300)
        if proc.returncode != 0:
            return None, ver, "tesseract failed: %s" % (
                proc.stderr.decode("utf-8", "replace")[:200])
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        return (text if text else None), ver, ""
    except subprocess.TimeoutExpired:
        return None, ver, "ocr timed out"
    except Exception as e:
        return None, ver, "ocr error: %s" % e


def _extract_image(con, data_dir, artifact):
    if not _PIL:
        return "unavailable", "PIL not installed"
    data = store.read_artifact_bytes(con, data_dir, artifact)
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        return "failed", "could not decode image: %s" % e
    info = {"width": im.width, "height": im.height,
            "format": im.format or "", "mode": im.mode}
    exif = {}
    try:
        raw = im.getexif()
        if raw:
            tags = {v: k for k, v in ExifTags.TAGS.items()}
            for wanted in ("Make", "Model", "DateTime", "DateTimeOriginal",
                           "Orientation", "ISOSpeedRatings"):
                tid = tags.get(wanted)
                if tid in raw:
                    v = raw[tid]
                    exif[wanted] = v if isinstance(v, (str, int, float)) else str(v)
    except Exception:
        pass
    if exif:
        info["exif"] = exif
    _merge_meta(con, artifact["id"], {"image": info})

    # OCR: extract text from the image when tesseract is available.
    # The original bytes are never modified; OCR text is derived data.
    ocr_text, ocr_backend, ocr_err = None, "", ""
    if shutil.which("tesseract"):
        # decompression-bomb guard before handing bytes to tesseract
        if im.width * im.height > 100_000_000:
            ocr_err = "image exceeds 100MP safety bound; OCR skipped"
        else:
            import tempfile
            fd, tmppath = tempfile.mkstemp(suffix=".png",
                                           prefix="substrate-ocr-")
            try:
                os.close(fd)
                im.save(tmppath, "PNG")
                ocr_text, ocr_backend, ocr_err = _ocr_image_file(tmppath)
            finally:
                try:
                    os.remove(tmppath)
                except OSError:
                    pass
    else:
        ocr_err = "tesseract not installed"

    prov = _provenance_for(con, artifact, "substrate-image", "metadata")
    if ocr_text:
        _store_chunks(
            con, artifact["id"],
            [("ocr", ocr_text,
              {"backend": ocr_backend or "tesseract", "method": "ocr"})],
            prov)
        info["ocr"] = {"backend": ocr_backend, "chars": len(ocr_text)}
    elif ocr_err:
        _store_chunks(con, artifact["id"], [], prov)
        info["ocr"] = {"error": ocr_err}
    else:
        _store_chunks(con, artifact["id"], [], prov)
    _merge_meta(con, artifact["id"], {"image": info})

    # Thumbnail: a NEW derived artifact; the original is never overwritten.
    try:
        thumb = im.copy()
        thumb.thumbnail((320, 320), Image.LANCZOS)
        if thumb.mode in ("RGBA", "LA", "P"):
            thumb = thumb.convert("RGB")
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=85)
        base = os.path.splitext(artifact["filename"])[0] or "image"
        tart = store.register_artifact(
            con, data_dir, data=buf.getvalue(),
            filename="%s-thumb.jpg" % base, mime="image/jpeg",
            visibility=artifact["visibility"], source="generated",
            source_ref=artifact["id"],
            meta={"thumbnail_of": artifact["id"],
                  "generated_by": "substrate-extract",
                  "thumb_max_px": 320},
            enqueue_extract=False)
        _model.add_edge(con, "artifact", tart["id"], "artifact",
                        artifact["id"], "derives_from",
                        note="320px JPEG thumbnail generated by extraction pipeline",
                        provenance="explicit", inferred=False)
    except Exception as e:
        return "failed", "thumbnail generation failed: %s" % e

    _sync_fts(con, artifact["id"])
    return "done", ""


def _pdf_page_count(path):
    """Return page count via pdfinfo, or None if unavailable/unreadable."""
    if not shutil.which("pdfinfo"):
        return None
    try:
        proc = subprocess.run(["pdfinfo", path], capture_output=True,
                              timeout=30)
        if proc.returncode != 0:
            return None
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return None


def _pdftotext_page(path, pageno):
    """Extract embedded text for a single page. Returns text or None on error."""
    try:
        proc = subprocess.run(
            ["pdftotext", "-layout", "-f", str(pageno), "-l", str(pageno),
             path, "-"],
            capture_output=True, timeout=120)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None


def _has_usable_text(text, min_chars=20):
    """Heuristic: a page has usable embedded text if it contains enough
    alphanumeric characters. Avoids OCRing pages that already have text,
    while treating near-empty pages as image-only."""
    if not text:
        return False
    alnum = sum(1 for c in text if c.isalnum())
    return alnum >= min_chars


def _ocr_pdf_page(path, pageno, dpi=200):
    """Render a PDF page with pdftoppm and OCR with tesseract.
    Returns (text, confidence_hint) or (None, error)."""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="substrate-ocr-")
    try:
        prefix = os.path.join(tmpdir, "page")
        proc = subprocess.run(
            ["pdftoppm", "-r", str(dpi), "-f", str(pageno), "-l", str(pageno),
             "-png", "-singlefile", path, prefix],
            capture_output=True, timeout=180)
        if proc.returncode != 0:
            return None, "pdftoppm failed"
        img = prefix + ".png"
        if not os.path.isfile(img):
            return None, "pdftoppm produced no image"
        # bound image dimensions for tesseract sanity
        if _PIL:
            try:
                with Image.open(img) as im:
                    if im.width * im.height > 100_000_000:  # 100MP cap
                        return None, "page image exceeds 100MP safety bound"
            except Exception:
                pass
        proc = subprocess.run(
            ["tesseract", img, "stdout", "-l", "eng", "--psm", "3"],
            capture_output=True, timeout=300)
        if proc.returncode != 0:
            return None, "tesseract failed: %s" % (
                proc.stderr.decode("utf-8", "replace")[:200])
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        return (text if text else None), ""
    except subprocess.TimeoutExpired:
        return None, "ocr timed out"
    except Exception as e:
        return None, "ocr error: %s" % e
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_pdf(con, data_dir, artifact):
    """Hybrid PDF extraction.

    1. Extract embedded text per page with pdftotext.
    2. Pages without usable embedded text are rendered (pdftoppm) and OCRed
       (tesseract) — but ONLY those pages, and only if both tools exist.
    3. Every chunk records its true origin: embedded-text vs ocr, with the
       backend name/version. Missing tools produce an honest 'unavailable'
       state naming the missing dependency, never fake text.
    """
    if not shutil.which("pdftotext"):
        return "unavailable", "no pdf toolchain (install poppler-utils)"
    try:
        backend_ver = subprocess.run(
            ["pdftotext", "-v"], capture_output=True,
            timeout=10).stderr.decode("utf-8", "replace").strip().split("\n")[0]
    except Exception:
        backend_ver = "pdftotext"
    has_ocr = bool(shutil.which("tesseract") and shutil.which("pdftoppm"))
    ocr_ver = ""
    if has_ocr:
        try:
            ocr_ver = subprocess.run(
                ["tesseract", "--version"], capture_output=True,
                timeout=10).stdout.decode("utf-8", "replace").strip().split(
                    "\n")[0]
        except Exception:
            ocr_ver = "tesseract"

    path, cleanup = _as_temp_file(con, data_dir, artifact)
    base_prov = _provenance_for(con, artifact, backend_ver, "embedded-text")
    try:
        npages = _pdf_page_count(path)
        if npages is None:
            return "failed", "could not read PDF structure (pdfinfo failed)"
        if npages > 2000:
            return "failed", "PDF has %d pages; exceeds 2000-page safety bound" % npages
        if npages == 0:
            _store_chunks(con, artifact["id"], [], base_prov)
            _sync_fts(con, artifact["id"])
            _merge_meta(con, artifact["id"], {"pdf": {"pages": 0,
                                                      "note": "empty PDF"}})
            return "done", ""

        chunks = []          # (page_no, text, prov_override)
        ocr_done, ocr_pages = 0, []
        embedded_pages, empty_pages = 0, 0
        for pageno in range(1, npages + 1):
            text = _pdftotext_page(path, pageno)
            if text is None:
                # pdftotext failed on this page; try OCR as fallback
                ocr_pages.append(pageno)
                continue
            if _has_usable_text(text):
                chunks.append(("p%d" % pageno, text.strip("\n"), None))
                embedded_pages += 1
            else:
                ocr_pages.append(pageno)

        ocr_unavailable = []
        for pageno in sorted(ocr_pages):
            if not has_ocr:
                ocr_unavailable.append(pageno)
                continue
            text, err = _ocr_pdf_page(path, pageno)
            if text:
                chunks.append((
                    "p%d" % pageno, text,
                    {"backend": ocr_ver or "tesseract", "method": "ocr"}))
                ocr_done += 1
            else:
                empty_pages += 1
                if err:
                    chunks.append((
                        "p%d" % pageno,
                        "[page %d: OCR failed: %s]" % (pageno, err),
                        {"backend": ocr_ver or "tesseract",
                         "method": "ocr-failed"}))
        # order chunks by page number for stable retrieval
        chunks.sort(key=lambda c: int(c[0][1:].split("-")[0]))
        _store_chunks(con, artifact["id"], chunks, base_prov)
        _sync_fts(con, artifact["id"])
        _merge_meta(con, artifact["id"], {"pdf": {
            "pages": npages,
            "embedded_text_pages": embedded_pages,
            "ocr_pages": ocr_done,
            "ocr_unavailable_pages": ocr_unavailable,
            "unreadable_pages": empty_pages,
            "backend": backend_ver,
            "ocr_backend": ocr_ver or None,
        }})
        return "done", ""
    finally:
        cleanup()


def _extract_av(con, data_dir, artifact):
    # No transcription backend is configured. Record that honestly; the
    # artifact itself is untouched and no fake transcript is produced.
    _merge_meta(con, artifact["id"], {
        "extraction_note":
        "transcription unavailable: no transcription backend configured"})
    _store_chunks(con, artifact["id"], [],
                  _provenance_for(con, artifact, "none", "unsupported"))
    _sync_fts(con, artifact["id"])
    return "done", ""


def _extract_docx(con, data_dir, artifact):
    """Extract text from DOCX using stdlib zipfile + XML parsing.
    Paragraphs become page units; tables are flattened with cell separators."""
    import xml.etree.ElementTree as ET
    data = store.read_artifact_bytes(con, data_dir, artifact)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return "failed", "not a valid docx/zip: %s" % e
    try:
        xml_bytes = zf.read("word/document.xml")
    except KeyError:
        return "failed", "docx missing word/document.xml"
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        return "failed", "docx XML parse error: %s" % e
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paras = []
    for p in root.iterfind(".//w:p", ns):
        texts = [t.text for t in p.iterfind(".//w:t", ns) if t.text]
        para = "".join(texts).strip()
        if para:
            paras.append(para)
    if not paras:
        _store_chunks(con, artifact["id"], [],
                      _provenance_for(con, artifact, "substrate-docx",
                                      "ooxml-text"))
        _sync_fts(con, artifact["id"])
        _merge_meta(con, artifact["id"], {"docx": {"paragraphs": 0,
                                                   "note": "no text found"}})
        return "done", ""
    # chunk by paragraph groups to keep chunks reasonably sized
    chunks, buf, start = [], [], 1
    for i, para in enumerate(paras, 1):
        buf.append(para)
        if len(buf) >= 20 or i == len(paras):
            chunks.append(("para%d-%d" % (start, i), "\n".join(buf)))
            buf, start = [], i + 1
    _store_chunks(con, artifact["id"], chunks,
                  _provenance_for(con, artifact, "substrate-docx",
                                  "ooxml-text"))
    _sync_fts(con, artifact["id"])
    _merge_meta(con, artifact["id"], {"docx": {"paragraphs": len(paras),
                                               "backend": "substrate-docx (stdlib)"}})
    return "done", ""


def _extract_unknown(con, data_dir, artifact):
    _store_chunks(con, artifact["id"], [],
                  _provenance_for(con, artifact, "none", "unsupported"))
    _sync_fts(con, artifact["id"])
    return "done", ""


_EXTRACTORS = {
    "text": _extract_text,
    "zip": _extract_zip,
    "image": _extract_image,
    "pdf": _extract_pdf,
    "docx": _extract_docx,
    "av": _extract_av,
    "unknown": _extract_unknown,
}


# ---------------------------------------------------------------- drain

def _process_job(con, data_dir, job):
    jid = job["id"]
    aid = job["artifact_id"]
    now = time.time()
    # Atomic claim: only one worker can transition pending->processing.
    # Prevents two workers from processing the same job concurrently.
    with con:
        cur = con.execute(
            "UPDATE extract_job SET status='processing', attempts=attempts+1,"
            " updated_at=? WHERE id=? AND status='pending'", (now, jid))
        if cur.rowcount == 0:
            return None  # claimed by another worker (or no longer pending)
    status, error = "done", ""
    try:
        artifact = store.get_artifact(con, aid)
        if not artifact:
            status, error = "failed", "artifact %s no longer exists" % aid
        else:
            kind = _classify(artifact.get("mime"), artifact.get("filename"))
            status, error = _EXTRACTORS[kind](con, data_dir, artifact)
    except Exception as e:  # extraction failure must not invalidate the artifact
        status, error = "failed", "extractor exception: %s: %s" % (
            type(e).__name__, e)
    error = (error or "")[:2000]
    # Permanent vs transient: 'unavailable' (missing dependency) stays
    # retryable; 'failed' after MAX_ATTEMPTS is terminal unless manually retried.
    attempts = con.execute("SELECT attempts FROM extract_job WHERE id=?",
                           (jid,)).fetchone()[0]
    if status == "failed" and attempts >= MAX_ATTEMPTS:
        error = "%s [gave up after %d attempts]" % (error, attempts)
    with con:
        con.execute("UPDATE extract_job SET status=?, last_error=?, updated_at=?"
                    " WHERE id=?", (status, error, time.time(), jid))
    return {"job_id": jid, "artifact_id": aid, "status": status, "error": error}


def reap_stale_jobs(con, data_dir=None, stale_after=3600):
    """Reset 'processing' jobs abandoned by crashed workers back to 'pending'.

    A job stuck in 'processing' longer than stale_after seconds is assumed
    orphaned (worker died). Returns the number of jobs reaped.
    """
    cutoff = time.time() - stale_after
    with con:
        cur = con.execute(
            "UPDATE extract_job SET status='pending', updated_at=? "
            "WHERE status='processing' AND updated_at < ?",
            (time.time(), cutoff))
        return cur.rowcount


def drain_once(con, data_dir, limit=5):
    """Process up to `limit` pending extraction jobs, oldest first.

    Jobs are claimed atomically; returns a list of
    {job_id, artifact_id, status, error} (skips jobs claimed elsewhere).
    """
    # opportunistically reap stale jobs so crashed workers don't wedge the queue
    reap_stale_jobs(con)
    rows = con.execute(
        "SELECT * FROM extract_job WHERE status='pending'"
        " ORDER BY created_at LIMIT ?", (limit,)).fetchall()
    results = []
    for r in rows:
        res = _process_job(con, data_dir, dict(r))
        if res is not None:
            results.append(res)
    return results
