let state = null;
let selectedSymbol = null;
let selectedPeriod = 60;
let seenAlerts = new Set();
let activeCandles = [];
let hoverIndex = null;
let chartDataKey = null;
let chartView = { start: 0, end: 0 };
let chartDrag = null;
let marketSocket = null;
let streamSymbol = null;
let streamGeneration = 0;
let reconnectAttempt = 0;
let reconnectTimer = null;
let chartSelectionGeneration = 0;
let recentTrades = [];
let myAccountSnapshot = null;
let currentProfile = null;
let currentBotRecommendation = null;
let availableBotMarkets = [];
let availableChartMarkets = [];
let selectedBotStrategy = "Multi-TF";
let liveAccountConnected = false;
let liveAccountMaskedKey = null;
let realUsdtBalance = null;
let botRecommendationGeneration = 0;
const expandedBotIds = new Set(
  (localStorage.getItem("bitsbots-expanded-bots") || "")
    .split(",")
    .map(Number)
    .filter(Number.isFinite)
);
const indicators = { ema: true, bands: true, volume: true, rsi: true, macd: true };

const money = value => new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 2
}).format(value || 0);
const price = value => Number(value || 0).toLocaleString("en-US", { maximumSignificantDigits: 8 });
const time = value => value ? new Date(value).toLocaleTimeString() : "Never";
const tfLabel = period => ({1440: "1D", 240: "4H", 60: "1H", 15: "15M"})[period];
const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const HELP_TIPS = {
  '[data-view="dashboard"]': "Return to live scanning, charts, order book, positions, and current bot activity.",
  '[data-view="strategies"]': "Review the signal logic and risk rules available to paper bots.",
  '[data-view="bots"]': "Create, pause, resume, delete, and focus your user-owned paper bots.",
  '[data-view="portfolio"]': "Review cash, equity, open positions, and realized paper results.",
  '[data-view="backtesting"]': "Review historical-data requirements and the validation workflow.",
  '[data-view="settings"]': "Review your local profile and paper execution defaults.",
  '[data-view="assistant"]': "Ask for data-grounded explanations of current signals and paper-account conditions.",
  "#notify": "Allow BitsBots to show browser notifications for paper entries, exits, and strong setups.",
  "#profileName": "Open your local profile and execution settings.",
  "#logout": "End this local session and return to sign in.",
  "#newBotRail": "Create a paper bot that permits simulated entries for one selected market.",
  "#scan": "Refresh NonKYC market discovery and recalculate every multi-timeframe score. The count is trading pairs, not exchanges; configured bot markets are always prioritized.",
  "#quickNewBot": "Configure a new user-owned paper bot.",
  "#quickScan": "Scan the selected set of liquid NonKYC trading pairs immediately. This does not scan multiple exchanges.",
  "#quickAlerts": "Enable browser notifications for important paper-trading events.",
  "#refreshLiveAccount": "Fetch current balances, open orders, and recent trades directly from your NonKYC account.",
  "#goToLiveAccountSettings": "Open Settings to connect your NonKYC API key.",
  "#liveAccessKey": "Your NonKYC API access key. A read-only key is recommended.",
  "#liveSecretKey": "Your NonKYC API secret key. It is encrypted and stored only on this PC.",
  "#disconnectLiveAccount": "Remove the stored key from this PC. It does not revoke it on NonKYC.",
  "#marketSearch": "Type a coin or pair to filter the complete active NonKYC USDT and USDC market list. Press Enter to select the first match.",
  "#marketSelect": "Choose which scanned market is shown in the chart and recent trades.",
  ".time-buttons button": "Change the candle period displayed without changing the bot's strategy.",
  ".indicator-controls button": "Show or hide this indicator. It changes the chart display, not bot rules.",
  ".new-bot-action": "Open the bot configuration form.",
  ".auto-bot-action": "Use current scanner evidence to prefill a conservative bot candidate for your review.",
  "#autoGenerateBot": "Rank unassigned markets and fill this form. You must still confirm creation.",
  "#createRecommendedBot": "Create the displayed recommendation as a paper-only bot using conservative automatically selected settings.",
  "#showCustomBot": "Reveal manual controls so you can choose the coin, risk, entry score, and allocation yourself.",
  "#botName": "A private label used to identify this paper bot.",
  "#botMarketSearch": "Search every active NonKYC USDT and USDC pair, including markets outside the top-20 discovery scan.",
  "#botSymbol": "The single market this bot may open simulated positions on.",
  "#botStrategy": "The analysis and entry/exit rules used by this bot.",
  ".strategy-choice": "Choose a directional momentum bot, a fixed-range grid, or a grid that can recenter when it has no open slices.",
  "#botRisk": "Controls equity risk per trade: low 0.25%, medium 0.50%, or high 1.00%.",
  "#botEntryScore": "The minimum multi-timeframe setup score required before a paper entry.",
  "#botAllocation": "Caps how much paper equity may be allocated to one position.",
  "#botLowerPrice": "The bottom of the grid. Buy levels are distributed between the lower and upper prices.",
  "#botUpperPrice": "The top of the grid. Each filled slice targets the next grid level.",
  "#botGridSlices": "The number of evenly spaced price levels. More levels use smaller allocations and must still cover fees.",
  "#botGridStop": "A safety exit below the grid range for all filled grid slices.",
  "#learnerEvidence": "Champion and challenger results are isolated paper-only shadow simulations. Promotion requires at least 15 completed trades, 7 days, and a material risk-adjusted edge.",
  ".assistant-input-wrap": "Ask about current scanner evidence, a market symbol, risk, bots, or your paper portfolio.",
  ".suggested-prompts button": "Send this example question to the local market assistant.",
};

function installHelpTips(root = document) {
  if (root === document) {
    Object.entries(HELP_TIPS).forEach(([selector, tip]) => {
      document.querySelectorAll(selector).forEach(element => {
        if (!element.dataset.help) element.dataset.help = tip;
      });
    });
  }
  root.querySelectorAll("[data-help]").forEach(element => {
    element.title = element.dataset.help;
    element.setAttribute("aria-description", element.dataset.help);
  });
}

function renderMetrics() {
  const open = state.positions.filter(position => position.status === "open").length;
  const pnl = state.account.equity - state.account.starting_cash;
  document.querySelector("#metrics").innerHTML = `
    <div class="metric"><span>TOTAL P&amp;L</span><strong class="${pnl >= 0 ? "positive" : "negative"}">${money(pnl)}</strong></div>
    <div class="metric"><span>PAPER EQUITY</span><strong>${money(state.account.equity)}</strong></div>
    <div class="metric"><span>AVAILABLE CASH</span><strong>${money(state.account.cash)}</strong></div>
    <div class="metric"><span>OPEN TRADES</span><strong>${open}</strong></div>`;
  drawEquityChart();
}

function renderBots() {
  const container = document.querySelector("#activeBots");
  if (!liveAccountConnected) {
    container.innerHTML = `<div class="empty-action">Connect your NonKYC API key in <button type="button" class="linklike" id="goToLiveAccountSettingsRail">Settings</button> to see your real active grid bots here.</div>`;
    document.querySelector("#goToLiveAccountSettingsRail")?.addEventListener("click", () => showView("settings"));
    return;
  }
  const bots = liveGridBotsSnapshot?.bots || [];
  const botIds = bots.map(bot => bot.bot_id);
  container.innerHTML = bots.map(bot => {
    const running = bot.status === "RUNNING";
    return `<article class="bot-card compact real-bot ${bot.symbol === selectedSymbol ? "selected" : ""}" data-symbol="${escapeHtml(bot.symbol)}">
      <div class="bot-top">
        <div class="bot-identity">
          <span class="legend-swatch" style="background:${colorForBot(bot.bot_id, botIds)}"></span>
          <div><strong>${escapeHtml(bot.symbol)}</strong><small>Bot #${escapeHtml(bot.short_id)} · ${bot.grids} grids · ${bot.fills_are_estimated ? "~" : ""}${bot.fills} fills</small></div>
        </div>
        <span class="badge ${running ? "buy" : "watch"}">${bot.status}</span>
      </div>
      <div class="bot-pnl-row"><span>Total P&amp;L</span><b class="${pnlClass(bot.total_pnl)}">${money(bot.total_pnl)}</b></div>
    </article>`;
  }).join("") || `<div class="empty-action">No active grid bots found on your NonKYC account yet. Create one at <a href="https://nonkyc.io/account/gridbots" target="_blank" rel="noopener">nonkyc.io/account/gridbots</a>.</div>`;
  container.querySelectorAll(".real-bot").forEach(card => card.addEventListener("click", async () => {
    await selectMarket(card.dataset.symbol);
    document.querySelector(".chart-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

function bindBotCardEvents(container) {
  container.querySelectorAll(".bot-card").forEach(card => card.addEventListener("click", async event => {
    const botId = Number(card.dataset.botId);
    const symbol = card.dataset.symbol;
    const action = event.target.closest("[data-bot-action]")?.dataset.botAction;
    if (action === "collapse") {
      event.stopPropagation();
      if (expandedBotIds.has(botId)) {
        expandedBotIds.delete(botId);
      } else {
        expandedBotIds.add(botId);
      }
      localStorage.setItem("bitsbots-expanded-bots", [...expandedBotIds].join(","));
      renderBots();
      return;
    }
    if (action === "toggle") {
      event.stopPropagation();
      const bot = state.bots.find(item => item.id === botId);
      await updateBotStatus(botId, bot.status === "running" ? "paused" : "running");
      return;
    }
    if (action === "delete") {
      event.stopPropagation();
      await deleteBot(botId);
      return;
    }
    showView("dashboard");
    await selectMarket(symbol);
    document.querySelector(".chart-panel").scrollIntoView({ behavior: "smooth", block: "start" });
    renderBots();
  }));
  container.querySelectorAll(".new-bot-action").forEach(button => button.addEventListener("click", openBotDialog));
}

function renderMarketOptions(query = "") {
  const chartMarkets = availableChartMarkets.length
    ? availableChartMarkets
    : state.signals.map(signal => ({ symbol: signal.symbol }));
  const normalizedQuery = query.trim().toUpperCase();
  const filteredMarkets = chartMarkets.filter(
    market => market.symbol.toUpperCase().includes(normalizedQuery)
  );
  if (
    selectedSymbol
    && !filteredMarkets.some(market => market.symbol === selectedSymbol)
  ) {
    filteredMarkets.unshift({ symbol: selectedSymbol });
  }
  const options = filteredMarkets.map(market =>
    `<option value="${escapeHtml(market.symbol)}" ${market.symbol === selectedSymbol ? "selected" : ""}>${escapeHtml(market.symbol)}</option>`
  ).join("");
  document.querySelector("#marketSelect").innerHTML =
    options || `<option value="">No matching markets</option>`;
}

function renderSignals() {
  renderMarketOptions(document.querySelector("#marketSearch").value);
  document.querySelector("#signals").innerHTML = state.signals.map(signal => {
    const regime = signal.timeframe_scores.find(item => item.period === 1440)?.score || 0;
    return `<tr data-symbol="${signal.symbol}"><td><strong>${signal.symbol}</strong><br><small class="muted">${signal.history_quality} history</small></td>
      <td><div class="score"><b>${signal.score.toFixed(0)}</b><span class="bar"><i style="width:${signal.score}%"></i></span></div></td>
      <td>${price(signal.price)}</td><td>${regime.toFixed(0)} / 100</td>
      <td><span class="badge ${signal.action === "paper-buy" ? "buy" : signal.action}">${signal.action}</span></td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">Waiting for the first market scan...</td></tr>`;
  document.querySelectorAll("#signals tr[data-symbol]").forEach(row => row.addEventListener("click", () => selectMarket(row.dataset.symbol)));
  document.querySelector("#snapshot").innerHTML = state.signals.slice(0, 5).map(signal =>
    `<div class="snapshot-row"><strong>${signal.symbol}</strong><span>${price(signal.price)}</span><b>${signal.score.toFixed(0)}</b></div>`
  ).join("") || `<p class="muted">Scanning markets...</p>`;
}

function renderPositions() {
  const open = state.positions.filter(position => position.status === "open");
  document.querySelector("#positionCount").textContent = `${open.length} PAPER TRADES`;
  document.querySelector("#positions").innerHTML = open.map(position => {
    const current = state.signals.find(item => item.symbol === position.symbol)?.price || position.entry_price;
    const pnl = (current - position.entry_price) * position.quantity;
    return `<tr data-symbol="${position.symbol}"><td><strong>${position.symbol}</strong> <span class="buy">Long</span></td>
      <td>${price(position.entry_price)}</td><td>${price(current)}</td>
      <td class="${pnl >= 0 ? "positive" : "negative"}">${money(pnl)}</td><td><span class="badge buy">OPEN</span></td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">No simulated positions are open.</td></tr>`;
}

function renderWorkspaceViews() {
  const bots = state.bots || [];
  const botManagement = document.querySelector("#botManagement");
  botManagement.innerHTML = bots.map(bot => {
    const running = bot.status === "running";
    const description = bot.strategy === "Multi-TF"
      ? `Entries require score ${bot.entry_score.toFixed(0)} or higher.`
      : `${bot.grid_slices} slices from ${price(bot.lower_price)} to ${price(bot.upper_price)}. Stop ${price(bot.grid_stop_price)}.`;
    return `<article class="workspace-card bot-card" data-bot-id="${bot.id}" data-symbol="${escapeHtml(bot.symbol)}">
      <div class="bot-top"><div><span class="badge ${running ? "buy" : "watch"}">${bot.status}</span><h2>${escapeHtml(bot.name)}</h2></div><strong>${escapeHtml(bot.symbol)}</strong></div>
      <p>${escapeHtml(bot.strategy)} strategy. ${description}</p>
      <dl><div><dt>Risk per trade</dt><dd>${{ low: "0.25%", medium: "0.50%", high: "1.00%" }[bot.risk_level]}</dd></div>
        <div><dt>Maximum allocation</dt><dd>${(bot.max_allocation * 100).toFixed(0)}%</dd></div></dl>
      <div class="bot-actions"><button data-bot-action="toggle" data-help="Pause or resume new entries for this market.">${running ? "Pause bot" : "Resume bot"}</button>
        <button data-bot-action="delete" data-help="Remove this configuration without deleting historical trades.">Delete</button></div>
    </article>`;
  }).join("") || `<article class="workspace-card empty-action">No bots yet. A bot defines which market is allowed to open paper positions.<button class="primary new-bot-action" data-help="Open the bot configuration form.">＋ Create Paper Bot</button></article>`;
  bindBotCardEvents(botManagement);
  installHelpTips(botManagement);
  const learning = state.learning || { versions: [], promotions: [] };
  const evidence = document.querySelector("#learnerEvidence");
  evidence.innerHTML = learning.versions.length ? `
    <table class="learner-table"><thead><tr><th>Scope</th><th>Role</th><th>Evidence</th><th>Objective</th></tr></thead><tbody>
    ${learning.versions.map(version => `<tr>
      <td>${escapeHtml(version.strategy)} · ${escapeHtml(version.market)}<small>immutable version #${version.id}</small></td>
      <td><span class="badge ${version.role === "champion" ? "buy" : "watch"}">${escapeHtml(version.role)}</span></td>
      <td>${version.completed_trades == null ? "Awaiting stored candles" : `${version.completed_trades} trades · ${version.started_at && version.ended_at ? ((version.ended_at - version.started_at) / 86400000).toFixed(1) : "0.0"} days`}
        <small>${escapeHtml(version.reason || "Not evaluated")}</small></td>
      <td>${version.objective == null ? "—" : `${(version.objective * 100).toFixed(2)}%`}
        <small>net ${version.net_return == null ? "—" : (version.net_return * 100).toFixed(2) + "%"} · drawdown ${version.max_drawdown == null ? "—" : (version.max_drawdown * 100).toFixed(2) + "%"}</small></td>
    </tr>`).join("")}</tbody></table>
    <h3>Promotion history</h3>
    ${learning.promotions.map(item => `<p><strong>${escapeHtml(item.strategy)} · ${escapeHtml(item.market)}</strong> version #${item.prior_version_id} → #${item.promoted_version_id}<small> ${escapeHtml(item.reason)}</small></p>`).join("") || `<p class="muted">No automatic promotions yet.</p>`}
  ` : `<p class="muted">Evidence appears after the next successful scan for a configured paper bot.</p>`;
  installHelpTips(evidence);

  const open = state.positions.filter(position => position.status === "open");
  const realized = state.positions.reduce((total, position) => total + (position.realized_pnl || 0), 0);
  document.querySelector("#portfolioSummary").innerHTML = realUsdtBalance ? `
    <div class="summary-card"><span>USDT AVAILABLE</span><strong>${money(realUsdtBalance.available)}</strong></div>
    <div class="summary-card"><span>USDT IN ORDERS</span><strong>${money(realUsdtBalance.held)}</strong></div>
    <div class="summary-card"><span>USDT TOTAL BALANCE</span><strong>${money(realUsdtBalance.total)}</strong></div>` : `
    <div class="summary-card full-width"><span>NONKYC BALANCE</span><strong>Connect your NonKYC API key in Settings to see your real USDT balance here.</strong></div>`;
  document.querySelector("#portfolioPositions").innerHTML = state.positions.map(position => {
    const current = state.signals.find(signal => signal.symbol === position.symbol)?.price;
    return `<tr><td>${position.symbol}</td><td><span class="badge ${position.status === "open" ? "buy" : "watch"}">${position.status}</span></td>
      <td>${price(position.entry_price)}</td><td>${price(position.exit_price || current || position.entry_price)}</td>
      <td>${compactNumber(position.quantity)}</td><td class="${(position.realized_pnl || 0) >= 0 ? "positive" : "negative"}">${position.realized_pnl == null ? "—" : money(position.realized_pnl)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">No paper positions recorded.</td></tr>`;

  if (currentProfile) {
    document.querySelector("#profileSettings").innerHTML = `
      <div><dt>Display name</dt><dd>${escapeHtml(currentProfile.display_name)}</dd></div>
      <div><dt>Email</dt><dd>${escapeHtml(currentProfile.email)}</dd></div>
      <div><dt>Storage</dt><dd>Local SQLite</dd></div>
      <div><dt>Trading mode</dt><dd class="caution">Paper bots simulate only. Real orders require Live Trading to be enabled and confirmed.</dd></div>`;
  }
  renderAssistantContext();
}

function renderActivity() {
  document.querySelector("#alerts").innerHTML = state.alerts.map(alert =>
    `<p><small>${time(alert.created_at)}</small><br>${alert.message}</p>`
  ).join("") || `<p class="muted">No activity yet.</p>`;
  document.querySelector("#errors").innerHTML = state.errors.map(error => `<p>${error}</p>`).join("");
  if (Notification.permission === "granted") {
    state.alerts.forEach(alert => {
      const key = `${alert.created_at}:${alert.message}`;
      if (!seenAlerts.has(key)) {
        seenAlerts.add(key);
        new Notification("BitsBots paper alert", { body: alert.message });
      }
    });
  }
}

function compactNumber(value) {
  return Number(value).toLocaleString("en-US", {
    maximumSignificantDigits: 6,
    notation: Math.abs(value) >= 1_000_000 ? "compact" : "standard",
  });
}

function setStreamStatus(text, stateClass = "") {
  const status = document.querySelector("#streamStatus");
  status.textContent = text;
  status.className = `stream-status ${stateClass}`;
}

function parseBookLevel(level) {
  const rawPrice = Array.isArray(level) ? level[0] : level?.price;
  const rawQuantity = Array.isArray(level) ? level[1] : level?.quantity;
  const parsedPrice = Number(rawPrice); const quantity = Number(rawQuantity);
  return Number.isFinite(parsedPrice) && Number.isFinite(quantity)
    ? { price: parsedPrice, quantity }
    : null;
}

// Renders your own real NonKYC open orders and recent fills - this
// replaces the old generic public order-book widget, which showed the
// whole market's book rather than what you've actually bought and sold.
function renderMyOrdersPanel() {
  const openOrdersEl = document.querySelector("#myOpenOrders");
  const recentFillsEl = document.querySelector("#myRecentFills");
  if (!openOrdersEl || !recentFillsEl) return;
  if (!liveAccountConnected) {
    openOrdersEl.innerHTML = `<p class="muted">Connect your NonKYC API key in Settings to see your real orders here.</p>`;
    recentFillsEl.innerHTML = "";
    document.querySelector("#myOpenOrdersCount").textContent = "—";
    document.querySelector("#myRecentFillsCount").textContent = "—";
    return;
  }
  const openOrders = (myAccountSnapshot?.open_orders || []).slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const recentFills = (myAccountSnapshot?.recent_trades || []).slice().sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  document.querySelector("#myOpenOrdersCount").textContent = openOrders.length;
  document.querySelector("#myRecentFillsCount").textContent = recentFills.length;
  const orderRow = order => `<div class="book-row"><span><span class="badge ${order.side === "buy" ? "buy" : "watch"}">${order.side}</span> ${escapeHtml(order.symbol)}</span><span>${price(order.price)}</span><span>${compactNumber(order.remaining ?? order.quantity)}</span></div>`;
  const fillRow = trade => `<div class="book-row"><span><span class="badge ${trade.side === "buy" ? "buy" : "watch"}">${trade.side}</span> ${escapeHtml(trade.symbol)}</span><span>${price(trade.price)}</span><span>${compactNumber(trade.quantity)}</span></div>`;
  openOrdersEl.innerHTML = openOrders.slice(0, 12).map(orderRow).join("") || `<p class="muted">No open orders.</p>`;
  recentFillsEl.innerHTML = recentFills.slice(0, 12).map(fillRow).join("") || `<p class="muted">No recent fills.</p>`;
}

function normalizeTrades(payload) {
  const items = Array.isArray(payload) ? payload : payload ? [payload] : [];
  return items.map(trade => ({
    id: String(trade.id || `${trade.timestampms}:${trade.price}:${trade.quantity}`),
    price: Number(trade.price),
    quantity: Number(trade.quantity),
    side: trade.side === "sell" ? "sell" : "buy",
    timestamp: Number(trade.timestampms || Date.parse(trade.timestamp)),
  })).filter(trade => Number.isFinite(trade.price) && Number.isFinite(trade.quantity));
}

function renderTrades() {
  document.querySelector("#liveTrades").innerHTML = recentTrades.slice(0, 10).map(trade =>
    `<div class="trade-row ${trade.side}-trade"><span>${time(trade.timestamp)}</span>
      <span>${price(trade.price)}</span><span>${compactNumber(trade.quantity)}</span></div>`
  ).join("") || `<p class="muted">Waiting for trades...</p>`;
}

function connectMarketStream(symbol) {
  if (!symbol || (symbol === streamSymbol && marketSocket?.readyState <= WebSocket.OPEN)) return;
  streamSymbol = symbol;
  streamGeneration += 1;
  const generation = streamGeneration;
  clearTimeout(reconnectTimer);
  if (marketSocket) marketSocket.close();
  recentTrades = [];
  document.querySelector("#bookSymbol").textContent = symbol;
  renderTrades();
  setStreamStatus("CONNECTING");

  const socket = new WebSocket("wss://api.nonkyc.io");
  marketSocket = socket;
  socket.addEventListener("open", () => {
    if (generation !== streamGeneration) return;
    reconnectAttempt = 0;
    setStreamStatus("LIVE", "connected");
    socket.send(JSON.stringify({ method: "subscribeTrades", params: { symbol }, id: 102 }));
  });
  socket.addEventListener("message", event => {
    if (generation !== streamGeneration) return;
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    const params = message.params || {};
    if (message.method === "snapshotTrades") {
      recentTrades = normalizeTrades(params.data).sort((left, right) => right.timestamp - left.timestamp).slice(0, 50);
      renderTrades();
    } else if (message.method === "updateTrades") {
      const incoming = normalizeTrades(params.data);
      const known = new Set(recentTrades.map(trade => trade.id));
      recentTrades = [...incoming.filter(trade => !known.has(trade.id)), ...recentTrades]
        .sort((left, right) => right.timestamp - left.timestamp).slice(0, 50);
      renderTrades();
    }
  });
  socket.addEventListener("error", () => socket.close());
  socket.addEventListener("close", () => {
    if (generation !== streamGeneration) return;
    setStreamStatus("RECONNECTING", "disconnected");
    const delay = Math.min(15_000, 1_000 * 2 ** reconnectAttempt++);
    reconnectTimer = setTimeout(() => {
      if (streamSymbol === symbol) {
        streamSymbol = null;
        connectMarketStream(symbol);
      }
    }, delay);
  });
}

async function selectMarket(symbol) {
  if (!symbol) return;
  const generation = ++chartSelectionGeneration;
  selectedSymbol = symbol;
  connectMarketStream(symbol);
  const marketSelect = document.querySelector("#marketSelect");
  if (![...marketSelect.options].some(option => option.value === symbol)) {
    marketSelect.add(new Option(symbol, symbol));
  }
  marketSelect.value = symbol;
  const signal = state.signals.find(item => item.symbol === symbol);
  const market = availableChartMarkets.find(item => item.symbol === symbol);
  document.querySelector("#chartSymbol").textContent = `${symbol} · ${tfLabel(selectedPeriod)}`;
  document.querySelector("#chartPrice").textContent = price(signal?.price ?? market?.last_price);
  const badge = document.querySelector("#chartSignal");
  if (signal) {
    badge.textContent = `PAPER ${signal.action.replace("paper-", "").toUpperCase()} SIGNAL ${signal.score.toFixed(0)}`;
    badge.className = `badge ${signal.action === "paper-buy" ? "buy" : signal.action}`;
    document.querySelector("#detail").innerHTML = `
      <div class="timeframes">${signal.timeframe_scores.map(item =>
        `<div class="timeframe"><small>${tfLabel(item.period)}</small><strong>${item.score.toFixed(0)}</strong></div>`
      ).join("")}</div>
      <ul class="reasons">${signal.reasons.slice(0, 6).map(reason => `<li>${reason}</li>`).join("")}</ul>`;
  } else {
    badge.textContent = "CHART ONLY";
    badge.className = "badge watch";
    document.querySelector("#detail").innerHTML =
      `<p class="muted">${escapeHtml(symbol)} is available on NonKYC but is outside the current deep-scan set. Its candle chart is loaded on demand.</p>`;
  }
  const response = await fetch(`/api/candles/${encodeURIComponent(symbol)}?period=${selectedPeriod}`);
  const nextCandles = response.ok ? await response.json() : [];
  if (generation !== chartSelectionGeneration || symbol !== selectedSymbol) return;
  const nextKey = `${symbol}:${selectedPeriod}`;
  if (nextKey !== chartDataKey || !activeCandles.length) {
    chartView = { start: 0, end: nextCandles.length };
    chartDataKey = nextKey;
  } else {
    const visibleCount = chartView.end - chartView.start;
    const wasAtLatest = chartView.end >= activeCandles.length;
    chartView.end = wasAtLatest
      ? nextCandles.length
      : Math.min(chartView.end, nextCandles.length);
    chartView.start = Math.max(0, chartView.end - visibleCount);
  }
  activeCandles = nextCandles;
  hoverIndex = null;
  drawMarketChart();
}

function setupCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = rect.width * ratio;
  canvas.height = rect.height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  return { context, width: rect.width, height: rect.height };
}

function ema(values, period) {
  if (!values.length) return [];
  const alpha = 2 / (period + 1);
  const result = [values[0]];
  for (let index = 1; index < values.length; index++) {
    result.push(alpha * values[index] + (1 - alpha) * result[index - 1]);
  }
  return result;
}

function bollinger(values, period = 20, multiplier = 2) {
  return values.map((_, index) => {
    if (index < period - 1) return null;
    const window = values.slice(index - period + 1, index + 1);
    const middle = window.reduce((total, value) => total + value, 0) / period;
    const deviation = Math.sqrt(window.reduce((total, value) => total + (value - middle) ** 2, 0) / period);
    return { middle, upper: middle + multiplier * deviation, lower: middle - multiplier * deviation };
  });
}

function rsiSeries(values, period = 14) {
  return values.map((_, index) => {
    if (index < period) return null;
    let gains = 0; let losses = 0;
    for (let cursor = index - period + 1; cursor <= index; cursor++) {
      const change = values[cursor] - values[cursor - 1];
      gains += Math.max(change, 0); losses += Math.max(-change, 0);
    }
    if (losses === 0) return gains > 0 ? 100 : 50;
    return 100 - 100 / (1 + gains / losses);
  });
}

function macdSeries(values) {
  const fast = ema(values, 12); const slow = ema(values, 26);
  const macd = values.map((_, index) => fast[index] - slow[index]);
  const signal = ema(macd, 9);
  return { macd, signal, histogram: macd.map((value, index) => value - signal[index]) };
}

function drawLine(ctx, values, xAt, yAt, color, width = 1.2) {
  ctx.beginPath(); let drawing = false;
  values.forEach((value, index) => {
    if (value == null || !Number.isFinite(value)) return;
    drawing ? ctx.lineTo(xAt(index), yAt(value)) : ctx.moveTo(xAt(index), yAt(value));
    drawing = true;
  });
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
}

function drawGrid(ctx, width, height, rows = 4) {
  ctx.strokeStyle = "#242424"; ctx.lineWidth = 1;
  for (let index = 1; index < rows; index++) {
    const y = index * height / rows;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
}

function drawTradeMarkers(ctx, candles, xAt, yAt) {
  const interval = selectedPeriod * 60_000;
  state.positions.filter(position => position.symbol === selectedSymbol).forEach(position => {
    [
      { timestamp: position.opened_at, value: position.entry_price, label: "PAPER BUY", color: "#45d483", up: true },
      position.closed_at && position.exit_price
        ? { timestamp: position.closed_at, value: position.exit_price, label: "PAPER SELL", color: "#ff554c", up: false }
        : null,
    ].filter(Boolean).forEach(marker => {
      let nearest = 0;
      candles.forEach((candle, index) => {
        if (Math.abs(candle.timestamp - marker.timestamp) < Math.abs(candles[nearest].timestamp - marker.timestamp)) nearest = index;
      });
      if (Math.abs(candles[nearest].timestamp - marker.timestamp) > interval * 2) return;
      const x = xAt(nearest); const y = yAt(marker.value); const direction = marker.up ? -1 : 1;
      ctx.fillStyle = marker.color; ctx.beginPath();
      ctx.moveTo(x, y); ctx.lineTo(x - 5, y + direction * 9); ctx.lineTo(x + 5, y + direction * 9); ctx.closePath(); ctx.fill();
      ctx.font = "bold 8px Segoe UI"; ctx.textAlign = "center";
      ctx.fillText(marker.label, x, y + direction * 17);
    });
  });
}

function drawMarketChart() {
  const start = Math.max(0, chartView.start);
  const end = Math.min(activeCandles.length, chartView.end || activeCandles.length);
  const candles = activeCandles.slice(start, end);
  const { context: ctx, width, height } = setupCanvas(document.querySelector("#marketChart"));
  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, width, height, 5);
  if (!candles.length) {
    ctx.fillStyle = "#9d968f"; ctx.fillText("Candle history will appear after the scan.", 20, 35); return;
  }
  const allCloses = activeCandles.map(candle => candle.close);
  const closes = candles.map(candle => candle.close);
  const trend = ema(allCloses, 20).slice(start, end);
  const bands = bollinger(allCloses).slice(start, end);
  const values = candles.flatMap(candle => [candle.high, candle.low]);
  if (indicators.bands) bands.filter(Boolean).forEach(band => values.push(band.upper, band.lower));
  if (typeof gridBotChartPrices === "function") values.push(...gridBotChartPrices(selectedSymbol));
  const min = Math.min(...values); const max = Math.max(...values); const range = max - min || 1;
  const plotWidth = width - 66; const xStep = plotWidth / candles.length;
  const xAt = index => index * xStep + xStep / 2;
  const yAt = value => 10 + (max - value) / range * (height - 30);

  if (indicators.bands) {
    drawLine(ctx, bands.map(item => item?.upper), xAt, yAt, "#4e79b9", 1);
    drawLine(ctx, bands.map(item => item?.middle), xAt, yAt, "#5c6572", 1);
    drawLine(ctx, bands.map(item => item?.lower), xAt, yAt, "#4e79b9", 1);
  }
  if (indicators.ema) drawLine(ctx, trend, xAt, yAt, "#69a7ff", 1.5);

  candles.forEach((candle, index) => {
    const x = xAt(index); const rising = candle.close >= candle.open;
    ctx.strokeStyle = rising ? "#45d483" : "#ff554c"; ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath(); ctx.moveTo(x, yAt(candle.high)); ctx.lineTo(x, yAt(candle.low)); ctx.stroke();
    const top = yAt(Math.max(candle.open, candle.close)); const bottom = yAt(Math.min(candle.open, candle.close));
    ctx.fillRect(x - Math.max(1, xStep * .28), top, Math.max(2, xStep * .56), Math.max(1, bottom - top));
  });
  drawTradeMarkers(ctx, candles, xAt, yAt);
  if (typeof drawGridBotOverlay === "function") drawGridBotOverlay(ctx, candles, xAt, yAt, plotWidth);

  ctx.fillStyle = "#8d8d8d"; ctx.font = "9px Segoe UI"; ctx.textAlign = "left";
  for (let index = 0; index <= 4; index++) {
    const value = max - range * index / 4;
    ctx.fillText(price(value), plotWidth + 6, 13 + (height - 30) * index / 4);
  }
  [0, Math.floor((candles.length - 1) / 2), candles.length - 1].forEach(index => {
    ctx.textAlign = index === 0 ? "left" : index === candles.length - 1 ? "right" : "center";
    ctx.fillText(new Date(candles[index].timestamp).toLocaleDateString([], { month: "short", day: "numeric" }), xAt(index), height - 3);
  });

  if (hoverIndex != null && candles[hoverIndex]) {
    const candle = candles[hoverIndex]; const x = xAt(hoverIndex);
    ctx.strokeStyle = "#7f8fa6"; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); ctx.setLineDash([]);
    document.querySelector("#indicatorLegend").textContent =
      `${new Date(candle.timestamp).toLocaleString()}  O ${price(candle.open)}  H ${price(candle.high)}  L ${price(candle.low)}  C ${price(candle.close)}  V ${price(candle.volume)}`;
  } else {
    const latestBand = bands.at(-1); const latestRsi = rsiSeries(allCloses).slice(start, end).at(-1);
    document.querySelector("#indicatorLegend").textContent =
      `EMA20 ${price(trend.at(-1))}  BB ${latestBand ? `${price(latestBand.lower)}–${price(latestBand.upper)}` : "—"}  RSI ${latestRsi?.toFixed(1) ?? "—"}  ·  ${candles.length}/${activeCandles.length} candles`;
  }
  drawVolume(candles, xAt, xStep);
  drawRsi(rsiSeries(allCloses).slice(start, end), xAt);
  drawMacd(allCloses, start, end, xAt);
}

function drawVolume(candles, xAt, xStep) {
  const canvas = document.querySelector("#volumeChart");
  const { context: ctx, width, height } = setupCanvas(canvas);
  ctx.clearRect(0, 0, width, height); drawGrid(ctx, width, height, 2);
  const maximum = Math.max(...candles.map(candle => candle.volume), 1);
  candles.forEach((candle, index) => {
    const barHeight = candle.volume / maximum * (height - 8);
    ctx.fillStyle = candle.close >= candle.open ? "#245d3c" : "#68292d";
    ctx.fillRect(xAt(index) - Math.max(1, xStep * .3), height - barHeight, Math.max(2, xStep * .6), barHeight);
  });
}

function drawRsi(values, xAt) {
  const { context: ctx, width, height } = setupCanvas(document.querySelector("#rsiChart"));
  ctx.clearRect(0, 0, width, height);
  const yAt = value => height - value / 100 * height;
  [30, 70].forEach(level => {
    ctx.strokeStyle = level === 70 ? "#68292d" : "#5c4d20"; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(0, yAt(level)); ctx.lineTo(width, yAt(level)); ctx.stroke();
  });
  ctx.setLineDash([]); drawLine(ctx, values, xAt, yAt, "#e8bd55", 1.2);
}

function drawMacd(closes, start, end, xAt) {
  const { context: ctx, width, height } = setupCanvas(document.querySelector("#macdChart"));
  ctx.clearRect(0, 0, width, height);
  const fullValues = macdSeries(closes);
  const values = {
    macd: fullValues.macd.slice(start, end),
    signal: fullValues.signal.slice(start, end),
    histogram: fullValues.histogram.slice(start, end),
  };
  const extent = Math.max(...values.macd.map(Math.abs), ...values.signal.map(Math.abs), 0.000001);
  const yAt = value => height / 2 - value / extent * (height * .42);
  ctx.strokeStyle = "#333"; ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke();
  const step = (width - 66) / Math.max(values.macd.length, 1);
  values.histogram.forEach((value, index) => {
    ctx.fillStyle = value >= 0 ? "#245d3c" : "#68292d";
    const y = yAt(value); ctx.fillRect(xAt(index) - step * .25, Math.min(y, height / 2), Math.max(1, step * .5), Math.abs(y - height / 2));
  });
  drawLine(ctx, values.macd, xAt, yAt, "#69a7ff", 1.1);
  drawLine(ctx, values.signal, xAt, yAt, "#e8bd55", 1.1);
}

function drawEquityChart() {
  const { context: ctx, width, height } = setupCanvas(document.querySelector("#equityChart"));
  const start = state.account.starting_cash; const end = state.account.equity;
  const points = [start, start * .998, start * 1.003, start * 1.001, (start + end) / 2, end];
  const min = Math.min(...points) * .995; const max = Math.max(...points) * 1.005; const range = max - min || 1;
  ctx.beginPath(); points.forEach((value, index) => {
    const x = 10 + index * (width - 20) / (points.length - 1);
    const y = height - 10 - (value - min) / range * (height - 20);
    index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = end >= start ? "#45d483" : "#ff554c"; ctx.lineWidth = 2; ctx.stroke();
}

async function refresh() {
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error(`Dashboard API returned ${response.status}`);
    state = await response.json();
    document.querySelector("#status").textContent = `${state.status} · ${time(state.last_scan_at)}`;
    if (!selectedSymbol && state.signals.length) selectedSymbol = state.signals[0].symbol;
    renderMetrics(); renderBots(); renderSignals(); renderPositions(); renderActivity();
    renderWorkspaceViews();
    if (selectedSymbol) await selectMarket(selectedSymbol);
    if (
      document.querySelector("#botDialog").open
      && !currentBotRecommendation
      && state.signals.length
      && document.querySelector("#botRecommendation").textContent.includes("has not finished")
    ) {
      await autoGenerateBot();
    }
  } catch (error) {
    document.querySelector("#status").textContent = error.message;
  }
}

async function loadChartMarkets() {
  try {
    const response = await fetch("/api/markets");
    if (!response.ok) throw new Error(`Market API returned ${response.status}`);
    const payload = await response.json();
    availableChartMarkets = payload.markets || [];
    if (state) renderSignals();
    return availableChartMarkets;
  } catch (error) {
    if (!availableChartMarkets.length && state) renderSignals();
    return [];
  }
}

async function loadProfile() {
  const response = await fetch("/api/auth/me");
  if (response.status === 401) {
    window.location.assign("/login");
    return;
  }
  const payload = await response.json();
  currentProfile = payload.user;
  document.querySelector("#profileName").textContent = payload.user.display_name;
  if (state) renderWorkspaceViews();
}

async function loadLiveAccountStatus() {
  try {
    const response = await fetch("/api/live-account/credentials");
    if (!response.ok) throw new Error("Unable to check connection status");
    const payload = await response.json();
    liveAccountConnected = payload.connected;
    liveAccountMaskedKey = payload.masked_access_key;
  } catch (error) {
    liveAccountConnected = false;
    liveAccountMaskedKey = null;
  }
  renderLiveAccountSettings();
  if (liveAccountConnected) {
    await refreshLiveAccountPanel();
  } else {
    renderLiveAccountPanel(null, null);
  }
}

function renderLiveAccountSettings() {
  const status = document.querySelector("#liveAccountStatus");
  const disconnectButton = document.querySelector("#disconnectLiveAccount");
  const form = document.querySelector("#liveAccountForm");
  if (liveAccountConnected) {
    status.textContent = `Connected · access key ${liveAccountMaskedKey}`;
    status.classList.remove("muted");
    disconnectButton.classList.remove("hidden");
    form.querySelector("button[type=submit]").textContent = "Reconnect with a different key";
  } else {
    status.textContent = "Not connected. Add your NonKYC API key below to see your real balances, open orders, and recent trades.";
    status.classList.add("muted");
    disconnectButton.classList.add("hidden");
    form.querySelector("button[type=submit]").textContent = "Connect account";
  }
}

function pnlClass(value) {
  return value > 0 ? "positive" : value < 0 ? "negative" : "";
}

const GRID_BOT_CHART_COLORS = [
  "#3d8bff", "#45d483", "#ff9f43", "#ff554c", "#a66bff",
  "#2dd4bf", "#f5d442", "#ff7edb", "#8bc34a", "#e57373",
];

// Real bots are colored by their stable bot_id (not by market symbol), so
// two different bots on the same coin still get distinct colors and one
// bot's color never changes as other bots start/stop.
function colorForBot(botId, botIds) {
  const index = botIds.indexOf(botId);
  return GRID_BOT_CHART_COLORS[(index < 0 ? 0 : index) % GRID_BOT_CHART_COLORS.length];
}

function drawGridBotsHistoryChart(canvas, history) {
  if (!canvas) return;
  const { context: ctx, width, height } = setupCanvas(canvas);
  ctx.clearRect(0, 0, width, height);
  if (!history.length) {
    ctx.fillStyle = "#8d8d8d"; ctx.font = "11px Segoe UI"; ctx.textAlign = "left";
    ctx.fillText("P&L history builds up over time as BitsBots keeps polling your account.", 10, height / 2);
    return;
  }
  const byBot = {};
  history.forEach(point => { (byBot[point.bot_id] ||= []).push(point); });
  const botIds = Object.keys(byBot);
  const times = history.map(point => point.recorded_at);
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const pnls = history.map(point => point.total_pnl);
  const minP = Math.min(...pnls, 0);
  const maxP = Math.max(...pnls, 0);
  const pad = Math.max((maxP - minP) * 0.15, 0.5);
  const yMin = minP - pad;
  const yMax = maxP + pad;
  // Leave room on the right for the min/max $ labels, and enough top/bottom/
  // left margin that the line and its end-of-line dot never touch (and look
  // like they run off) the edge of the canvas.
  const padL = 6, padR = 38, padT = 10, padB = 10;
  const xOf = t => padL + (maxT > minT ? (t - minT) / (maxT - minT) : 0.5) * (width - padL - padR);
  const yOf = v => padT + (1 - (v - yMin) / ((yMax - yMin) || 1)) * (height - padT - padB);

  ctx.strokeStyle = "#2a2a2a"; ctx.beginPath();
  ctx.moveTo(padL, yOf(0)); ctx.lineTo(width - padR, yOf(0)); ctx.stroke();

  botIds.forEach(botId => {
    const points = byBot[botId].slice().sort((a, b) => a.recorded_at - b.recorded_at);
    ctx.strokeStyle = colorForBot(botId, botIds);
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xOf(point.recorded_at); const y = yOf(point.total_pnl);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    if (points.length === 1) {
      const only = points[0];
      ctx.arc(xOf(only.recorded_at), yOf(only.total_pnl), 2, 0, Math.PI * 2);
    }
    ctx.stroke();
    const last = points[points.length - 1];
    ctx.fillStyle = colorForBot(botId, botIds);
    ctx.beginPath(); ctx.arc(xOf(last.recorded_at), yOf(last.total_pnl), 2.5, 0, Math.PI * 2); ctx.fill();
  });

  // Plain $ scale on the right edge so the line heights mean something at a
  // glance instead of being unlabeled squiggles.
  ctx.fillStyle = "#8d8d8d"; ctx.font = "9px Segoe UI"; ctx.textAlign = "left";
  ctx.fillText(money(yMax), width - padR + 4, padT + 3);
  ctx.fillText(money(yMin), width - padR + 4, height - padB + 3);
  if (yMin < 0 && yMax > 0) ctx.fillText("$0", width - padR + 4, yOf(0) + 3);
}

function gridBotsHistoryLegendHtml(history) {
  const seen = new Map();
  history.forEach(point => { if (!seen.has(point.bot_id)) seen.set(point.bot_id, point.symbol); });
  const botIds = [...seen.keys()];
  if (!botIds.length) return "";
  return `<div class="history-legend">${botIds.map(botId => `
    <span class="legend-item"><span class="legend-swatch" style="background:${colorForBot(botId, botIds)}"></span>${escapeHtml(seen.get(botId))} <small class="muted">#${escapeHtml(botId.slice(0, 8))}</small></span>`).join("")}</div>`;
}

function gridBotStatSummary(snapshot, mini = false) {
  const rowClass = mini ? "summary-row live-account-stats live-account-stats-mini" : "summary-row live-account-stats";
  const runningTile = mini
    ? `<div class="summary-card"><span>BOTS RUNNING</span><strong>${snapshot.bots_running}</strong></div>`
    : `<div class="summary-card"><span>BOTS</span><strong>${snapshot.bots_running} running / ${snapshot.bots_idle} idle</strong></div>`;
  return `<div class="${rowClass}">
    ${runningTile}
    <div class="summary-card"><span>GRID PROFIT (BOOKED)</span><strong class="${pnlClass(snapshot.grid_profit_booked)}">${money(snapshot.grid_profit_booked)}</strong></div>
    <div class="summary-card"><span>UNREALIZED</span><strong class="${pnlClass(snapshot.unrealized)}">${money(snapshot.unrealized)}</strong></div>
    <div class="summary-card"><span>TOTAL P&amp;L</span><strong class="${pnlClass(snapshot.total_pnl)}">${money(snapshot.total_pnl)}</strong></div>
  </div>`;
}

// Compact, single-line version of the same numbers for the dashboard hero
// panel - plain small chips instead of bordered/padded cards, so the same
// information takes a fraction of the vertical space.
function gridBotMiniStrip(snapshot) {
  const best = snapshot.best_performer;
  const worst = snapshot.worst_performer;
  const chip = (label, valueHtml) => `<div class="stat-chip"><span>${label}</span><strong>${valueHtml}</strong></div>`;
  const performerChip = (label, bot) => bot
    ? chip(label, `${escapeHtml(bot.symbol)} <span class="${pnlClass(bot.total_pnl)}">${money(bot.total_pnl)}</span>`)
    : "";
  return `<div class="stat-chip-row">
    ${chip("BOTS", snapshot.bots_running)}
    ${chip("BOOKED", `<span class="${pnlClass(snapshot.grid_profit_booked)}">${money(snapshot.grid_profit_booked)}</span>`)}
    ${chip("UNREALIZED", `<span class="${pnlClass(snapshot.unrealized)}">${money(snapshot.unrealized)}</span>`)}
    ${chip("TOTAL P&amp;L", `<span class="${pnlClass(snapshot.total_pnl)}">${money(snapshot.total_pnl)}</span>`)}
    ${performerChip("BEST", best)}
    ${performerChip("WORST", worst)}
  </div>`;
}


function gridBotPerformerCards(snapshot) {
  const best = snapshot.best_performer; const worst = snapshot.worst_performer;
  if (!best) return "";
  const card = (title, bot, cls) => bot ? `
    <div class="performer-card ${cls}">
      <span class="performer-label">${title}</span>
      <strong>${escapeHtml(bot.symbol)}</strong>
      <span class="${pnlClass(bot.total_pnl)}">${money(bot.total_pnl)}</span>
    </div>` : "";
  return `<div class="performer-row">
    ${card("BEST PERFORMER", best, "best")}
    ${card("WORST PERFORMER", worst || best, "worst")}
  </div>`;
}

function gridBotTableHtml(bots) {
  if (!bots.length) return `<p class="muted">No grid bot activity found on your NonKYC account yet.</p>`;
  const showBotId = bots.some(bot => bot.short_id);
  return `<div class="table-wrap"><table><thead><tr>
    ${showBotId ? "<th>Bot</th>" : ""}<th>Market</th><th>Status</th><th>Grids</th><th>Range</th><th>Invested</th><th>Fills</th>
    <th>Grid Profit</th><th>Unrealized</th><th>Total P&amp;L</th></tr></thead>
    <tbody>${bots.map(bot => `<tr>
      ${showBotId ? `<td>#${escapeHtml(bot.short_id || "—")}</td>` : ""}
      <td>${escapeHtml(bot.symbol)}</td>
      <td><span class="badge ${bot.status === "RUNNING" ? "buy" : "watch"}">${escapeHtml(bot.status)}</span></td>
      <td>${bot.grids}</td>
      <td>${bot.range_low != null ? `${price(bot.range_low)} – ${price(bot.range_high)}` : "—"}</td>
      <td>${money(bot.invested)}</td>
      <td>${bot.fills_are_estimated ? "~" : ""}${bot.fills}</td>
      <td class="${pnlClass(bot.grid_profit)}">${money(bot.grid_profit)}</td>
      <td class="${pnlClass(bot.unrealized)}">${money(bot.unrealized)}</td>
      <td class="${pnlClass(bot.total_pnl)}">${money(bot.total_pnl)}</td>
    </tr>`).join("")}</tbody></table></div>`;
}

function renderLiveAccountPanel(snapshot, errorMessage, history) {
  const body = document.querySelector("#liveAccountBody");
  if (!liveAccountConnected) {
    body.innerHTML = `<p class="muted">Connect your NonKYC API key in <button type="button" class="linklike" id="goToLiveAccountSettings" data-help="Open Settings to connect your NonKYC API key.">Settings</button> to see your real grid bots here, reconstructed from your NonKYC order and trade history. This never places or changes orders.</p>`;
    document.querySelector("#goToLiveAccountSettings")?.addEventListener("click", () => showView("settings"));
    installHelpTips(body);
    return;
  }
  if (errorMessage && !snapshot) {
    body.innerHTML = `<p class="form-error">${escapeHtml(errorMessage)}</p>`;
    return;
  }
  if (!snapshot) {
    body.innerHTML = `<p class="muted">Loading your live grid bots…</p>`;
    return;
  }
  const bots = snapshot.bots || [];
  const otherActivity = snapshot.other_activity || [];
  const historyPoints = history || [];
  const wasOtherActivityOpen = body.querySelector(".other-activity")?.open ?? false;
  body.innerHTML = `
    ${gridBotStatSummary(snapshot)}
    ${gridBotPerformerCards(snapshot)}
    <h3>P&amp;L over time by bot</h3>
    <div class="live-holdings-wrap"><canvas id="liveGridBotsHistoryChart"></canvas></div>
    ${gridBotsHistoryLegendHtml(historyPoints)}
    <h3>Your grid bots (${bots.length})</h3>
    ${gridBotTableHtml(bots)}
    ${otherActivity.length ? `<details class="other-activity"${wasOtherActivityOpen ? " open" : ""}><summary>Other manual trades &amp; leftover orders (${otherActivity.length} markets, not grid bots)</summary>${gridBotTableHtml(otherActivity)}</details>` : ""}
    <p class="muted small">Each row above is a single real NonKYC grid bot, identified by the bot ID NonKYC itself stamps on every order it places (not a market-wide history reconstruction) — so manual trading on a coin never gets counted as one of your bots. Fills, booked profit and unrealized P&amp;L are computed from your full trade history on that bot's market; invested amount is the notional value of its currently-active orders. NonKYC doesn't expose an official grid-bot API, so treat these as close estimates of NonKYC's own numbers (fees aren't included), not an exact mirror. A "~" before the fill count means two bots share that market, so BitsBots couldn't split trade history cleanly and fell back to a rougher order-based count.</p>`;
  drawGridBotsHistoryChart(document.querySelector("#liveGridBotsHistoryChart"), historyPoints);
}

function renderLiveAccountMini(snapshot, errorMessage) {
  const body = document.querySelector("#liveAccountMiniBody");
  if (!body) return;
  if (!liveAccountConnected) {
    body.innerHTML = `<p class="muted">Connect your NonKYC API key in Settings to see your real grid bots here.</p>`;
    return;
  }
  if (errorMessage && !snapshot) {
    body.innerHTML = `<p class="form-error">${escapeHtml(errorMessage)}</p>`;
    return;
  }
  if (!snapshot) {
    body.innerHTML = `<p class="muted">Loading your live grid bots…</p>`;
    return;
  }
  const historyPoints = liveGridBotsHistory || [];
  body.innerHTML = `
    ${gridBotMiniStrip(snapshot)}
    <div class="live-holdings-wrap mini"><canvas id="liveGridBotsMiniChart"></canvas></div>
    ${gridBotsHistoryLegendHtml(historyPoints)}`;
  drawGridBotsHistoryChart(document.querySelector("#liveGridBotsMiniChart"), historyPoints);
}

let liveGridBotsSnapshot = null;
let liveGridBotsHistory = [];

async function refreshLiveAccountPanel() {
  if (!liveAccountConnected) {
    liveGridBotsSnapshot = null;
    liveGridBotsHistory = [];
    realUsdtBalance = null;
    myAccountSnapshot = null;
    renderLiveAccountPanel(null, null);
    renderLiveAccountMini(null, null);
    renderBots();
    renderMyOrdersPanel();
    return;
  }
  // Only show the "Loading…" placeholder the very first time — later
  // refreshes swap the content in place once new data arrives instead of
  // clearing the panel first, so it doesn't visibly flash/blink every 20s.
  if (!liveGridBotsSnapshot) {
    renderLiveAccountPanel(null, null);
    renderLiveAccountMini(null, null);
  }
  refreshRealUsdtBalance();
  try {
    const [snapshotResponse, historyResponse] = await Promise.all([
      fetch("/api/live-account/grid-bots"),
      fetch("/api/live-account/grid-bots/history"),
    ]);
    if (!snapshotResponse.ok) {
      const payload = await snapshotResponse.json().catch(() => ({ detail: "Unable to reach your NonKYC account" }));
      if (!liveGridBotsSnapshot) {
        renderLiveAccountPanel(null, payload.detail);
        renderLiveAccountMini(null, payload.detail);
      }
      return;
    }
    liveGridBotsSnapshot = await snapshotResponse.json();
    if (historyResponse.ok) {
      liveGridBotsHistory = (await historyResponse.json()).history || [];
    }
    renderLiveAccountPanel(liveGridBotsSnapshot, null, liveGridBotsHistory);
    renderLiveAccountMini(liveGridBotsSnapshot, null);
    renderBots();
  } catch (error) {
    if (!liveGridBotsSnapshot) {
      renderLiveAccountPanel(null, "Unable to reach your NonKYC account.");
      renderLiveAccountMini(null, "Unable to reach your NonKYC account.");
    }
  }
}

// Real USDT balance pulled directly from NonKYC — replaces the paper-mode
// "equity"/"cash" figures everywhere they used to be shown, so the user
// only ever sees their actual account balance, never a simulated one.
async function refreshRealUsdtBalance() {
  if (!liveAccountConnected) {
    realUsdtBalance = null;
    myAccountSnapshot = null;
    return;
  }
  try {
    const response = await fetch("/api/live-account/snapshot");
    if (!response.ok) return;
    const payload = await response.json();
    myAccountSnapshot = payload;
    realUsdtBalance = (payload.balances || []).find(item => item.asset === "USDT")
      || { asset: "USDT", available: 0, held: 0, total: 0 };
  } catch (error) {
    // keep the last known value on a transient network error
  }
  if (state) renderWorkspaceViews();
  renderAssistantContext();
  renderMyOrdersPanel();
}

async function connectLiveAccount(accessKey, secretKey) {
  const errorBox = document.querySelector("#liveAccountFormError");
  errorBox.classList.add("hidden");
  const submitButton = document.querySelector("#liveAccountForm button[type=submit]");
  submitButton.disabled = true;
  try {
    const response = await fetch("/api/live-account/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ access_key: accessKey, secret_key: secretKey }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      errorBox.textContent = payload.detail || "Unable to connect this NonKYC API key.";
      errorBox.classList.remove("hidden");
      return;
    }
    document.querySelector("#liveAccountForm").reset();
    await loadLiveAccountStatus();
  } finally {
    submitButton.disabled = false;
  }
}

async function disconnectLiveAccount() {
  await fetch("/api/live-account/credentials", { method: "DELETE" });
  await loadLiveAccountStatus();
}

function showView(viewName) {
  document.querySelectorAll(".app-view").forEach(view => {
    view.classList.toggle("hidden", view.id !== `view-${viewName}`);
  });
  document.querySelectorAll(".nav-links [data-view]").forEach(button => {
    button.classList.toggle("nav-active", button.dataset.view === viewName);
  });
  if (state) renderWorkspaceViews();
  if (viewName === "portfolio" && liveAccountConnected) refreshLiveAccountPanel();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function updateBotStatus(botId, status) {
  const response = await fetch(`/api/bots/${botId}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "Unable to update bot" }));
    document.querySelector("#errors").textContent = payload.detail;
    return;
  }
  await refresh();
}

async function deleteBot(botId) {
  const response = await fetch(`/api/bots/${botId}`, { method: "DELETE" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: "Unable to delete bot" }));
    document.querySelector("#errors").textContent = payload.detail;
    return;
  }
  await refresh();
}

function renderBotMarketOptions(query = "") {
  const existingSymbols = new Set((state.bots || []).map(bot => bot.symbol));
  const normalizedQuery = query.trim().toUpperCase();
  const scoreBySymbol = new Map(state.signals.map(signal => [signal.symbol, signal.score]));
  const available = availableBotMarkets.filter(market =>
    !existingSymbols.has(market.symbol)
    && (!normalizedQuery || market.symbol.includes(normalizedQuery))
  );
  const select = document.querySelector("#botSymbol");
  const previous = select.value;
  select.innerHTML = available.map(market => {
    const score = scoreBySymbol.get(market.symbol);
    const status = score == null
      ? "manual/speculative · deep scan starts after bot creation"
      : `currently deep-scanned · score ${score.toFixed(0)}`;
    return `<option value="${escapeHtml(market.symbol)}">${escapeHtml(market.symbol)} · ${status}</option>`;
  }).join("");
  if (available.some(market => market.symbol === previous)) select.value = previous;
  document.querySelector("#botForm").querySelector("[type=submit]").disabled = !available.length;
  updateMarketSelectionNote();
}

function updateMarketSelectionNote() {
  const symbol = document.querySelector("#botSymbol").value;
  const currentSignal = state.signals.find(signal => signal.symbol === symbol);
  const note = document.querySelector("#marketSelectionNote");
  if (!symbol) {
    note.textContent = "No matching unassigned market is available.";
    note.className = "market-selection-note speculative";
  } else if (currentSignal) {
    note.textContent = `${symbol} is in the current deep scan with score ${currentSignal.score.toFixed(0)}/100.`;
    note.className = "market-selection-note";
  } else {
    note.textContent = `${symbol} is a manual speculative selection outside the current top-20 discovery set. Creating the bot will prioritize it for a deep multi-timeframe scan; it cannot enter until a valid score is available.`;
    note.className = "market-selection-note speculative";
  }
}

function updateGridEstimate() {
  const lower = Number(document.querySelector("#botLowerPrice").value);
  const upper = Number(document.querySelector("#botUpperPrice").value);
  const slices = Number(document.querySelector("#botGridSlices").value);
  const allocation = Number(document.querySelector("#botAllocation").value);
  const estimate = document.querySelector("#gridEstimate");
  if (!(lower > 0 && upper > lower && slices >= 3)) {
    estimate.textContent = "Enter a valid range and slice count to preview the grid.";
    estimate.className = "grid-estimate caution";
    return;
  }
  const spacing = (upper - lower) / slices;
  const symbol = document.querySelector("#botSymbol").value;
  const marketPrice = availableBotMarkets.find(item => item.symbol === symbol)?.last_price;
  const referencePrice = marketPrice || (upper + lower) / 2;
  const roundTripCost = 2 * ((state?.settings?.fee_rate || 0.002) + (state?.settings?.slippage_rate || 0.001)) * 100;
  const grossPercent = spacing / referencePrice * 100;
  const netPercent = grossPercent - roundTripCost;
  estimate.textContent = `${price(spacing)} spacing · about ${grossPercent.toFixed(2)}% gross per level · ${netPercent.toFixed(2)}% after the modeled ${roundTripCost.toFixed(2)}% round-trip fees and slippage · about ${(allocation / slices).toFixed(2)}% allocation per slice.`;
  estimate.className = `grid-estimate ${netPercent <= 0 ? "caution" : ""}`;
}

function fillGridDefaults() {
  const symbol = document.querySelector("#botSymbol").value;
  const market = availableBotMarkets.find(item => item.symbol === symbol);
  if (!market || selectedBotStrategy === "Multi-TF") return;
  const signal = state.signals.find(item => item.symbol === symbol);
  const atr = signal?.atr || market.last_price * 0.01;
  const halfWidth = Math.min(market.last_price * 0.20, Math.max(atr * 4, market.last_price * 0.04));
  const slices = selectedBotStrategy === "Adaptive Grid" ? 10 : 8;
  const lower = Math.max(market.last_price - halfWidth, market.last_price * 0.01);
  const upper = market.last_price + halfWidth;
  const spacing = (upper - lower) / slices;
  const stop = Math.max(lower - Math.max(atr * 1.5, spacing * 1.5), market.last_price * 0.001);
  document.querySelector("#botLowerPrice").value = Number(lower.toPrecision(8));
  document.querySelector("#botUpperPrice").value = Number(upper.toPrecision(8));
  document.querySelector("#botGridSlices").value = slices;
  document.querySelector("#botGridStop").value = Number(stop.toPrecision(8));
  updateGridEstimate();
}

function selectBotStrategy(strategy, generateRecommendation = true) {
  selectedBotStrategy = strategy;
  document.querySelector("#botStrategy").value = strategy;
  document.querySelectorAll(".strategy-choice").forEach(button => {
    button.classList.toggle("selected", button.dataset.strategy === strategy);
  });
  const isGrid = strategy !== "Multi-TF";
  document.querySelector("#gridSettings").classList.toggle("hidden", !isGrid);
  document.querySelector("#momentumEntrySetting").classList.toggle("hidden", isGrid);
  ["#botLowerPrice", "#botUpperPrice", "#botGridSlices", "#botGridStop"].forEach(selector => {
    document.querySelector(selector).required = isGrid;
  });
  document.querySelector("#automaticSetupTitle").textContent = {
    "Multi-TF": "Recommended Momentum setup",
    "Grid": "Recommended fixed-range Grid setup",
    "Adaptive Grid": "Recommended Adaptive Grid setup",
  }[strategy];
  document.querySelector("#automaticSetupDescription").textContent = {
    "Multi-TF": "BitsBots chooses the strongest unassigned directional setup and applies conservative paper-trading risk controls.",
    "Grid": "BitsBots chooses a suitable market, calculates a fixed range from price and volatility, and divides it into fee-aware slices.",
    "Adaptive Grid": "BitsBots creates a volatility-based grid that may recenter only when price leaves the range and no slices are open.",
  }[strategy];
  document.querySelector("#allocationHelp").textContent = isGrid
    ? "The total paper equity available to this grid; it is divided across the slices."
    : "The most paper equity this bot may place in one position.";
  document.querySelector("#botModeNote").textContent = isGrid
    ? "Paper mode only. Buys occur after downward grid-level crossings; filled slices sell one level higher or at the safety stop."
    : "Paper mode only. The bot may open simulated positions only when its score reaches the entry threshold.";
  document.querySelector("#customCreateBot").textContent = `Create ${strategy === "Multi-TF" ? "Momentum" : strategy} Bot`;
  if (isGrid) fillGridDefaults();
  if (generateRecommendation && document.querySelector("#botDialog").open) {
    autoGenerateBot();
  }
}

async function openBotDialog() {
  const dialog = document.querySelector("#botDialog");
  if (!dialog.open) dialog.showModal();
  selectBotStrategy("Multi-TF", false);
  document.querySelector("#botFormError").textContent = "";
  document.querySelector("#botRecommendation").textContent = "Loading the complete NonKYC USDT/USDC market catalog...";
  document.querySelector("#createRecommendedBot").disabled = true;
  const response = await fetch("/api/markets");
  const payload = await response.json().catch(() => ({ detail: "Unable to load markets" }));
  if (!response.ok) {
    document.querySelector("#botFormError").textContent = payload.detail;
    return;
  }
  availableBotMarkets = payload.markets;
  document.querySelector("#botMarketSearch").value = "";
  renderBotMarketOptions();
  const existingSymbols = new Set((state.bots || []).map(bot => bot.symbol));
  const availableCount = availableBotMarkets.filter(
    market => !existingSymbols.has(market.symbol)
  ).length;
  document.querySelector("#botFormError").textContent = availableCount
    ? ""
    : "Every active USDT/USDC market already has a bot.";
  document.querySelector("#customBotFields").classList.add("hidden");
  document.querySelector("#showCustomBot").textContent = "Customize market or settings";
  currentBotRecommendation = null;
  if (state?.signals.length) {
    await autoGenerateBot();
  } else {
    document.querySelector("#botRecommendation").textContent =
      "The deep scanner has not finished, so automatic recommendation is unavailable. You can still choose a market manually.";
  }
}

async function autoGenerateBot() {
  if (!document.querySelector("#botDialog").open) {
    openBotDialog();
    return;
  }
  if (!document.querySelector("#botDialog").open) return;
  const note = document.querySelector("#botRecommendation");
  note.textContent = "Evaluating current multi-timeframe candidates...";
  document.querySelector("#createRecommendedBot").disabled = true;
  const strategy = selectedBotStrategy;
  const generation = ++botRecommendationGeneration;
  const response = await fetch(`/api/bots/recommendation?strategy=${encodeURIComponent(strategy)}`);
  const payload = await response.json().catch(() => ({ detail: "Unable to generate a candidate" }));
  if (generation !== botRecommendationGeneration || strategy !== selectedBotStrategy) return;
  if (!response.ok) {
    currentBotRecommendation = null;
    note.textContent = payload.detail;
    return;
  }
  const recommendation = payload.recommendation;
  currentBotRecommendation = recommendation;
  document.querySelector("#botName").value = recommendation.name;
  document.querySelector("#botSymbol").value = recommendation.symbol;
  document.querySelector("#botStrategy").value = recommendation.strategy;
  document.querySelector("#botRisk").value = recommendation.risk_level;
  document.querySelector("#botEntryScore").value = recommendation.entry_score;
  document.querySelector("#botAllocation").value = recommendation.max_allocation * 100;
  document.querySelector("#allocationLabel").textContent = `${Math.round(recommendation.max_allocation * 100)}%`;
  if (recommendation.strategy !== "Multi-TF") {
    document.querySelector("#botLowerPrice").value = Number(recommendation.lower_price.toPrecision(8));
    document.querySelector("#botUpperPrice").value = Number(recommendation.upper_price.toPrecision(8));
    document.querySelector("#botGridSlices").value = recommendation.grid_slices;
    document.querySelector("#botGridStop").value = Number(recommendation.grid_stop_price.toPrecision(8));
    updateGridEstimate();
  }
  updateMarketSelectionNote();
  note.textContent = recommendation.strategy === "Multi-TF"
    ? `Recommended market: ${recommendation.symbol} · current score ${recommendation.current_score.toFixed(0)}/100 · entry score ${recommendation.entry_score} · ${recommendation.risk_level} risk · ${Math.round(recommendation.max_allocation * 100)}% maximum allocation. Nothing starts until you click create.`
    : `Recommended market: ${recommendation.symbol} · range ${price(recommendation.lower_price)} to ${price(recommendation.upper_price)} · ${recommendation.grid_slices} slices · stop ${price(recommendation.grid_stop_price)} · ${recommendation.risk_level} risk · ${Math.round(recommendation.max_allocation * 100)}% total allocation. Nothing starts until you click create.`;
  document.querySelector("#createRecommendedBot").disabled = false;
}

async function submitBot(payload) {
  const response = await fetch("/api/bots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const result = await response.json().catch(() => ({ detail: "Unable to create bot" }));
    const detail = Array.isArray(result.detail)
      ? result.detail.map(item => item.msg || "Invalid setting").join(". ")
      : result.detail;
    document.querySelector("#botFormError").textContent = detail;
    return false;
  }
  document.querySelector("#botDialog").close();
  document.querySelector("#botForm").reset();
  document.querySelector("#allocationLabel").textContent = "10%";
  currentBotRecommendation = null;
  await refresh();
  showView("bots");
  return true;
}

async function createRecommendedBot() {
  if (!currentBotRecommendation) await autoGenerateBot();
  if (!currentBotRecommendation) return;
  const button = document.querySelector("#createRecommendedBot");
  button.disabled = true;
  button.textContent = "Creating recommended paper bot...";
  await submitBot({
    name: currentBotRecommendation.name,
    symbol: currentBotRecommendation.symbol,
    strategy: currentBotRecommendation.strategy,
    risk_level: currentBotRecommendation.risk_level,
    entry_score: currentBotRecommendation.entry_score,
    max_allocation: currentBotRecommendation.max_allocation,
    lower_price: currentBotRecommendation.lower_price,
    upper_price: currentBotRecommendation.upper_price,
    grid_slices: currentBotRecommendation.grid_slices,
    grid_stop_price: currentBotRecommendation.grid_stop_price,
  });
  button.disabled = false;
  button.textContent = "Create recommended paper bot";
}

function renderAssistantContext() {
  if (!state) return;
  const top = state.signals[0];
  const selected = state.signals.find(signal => signal.symbol === selectedSymbol) || top;
  document.querySelector("#assistantContext").innerHTML = top ? `
    <div class="context-signal"><span>TOP CURRENT SETUP</span><strong>${escapeHtml(top.symbol)} · ${top.score.toFixed(0)}/100</strong></div>
    <div class="context-signal"><span>SELECTED MARKET</span><strong>${escapeHtml(selected.symbol)} · ${selected.score.toFixed(0)}/100</strong></div>
    <div class="context-signal"><span>USDT BALANCE (NONKYC)</span><strong>${realUsdtBalance ? money(realUsdtBalance.total) : "Not connected"}</strong></div>
    <div class="context-signal"><span>LIVE BOTS RUNNING</span><strong>${liveGridBotsSnapshot ? liveGridBotsSnapshot.bots_running : "—"}</strong></div>
    <div class="context-signal"><span>DATA STATUS</span><strong>${escapeHtml(state.status)}</strong></div>
  ` : `<p class="muted">Waiting for the scanner to produce current context.</p>`;
}

function appendChatMessage(text, role) {
  const message = document.createElement("div");
  message.className = `chat-message ${role === "user" ? "user-message" : "assistant-message"}`;
  message.textContent = text;
  const messages = document.querySelector("#assistantMessages");
  messages.appendChild(message);
  messages.scrollTop = messages.scrollHeight;
  return message;
}

async function sendAssistantMessage(message) {
  const trimmed = message.trim();
  if (!trimmed) return;
  appendChatMessage(trimmed, "user");
  const pending = appendChatMessage("Reviewing the latest scanner and paper-account context...", "assistant");
  const response = await fetch("/api/assistant/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: trimmed }),
  });
  const payload = await response.json().catch(() => ({ detail: "The assistant could not respond" }));
  if (!response.ok) {
    pending.textContent = payload.detail;
    return;
  }
  pending.textContent = `${payload.answer}\n\n${payload.disclaimer}`;
  if (payload.context_symbol) {
    pending.dataset.symbol = payload.context_symbol;
    pending.title = `Click to focus ${payload.context_symbol}`;
    pending.addEventListener("click", () => {
      showView("dashboard");
      selectMarket(payload.context_symbol);
    });
  }
}

async function runScan() {
  await fetch("/api/scan", { method: "POST" });
  setTimeout(refresh, 500);
}
async function enableAlerts() {
  await Notification.requestPermission();
}

document.querySelector("#scan").addEventListener("click", runScan);
document.querySelector("#quickScan").addEventListener("click", runScan);
document.querySelector("#notify").addEventListener("click", enableAlerts);
document.querySelector("#quickAlerts").addEventListener("click", enableAlerts);
document.querySelector("#newBotRail")?.addEventListener("click", openBotDialog);
document.querySelector("#quickNewBot").addEventListener("click", openBotDialog);
document.querySelectorAll(".new-bot-action").forEach(button => button.addEventListener("click", openBotDialog));
document.querySelectorAll(".auto-bot-action").forEach(button => button.addEventListener("click", openBotDialog));
document.querySelectorAll(".strategy-choice").forEach(button => button.addEventListener("click", () => {
  selectBotStrategy(button.dataset.strategy);
}));
document.querySelector("#autoGenerateBot").addEventListener("click", autoGenerateBot);
document.querySelector("#createRecommendedBot").addEventListener("click", createRecommendedBot);
document.querySelector("#showCustomBot").addEventListener("click", event => {
  const fields = document.querySelector("#customBotFields");
  fields.classList.toggle("hidden");
  event.currentTarget.textContent = fields.classList.contains("hidden")
    ? "Customize market or settings"
    : "Hide custom settings";
});
document.querySelector("#assignStrategy").addEventListener("click", () => {
  showView("bots");
  openBotDialog();
});
document.querySelectorAll(".nav-links [data-view]").forEach(button => button.addEventListener("click", () => {
  showView(button.dataset.view);
}));
document.querySelector("#profileName").addEventListener("click", () => showView("settings"));
document.querySelector("#logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  window.location.assign("/login");
});
document.querySelector("#marketSelect").addEventListener("change", event => selectMarket(event.target.value));
document.querySelector("#marketSearch").addEventListener("input", event => {
  renderMarketOptions(event.target.value);
});
document.querySelector("#marketSearch").addEventListener("keydown", async event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  const query = event.currentTarget.value.trim().toUpperCase();
  if (!query) return;
  const markets = availableChartMarkets.length
    ? availableChartMarkets
    : await loadChartMarkets();
  const exactMatch = markets.find(market => market.symbol.toUpperCase() === query);
  const firstMatch = exactMatch || markets.find(
    market => market.symbol.toUpperCase().includes(query)
  );
  if (firstMatch) {
    await selectMarket(firstMatch.symbol);
    event.currentTarget.value = firstMatch.symbol;
    renderMarketOptions(firstMatch.symbol);
  }
});
document.querySelectorAll(".indicator-controls button").forEach(button => button.addEventListener("click", event => {
  const indicator = event.currentTarget.dataset.indicator;
  indicators[indicator] = !indicators[indicator];
  event.currentTarget.classList.toggle("active", indicators[indicator]);
  const panel = document.querySelector(`[data-panel="${indicator}"]`);
  if (panel) panel.classList.toggle("hidden", !indicators[indicator]);
  drawMarketChart();
}));
const marketChart = document.querySelector("#marketChart");

function setChartWindow(start, count) {
  const boundedCount = Math.max(10, Math.min(activeCandles.length, count));
  const boundedStart = Math.max(0, Math.min(
    activeCandles.length - boundedCount,
    Math.round(start)
  ));
  chartView = { start: boundedStart, end: boundedStart + boundedCount };
}

marketChart.addEventListener("wheel", event => {
  if (activeCandles.length < 2) return;
  event.preventDefault();
  const rect = marketChart.getBoundingClientRect();
  const plotWidth = Math.max(1, rect.width - 66);
  const pointerRatio = Math.max(0, Math.min(1, (event.clientX - rect.left) / plotWidth));
  const currentCount = chartView.end - chartView.start;
  const factor = event.deltaY < 0 ? 0.8 : 1.25;
  const nextCount = Math.round(Math.max(10, Math.min(activeCandles.length, currentCount * factor)));
  const anchor = chartView.start + pointerRatio * currentCount;
  setChartWindow(anchor - pointerRatio * nextCount, nextCount);
  hoverIndex = null;
  drawMarketChart();
}, { passive: false });

marketChart.addEventListener("pointerdown", event => {
  if (event.button !== 0 || !activeCandles.length) return;
  if (chartView.end - chartView.start >= activeCandles.length && activeCandles.length > 12) {
    const rect = marketChart.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width - 66)));
    const count = Math.max(10, Math.round(activeCandles.length * 0.8));
    setChartWindow(ratio * activeCandles.length - ratio * count, count);
    drawMarketChart();
  }
  marketChart.setPointerCapture(event.pointerId);
  chartDrag = {
    pointerId: event.pointerId,
    x: event.clientX,
    start: chartView.start,
    count: chartView.end - chartView.start,
  };
  marketChart.classList.add("dragging");
});

marketChart.addEventListener("pointermove", event => {
  if (!activeCandles.length) return;
  const rect = marketChart.getBoundingClientRect();
  const plotWidth = Math.max(1, rect.width - 66);
  if (chartDrag) {
    const candleShift = Math.round((chartDrag.x - event.clientX) / plotWidth * chartDrag.count);
    setChartWindow(chartDrag.start + candleShift, chartDrag.count);
    hoverIndex = null;
    document.querySelector("#chartTooltip").hidden = true;
    drawMarketChart();
    return;
  }
  const visibleCount = chartView.end - chartView.start;
  hoverIndex = Math.max(0, Math.min(
    visibleCount - 1,
    Math.floor((event.clientX - rect.left) / plotWidth * visibleCount)
  ));
  const candle = activeCandles[chartView.start + hoverIndex];
  const tooltip = document.querySelector("#chartTooltip");
  tooltip.hidden = false;
  tooltip.textContent = `${new Date(candle.timestamp).toLocaleString()}  O ${price(candle.open)}  H ${price(candle.high)}  L ${price(candle.low)}  C ${price(candle.close)}`;
  tooltip.style.left = `${Math.min(event.clientX - rect.left + 12, rect.width - tooltip.offsetWidth - 8)}px`;
  tooltip.style.top = `${Math.max(6, event.clientY - rect.top - 34)}px`;
  drawMarketChart();
});
marketChart.addEventListener("pointerup", event => {
  if (chartDrag?.pointerId === event.pointerId) {
    marketChart.releasePointerCapture(event.pointerId);
    chartDrag = null;
    marketChart.classList.remove("dragging");
  }
});
marketChart.addEventListener("pointercancel", () => {
  chartDrag = null;
  marketChart.classList.remove("dragging");
});
marketChart.addEventListener("mouseleave", () => {
  if (chartDrag) return;
  hoverIndex = null;
  document.querySelector("#chartTooltip").hidden = true;
  drawMarketChart();
});
marketChart.addEventListener("dblclick", () => {
  chartView = { start: 0, end: activeCandles.length };
  hoverIndex = null;
  drawMarketChart();
});
document.querySelectorAll(".time-buttons button").forEach(button => button.addEventListener("click", event => {
  document.querySelectorAll(".time-buttons button").forEach(item => item.classList.remove("selected"));
  event.target.classList.add("selected"); selectedPeriod = Number(event.target.dataset.period);
  selectMarket(selectedSymbol);
}));
window.addEventListener("resize", () => {
  if (activeCandles.length) drawMarketChart();
  if (state) drawEquityChart();
});
document.querySelector("#closeBotDialog").addEventListener("click", () => {
  document.querySelector("#botDialog").close();
});
document.querySelector("#botAllocation").addEventListener("input", event => {
  document.querySelector("#allocationLabel").textContent = `${event.target.value}%`;
  updateGridEstimate();
});
["#botLowerPrice", "#botUpperPrice", "#botGridSlices", "#botGridStop"].forEach(selector => {
  document.querySelector(selector).addEventListener("input", updateGridEstimate);
});
document.querySelector("#botMarketSearch").addEventListener("input", event => {
  renderBotMarketOptions(event.target.value);
});
document.querySelector("#botSymbol").addEventListener("change", () => {
  updateMarketSelectionNote();
  fillGridDefaults();
});
document.querySelector("#botForm").addEventListener("submit", async event => {
  event.preventDefault();
  const payload = {
    name: document.querySelector("#botName").value,
    symbol: document.querySelector("#botSymbol").value,
    strategy: document.querySelector("#botStrategy").value,
    risk_level: document.querySelector("#botRisk").value,
    entry_score: Number(document.querySelector("#botEntryScore").value),
    max_allocation: Number(document.querySelector("#botAllocation").value) / 100,
  };
  if (payload.strategy !== "Multi-TF") {
    payload.lower_price = Number(document.querySelector("#botLowerPrice").value);
    payload.upper_price = Number(document.querySelector("#botUpperPrice").value);
    payload.grid_slices = Number(document.querySelector("#botGridSlices").value);
    payload.grid_stop_price = Number(document.querySelector("#botGridStop").value);
  }
  await submitBot(payload);
});
document.querySelector("#assistantForm").addEventListener("submit", async event => {
  event.preventDefault();
  const input = document.querySelector("#assistantInput");
  const message = input.value;
  input.value = "";
  await sendAssistantMessage(message);
});
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
  showView("assistant");
  sendAssistantMessage(button.dataset.prompt);
}));
document.querySelector("#liveAccountForm").addEventListener("submit", async event => {
  event.preventDefault();
  const accessKey = document.querySelector("#liveAccessKey").value.trim();
  const secretKey = document.querySelector("#liveSecretKey").value.trim();
  await connectLiveAccount(accessKey, secretKey);
});
document.querySelector("#disconnectLiveAccount").addEventListener("click", disconnectLiveAccount);
document.querySelector("#refreshLiveAccount").addEventListener("click", refreshLiveAccountPanel);
document.querySelector("#viewLiveAccountDetails").addEventListener("click", () => showView("portfolio"));

installHelpTips();
loadProfile();
loadLiveAccountStatus();
loadChartMarkets();
refresh();
setInterval(refresh, 5000);
setInterval(loadChartMarkets, 300000);
setInterval(() => { if (liveAccountConnected) refreshLiveAccountPanel(); }, 20000);
