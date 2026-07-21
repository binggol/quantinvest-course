(function () {
  "use strict";
  const context = document.getElementById("csrf-context");
  const token = context ? context.dataset.csrfToken : "";
  if (typeof window.fetch === "function" && !window.fetch.__quantinvestCsrfWrapped) {
    const originalFetch = window.fetch.bind(window);
    const securedFetch = function (input, init) {
      const options = Object.assign({}, init || {});
      const isRequest = typeof Request !== "undefined" && input instanceof Request;
      const isUrl = typeof URL !== "undefined" && input instanceof URL;
      const method = String(options.method || (isRequest ? input.method : "GET")).toUpperCase();
      const rawUrl = typeof input === "string" || isUrl ? String(input) : (input && input.url);
      let sameOrigin = false;
      try {
        sameOrigin = Boolean(rawUrl) && new URL(rawUrl, window.location.href).origin === window.location.origin;
      } catch (_) {
        sameOrigin = false;
      }
      if (token && sameOrigin && !["GET", "HEAD", "OPTIONS"].includes(method)) {
        const headers = new Headers(options.headers || (isRequest ? input.headers : undefined));
        headers.set("X-CSRF-Token", token);
        options.headers = headers;
      }
      return originalFetch(input, options);
    };
    Object.defineProperty(securedFetch, "__quantinvestCsrfWrapped", { value: true });
    window.fetch = securedFetch;
  }

  const toggle = document.querySelector(".nav-toggle");
  const nav = document.getElementById("main-nav");
  if (toggle && nav) {
    const groups = Array.from(nav.querySelectorAll("details.nav-group"));
    const closeGroups = function (except) {
      groups.forEach(function (group) {
        if (group !== except) group.open = false;
      });
    };
    const setOpen = function (open) {
      nav.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", String(open));
      toggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
      if (!open) closeGroups();
    };
    toggle.addEventListener("click", function () {
      setOpen(!nav.classList.contains("open"));
    });
    groups.forEach(function (group) {
      group.addEventListener("toggle", function () {
        if (group.open) closeGroups(group);
      });
    });
    nav.addEventListener("click", function (event) {
      if (event.target.closest("a")) setOpen(false);
    });
    document.addEventListener("click", function (event) {
      if (!nav.contains(event.target) && event.target !== toggle) closeGroups();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        const hadOpenNav = nav.classList.contains("open");
        const hadOpenGroup = groups.some(function (group) { return group.open; });
        closeGroups();
        if (hadOpenNav) setOpen(false);
        if (hadOpenGroup || hadOpenNav) toggle.focus();
      }
    });
  }
})();
