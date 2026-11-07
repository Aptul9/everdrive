"use strict";

window.drive = {
  csrf: document.querySelector('meta[name="csrf-token"]').content,
  formatBytes(value) {
    if (value >= 1e9) return `${(value / 1e9).toLocaleString("it-IT", { maximumFractionDigits: 2 })} GB`;
    if (value >= 1e6) return `${(value / 1e6).toLocaleString("it-IT", { maximumFractionDigits: 1 })} MB`;
    if (value >= 1e3) return `${(value / 1e3).toLocaleString("it-IT", { maximumFractionDigits: 1 })} KB`;
    return `${value} B`;
  },
  async api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      credentials: "same-origin",
      headers: { "X-CSRFToken": this.csrf, ...options.headers },
    });
    const value = await response.json().catch(() => ({ error: "Richiesta non riuscita. Ricarica la pagina." }));
    if (!response.ok) throw new Error(value.error || "Richiesta non riuscita.");
    return value;
  },
  json(value) {
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
  },
  toast(message, error = false) {
    const node = document.getElementById("toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.hidden = false;
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => { node.hidden = true; }, 6000);
  },
  async capacity() {
    try {
      const value = await this.api("/api/storage/");
      const gb = (value.available_bytes / 1e9).toLocaleString("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      document.querySelectorAll("[data-capacity]").forEach(node => { node.textContent = `${gb} GB disponibili`; });
      document.querySelectorAll("[data-storage-progress]").forEach(node => {
        node.value = value.quota_bytes ? Math.min(100, (value.used_bytes + value.reserved_bytes) / value.quota_bytes * 100) : 100;
      });
      document.querySelectorAll("[data-storage-used]").forEach(node => {
        node.textContent = `${this.formatBytes(value.used_bytes)} usati su ${this.formatBytes(value.quota_bytes)}`;
      });
      return value;
    } catch {
      document.querySelectorAll("[data-capacity]").forEach(node => { node.textContent = "Spazio Drive non disponibile"; });
    }
  },
};
drive.capacity();
setInterval(() => drive.capacity(), 15000);
