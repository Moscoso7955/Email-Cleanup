async function loadLabels() {
  const select = document.getElementById("label-select");
  try {
    const resp = await fetch("/api/labels");
    if (resp.status === 401) {
      window.location.href = "/oauth/login";
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const labels = data.labels || [];
    select.innerHTML = '<option value="">— pick a label —</option>';
    for (const l of labels) {
      const opt = document.createElement("option");
      opt.value = l.id;
      opt.textContent = l.name;
      select.appendChild(opt);
    }
  } catch (err) {
    select.innerHTML = `<option value="">Failed to load labels: ${err.message}</option>`;
  }
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
  if (resp.status === 401) {
    window.location.href = "/oauth/login";
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    alert(`Picker config failed: ${body.message || resp.status}`);
    return;
  }
  const { api_key, access_token, app_id } = await resp.json();
  await loadPickerApi();

  const view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
    .setSelectFolderEnabled(true)
    .setMimeTypes("application/vnd.google-apps.folder")
    .setIncludeFolders(true)
    .setParent("root");

  const sharedView = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
    .setSelectFolderEnabled(true)
    .setMimeTypes("application/vnd.google-apps.folder")
    .setIncludeFolders(true)
    .setEnableDrives(true);

  const picker = new google.picker.PickerBuilder()
    .setOAuthToken(access_token)
    .setDeveloperKey(api_key)
    .setAppId(app_id)
    .addView(view)
    .addView(sharedView)
    .enableFeature(google.picker.Feature.SUPPORT_DRIVES)
    .setCallback(onPicked)
    .build();
  picker.setVisible(true);
}

function onPicked(data) {
  if (data.action !== google.picker.Action.PICKED) return;
  const folder = data.docs[0];
  document.getElementById("folder-id").value = folder.id;
  document.getElementById("folder-name").textContent = folder.name;
}

document.getElementById("pick-folder-btn").addEventListener("click", openPicker);
loadLabels();
