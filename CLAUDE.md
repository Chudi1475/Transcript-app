# transcript-app

One-click transcription tool for Instagram reels (and anything else yt-dlp can
download). Local-first with a cloud fallback. Triggered from a Brave/Chrome
extension on desktop or an iOS Shortcut on iPhone.

## Architecture

```
              ┌─ Brave extension (Instagram) ─┐
              │                               │
              │   probe localhost:8000/healthz │
              │   (1s timeout)                 │
              │                               │
              └───── alive? ──────────────────┘
                     │              │
                     │              │
                     ▼              ▼
              ┌──────────────┐  ┌────────────────────────────────────┐
              │ LOCAL app.py │  │ CLOUD cloud/app.py (Render free)   │
              │ FastAPI      │  │ FastAPI                            │
              │ faster-whisper│ │ Groq whisper-large-v3-turbo        │
              │ small.en CUDA│  │                                    │
              └──────┬───────┘  └──────────┬─────────────────────────┘
                     │                     │
                     │  serves /static/    │
                     ▼                     ▼
                ┌──────────────────────────────────┐
                │ static/ frontend (shared)        │
                │ index.html + app.js + style.css  │
                │ auto-transcribes from ?url=…     │
                └──────────────────────────────────┘
```

iPhone path is identical to the cloud arm — an iOS Shortcut pipes the reel URL
into `https://transcript-app-cloud.onrender.com/?url=<encoded>` via the share
sheet, and `app.js` picks up the query param and fires transcription.

## File layout

```
app.py                      # local FastAPI, faster-whisper CUDA
cloud/
  app.py                    # cloud FastAPI, Groq Whisper API
  requirements.txt          # slim deps (no torch, no faster-whisper)
static/
  index.html                # single-page UI
  app.js                    # all frontend logic, no build step
  style.css                 # gradient/glass theme
extension/
  manifest.json             # MV3, host_permissions for localhost
  content.js                # hover widget + local-or-cloud picker
  content.css
  README.md
start.bat                   # activates venv, runs app.py, logs to server.log
render.yaml                 # Render Blueprint for cloud deploy
requirements.txt            # local deps (faster-whisper, torch, etc.)
.env.example                # ANTHROPIC_API_KEY placeholder
uploads/                    # tmp downloads (gitignored)
server.log                  # local server log (gitignored)
```

## API (same shape in local and cloud)

| Method | Path                  | Body / Params                     | Returns                                |
|--------|-----------------------|-----------------------------------|----------------------------------------|
| POST   | `/transcribe-url`     | `{url, language?}`                | `{job_id}`                             |
| POST   | `/transcribe`         | multipart `file`, `language?`     | `{job_id}`                             |
| GET    | `/status/{job_id}`    |                                   | job dict                               |
| GET    | `/config`             |                                   | `{ai_enabled, cloud?}`                 |
| GET    | `/healthz`            |                                   | `{ok, cloud}` — used by extension probe|
| POST   | `/summarize`          | `{text, instruction?}`            | `{summary}`                            |
| GET    | `/`                   |                                   | serves `static/index.html`             |

Job dict lifecycle: `queued` → `downloading` (URL jobs only) → `transcribing` →
`done` | `error`. Fields: `status`, `progress` (0-1), `text`, `segments`,
`language`, `duration`, `source_url`/`source_title`, `error`.

## Auto-transcribe from `?url=`

`static/app.js` ends with an IIFE that reads `location.search`. If `?url=` is
present, it:

1. Hides the input form, shows the URL section
2. Fills `urlInput.value` with the param
3. Calls `history.replaceState({}, "", "/")` to clean the URL bar
4. Calls `handleUrl()` to kick off transcription

That's how both the desktop extension and the iOS Shortcut pipe URLs in
without the user pasting anything.

## Running locally

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in ANTHROPIC_API_KEY if you want /summarize
python app.py
```

Server binds to `0.0.0.0:8000` and prints its LAN IP so you can hit it from
your phone on the same Wi-Fi.

### Auto-start on Windows login

`start.bat` (in repo root) runs `python app.py` with output appended to
`server.log`. A VBS wrapper at
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\transcript-app.vbs`
calls it silently on login. Delete that .vbs to disable auto-start.

## Cloud deploy (Render free tier)

Render auto-reads `render.yaml`. Required env vars:

- `GROQ_API_KEY` — free at <https://console.groq.com/keys>
- `ANTHROPIC_API_KEY` — optional, only enables `/summarize`

Live URL: <https://transcript-app-cloud.onrender.com>

Cold start is ~30s after 15min idle (free tier sleeps). After that, requests
are fast.

## Extension setup

1. `brave://extensions` → Developer mode ON → Load unpacked → pick `extension/`
2. Edit `extension/content.js` line 17 if your Render URL ever changes:
   ```js
   const CLOUD_SERVER = "https://transcript-app-cloud.onrender.com";
   ```
3. Reload the extension card after edits.

The content script injects a gradient hover-pill at top-right of any
`instagram.com` page. Paste a reel URL → hits `/healthz` on localhost with a
1s timeout → opens local if alive, cloud otherwise.

## iPhone setup

iOS Shortcut named **Transcribe Reel**, wired into Share Sheet (URLs only):

1. Receive URLs from Share Sheet
2. URL Encode `Shortcut Input` (mode: Encode)
3. URL: `https://transcript-app-cloud.onrender.com/?url=` + `URL Encoded Text` (magic variable)
4. Open URL with the URL from step 3 (must be a **solid blue magic variable**, not the faded placeholder hint — otherwise step 4 errors with "No URL Specified")

iPhone always hits cloud. LAN detection is doable (try `192.168.4.38:8000/healthz` via "Get Contents of URL" with timeout, branch on success) but not currently wired up.

## Tech stack

- Python 3.11, FastAPI, uvicorn
- **Local transcription:** `faster-whisper` (`small.en`, CUDA, `float16`, beam 5)
- **Cloud transcription:** Groq `whisper-large-v3-turbo` (25MB per-file limit)
- `yt-dlp` for video URL → audio download (`bestaudio/best`, no ffmpeg required for IG reels)
- Anthropic SDK with `claude-sonnet-4-6` for `/summarize`
- Vanilla JS frontend, no bundler, no framework
- Chromium MV3 extension, content-script-only (no service worker)

## Gotchas

- **`ANTHROPIC_API_KEY` is separate from Claude Pro.** Pro subscription doesn't grant API access — you'd need a separate console.anthropic.com account with credits. The **Ask Claude** button works without it (just copies the transcript and opens claude.ai); only **Summarize** needs it.
- **Groq 25MB cap.** Long videos (>~25 min) won't go through cloud. Use local for those.
- **CUDA DLL dance.** Top of `app.py` adds `nvidia/cublas/bin` and `nvidia/cudnn/bin` to the DLL search path on Windows. If torch/whisper deps are upgraded or the GPU swapped, that block may need adjustment.
- **Render cold start.** First request after 15min idle waits ~30s for the dyno to wake. Subsequent requests are fast until idle again.
- **CRLF warnings on commit.** Windows checkout converts LF → CRLF in working tree. Harmless, ignore the git warnings.
- **Server log:** `server.log` in repo root, gitignored. Tail it if a transcription silently fails.

## Commit style

Casual, lowercase, terse. Focus on the "why" when it isn't obvious from the
diff. No AI/Claude/Anthropic attribution anywhere — no `Co-Authored-By`
trailers, no "generated with" footers. Example good messages:

```
add cloud fallback (groq whisper on render) + local /healthz probe
wire extension to deployed render cloud url
extension opens transcript app with url param, app auto-starts transcription
```
