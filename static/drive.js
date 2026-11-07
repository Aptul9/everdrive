"use strict";

(() => {
  const anonymous = document.body.dataset.anonymous === "true";
  const owner = document.body.dataset.owner === "true";
  const input = document.getElementById("file-input");
  const list = document.getElementById("file-list");
  const status = document.getElementById("list-status");
  const localKey = "everbright-drive-shares";
  let files = [];
  let busy = false;
  let cancelled = false;
  let active = null;
  let controller = null;
  const remembered = () => {
    try { return JSON.parse(localStorage.getItem(localKey) || "[]").filter(item => item.token && item.share); }
    catch { return []; }
  };
  function remember(file, upload) {
    const existing = remembered().filter(item => item.id !== file.id);
    existing.unshift({ id: file.id, token: upload.token, share: file.share_url.split("/")[2] });
    try { localStorage.setItem(localKey, JSON.stringify(existing.slice(0, 200))); }
    catch { drive.toast("Conserva il link: questo browser non può memorizzarlo."); }
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(svg.namespaceURI, "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }
  function cell(text, className = "") {
    const node = document.createElement("td");
    node.textContent = text;
    node.className = className;
    return node;
  }
  function button(label, name, action) {
    const node = document.createElement("button");
    node.className = "icon-button";
    node.title = label;
    node.setAttribute("aria-label", label);
    node.append(icon(name));
    node.addEventListener("click", action);
    return node;
  }
  function showShare(file) {
    const box = document.getElementById("share-result");
    const url = new URL(file.share_url, location.origin).href;
    document.getElementById("share-link").value = url;
    document.getElementById("open-share-link").href = url;
    box.hidden = false;
  }
  function render() {
    const search = document.querySelector("[data-testid=file-search]").value.toLocaleLowerCase("it");
    const selected = files.filter(file => file.name.toLocaleLowerCase("it").includes(search));
    list.replaceChildren();
    for (const file of selected) {
      const row = document.createElement("tr");
      row.dataset.fileId = file.id;
      const title = cell("", "name-cell");
      const marker = document.createElement("span");
      marker.className = "file-type";
      marker.append(icon("file"));
      const name = document.createElement("span");
      name.textContent = file.name;
      name.title = file.name;
      title.append(marker, name);
      row.append(title, cell(new Date(file.created_at).toLocaleDateString("it-IT", { day: "numeric", month: "short" }), "date-column"), cell(drive.formatBytes(file.size), "size-cell"));
      if (anonymous) {
        const remaining = Math.max(0, new Date(file.expires_at) - Date.now());
        const totalMinutes = Math.ceil(remaining / 60000);
        const hours = Math.floor(totalMinutes / 60);
        const minutes = totalMinutes % 60;
        row.append(cell(remaining ? `${hours}h ${minutes}m` : "Scaduto", "expiry-cell"));
      }
      const actions = cell("", "file-actions");
      if (anonymous) actions.append(button(`Condividi ${file.name}`, "link", () => showShare(file)));
      const download = document.createElement("a");
      download.href = file.download_url;
      download.className = "icon-button";
      download.title = `Scarica ${file.name}`;
      download.setAttribute("aria-label", `Scarica ${file.name}`);
      download.append(icon("download"));
      actions.append(download);
      const saved = remembered().find(item => item.id === file.id);
      if (owner || saved) actions.append(button(`Elimina ${file.name}`, "trash", () => remove(file, saved)));
      row.append(actions);
      list.append(row);
    }
    document.getElementById("file-count").textContent = files.length;
    document.getElementById("empty-state").hidden = selected.length > 0;
    document.querySelector(".file-table-wrap").hidden = selected.length === 0;
    status.textContent = search && !selected.length ? "Nessun file trovato." : "";
  }
  async function refresh() {
    try {
      const value = owner
        ? await drive.api(`/api/files/?anonymous=${anonymous ? "1" : "0"}`)
        : await drive.api("/api/anonymous-files/", drive.json({ tokens: remembered().map(item => item.share) }));
      files = value.files;
      render();
    } catch (error) { status.textContent = error.message; }
  }
  async function remove(file, saved) {
    const dialog = document.getElementById("delete-dialog");
    document.getElementById("delete-name").textContent = file.name;
    dialog.returnValue = "cancel";
    dialog.showModal();
    dialog.addEventListener("close", async () => {
      if (dialog.returnValue !== "delete") return;
      try {
        await drive.api(owner ? `/api/files/${file.id}/delete/` : `/api/uploads/${file.id}/cancel/`, {
          method: "POST", headers: saved ? { "X-Upload-Token": saved.token } : {},
        });
        drive.toast("File eliminato.");
        await Promise.all([refresh(), drive.capacity()]);
      } catch (error) { drive.toast(error.message, true); }
    }, { once: true });
  }
  async function upload(selection) {
    if (busy || !selection.length) return;
    busy = true;
    cancelled = false;
    document.querySelectorAll("[data-upload-trigger]").forEach(node => { node.disabled = true; });
    const progress = document.getElementById("upload-progress");
    progress.hidden = false;
    try {
      for (const file of selection) {
        if (cancelled) break;
        document.getElementById("upload-name").textContent = file.name;
        document.getElementById("upload-bar").value = 0;
        document.getElementById("upload-percent").textContent = "0%";
        active = await drive.api("/api/uploads/", drive.json({ name: file.name, size: file.size, anonymous }));
        for (let offset = 0; offset < file.size; offset += active.chunk_bytes) {
          if (cancelled) throw new Error("Caricamento annullato.");
          const data = await file.slice(offset, offset + active.chunk_bytes).arrayBuffer();
          const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", data)), byte => byte.toString(16).padStart(2, "0")).join("");
          controller = new AbortController();
          const value = await drive.api(`/api/uploads/${active.id}/chunk/`, {
            method: "POST", body: data, signal: controller.signal,
            headers: { "Content-Type": "application/octet-stream", "X-Upload-Token": active.token, "X-Upload-Offset": String(offset), "X-Chunk-Sha256": digest },
          });
          const percent = Math.round(value.received / file.size * 100);
          document.getElementById("upload-bar").value = percent;
          document.getElementById("upload-percent").textContent = `${percent}%`;
        }
        if (cancelled) throw new Error("Caricamento annullato.");
        const complete = await drive.api(`/api/uploads/${active.id}/finish/`, { method: "POST", headers: { "X-Upload-Token": active.token } });
        if (anonymous) { remember(complete, active); showShare(complete); }
        active = null;
        drive.toast(`Caricato: ${file.name}`);
        await Promise.all([refresh(), drive.capacity()]);
      }
    } catch (error) {
      if (active) {
        await drive.api(`/api/uploads/${active.id}/cancel/`, { method: "POST", headers: { "X-Upload-Token": active.token } }).catch(() => {});
      }
      drive.toast(cancelled ? "Caricamento annullato." : error.message, !cancelled);
    } finally {
      busy = false;
      active = null;
      controller = null;
      progress.hidden = true;
      input.value = "";
      document.querySelectorAll("[data-upload-trigger]").forEach(node => { node.disabled = false; });
      await drive.capacity();
    }
  }
  document.querySelectorAll("[data-upload-trigger]").forEach(node => node.addEventListener("click", () => input.click()));
  input.addEventListener("change", () => upload(Array.from(input.files)));
  document.querySelector("[data-testid=file-search]").addEventListener("input", render);
  document.getElementById("refresh-files").addEventListener("click", () => { refresh(); drive.capacity(); });
  document.getElementById("select-share-link").addEventListener("click", () => {
    const field = document.getElementById("share-link");
    field.focus(); field.select();
    drive.toast("Link selezionato. Premi Ctrl+C o usa Copia sul telefono.");
  });
  document.getElementById("cancel-upload").addEventListener("click", () => { cancelled = true; controller?.abort(); });
  const zone = document.querySelector(".dropzone");
  let dragDepth = 0;
  zone.addEventListener("dragenter", event => { event.preventDefault(); dragDepth++; zone.classList.add("dragging"); });
  zone.addEventListener("dragover", event => { event.preventDefault(); });
  zone.addEventListener("dragleave", () => { if (--dragDepth <= 0) zone.classList.remove("dragging"); });
  zone.addEventListener("drop", event => { event.preventDefault(); dragDepth = 0; zone.classList.remove("dragging"); upload(Array.from(event.dataTransfer.files)); });
  refresh();
  setInterval(() => { if (!busy) refresh(); }, 15000);
})();
