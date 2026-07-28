// Probe relay. Content-script fetches to localhost are attributed to the PAGE
// (instagram.com etc.), which Brave silently blocks and Chrome gates behind a
// local-network permission prompt — so from a content script the PC always
// looks dead and everything falls back to cloud. Fetches from this service
// worker run as the EXTENSION, which host_permissions exempts from both.
const LOCAL_SERVER = "http://localhost:8000";
const PROBE_TIMEOUT_MS = 1500;

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "probe-local") return;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS);
  fetch(`${LOCAL_SERVER}/healthz`, { cache: "no-store", signal: ctrl.signal })
    .then((r) => sendResponse({ ok: r.ok }))
    .catch(() => sendResponse({ ok: false }))
    .finally(() => clearTimeout(timer));
  return true; // keep the channel open for the async sendResponse
});
