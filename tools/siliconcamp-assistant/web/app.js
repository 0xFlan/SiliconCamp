"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const SAFETY_KEY = "siliconcamp.safety.v1";

  const state = {
    status: null,
    running: false,
    safety: loadSafety(),
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
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  function saveSafety() {
    try {
      localStorage.setItem(SAFETY_KEY, JSON.stringify(state.safety));
    } catch {
      /* Private browsing can refuse storage; the panel still works. */
    }
  }

  function bytes(value) {
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

  function chip(text, kind) {
    return el("span", `chip ${kind || "muted"}`, text);
  }

  function artifactState(row) {
    switch (row.status) {
      case "match":
        return { label: "hash match", kind: "ok" };
      case "mismatch":
        return { label: "hash mismatch", kind: "bad" };
      case "unpinned":
        return { label: "unpinned", kind: "warn" };
      default:
        return { label: "build required", kind: "warn" };
    }
  }

  function roadmapKind(status) {
    if (status === "blocked" || status === "missing") return "bad";
    return "warn";
  }

  function renderMachine(status) {
    const machine = status.machine;
    const repo = status.repo;
    $("#machine-pill").textContent = `${machine.model} · ${machine.chip}`;
    $("#tag-pill").textContent = `${repo.tag || "untagged"} @ ${repo.commit}`;
    $("#checkout-line").textContent =
      `${repo.branch} @ ${repo.commit} · checked ${new Date(status.generated_at).toLocaleTimeString()}`;

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

    const machineChip = $("#machine-chip");
    machineChip.textContent = machine.supported ? "captured target" : "untested target";
    machineChip.className = `chip ${machine.supported ? "ok" : "warn"}`;

    const notes = [];
    if (!machine.supported) {
      notes.push(
        "This is not the Mac16,3/J624 machine the firmware tables were captured from. " +
          "The build still works, but every hardware expectation in this repository is untested here."
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
  }

  function renderArtifacts(status) {
    const list = $("#artifact-list");
    list.replaceChildren();
    const matched = status.artifacts.filter((row) => row.status === "match").length;

    for (const row of status.artifacts) {
      const item = el("div", "artifact");
      item.setAttribute("role", "listitem");

      const name = el("div", "artifact-name");
      name.append(el("strong", null, row.name), el("small", null, row.description));

      const path = el("div", "artifact-path");
      path.append(document.createTextNode(row.path));
      path.append(el("br"));
      path.append(document.createTextNode(`build: ${row.build}`));

      const meta = el("div", "artifact-meta");
      const state = artifactState(row);
      meta.append(chip(state.label, state.kind), el("small", null, bytes(row.size)));
      if (row.expected && row.actual) {
        meta.append(el("small", null, `${row.actual.slice(0, 12)}…`));
      }

      item.append(name, path, meta);
      list.append(item);
    }

    const imagesChip = $("#images-chip");
    imagesChip.textContent =
      status.artifacts.length === 0
        ? "no artifacts pinned"
        : `${matched} of ${status.artifacts.length} match`;
    imagesChip.className = `chip ${matched === status.artifacts.length && matched > 0 ? "ok" : "warn"}`;
  }

  function renderBlockers(status) {
    const list = $("#blocker-list");
    list.replaceChildren();
    for (const blocker of status.blockers) {
      const item = el("li");
      item.append(el("strong", null, blocker.name));
      item.append(el("small", null, `${blocker.status} — ${blocker.detail}`));
      list.append(item);
    }
  }

  function renderRoadmap(status) {
    const body = $("#roadmap-body");
    body.replaceChildren();
    for (const row of status.roadmap) {
      const tr = el("tr");
      const feature = el("td");
      feature.append(el("strong", null, row.feature));
      const stateCell = el("td");
      stateCell.append(chip(row.state, roadmapKind(row.state)));
      const detail = el("td", null, row.detail);
      tr.append(feature, stateCell, detail);
      body.append(tr);
    }
  }

  function renderChain(status) {
    const list = $("#chain-list");
    list.replaceChildren();
    for (const step of status.boot_chain) {
      const item = el("li");
      item.append(el("strong", null, step.name), el("small", null, step.detail));
      list.append(item);
    }
  }

  function updateSafety() {
    const boxes = Array.from(document.querySelectorAll("[data-gate]"));
    let checked = 0;
    for (const box of boxes) {
      if (state.safety[box.dataset.gate]) {
        box.checked = true;
        checked += 1;
      } else {
        box.checked = false;
      }
    }
    const progress = $("#safety-progress");
    progress.textContent = `${checked} of ${boxes.length}`;
    progress.className = `chip ${checked === boxes.length ? "ok" : "warn"}`;
    $("#safety-note").textContent =
      checked === boxes.length
        ? "All four acknowledgements are present. Installation is still disabled: the Windows boot path does not exist."
        : `${boxes.length - checked} acknowledgement(s) missing. All four are required before any hardware experiment, and they still do not enable an install.`;
  }

  function wireSafety() {
    const boxes = Array.from(document.querySelectorAll("[data-gate]"));
    for (const box of boxes) {
      box.addEventListener("change", () => {
        state.safety[box.dataset.gate] = box.checked;
        saveSafety();
        updateSafety();
      });
    }
    updateSafety();
  }

  function renderDiagnostics(status) {
    const row = $("#diagnostic-buttons");
    row.replaceChildren();
    for (const check of status.checks) {
      const button = el("button", "button secondary small", check.label);
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
    const status = $("#diag-state");
    status.textContent = "running";
    status.className = "chip warn";
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
        status.textContent = "error";
        status.className = "chip bad";
        return;
      }
      const verdict = payload.skipped ? "SKIPPED" : payload.ok ? "PASS" : "FAIL";
      output.textContent =
        `$ ${payload.label}\n` +
        `${verdict} (exit ${payload.exit}, ${payload.seconds}s)\n\n` +
        payload.output;
      status.textContent = payload.skipped ? "skipped" : payload.ok ? "pass" : "fail";
      status.className = `chip ${payload.skipped ? "warn" : payload.ok ? "ok" : "bad"}`;
    } catch (error) {
      output.textContent = `cannot reach the assistant: ${error}\n`;
      status.textContent = "offline";
      status.className = "chip bad";
    } finally {
      state.running = false;
      for (const button of buttons) button.disabled = false;
    }
  }

  function showError(message) {
    let banner = $("#load-error");
    if (!banner) {
      banner = el("div", "error-banner");
      banner.id = "load-error";
      document.querySelector("main").prepend(banner);
    }
    banner.textContent = message;
  }

  function render() {
    const status = state.status;
    if (!status) return;
    renderMachine(status);
    renderArtifacts(status);
    renderBlockers(status);
    renderRoadmap(status);
    renderChain(status);
    renderDiagnostics(status);
  }

  async function load() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`status ${response.status}`);
      state.status = await response.json();
      render();
    } catch (error) {
      $("#machine-pill").textContent = "assistant offline";
      $("#machine-pill").className = "pill";
      showError(
        `Cannot read the local assistant (${error}). Start it with ` +
          "`python3 tools/siliconcamp-assistant/server.py` and reload this page."
      );
    }
  }

  wireSafety();
  load();
})();
