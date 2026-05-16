# Reel to Claude — Brave/Chrome extension

A small gradient pill pinned to the top-right corner of Instagram. Hover to
expand it into a URL input, paste a reel URL, hit Enter. The local
transcript-app transcribes it and opens Claude with the transcript on your
clipboard.

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

A small gradient circle appears at the top-right of any instagram.com page.

## Use

1. Hover the circle in the top-right — it slides open into an input pill.
2. Paste a reel URL into the input.
3. Press `Enter` (or click the arrow). Wait a few seconds while it
   downloads and transcribes.
4. claude.ai/new opens in a new tab with the transcript on your clipboard.
   Paste with `Ctrl+V`.

## Troubleshooting

- **"Can't reach the local server"** — start `python app.py` first.
- **Pill doesn't appear** — reload the extension at `brave://extensions`
  then refresh Instagram. Open devtools (`F12` → Console) and look for
  `[reel-to-claude]` messages to confirm the script loaded.
