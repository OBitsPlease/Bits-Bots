// Live Trading page: settings, risk limits, manual orders, BitsBots-hosted
// grid bots, and the startup risk notice. Paper trading lives elsewhere and
// is never affected by anything on this page.

let liveStatus = null;
let liveOrders = [];
let liveBots = [];
let currentPreview = null;
let previewTimer = null;
let currentGridPlan = null;

const LIMIT_FIELDS = [
  ["max_order_value", "Maximum order value", "Largest value of a single buy order, in USDT/USDC. Larger buys are refused.", "decimal"],
  ["max_total_exposure", "Maximum total exposure", "Total of open BitsBots buy orders plus coins your bots hold (at cost). New buys stop once this is reached.", "decimal"],
  ["max_bot_allocation", "Maximum per-bot allocation", "The most any one grid bot may commit, including open buys and holdings. Trailing Up never exceeds it.", "decimal"],
  ["reserve_balance", "Reserve balance", "USDT/USDC that must stay unused after every buy, so one bot cannot drain the account.", "decimal"],
  ["daily_loss_limit", "Daily loss limit", "Stops new buys once today's realized losses (UTC day) reach this amount. Protective sells are still allowed.", "decimal"],
  ["max_open_orders", "Maximum open orders", "Most BitsBots orders that may be open at the same time across all bots and manual orders.", "integer"],
  ["max_running_bots", "Maximum running bots", "Most grid bots that may run at once in each mode.", "integer"],
  ["max_daily_orders", "Maximum orders per day", "Caps how many orders BitsBots may submit per UTC day. Protects against runaway behavior.", "integer"],
  ["stale_data_seconds", "Market data age limit (seconds)", "Orders are refused if market data is older than this. Old data can produce orders at unsafe prices.", "integer"],
  ["max_consecutive_errors", "Consecutive error limit", "A bot halts after this many failed market-data reads in a row.", "integer"],
  ["max_price_deviation", "Manual price band (%)", "Manual buys more than this far above, or sells this far below, the last price are refused to catch typing mistakes.", "percent"],
];

const ORDER_STATE_CLASS = {
  open: "state-ok", partially_filled: "state-ok", filled: "state-ok",
  pending: "state-warn", submitting: "state-warn", cancel_requested: "state-warn",
  cancelled: "state-muted", not_found: "state-muted",
  rejected: "state-bad", submission_unknown: "state-bad", needs_review: "state-bad",
};
const BOT_STATE_CLASS = {
  running: "state-ok", recentering: "state-warn", stopping: "state-warn",
  armed: "state-muted", paused: "state-muted", stopped: "state-muted", halted: "state-bad",
};

const decimalText = value => {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-US", { maximumSignificantDigits: 10 })
    : escapeHtml(value);
};
const modeLabel = mode => (mode === "live" ? "LIVE" : "DRY RUN");
const timeText = value => (value ? new Date(Number(value)).toLocaleString() : "—");

async function liveApi(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map(item => item.msg || "Invalid value").join(". ")
      : payload.detail || `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return payload;
}

function showLiveError(message) {
  const element = document.querySelector("#liveError");
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
  if (message) element.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showFeedback(selector, message, kind = "") {
  const element = document.querySelector(selector);
  element.textContent = message || "";
  element.classList.toggle("hidden", !message);
  element.classList.toggle("feedback-error", kind === "error");
  element.classList.toggle("feedback-success", kind === "success");
  if (message) element.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const friendlyError = error =>
  error.message === "Password confirmation failed" ? "Incorrect BitsBots password." : error.message;

function askPassword(message) {
  const dialog = document.querySelector("#passwordDialog");
  const input = document.querySelector("#passwordDialogInput");
  document.querySelector("#passwordDialogMessage").textContent = message;
  input.value = "";
  return new Promise(resolve => {
    const finish = () => {
      dialog.removeEventListener("close", finish);
      const value = dialog.returnValue === "ok" ? input.value : null;
      input.value = "";
      resolve(value || null);
    };
    dialog.returnValue = "";
    dialog.addEventListener("close", finish);
    dialog.showModal();
    input.focus();
  });
}

// ------------------------------------------------------------------ status
function renderLiveStatus() {
  if (!liveStatus) return;
  const s = liveStatus;
  const banner = document.querySelector("#liveModeBanner");
  banner.textContent = s.kill_switch ? "KILL SWITCH ENGAGED" : s.live_enabled ? "LIVE TRADING ON" : "LIVE TRADING OFF";
  banner.className = `mode-banner ${s.kill_switch ? "mode-kill" : s.live_enabled ? "mode-live" : ""}`;
  const pill = document.querySelector("#modePill");
  if (pill) {
    pill.textContent = s.live_enabled ? "LIVE TRADING ON · NONKYC" : "READ-ONLY · NONKYC";
    pill.classList.toggle("live-pill", Boolean(s.live_enabled));
  }
  const killButton = document.querySelector("#killSwitchButton");
  killButton.textContent = s.kill_switch ? "Turn kill switch off" : "Engage kill switch";
  const dry = s.modes.dry_run;
  const live = s.modes.live;
  const balances = (s.dry_run_balances || [])
    .filter(row => Number(row.available) + Number(row.held) > 0)
    .map(row => `${escapeHtml(row.asset)} ${decimalText(row.available)}`)
    .join(" · ") || "—";
  document.querySelector("#liveStatusList").innerHTML = `
    <div><dt>NonKYC key</dt><dd>${s.credentials_connected ? "Connected" : '<span class="caution">Not connected (Settings)</span>'}</dd></div>
    <div><dt>Live trading</dt><dd class="${s.live_enabled ? "negative" : ""}">${s.live_enabled ? `ON since ${timeText(s.enabled_at)}` : "Off"}</dd></div>
    <div><dt>Kill switch</dt><dd class="${s.kill_switch ? "negative" : ""}">${s.kill_switch ? "Engaged" : "Off"}</dd></div>
    <div><dt>Expected public IP</dt><dd>${s.ip_address ? `${escapeHtml(s.ip_address)} <small class="muted">(confirmed by you ${timeText(s.ip_confirmed_at)})</small>` : "—"}</dd></div>
    <div><dt>Live exposure</dt><dd>${decimalText(live.exposure)} · ${live.open_orders} open · today P&amp;L ${decimalText(live.daily_realized_pnl)}</dd></div>
    <div><dt>Dry-run exposure</dt><dd>${decimalText(dry.exposure)} · ${dry.open_orders} open · today P&amp;L ${decimalText(dry.daily_realized_pnl)}</dd></div>
    <div><dt>Dry-run balances</dt><dd>${balances}</dd></div>
    <div><dt>Exchange notes</dt><dd class="muted">${s.capabilities.nonkyc.notes.map(escapeHtml).join("<br>")}</dd></div>`;
  document.querySelector("#liveEnabledView").classList.toggle("hidden", !s.live_enabled);
  document.querySelector("#liveEnableForm").classList.toggle("hidden", Boolean(s.live_enabled));
  const reconfirm = document.querySelector("#liveIpReconfirm");
  if (document.activeElement !== reconfirm) reconfirm.value = s.ip_address || "";
  renderLimitsForm();
}

function renderLimitsForm() {
  const form = document.querySelector("#liveLimitsForm");
  if (form.contains(document.activeElement)) return;
  const limits = liveStatus.limits;
  form.innerHTML = LIMIT_FIELDS.map(([key, label, help, kind]) => {
    const raw = limits[key];
    const value = kind === "percent" ? Number(raw) * 100 : raw;
    return `<label class="live-field" data-help="${escapeHtml(help)}">${escapeHtml(label)}
      <input name="${key}" inputmode="decimal" value="${escapeHtml(String(value))}" aria-description="${escapeHtml(help)}">
      <small>${escapeHtml(help)}</small></label>`;
  }).join("") + `<button type="submit" class="primary">Save limits</button>`;
  installHelpTips(form);
}

async function refreshLive() {
  try {
    const modeFilter = document.querySelector("#orderModeFilter").value;
    const [status, orders, bots] = await Promise.all([
      liveApi("/api/live/status"),
      liveApi(`/api/live/orders${modeFilter ? `?mode=${modeFilter}` : ""}`),
      liveApi("/api/live/bots"),
    ]);
    liveStatus = status;
    liveOrders = orders.orders;
    liveBots = bots.bots;
    renderLiveStatus();
    renderLiveOrders();
    renderLiveBots();
    renderDashboardBots();
    if (dashboardBotId && !liveBots.some(bot => bot.id === dashboardBotId)) {
      clearDashboardBot();
    } else if (dashboardBotId && !dashboardRestored) {
      dashboardRestored = true;
      await selectDashboardBot(dashboardBotId);
    } else if (dashboardBotId && isViewVisible("dashboard")) {
      await refreshDashboardActivity();
    }
  } catch (error) {
    showLiveError(error.message);
  }
}

function renderMarketList() {
  const list = document.querySelector("#liveMarketList");
  if (!list || typeof availableChartMarkets === "undefined") return;
  list.innerHTML = availableChartMarkets
    .filter(market => {
      const [base, quote] = market.symbol.split("/");
      return ["USDT", "USDC"].includes(quote) && !["USDT", "USDC"].includes(base);
    })
    .map(market => `<option value="${escapeHtml(market.symbol)}"></option>`)
    .join("");
}

// ------------------------------------------------------------------ orders
function renderLiveOrders() {
  document.querySelector("#liveOrderRows").innerHTML = liveOrders.map(order => {
    const actions = [];
    if (["open", "partially_filled", "cancel_requested"].includes(order.state)) {
      actions.push(`<button type="button" data-order-action="cancel" data-id="${order.id}">Cancel</button>`);
    }
    if (["open", "partially_filled", "submission_unknown", "cancel_requested"].includes(order.state)) {
      actions.push(`<button type="button" data-order-action="reconcile" data-id="${order.id}" data-help="Ask the exchange for this order's current state.">Recheck</button>`);
    }
    if (["needs_review", "submission_unknown"].includes(order.state)) {
      actions.push(`<button type="button" data-order-action="resolve" data-id="${order.id}" data-help="After checking your NonKYC order history yourself, link the matching exchange order or confirm none exists.">Resolve</button>`);
    }
    const stateClass = ORDER_STATE_CLASS[order.state] || "state-muted";
    return `<tr class="order-row" data-order-row="${order.id}" tabindex="0" title="Click for order details">
      <td>${timeText(order.created_at)}</td>
      <td><span class="mode-tag ${order.mode === "live" ? "mode-tag-live" : ""}">${modeLabel(order.mode)}</span></td>
      <td>${escapeHtml(sourceLabel(order))}</td>
      <td>${escapeHtml(order.symbol)}</td>
      <td class="${order.side === "buy" ? "positive" : "negative"}">${escapeHtml(order.side.toUpperCase())}</td>
      <td>${decimalText(order.price)}</td><td>${decimalText(order.quantity)}</td><td>${decimalText(order.executed_quantity)}</td>
      <td><span class="state-tag ${stateClass}" title="${escapeHtml(order.error || "")}">${escapeHtml(order.state.replace("_", " "))}</span></td>
      <td class="row-actions">${actions.join("")}</td></tr>`;
  }).join("") || `<tr><td colspan="10" class="muted">No BitsBots orders yet. Orders from native NonKYC bots and manual NonKYC trading are not listed here.</td></tr>`;
  installHelpTips(document.querySelector("#liveOrderRows"));
}

function sourceLabel(order) {
  const bot = liveBots.find(item => item.id === order.bot_id);
  const botName = bot ? `${bot.name}` : `bot #${order.bot_id}`;
  if (order.purpose === "manual") return "Manual (one-time)";
  if (order.purpose === "grid_buy") return `Grid buy · ${botName}`;
  if (order.purpose === "grid_sell") return `Grid sell · ${botName}`;
  if (order.purpose === "liquidation") return `Stop sell · ${botName}`;
  return order.purpose.replace("_", " ");
}

const ORDER_EVENT_LABELS = {
  created: "Order created", submitting: "Sent", acknowledged: "Accepted", refreshed: "Status checked",
  blocked: "Blocked", rejected: "Rejected", interrupted: "Interrupted", submission_unknown: "Outcome unknown",
  needs_review: "Needs review", reconcile_pending: "Checking", not_found: "Not found", resolved: "Resolved",
  cancel_requested: "Cancel requested", cancel_unknown: "Cancel outcome unknown", cancel_failed: "Cancel failed",
};
const ORDER_STATE_TEXT = {
  filled: "Filled — the full quantity was bought or sold.",
  partially_filled: "Partly filled — the rest is still waiting on the order book.",
  open: "Open — waiting on the order book for the price to reach the limit.",
  pending: "Pending — about to be sent.", submitting: "Being sent.",
  cancelled: "Cancelled — anything shown as filled still happened; the rest did not.",
  cancel_requested: "Cancel requested — waiting for confirmation.",
  rejected: "Rejected — this order was not placed.", not_found: "Not placed — no matching order was found.",
  submission_unknown: "Outcome unknown — check your NonKYC order history, then use Resolve.",
  needs_review: "Needs review — check your NonKYC order history, then use Resolve.",
};
let detailOrder = null;

async function showOrderDetail(id) {
  const order = liveOrders.find(item => String(item.id) === String(id));
  if (!order) return;
  detailOrder = order;
  const dialog = document.querySelector("#orderDetailDialog");
  const body = document.querySelector("#orderDetailBody");
  const live = order.mode === "live";
  const filled = Number(order.executed_quantity);
  const value = filled * Number(order.price);
  const fee = value * (Number(liveStatus?.fee_rate) || 0);
  const [base, quote] = order.symbol.split("/");
  const bot = liveBots.find(item => item.id === order.bot_id);
  const what = order.purpose === "manual"
    ? `A one-time ${order.side} you placed yourself. It is <strong>not a bot</strong>: nothing else happens automatically after it fills.`
    : `Placed automatically by grid bot <strong>${escapeHtml(bot ? bot.name : `#${order.bot_id}`)}</strong>${order.purpose === "liquidation" ? " to sell its holdings when it stopped" : ""}.`;
  const effect = filled > 0
    ? (order.side === "buy"
      ? `Spent about ${value.toFixed(6)} ${escapeHtml(quote)} (plus ~${fee.toFixed(6)} fee) for ${decimalText(order.executed_quantity)} ${escapeHtml(base)}.`
      : `Sold ${decimalText(order.executed_quantity)} ${escapeHtml(base)} for about ${value.toFixed(6)} ${escapeHtml(quote)} (minus ~${fee.toFixed(6)} fee).`)
      + (live ? "" : ` These are practice funds — see <em>Dry-run balances</em> under Status on the Live Trading page.`)
    : "Nothing has been bought or sold yet.";
  document.querySelector("#orderDetailKicker").textContent = `${modeLabel(order.mode)} ORDER #${order.id}`;
  document.querySelector("#orderDetailTitle").textContent =
    `${order.side === "buy" ? "Buy" : "Sell"} ${decimalText(order.quantity)} ${base} @ ${decimalText(order.price)}`;
  body.innerHTML = `
    ${live ? '<div class="live-warning">LIVE — a real order on NonKYC.</div>' : '<div class="dry-warning">DRY RUN — simulated with real prices. No real money was used.</div>'}
    <p class="order-detail-what">${what}</p>
    <p class="order-detail-what">${effect}</p>
    <dl class="preview-grid">
      <div><dt>Market</dt><dd>${escapeHtml(order.symbol)}</dd></div>
      <div><dt>Side</dt><dd class="${order.side === "buy" ? "positive" : "negative"}">${escapeHtml(order.side.toUpperCase())}</dd></div>
      <div><dt>Limit price</dt><dd>${decimalText(order.price)} ${escapeHtml(quote)}</dd></div>
      <div><dt>Quantity</dt><dd>${decimalText(order.quantity)} ${escapeHtml(base)}</dd></div>
      <div><dt>Filled</dt><dd>${decimalText(order.executed_quantity)} ${escapeHtml(base)}</dd></div>
      <div><dt>Filled value</dt><dd>${value.toFixed(6)} ${escapeHtml(quote)}</dd></div>
      <div><dt>State</dt><dd>${escapeHtml(ORDER_STATE_TEXT[order.state] || order.state)}</dd></div>
      <div><dt>Placed</dt><dd>${timeText(order.created_at)}</dd></div>
      <div><dt>Filled at</dt><dd>${order.filled_at ? timeText(order.filled_at) : "—"}</dd></div>
      <div><dt>Client order ID</dt><dd><code>${escapeHtml(order.client_id || "—")}</code></dd></div>
      ${order.exchange_id ? `<div><dt>${live ? "NonKYC order ID" : "Simulated order ID"}</dt><dd><code>${escapeHtml(order.exchange_id)}</code></dd></div>` : ""}
      ${order.error ? `<div><dt>Error</dt><dd class="negative">${escapeHtml(order.error)}</dd></div>` : ""}
    </dl>
    <h3>History</h3><ul class="order-timeline"><li class="muted">Loading…</li></ul>`;
  if (!dialog.open) dialog.showModal();
  try {
    const { events } = await liveApi(`/api/live/orders/${order.id}/events`);
    if (detailOrder !== order) return;
    body.querySelector(".order-timeline").innerHTML = events.map(event => `
      <li><small>${timeText(event.created_at)}</small> <strong>${escapeHtml(ORDER_EVENT_LABELS[event.event] || event.event)}</strong>
        <span class="muted">${escapeHtml(event.detail)}</span></li>`).join("") || `<li class="muted">No history recorded.</li>`;
  } catch (error) {
    body.querySelector(".order-timeline").innerHTML = `<li class="negative">${escapeHtml(error.message)}</li>`;
  }
}

async function showOrderOnChart() {
  const order = detailOrder;
  if (!order) return;
  document.querySelector("#orderDetailDialog").close();
  if (order.bot_id && liveBots.some(bot => bot.id === order.bot_id)) {
    await selectDashboardBot(order.bot_id);
    return;
  }
  showView("dashboard");
  if (selectedPeriod !== 15) {
    selectedPeriod = 15;
    document.querySelectorAll(".time-buttons button").forEach(button => {
      button.classList.toggle("selected", button.dataset.period === "15");
    });
  }
  await selectMarket(order.symbol);
  document.querySelector(".chart-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function orderAction(action, id) {
  showLiveError("");
  try {
    if (action === "cancel") {
      await liveApi(`/api/live/orders/${id}/cancel`, { method: "POST" });
    } else if (action === "reconcile") {
      await liveApi(`/api/live/orders/${id}/reconcile`, { method: "POST" });
    } else if (action === "resolve") {
      const exchangeId = window.prompt(
        "Check your NonKYC order history. Enter the matching NonKYC order ID, or leave blank to confirm no order exists.",
        ""
      );
      if (exchangeId === null) return;
      const password = await askPassword("Resolving an order changes how BitsBots accounts for it.");
      if (!password) return;
      await liveApi(`/api/live/orders/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ exchange_id: exchangeId.trim() || null, password }),
      });
    }
  } catch (error) {
    showLiveError(error.message);
  }
  await refreshLive();
}

async function previewOrder(event) {
  event.preventDefault();
  showLiveError("");
  try {
    currentPreview = await liveApi("/api/live/orders/preview", {
      method: "POST",
      body: JSON.stringify({
        mode: document.querySelector("#ticketMode").value,
        symbol: document.querySelector("#ticketSymbol").value.trim(),
        side: document.querySelector("#ticketSide").value,
        price: document.querySelector("#ticketPrice").value.trim(),
        quantity: document.querySelector("#ticketQuantity").value.trim(),
      }),
    });
  } catch (error) {
    showLiveError(error.message);
    return;
  }
  openPreviewDialog();
}

function openPreviewDialog() {
  const p = currentPreview;
  const live = p.mode === "live";
  const checks = p.risk.checks.map(check => `
    <li class="${check.passed ? "check-pass" : "check-fail"}"><span aria-hidden="true">${check.passed ? "✓" : "✗"}</span>
      <span class="sr-only">${check.passed ? "Passed" : "Failed"}:</span> ${escapeHtml(check.detail)}</li>`).join("");
  document.querySelector("#orderPreviewTitle").textContent =
    `${live ? "LIVE" : "Dry run"} ${p.side.toUpperCase()} ${p.symbol}`;
  document.querySelector("#orderPreviewBody").innerHTML = `
    ${live ? '<div class="live-warning">LIVE ORDER — this sends a real order with real funds to NonKYC.</div>' : '<div class="dry-warning">DRY RUN — simulated with real prices. Nothing is sent to NonKYC.</div>'}
    <dl class="preview-grid">
      <div><dt>Side</dt><dd class="${p.side === "buy" ? "positive" : "negative"}">${escapeHtml(p.side.toUpperCase())}</dd></div>
      <div><dt>Market</dt><dd>${escapeHtml(p.symbol)}</dd></div>
      <div><dt>Limit price</dt><dd>${decimalText(p.price)} ${escapeHtml(p.quote)}</dd></div>
      <div><dt>Quantity</dt><dd>${decimalText(p.quantity)} ${escapeHtml(p.base)}</dd></div>
      <div><dt>Order value</dt><dd>${decimalText(p.notional)} ${escapeHtml(p.quote)}</dd></div>
      <div><dt>Estimated fee</dt><dd>${decimalText(p.estimated_fee)} ${escapeHtml(p.quote)}</dd></div>
      <div><dt>${p.side === "buy" ? "Maximum spend" : "Estimated proceeds"}</dt><dd><strong>${decimalText(p.total)} ${escapeHtml(p.quote)}</strong></dd></div>
      <div><dt>Last / bid / ask</dt><dd>${decimalText(p.last_price)} / ${decimalText(p.best_bid)} / ${decimalText(p.best_ask)}</dd></div>
      <div><dt>Available</dt><dd>${decimalText(p.quote_available)} ${escapeHtml(p.quote)} · ${decimalText(p.base_available)} ${escapeHtml(p.base)}</dd></div>
      <div><dt>Expires</dt><dd id="previewCountdown"></dd></div>
    </dl>
    ${p.warnings.length ? `<ul class="preview-warnings">${p.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>` : ""}
    <h3>Risk checks</h3><ul class="risk-checks">${checks}</ul>`;
  document.querySelector("#orderPreviewPasswordRow").classList.toggle("hidden", !live);
  document.querySelector("#orderPreviewPassword").value = "";
  const confirmButton = document.querySelector("#orderPreviewConfirm");
  confirmButton.disabled = !p.allowed;
  confirmButton.textContent = p.allowed
    ? `Confirm ${live ? "LIVE " : ""}${p.side} of ${decimalText(p.quantity)} ${p.base}`
    : "Blocked by risk checks";
  confirmButton.classList.toggle("live-confirm", live);
  const errorElement = document.querySelector("#orderPreviewError");
  errorElement.classList.add("hidden");
  clearInterval(previewTimer);
  const tick = () => {
    const remaining = Math.max(0, Math.round((p.expires_at - Date.now()) / 1000));
    const countdown = document.querySelector("#previewCountdown");
    if (countdown) countdown.textContent = remaining ? `${remaining}s` : "Expired — create a new preview";
    if (!remaining) { confirmButton.disabled = true; clearInterval(previewTimer); }
  };
  tick();
  previewTimer = setInterval(tick, 1000);
  document.querySelector("#orderPreviewDialog").showModal();
}

async function confirmPreview() {
  const p = currentPreview;
  if (!p) return;
  const button = document.querySelector("#orderPreviewConfirm");
  const errorElement = document.querySelector("#orderPreviewError");
  const password = document.querySelector("#orderPreviewPassword").value;
  if (p.mode === "live" && !password) {
    errorElement.textContent = "Enter your password to confirm a live order.";
    errorElement.classList.remove("hidden");
    return;
  }
  button.disabled = true;
  try {
    const result = await liveApi("/api/live/orders/confirm", {
      method: "POST",
      body: JSON.stringify({ preview_id: p.id, password: password || null }),
    });
    document.querySelector("#orderPreviewPassword").value = "";
    clearInterval(previewTimer);
    document.querySelector("#orderPreviewDialog").close();
    currentPreview = null;
    const state = result.order.state;
    if (["submission_unknown", "rejected"].includes(state)) {
      showLiveError(`Order ${state.replace("_", " ")}: ${result.order.error || "see the order list"}`);
    }
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
    button.disabled = false;
  }
  await refreshLive();
}

// -------------------------------------------------------------------- bots
function renderLiveBots() {
  const container = document.querySelector("#liveBotList");
  container.innerHTML = liveBots.map(bot => {
    const stateClass = BOT_STATE_CLASS[bot.status] || "state-muted";
    const actions = [`<button type="button" data-bot-live="chart" data-id="${bot.id}" data-help="Show this bot's grid, open orders, and every buy and sell on the Dashboard chart.">View on chart</button>`];
    if (["armed", "paused", "halted"].includes(bot.status)) actions.push(`<button type="button" class="primary" data-bot-live="start" data-id="${bot.id}">${bot.status === "armed" ? "Start" : "Resume"}</button>`);
    if (bot.status === "running") actions.push(`<button type="button" data-bot-live="pause" data-id="${bot.id}" data-help="Stops new orders. Existing orders stay on the exchange.">Pause</button>`);
    if (!["stopped", "stopping"].includes(bot.status)) {
      actions.push(`<button type="button" data-bot-live="stop" data-id="${bot.id}" data-help="Cancels this bot's open orders and stops. Coins it bought stay in your account.">Stop</button>`);
      actions.push(`<button type="button" class="danger-button" data-bot-live="stop-sell" data-id="${bot.id}" data-help="Cancels this bot's open orders, then sells what it holds at up to 1% below the best bid.">Stop &amp; sell</button>`);
    }
    if (["armed", "stopped"].includes(bot.status)) actions.push(`<button type="button" data-bot-live="delete" data-id="${bot.id}">Delete</button>`);
    const counts = Object.entries(bot.slot_counts || {})
      .map(([state, count]) => `${count} ${escapeHtml(state.replace("_", " "))}`).join(" · ");
    const events = (bot.events || []).slice(0, 5)
      .map(event => `<li><small>${timeText(event.created_at)}</small> ${escapeHtml(event.detail)}</li>`).join("");
    return `<article class="live-bot-card ${bot.mode === "live" ? "live-bot-real" : ""}">
      <div class="bot-top"><div>
        <span class="mode-tag ${bot.mode === "live" ? "mode-tag-live" : ""}">${modeLabel(bot.mode)}</span>
        <span class="state-tag ${stateClass}">${escapeHtml(bot.status)}</span>
        <h3>${escapeHtml(bot.name)} · ${escapeHtml(bot.symbol)}</h3>
        <small class="muted">Hosted by ${escapeHtml(bot.hosted_by)}</small></div>
        <div class="row-actions">${actions.join("")}</div></div>
      ${bot.halt_reason ? `<p class="caution">${escapeHtml(bot.halt_reason)}</p>` : ""}
      <dl class="preview-grid">
        <div><dt>Range</dt><dd>${decimalText(bot.lower_price)} – ${decimalText(bot.upper_price)} (${bot.levels} lines)</dd></div>
        <div><dt>Stop loss</dt><dd>${decimalText(bot.stop_price)}</dd></div>
        <div><dt>Committed / allocation</dt><dd>${decimalText(bot.committed)} / ${decimalText(bot.max_allocation)}</dd></div>
        <div><dt>Realized P&amp;L</dt><dd class="${Number(bot.realized_pnl) >= 0 ? "positive" : "negative"}">${decimalText(bot.realized_pnl)}</dd></div>
        <div><dt>Holding</dt><dd>${decimalText(bot.held_quantity)} (cost ${decimalText(bot.holding_cost)})</dd></div>
        <div><dt>Slots</dt><dd>${counts || "—"}</dd></div>
        <div><dt>Trailing Up</dt><dd>${bot.trailing_enabled ? `On · ${bot.recenter_count}/${bot.trailing_max_recenters} moves · ceiling ${decimalText(bot.trailing_ceiling)}` : "Off"}</dd></div>
      </dl>
      ${events ? `<ul class="bot-events">${events}</ul>` : ""}
    </article>`;
  }).join("") || `<p class="muted">No grid bots yet. Start with a dry-run bot to rehearse.</p>`;
  installHelpTips(container);
}

async function botAction(action, id) {
  showLiveError("");
  const bot = liveBots.find(item => String(item.id) === String(id));
  if (action === "chart") {
    await selectDashboardBot(id);
    return;
  }
  try {
    if (action === "start") {
      let body = null;
      if (bot && bot.mode === "live") {
        const password = await askPassword(`Start LIVE grid bot "${bot.name}" on ${bot.symbol}? It will place real orders.`);
        if (!password) return;
        body = JSON.stringify({ password });
      }
      await liveApi(`/api/live/bots/${id}/start`, { method: "POST", body: body || undefined });
    } else if (action === "pause") {
      await liveApi(`/api/live/bots/${id}/pause`, { method: "POST" });
    } else if (action === "stop" || action === "stop-sell") {
      const liquidate = action === "stop-sell";
      const question = liquidate
        ? "Stop this bot, cancel its orders, and SELL its holdings near the market price?"
        : "Stop this bot and cancel its open orders? Coins it holds stay in your account.";
      if (!window.confirm(question)) return;
      await liveApi(`/api/live/bots/${id}/stop`, { method: "POST", body: JSON.stringify({ liquidate }) });
    } else if (action === "delete") {
      if (!window.confirm("Delete this bot's configuration? Its order history is kept.")) return;
      await liveApi(`/api/live/bots/${id}`, { method: "DELETE" });
    }
  } catch (error) {
    showLiveError(error.message);
  }
  await refreshLive();
}

// ---------------------------------------------------- dashboard bot view
const OPEN_ORDER_STATES = ["pending", "submitting", "open", "partially_filled", "cancel_requested"];
let dashboardBotId = Number(localStorage.getItem("bitsbots-dashboard-bot")) || null;
let dashboardActivity = null;
let dashboardRestored = false;

const isViewVisible = name => !document.querySelector(`#view-${name}`).classList.contains("hidden");
const filledOrders = activity => activity.orders
  .filter(order => Number(order.executed_quantity) > 0 && order.filled_at)
  .sort((a, b) => b.filled_at - a.filled_at);
const openOrdersOf = activity => activity.orders.filter(order => OPEN_ORDER_STATES.includes(order.state));

function agoText(timestamp) {
  if (!timestamp) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - Number(timestamp)) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return timeText(timestamp);
}

function renderDashboardBots() {
  const container = document.querySelector("#dashGridBots");
  const order = { running: 0, recentering: 0, stopping: 1, halted: 2, paused: 3, armed: 4, stopped: 5 };
  const bots = [...liveBots].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || b.id - a.id);
  container.innerHTML = bots.map(bot => {
    const counts = bot.slot_counts || {};
    const pnl = Number(bot.realized_pnl);
    return `<article class="bot-card compact grid-bot-card ${bot.id === dashboardBotId ? "selected" : ""} ${bot.status === "stopped" ? "grid-bot-stopped" : ""}"
        data-grid-bot="${bot.id}" tabindex="0" role="button" aria-label="Show ${escapeHtml(bot.name)} on the chart">
      <div class="bot-top">
        <div class="bot-identity"><div><strong>${escapeHtml(bot.symbol)}</strong>
          <small>${escapeHtml(bot.name)} · ${counts.buy_open || 0} buys / ${counts.sell_open || 0} sells open</small></div></div>
        <span class="state-tag ${BOT_STATE_CLASS[bot.status] || "state-muted"}">${escapeHtml(bot.status)}</span>
      </div>
      <div class="bot-pnl-row"><span class="mode-tag ${bot.mode === "live" ? "mode-tag-live" : ""}">${modeLabel(bot.mode)}</span>
        <span>Realized <b class="${pnl >= 0 ? "positive" : "negative"}">${decimalText(bot.realized_pnl)}</b></span></div>
    </article>`;
  }).join("") || `<div class="empty-action">No BitsBots grid bots yet. <button type="button" class="linklike" data-dash-open-live>Create one in Live Trading</button></div>`;
}

async function selectDashboardBot(botId) {
  dashboardBotId = Number(botId);
  localStorage.setItem("bitsbots-dashboard-bot", String(dashboardBotId));
  const bot = liveBots.find(item => item.id === dashboardBotId);
  renderDashboardBots();
  if (!isViewVisible("dashboard")) showView("dashboard");
  if (bot) {
    if (selectedPeriod !== 15) {
      selectedPeriod = 15;
      document.querySelectorAll(".time-buttons button").forEach(button => {
        button.classList.toggle("selected", button.dataset.period === "15");
      });
    }
    await refreshDashboardActivity();
    await selectMarket(bot.symbol);
  }
  document.querySelector(".chart-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearDashboardBot() {
  dashboardBotId = null;
  dashboardActivity = null;
  localStorage.removeItem("bitsbots-dashboard-bot");
  renderDashboardBots();
  renderDashboardActivity();
  if (typeof activeCandles !== "undefined" && activeCandles.length) drawMarketChart();
}

async function refreshDashboardActivity() {
  if (!dashboardBotId) return;
  try {
    dashboardActivity = await liveApi(`/api/live/bots/${dashboardBotId}/activity`);
  } catch (error) {
    if (/not found/i.test(error.message)) clearDashboardBot();
    return;
  }
  renderDashboardActivity();
  if (dashboardActivity.bot.symbol === selectedSymbol && activeCandles.length) drawMarketChart();
}

function renderDashboardActivity() {
  const panel = document.querySelector("#gridActivityPanel");
  const activity = dashboardActivity;
  panel.classList.toggle("hidden", !activity);
  if (!activity) return;
  const bot = activity.bot;
  const fills = filledOrders(activity);
  const open = openOrdersOf(activity).sort((a, b) => Number(b.price) - Number(a.price));
  const last = activity.last_price != null ? Number(activity.last_price) : null;
  const fee = Number(activity.fee_rate) || 0;
  const held = Number(bot.held_quantity);
  const unrealized = last != null && held > 0 ? held * last * (1 - fee) - Number(bot.holding_cost) : null;
  const buys = fills.filter(order => order.side === "buy").length;
  const stale = ["running", "recentering", "stopping"].includes(bot.status)
    && (!bot.last_tick_at || Date.now() - Number(bot.last_tick_at) > 60_000);
  document.querySelector("#gridActivityTitle").textContent =
    `${modeLabel(bot.mode)} GRID BOT · ${bot.name} · ${bot.symbol}`.toUpperCase();
  document.querySelector("#gridActivityStats").innerHTML = `
    ${bot.halt_reason ? `<p class="caution grid-activity-note">${escapeHtml(bot.halt_reason)}</p>` : ""}
    ${stale ? `<p class="caution grid-activity-note">The BitsBots engine has not checked this bot for over a minute. Bots only trade while BitsBots is running.</p>` : ""}
    ${bot.symbol !== selectedSymbol ? `<p class="grid-activity-note">The chart is showing ${escapeHtml(selectedSymbol || "another market")}. <button type="button" class="linklike" data-dash-show-chart>Show ${escapeHtml(bot.symbol)} on the chart</button></p>` : ""}
    <div><span>STATUS</span><strong><span class="state-tag ${BOT_STATE_CLASS[bot.status] || "state-muted"}">${escapeHtml(bot.status)}</span></strong><small>Checked ${agoText(bot.last_tick_at)}</small></div>
    <div><span>PRICE NOW</span><strong>${last != null ? decimalText(activity.last_price) : "—"}</strong><small>Range ${decimalText(bot.lower_price)} – ${decimalText(bot.upper_price)}</small></div>
    <div><span>REALIZED P&amp;L</span><strong class="${Number(bot.realized_pnl) >= 0 ? "positive" : "negative"}">${decimalText(bot.realized_pnl)}</strong><small>After fees</small></div>
    <div><span>UNREALIZED (EST.)</span><strong class="${unrealized == null ? "" : unrealized >= 0 ? "positive" : "negative"}">${unrealized == null ? "—" : unrealized.toFixed(4)}</strong><small>If holdings sold now</small></div>
    <div><span>HOLDING</span><strong>${decimalText(bot.held_quantity)}</strong><small>Cost ${decimalText(bot.holding_cost)}</small></div>
    <div><span>FILLS</span><strong>${fills.length}</strong><small>${buys} buys · ${fills.length - buys} sells</small></div>
    <div><span>OPEN ORDERS</span><strong>${open.length}</strong><small>${open.filter(o => o.side === "buy").length} buys · ${open.filter(o => o.side === "sell").length} sells</small></div>
    <div><span>STOP LOSS</span><strong class="negative">${decimalText(bot.stop_price)}</strong><small>${bot.trailing_enabled ? `Trailing Up ${bot.recenter_count}/${bot.trailing_max_recenters}` : "Trailing Up off"}</small></div>`;
  document.querySelector("#gridActivityFills").innerHTML = fills.slice(0, 200).map(order => {
    const qty = Number(order.executed_quantity);
    return `<tr>
      <td>${timeText(order.filled_at)}</td>
      <td class="${order.side === "buy" ? "positive" : "negative"}">${escapeHtml(order.side.toUpperCase())}</td>
      <td>${decimalText(order.price)}</td><td>${decimalText(order.executed_quantity)}</td>
      <td>${(qty * Number(order.price)).toFixed(4)}</td>
      <td><small class="muted">${escapeHtml(order.purpose.replace("_", " "))} · ${escapeHtml(order.state.replace("_", " "))}</small></td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No fills yet. Buys fill when the price drops to a buy line; each buy then gets a sell one line higher.</td></tr>`;
  document.querySelector("#gridActivityOpen").innerHTML = open.map(order => `
    <div class="book-row"><span><span class="badge ${order.side === "buy" ? "buy" : "watch"}">${escapeHtml(order.side)}</span></span>
      <span>${decimalText(order.price)}</span><span>${decimalText(order.quantity)}</span></div>`).join("")
    || `<p class="muted">No open orders.</p>`;
  document.querySelector("#gridActivityEvents").innerHTML = (bot.events || []).slice(0, 8)
    .map(event => `<li><small>${timeText(event.created_at)}</small> ${escapeHtml(event.detail)}</li>`).join("")
    || `<li class="muted">No events yet.</li>`;
}

// Called by drawMarketChart in app.js so the price scale includes the grid.
function gridBotChartPrices(symbol) {
  const manual = manualOrdersFor(symbol).filter(order => OPEN_ORDER_STATES.includes(order.state)).map(order => Number(order.price));
  const activity = dashboardActivity;
  if (!activity || activity.bot.symbol !== symbol) return manual;
  const bot = activity.bot;
  return [...manual, ...[bot.lower_price, bot.upper_price, bot.stop_price, activity.last_price]
    .map(Number).filter(value => Number.isFinite(value) && value > 0)];
}

const manualOrdersFor = symbol => liveOrders.filter(order => !order.bot_id && order.symbol === symbol);

function drawFillMarker(ctx, candles, xAt, yAt, order, label) {
  if (!order.filled_at || order.filled_at < candles[0].timestamp) return;
  let index = candles.length - 1;
  while (index > 0 && candles[index].timestamp > order.filled_at) index--;
  const x = xAt(index); const y = yAt(Number(order.price));
  const buy = order.side === "buy"; const direction = buy ? 1 : -1;
  ctx.save();
  ctx.fillStyle = buy ? "#45d483" : "#ff554c";
  ctx.strokeStyle = "#050505"; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x, y); ctx.lineTo(x - 6, y + direction * 11); ctx.lineTo(x + 6, y + direction * 11); ctx.closePath();
  ctx.fill(); ctx.stroke();
  ctx.font = "bold 8px Segoe UI"; ctx.textAlign = "center";
  ctx.fillText(label, x, y + direction * 20);
  ctx.restore();
}

function drawOrderLine(ctx, yAt, plotWidth, value, color, dash, label, align = "left") {
  if (!Number.isFinite(value)) return;
  const y = yAt(value);
  ctx.save();
  ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotWidth, y); ctx.stroke();
  if (label) {
    ctx.font = "bold 9px Segoe UI"; ctx.textAlign = align; ctx.fillStyle = color;
    ctx.fillText(label, align === "left" ? 4 : plotWidth - 4, y - 3);
  }
  ctx.restore();
}

// Called by drawMarketChart in app.js after candles are drawn.
function drawGridBotOverlay(ctx, candles, xAt, yAt, plotWidth) {
  if (!candles.length) return;
  manualOrdersFor(selectedSymbol).forEach(order => {
    if (OPEN_ORDER_STATES.includes(order.state)) {
      drawOrderLine(ctx, yAt, plotWidth, Number(order.price), "#b48cff", [5, 3],
        `MANUAL ${order.side.toUpperCase()} ${decimalText(order.price)}`, "right");
    }
    if (Number(order.executed_quantity) > 0) {
      drawFillMarker(ctx, candles, xAt, yAt, order, order.side === "buy" ? "MANUAL BUY" : "MANUAL SELL");
    }
  });
  const activity = dashboardActivity;
  if (!activity || activity.bot.symbol !== selectedSymbol) return;
  const bot = activity.bot;
  const line = (value, color, dash, label, align) => drawOrderLine(ctx, yAt, plotWidth, value, color, dash, label, align);
  const levels = new Set();
  (bot.slots || []).filter(slot => slot.ladder_version === bot.ladder_version).forEach(slot => {
    levels.add(Number(slot.buy_price)); levels.add(Number(slot.sell_price));
  });
  levels.forEach(value => line(value, "#3d4552", [2, 4]));
  line(Number(bot.upper_price), "#4e79b9", [], "GRID HIGH");
  line(Number(bot.lower_price), "#4e79b9", [], "GRID LOW");
  line(Number(bot.stop_price), "#ff554c", [7, 4], "STOP LOSS");
  openOrdersOf(activity).forEach(order => {
    const buy = order.side === "buy";
    line(Number(order.price), buy ? "#45d483" : "#ff8a80", [5, 3], `${buy ? "BUY" : "SELL"} ${decimalText(order.price)}`, "right");
  });
  if (activity.last_price != null) line(Number(activity.last_price), "#e8bd55", [1, 3], `NOW ${decimalText(activity.last_price)}`, "right");
  filledOrders(activity).forEach(order => drawFillMarker(ctx, candles, xAt, yAt, order, order.side === "buy" ? "B" : "S"));
}

function gridPayload() {
  const trailing = document.querySelector("#gridTrailing").checked;
  const payload = {
    mode: document.querySelector("#gridMode").value,
    name: document.querySelector("#gridName").value.trim(),
    symbol: document.querySelector("#gridSymbol").value.trim(),
    lower_price: document.querySelector("#gridLower").value.trim(),
    upper_price: document.querySelector("#gridUpper").value.trim(),
    levels: Number(document.querySelector("#gridLevels").value),
    order_value: document.querySelector("#gridOrderValue").value.trim(),
    max_allocation: document.querySelector("#gridAllocation").value.trim(),
    stop_price: document.querySelector("#gridStop").value.trim(),
    trailing_enabled: trailing,
  };
  if (trailing) {
    payload.trailing_trigger = String(Number(document.querySelector("#gridTrailTrigger").value) / 100);
    payload.trailing_confirm_seconds = Number(document.querySelector("#gridTrailConfirm").value);
    payload.trailing_max_recenters = Number(document.querySelector("#gridTrailMax").value);
    payload.trailing_ceiling = document.querySelector("#gridTrailCeiling").value.trim();
  }
  return payload;
}

// -------------------------------------------------------- grid suggestions
const SUGGEST_FIELDS = {
  name: "#gridName", lower_price: "#gridLower", upper_price: "#gridUpper", levels: "#gridLevels",
  order_value: "#gridOrderValue", max_allocation: "#gridAllocation", stop_price: "#gridStop",
  trailing_trigger: "#gridTrailTrigger", trailing_confirm_seconds: "#gridTrailConfirm",
  trailing_max_recenters: "#gridTrailMax", trailing_ceiling: "#gridTrailCeiling",
};
const SYMBOL_PATTERN = /^[A-Z0-9]+\/(USDT|USDC)$/;
let suggestTimer = null;
let suggestGeneration = 0;

// Untouched fields hold their default or a previous suggestion, so they may be refilled.
function fieldIsUntouched(input) {
  return !input.value.trim() || input.value === input.dataset.suggested || input.value === input.defaultValue;
}

async function suggestGridValues(force = false) {
  const symbol = document.querySelector("#gridSymbol").value.trim().toUpperCase();
  const note = document.querySelector("#gridSuggestNote");
  const button = document.querySelector("#gridSuggestButton");
  if (!SYMBOL_PATTERN.test(symbol)) {
    if (force) {
      note.className = "grid-suggest-note feedback-error";
      note.textContent = "Choose a COIN/USDT or COIN/USDC market first.";
    }
    return;
  }
  const generation = ++suggestGeneration;
  button.disabled = true;
  note.className = "grid-suggest-note";
  note.textContent = `Looking at ${symbol}'s current price and recent trading…`;
  try {
    const result = await liveApi("/api/live/bots/suggest", {
      method: "POST",
      body: JSON.stringify({ symbol, mode: document.querySelector("#gridMode").value }),
    });
    if (generation !== suggestGeneration) return;
    let changed = 0;
    Object.entries(SUGGEST_FIELDS).forEach(([key, selector]) => {
      const input = document.querySelector(selector);
      const value = String(result.values[key]);
      if (force || fieldIsUntouched(input)) {
        if (input.value !== value) changed++;
        input.value = value;
        input.dataset.suggested = value;
      }
    });
    currentGridPlan = null;
    document.querySelector("#gridPlan").innerHTML = "";
    document.querySelector("#gridCreateButton").disabled = true;
    const kept = !force && Object.values(SUGGEST_FIELDS).some(selector => {
      const input = document.querySelector(selector);
      return input.value !== input.dataset.suggested;
    });
    note.className = `grid-suggest-note ${result.valid ? "" : "feedback-error"}`;
    note.innerHTML = `
      <strong>${result.valid ? "Suggested values filled in." : "Suggested values filled in, but they need attention:"}</strong>
      ${result.errors.length ? `<ul class="risk-checks">${result.errors.map(e => `<li class="check-fail">✗ ${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      <ul>${result.notes.map(n => `<li>${escapeHtml(n)}</li>`).join("")}</ul>
      ${kept ? '<p class="muted small">Values you typed yourself were kept. Use the button above to replace them too.</p>' : ""}
      <p class="muted small">Next: press <strong>Preview grid</strong> to see every order before creating the bot.</p>`;
  } catch (error) {
    if (generation !== suggestGeneration) return;
    note.className = "grid-suggest-note feedback-error";
    note.textContent = `Could not suggest values: ${error.message}`;
  } finally {
    if (generation === suggestGeneration) button.disabled = false;
  }
}

function scheduleGridSuggestion() {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(() => suggestGridValues(false), 500);
}

async function previewGrid() {
  const errorElement = document.querySelector("#gridError");
  const createButton = document.querySelector("#gridCreateButton");
  errorElement.classList.add("hidden");
  createButton.disabled = true;
  try {
    currentGridPlan = await liveApi("/api/live/bots/preview", { method: "POST", body: JSON.stringify(gridPayload()) });
  } catch (error) {
    currentGridPlan = null;
    document.querySelector("#gridPlan").innerHTML = "";
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
    return;
  }
  const plan = currentGridPlan;
  const rows = plan.slots.map(slot => `<tr class="${slot.places_now ? "" : "muted"}">
    <td>${decimalText(slot.buy_price)}</td><td>${decimalText(slot.sell_price)}</td><td>${decimalText(slot.quantity)}</td>
    <td>${decimalText(slot.notional)}</td><td class="${Number(slot.cycle_profit) > 0 ? "positive" : "negative"}">${decimalText(slot.cycle_profit)}</td>
    <td>${slot.places_now ? "Buy placed at start" : "Waits until price is above"}</td></tr>`).join("");
  document.querySelector("#gridPlan").innerHTML = `
    ${plan.mode === "live" ? '<div class="live-warning">LIVE GRID — real orders with real funds.</div>' : '<div class="dry-warning">DRY RUN — simulated with real prices.</div>'}
    <p>Current price ${decimalText(plan.last_price)} · initial commitment ${decimalText(plan.initial_commitment)}</p>
    ${plan.errors.length ? `<ul class="risk-checks">${plan.errors.map(e => `<li class="check-fail">✗ ${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
    <ul class="preview-warnings">${plan.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join("")}</ul>
    <div class="table-wrap"><table><thead><tr><th>Buy</th><th>Sell</th><th>Qty</th><th>Value</th><th>Profit / cycle after fees</th><th>At start</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
  createButton.disabled = !plan.valid;
}

async function createGrid(event) {
  event.preventDefault();
  if (!currentGridPlan || !currentGridPlan.valid) return;
  const errorElement = document.querySelector("#gridError");
  try {
    await liveApi("/api/live/bots", { method: "POST", body: JSON.stringify(gridPayload()) });
    document.querySelector("#gridDialog").close();
    currentGridPlan = null;
    document.querySelector("#gridPlan").innerHTML = "";
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.classList.remove("hidden");
    return;
  }
  await refreshLive();
}

// --------------------------------------------------------------- settings
async function enableLive(event) {
  event.preventDefault();
  showLiveError("");
  const passwordInput = document.querySelector("#liveEnablePassword");
  const submitButton = document.querySelector("#liveEnableSubmit");
  if (submitButton.disabled) return;
  submitButton.disabled = true;
  submitButton.textContent = "Checking…";
  showFeedback("#liveEnableFeedback", "Checking your NonKYC API key. This can take a few seconds…");
  showFeedback("#liveEnabledFeedback", "");
  try {
    liveStatus = await liveApi("/api/live/enable", {
      method: "POST",
      body: JSON.stringify({
        password: passwordInput.value,
        ip_address: document.querySelector("#liveIpAddress").value.trim(),
        trade_permission_confirmed: document.querySelector("#liveConfirmTrade").checked,
        withdrawals_disabled_confirmed: document.querySelector("#liveConfirmWithdraw").checked,
        ip_allowlisted_confirmed: document.querySelector("#liveConfirmIp").checked,
        risk_acknowledged: document.querySelector("#liveConfirmRisk").checked,
        typed_phrase: document.querySelector("#liveTypedPhrase").value,
      }),
    });
    document.querySelector("#liveEnableForm").reset();
    showFeedback("#liveEnableFeedback", "");
    renderLiveStatus();
    showFeedback(
      "#liveEnabledFeedback",
      "Live trading is now ON. NonKYC accepted your API key. Nothing trades until you confirm an order or start a bot.",
      "success",
    );
  } catch (error) {
    showFeedback(
      "#liveEnableFeedback",
      `Live trading was not turned on. ${friendlyError(error)} Re-enter your password to try again.`,
      "error",
    );
  } finally {
    passwordInput.value = "";
    submitButton.disabled = false;
    submitButton.textContent = "Enable live trading";
  }
  await refreshLive();
}

async function saveLimits(event) {
  event.preventDefault();
  showLiveError("");
  const values = {};
  new FormData(event.currentTarget).forEach((value, key) => {
    const field = LIMIT_FIELDS.find(item => item[0] === key);
    values[key] = field && field[3] === "percent" ? String(Number(value) / 100) : String(value).trim();
  });
  try {
    liveStatus = await liveApi("/api/live/limits", { method: "PUT", body: JSON.stringify(values) });
    document.activeElement?.blur();
  } catch (error) {
    showLiveError(error.message);
  }
  await refreshLive();
}

async function toggleKillSwitch() {
  showLiveError("");
  const engaging = !liveStatus?.kill_switch;
  try {
    if (engaging) {
      await liveApi("/api/live/kill-switch", { method: "POST", body: JSON.stringify({ engaged: true }) });
    } else {
      const password = await askPassword("Turning the kill switch off allows BitsBots orders again. Bots stay halted until you resume them.");
      if (!password) return;
      await liveApi("/api/live/kill-switch", { method: "POST", body: JSON.stringify({ engaged: false, password }) });
    }
  } catch (error) {
    showLiveError(error.message);
  }
  await refreshLive();
}

async function emergencyCancel() {
  const mode = window.prompt('Emergency cancel which mode? Type "live" or "dry_run".', "live");
  if (!mode || !["live", "dry_run"].includes(mode.trim())) return;
  showLiveError("");
  try {
    const result = await liveApi("/api/live/emergency-cancel", { method: "POST", body: JSON.stringify({ mode: mode.trim() }) });
    showLiveError(`Kill switch engaged. Cancelled ${result.cancelled} BitsBots order(s)${result.failed.length ? `; ${result.failed.length} need attention` : ""}.`);
  } catch (error) {
    showLiveError(error.message);
  }
  await refreshLive();
}

// ------------------------------------------------------------ risk notice
async function showRiskNoticeOncePerStart() {
  const dialog = document.querySelector("#riskNoticeDialog");
  let bootId = "unknown";
  try {
    bootId = (await liveApi("/api/app/info")).boot_id;
  } catch (error) {
    // Show the notice anyway if the boot ID cannot be read.
  }
  if (localStorage.getItem("bitsbots-risk-notice-boot") === bootId) return;
  const dismiss = () => {
    localStorage.setItem("bitsbots-risk-notice-boot", bootId);
    if (dialog.open) dialog.close();
  };
  document.querySelector("#riskNoticeClose").addEventListener("click", dismiss);
  document.querySelector("#riskNoticeAccept").addEventListener("click", dismiss);
  dialog.addEventListener("cancel", event => { event.preventDefault(); dismiss(); });
  dialog.showModal();
  document.querySelector("#riskNoticeAccept").focus();
}

// ------------------------------------------------------------------ wiring
document.querySelectorAll("[data-close-dialog]").forEach(button => {
  button.addEventListener("click", () => button.closest("dialog").close());
});
document.querySelector("#orderPreviewDialog").addEventListener("close", () => {
  clearInterval(previewTimer);
  document.querySelector("#orderPreviewPassword").value = "";
});
document.querySelector("#orderTicket").addEventListener("submit", previewOrder);
document.querySelector("#orderPreviewConfirm").addEventListener("click", confirmPreview);
document.querySelector("#liveEnableForm").addEventListener("submit", enableLive);
document.querySelector("#liveLimitsForm").addEventListener("submit", saveLimits);
document.querySelector("#liveDisableButton").addEventListener("click", async () => {
  try {
    liveStatus = await liveApi("/api/live/disable", { method: "POST" });
    showFeedback("#liveEnabledFeedback", "");
    renderLiveStatus();
    showFeedback("#liveEnableFeedback", "Live trading is now OFF. Any live bots were halted.", "success");
  } catch (error) {
    showFeedback("#liveEnabledFeedback", `Live trading is still on. ${friendlyError(error)}`, "error");
  }
  await refreshLive();
});
document.querySelector("#liveIpReconfirmButton").addEventListener("click", async () => {
  const address = document.querySelector("#liveIpReconfirm").value.trim();
  try {
    await liveApi("/api/live/ip", { method: "POST", body: JSON.stringify({ ip_address: address }) });
    showFeedback("#liveEnabledFeedback", `Public IP ${address} reconfirmed.`, "success");
  } catch (error) {
    showFeedback("#liveEnabledFeedback", `IP not updated. ${friendlyError(error)}`, "error");
  }
  await refreshLive();
});
document.querySelector("#killSwitchButton").addEventListener("click", toggleKillSwitch);
document.querySelector("#emergencyCancelButton").addEventListener("click", emergencyCancel);
document.querySelector("#orderModeFilter").addEventListener("change", refreshLive);
document.querySelector("#liveOrderRows").addEventListener("click", event => {
  const button = event.target.closest("[data-order-action]");
  if (button) {
    orderAction(button.dataset.orderAction, button.dataset.id);
    return;
  }
  const row = event.target.closest("[data-order-row]");
  if (row) showOrderDetail(row.dataset.orderRow);
});
document.querySelector("#liveOrderRows").addEventListener("keydown", event => {
  const row = event.target.closest("[data-order-row]");
  if (row && event.target === row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    showOrderDetail(row.dataset.orderRow);
  }
});
document.querySelector("#orderDetailChart").addEventListener("click", showOrderOnChart);
document.querySelector("#liveBotList").addEventListener("click", event => {
  const button = event.target.closest("[data-bot-live]");
  if (button) botAction(button.dataset.botLive, button.dataset.id);
});
document.querySelector("#newGridBot").addEventListener("click", () => {
  renderMarketList();
  currentGridPlan = null;
  document.querySelector("#gridPlan").innerHTML = "";
  document.querySelector("#gridError").classList.add("hidden");
  document.querySelector("#gridCreateButton").disabled = true;
  document.querySelector("#gridDialog").showModal();
  if (document.querySelector("#gridSymbol").value.trim()) scheduleGridSuggestion();
});
document.querySelector("#gridSymbol").addEventListener("input", scheduleGridSuggestion);
document.querySelector("#gridMode").addEventListener("change", scheduleGridSuggestion);
document.querySelector("#gridSuggestButton").addEventListener("click", () => suggestGridValues(true));
document.querySelector("#gridTrailing").addEventListener("change", event => {
  document.querySelector("#gridTrailingFields").classList.toggle("hidden", !event.target.checked);
});
document.querySelector("#gridForm").addEventListener("input", () => {
  document.querySelector("#gridCreateButton").disabled = true;
});
document.querySelector("#gridPreviewButton").addEventListener("click", previewGrid);
document.querySelector("#gridForm").addEventListener("submit", createGrid);
document.querySelector("#quickActionsLiveLocked").addEventListener("click", () => {
  showView("live");
  refreshLive();
});
document.querySelectorAll('.nav-links [data-view="live"]').forEach(button => {
  button.addEventListener("click", () => { renderMarketList(); refreshLive(); });
});

installHelpTips(document.querySelector("#view-live"));
installHelpTips(document.querySelector("#gridDialog"));
showRiskNoticeOncePerStart();
refreshLive();
setInterval(() => {
  if (isViewVisible("live") || isViewVisible("dashboard")) refreshLive();
}, 5000);
setInterval(() => {
  if (!isViewVisible("live") && !isViewVisible("dashboard")) refreshLive();
}, 30000);
document.querySelector("#dashGridBots").addEventListener("click", event => {
  if (event.target.closest("[data-dash-open-live]")) {
    showView("live");
    refreshLive();
    return;
  }
  const card = event.target.closest("[data-grid-bot]");
  if (card) selectDashboardBot(card.dataset.gridBot);
});
document.querySelector("#dashGridBots").addEventListener("keydown", event => {
  const card = event.target.closest("[data-grid-bot]");
  if (card && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    selectDashboardBot(card.dataset.gridBot);
  }
});
document.querySelector("#gridActivityStats").addEventListener("click", event => {
  if (event.target.closest("[data-dash-show-chart]") && dashboardBotId) selectDashboardBot(dashboardBotId);
});
document.querySelector("#gridActivityClose").addEventListener("click", clearDashboardBot);
document.querySelector("#dashManageBots").addEventListener("click", () => {
  showView("live");
  refreshLive();
});
installHelpTips(document.querySelector("#view-dashboard"));
