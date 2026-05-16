function isReelPath() {
  return /^\/reels?\/[^/]+/.test(location.pathname);
}

function getReelUrl() {
  if (isReelPath()) return location.href.split("?")[0];
  return null;
}

function createButton() {
  const btn = document.createElement("button");
  btn.id = "rtc-btn";
  btn.title = "Transcribe this reel and send to Claude";
  btn.innerHTML = `
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      <line x1="9" y1="10" x2="15" y2="10"/>
      <line x1="9" y1="14" x2="13" y2="14"/>
    </svg>
  `;
  btn.addEventListener("click", onClick);
  return btn;
}

function ensureButton() {
  if (document.getElementById("rtc-btn")) return;
  document.body.appendChild(createButton());
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

ensureButton();

const observer = new MutationObserver(() => ensureButton());
observer.observe(document.body, { childList: true, subtree: true });
