"""
Cloud version of transcript-app.

Same API surface as ../app.py, but transcription runs on Groq's
whisper-large-v3-turbo instead of a local faster-whisper model.
Designed to run on Render's free tier as a fallback for when the
local PC is off.
"""

import os
import re
import shutil
import threading
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

APP_DIR = Path(__file__).parent
REPO_DIR = APP_DIR.parent
STATIC_DIR = REPO_DIR / "static"
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# 25 MB is Groq's per-file limit for whisper.
MAX_FILE_BYTES = 25 * 1024 * 1024

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


jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


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
                "file": (file_path.name, f.read()),
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
        update_job(job_id, status="error", error=f"Groq error: {e}")
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


def _yt_creds_configured() -> bool:
    # On a headless server only a cookies file + proxy can help YouTube; there
    # is no browser to read cookies from.
    return bool(
        os.environ.get("YT_COOKIES_FILE", "").strip()
        or os.environ.get("YT_PROXY", "").strip()
    )


def _safe_err(e) -> str:
    # Redact userinfo (e.g. a proxy's user:pass@) so a configured YT_PROXY
    # credential can't leak into job['error'], which /status returns to anyone.
    return re.sub(r"//[^/@\s]+@", "//***@", str(e))


def _yt_extra_opts():
    """Optional cookie/proxy hooks (YouTube only — gated by _is_youtube at the
    call site since these are GLOBAL yt-dlp opts). Off unless the env var is set.
    On a datacenter IP YouTube needs cookies + a RESIDENTIAL proxy and still
    often fails — these are best-effort hooks."""
    extra = {}
    cookiefile = os.environ.get("YT_COOKIES_FILE", "").strip()
    if cookiefile and Path(cookiefile).exists():
        extra["cookiefile"] = cookiefile
    proxy = os.environ.get("YT_PROXY", "").strip()
    if proxy:
        extra["proxy"] = proxy
    if extra:  # cookies/proxy present -> use a cookie-compatible YT client
        extra["extractor_args"] = {"youtube": {"player_client": ["web_safari", "default"]}}
    return extra


def run_url_job(job_id: str, url: str, language: str | None):
    try:
        update_job(job_id, status="downloading", progress=0.0)

        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                if total:
                    # Download is roughly first half of work; transcribe is fast.
                    update_job(job_id, progress=min(done / total * 0.5, 0.49))

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(UPLOAD_DIR / f"{job_id}.%(ext)s"),
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
        }
        if _is_youtube(url):  # YT_* opts are global yt-dlp opts; YouTube only
            ydl_opts.update(_yt_extra_opts())

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))
            title = info.get("title")
            if title:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["source_title"] = title

        if file_path.stat().st_size > MAX_FILE_BYTES:
            update_job(
                job_id,
                status="error",
                error="File >25MB — too large for cloud transcription. Run on your local PC.",
            )
            file_path.unlink(missing_ok=True)
            return

        transcribe_with_groq(job_id, file_path, language)
    except yt_dlp.utils.DownloadError as e:
        msg = _safe_err(e)
        ml = msg.lower()
        if "sign in to confirm" in ml or "not a bot" in ml:
            msg = (
                "YouTube blocked our cloud server (bot check). Transcribe YouTube "
                "on your PC instead — other sites work fine here."
            )
        elif "login" in ml or "private" in ml:
            msg = "This video is private or requires login."
        elif "rate" in ml or "429" in msg:
            msg = "Rate-limited by the site. Try again in a minute."
        update_job(job_id, status="error", error=msg)
    except Exception as e:
        update_job(job_id, status="error", error=f"Download failed: {_safe_err(e)}")


@app.post("/transcribe-url")
def transcribe_url(payload: dict = Body(...)):
    url = (payload.get("url") or "").strip()
    language = payload.get("language") or "auto"
    if not url:
        raise HTTPException(400, "No URL provided")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    # YouTube hard-blocks our datacenter IP. Without cookies+proxy configured,
    # fail fast with a clear message instead of a cryptic bot-check after a wait.
    if _is_youtube(url) and not _yt_creds_configured():
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
    threading.Thread(
        target=run_url_job, args=(job_id, url, language), daemon=True
    ).start()
    return {"job_id": job_id}


@app.post("/transcribe")
def transcribe(file: UploadFile = File(...), language: str = "auto"):
    if not file.filename:
        raise HTTPException(400, "No file provided")

    job_id = uuid.uuid4().hex
    ext = Path(file.filename).suffix or ".bin"
    saved_path = UPLOAD_DIR / f"{job_id}{ext}"

    with saved_path.open("wb") as out:
        shutil.copyfileobj(file.file, out, length=1024 * 1024)

    if saved_path.stat().st_size > MAX_FILE_BYTES:
        saved_path.unlink(missing_ok=True)
        raise HTTPException(413, "File >25MB — too large for cloud. Run on your local PC.")

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": file.filename,
            "created": datetime.utcnow().isoformat(),
        }

    threading.Thread(
        target=transcribe_with_groq, args=(job_id, saved_path, language), daemon=True
    ).start()
    return {"job_id": job_id}


@app.get("/status/{job_id}")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/config")
def config():
    return {"ai_enabled": anthropic_client is not None, "cloud": True}


@app.post("/summarize")
def summarize(payload: dict = Body(...)):
    if not anthropic_client:
        raise HTTPException(503, "ANTHROPIC_API_KEY not configured")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "No text provided")
    instruction = (
        payload.get("instruction")
        or "Summarize this transcript in 2-3 sentences, then list the key points as bullets. If there are any clear action items, list them at the end under 'Action items:'. Keep it concise."
    ).strip()

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
