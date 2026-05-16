const SERVER = "http://localhost:8000";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type !== "transcribe") return false;

  (async () => {
    try {
      const r = await fetch(`${SERVER}/transcribe-url`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: msg.url, language: "auto" }),
      });
      if (!r.ok) throw new Error(`Server error ${r.status}: ${await r.text()}`);
      const { job_id } = await r.json();

      while (true) {
        await new Promise((res) => setTimeout(res, 1000));
        const s = await fetch(`${SERVER}/status/${job_id}`);
        if (!s.ok) throw new Error(`Status check failed (${s.status})`);
        const job = await s.json();
        if (job.status === "done") {
          sendResponse({ text: job.text || "" });
          return;
        }
        if (job.status === "error") {
          throw new Error(job.error || "Transcription failed");
        }
      }
    } catch (e) {
      const raw = e?.message || String(e);
      const friendly = raw.includes("Failed to fetch")
        ? "Can't reach the local server — is it running on localhost:8000?"
        : raw;
      sendResponse({ error: friendly });
    }
  })();

  return true;
});
