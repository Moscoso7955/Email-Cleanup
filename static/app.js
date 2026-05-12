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

loadLabels();
