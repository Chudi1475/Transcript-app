import os
import shutil
import socket
import sys
import sysconfig
import threading
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
# Sequential decoding is the default: on short clips it catches slightly more
# words and yields fine-grained segments (clean timestamps/SRT). Batched is
# faster on long audio but coarsens segments to ~30s chunks and catches a hair
# less, so it's opt-in via WHISPER_BATCHED=1 (batch size only matters then).
USE_BATCHED = os.environ.get("WHISPER_BATCHED", "0") == "1"
BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", "8"))

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
# Batched pipeline only when explicitly enabled and on GPU (it costs more RAM
# and coarsens segments). Off by default — see USE_BATCHED note above.
batched_model = (
    BatchedInferencePipeline(model=model)
    if (USE_BATCHED and ACTIVE_DEVICE != "cpu")
    else None
)
print(f"Active: {ACTIVE_MODEL} on {ACTIVE_DEVICE}, batched={batched_model is not None}")

# One ~4GB GPU: serialize transcription so two jobs can't OOM each other.
transcribe_lock = threading.Lock()

app = FastAPI()


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


def _transcribe(file_path: Path, language: str | None, batched: bool):
    kwargs = {
        "beam_size": BEAM_SIZE,
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


def run_transcription(job_id: str, file_path: Path, language: str | None):
    try:
        update_job(job_id, status="transcribing", progress=0.0)

        # Serialize GPU work so concurrent jobs can't OOM the 4GB card. If a
        # batched run runs out of VRAM, retry once sequentially (lower peak use).
        with transcribe_lock:
            use_batched = batched_model is not None
            try:
                segments_iter, info = _transcribe(file_path, language, use_batched)
                segs, text_parts = _drain(job_id, segments_iter, info)
            except Exception as e:
                if use_batched and _is_oom(e):
                    print(f"[{job_id}] batched OOM ({e}); retrying sequentially")
                    update_job(job_id, status="transcribing", progress=0.0)
                    segments_iter, info = _transcribe(file_path, language, False)
                    segs, text_parts = _drain(job_id, segments_iter, info)
                else:
                    raise

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
        update_job(job_id, status="error", error=str(e))
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


def run_url_job(job_id: str, url: str, language: str | None):
    try:
        update_job(job_id, status="downloading", progress=0.0)

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
            "socket_timeout": 30,
        }

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
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            msg = "This video is private or requires login."
        elif "rate" in msg.lower() or "429" in msg:
            msg = "Rate-limited by the site. Try again in a minute."
        update_job(job_id, status="error", error=msg)
    except Exception as e:
        update_job(job_id, status="error", error=f"Download failed: {e}")


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
