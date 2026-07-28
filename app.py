import os
import re
import shutil
import socket
import sys
import sysconfig
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

if sys.platform == "win32":
    site_packages = Path(sysconfig.get_paths()["purelib"])
    for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
        dll_dir = site_packages / sub
        if dll_dir.exists():
            os.add_dll_directory(str(dll_dir))

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import BatchedInferencePipeline, WhisperModel

import yt_dlp

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
anthropic_client = (
    Anthropic(api_key=ANTHROPIC_API_KEY)
    if (Anthropic and ANTHROPIC_API_KEY)
    else None
)
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-6")

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# Accuracy first: large-v3 is Whisper's most accurate model. It won't fit in
# float16 on a small/shared GPU, so we load it int8_float16 (near-identical
# accuracy at ~half the VRAM) and walk down a fallback ladder if even that
# can't fit, so the app never fails to start.
MODEL_NAME = os.environ.get("WHISPER_MODEL", "large-v3")
DEVICE = os.environ.get("WHISPER_DEVICE")  # explicit override; else auto-ladder
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE")  # explicit override
BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))
# Sequential decoding for short clips: it catches slightly more words and
# yields fine-grained segments (clean timestamps/SRT). Batched decoding is
# faster but coarsens segments to ~30s chunks and catches a hair less — it
# engages automatically for long audio (see LONG_AUDIO_SEC below, where
# BATCH_SIZE matters), and WHISPER_BATCHED=1 forces it for every job.
USE_BATCHED = os.environ.get("WHISPER_BATCHED", "0") == "1"
BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "8"))

# Long uploads: the sequential beam-5 path is accuracy-king but runs near
# realtime on this GPU — a 30min file takes ~30min. Past this duration the job
# auto-switches to the batched pipeline at a greedy beam (several times
# realtime, slightly coarser segments). Short reels/clips keep the
# max-accuracy path unchanged.
LONG_AUDIO_SEC = float(os.environ.get("WHISPER_LONG_AUDIO_SEC", "480"))
FAST_BEAM_SIZE = int(os.environ.get("WHISPER_FAST_BEAM_SIZE", "1"))

# Sensitive VAD so quiet speech and words at chunk edges still get caught. Lower
# threshold = more eager to treat audio as speech; padding protects word edges.
VAD_PARAMS = {
    "threshold": float(os.environ.get("WHISPER_VAD_THRESHOLD", "0.2")),
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 400,
}


def _load_plan():
    """Ordered (model, device, compute) attempts — first that loads wins."""
    if DEVICE or COMPUTE_TYPE:
        dev = DEVICE or "cuda"
        comp = COMPUTE_TYPE or ("int8_float16" if dev != "cpu" else "int8")
        plan = [(MODEL_NAME, dev, comp)]
        if dev != "cpu":
            plan.append(("small.en", "cpu", "int8"))
        return plan
    return [
        (MODEL_NAME, "cuda", "int8_float16"),
        (MODEL_NAME, "cuda", "int8"),
        ("distil-large-v3", "cuda", "int8_float16"),
        ("small.en", "cuda", "int8_float16"),
        ("small.en", "cpu", "int8"),
    ]


def load_model():
    last_err = None
    for name, device, compute in _load_plan():
        try:
            print(f"Loading Whisper model: {name} ({device}, {compute})")
            kw = {"cpu_threads": os.cpu_count() or 4} if device == "cpu" else {}
            m = WhisperModel(name, device=device, compute_type=compute, **kw)
            print(f"Model loaded: {name} ({device}, {compute})")
            return m, name, device
        except Exception as e:
            last_err = e
            print(f"  load failed for {name} ({device}, {compute}): {e}")
    raise RuntimeError(f"Could not load any Whisper model. Last error: {last_err}")


model, ACTIVE_MODEL, ACTIVE_DEVICE = load_model()
# Wraps the same weights (no extra VRAM until used). WHISPER_BATCHED=1 forces
# it for every job; otherwise it kicks in automatically for long audio only
# (see LONG_AUDIO_SEC) so short clips keep fine-grained segments.
batched_model = (
    BatchedInferencePipeline(model=model) if ACTIVE_DEVICE != "cpu" else None
)
print(f"Active: {ACTIVE_MODEL} on {ACTIVE_DEVICE}, batched={batched_model is not None}")

# One ~4GB GPU: serialize transcription so two jobs can't OOM each other.
transcribe_lock = threading.Lock()

app = FastAPI()

# Permissive CORS, same as cloud: the extension's /healthz probe is a fetch
# from a content script on instagram.com etc., and MV3 content scripts are
# subject to the page's CORS — without this header the probe fails and the
# extension routes to cloud even when this PC is up.
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


def _transcribe(
    file_path: Path, language: str | None, batched: bool, beam_size: int | None = None
):
    kwargs = {
        "beam_size": beam_size or BEAM_SIZE,
        "vad_filter": True,
        "vad_parameters": dict(VAD_PARAMS),
    }
    if language and language != "auto":
        kwargs["language"] = language
    if batched and batched_model is not None:
        return batched_model.transcribe(str(file_path), batch_size=BATCH_SIZE, **kwargs)
    return model.transcribe(str(file_path), **kwargs)


def _drain(job_id: str, segments_iter, info):
    duration = info.duration or 1.0
    segs, text_parts = [], []
    for seg in segments_iter:
        segs.append({"start": seg.start, "end": seg.end, "text": seg.text})
        text_parts.append(seg.text)
        update_job(job_id, progress=min((seg.end or 0) / duration, 0.99))
    return segs, text_parts


def _is_oom(err: Exception) -> bool:
    s = str(err).lower()
    return any(k in s for k in ("out of memory", "cublas", "cudnn", "cuda failed"))


def _media_duration_sec(path: Path) -> float | None:
    """Container-metadata duration — no decode, cheap enough to run per job.
    PyAV is already a faster-whisper dependency."""
    try:
        import av

        with av.open(str(path)) as container:
            if container.duration:
                return container.duration / av.time_base
            for s in container.streams:
                if s.duration and s.time_base:
                    return float(s.duration * s.time_base)
    except Exception:
        return None
    return None


def run_transcription(job_id: str, file_path: Path, language: str | None):
    try:
        update_job(job_id, status="transcribing", progress=0.0)

        media_sec = _media_duration_sec(file_path)
        long_audio = media_sec is not None and media_sec >= LONG_AUDIO_SEC

        # Serialize GPU work so concurrent jobs can't OOM the 4GB card. If a
        # batched run runs out of VRAM, retry once sequentially (lower peak use).
        with transcribe_lock:
            use_batched = batched_model is not None and (USE_BATCHED or long_audio)
            beam = FAST_BEAM_SIZE if long_audio else None
            t0 = time.perf_counter()
            try:
                segments_iter, info = _transcribe(file_path, language, use_batched, beam)
                segs, text_parts = _drain(job_id, segments_iter, info)
            except Exception as e:
                if use_batched and _is_oom(e):
                    print(f"[{job_id}] batched OOM ({e}); retrying sequentially")
                    update_job(job_id, status="transcribing", progress=0.0)
                    use_batched = False
                    t0 = time.perf_counter()  # time the retry, not the dead attempt
                    segments_iter, info = _transcribe(file_path, language, False, beam)
                    segs, text_parts = _drain(job_id, segments_iter, info)
                else:
                    raise

        elapsed = time.perf_counter() - t0
        speed = (info.duration or 0) / max(elapsed, 0.001)
        print(
            f"[{job_id}] {info.duration or 0:.0f}s audio in {elapsed:.0f}s "
            f"({speed:.1f}x realtime, batched={use_batched}, beam={beam or BEAM_SIZE})"
        )

        update_job(
            job_id,
            status="done",
            progress=1.0,
            text="".join(text_parts).strip(),
            segments=segs,
            language=info.language,
            duration=info.duration,
        )
    except Exception as e:
        update_job(job_id, status="error", error=_safe_err(e))
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


def _is_tiktok(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return host == "tiktok.com" or host.endswith(".tiktok.com")


# Cap on tikwm direct downloads. Local whisper has no hard file limit like
# Groq's 25MB, but a cap keeps a runaway download from filling the disk.
TIKTOK_MAX_BYTES = 500 * 1024 * 1024
TIKTOK_UNAVAILABLE_MSG = (
    "Couldn't fetch this TikTok — it may be private, deleted, region-locked, or the "
    "link has expired. Double-check it opens in a browser, then try again."
)


def _tikwm_media(url: str) -> tuple[str, str | None, bool] | None:
    """Resolve a TikTok URL to a directly-downloadable, AUDIO-BEARING media URL
    via tikwm's public API. yt-dlp's cookieless web extractor can't resolve
    share links and hands whisper an audioless image for photo/slideshow posts;
    tikwm runs its own TikTok sessions and returns, for any IP:
      - regular video   -> `play`: no-watermark mp4 WITH the spoken audio
      - photo/slideshow  -> `play`==`music`: the voiceover/sound track (m4a)
    Returns (media_url, title, is_photo) or None if tikwm can't fetch it."""
    try:
        from curl_cffi import requests as _curl
    except Exception as e:
        print(f"curl_cffi unavailable, can't use tikwm: {e}")
        return None
    try:
        r = _curl.get("https://www.tikwm.com/api/", params={"url": url},
                      impersonate="chrome", timeout=20)
        d = r.json()
    except Exception as e:
        print(f"tikwm fetch error: {e}")
        return None
    if not isinstance(d, dict) or d.get("code") != 0:
        print(f"tikwm miss: {d.get('msg') if isinstance(d, dict) else d!r}")
        return None
    data = d.get("data") or {}
    is_photo = bool(data.get("images"))
    media = data.get("play") or data.get("music") or data.get("wmplay")
    if not media:
        return None
    if media.startswith("/"):
        media = "https://www.tikwm.com" + media
    return media, (data.get("title") or None), is_photo


def _download_capped(media_url, dest: Path, max_bytes: int, on_progress=None) -> int:
    """Stream a direct media URL to `dest`, aborting past `max_bytes`. curl_cffi's
    chrome fingerprint keeps TikTok's CDN from 403-ing a bare client."""
    from curl_cffi import requests as _curl
    r = _curl.get(media_url, impersonate="chrome", stream=True, timeout=60)
    try:
        if getattr(r, "status_code", 0) >= 400:
            raise RuntimeError(f"CDN returned HTTP {r.status_code}")
        written = 0
        with dest.open("wb") as out:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError("file exceeds size cap")
                out.write(chunk)
                if on_progress:
                    on_progress(written)
        return written
    finally:
        try:
            r.close()
        except Exception:
            pass


def _safe_err(e) -> str:
    # Redact userinfo (e.g. a proxy's user:pass@) so a configured PROXY_URL
    # credential can't leak into job['error'], which /status returns to callers.
    return re.sub(r"//[^/@\s]+@", "//***@", str(e))


def _cookiefile() -> str:
    """General cookies file (Netscape cookies.txt), same as cloud. yt-dlp
    scopes cookies by domain, so one file can safely hold YouTube + Instagram
    + any other site. New COOKIES_FILE wins; YT_COOKIES_FILE is back-compat."""
    for var in ("COOKIES_FILE", "YT_COOKIES_FILE"):
        p = os.environ.get(var, "").strip()
        if p:
            if Path(p).exists():
                return p
            print(f"WARNING: {var}={p} does not exist; ignoring")
    return ""


def _proxy() -> str:
    for var in ("PROXY_URL", "YT_PROXY"):
        p = os.environ.get(var, "").strip()
        if p:
            return p
    return ""


def _extra_opts(url: str) -> dict:
    """Cookie/proxy hooks applied to EVERY download, mirroring cloud. Cookies
    are domain-scoped by yt-dlp, so sharing one file across sites is safe.
    Local-only extra: YT_COOKIES_BROWSER reads cookies straight from a browser
    profile (also domain-scoped). Everything stays off unless the matching env
    var is set, so default behavior is unchanged."""
    extra = {}
    cf = _cookiefile()
    if cf:
        extra["cookiefile"] = cf
    browser = os.environ.get("YT_COOKIES_BROWSER", "").strip()
    if browser and "cookiefile" not in extra:  # never combine the two
        extra["cookiesfrombrowser"] = (browser,)  # MUST be a tuple, not a string
    px = _proxy()
    if px:
        extra["proxy"] = px
    if _is_youtube(url) and extra:  # auth present -> cookie-compatible YT client
        extra["extractor_args"] = {"youtube": {"player_client": ["web_safari", "default"]}}
    return extra


def run_url_job(job_id: str, url: str, language: str | None):
    try:
        update_job(job_id, status="downloading", progress=0.0)

        # --- TikTok via tikwm first: it resolves share links and returns
        # audio-bearing media for both videos and photo/slideshow posts, which
        # yt-dlp's cookieless web extractor can't. Falls through to yt-dlp if
        # tikwm can't fetch it. ---
        if _is_tiktok(url):
            media = _tikwm_media(url)
            if media:
                media_url, title, is_photo = media
                print(f"[{job_id}] tikwm ok (photo={is_photo}) for {url}")
                if title:
                    with jobs_lock:
                        if job_id in jobs:
                            jobs[job_id]["source_title"] = title
                ext = ".m4a" if is_photo else ".mp4"
                file_path = UPLOAD_DIR / f"{job_id}{ext}"
                try:
                    _download_capped(media_url, file_path, TIKTOK_MAX_BYTES)
                except Exception as e:
                    file_path.unlink(missing_ok=True)
                    update_job(job_id, status="error", error=f"Download failed: {_safe_err(e)}")
                    return
                if not file_path.is_file() or file_path.stat().st_size == 0:
                    file_path.unlink(missing_ok=True)
                    update_job(job_id, status="error", error=TIKTOK_UNAVAILABLE_MSG)
                    return
                run_transcription(job_id, file_path, language)
                return
            print(f"[{job_id}] tikwm miss for {url}; falling back to yt-dlp")

        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                if total:
                    update_job(job_id, progress=min(done / total, 0.99))

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(UPLOAD_DIR / f"{job_id}.%(ext)s"),
            "progress_hooks": [progress_hook],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 30,
        }
        ydl_opts.update(_extra_opts(url))  # cookie/proxy hooks (any site), if set

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = Path(ydl.prepare_filename(info))
            title = info.get("title")
            if title:
                with jobs_lock:
                    if job_id in jobs:
                        jobs[job_id]["source_title"] = title

        run_transcription(job_id, file_path, language)
    except yt_dlp.utils.DownloadError as e:
        msg = _safe_err(e)
        ml = msg.lower()
        if _is_tiktok(url):  # reached only after tikwm already missed
            msg = TIKTOK_UNAVAILABLE_MSG
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

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "filename": file.filename,
            "created": datetime.utcnow().isoformat(),
        }

    threading.Thread(
        target=run_transcription, args=(job_id, saved_path, language), daemon=True
    ).start()
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
    return {"ai_enabled": anthropic_client is not None}


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
    return {"ok": True, "cloud": False}


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    lan_ip = get_lan_ip()
    print()
    print("Server running:")
    print(f"  This computer:  http://localhost:{port}")
    print(f"  Your phone:     http://{lan_ip}:{port}  (same Wi-Fi)")
    print()
    uvicorn.run(app, host="0.0.0.0", port=port)
