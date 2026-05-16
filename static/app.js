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
let showTimestamps = false;

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

function showError(msg) {
  hide(statusEl);
  show(errorEl);
  errorEl.textContent = "Error: " + msg;
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
  currentFilename = file.name;
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
      showError(`Upload failed (${xhr.status})`);
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

async function pollStatus(jobId) {
  while (true) {
    try {
      const res = await fetch(`/status/${jobId}`);
      if (!res.ok) {
        showError(`Status check failed (${res.status})`);
        return;
      }
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
        showError(job.error || "Unknown error");
        return;
      }
    } catch (e) {
      showError(e.message);
      return;
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
  fileMeta.textContent = url;
  setProgress("Starting…", 0);

  try {
    const res = await fetch("/transcribe-url", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, language: languageSelect.value }),
    });
    if (!res.ok) {
      const errText = await res.text();
      showError(`Request failed: ${errText}`);
      return;
    }
    const { job_id } = await res.json();
    pollStatus(job_id);
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

copyBtn.addEventListener("click", async () => {
  const text = currentOutputText();
  try {
    await navigator.clipboard.writeText(text);
    const orig = copyBtn.textContent;
    copyBtn.textContent = "Copied!";
    setTimeout(() => (copyBtn.textContent = orig), 1500);
  } catch {
    copyBtn.textContent = "Copy failed";
  }
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

openClaudeBtn.addEventListener("click", () => {
  const text = currentOutputText();
  const prefill = `Here's a transcript I want to discuss:\n\n${text}`;
  navigator.clipboard.writeText(prefill).catch(() => {});
  showToast("Transcript copied — paste it in Claude");
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
    const res = await fetch("/summarize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: currentText }),
    });
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
    if (cfg.ai_enabled) summarizeBtn.classList.remove("hidden");
  })
  .catch(() => {});
