"use strict";

const expiry = document.querySelector("[data-expires]");
if (expiry) {
  const deadline = new Date(expiry.dataset.expires).getTime();
  const check = () => {
    if (Date.now() >= deadline) {
      const button = document.querySelector("[data-testid=public-download]");
      button.removeAttribute("href");
      button.classList.add("disabled");
      button.textContent = "Condivisione scaduta";
      expiry.querySelector("span").textContent = "Il file non è più disponibile.";
    }
  };
  check();
  setInterval(check, 1000);
}
