// Rehearsal website: the theme choice and the copy buttons.
// Loaded in <head> so a saved theme applies before the first paint. Without JavaScript the page
// follows the system theme and the buttons stay hidden, so nothing is broken.
(() => {
  const root = document.documentElement;
  const KEY = "rehearsal-theme";
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  root.classList.add("js");

  const current = () => root.dataset.theme || (systemDark.matches ? "dark" : "light");

  function apply(theme) {
    if (theme !== "light" && theme !== "dark") return;
    root.dataset.theme = theme;
    for (const meta of document.querySelectorAll('meta[name="theme-color"]')) {
      meta.content = theme === "dark" ? "#1a1a18" : "#faf9f5";
    }
  }

  try {
    apply(localStorage.getItem(KEY));
  } catch {
    /* storage can be blocked; the system theme is used instead */
  }

  document.addEventListener("DOMContentLoaded", () => {
    const status = document.createElement("p");
    status.className = "visually-hidden";
    status.setAttribute("role", "status");
    document.body.append(status);
    const announce = (text) => {
      status.textContent = "";
      setTimeout(() => (status.textContent = text), 50);
    };

    const toggle = document.querySelector(".theme-toggle");
    if (toggle) {
      const label = () =>
        toggle.setAttribute("aria-label", current() === "dark" ? "Switch to light theme" : "Switch to dark theme");
      label();
      systemDark.addEventListener("change", label);
      toggle.addEventListener("click", () => {
        const next = current() === "dark" ? "light" : "dark";
        apply(next);
        try {
          localStorage.setItem(KEY, next);
        } catch {
          /* the choice then lasts for this page view only */
        }
        label();
        announce(next === "dark" ? "Dark theme on" : "Light theme on");
      });
    }

    for (const box of document.querySelectorAll(".code")) {
      const button = box.querySelector(".copy");
      const code = box.querySelector("pre");
      if (!button || !code) continue;
      const text = button.querySelector(".copy-text");
      let timer;

      const show = (message, copied) => {
        text.textContent = message;
        button.toggleAttribute("data-copied", copied);
        announce(copied ? "Copied to clipboard" : "Code selected. Copy it with your keyboard.");
        clearTimeout(timer);
        timer = setTimeout(() => {
          text.textContent = "Copy";
          button.removeAttribute("data-copied");
        }, 2000);
      };

      // When the clipboard is unavailable, select the code so it can be copied by hand.
      const selectCode = () => {
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        show("Selected", false);
      };

      button.addEventListener("click", () => {
        const value = code.textContent.replace(/\n+$/, "");
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(value).then(() => show("Copied", true), selectCode);
        } else {
          selectCode();
        }
      });
    }
  });
})();
