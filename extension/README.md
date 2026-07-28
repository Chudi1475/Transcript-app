# Reel to Claude — Brave/Chrome extension

A small gradient pill pinned to the top-right corner of supported social media
sites. Hover to expand it into a URL input, paste any video URL, hit Enter.
A new tab opens to your transcript-app (local when reachable, cloud otherwise)
with the URL pre-loaded — transcription starts automatically.

## Supported sites

The widget appears on:

- Instagram (reels, posts)
- TikTok (incl. `vm.tiktok.com` share links)
- YouTube (incl. `youtu.be`, mobile)
- X / Twitter
- Facebook (incl. `fb.watch`)
- Reddit (old + new)
- Threads
- Snapchat
- Twitch (incl. `clips.twitch.tv`)

The backend uses `yt-dlp`, which actually supports hundreds of sites — you
can also paste any URL from a site not in this list and it'll still work, as
long as `yt-dlp` knows the site. The page list above is just where the widget
auto-injects.

## Requirements

For the **fast local path** to work, the transcript-app server must be running
at `http://localhost:8000`. It auto-starts on Windows login (see `start.bat`
and the Startup-folder VBS). To verify it's up, hit `http://localhost:8000/`
in a browser.

The **cloud fallback** is always available at
`https://transcript-app-cloud.onrender.com` — used automatically when the
local server doesn't respond. The alive-check runs through the extension's
background service worker (`background.js`): browsers treat a page-context
fetch to localhost as suspect (Brave blocks it silently, Chrome prompts), so
probing from the content script would always report the PC dead.

## Install in Brave (or Chrome)

1. Open `brave://extensions` (or `chrome://extensions`).
2. Toggle **Developer mode** on (top right).
3. Click **Load unpacked**.
4. Select this `extension` folder.

A small gradient circle appears at the top-right of any supported page.

## Use

1. Hover the circle in the top-right — it slides open into an input pill.
2. Paste a video URL into the input (from any supported site).
3. Press `Enter` (or click the arrow). A new tab opens to the transcript
   app and transcription starts immediately.
4. Use the action buttons on the transcript page (Ask Claude / Summarize /
   Copy / .txt / .srt) when it finishes.

## Troubleshooting

- **"PC is off and no cloud configured"** — `CLOUD_SERVER` in `content.js`
  isn't set. Edit line 17 to point at your deployed Render URL.
- **Pill doesn't appear** — reload the extension at `brave://extensions`
  then refresh the page. Open devtools (`F12` → Console) and look for
  `[reel-to-claude]` messages to confirm the script loaded.
- **Always goes to cloud even though the PC server is running** — the
  extension was loaded before `background.js` existed. Reload it at
  `brave://extensions` (the probe relay only exists after a reload).
- **Want to add another site?** — append its origin pattern to
  `manifest.json` → `content_scripts[0].matches` and reload the extension.
