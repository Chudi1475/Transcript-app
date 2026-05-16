const ICON_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="20" height="20">
  <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
  <line x1="9" y1="10" x2="15" y2="10"/>
  <line x1="9" y1="14" x2="13" y2="14"/>
</svg>`;

const ARROW_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" width="16" height="16">
  <line x1="5" y1="12" x2="19" y2="12"/>
  <polyline points="12 5 19 12 12 19"/>
</svg>`;

const LOG = "[reel-to-claude]";
const SERVER = "http://localhost:8000";

function ensureWidget() {
  let widget = document.getElementById("rtc-widget");
  if (widget && document.body.contains(widget)) return widget;
  if (widget) widget.remove();

  widget = document.createElement("div");
  widget.id = "rtc-widget";
  widget.innerHTML = `
    <span class="rtc-icon">${ICON_SVG}</span>
    <input class="rtc-url" type="url" placeholder="Paste reel URL…" autocomplete="off" spellcheck="false" />
    <button class="rtc-go" type="button" aria-label="Transcribe">${ARROW_SVG}</button>
  `;
  document.body.appendChild(widget);

  const input = widget.querySelector(".rtc-url");
  const goBtn = widget.querySelector(".rtc-go");

  const submit = () => handleSubmit(input.value, widget, input);
  goBtn.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    } else if (e.key === "Escape") {
      input.blur();
      widget.classList.remove("rtc-open");
    }
  });
  input.addEventListener("focus", () => {
    widget.classList.add("rtc-open");
    setTimeout(() => input.select(), 0);
  });

  console.log(LOG, "widget ready");
  return widget;
}

function handleSubmit(url, widget, input) {
  url = (url || "").trim();
  if (!url) {
    input.focus();
    return;
  }
  if (!/^https?:\/\//i.test(url)) {
    showStatus("Need an http(s):// URL", true);
    input.focus();
    return;
  }

  const target = `${SERVER}/?url=${encodeURIComponent(url)}`;
  window.open(target, "_blank", "noopener");

  input.value = "";
  showStatus("Opened transcript app");
  setTimeout(() => {
    hideStatus();
    widget.classList.remove("rtc-open");
  }, 1800);
}

let statusEl;
function showStatus(msg, isError = false) {
  if (!statusEl) {
    statusEl = document.createElement("div");
    statusEl.id = "rtc-status";
    document.body.appendChild(statusEl);
  }
  statusEl.textContent = msg;
  statusEl.classList.toggle("rtc-error", isError);
  statusEl.style.display = "block";
}
function hideStatus() {
  if (statusEl) statusEl.style.display = "none";
}

ensureWidget();
console.log(LOG, "content script loaded on", location.href);

const observer = new MutationObserver(() => {
  if (!document.getElementById("rtc-widget")) ensureWidget();
});
observer.observe(document.body, { childList: true, subtree: true });
