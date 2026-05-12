async function loadLabels() {
  const select = document.getElementById("label-select");
  try {
    const resp = await fetch("/api/labels");
    if (resp.status === 401) { window.location.href = "/oauth/login"; return; }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    select.innerHTML = '<option value="">— pick a label —</option>';
    for (const l of data.labels || []) {
      const opt = document.createElement("option");
      opt.value = l.id;
      opt.textContent = l.name;
      select.appendChild(opt);
    }
  } catch (err) {
    select.innerHTML = `<option value="">Failed to load labels: ${err.message}</option>`;
  }
  updateStartGate();
}

let pickerApiLoaded = false;
function loadPickerApi() {
  return new Promise((resolve, reject) => {
    if (pickerApiLoaded) return resolve();
    gapi.load("picker", {
      callback: () => { pickerApiLoaded = true; resolve(); },
      onerror: reject,
    });
  });
}

async function openPicker() {
  const resp = await fetch("/api/picker-config");
  if (resp.status === 401) { window.location.href = "/oauth/login"; return; }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    alert(`Picker config failed: ${body.message || resp.status}`);
    return;
  }
  const { api_key, access_token, app_id } = await resp.json();
  await loadPickerApi();

  const myDrive = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
    .setSelectFolderEnabled(true)
    .setMimeTypes("application/vnd.google-apps.folder")
    .setIncludeFolders(true)
    .setParent("root");

  const shared = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
    .setSelectFolderEnabled(true)
    .setMimeTypes("application/vnd.google-apps.folder")
    .setIncludeFolders(true)
    .setEnableDrives(true);

  const picker = new google.picker.PickerBuilder()
    .setOAuthToken(access_token)
    .setDeveloperKey(api_key)
    .setAppId(app_id)
    .addView(myDrive)
    .addView(shared)
    .enableFeature(google.picker.Feature.SUPPORT_DRIVES)
    .setCallback(onPicked)
    .build();
  picker.setVisible(true);
}

function onPicked(data) {
  if (data.action !== google.picker.Action.PICKED) return;
  const folder = data.docs[0];
  document.getElementById("folder-id").value = folder.id;
  document.getElementById("folder-name").innerHTML = `<b>${escapeHtml(folder.name)}</b>`;
  document.getElementById("folder-name").classList.add("selected");
  updateStartGate();
}

function updateStartGate() {
  const btn = document.getElementById("start-btn");
  if (btn.dataset.running === "1") return;
  const ready = document.getElementById("label-select").value &&
                document.getElementById("folder-id").value;
  btn.disabled = !ready;
}

function logLine(html, cls = "") {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  li.innerHTML = html;
  document.getElementById("log").appendChild(li);
  li.scrollIntoView({ block: "end" });
}

function renderEvent(ev) {
  if (ev.type === "start") {
    logLine(`<b>Start:</b> ${escapeHtml(ev.label)} — ${ev.thread_count} thread(s)`);
  } else if (ev.type === "thread") {
    const tid = ev.thread_id ? `<code>${escapeHtml(ev.thread_id)}</code>` : "";
    if (ev.status === "processing") return; // noisy
    if (ev.status === "uploaded") logLine(`✓ uploaded <b>${escapeHtml(ev.filename)}</b>`, "ok");
    else if (ev.status === "planned") logLine(`· planned <b>${escapeHtml(ev.filename)}</b>`, "muted");
    else if (ev.status === "skipped") logLine(`– skipped ${tid} (${escapeHtml(ev.reason || "")})`, "muted");
    else if (ev.status === "error") logLine(`! error ${tid}: ${escapeHtml(ev.message || "")}`, "err");
  } else if (ev.type === "complete") {
    document.getElementById("summary").innerHTML =
      `<b>Done.</b> uploaded=${ev.uploaded} skipped=${ev.skipped} failed=${ev.failed}`;
  } else if (ev.type === "error") {
    logLine(`<b>Fatal:</b> ${escapeHtml(ev.message || "")}`, "err");
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let currentSource = null;

async function startRun(e) {
  e.preventDefault();
  const labelId = document.getElementById("label-select").value;
  const folderId = document.getElementById("folder-id").value;
  if (!labelId) { alert("Pick a Gmail label."); return; }
  if (!folderId) { alert("Pick a target Drive folder."); return; }

  const payload = {
    label_id: labelId,
    folder_id: folderId,
    after: document.getElementById("after-input").value || null,
    before: document.getElementById("before-input").value || null,
    limit: document.getElementById("limit-input").value || null,
    dry_run: document.getElementById("dry-run-input").checked,
  };

  const btn = document.getElementById("start-btn");
  btn.dataset.running = "1";
  btn.disabled = true;
  btn.textContent = "Running…";
  document.getElementById("progress-section").hidden = false;
  document.getElementById("log").innerHTML = "";
  document.getElementById("summary").innerHTML = "";

  let resp;
  try {
    resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    logLine(`<b>Network error:</b> ${escapeHtml(err.message)}`, "err");
    resetButton();
    return;
  }
  if (resp.status === 401) { window.location.href = "/oauth/login"; return; }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    logLine(`<b>Start failed:</b> ${escapeHtml(body.error || resp.status)}`, "err");
    resetButton();
    return;
  }
  const { job_id } = await resp.json();

  currentSource = new EventSource(`/api/jobs/${job_id}/stream`);
  currentSource.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    renderEvent(ev);
    if (ev.type === "complete" || ev.type === "error") {
      currentSource.close();
      currentSource = null;
      resetButton();
    }
  };
  currentSource.onerror = () => {
    logLine(`<b>Stream disconnected.</b>`, "err");
    if (currentSource) { currentSource.close(); currentSource = null; }
    resetButton();
  };
}

function resetButton() {
  const btn = document.getElementById("start-btn");
  btn.dataset.running = "0";
  btn.textContent = "Start";
  updateStartGate();
}

document.getElementById("pick-folder-btn").addEventListener("click", openPicker);
document.getElementById("filing-form").addEventListener("submit", startRun);
document.getElementById("label-select").addEventListener("change", updateStartGate);
document.getElementById("start-btn").disabled = true;
loadLabels();
