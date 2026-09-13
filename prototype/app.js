(() => {
  "use strict";
  const data = window.BUILD_ROOM_PROJECTION;
  if (!data || data.schema_version !== "build-room.projection.v1") throw new Error("Unsupported Build Room projection");
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  const words = (value) => String(value).replaceAll("_", " ").replace(/\b\w/g, c => c.toUpperCase());
  const date = (value) => new Intl.DateTimeFormat("en-US", {month:"short", day:"numeric", hour:"numeric", minute:"2-digit", timeZone:"UTC", timeZoneName:"short"}).format(new Date(value));

  $("#companyName").textContent = data.company.display_name;
  $("#companyMeta").textContent = `${data.company.archetype} · ${data.company.jurisdiction} · ${data.company.offer} · ${data.company.service_area}`;
  $("#generatedAt").textContent = date(data.generated_at);
  $("#readinessExplanation").textContent = data.readiness.explanation;
  const renderBooleanState = (element, value, trueLabel, falseLabel, falseClass) => {
    const label = value ? trueLabel : falseLabel;
    element.textContent = label;
    element.classList.remove("good", "blocked", "neutral");
    element.classList.add(value ? "good" : falseClass);
    element.dataset.state = value ? "true" : "false";
    element.setAttribute("aria-label", `${trueLabel} status: ${label}`);
  };
  renderBooleanState($("#readyState"), data.readiness.ready, "Ready", "Not ready", "blocked");
  renderBooleanState($("#fullySetState"), data.readiness.fully_set, "Fully Set", "Not yet", "neutral");
  $("#progressText").textContent = `${data.summary.progress_percent}%`;
  $("#progressBar").setAttribute("aria-valuenow", data.summary.progress_percent);
  $("#progressBar span").style.width = `${data.summary.progress_percent}%`;
  $("#fullySetList").innerHTML = data.readiness.unmet_fully_set.map(item => `<li>${escapeHtml(words(item))}</li>`).join("");
  $("#budgetAvailable").textContent = data.budget.available;
  $("#budgetSettled").textContent = data.budget.settled;
  $("#budgetReserved").textContent = data.budget.reserved;
  $("#budgetCeiling").textContent = data.budget.ceiling;

  const renderWork = (filter = "all") => {
    const items = data.work_items.filter(item => filter === "all" || item.status_group === filter);
    $("#workGrid").innerHTML = items.map(item => `
      <article class="work-card" data-status="${escapeHtml(item.status_group)}">
        <div class="work-top"><span class="kind">${escapeHtml(item.kind)}</span><span class="work-status"><span class="status-dot ${escapeHtml(item.status_group)}" aria-hidden="true"></span>${escapeHtml(words(item.status))}</span></div>
        <h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.description)}</p>
        <div class="work-meta"><span>${escapeHtml(item.owner)} · ${escapeHtml(item.cost)}</span><button class="detail-button" type="button" data-record="${escapeHtml(item.id)}">Details</button></div>
      </article>`).join("") || `<p>No ${escapeHtml(filter)} work records.</p>`;
  };
  renderWork();

  document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach(item => { item.classList.remove("active"); item.setAttribute("aria-pressed", "false"); });
    button.classList.add("active"); button.setAttribute("aria-pressed", "true"); renderWork(button.dataset.filter);
  }));

  const actionableStates = new Set(["required", "in_progress", "submitted"]);
  $("#actionCount").textContent = `${data.founder_actions.filter(x => actionableStates.has(x.state)).length} open`;
  $("#actionList").innerHTML = data.founder_actions.map(item => `<article class="action"><div class="action-head"><div><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(item.reason)}</p></div><span class="risk">${escapeHtml(item.risk)} risk</span></div><p><strong>State:</strong> ${escapeHtml(words(item.state))}</p><ol>${item.instructions.map(step => `<li>${escapeHtml(step)}</li>`).join("")}</ol></article>`).join("");
  const openBlockers = data.blockers.filter(item => item.open);
  $("#blockerCount").textContent = `${openBlockers.length} open`;
  $("#blockerList").innerHTML = openBlockers.map(item => `<article class="blocker ${escapeHtml(item.severity)}"><div class="blocker-head"><div><h3>${escapeHtml(item.reason)}</h3><p>${escapeHtml(item.remediation)}</p></div><span class="risk">${escapeHtml(item.severity)}</span></div><div class="owner">Owner · ${escapeHtml(item.owner)}</div></article>`).join("");
  const approvalIcon = state => ({granted:"✓", denied:"×", revoked:"↶", expired:"!", superseded:"↗", requested:"…"}[state] || "?");
  const approvals = data.approvals.map(item => {
    const timestamp = item.decided_at ? `<time datetime="${escapeHtml(item.decided_at)}">Decided ${escapeHtml(date(item.decided_at))}</time>` : `<span class="pending-time">Requested ${escapeHtml(date(item.requested_at))}</span>`;
    return `<article class="approval approval-${escapeHtml(item.state)}"><span class="approval-check" aria-hidden="true">${approvalIcon(item.state)}</span><div><h3>${escapeHtml(item.title)}</h3><p><strong>${escapeHtml(words(item.state))}</strong> · ${escapeHtml(item.required_approver_role)}</p><p>${escapeHtml(item.summary)}</p><p>Bound record · ${escapeHtml(item.subject_digest)}</p></div>${timestamp}</article>`;
  });
  const evidence = data.evidence.map(item => `<article class="approval evidence-record"><span class="approval-check" aria-hidden="true">✓</span><div><h3>${escapeHtml(item.title)}</h3><p>${escapeHtml(words(item.evidence_type))} · ${escapeHtml(item.artifact_ref)}</p><p>${item.test_name ? `Test ${escapeHtml(item.test_name)} · ${item.test_passed ? "passed" : "not passed"}` : "Provider evidence"}</p></div><time datetime="${escapeHtml(item.captured_at)}">Captured ${escapeHtml(date(item.captured_at))}</time></article>`);
  $("#approvalGrid").innerHTML = [...approvals, ...evidence].join("");
  $("#handoffDescription").textContent = data.handoff.description;
  $("#handoffList").innerHTML = data.handoff.includes.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  $("#handoffBlocked").textContent = `Waiting on ${data.handoff.blocked_by.length} canonical records`;

  const dialog = $("#detailDialog");
  const showRecord = (item) => {
    $("#dialogTitle").textContent = item.title || "Activity";
    $("#dialogContent").innerHTML = `<dl>${Object.entries(item).filter(([, value]) => !Array.isArray(value) || value.length).map(([key, value]) => `<dt>${escapeHtml(words(key))}</dt><dd>${escapeHtml(Array.isArray(value) ? value.join(", ") : value)}</dd>`).join("")}</dl>`;
    dialog.showModal();
  };
  $("#workGrid").addEventListener("click", event => { const button = event.target.closest("[data-record]"); if (button) showRecord(data.work_items.find(item => item.id === button.dataset.record)); });
  $("#timelineButton").addEventListener("click", () => {
    $("#dialogTitle").textContent = "Canonical activity";
    $("#dialogContent").innerHTML = data.timeline.map(item => `<article class="action"><h3>${escapeHtml(item.label)}</h3><p>${escapeHtml(item.detail)}</p><div class="owner">${escapeHtml(date(item.occurred_at))} · ${escapeHtml(item.event_type)}</div></article>`).join("");
    dialog.showModal();
  });
  $("#helpButton").addEventListener("click", () => showRecord({title:"About this view", mode:"Offline fixture", authority:"Read-only projection", truth:"Canonical platform records", note:"This prototype cannot approve, spend, execute, deploy or mutate state."}));
  $("#closeDialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });

  const sections = [...document.querySelectorAll("main section[id]")];
  const links = [...document.querySelectorAll(".nav-link")];
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      links.forEach(link => {
        const active = link.hash === `#${entry.target.id}`;
        link.classList.toggle("active", active);
        active ? link.setAttribute("aria-current", "page") : link.removeAttribute("aria-current");
      });
    }), {rootMargin:"-25% 0px -65%"});
    sections.forEach(section => observer.observe(section));
  }
})();
