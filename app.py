import os
import shutil
import socket
import sys
import sysconfig
import threading
import uuid
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    site_packages = Path(sysconfig.get_paths()["purelib"])
    for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin"):
        dll_dir = site_packages / sub
        if dll_dir.exists():
            os.add_dll_directory(str(dll_dir))

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from faster_whisper import WhisperModel

import yt_dlp

APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MODEL_NAME = os.environ.get("WHISPER_MODEL", "small.en")
DEVICE = os.environ.get("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "float16")
BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))


def load_model():
    print(f"Loading Whisper model: {MODEL_NAME} ({DEVICE}, {COMPUTE_TYPE})")
    try:
        return WhisperModel(MODEL_NAME, device=DEVICE, compute_type=COMPUTE_TYPE)
    except Exception as e:
        if DEVICE != "cpu":
            print(f"GPU init failed: {e}")
            print("Falling back to CPU (int8). Set WHISPER_DEVICE=cuda to retry.")
            return WhisperModel(
                MODEL_NAME,
                device="cpu",
                compute_type="int8",
                cpu_threads=os.cpu_count() or 4,
            )
        raise


model = load_model()
print("Model loaded.")

app = FastAPI()

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def update_job(job_id: str, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def run_transcription(job_id: str, file_path: Path, language: str | None):
    try:
        update_job(job_id, status="transcribing", progress=0.0)

        kwargs = {"beam_size": BEAM_SIZE, "vad_filter": True}
        if language and language != "auto":
            kwargs["language"] = language

        segments_iter, info = model.transcribe(str(file_path), **kwargs)
        duration = info.duration or 1.0

        segs = []
        text_parts = []
        for seg in segments_iter:
            segs.append({"start": seg.start, "end": seg.end, "text": seg.text})
            text_parts.append(seg.text)
            update_job(job_id, progress=min(seg.end / duration, 0.99))

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
