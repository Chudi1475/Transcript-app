"""
Cloud version of transcript-app.

Same API surface as ../app.py, but transcription runs on Groq's
whisper-large-v3-turbo instead of a local faster-whisper model.
Designed to run on Render's free tier as a fallback for when the
local PC is off.
"""

import os
import re
import threading
import time
import urllib.parse
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from groq import Groq

import yt_dlp

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
# large-v3 (not -turbo) for max accuracy. Cloud's slow part is the cold start,
# not the model, so the small speed cost over turbo is worth the better words.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
anthropic_client = (
    Anthropic(api_key=ANTHROPIC_API_KEY)
    if (Anthropic and ANTHROPIC_API_KEY)
    else None
)
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-6")

# Optional shared-secret for /summarize (the one endpoint that spends real
# Anthropic credits). Set APP_KEY in the Render dashboard to enable; unset
# means no gate, so local/dev keeps working key-less.
APP_KEY = os.environ.get("APP_KEY", "").strip()

APP_DIR = Path(__file__).parent
REPO_DIR = APP_DIR.parent
STATIC_DIR = REPO_DIR / "static"
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# 25 MB is Groq's per-file limit for whisper.
MAX_FILE_BYTES = 25 * 1024 * 1024

# Cheap global abuse brake: this app has ONE real user, so a global cap needs
# no per-IP bookkeeping (Render's proxy hides real client IPs anyway) and
# can't be dodged by IP rotation. Worst case under attack the owner waits
# <1h instead of losing a whole day of Groq quota.
RATE_WINDOW_SEC = 3600
RATE_MAX_JOBS = 30
_rate_times = deque()
_rate_lock = threading.Lock()


def _rate_limit_or_429():
    now = time.time()
    with _rate_lock:
        while _rate_times and now - _rate_times[0] > RATE_WINDOW_SEC:
            _rate_times.popleft()
        if len(_rate_times) >= RATE_MAX_JOBS:
            raise HTTPException(429, "Too many jobs this hour — try again later.")
        _rate_times.append(now)


# Bounded workers so a request burst can't spawn unbounded threads on the
# 512MB dyno. 2 is plenty for one user; excess jobs just queue.
executor = ThreadPoolExecutor(max_workers=2)

app = FastAPI()

# Permissive CORS so the extension (or any page) can probe /healthz
# from any origin without the browser fussing over preflight.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_cache_for_static(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path == "/config":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def reject_oversized_uploads(request, call_next):
    # Fail a >25MB upload from the Content-Length header, BEFORE the body is
    # read/spooled to disk — instant 413 instead of uploading the whole file
    # only to be rejected after.
    if request.method == "POST" and request.url.path == "/transcribe":
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > MAX_FILE_BYTES + 1024 * 1024:  # +1MB multipart framing slack
            return JSONResponse(
                {"detail": "File >25MB — too large for cloud. Run on your local PC."},
                status_code=413,
            )
    return await call_next(request)


jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

MAX_JOBS = 50  # single-user app; bounds jobs-dict memory on the 512MB dyno


def _prune_jobs_locked():
    """Call with jobs_lock held. Evicts oldest FINISHED jobs beyond the cap —
    never in-flight ones, so an active /status poll can't 404."""
    if len(jobs) <= MAX_JOBS:
        return
    for jid, j in sorted(jobs.items(), key=lambda kv: kv[1].get("created", "")):
        if len(jobs) <= MAX_JOBS:
            break
        if j.get("status") in ("done", "error"):
            del jobs[jid]


def update_job(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def transcribe_with_groq(job_id: str, file_path: Path, language: str | None):
    try:
        if not groq_client:
            raise RuntimeError("GROQ_API_KEY env var is not set on the server")

        update_job(job_id, status="transcribing", progress=0.6)

        with file_path.open("rb") as f:
            kwargs = {
                # handle, not f.read() — stream the upload instead of holding
                # up to 25MB per concurrent job in RAM on the 512MB dyno
                "file": (file_path.name, f),
                "model": GROQ_MODEL,
                "response_format": "verbose_json",
                "temperature": 0.0,
            }
            if language and language != "auto":
                kwargs["language"] = language
            result = groq_client.audio.transcriptions.create(**kwargs)

        # The Groq SDK returns a pydantic-ish object whose `segments` is a
        # list of dicts (start/end/text plus extras we don't need).
        raw_segments = getattr(result, "segments", None) or []
        segments = []
        for seg in raw_segments:
            if isinstance(seg, dict):
                segments.append(
                    {
                        "start": seg.get("start"),
                        "end": seg.get("end"),
                        "text": seg.get("text", ""),
                    }
                )
            else:
                segments.append(
                    {
                        "start": getattr(seg, "start", None),
                        "end": getattr(seg, "end", None),
                        "text": getattr(seg, "text", ""),
                    }
                )

        update_job(
            job_id,
            status="done",
            progress=1.0,
            text=(result.text or "").strip(),
            segments=segments,
            language=getattr(result, "language", language or "auto"),
            duration=getattr(result, "duration", None),
        )
    except Exception as e:
        update_job(job_id, status="error", error=f"Groq error: {_safe_err(e)}")
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


def _is_youtube(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return any(
        host == d or host.endswith("." + d)
        for d in ("youtube.com", "youtu.be", "youtube-nocookie.com")
    )


def _is_instagram(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return host == "instagram.com" or host.endswith(".instagram.com")


def _cookiefile() -> str:
    """General cookies file (Netscape cookies.txt). yt-dlp scopes cookies by
    domain, so a single file can safely hold YouTube + Instagram + any other
    site. New COOKIES_FILE wins; YT_COOKIES_FILE kept for back-compat."""
    for var in ("COOKIES_FILE", "YT_COOKIES_FILE"):
        p = os.environ.get(var, "").strip()
        if p:
            if Path(p).exists():
                return p
            print(f"WARNING: {var}={p} does not exist; ignoring")  # Render logs
    return ""


def _proxy() -> str:
    for var in ("PROXY_URL", "YT_PROXY"):
        p = os.environ.get(var, "").strip()
        if p:
            return p
    return ""


def _creds_configured() -> bool:
    # A cookies file and/or (residential) proxy is the only thing that gets a
    # headless datacenter server past YouTube/Instagram's IP + login gates.
    return bool(_cookiefile() or _proxy())


def _safe_err(e) -> str:
    # Redact userinfo (e.g. a proxy's user:pass@) so a configured proxy
    # credential can't leak into job['error'], which /status returns to anyone.
    # Also collapse server paths so infra layout stays out of error text.
    msg = re.sub(r"//[^/@\s]+@", "//***@", str(e))
    return msg.replace(str(UPLOAD_DIR), "uploads")


def _extra_opts(url: str) -> dict:
    """Cookie/proxy hooks applied to EVERY download (not just YouTube). Cookies
    are domain-scoped by yt-dlp, so sharing one file across sites is safe — the
    IG cookies only ever go to instagram.com, the YT ones to youtube.com, etc.
    YouTube additionally needs a cookie-compatible player client. Off unless the
    env var is set; on a datacenter IP these are best-effort and can still fail."""
    extra = {}
    cf = _cookiefile()
    if cf:
        extra["cookiefile"] = cf
    px = _proxy()
    if px:
        extra["proxy"] = px
    if _is_youtube(url) and (cf or px):
        extra["extractor_args"] = {"youtube": {"player_client": ["web_safari", "default"]}}
    return extra


class FileTooBig(Exception):
    """Raised from the progress hook to abort a download past the Groq cap."""


TOO_BIG_MSG = "File >25MB — too large for cloud transcription. Run on your local PC."


def _cleanup_job_files(job_id: str):
    """Remove whatever a failed/aborted download left behind (incl. .part)."""
    for p in UPLOAD_DIR.glob(f"{job_id}.*"):
        try:
            p.unlink()
        except Exception:
            pass


def run_url_job(job_id: str, url: str, language: str | None):
    try:
        update_job(job_id, status="downloading", progress=0.0)

        def progress_hook(d):
            done = d.get("downloaded_bytes", 0) or 0
            if done > MAX_FILE_BYTES:
                raise FileTooBig()  # >25MB can't go to Groq — stop wasting bandwidth
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    # Download is roughly first half of work; transcribe is fast.
                    update_job(job_id, progress=min(done / total * 0.5, 0.49))

        ydl_opts = {
            # Smallest useful audio first: whisper transcribes speech fine at
            # ~64kbps, and half the bitrate = double the duration that fits
            # under Groq's 25MB cap (plus faster download AND upload). The <=?
            # includes formats with unknown abr (IG often doesn't report it).
            "format": "ba[abr<=?64]/ba/b",
            "outtmpl": str(UPLOAD_DIR / f"{job_id}.%(ext)s"),
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "playlist_items": "1",  # a pure playlist URL -> just the first video
            "max_filesize": MAX_FILE_BYTES,  # skip formats known too big upfront
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 30,
        }
        ydl_opts.update(_extra_opts(url))  # cookie/proxy hooks (any site), if set

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and info.get("entries"):  # pure playlist -> first entry
                entries = list(info["entries"] or [])
                if entries and entries[0]:
                    info = entries[0]
            file_path = Path(ydl.prepare_filename(info))
            if not file_path.is_file():
                # post-download fixup can change the ext — find it by job id
                cands = [p for p in UPLOAD_DIR.glob(f"{job_id}.*")
                         if not p.name.endswith(".part")]
                if cands:
                    file_path = cands[0]
            title = info.get("title")
            if title:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["source_title"] = title

        if file_path.stat().st_size > MAX_FILE_BYTES:
            update_job(job_id, status="error", error=TOO_BIG_MSG)
            _cleanup_job_files(job_id)
            return

        transcribe_with_groq(job_id, file_path, language)
    except FileTooBig:
        _cleanup_job_files(job_id)
        update_job(job_id, status="error", error=TOO_BIG_MSG)
    except yt_dlp.utils.DownloadError as e:
        _cleanup_job_files(job_id)
        msg = _safe_err(e)
        ml = msg.lower()
        ig = _is_instagram(url) or "[instagram]" in ml or "empty media response" in ml
        rate_limited = ("rate limit" in ml or "rate-limit" in ml
                        or "too many requests" in ml or "429" in msg)
        if "sign in to confirm" in ml or "not a bot" in ml:
            msg = (
                "YouTube blocked our cloud server (bot check). Transcribe YouTube "
                "on your PC instead — other sites work fine here."
            )
        elif "larger than max" in ml or "filetoobig" in ml:
            msg = TOO_BIG_MSG
        elif ig and ("empty media response" in ml or "logged-in" in ml
                     or "log in" in ml or "cookies" in ml or rate_limited):
            if _cookiefile():
                msg = (
                    "Instagram rejected the cloud server even with cookies — they've "
                    "likely expired or this post needs a fresh login. Refresh the "
                    "cookies file, or transcribe this reel on your PC."
                )
            else:
                msg = (
                    "Instagram is blocking our cloud server's IP (it wants a logged-in "
                    "session). Transcribe this reel on your PC, or add Instagram "
                    "cookies to the server — see the README."
                )
        elif "login" in ml or "private" in ml:
            msg = "This video is private or requires login."
        elif rate_limited:
            msg = "Rate-limited by the site. Try again in a minute."
        update_job(job_id, status="error", error=msg)
    except Exception as e:
        _cleanup_job_files(job_id)
        update_job(job_id, status="error", error=f"Download failed: {_safe_err(e)}")


@app.post("/transcribe-url")
def transcribe_url(payload: dict = Body(...)):
    url = (payload.get("url") or "").strip()
    language = payload.get("language") or "auto"
    if not url:
        raise HTTPException(400, "No URL provided")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    _rate_limit_or_429()

    # YouTube hard-blocks our datacenter IP. Without cookies+proxy configured,
    # fail fast with a clear message instead of a cryptic bot-check after a wait.
    if _is_youtube(url) and not _creds_configured():
        job_id = uuid.uuid4().hex
        with jobs_lock:
            jobs[job_id] = {
                "status": "error",
                "progress": 0.0,
                "filename": url,
                "source_url": url,
                "created": datetime.utcnow().isoformat(),
                "error": (
                    "YouTube blocks transcription from our cloud server's IP. Open "
                    "the app on your computer (with it running) to do YouTube — "
                    "TikTok, Instagram, X, Reddit and the rest work fine here."
                ),
            }
            _prune_jobs_locked()
        return {"job_id": job_id}

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": url,
            "source_url": url,
            "created": datetime.utcnow().isoformat(),
        }
        _prune_jobs_locked()
    executor.submit(run_url_job, job_id, url, language)
    return {"job_id": job_id}


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...), language: str = "auto"):
    if not file.filename:
        raise HTTPException(400, "No file provided")
    _rate_limit_or_429()

    job_id = uuid.uuid4().hex
    # keep only a sane extension — the filename is caller-controlled
    ext = re.sub(r"[^A-Za-z0-9.]", "", Path(file.filename).suffix)[:10] or ".bin"
    saved_path = UPLOAD_DIR / f"{job_id}{ext}"

    # Counted write: rejects >25MB mid-stream (backstop for chunked uploads the
    # Content-Length middleware can't see) and never strands a partial file —
    # cleanup runs on ANY failure, including a disk-full ENOSPC.
    written = 0
    try:
        with saved_path.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_FILE_BYTES:
                    raise HTTPException(413, "File >25MB — too large for cloud. Run on your local PC.")
                out.write(chunk)
    except BaseException:
        saved_path.unlink(missing_ok=True)
        raise

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": file.filename,
            "created": datetime.utcnow().isoformat(),
        }
        _prune_jobs_locked()

    executor.submit(transcribe_with_groq, job_id, saved_path, language)
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        # copy under the lock: don't serialize a dict a worker is mutating
        job = dict(job) if job else None
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/config")
def config():
    return {"ai_enabled": anthropic_client is not None, "cloud": True}


@app.post("/summarize")
def summarize(request: Request, payload: dict = Body(...)):
    # Optional shared-secret gate — this is the one endpoint that spends real
    # Anthropic credits, on a public URL. Only active when APP_KEY env is set.
    if APP_KEY and request.headers.get("x-app-key") != APP_KEY:
        raise HTTPException(401, "unauthorized")
    if not anthropic_client:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "No text provided")
    if len(text) > 60_000:  # cost cap even if the key leaks
        raise HTTPException(413, "Transcript too long to summarize")
    instruction = (
        payload.get("instruction")
        or "Summarize this transcript in 2-3 sentences, then list the key points as bullets. If there are any clear action items, list them at the end under 'Action items:'. Keep it concise."
    ).strip()[:1000]

    try:
        msg = anthropic_client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": f"{instruction}\n\nTranscript:\n{text}",
                }
            ],
        )
        return {"summary": msg.content[0].text}
    except Exception as e:
        raise HTTPException(500, f"Claude API error: {e}")


@app.get("/healthz")
def healthz():
    return {"ok": True, "cloud": True}


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
