const ICON_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="22" height="22">
  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
  <line x1="9" y1="10" x2="15" y2="10"/>
  <line x1="9" y1="14" x2="13" y2="14"/>
</svg>`;

const LOG = "[reel-to-claude]";

function isReelPath() {
  const p = location.pathname;
  return /^\/reels?\//.test(p) || /^\/p\//.test(p);
}

function getReelUrl() {
  return isReelPath() ? location.href.split("?")[0] : null;
}

function isVisible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  return cx >= 0 && cx <= innerWidth && cy >= 0 && cy <= innerHeight;
}

function findPlaybackSpeed() {
  for (const el of document.querySelectorAll("[aria-label]")) {
    const lbl = (el.getAttribute("aria-label") || "").toLowerCase();
    if (lbl.includes("playback") || lbl.includes("speed") || /^\d+(\.\d+)?x$/i.test(lbl)) {
      if (isVisible(el)) return el;
    }
  }
  for (const b of document.querySelectorAll("button, [role='button'], span")) {
    const t = (b.textContent || "").trim();
    if (/^\d+(\.\d+)?x$/i.test(t) && t.length < 6 && isVisible(b)) {
      return b.closest("button, [role='button']") || b;
    }
  }
  return null;
}

const ACTION_LABELS = [
  "Like", "Unlike", "Comment", "Share", "Save", "Remove",
  "More options", "Audio", "Audio page", "Mute", "Unmute",
];

function findLowestActionButton() {
  let lowest = null, lowestY = -Infinity;
  for (const label of ACTION_LABELS) {
    const els = document.querySelectorAll(`[aria-label="${label}"]`);
    for (const el of els) {
      if (!isVisible(el)) continue;
      const rect = el.getBoundingClientRect();
      if (rect.top > lowestY && rect.right > innerWidth / 2) {
        lowestY = rect.top;
        lowest = el.closest("button, [role='button'], div[role='button']") || el;
      }
    }
  }
  return lowest;
}

function getOrCreateButton() {
  let btn = document.getElementById("rtc-btn");
  if (btn && document.body.contains(btn)) return btn;
  if (btn) btn.remove();

  btn = document.createElement("button");
  btn.id = "rtc-btn";
  btn.type = "button";
  btn.setAttribute("aria-label", "Transcribe and send to Claude");
  btn.title = "Transcribe and send to Claude";
  btn.innerHTML = ICON_SVG;
  btn.addEventListener("click", onClick);
  document.body.appendChild(btn);
  console.log(LOG, "button created");
  return btn;
}

function hideButton() {
  const btn = document.getElementById("rtc-btn");
  if (btn) btn.style.display = "none";
}

function positionAbove(btn, target) {
  const rect = target.getBoundingClientRect();
  const size = 46;
  btn.style.display = "flex";
  btn.style.left = `${Math.round(rect.left + rect.width / 2 - size / 2)}px`;
  btn.style.top = `${Math.round(rect.top - size - 6)}px`;
  btn.style.right = "auto";
  btn.style.bottom = "auto";
}

function positionCorner(btn) {
  btn.style.display = "flex";
  btn.style.left = "auto";
  btn.style.top = "auto";
  btn.style.right = "24px";
  btn.style.bottom = "120px";
}

let lastMode = "";
function logMode(mode, target) {
  if (mode === lastMode) return;
  lastMode = mode;
  console.log(LOG, "position mode:", mode, target || "");
}

function update() {
  if (!isReelPath()) {
    hideButton();
    logMode("hidden (not a reel path)");
    return;
  }

  const btn = getOrCreateButton();

  const playback = findPlaybackSpeed();
  if (playback) {
    positionAbove(btn, playback);
    logMode("above-playback", playback);
    return;
  }

  const lowest = findLowestActionButton();
  if (lowest) {
    positionAbove(btn, lowest);
    logMode("above-lowest-action", lowest);
    return;
  }

  positionCorner(btn);
  logMode("corner-fallback");
}

let busy = false;

async function onClick() {
  if (busy) return;

  const url = getReelUrl();
  if (!url) {
    showStatus("Open a reel first", true);
    return;
  }

  busy = true;
  setBusy(true);
  showStatus("Transcribing…");

  try {
    const resp = await chrome.runtime.sendMessage({ type: "transcribe", url });
    if (!resp) throw new Error("No response — is the local server running?");
    if (resp.error) throw new Error(resp.error);

    const text = resp.text || "";
    const prefill = `Here's a transcript I want to discuss:\n\n${text}`;
    try { await navigator.clipboard.writeText(prefill); } catch {}

    showStatus("Opening Claude…");
    window.open("https://claude.ai/new", "_blank", "noopener");
  } catch (e) {
    showStatus("Error: " + e.message, true);
  } finally {
    busy = false;
    setBusy(false);
    setTimeout(hideStatus, 2500);
  }
}

function setBusy(b) {
  const btn = document.getElementById("rtc-btn");
  if (btn) btn.classList.toggle("busy", b);
}

let statusEl;
function showStatus(msg, isError = false) {
  if (!statusEl) {
    statusEl = document.createElement("div");
    statusEl.id = "rtc-status";
    document.body.appendChild(statusEl);
  }
  statusEl.textContent = msg;
  statusEl.classList.toggle("error", isError);
  statusEl.style.display = "block";
}
function hideStatus() {
  if (statusEl) statusEl.style.display = "none";
}

let pending = false;
function schedule() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    update();
  });
}

console.log(LOG, "content script loaded on", location.href);
schedule();
window.addEventListener("scroll", schedule, { passive: true, capture: true });
window.addEventListener("resize", schedule);
const observer = new MutationObserver(schedule);
observer.observe(document.body, { childList: true, subtree: true });
setInterval(schedule, 1500);
