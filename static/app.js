const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const statusEl = document.getElementById("status");
const statusLabel = document.getElementById("statusLabel");
const statusPercent = document.getElementById("statusPercent");
const progressBar = document.getElementById("progressBar");
const fileMeta = document.getElementById("fileMeta");
const resultEl = document.getElementById("result");
const resultMeta = document.getElementById("resultMeta");
const transcriptEl = document.getElementById("transcript");
const errorEl = document.getElementById("error");
const languageSelect = document.getElementById("language");
const copyBtn = document.getElementById("copyBtn");
const downloadBtn = document.getElementById("downloadBtn");
const downloadSrtBtn = document.getElementById("downloadSrtBtn");
const toggleTimestamps = document.getElementById("toggleTimestamps");
const urlInput = document.getElementById("urlInput");
const urlBtn = document.getElementById("urlBtn");
const urlCancelBtn = document.getElementById("urlCancelBtn");
const urlSection = document.getElementById("urlSection");
const inputOptions = document.getElementById("inputOptions");
const linkCardBtn = document.getElementById("linkCardBtn");
const openClaudeBtn = document.getElementById("openClaudeBtn");
const summarizeBtn = document.getElementById("summarizeBtn");
const summaryEl = document.getElementById("summary");
const summaryContent = document.getElementById("summaryContent");
const summaryCloseBtn = document.getElementById("summaryCloseBtn");

let currentSegments = [];
let currentText = "";
let currentFilename = "";
let currentSourceUrl = "";
let showTimestamps = false;
let serverConfig = { ai_enabled: false, cloud: false };

const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");

function formatBytes(b) {
  if (b < 1024) return b + " B";
  if (b < 1024 ** 2) return (b / 1024).toFixed(1) + " KB";
  if (b < 1024 ** 3) return (b / 1024 ** 2).toFixed(1) + " MB";
  return (b / 1024 ** 3).toFixed(2) + " GB";
}

function formatTime(s) {
  s = Math.max(0, s || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function setProgress(label, pct) {
  show(statusEl);
  statusLabel.textContent = label;
  statusPercent.textContent = Math.round(pct) + "%";
  progressBar.style.width = pct + "%";
}

function showError(msg, sourceUrl, sourceLang) {
  hide(statusEl);
  show(errorEl);
  errorEl.textContent = "Error: " + msg;
  // Groq caps cloud files at 25MB, but the PC app has no such limit — when a
  // job dies on size, offer the local handoff (and take it if the PC answers).
  if (serverConfig.cloud && />25MB|too large/i.test(String(msg))) {
    offerLocalRun(sourceUrl, sourceLang);
  }
}

const LOCAL_APP = "http://localhost:8000";

async function offerLocalRun(sourceUrl, sourceLang) {
  if (isMobileDevice()) {
    // localhost on a phone is the phone — no handoff possible from here.
    const hint = document.createElement("div");
    hint.className = "local-fallback";
    hint.textContent =
      "Too long for cloud. Open the app on your PC to transcribe it (or use your PC's Wi-Fi address from this phone).";
    errorEl.appendChild(hint);
    return;
  }
  // carry the language the job was STARTED with (not the live dropdown, which
  // the user may have re-picked since) — the local page auto-starts from ?url=
  // before they could correct it
  const target = sourceUrl
    ? `${LOCAL_APP}/?url=${encodeURIComponent(sourceUrl)}&language=${encodeURIComponent(sourceLang || "auto")}`
    : LOCAL_APP;
  // Manual link first: top-level navigation still works where the alive-probe
  // fetch below gets blocked (e.g. the browser's local-network permission).
  const link = document.createElement("a");
  link.href = target;
  link.className = "local-fallback";
  link.textContent = sourceUrl
    ? "Run it on this PC instead →"
    : "Open the PC app and drop the file there →";
  errorEl.appendChild(document.createElement("br"));
  errorEl.appendChild(link);
  if (!sourceUrl) return; // a picked file can't ride along on a redirect
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    const resp = await fetch(`${LOCAL_APP}/healthz`, { cache: "no-store", signal: ctrl.signal });
    clearTimeout(timer);
    // Only auto-redirect if the user hasn't started a different job while the
    // probe was in flight — otherwise leave the manual link and stay put.
    if (resp.ok && currentSourceUrl === sourceUrl) {
      link.textContent = "PC app is running — sending it there…";
      window.location.href = target;
    }
  } catch {
    /* PC off or probe blocked — the manual link stays */
  }
}

["dragenter", "dragover"].forEach((e) =>
  dropzone.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((e) =>
  dropzone.addEventListener(e, (ev) => {
    ev.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (ev) => {
  if (ev.dataTransfer.files.length) handleFile(ev.dataTransfer.files[0]);
});
fileInput.addEventListener("change", () => {
  if (fileInput.files.length) handleFile(fileInput.files[0]);
});

function handleFile(file) {
  hide(resultEl);
  hide(errorEl);
  // Cloud (Groq) caps files at 25MB — reject before wasting the whole upload.
  if (serverConfig.cloud && file.size > 25 * 1024 * 1024) {
    showError("File >25MB — too large for cloud transcription. Run it on your local PC.");
    return;
  }
  currentFilename = file.name;
  currentSourceUrl = ""; // file job — don't let a stale URL leak into its errors
  fileMeta.textContent = `${file.name} · ${formatBytes(file.size)}`;
  setProgress("Uploading…", 0);

  const form = new FormData();
  form.append("file", file);
  const language = languageSelect.value;

  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/transcribe?language=${encodeURIComponent(language)}`);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      setProgress("Uploading…", (e.loaded / e.total) * 100);
    }
  };
  xhr.onload = () => {
    if (xhr.status !== 200) {
      // surface the server's friendly message (e.g. the 25MB 413) when present
      let detail;
      try { detail = JSON.parse(xhr.responseText).detail; } catch {}
      showError(typeof detail === "string" && detail ? detail : `Upload failed (${xhr.status})`);
      return;
    }
    try {
      const { job_id } = JSON.parse(xhr.responseText);
      pollStatus(job_id);
    } catch (e) {
      showError("Bad server response");
    }
  };
  xhr.onerror = () => showError("Network error during upload");
  xhr.send(form);
}

async function pollStatus(jobId, sourceUrl, sourceLang) {
  let failures = 0; // tolerate transient blips (Wi-Fi hiccup, Render restart)
  while (true) {
    try {
      const res = await fetch(`/status/${jobId}`);
      if (!res.ok) throw new Error(`Status check failed (${res.status})`);
      failures = 0;
      const job = await res.json();
      const labels = {
        queued: "Queued…",
        downloading: "Downloading…",
        transcribing: "Transcribing…",
      };
      if (job.status in labels) {
        setProgress(labels[job.status], (job.progress || 0) * 100);
        if (job.source_title) fileMeta.textContent = job.source_title;
      } else if (job.status === "done") {
        setProgress("Done", 100);
        renderResult(job);
        return;
      } else if (job.status === "error") {
        // the job's OWN url and language, not the globals — a stale job's
        // error must never hand off whatever the user started afterwards
        showError(job.error || "Unknown error", sourceUrl, sourceLang);
        return;
      }
    } catch (e) {
      if (++failures >= 4) {
        showError(e.message);
        return;
      }
    }
    await new Promise((r) => setTimeout(r, 800));
  }
}

async function handleUrl() {
  const url = urlInput.value.trim();
  if (!url) {
    urlInput.focus();
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    showError("Link must start with http:// or https://");
    return;
  }

  hide(resultEl);
  hide(errorEl);
  currentFilename = url.split("/").filter(Boolean).pop() || "transcript";
  currentSourceUrl = url;
  fileMeta.textContent = url;
  setProgress("Starting…", 0);

  const language = languageSelect.value; // snapshot: the dropdown may change mid-job
  try {
    const res = await fetch("/transcribe-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, language }),
    });
    if (!res.ok) {
      // surface the server's friendly message (e.g. the 429 rate-limit) when present
      let detail;
      try { detail = JSON.parse(await res.text()).detail; } catch {}
      showError(typeof detail === "string" && detail ? detail : `Request failed (${res.status})`, url, language);
      return;
    }
    const { job_id } = await res.json();
    pollStatus(job_id, url, language);
  } catch (e) {
    showError(e.message);
  }
}

urlBtn.addEventListener("click", handleUrl);
urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") handleUrl();
});

linkCardBtn.addEventListener("click", () => {
  hide(inputOptions);
  show(urlSection);
  urlInput.focus();
});

urlCancelBtn.addEventListener("click", () => {
  hide(urlSection);
  show(inputOptions);
  urlInput.value = "";
});

function renderResult(job) {
  hide(statusEl);
  show(resultEl);
  currentSegments = job.segments || [];
  currentText = job.text || "";
  const parts = [];
  if (job.language) parts.push(job.language);
  if (job.duration) parts.push(formatTime(job.duration));
  parts.push(`${currentSegments.length} segments`);
  resultMeta.textContent = parts.join(" · ");
  renderTranscript();
}

function renderTranscript() {
  transcriptEl.innerHTML = "";
  if (showTimestamps) {
    for (const s of currentSegments) {
      const row = document.createElement("div");
      row.className = "seg-line";
      const stamp = document.createElement("span");
      stamp.className = "timestamp";
      stamp.textContent = formatTime(s.start);
      const txt = document.createElement("span");
      txt.textContent = s.text.trim();
      row.appendChild(stamp);
      row.appendChild(txt);
      transcriptEl.appendChild(row);
    }
  } else {
    transcriptEl.textContent = currentText;
  }
}

toggleTimestamps.addEventListener("click", () => {
  showTimestamps = !showTimestamps;
  toggleTimestamps.textContent = showTimestamps ? "Hide timestamps" : "Show timestamps";
  renderTranscript();
});

function currentOutputText() {
  if (showTimestamps) {
    return currentSegments
      .map((s) => `[${formatTime(s.start)}] ${s.text.trim()}`)
      .join("\n");
  }
  return currentText;
}

// Captured once at load so retries always reset to the original label,
// no matter how many times we've stomped on it with "Copied!" or "Copy failed".
const COPY_BTN_DEFAULT_TEXT = copyBtn.textContent;

// Robust copy: try execCommand first (synchronous, works in non-secure
// contexts like http://192.168.x.x and on iOS Safari), fall back to the
// modern async clipboard API. This makes "Copy failed" rare.
function copyTextRobust(text) {
  // 1) Synchronous execCommand path — needs to run inside the click handler's
  //    user-gesture window. Try first because it's the broadest-compat option.
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    // Off-screen + readonly so iOS doesn't zoom or show the keyboard.
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "0";
    ta.style.left = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return Promise.resolve(true);
  } catch {
    /* fall through */
  }
  // 2) Modern async path — works in secure contexts (https, localhost).
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(
      () => true,
      () => false
    );
  }
  return Promise.resolve(false);
}

copyBtn.addEventListener("click", async () => {
  const text = currentOutputText();
  if (!text) return;
  const ok = await copyTextRobust(text);
  copyBtn.textContent = ok ? "Copied!" : "Copy failed — tap to retry";
  // Always reset, so retry is one tap away even after a failure.
  clearTimeout(copyBtn._resetTimer);
  copyBtn._resetTimer = setTimeout(() => {
    copyBtn.textContent = COPY_BTN_DEFAULT_TEXT;
  }, ok ? 1500 : 2200);
});

function triggerDownload(text, ext, mime) {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = currentFilename.replace(/\.[^.]+$/, "") + "." + ext;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

downloadBtn.addEventListener("click", () => {
  triggerDownload(currentOutputText(), "txt", "text/plain");
});

function formatSrtTime(s) {
  const total = Math.max(0, s || 0);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = Math.floor(total % 60);
  const ms = Math.floor((total * 1000) % 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")},${String(ms).padStart(3, "0")}`;
}

function segmentsToSrt(segments) {
  return segments
    .map(
      (s, i) =>
        `${i + 1}\n${formatSrtTime(s.start)} --> ${formatSrtTime(s.end)}\n${s.text.trim()}\n`
    )
    .join("\n");
}

downloadSrtBtn.addEventListener("click", () => {
  if (!currentSegments.length) return;
  triggerDownload(segmentsToSrt(currentSegments), "srt", "application/x-subrip");
});

function showToast(msg, ms = 2200) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function isMobileDevice() {
  return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
}

function tryOpenClaudeApp() {
  let appOpened = false;
  const onHide = () => { if (document.hidden) appOpened = true; };
  document.addEventListener("visibilitychange", onHide);

  const a = document.createElement("a");
  a.href = "claude://new";
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();

  setTimeout(() => {
    document.removeEventListener("visibilitychange", onHide);
    if (!appOpened && !document.hidden) {
      window.location.href = "https://claude.ai/new";
    }
  }, 1500);
}

openClaudeBtn.addEventListener("click", async (e) => {
  const text = currentOutputText();
  const prefill = `Here's a transcript I want to discuss:\n\n${text}`;
  // Fire-and-forget, but use the robust copier so LAN IP / mobile still works.
  const copied = await copyTextRobust(prefill);
  showToast(copied
    ? "Transcript copied — paste it in Claude"
    : "Open Claude, then long-press the input and paste manually");

  if (isMobileDevice()) {
    e.preventDefault();
    tryOpenClaudeApp();
  }
});

summarizeBtn.addEventListener("click", async () => {
  if (!currentText) return;
  show(summaryEl);
  summaryContent.classList.add("loading");
  summaryContent.textContent = "Asking Claude…";
  summarizeBtn.disabled = true;
  const orig = summarizeBtn.textContent;
  summarizeBtn.textContent = "Thinking…";

  try {
    // x-app-key: only enforced when the server has APP_KEY set (cloud); on a
    // 401 we ask once and remember the key in localStorage.
    const doFetch = () =>
      fetch("/summarize", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-app-key": localStorage.getItem("appKey") || "",
        },
        body: JSON.stringify({ text: currentText }),
      });
    let res = await doFetch();
    if (res.status === 401) {
      const key = prompt("This server requires an access key for Summarize:");
      if (key) {
        localStorage.setItem("appKey", key.trim());
        res = await doFetch();
      }
    }
    if (!res.ok) {
      const errText = await res.text();
      summaryContent.classList.remove("loading");
      summaryContent.textContent = "Error: " + errText;
      return;
    }
    const { summary } = await res.json();
    summaryContent.classList.remove("loading");
    summaryContent.textContent = summary;
  } catch (e) {
    summaryContent.classList.remove("loading");
    summaryContent.textContent = "Error: " + e.message;
  } finally {
    summarizeBtn.disabled = false;
    summarizeBtn.textContent = orig;
  }
});

summaryCloseBtn.addEventListener("click", () => hide(summaryEl));

fetch("/config")
  .then((r) => r.json())
  .then((cfg) => {
    serverConfig = cfg || serverConfig;
    if (cfg.ai_enabled) summarizeBtn.classList.remove("hidden");
  })
  .catch(() => {});

(function autoTranscribeFromQuery() {
  const params = new URLSearchParams(location.search);
  const autoUrl = params.get("url");
  if (!autoUrl) return;
  const lang = params.get("language");
  if (lang && [...languageSelect.options].some((o) => o.value === lang)) {
    languageSelect.value = lang;
  }
  hide(inputOptions);
  show(urlSection);
  urlInput.value = autoUrl;
  history.replaceState({}, "", "/");
  handleUrl();
})();
