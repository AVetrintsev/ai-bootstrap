(() => {
  "use strict";

  let current = null;
  let changing = false;
  let reading = false;
  let startupAttempts = 0;
  let nextStartup = 0;
  const label = document.createElement("label");
  label.style.cssText = "display:flex;width:fit-content;align-items:center;gap:.35rem;margin-top:.35rem;font-size:.75rem";
  label.append("Размышления");
  const select = document.createElement("select");
  select.dataset.testid = "ai-bootstrap-reasoning";
  select.style.cssText = "min-height:1.75rem;max-width:9rem;color:inherit;background:transparent;border-radius:.25rem";
  const status = document.createElement("span");
  status.setAttribute("role", "status");
  label.append(select, status);

  function conversation() {
    const id = location.pathname.match(/^\/conversations\/([a-f0-9-]{36})\/?$/i)?.[1];
    let project = new URLSearchParams(location.search).get("backend");
    if (!project) {
      try {
        project = JSON.parse(sessionStorage.getItem("openhands-active-backend"))?.backendId;
      } catch {
        return null;
      }
    }
    return id && project && /^gh-[1-9][0-9]{0,18}$/.test(project)
      ? `/agent/${project}/api/conversations/${id}` : null;
  }

  function mount() {
    const editor = document.querySelector('[data-testid="chat-input"]');
    if (!editor || current !== conversation() || !current || !select.options.length) {
      label.remove();
    } else if (editor.nextElementSibling !== label) {
      // The narrow Canvas toolbar hides its measured model button in an overflow menu.
      editor.insertAdjacentElement("afterend", label);
    }
  }

  async function refresh() {
    const url = conversation();
    if (url !== current) {
      current = url;
      startupAttempts = 0;
      nextStartup = 0;
      select.replaceChildren();
      status.textContent = "";
      mount();
    }
    if (!url || reading || changing || document.hidden || document.activeElement === select) return;
    reading = true;
    try {
      const startAgent = !select.options.length && startupAttempts < 3 && Date.now() >= nextStartup;
      if (startAgent) {
        startupAttempts += 1;
        nextStartup = Date.now() + 15000;
      }
      const response = await fetch(startAgent ? url : url + "?initialize_acp=false", {
        credentials: "same-origin",
        signal: AbortSignal.timeout?.(20000),
      });
      if (!response.ok) return;
      const info = await response.json();
      if (url !== conversation() || changing || document.activeElement === select) return;
      const options = (info.available_efforts ?? []).map(({value, name, description}) => {
        const option = new Option(name || value, value, false, value === info.current_effort);
        option.title = description || "";
        return option;
      });
      if (options.length && !options.some(option => option.selected)) {
        const placeholder = new Option("По умолчанию", "", false, true);
        placeholder.disabled = true;
        options.unshift(placeholder);
      }
      select.replaceChildren(...options);
      mount();
    } catch {
      // Canvas's Agent Server may still be starting the conversation.
    } finally {
      reading = false;
    }
  }

  select.addEventListener("change", async () => {
    const url = conversation();
    if (!url || url !== current) return;
    changing = true;
    select.disabled = true;
    status.textContent = "";
    try {
      const response = await fetch(url + "/switch_acp_effort", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({effort: select.value}),
      });
      if (!response.ok) throw new Error();
    } catch {
      if (url === conversation()) status.textContent = "Не удалось применить";
    } finally {
      select.disabled = false;
      changing = false;
      select.blur();
      void refresh();
    }
  });

  new MutationObserver(mount).observe(document.body, {childList: true, subtree: true});
  document.addEventListener("visibilitychange", refresh);
  window.setInterval(refresh, 4000);
  void refresh();
})();
