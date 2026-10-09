const form = document.querySelector("#search-form");
const searchError = document.querySelector("#search-error");
const sourceStatus = document.querySelector("#source-status");
const progressSection = document.querySelector("#search-progress");
const progressElement = document.querySelector("#scan-progress");
const progressTitle = document.querySelector("#progress-title");
const progressDetail = document.querySelector("#progress-detail");
const resultsSection = document.querySelector("#results-section");
const resultList = document.querySelector("#result-list");
const resultCount = document.querySelector("#result-count");
const scanNotice = document.querySelector("#scan-notice");
const pagination = document.querySelector("#pagination");
const pageLabel = document.querySelector("#page-label");
const previousButton = document.querySelector("#previous-page");
const nextButton = document.querySelector("#next-page");
const cancelButton = document.querySelector("#cancel-search");

const pageSize = 25;
let activeSearchId = null;
let currentOffset = 0;

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.detail || "The request could not be completed.");
  }
  return body;
}

function setError(message) {
  searchError.textContent = message;
  searchError.hidden = !message;
}

function updateProgress(job) {
  progressTitle.textContent = job.status === "cancelled" ? "Search cancelled" : "Scanning logs";
  progressDetail.textContent = job.current_file
    ? `${job.files_scanned} of ${job.files_total} files · ${job.lines_scanned.toLocaleString()} lines · ${job.matches.toLocaleString()} matches · ${job.current_file}`
    : `${job.files_scanned} of ${job.files_total} files · ${job.lines_scanned.toLocaleString()} lines · ${job.matches.toLocaleString()} matches`;
  progressElement.max = Math.max(job.files_total, 1);
  progressElement.value = job.files_scanned;
}

function createResult(result, index) {
  const item = document.createElement("li");
  item.className = "result-item";

  const header = document.createElement("div");
  header.className = "result-meta";

  const metadata = document.createElement("div");
  metadata.className = "result-location";

  const timestamp = document.createElement("time");
  timestamp.className = "result-time";
  timestamp.dateTime = result.timestamp;
  timestamp.textContent = result.timestamp;

  const source = document.createElement("span");
  source.className = "result-source";
  source.textContent = `${result.source_file} · line ${result.line_number}`;
  metadata.append(timestamp, source);

  const copyButton = document.createElement("button");
  copyButton.className = "button button-copy";
  copyButton.type = "button";
  copyButton.textContent = "Copy JSON";
  copyButton.setAttribute("aria-label", `Copy JSON for result ${index}`);
  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(result.payload_json);
      copyButton.textContent = "Copied";
      window.setTimeout(() => {
        copyButton.textContent = "Copy JSON";
      }, 1400);
    } catch {
      setError("Clipboard access is unavailable in this browser.");
    }
  });
  header.append(metadata, copyButton);

  const payload = document.createElement("pre");
  payload.className = "payload-json";
  const code = document.createElement("code");
  code.textContent = result.payload_json;
  payload.append(code);
  item.append(header, payload);
  return item;
}

async function loadPage(offset) {
  if (!activeSearchId) return;
  const job = await requestJson(
    `/api/search/${activeSearchId}?offset=${offset}&limit=${pageSize}`,
  );
  currentOffset = offset;
  resultList.replaceChildren(
    ...job.results.map((result, index) => createResult(result, offset + index + 1)),
  );
  const totalPages = Math.max(1, Math.ceil(job.matches / pageSize));
  const currentPage = Math.floor(offset / pageSize) + 1;
  resultCount.textContent = `${job.matches.toLocaleString()} ${job.matches === 1 ? "match" : "matches"}`;
  pageLabel.textContent = `Page ${currentPage} of ${totalPages}`;
  pagination.hidden = job.matches <= pageSize;
  previousButton.disabled = offset === 0;
  nextButton.disabled = offset + pageSize >= job.matches;
  resultsSection.hidden = false;

  const notices = [];
  if (job.status === "cancelled") {
    notices.push("Search cancelled. Showing matches collected before cancellation.");
  }
  notices.push(...job.warnings);
  scanNotice.textContent = notices.join(" ");
  scanNotice.hidden = notices.length === 0;
}

async function pollSearch(searchId) {
  if (activeSearchId !== searchId) return;
  try {
    const job = await requestJson(`/api/search/${searchId}?offset=0&limit=${pageSize}`);
    updateProgress(job);
    if (["queued", "running"].includes(job.status)) {
      window.setTimeout(() => pollSearch(searchId), 500);
      return;
    }
    progressSection.hidden = true;
    cancelButton.disabled = true;
    if (job.status === "failed") {
      setError(job.warnings.join(" ") || "The search failed.");
      return;
    }
    await loadPage(0);
  } catch (error) {
    progressSection.hidden = true;
    setError(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  scanNotice.hidden = true;
  resultsSection.hidden = true;
  resultList.replaceChildren();
  currentOffset = 0;
  const data = new FormData(form);
  try {
    const job = await requestJson("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        field: data.get("field"),
        value: data.get("value"),
        include_rotations: data.has("include_rotations"),
      }),
    });
    activeSearchId = job.id;
    progressSection.hidden = false;
    cancelButton.disabled = false;
    updateProgress(job);
    pollSearch(job.id);
  } catch (error) {
    setError(error.message);
  }
});

cancelButton.addEventListener("click", async () => {
  if (!activeSearchId) return;
  cancelButton.disabled = true;
  try {
    await requestJson(`/api/search/${activeSearchId}/cancel`, { method: "POST" });
  } catch (error) {
    setError(error.message);
  }
});

previousButton.addEventListener("click", () => loadPage(Math.max(0, currentOffset - pageSize)));
nextButton.addEventListener("click", () => loadPage(currentOffset + pageSize));

requestJson("/api/health")
  .then((health) => {
    sourceStatus.textContent = health.available
      ? "Log source available"
      : health.configured
        ? "Log source unavailable"
        : "Local configuration needed";
    sourceStatus.parentElement.classList.toggle("is-ready", health.available);
  })
  .catch(() => {
    sourceStatus.textContent = "Log source status unknown";
  });