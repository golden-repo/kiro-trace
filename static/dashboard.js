/**
 * dashboard.js
 * =============
 * Client-side logic for the kiro-trace dashboard. Plain vanilla
 * JavaScript, no build step and no external libraries -- this file is
 * served as-is by the FastAPI app's StaticFiles mount, and the charts
 * below are hand-rolled inline SVG rather than a charting library, so
 * the dashboard keeps working fully offline / inside a locked-down
 * cluster network with no outbound internet access.
 *
 * Responsibilities:
 *   - Store/send the API key the user enters (via X-API-Key header).
 *   - Drive the "Usage Analytics" section: a filter bar (user, session,
 *     organization, repository, project name, branch, SCM type, VCS
 *     type, date range) backed by GET /api/telemetry/analytics, rendered
 *     across three tabs -- Overview (computed aggregates), Detailed
 *     Records (individual events), and Charts (trend visualizations).
 *   - Drive the "Fleet Health by Machine & Session" section: liveness
 *     status per component (handler/watcher), grouped machine -> user ->
 *     session, backed by GET /api/health + GET /api/telemetry, with
 *     destructive "Clear" actions gated behind a confirmation modal.
 */

const API_KEY_STORAGE_KEY = "kiroTraceApiKey";
const AUTO_REFRESH_INTERVAL_MS = 10000;
const COMPONENTS = ["handler", "watcher"];

let autoRefreshTimer = null;
let latestHealthRecords = [];
let latestTelemetryRecords = []; // from the fleet-health GET /api/telemetry call (unfiltered)
let latestAnalytics = null; // last GET /api/telemetry/analytics response
let pendingClearAction = null; // set right before showing the confirm modal
let activeTab = "overview";

/** Reads the saved API key (if any) from the browser's localStorage. */
function getSavedApiKey() {
  return window.localStorage.getItem(API_KEY_STORAGE_KEY) || "";
}

/** Saves the API key to localStorage so the user doesn't retype it every visit. */
function saveApiKey(key) {
  window.localStorage.setItem(API_KEY_STORAGE_KEY, key);
}

/**
 * Wrapper around fetch() that always attaches the X-API-Key header and
 * gives a consistent error shape to callers.
 */
async function apiFetch(path, options = {}) {
  const apiKey = getSavedApiKey();
  const response = await fetch(path, {
    ...options,
    headers: { "X-API-Key": apiKey, ...(options.headers || {}) },
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(`${response.status} ${response.statusText}: ${text}`);
  }
  // DELETE responses also return JSON in this API, so this is safe for
  // every verb currently used by this dashboard.
  return response.json();
}

function setAuthStatus(connected) {
  const pill = document.getElementById("auth-status");
  if (connected) {
    pill.textContent = "connected";
    pill.classList.add("connected");
  } else {
    pill.textContent = "not connected";
    pill.classList.remove("connected");
  }
}

function formatSecondsAgo(seconds) {
  if (!isFinite(seconds)) return "unknown";
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function dash(value) {
  return value === null || value === undefined || value === ""
    ? "&mdash;"
    : escapeHtml(value);
}

// =======================================================================
// Usage Analytics section (filter bar + tabs, backed by /analytics)
// =======================================================================

/** Reads the current value of every analytics filter field into a plain object. */
function readAnalyticsFilters() {
  return {
    user_name: document.getElementById("filter-user").value || undefined,
    session_id: document.getElementById("filter-session").value || undefined,
    organization:
      document.getElementById("filter-organization").value || undefined,
    repo_name: document.getElementById("filter-repo").value || undefined,
    project_name: document.getElementById("filter-project").value || undefined,
    branch: document.getElementById("filter-branch").value || undefined,
    scm_type: document.getElementById("filter-scm-type").value || undefined,
    vcs_type: document.getElementById("filter-vcs-type").value || undefined,
    start_date: document.getElementById("filter-start-date").value || undefined,
    end_date: document.getElementById("filter-end-date").value || undefined,
    group_by: document.getElementById("group-by-select").value || undefined,
  };
}

/** Rebuilds the option list of a <select>, preserving the current selection if still valid. */
function populateSelectOptions(selectEl, values, allLabel) {
  const current = selectEl.value;
  selectEl.innerHTML =
    `<option value="">${escapeHtml(allLabel)}</option>` +
    values
      .map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`)
      .join("");
  if (values.includes(current)) {
    selectEl.value = current;
  }
}

/** Applies the filter_options block from the analytics response to every filter dropdown. */
function updateAnalyticsFilterOptions(filterOptions) {
  populateSelectOptions(
    document.getElementById("filter-user"),
    filterOptions.user_names,
    "All users",
  );
  populateSelectOptions(
    document.getElementById("filter-session"),
    filterOptions.session_ids,
    "All sessions",
  );
  populateSelectOptions(
    document.getElementById("filter-organization"),
    filterOptions.organizations,
    "All organizations",
  );
  populateSelectOptions(
    document.getElementById("filter-repo"),
    filterOptions.repo_names,
    "All repositories",
  );
  populateSelectOptions(
    document.getElementById("filter-project"),
    filterOptions.project_names,
    "All projects",
  );
  populateSelectOptions(
    document.getElementById("filter-branch"),
    filterOptions.branches,
    "All branches",
  );
  populateSelectOptions(
    document.getElementById("filter-scm-type"),
    filterOptions.scm_types,
    "All SCM types",
  );
  populateSelectOptions(
    document.getElementById("filter-vcs-type"),
    filterOptions.vcs_types,
    "All VCS types",
  );
}

function renderAnalyticsSummary(data) {
  const turnCount = data.aggregate.reduce((sum, b) => sum + b.turn_count, 0);
  const totalTokens = data.aggregate.reduce(
    (sum, b) => sum + b.total_tokens,
    0,
  );
  const totalCredits = data.aggregate.reduce(
    (sum, b) => sum + b.total_credits_used,
    0,
  );

  document.getElementById("an-summary-records").textContent =
    data.total_matching_records.toLocaleString();
  document.getElementById("an-summary-turns").textContent =
    turnCount.toLocaleString();
  document.getElementById("an-summary-tokens").textContent =
    totalTokens.toLocaleString();
  document.getElementById("an-summary-credits").textContent =
    totalCredits.toFixed(4);
  document.getElementById("an-summary-groups").textContent =
    data.aggregate.length.toLocaleString();
}

function renderAggregateTable(data) {
  const tbody = document.getElementById("aggregate-table-body");
  if (!data.aggregate || data.aggregate.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="9" class="empty-row">No usage matches the current filters.</td></tr>';
    return;
  }

  tbody.innerHTML = data.aggregate
    .map(
      (b) => `
                <tr>
                    <td>${dash(b.organization)}</td>
                    <td>${dash(b.repo_name)}</td>
                    <td>${dash(b.branch)}</td>
                    <td>${dash(b.user_name)}</td>
                    <td class="session-id-cell">${dash(b.session_id)}</td>
                    <td>${b.turn_count.toLocaleString()}</td>
                    <td>${b.total_tokens.toLocaleString()}</td>
                    <td>${b.total_credits_used.toFixed(4)}</td>
                    <td>${dash(b.last_seen_at)}</td>
                </tr>
            `,
    )
    .join("");
}

function renderAnalyticsRecordsTable(data) {
  const tbody = document.getElementById("telemetry-table-body");
  if (!data.records || data.records.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="13" class="empty-row">No telemetry data matches the current filters.</td></tr>';
    return;
  }

  tbody.innerHTML = data.records
    .map((r, index) => {
      const hasDetail = r.request_payload || r.response_payload;
      const detailBtn = hasDetail
        ? `<button class="link-btn" data-index="${index}">view</button>`
        : "&mdash;";
      return `
                <tr>
                    <td>${escapeHtml(r.timestamp)}</td>
                    <td>${escapeHtml(r.type)}</td>
                    <td class="session-id-cell">${escapeHtml(r.session_id)}</td>
                    <td>${dash(r.organization)}</td>
                    <td>${dash(r.repo_name)}</td>
                    <td>${dash(r.branch)}</td>
                    <td>${dash(r.scm_type)}</td>
                    <td>${escapeHtml(r.model || "")}</td>
                    <td>${escapeHtml(r.prompt_tokens_estimated ?? "")}</td>
                    <td>${escapeHtml(r.response_tokens_estimated ?? "")}</td>
                    <td>${r.credits_used != null ? r.credits_used.toFixed(4) : ""}</td>
                    <td>${escapeHtml(r.status || "")}</td>
                    <td>${detailBtn}</td>
                </tr>
            `;
    })
    .join("");

  tbody.querySelectorAll(".link-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const record = data.records[Number(btn.dataset.index)];
      const parts = [];
      if (record.request_payload)
        parts.push(`REQUEST:\n${record.request_payload}`);
      if (record.response_payload)
        parts.push(`RESPONSE:\n${record.response_payload}`);
      showModal(
        "Record detail",
        `<pre>${escapeHtml(parts.join("\n\n"))}</pre>`,
      );
    });
  });
}

// ---------------------------------------------------------------------
// Charts (hand-rolled inline SVG -- no charting library dependency)
// ---------------------------------------------------------------------

const CHART_COLORS = [
  "#4f8cff",
  "#33c481",
  "#e5a63b",
  "#e5534b",
  "#7c5cff",
  "#3bc9db",
  "#f783ac",
  "#95c11f",
];

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  return el;
}

function renderEmptyChart(svg, message) {
  svg.innerHTML = "";
  const viewBox = svg.viewBox.baseVal;
  const text = svgEl("text", {
    x: viewBox.width / 2,
    y: viewBox.height / 2,
    class: "chart-empty",
  });
  text.textContent = message;
  svg.appendChild(text);
}

/** Renders a simple line+area chart for a daily time series onto the given <svg>. */
function renderLineChart(svg, points, valueKey, color, formatValue) {
  if (!points || points.length === 0) {
    renderEmptyChart(svg, "No data for the current filters");
    return;
  }
  svg.innerHTML = "";

  const viewBox = svg.viewBox.baseVal;
  const width = viewBox.width;
  const height = viewBox.height;
  const paddingLeft = 40;
  const paddingRight = 12;
  const paddingTop = 14;
  const paddingBottom = 26;
  const plotWidth = width - paddingLeft - paddingRight;
  const plotHeight = height - paddingTop - paddingBottom;

  const values = points.map((p) => p[valueKey]);
  const maxValue = Math.max(...values, 0.0001);

  const xStep = points.length > 1 ? plotWidth / (points.length - 1) : 0;
  const xFor = (i) => paddingLeft + i * xStep;
  const yFor = (v) => paddingTop + plotHeight - (v / maxValue) * plotHeight;

  // Y-axis gridlines + labels (0%, 50%, 100% of max)
  [0, 0.5, 1].forEach((fraction) => {
    const y = paddingTop + plotHeight - fraction * plotHeight;
    svg.appendChild(
      svgEl("line", {
        x1: paddingLeft,
        x2: width - paddingRight,
        y1: y,
        y2: y,
        stroke: "#2a2e3b",
        "stroke-width": 1,
      }),
    );
    const label = svgEl("text", { x: 4, y: y + 3, class: "chart-axis-label" });
    label.textContent = formatValue(maxValue * fraction);
    svg.appendChild(label);
  });

  // Area fill under the line
  const areaPoints = points
    .map((p, i) => `${xFor(i)},${yFor(p[valueKey])}`)
    .join(" ");
  const areaPath = `M${paddingLeft},${paddingTop + plotHeight} L${areaPoints} L${xFor(points.length - 1)},${paddingTop + plotHeight} Z`;
  svg.appendChild(
    svgEl("path", { d: areaPath, fill: color, opacity: 0.12, stroke: "none" }),
  );

  // The line itself
  const linePoints = points
    .map((p, i) => `${xFor(i)},${yFor(p[valueKey])}`)
    .join(" ");
  svg.appendChild(
    svgEl("polyline", {
      points: linePoints,
      fill: "none",
      stroke: color,
      "stroke-width": 2,
    }),
  );

  // Data point markers with a native tooltip (title) showing exact value + date
  points.forEach((p, i) => {
    const circle = svgEl("circle", {
      cx: xFor(i),
      cy: yFor(p[valueKey]),
      r: 3.5,
      fill: color,
      class: "chart-tooltip-target",
    });
    const title = svgEl("title", {});
    title.textContent = `${p.date}: ${formatValue(p[valueKey])}`;
    circle.appendChild(title);
    svg.appendChild(circle);
  });

  // X-axis labels: first, middle, last date to avoid crowding
  const labelIndexes =
    points.length <= 2
      ? points.map((_, i) => i)
      : [0, Math.floor((points.length - 1) / 2), points.length - 1];
  new Set(labelIndexes).forEach((i) => {
    const label = svgEl("text", {
      x: xFor(i),
      y: height - 6,
      class: "chart-axis-label",
      "text-anchor":
        i === 0 ? "start" : i === points.length - 1 ? "end" : "middle",
    });
    label.textContent = points[i].date;
    svg.appendChild(label);
  });
}

/** Renders a horizontal bar chart showing each aggregate bucket's share of total credits, grouped by repository. */
function renderProjectBarChart(svg, aggregateBuckets) {
  // Roll aggregate buckets up by repo_name (the aggregate table may
  // already be grouped more finely, e.g. down to session level).
  const byRepo = new Map();
  for (const bucket of aggregateBuckets) {
    const key = bucket.repo_name || "(no repository detected)";
    byRepo.set(key, (byRepo.get(key) || 0) + bucket.total_credits_used);
  }
  const rows = Array.from(byRepo.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8); // top 8 repositories keeps the chart readable

  if (rows.length === 0 || rows.every(([, credits]) => credits === 0)) {
    renderEmptyChart(svg, "No credit usage for the current filters");
    return;
  }
  svg.innerHTML = "";

  const viewBox = svg.viewBox.baseVal;
  const width = viewBox.width;
  const height = viewBox.height;
  const paddingLeft = 150;
  const paddingRight = 70;
  const paddingTop = 10;
  const paddingBottom = 10;
  const rowHeight = (height - paddingTop - paddingBottom) / rows.length;
  const barHeight = Math.min(24, rowHeight * 0.6);
  const plotWidth = width - paddingLeft - paddingRight;
  const maxCredits = Math.max(...rows.map(([, credits]) => credits), 0.0001);

  rows.forEach(([name, credits], i) => {
    const y = paddingTop + i * rowHeight + (rowHeight - barHeight) / 2;
    const barWidth = (credits / maxCredits) * plotWidth;
    const color = CHART_COLORS[i % CHART_COLORS.length];

    const label = svgEl("text", {
      x: paddingLeft - 10,
      y: y + barHeight / 2 + 4,
      class: "chart-bar-label",
      "text-anchor": "end",
    });
    label.textContent = name.length > 22 ? name.slice(0, 21) + "…" : name;
    const titleEl = svgEl("title", {});
    titleEl.textContent = name;
    label.appendChild(titleEl);
    svg.appendChild(label);

    const bar = svgEl("rect", {
      x: paddingLeft,
      y,
      width: Math.max(barWidth, 1),
      height: barHeight,
      rx: 4,
      fill: color,
      class: "chart-tooltip-target",
    });
    const barTitle = svgEl("title", {});
    barTitle.textContent = `${name}: ${credits.toFixed(4)} credits`;
    bar.appendChild(barTitle);
    svg.appendChild(bar);

    const valueLabel = svgEl("text", {
      x: paddingLeft + barWidth + 8,
      y: y + barHeight / 2 + 4,
      class: "chart-bar-label",
    });
    valueLabel.textContent = credits.toFixed(2);
    svg.appendChild(valueLabel);
  });
}

function renderCharts(data) {
  renderLineChart(
    document.getElementById("chart-tokens"),
    data.daily_series,
    "total_tokens",
    "#4f8cff",
    (v) => Math.round(v).toLocaleString(),
  );
  renderLineChart(
    document.getElementById("chart-credits"),
    data.daily_series,
    "total_credits_used",
    "#33c481",
    (v) => v.toFixed(2),
  );
  renderProjectBarChart(
    document.getElementById("chart-projects"),
    data.aggregate,
  );
}

// ---------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------

function activateTab(tabName) {
  activeTab = tabName;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const isActive = btn.dataset.tab === tabName;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`);
  });
  // Charts are sized from the SVG's own viewBox, but re-render on
  // activation in case the container was hidden (0 width) during the
  // last data refresh, which some browsers use to skip layout work.
  if (tabName === "charts" && latestAnalytics) {
    renderCharts(latestAnalytics);
  }
}

// ---------------------------------------------------------------------
// Fetch + orchestrate the analytics section
// ---------------------------------------------------------------------

async function refreshAnalytics() {
  try {
    const filters = readAnalyticsFilters();
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const recordLimit = document.getElementById("limit-select").value;
    params.set("record_limit", recordLimit);

    const data = await apiFetch(
      `/api/telemetry/analytics?${params.toString()}`,
    );
    latestAnalytics = data;

    renderAnalyticsSummary(data);
    renderAggregateTable(data);
    renderAnalyticsRecordsTable(data);
    renderCharts(data);
    updateAnalyticsFilterOptions(data.filter_options);
    setAuthStatus(true);
  } catch (error) {
    setAuthStatus(false);
    console.error("Failed to load analytics data:", error);
  }
}

function clearAnalyticsFilters() {
  [
    "filter-user",
    "filter-session",
    "filter-organization",
    "filter-repo",
    "filter-project",
    "filter-branch",
    "filter-scm-type",
    "filter-vcs-type",
  ].forEach((id) => {
    document.getElementById(id).value = "";
  });
  document.getElementById("filter-start-date").value = "";
  document.getElementById("filter-end-date").value = "";
  refreshAnalytics();
}

// =======================================================================
// Fleet Health section (machine -> user -> session, liveness monitoring)
// =======================================================================

function buildFleetTree(healthRecords, telemetryRecords) {
  // machine -> user -> session -> { health: {handler, watcher}, usage: {...} }
  const machines = new Map();

  function getSessionNode(machineName, userName, sessionId) {
    if (!machines.has(machineName)) {
      machines.set(machineName, { name: machineName, users: new Map() });
    }
    const machine = machines.get(machineName);

    if (!machine.users.has(userName)) {
      machine.users.set(userName, { name: userName, sessions: new Map() });
    }
    const user = machine.users.get(userName);

    const key = sessionId || "(none yet)";
    if (!user.sessions.has(key)) {
      user.sessions.set(key, {
        sessionId: key,
        health: {},
        creditsUsed: 0,
        promptTokens: 0,
        responseTokens: 0,
        turnCount: 0,
        lastActiveAt: null,
        models: new Set(),
      });
    }
    return user.sessions.get(key);
  }

  for (const record of healthRecords) {
    const node = getSessionNode(
      record.machine_name || "unknown",
      record.user_name || "unknown",
      record.session_id,
    );
    node.health[record.component] = record;
  }

  for (const record of telemetryRecords) {
    // Telemetry records don't carry machine_name/user_name directly
    // (see models.py), so we attribute them to a session using
    // whatever health record(s) already told us about that session.
    // If no health record exists yet for this session, group it under
    // "unknown" machine/user rather than dropping the usage data.
    let matched = null;
    outer: for (const machine of machines.values()) {
      for (const user of machine.users.values()) {
        if (user.sessions.has(record.session_id)) {
          matched = user.sessions.get(record.session_id);
          break outer;
        }
      }
    }
    const node =
      matched || getSessionNode("unknown", "unknown", record.session_id);

    if (record.credits_used != null) node.creditsUsed += record.credits_used;
    if (record.prompt_tokens_estimated != null)
      node.promptTokens += record.prompt_tokens_estimated;
    if (record.response_tokens_estimated != null)
      node.responseTokens += record.response_tokens_estimated;
    if (record.type === "turn_tokens") node.turnCount += 1;
    if (record.model) node.models.add(record.model);

    if (!node.lastActiveAt || record.timestamp > node.lastActiveAt) {
      node.lastActiveAt = record.timestamp;
    }
  }

  return machines;
}

function renderFleetSummary(machines, healthRecords) {
  let userSet = new Set();
  let sessionCount = 0;
  let activeComponents = 0;
  let totalCredits = 0;
  let totalTokens = 0;

  for (const machine of machines.values()) {
    for (const user of machine.users.values()) {
      userSet.add(user.name);
      for (const session of user.sessions.values()) {
        sessionCount += 1;
        totalCredits += session.creditsUsed;
        totalTokens += session.promptTokens + session.responseTokens;
      }
    }
  }
  activeComponents = healthRecords.filter((r) => r.is_alive).length;

  document.getElementById("summary-machines").textContent = machines.size;
  document.getElementById("summary-users").textContent = userSet.size;
  document.getElementById("summary-sessions").textContent = sessionCount;
  document.getElementById("summary-active").textContent = activeComponents;
  document.getElementById("summary-credits").textContent =
    totalCredits.toFixed(2);
  document.getElementById("summary-tokens").textContent =
    totalTokens.toLocaleString();
}

function statusIconHtml(sessionId, component, healthRecord) {
  const label = component === "handler" ? "H" : "W";
  let cls = "unknown";
  if (healthRecord) {
    cls = healthRecord.is_alive ? "alive" : "dead";
  }
  return `<button type="button" class="status-icon-btn ${cls}" data-session-id="${escapeHtml(sessionId)}" data-component="${escapeHtml(component)}" title="${component} status">${label}</button>`;
}

function renderFleetGroups(machines) {
  const container = document.getElementById("summary-groups");

  if (machines.size === 0) {
    container.innerHTML = '<p class="empty-row">No data yet.</p>';
    return;
  }

  const machineFilter = document.getElementById("machine-health-filter").value;

  let html = "";
  for (const machine of machines.values()) {
    if (machineFilter && machine.name !== machineFilter) continue;

    const visibleUsers = Array.from(machine.users.values());
    if (visibleUsers.length === 0) continue;

    html += `<div class="machine-group">`;
    html += `<div class="machine-group-header"><span class="machine-icon">&#128421;</span> ${escapeHtml(machine.name)}
            <span class="group-meta">${visibleUsers.length} user(s)</span>
            <button type="button" class="clear-btn clear-btn-telemetry" data-clear-type="telemetry-machine" data-machine-name="${escapeHtml(machine.name)}">Clear telemetry</button>
        </div>`;

    for (const user of visibleUsers) {
      const sessions = Array.from(user.sessions.values());
      html += `<div class="user-group">`;
      html += `<div class="user-group-header">
                <span class="user-name">&#128100; ${escapeHtml(user.name)}</span>
                <span class="group-meta">${sessions.length} session(s)</span>
                <button type="button" class="clear-btn" data-clear-type="health-user" data-user-name="${escapeHtml(user.name)}">Clear heartbeats</button>
                <button type="button" class="clear-btn clear-btn-telemetry" data-clear-type="telemetry-user" data-user-name="${escapeHtml(user.name)}">Clear telemetry</button>
            </div>`;

      html += `<table class="session-table"><thead><tr>
                <th>Status</th>
                <th>Session ID</th>
                <th>Model(s)</th>
                <th>Turns</th>
                <th>Tokens (est.)</th>
                <th>Credits Used</th>
                <th>Last Active</th>
                <th></th>
            </tr></thead><tbody>`;

      for (const session of sessions) {
        const icons = COMPONENTS.map((component) =>
          statusIconHtml(
            session.sessionId,
            component,
            session.health[component],
          ),
        ).join("");
        const models = Array.from(session.models).join(", ") || "&mdash;";
        const lastActive = session.lastActiveAt
          ? escapeHtml(session.lastActiveAt)
          : "&mdash;";
        const totalTokens = session.promptTokens + session.responseTokens;

        html += `<tr>
                    <td><span class="status-icons">${icons}</span></td>
                    <td class="session-id-cell">${escapeHtml(session.sessionId)}</td>
                    <td>${models}</td>
                    <td>${session.turnCount}</td>
                    <td>${totalTokens.toLocaleString()}</td>
                    <td>${session.creditsUsed.toFixed(4)}</td>
                    <td>${lastActive}</td>
                    <td>
                        <button type="button" class="clear-btn" data-clear-type="health-session" data-session-id="${escapeHtml(session.sessionId)}">Clear heartbeats</button>
                        <button type="button" class="clear-btn clear-btn-telemetry" data-clear-type="telemetry-session" data-session-id="${escapeHtml(session.sessionId)}">Clear telemetry</button>
                    </td>
                </tr>`;
      }

      html += `</tbody></table></div>`;
    }

    html += `</div>`;
  }

  container.innerHTML =
    html || '<p class="empty-row">No sessions match the current filter.</p>';

  container.querySelectorAll(".status-icon-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sessionId = btn.dataset.sessionId;
      const component = btn.dataset.component;
      showHealthDetail(sessionId, component);
    });
  });

  container.querySelectorAll(".clear-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const clearType = btn.dataset.clearType;
      switch (clearType) {
        case "health-session":
          requestClearHeartbeats({ sessionId: btn.dataset.sessionId });
          break;
        case "health-user":
          requestClearHeartbeats({ userName: btn.dataset.userName });
          break;
        case "telemetry-session":
          requestClearTelemetry({ sessionId: btn.dataset.sessionId });
          break;
        case "telemetry-user":
          requestClearTelemetry({ userName: btn.dataset.userName });
          break;
        case "telemetry-machine":
          requestClearTelemetry({ machineName: btn.dataset.machineName });
          break;
        default:
          break;
      }
    });
  });
}

/** Populates the machine filter dropdown from the aggregated tree. */
function updateMachineFilterOptions(machines) {
  const machineSelect = document.getElementById("machine-health-filter");
  const currentMachine = machineSelect.value;
  const machineNames = Array.from(machines.keys());

  machineSelect.innerHTML =
    '<option value="">All machines</option>' +
    machineNames
      .map(
        (name) =>
          `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`,
      )
      .join("");

  if (machineNames.includes(currentMachine))
    machineSelect.value = currentMachine;
}

// ---------------------------------------------------------------------
// Detail modal (health heartbeat detail + raw telemetry payload detail)
// ---------------------------------------------------------------------

function showModal(title, bodyHtml) {
  document.getElementById("detail-modal-title").textContent = title;
  document.getElementById("detail-modal-body").innerHTML = bodyHtml;
  document.getElementById("detail-modal").classList.remove("hidden");
}

function hideDetailModal() {
  document.getElementById("detail-modal").classList.add("hidden");
}

function showHealthDetail(sessionId, component) {
  const record = latestHealthRecords.find(
    (r) =>
      (r.session_id || "(none yet)") === sessionId && r.component === component,
  );

  if (!record) {
    showModal(
      `${component} status`,
      `<p>No heartbeat has ever been recorded for this session/component pair.</p>`,
    );
    return;
  }

  const statusLabel = record.is_alive ? "Alive" : "Stale / not responding";
  const dl = `
        <dl>
            <dt>Component</dt><dd>${escapeHtml(record.component)}</dd>
            <dt>Status</dt><dd><span class="dot ${record.is_alive ? "alive" : "dead"}"></span>${escapeHtml(statusLabel)}</dd>
            <dt>Session ID</dt><dd>${escapeHtml(record.session_id || "(none yet)")}</dd>
            <dt>Machine</dt><dd>${escapeHtml(record.machine_name)}</dd>
            <dt>User</dt><dd>${escapeHtml(record.user_name)}</dd>
            <dt>PID</dt><dd>${escapeHtml(record.pid)}</dd>
            <dt>Organization</dt><dd>${dash(record.organization)}</dd>
            <dt>Repository</dt><dd>${dash(record.repo_name)}</dd>
            <dt>Branch</dt><dd>${dash(record.branch)}</dd>
            <dt>SCM Type</dt><dd>${dash(record.scm_type)}</dd>
            <dt>VCS Type</dt><dd>${dash(record.vcs_type)}</dd>
            <dt>Last Heartbeat</dt><dd>${escapeHtml(record.last_heartbeat_at)}</dd>
            <dt>Seconds Ago</dt><dd>${formatSecondsAgo(record.seconds_since_last_heartbeat)}</dd>
        </dl>
    `;
  showModal(`${component} heartbeat detail`, dl);
}

// ---------------------------------------------------------------------
// Clear heartbeat / telemetry data (destructive actions -> always
// confirm first). Both share the same confirm modal; `pendingClearAction`
// records which endpoint and filter to use once the user confirms.
// ---------------------------------------------------------------------

function requestClearHeartbeats({ sessionId, userName }) {
  pendingClearAction = { kind: "health", sessionId, userName };
  const text = sessionId
    ? `This removes all stored heartbeat (health) data for session "${sessionId}". This does not affect the telemetry log.`
    : `This removes all stored heartbeat (health) data for every session belonging to user "${userName}". This does not affect the telemetry log.`;
  document.getElementById("confirm-modal-title").textContent =
    "Clear heartbeat data?";
  document.getElementById("confirm-modal-text").textContent = text;
  document.getElementById("confirm-modal").classList.remove("hidden");
}

function requestClearTelemetry({ sessionId, userName, machineName }) {
  pendingClearAction = { kind: "telemetry", sessionId, userName, machineName };
  let text;
  if (sessionId) {
    text = `This permanently removes all stored telemetry (token/credit usage) records for session "${sessionId}". This does not affect heartbeat/liveness data.`;
  } else if (userName) {
    text = `This permanently removes all stored telemetry (token/credit usage) records for every session belonging to user "${userName}". This does not affect heartbeat/liveness data.`;
  } else {
    text = `This permanently removes all stored telemetry (token/credit usage) records for every session reported from machine "${machineName}". This does not affect heartbeat/liveness data.`;
  }
  document.getElementById("confirm-modal-title").textContent =
    "Clear telemetry data?";
  document.getElementById("confirm-modal-text").textContent = text;
  document.getElementById("confirm-modal").classList.remove("hidden");
}

function hideConfirmModal() {
  pendingClearAction = null;
  document.getElementById("confirm-modal").classList.add("hidden");
}

async function performPendingClear() {
  if (!pendingClearAction) return;
  const { kind, sessionId, userName, machineName } = pendingClearAction;

  const params = new URLSearchParams();
  if (sessionId)
    params.set("session_id", sessionId === "(none yet)" ? "none" : sessionId);
  if (userName) params.set("user_name", userName);
  if (machineName) params.set("machine_name", machineName);

  const path = kind === "telemetry" ? "/api/telemetry" : "/api/health";

  try {
    await apiFetch(`${path}?${params.toString()}`, { method: "DELETE" });
    hideConfirmModal();
    await refreshAll();
  } catch (error) {
    console.error(`Failed to clear ${kind} data:`, error);
    alert(`Failed to clear ${kind} data: ${error.message}`);
    hideConfirmModal();
  }
}

// ---------------------------------------------------------------------
// Data refresh / polling
// ---------------------------------------------------------------------

async function refreshHealth() {
  try {
    const data = await apiFetch("/api/health");
    latestHealthRecords = data.records || [];
    setAuthStatus(true);
  } catch (error) {
    setAuthStatus(false);
    console.error("Failed to load health data:", error);
  }
}

async function refreshFleetTelemetry() {
  try {
    const data = await apiFetch("/api/telemetry?limit=2000");
    latestTelemetryRecords = data.records || [];
    setAuthStatus(true);
  } catch (error) {
    setAuthStatus(false);
    console.error("Failed to load telemetry data:", error);
  }
}

async function refreshAll() {
  await Promise.all([
    refreshHealth(),
    refreshFleetTelemetry(),
    refreshAnalytics(),
  ]);
  const machines = buildFleetTree(latestHealthRecords, latestTelemetryRecords);
  renderFleetSummary(machines, latestHealthRecords);
  updateMachineFilterOptions(machines);
  renderFleetGroups(machines);
}

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = window.setInterval(refreshAll, AUTO_REFRESH_INTERVAL_MS);
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    window.clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

function initEventHandlers() {
  document.getElementById("save-key-btn").addEventListener("click", () => {
    const value = document.getElementById("api-key-input").value.trim();
    saveApiKey(value);
    refreshAll();
  });

  document.getElementById("refresh-btn").addEventListener("click", refreshAll);
  document
    .getElementById("analytics-refresh-btn")
    .addEventListener("click", refreshAnalytics);

  // Analytics filter bar: any change re-queries the analytics endpoint.
  [
    "filter-user",
    "filter-session",
    "filter-organization",
    "filter-repo",
    "filter-project",
    "filter-branch",
    "filter-scm-type",
    "filter-vcs-type",
    "filter-start-date",
    "filter-end-date",
    "group-by-select",
    "limit-select",
  ].forEach((id) => {
    document.getElementById(id).addEventListener("change", refreshAnalytics);
  });
  document
    .getElementById("filter-clear-btn")
    .addEventListener("click", clearAnalyticsFilters);

  // Filter bar collapse/expand toggle
  (function () {
    const toggle = document.getElementById("filter-bar-toggle");
    const body = document.getElementById("filter-bar-body");
    const hint = document.getElementById("filter-bar-hint");
    const title = toggle.querySelector(".filter-bar-title");

    function setExpanded(expanded) {
      toggle.setAttribute("aria-expanded", String(expanded));
      body.classList.toggle("hidden", !expanded);
      title.textContent = (expanded ? "▲" : "▼") + " Filters";
      hint.textContent = expanded ? "Click to collapse" : "Click to expand";
    }

    toggle.addEventListener("click", () =>
      setExpanded(toggle.getAttribute("aria-expanded") !== "true"),
    );
    toggle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle.click();
      }
    });

    setExpanded(false); // collapsed by default
  })();

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  document
    .getElementById("machine-health-filter")
    .addEventListener("change", () => {
      const machines = buildFleetTree(
        latestHealthRecords,
        latestTelemetryRecords,
      );
      renderFleetGroups(machines);
    });

  document
    .getElementById("auto-refresh-toggle")
    .addEventListener("change", (event) => {
      if (event.target.checked) {
        startAutoRefresh();
      } else {
        stopAutoRefresh();
      }
    });

  document
    .getElementById("detail-modal-close")
    .addEventListener("click", hideDetailModal);
  document.getElementById("detail-modal").addEventListener("click", (event) => {
    if (event.target.id === "detail-modal") hideDetailModal();
  });

  document
    .getElementById("confirm-modal-cancel")
    .addEventListener("click", hideConfirmModal);
  document
    .getElementById("confirm-modal-confirm")
    .addEventListener("click", performPendingClear);
  document
    .getElementById("confirm-modal")
    .addEventListener("click", (event) => {
      if (event.target.id === "confirm-modal") hideConfirmModal();
    });
}

function init() {
  const savedKey = getSavedApiKey();
  if (savedKey) {
    document.getElementById("api-key-input").value = savedKey;
  }
  initEventHandlers();
  refreshAll();
  startAutoRefresh();
}

document.addEventListener("DOMContentLoaded", init);
