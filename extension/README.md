# Reel to Claude — Brave/Chrome extension

A floating button on Instagram that transcribes the current reel using your
local transcript-app and opens Claude with the transcript on your clipboard.

## Requirements

The transcript-app server must be running at `http://localhost:8000`:

```bash
cd transcript-app
python app.py
```

## Install in Brave (or Chrome)

1. Open `brave://extensions` (or `chrome://extensions`).
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked**.
4. Select this `extension` folder.

A purple/pink circular button appears in the bottom-right corner of
instagram.com when you're on a reel page.

## Use

1. Open any reel — URL looks like `https://www.instagram.com/reel/<id>/` or
   `https://www.instagram.com/reels/<id>/`.
2. Click the floating button.
3. Wait a few seconds while the server downloads + transcribes the audio.
4. claude.ai/new opens in a new tab with the transcript on your clipboard —
   paste with `Ctrl+V`.

## Troubleshooting

- **"Can't reach the local server"** — start `python app.py` first.
- **"Open a reel first"** — the URL has to contain `/reel/` or `/reels/`. The
  reels feed updates the URL as you scroll, so just scroll to a reel.
- **Button doesn't appear** — Instagram heavily re-renders the page; reload
  the tab. If it still doesn't show, check the extension is enabled at
  `brave://extensions`.
