"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const SAFETY_KEY = "siliconcamp.safety.v1";
  const TOKEN =
    document.querySelector('meta[name="siliconcamp-token"]')?.content || "";

  // Boot Camp's order, with the two steps this project can actually perform.
  const STEPS = [
    { id: "intro", needs: [] },
    { id: "tasks", needs: [] },
    { id: "readiness", needs: ["check"] },
    { id: "installer", needs: ["media"] },
    { id: "allocation", needs: ["space"] },
    { id: "safety", needs: [] },
    { id: "review", needs: [] },
    { id: "install", needs: [] },
  ];

  const TASK_IDS = ["check", "media", "space"];

  const state = {
    status: null,
    installer: null,
    allocation: null,
    plan: null,
    spacePlan: null,
    iso: "",
    job: null,
    jobTimer: null,
    spaceJob: null,
    spaceStarting: false,
    spaceTimer: null,
    spaceKind: "",
    confirm: null,
    armed: "",
    busy: false,
    current: 0,
    safety: loadSafety(),
    toastTimer: null,
  };

  const bridge = window.webkit?.messageHandlers?.siliconcamp;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // -- stored acknowledgements -------------------------------------------------

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

  // -- helpers -----------------------------------------------------------------

  function formatBytes(value) {
    const size = Number(value) || 0;
    if (!size) return "none";
    const units = ["bytes", "KB", "MB", "GB", "TB"];
    let scaled = size;
    let unit = 0;
    while (scaled >= 1000 && unit < units.length - 1) {
      scaled /= 1000;
      unit += 1;
    }
    return `${scaled.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function formatGib(value) {
    return `${Number(value).toFixed(0)} GB`;
  }

  function taskOn(id) {
    const box = document.querySelector(`[data-task="${id}"]`);
    return box ? box.checked : false;
  }

  function activeSteps() {
    return STEPS.filter((step) => step.needs.every((id) => taskOn(id)));
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

  function actionNote(step) {
    switch (step.id) {
      case "intro":
        return "Nothing is written until you press Create Installer.";
      case "tasks": {
        const count = TASK_IDS.filter(taskOn).length;
        return count
          ? `${count} task(s) selected.`
          : "Select at least one task to continue.";
      }
      case "readiness":
        return "Checks run locally.";
      case "installer": {
        if (state.job && state.job.state === "running") return "Writing the installer…";
        if (!state.iso) return "Choose a Windows Arm64 ISO.";
        if (state.plan?.blockers?.length) return "The destination cannot hold this media.";
        if (selectedTarget().kind === "disk") {
          return "The USB disk will be erased before it is written.";
        }
        return "A new disk image will be created, written and verified.";
      }
      case "allocation": {
        if (state.spacePlan?.existing) return "A BOOTCAMP partition is present; remove it before allocating another.";
        if (state.spacePlan?.apply?.enabled) return "Allocating resizes the internal disk.";
        return "The internal disk is not changed; the rehearsal runs the real commands on a throwaway image.";
      }
      case "safety": {
        const checked = Array.from(document.querySelectorAll("[data-gate]")).filter(
          (box) => box.checked
        ).length;
        return `${checked} of 4 checked. Installation stays disabled.`;
      }
      case "review":
        return "Nothing here enables an install.";
      default:
        return "Nothing was written to the internal disk.";
    }
  }

  function renderStepList() {
    const steps = activeSteps();
    const step = steps[Math.min(state.current, steps.length - 1)];
    for (const pane of document.querySelectorAll(".pane")) {
      const active = pane.dataset.pane === step.id;
      pane.hidden = !active;
      pane.classList.toggle("is-active", active);
    }
    $("#back-button").disabled = state.current === 0;
    const next = $("#next-button");
    const last = state.current >= steps.length - 1;
    next.hidden = last;
    next.textContent = "Continue";
    next.disabled = step.id === "tasks" && !TASK_IDS.some(taskOn);
    $("#action-note").textContent = actionNote(step);
    return step;
  }

  function showStep(index, focus = true) {
    const steps = activeSteps();
    const max = steps.length - 1;
    const clamped = Math.max(0, Math.min(max, index));
    if (clamped > state.current && steps[state.current].id === "tasks" && !TASK_IDS.some(taskOn)) {
      showToast("Select at least one task to continue.");
      return;
    }
    state.current = clamped;
    const step = renderStepList();
    $("#panes").scrollTop = 0;
    if (step.id === "installer" && !state.installer) loadInstaller();
    if (step.id === "allocation" && !state.allocation) loadAllocation();
    if (focus) {
      const heading = document.querySelector(`.pane[data-pane="${step.id}"] h1`);
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
    }
  }

  function nextStep() {
    showStep(state.current + 1);
  }

  function previousStep() {
    showStep(state.current - 1);
  }

  // -- readiness ---------------------------------------------------------------

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
    notes.push(
      machine.supported
        ? "This is the captured Mac16,3 / J624 / T8132 target."
        : "This is not the Mac16,3/J624 machine the firmware tables were captured from. Every hardware expectation in this repository is untested here."
    );
    if (repo.source_modified.length) {
      notes.push(`${repo.source_modified.length} tracked file(s) differ from the commit.`);
    }
    if (repo.submodules_modified.length) {
      notes.push("The patched submodules are intentionally dirty; the patches are the source of truth.");
    }
    $("#machine-note").textContent = notes.join(" ");
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
      if (row.actual) meta.append(el("small", null, `${row.actual.slice(0, 10)}…`));
      item.append(copy, meta);
      list.append(item);
    }
  }

  function roadmapKind(value) {
    if (value === "available") return "ok";
    if (value === "blocked" || value === "missing") return "bad";
    return "warn";
  }

  function blockerKind(value) {
    return value === "unsolved" || value === "none" ? "bad" : "warn";
  }

  function renderBlockers(status) {
    const list = $("#blocker-list");
    list.replaceChildren();
    for (const blocker of status.blockers) {
      const item = el("li");
      const head = el("div");
      head.append(el("strong", null, blocker.name));
      head.append(el("span", `status-text ${blockerKind(blocker.status)}`, blocker.status));
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
      stateCell.append(el("span", `status-text ${roadmapKind(row.state)}`, row.state));
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
    if (state.busy) return;
    state.busy = true;
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
      state.busy = false;
      for (const button of buttons) button.disabled = false;
    }
  }

  // -- installer media ---------------------------------------------------------

  function writeHeaders(json = true) {
    const headers = { "X-SiliconCamp-Token": TOKEN };
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  function selectedTarget() {
    const select = $("#target-select");
    const value = select?.value || "";
    if (value.startsWith("disk:")) {
      return { kind: "disk", device: value.slice(5) };
    }
    return { kind: "image", path: $("#image-path")?.value.trim() || "" };
  }

  function targetLabel() {
    const select = $("#target-select");
    return select?.selectedOptions?.[0]?.textContent || "destination";
  }

  function renderTargets(data) {
    const select = $("#target-select");
    select.replaceChildren();
    for (const row of data.targets || []) {
      const option = el(
        "option",
        null,
        `${row.name} — ${formatBytes(row.size)} (${row.device.replace("/dev/", "")})`
      );
      option.value = `disk:${row.device}`;
      select.append(option);
    }
    const image = el("option", null, "Disk image…");
    image.value = "image:";
    select.append(image);

    if (!data.targets?.length) {
      select.value = "image:";
    }

    const note = $("#target-note");
    if (data.error) {
      note.textContent = `The disk list is unavailable: ${data.error}`;
    } else if (!data.targets?.length) {
      note.textContent =
        "No external USB disk is connected. A disk image is written to a file and can be flashed later.";
    } else {
      note.textContent = `${data.targets.length} external USB disk(s) found. Erasing a USB disk destroys everything on it.`;
    }
    if (!data.wimlib) {
      note.textContent +=
        " wimlib is not installed; an install.wim larger than 4 GiB cannot be split (`brew install wimlib`).";
    }
    syncTargetRows();
  }

  function syncTargetRows() {
    const target = selectedTarget();
    $("#image-row").hidden = target.kind !== "image";
    state.armed = "";
    updateCreateButton();
  }

  function updateCreateButton() {
    const button = $("#create-media");
    if (!button) return;
    const plan = state.plan;
    const blocked = !plan || plan.blockers?.length > 0 || !state.iso;
    const target = selectedTarget();
    if (target.kind === "image" && !target.path) {
      button.disabled = true;
      button.textContent = "Create Installer";
      return;
    }
    button.disabled = blocked || Boolean(state.job && state.job.state === "running");
    if (target.kind === "disk") {
      button.textContent = state.armed === target.device ? "Erase and Create" : "Create Installer";
    } else {
      button.textContent = "Create Installer";
    }
  }

  async function loadInstaller() {
    try {
      const response = await fetch("/api/installer", { cache: "no-store" });
      state.installer = await response.json();
      renderTargets(state.installer);
    } catch (error) {
      $("#target-note").textContent = `Cannot read the destination list: ${error}`;
    }
  }

  function renderIsoFacts(payload) {
    const facts = $("#iso-facts");
    facts.replaceChildren();
    const rows = [
      ["Boot loader", payload.boot_efi],
      ["Setup image", payload.boot_wim],
      ["Install image", `${payload.install_image} (${formatBytes(payload.install_bytes)})`],
      ["Files", `${payload.file_count} · ${formatBytes(payload.total_bytes)}`],
      ["Split needed", payload.needs_split ? "yes, install.wim exceeds 4 GiB" : "no"],
    ];
    for (const [term, value] of rows) {
      const wrap = el("div");
      wrap.append(el("dt", null, term), el("dd", null, value));
      facts.append(wrap);
    }
  }

  async function checkIso() {
    const iso = $("#iso-path").value.trim();
    const note = $("#iso-note");
    state.iso = "";
    state.plan = null;
    $("#iso-facts").replaceChildren();
    $("#media-plan").hidden = true;
    updateCreateButton();
    if (!iso) {
      note.textContent = "Enter the path to a Windows 11 Arm64 ISO.";
      return;
    }
    note.textContent = "Mounting and inspecting the ISO…";
    try {
      const response = await fetch("/api/installer/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ iso }),
      });
      const payload = await response.json();
      if (!response.ok) {
        note.textContent = payload.error || "The ISO was rejected.";
        return;
      }
      state.iso = payload.iso;
      renderIsoFacts(payload.payload);
      note.textContent = "This ISO has the Arm64 UEFI boot loader and the setup images Windows Setup needs.";
      planMedia();
    } catch (error) {
      note.textContent = `Cannot reach the assistant: ${error}`;
    }
  }

  async function planMedia() {
    const target = selectedTarget();
    const box = $("#media-plan");
    if (!state.iso || (target.kind === "image" && !target.path)) {
      box.hidden = true;
      updateCreateButton();
      return;
    }
    try {
      const response = await fetch("/api/installer/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ iso: state.iso, target }),
      });
      const plan = await response.json();
      if (!response.ok) {
        state.plan = null;
        box.hidden = false;
        box.replaceChildren(el("p", "plan-block", plan.error || "The destination was rejected."));
        updateCreateButton();
        updateChrome();
        return;
      }
      state.plan = plan;
      box.hidden = false;
      box.replaceChildren();
      box.append(el("h3", null, `${targetLabel()} · ${formatBytes(plan.capacity)}`));
      const list = el("ol", "plan-steps");
      for (const step of plan.steps) list.append(el("li", null, step));
      box.append(list);
      for (const blocker of plan.blockers) box.append(el("p", "plan-block", blocker));
      for (const warning of plan.warnings) box.append(el("p", "plan-warn", warning));
      if (target.kind === "disk") {
        box.append(
          el("p", "plan-warn", `Everything on ${target.device} is destroyed by this operation.`)
        );
      }
      updateCreateButton();
      updateChrome();
    } catch (error) {
      box.hidden = false;
      box.replaceChildren(el("p", "plan-block", `Cannot plan: ${error}`));
      updateCreateButton();
      updateChrome();
    }
  }

  function appendLog(line) {
    const log = $("#media-log");
    log.hidden = false;
    log.textContent += `${line}\n`;
    log.scrollTop = log.scrollHeight;
  }

  async function createMedia() {
    if (state.busy) return;
    const target = selectedTarget();
    if (target.kind === "disk" && state.armed !== target.device) {
      state.armed = target.device;
      updateCreateButton();
      showToast(`Press Create Installer again to erase ${target.device}.`);
      return;
    }
    state.busy = true;
    $("#media-log").textContent = "";
    $("#media-log").hidden = false;
    $("#media-state").textContent = "Starting";
    $("#media-state").className = "status-text warn";
    $("#create-media").disabled = true;
    try {
      const response = await fetch("/api/installer/create", {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify({ iso: state.iso, target, confirm: true }),
      });
      const payload = await response.json();
      if (!response.ok) {
        appendLog(`error: ${payload.error || response.statusText}`);
        $("#media-state").textContent = "Refused";
        $("#media-state").className = "status-text bad";
        return;
      }
      state.job = { id: payload.job, state: "running", seen: 0 };
      pollJob();
    } catch (error) {
      appendLog(`cannot reach the assistant: ${error}`);
      $("#media-state").textContent = "Offline";
      $("#media-state").className = "status-text bad";
    } finally {
      state.busy = false;
    }
  }

  async function pollJob() {
    const job = state.job;
    if (!job) return;
    try {
      const response = await fetch(`/api/jobs/${job.id}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) {
        appendLog(payload.error || "the job disappeared");
        finishJob("Failed", "bad");
        return;
      }
      const seen = job.id === payload.id ? job.seen || 0 : 0;
      for (const line of payload.notes.slice(seen)) appendLog(line);
      state.job = { ...payload, seen: payload.notes.length };
      if (payload.state === "running") {
        $("#media-state").textContent = "Writing";
        state.jobTimer = setTimeout(pollJob, 700);
        return;
      }
      if (payload.state === "done") {
        const verified = payload.result?.verified;
        appendLog(
          verified
            ? `done in ${payload.seconds}s: ${verified.checked} file(s) verified, ` +
              `${verified.hashed} hashed`
            : `done in ${payload.seconds}s`
        );
        finishJob("Created", "ok");
        if (payload.result?.image) {
          appendLog(`installer image: ${payload.result.image}`);
        }
      } else {
        finishJob("Failed", "bad");
      }
    } catch (error) {
      appendLog(`lost contact with the assistant: ${error}`);
      finishJob("Failed", "bad");
    }
  }

  function finishJob(label, kind) {
    if (state.jobTimer) clearTimeout(state.jobTimer);
    state.jobTimer = null;
    state.job = state.job ? { ...state.job, state: "finished" } : null;
    $("#media-state").textContent = label;
    $("#media-state").className = `status-text ${kind}`;
    updateCreateButton();
    updateChrome();
  }

  // -- space allocation --------------------------------------------------------

  function renderSpaceFacts(stats) {
    const facts = $("#space-facts");
    facts.replaceChildren();
    const rows = [
      ["Volume", `${stats.volume} (${stats.container})`],
      ["Container", formatBytes(stats.capacity)],
      ["Free now", formatBytes(stats.free)],
      ["Kept for macOS", formatBytes(stats.reserve)],
      ["Maximum for Windows", `${stats.max_gb} GB`],
    ];
    for (const [term, value] of rows) {
      const wrap = el("div");
      wrap.append(el("dt", null, term), el("dd", null, value));
      facts.append(wrap);
    }
    $("#space-note").textContent =
      stats.max_gb < stats.minimum_gb
        ? `Windows needs at least ${stats.minimum_gb} GB, and this Mac cannot give that up without eating into the reserve. Free space first.`
        : "Measured from this Mac's APFS container; nothing has been resized.";
  }

  async function loadAllocation() {
    const slider = $("#space-slider");
    try {
      const response = await fetch("/api/allocation", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) {
        $("#space-note").textContent = payload.error || "The APFS container could not be read.";
        slider.disabled = true;
        return;
      }
      state.allocation = payload;
      renderSpaceFacts(payload.stats);
      // Windows' own minimum is the floor, even when the Mac cannot meet it:
      // the slider must not offer a size Windows refuses, and the plan box then
      // explains the shortfall instead of showing a different number.
      const floor = payload.stats.minimum_gb;
      const ceiling = Math.max(floor, payload.stats.max_gb || payload.default_gb);
      slider.min = String(floor);
      slider.max = String(ceiling);
      slider.step = "1";
      slider.disabled = payload.stats.max_gb < floor;
      slider.value = String(payload.default_gb);
      renderSpacePlan(payload.plan);
      if (state.current === activeSteps().findIndex((s) => s.id === "allocation")) {
        updateChrome();
      }
    } catch (error) {
      $("#space-note").textContent = `Cannot read the APFS container: ${error}`;
    }
  }

  function renderSpacePlan(plan) {
    state.spacePlan = plan;
    const box = $("#space-plan");
    box.replaceChildren();
    $("#space-value").textContent = formatGib(plan.requested_gb);
    box.append(el("h3", null, "Planned partition"));
    const list = el("ul", "layout-list");
    for (const row of plan.layout) {
      const item = el("li");
      item.append(el("strong", null, `${row.name} — ${formatBytes(row.bytes)}`));
      item.append(el("span", null, `${row.role} · ${row.format} · ${row.note}`));
      list.append(item);
    }
    box.append(list);
    const after = el("p", "fine", `macOS keeps ${formatBytes(plan.after.free)} free (reserve ${formatBytes(plan.after.reserve)}).`);
    box.append(after);
    for (const blocker of plan.blockers) box.append(el("p", "plan-block", blocker));
    for (const warning of plan.warnings) box.append(el("p", "plan-warn", warning));

    $("#allocate-reason").textContent = plan.apply.reason || "";
    renderExisting(plan);
    spaceButtons();
    if (!state.spaceJob) $("#space-state").textContent = "";
  }

  function renderExisting(plan) {
    const existing = plan.existing;
    $("#existing-group").hidden = !existing;
    if (!existing) {
      closeSpaceConfirm();
      return;
    }
    const facts = $("#existing-facts");
    facts.replaceChildren();
    const rows = [
      ["Partition", existing.device],
      ["Size", formatBytes(existing.bytes)],
      ["Format", existing.filesystem ? existing.filesystem.toUpperCase() : "unknown"],
      ["Mounted at", existing.mount || "not mounted"],
    ];
    for (const [term, value] of rows) {
      const wrap = el("div");
      wrap.append(el("dt", null, term), el("dd", null, value));
      facts.append(wrap);
    }
    $("#existing-note").textContent =
      "Removing it erases the partition and returns its space to macOS. Any Windows " +
      "installation on it is lost.";
  }

  function spaceButtons() {
    const plan = state.spacePlan;
    const busy = Boolean(state.spaceJob) || state.spaceStarting;
    $("#allocate").disabled = busy || !plan?.apply?.enabled;
    $("#remove-space").disabled = busy || !plan?.remove?.enabled;
    $("#rehearse-space").disabled = busy || !plan?.fit;
  }

  async function planSpace() {
    const gib = Number($("#space-slider").value);
    try {
      const response = await fetch("/api/allocation/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gib }),
      });
      const plan = await response.json();
      if (!response.ok) {
        $("#space-plan").replaceChildren(el("p", "plan-block", plan.error || "rejected"));
        return;
      }
      renderSpacePlan(plan);
    } catch (error) {
      $("#space-plan").replaceChildren(el("p", "plan-block", `Cannot plan: ${error}`));
    }
  }

  function appendSpaceLog(line) {
    const log = $("#space-log");
    const wasHidden = log.hidden;
    log.hidden = false;
    log.textContent += `${line}\n`;
    log.scrollTop = log.scrollHeight;
    if (wasHidden) log.scrollIntoView({ block: "nearest" });
  }

  function closeSpaceConfirm() {
    $("#space-confirm").hidden = true;
    $("#space-confirm-field").value = "";
    $("#space-confirm-ok").disabled = true;
    state.confirm = null;
  }

  function openSpaceConfirm(kind) {
    const plan = state.spacePlan;
    if (!plan || state.spaceJob || state.spaceStarting) return;
    const applying = kind === "apply";
    if (applying && !plan.apply.enabled) return;
    if (!applying && !plan.remove.enabled) return;
    const expected = applying
      ? plan.host.container
      : String(plan.remove.device || "").replace("/dev/", "");
    const free = formatBytes(plan.after.reserve);
    state.confirm = { kind, expected };
    $("#space-confirm-title").textContent = applying
      ? "Allocate Space"
      : "Remove Windows Partition";
    $("#space-confirm-text").textContent = applying
      ? `macOS will shrink ${plan.host.container} by ${formatGib(plan.requested_gb)} and create one FAT32 partition named BOOTCAMP. ${free} stays reserved for macOS. This changes the internal disk.`
      : `This erases ${plan.remove.device} (${formatBytes(plan.remove.bytes)}) and returns its space to macOS. Any Windows installation on it is lost.`;
    $("#space-confirm-hint").textContent = `Type ${expected} to confirm.`;
    $("#space-confirm-field").placeholder = expected;
    $("#space-confirm-field").value = "";
    $("#space-confirm-ok").textContent = applying ? "Allocate" : "Remove";
    $("#space-confirm-ok").disabled = true;
    $("#space-confirm").hidden = false;
    $("#space-confirm-field").focus();
  }

  function confirmTyped() {
    if (!state.confirm) return;
    const typed = $("#space-confirm-field").value.trim().replace(/^\/dev\//, "");
    $("#space-confirm-ok").disabled = typed !== state.confirm.expected;
  }

  async function confirmSpace() {
    const pending = state.confirm;
    if (!pending) return;
    const typed = $("#space-confirm-field").value.trim().replace(/^\/dev\//, "");
    if (typed !== pending.expected) return;
    const gb = Number($("#space-slider").value);
    const body = pending.kind === "apply" ? { gb, confirm: typed } : { confirm: typed };
    const path =
      pending.kind === "apply" ? "/api/allocation/apply" : "/api/allocation/remove";
    closeSpaceConfirm();
    await startSpaceJob(pending.kind, path, body);
  }

  function endSpaceJob(label, kind) {
    if (state.spaceTimer) clearTimeout(state.spaceTimer);
    state.spaceTimer = null;
    state.spaceJob = null;
    $("#space-state").textContent = label;
    $("#space-state").className = `status-text ${kind}`;
    spaceButtons();
  }

  async function startSpaceJob(kind, path, body) {
    //
    // The request is async, so the job is not known until it returns.  Without
    // this flag a second click during that window posts a second job; the
    // server refuses it, but the refusal can arrive before the first job's id,
    // leaving a running write unobserved.
    //
    if (state.spaceJob || state.spaceStarting) return;
    state.spaceStarting = true;
    state.spaceKind = kind;
    $("#space-log").textContent = "";
    $("#space-log").hidden = false;
    $("#space-state").textContent = { rehearsal: "Rehearsing", apply: "Allocating", remove: "Removing" }[kind] || "Working";
    $("#space-state").className = "status-text warn";
    spaceButtons();
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) {
        appendSpaceLog(`error: ${payload.error || response.statusText}`);
        endSpaceJob("Refused", "bad");
        return;
      }
      state.spaceJob = { id: payload.job, seen: 0 };
      pollSpaceJob();
    } catch (error) {
      appendSpaceLog(`cannot reach the assistant: ${error}`);
      endSpaceJob("Offline", "bad");
    } finally {
      state.spaceStarting = false;
      spaceButtons();
    }
  }

  function rehearseSpace() {
    return startSpaceJob("rehearsal", "/api/allocation/rehearse", {
      gb: Number($("#space-slider").value),
    });
  }

  async function pollSpaceJob() {
    const job = state.spaceJob;
    if (!job) return;
    const kind = state.spaceKind;
    try {
      const response = await fetch(`/api/jobs/${job.id}`, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok) {
        appendSpaceLog(payload.error || "the job disappeared");
        endSpaceJob("Failed", "bad");
        return;
      }
      for (const line of payload.notes.slice(job.seen)) appendSpaceLog(line);
      state.spaceJob = { id: payload.id, seen: payload.notes.length };
      if (payload.state === "running") {
        state.spaceTimer = setTimeout(pollSpaceJob, 700);
        return;
      }
      if (payload.state !== "done") {
        endSpaceJob("Failed", "bad");
        return;
      }
      const result = payload.result || {};
      if (kind === "rehearsal" && result.created && result.restored) {
        appendSpaceLog(
          `done in ${payload.seconds}s: ${result.created.mount || result.created.device} was ` +
            `created and removed; the container returned to ${formatBytes(result.restored.container_after)}`
        );
      } else if (kind === "apply" && result.device) {
        appendSpaceLog(
          `done in ${payload.seconds}s: ${result.device} is ${formatBytes(result.bytes)} FAT32; ` +
            `the container is now ${formatBytes(result.container_after)}`
        );
      } else if (kind === "remove" && result.removed) {
        appendSpaceLog(
          `done in ${payload.seconds}s: ${result.removed} was removed; the container grew to ` +
            `${formatBytes(result.container_after)}`
        );
      } else {
        appendSpaceLog(`done in ${payload.seconds}s`);
      }
      endSpaceJob(
        { rehearsal: "Rehearsed", apply: "Allocated", remove: "Removed" }[kind] || "Done",
        "ok"
      );
      if (kind !== "rehearsal") {
        showToast(
          kind === "remove" ? "Space returned to macOS." : "Windows partition created."
        );
        // The live container changed; read it again rather than keeping the
        // numbers this page planned against.
        state.allocation = null;
        loadAllocation();
      }
    } catch (error) {
      appendSpaceLog(`lost contact with the assistant: ${error}`);
      endSpaceJob("Failed", "bad");
    }
  }

  // -- native file pickers -----------------------------------------------------

  window.siliconcampPicked = (kind, path) => {
    if (!path) return;
    if (kind === "iso") {
      $("#iso-path").value = path;
      checkIso();
    } else if (kind === "image") {
      $("#image-path").value = path;
      planMedia();
    }
  };

  function wireBrowsers() {
    if (!bridge) return;
    $("#iso-browse").hidden = false;
    $("#image-browse").hidden = false;
    $("#iso-browse").addEventListener("click", () => bridge.postMessage({ kind: "iso" }));
    $("#image-browse").addEventListener("click", () => bridge.postMessage({ kind: "image" }));
  }

  // -- shell -------------------------------------------------------------------

  function updateChrome() {
    renderStepList();
  }

  function wireTasks() {
    for (const id of TASK_IDS) {
      const box = document.querySelector(`[data-task="${id}"]`);
      if (box) box.addEventListener("change", () => showStep(state.current, false));
    }
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

  function showError(message) {
    showToast(message);
    $("#machine-note").textContent = message;
  }

  async function load() {
    try {
      const response = await fetch("/api/status", { cache: "no-store" });
      if (!response.ok) throw new Error(`status ${response.status}`);
      state.status = await response.json();
      renderMachine(state.status);
      renderArtifacts(state.status);
      renderBlockers(state.status);
      renderChain(state.status);
      renderRoadmap(state.status);
      renderDiagnostics(state.status);
      updateChrome();
    } catch (error) {
      showError(
        `Cannot read ${location.origin}/api/status (${error}). ` +
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
    } else if (event.key === "Escape" && !$("#space-confirm").hidden) {
      closeSpaceConfirm();
    }
  });

  wireTasks();
  wireSafety();
  wireBrowsers();
  $("#iso-check").addEventListener("click", checkIso);
  $("#iso-path").addEventListener("change", checkIso);
  $("#target-select").addEventListener("change", () => {
    syncTargetRows();
    planMedia();
  });
  $("#target-refresh").addEventListener("click", loadInstaller);
  $("#image-path").addEventListener("change", planMedia);
  $("#create-media").addEventListener("click", createMedia);
  $("#space-slider").addEventListener("input", () => {
    $("#space-value").textContent = formatGib(Number($("#space-slider").value));
  });
  $("#space-slider").addEventListener("change", planSpace);
  $("#rehearse-space").addEventListener("click", rehearseSpace);
  $("#allocate").addEventListener("click", () => openSpaceConfirm("apply"));
  $("#remove-space").addEventListener("click", () => openSpaceConfirm("remove"));
  $("#space-confirm-field").addEventListener("input", confirmTyped);
  $("#space-confirm-field").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      confirmSpace();
    }
  });
  $("#space-confirm-cancel").addEventListener("click", closeSpaceConfirm);
  $("#space-confirm-ok").addEventListener("click", confirmSpace);

  renderStepList();
  load();
})();
