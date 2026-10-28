"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const SAFETY_KEY = "siliconcamp.safety.v1";

  const STEPS = [
    {
      id: "intro",
      note: "Read-only. Nothing is downloaded or installed.",
      next: "Continue",
    },
    {
      id: "tasks",
      note: "The check is read-only.",
      next: "Continue",
    },
    {
      id: "readiness",
      note: "Checks run locally.",
      next: "Continue",
    },
    {
      id: "safety",
      note: "",
      next: "Continue",
    },
    {
      id: "review",
      note: "Nothing here enables an install.",
      next: "Continue",
    },
    {
      id: "install",
      note: "Nothing was written to a disk.",
      next: "",
    },
  ];

  const state = {
    status: null,
    current: 0,
    running: false,
    safety: loadSafety(),
    toastTimer: null,
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function loadSafety() {
    try {
      const raw = localStorage.getItem(SAFETY_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
      const clean = {};
      for (const key of ["backup", "secondMac", "cable", "owner"]) {
        if (parsed[key] === true) clean[key] = true;
      }
      return clean;
    } catch {
      return {};
    }
  }

  function saveSafety() {
    try {
      localStorage.setItem(SAFETY_KEY, JSON.stringify(state.safety));
    } catch {
      /* Private browsing can refuse storage; the walkthrough still works. */
    }
  }

  function formatBytes(value) {
    if (!value) return "not built";
    const units = ["B", "KB", "MB", "GB"];
    let size = Number(value);
    let unit = 0;
    while (size >= 1000 && unit < units.length - 1) {
      size /= 1000;
      unit += 1;
    }
    return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function artifactState(row) {
    switch (row.status) {
      case "match":
        return { label: "Matches", kind: "ok" };
      case "mismatch":
        return { label: "Mismatch", kind: "bad" };
      case "unpinned":
        return { label: "Not pinned", kind: "warn" };
      default:
        return { label: "Not built", kind: "warn" };
    }
  }

  function roadmapKind(value) {
    if (value === "blocked" || value === "missing") return "bad";
    if (value === "preview") return "ok";
    return "warn";
  }

  function blockerKind(value) {
    return value === "unsolved" || value === "none" ? "bad" : "warn";
  }

  function showToast(message) {
    const toast = $("#toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("is-visible");
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      toast.classList.remove("is-visible");
      state.toastTimer = null;
    }, 2600);
  }

  function taskSelected() {
    const box = $("#task-check");
    return box ? box.checked : false;
  }

  function countGates() {
    return Array.from(document.querySelectorAll("[data-gate]")).filter(
      (box) => box.checked
    ).length;
  }

  function updateChrome() {
    const step = STEPS[state.current];
    const back = $("#back-button");
    const next = $("#next-button");

    back.disabled = state.current === 0;
    next.hidden = state.current === STEPS.length - 1;
    next.textContent = step.next || "Continue";
    next.disabled = step.id === "tasks" && !taskSelected();

    if (step.id === "tasks") {
      $("#action-note").textContent = taskSelected()
        ? step.note
        : "Select the compatibility check to continue.";
    } else if (step.id === "safety") {
      const checked = countGates();
      $("#action-note").textContent =
        `${checked} of 4 checked. Installation stays disabled.`;
    } else {
      $("#action-note").textContent = step.note;
    }
  }

  function showStep(index, focus = true) {
    const clamped = Math.max(0, Math.min(STEPS.length - 1, index));
    if (
      clamped > state.current &&
      STEPS[state.current].id === "tasks" &&
      !taskSelected()
    ) {
      showToast("Select the compatibility check to continue.");
      return;
    }
    state.current = clamped;

    for (const pane of document.querySelectorAll(".pane")) {
      const active = pane.dataset.pane === STEPS[clamped].id;
      pane.hidden = !active;
      pane.classList.toggle("is-active", active);
    }

    $("#panes").scrollTop = 0;
    updateChrome();

    if (focus) {
      const heading = document.querySelector(
        `.pane[data-pane="${STEPS[clamped].id}"] h1`
      );
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
    }
  }

  function nextStep() {
    if (state.current < STEPS.length - 1) showStep(state.current + 1);
  }

  function previousStep() {
    if (state.current > 0) showStep(state.current - 1);
  }

  function updateSafety() {
    const gates = Array.from(document.querySelectorAll("[data-gate]"));
    let checked = 0;
    for (const gate of gates) {
      gate.checked = state.safety[gate.dataset.gate] === true;
      if (gate.checked) checked += 1;
    }
    $("#safety-note").textContent =
      checked === gates.length
        ? "All four are checked. Installation is still unavailable: the Windows boot path does not exist."
        : "All four are required before any hardware experiment. They do not enable installation.";
    updateChrome();
  }

  function wireSafety() {
    for (const gate of document.querySelectorAll("[data-gate]")) {
      gate.addEventListener("change", () => {
        state.safety[gate.dataset.gate] = gate.checked;
        saveSafety();
        updateSafety();
      });
    }
    updateSafety();
  }

  function wireTasks() {
    const box = $("#task-check");
    if (box) box.addEventListener("change", updateChrome);
  }

  function renderMachine(status) {
    const machine = status.machine;
    const repo = status.repo;

    const facts = $("#machine-facts");
    facts.replaceChildren();
    const rows = [
      ["Model", `${machine.model} (${machine.target})`],
      ["Chip", machine.chip],
      ["macOS", `${machine.os} · ${machine.arch}`],
      ["Checkout", `${repo.branch} @ ${repo.commit}`],
    ];
    for (const [term, value] of rows) {
      const wrap = el("div");
      wrap.append(el("dt", null, term), el("dd", null, value));
      facts.append(wrap);
    }

    const notes = [];
    if (!machine.supported) {
      notes.push(
        "This is not the Mac16,3/J624 machine the firmware tables were captured from. The build still works, but every hardware expectation in this repository is untested here."
      );
    } else {
      notes.push("This is the captured Mac16,3 / J624 / T8132 target.");
    }
    if (repo.source_modified.length) {
      notes.push(`${repo.source_modified.length} tracked file(s) differ from the commit.`);
    }
    if (repo.submodules_modified.length) {
      notes.push("The patched submodules are intentionally dirty; the patches are the source of truth.");
    }
    $("#machine-note").textContent = notes.join(" ");

    if (!status.artifacts_ready) {
      $("#intro-status-detail").textContent =
        "One or more firmware images is missing or does not match its pinned hash. The install stays off: there is no Windows boot path and no graphics driver.";
    }
  }

  function renderArtifacts(status) {
    const list = $("#artifact-list");
    list.replaceChildren();

    for (const row of status.artifacts) {
      const item = el("div", "artifact");

      const copy = el("div", "artifact-copy");
      copy.append(el("strong", null, row.name));
      copy.append(el("small", null, row.description));
      copy.append(el("div", "artifact-build", `build: ${row.build}`));

      const meta = el("div", "artifact-meta");
      const tag = artifactState(row);
      meta.append(el("span", `status-text ${tag.kind}`, tag.label));
      meta.append(el("small", null, formatBytes(row.size)));
      if (row.actual) {
        meta.append(el("small", null, `${row.actual.slice(0, 10)}…`));
      }

      item.append(copy, meta);
      list.append(item);
    }
  }

  function renderBlockers(status) {
    const list = $("#blocker-list");
    list.replaceChildren();
    for (const blocker of status.blockers) {
      const item = el("li");
      const head = el("div");
      head.append(el("strong", null, blocker.name));
      head.append(
        el("span", `status-text ${blockerKind(blocker.status)}`, blocker.status)
      );
      item.append(head, el("p", null, blocker.detail));
      list.append(item);
    }
  }

  function renderChain(status) {
    const list = $("#chain-list");
    list.replaceChildren();
    status.boot_chain.forEach((step, index) => {
      const item = el("li");
      if (index === status.boot_chain.length - 1) item.className = "is-unreached";
      const copy = el("div");
      copy.append(el("strong", null, step.name));
      copy.append(el("small", null, step.detail));
      item.append(copy);
      list.append(item);
    });
  }

  function renderRoadmap(status) {
    const body = $("#roadmap-body");
    body.replaceChildren();
    for (const row of status.roadmap) {
      const tr = el("tr");
      const feature = el("td");
      feature.append(el("strong", null, row.feature));
      const stateCell = el("td");
      stateCell.append(
        el("span", `status-text ${roadmapKind(row.state)}`, row.state)
      );
      tr.append(feature, stateCell, el("td", null, row.detail));
      body.append(tr);
    }
  }

  function renderDiagnostics(status) {
    const row = $("#diagnostic-buttons");
    row.replaceChildren();
    for (const check of status.checks) {
      const button = el("button", "button small", check.label);
      button.type = "button";
      button.addEventListener("click", () => runCheck(check.id, check.label));
      row.append(button);
    }
  }

  async function runCheck(id, label) {
    if (state.running) return;
    state.running = true;
    const buttons = Array.from(document.querySelectorAll("#diagnostic-buttons button"));
    for (const button of buttons) button.disabled = true;
    const tag = $("#diag-state");
    tag.textContent = "Running";
    tag.className = "status-text warn";
    const output = $("#diag-output");
    output.textContent = `Running ${label}…\n`;

    try {
      const response = await fetch("/api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const payload = await response.json();
      if (!response.ok) {
        output.textContent = `error: ${payload.error || response.statusText}\n`;
        tag.textContent = "Error";
        tag.className = "status-text bad";
        return;
      }
      const verdict = payload.skipped ? "SKIPPED" : payload.ok ? "PASS" : "FAIL";
      output.textContent =
        `$ ${payload.label}\n` +
        `${verdict} (exit ${payload.exit}, ${payload.seconds}s)\n\n` +
        payload.output;
      tag.textContent = payload.skipped ? "Skipped" : payload.ok ? "Pass" : "Fail";
      tag.className = `status-text ${payload.skipped ? "warn" : payload.ok ? "ok" : "bad"}`;
    } catch (error) {
      output.textContent = `cannot reach the assistant: ${error}\n`;
      tag.textContent = "Offline";
      tag.className = "status-text bad";
    } finally {
      state.running = false;
      for (const button of buttons) button.disabled = false;
    }
  }

  function showError(message) {
    $("#intro-status-detail").textContent = message;
    showToast("The local assistant is not responding. Start server.py and reload.");
  }

  function render() {
    const status = state.status;
    if (!status) return;
    renderMachine(status);
    renderArtifacts(status);
    renderBlockers(status);
    renderChain(status);
    renderRoadmap(status);
    renderDiagnostics(status);
    updateChrome();
  }

  async function load() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`status ${response.status}`);
      state.status = await response.json();
      render();
    } catch (error) {
      showError(
        `Cannot read http://127.0.0.1:8770/api/status (${error}). ` +
          "Start `python3 tools/siliconcamp-assistant/server.py` and reload this page."
      );
    }
  }

  if (new URLSearchParams(location.search).get("native") === "1") {
    document.body.classList.add("native");
  }

  $("#next-button").addEventListener("click", nextStep);
  $("#back-button").addEventListener("click", previousStep);
  document.addEventListener("keydown", (event) => {
    if (event.metaKey && event.key === "[") {
      event.preventDefault();
      previousStep();
    } else if (event.metaKey && event.key === "]") {
      event.preventDefault();
      nextStep();
    }
  });

  wireTasks();
  wireSafety();
  showStep(0, false);
  load();
})();
