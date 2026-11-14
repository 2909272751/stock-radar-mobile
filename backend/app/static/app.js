(() => {
  "use strict";

  const state = {
    token: localStorage.getItem("stockRadarToken") || "",
    user: "",
    system: null,
    metadata: null,
    monitors: [],
    alerts: [],
    alertPage: 1,
    alertTotalPages: 1,
    notification: null,
    pollInterval: 900,
    activeChannel: "wecom",
    draftRules: [],
    editingMonitorId: null,
    editingRuleIndex: null,
    stream: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
  const fmtPrice = (value) => value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toFixed(2);
  const fmtPct = (value) => {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const n = Number(value);
    return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
  };
  const toneClass = (value) => Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "flat";
  const fmtDateTime = (value) => {
    if (!value) return "—";
    const d = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };
  const fmtRelative = (value) => {
    if (!value) return "";
    const d = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(d.getTime())) return value;
    const seconds = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  };
  const intervalText = (seconds) => {
    const n = Number(seconds) || 900;
    if (n % 3600 === 0) return `${n / 3600} 小时`;
    return `${Math.round(n / 60)} 分钟`;
  };
  const fieldLabel = (value) => state.metadata?.fields?.find((item) => item.value === value)?.label || value;
  const operatorLabel = (value) => state.metadata?.operators?.find((item) => item.value === value)?.label || value;
  const periodLabel = (value) => state.metadata?.periods?.find((item) => item.value === value)?.label || value;

  let toastTimer;
  const toast = (message, isError = false) => {
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show${isError ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "toast"; }, 3300);
  };

  const api = async (path, options = {}) => {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (state.token) headers["X-Auth-Token"] = state.token;
    const response = await fetch(path, { ...options, headers });
    if (response.status === 401) {
      clearSession();
      showLogin();
      throw new Error("登录已过期，请重新登录");
    }
    if (!response.ok) {
      let detail = `请求失败 (${response.status})`;
      try {
        const body = await response.json();
        detail = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join("；") : detail;
      } catch (_) { /* ignore malformed error body */ }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return response.json();
  };

  const setBusy = (node, busy, label = "处理中…") => {
    if (!node) return;
    if (busy) {
      node.dataset.originalText = node.textContent;
      node.textContent = label;
      node.disabled = true;
    } else {
      node.textContent = node.dataset.originalText || node.textContent;
      node.disabled = false;
    }
  };

  const clearSession = () => {
    state.token = "";
    state.user = "";
    localStorage.removeItem("stockRadarToken");
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
  };

  const showLogin = () => {
    $("#app-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
    $("#logout-btn").classList.add("hidden");
    $("#change-password-btn").classList.add("hidden");
    $("#market-pill").textContent = "未登录";
    $("#market-pill").className = "pill pill-neutral";
  };

  const showApp = () => {
    $("#login-view").classList.add("hidden");
    $("#app-view").classList.remove("hidden");
    $("#logout-btn").classList.remove("hidden");
    $("#change-password-btn").classList.remove("hidden");
  };

  const switchAuthTab = (tab) => {
    $$("[data-auth-tab]").forEach((node) => {
      const active = node.dataset.authTab === tab;
      node.classList.toggle("active", active);
      if (node.getAttribute("role") === "tab") node.setAttribute("aria-selected", String(active));
    });
    $("#login-form").classList.toggle("hidden", tab !== "login");
    $("#register-form").classList.toggle("hidden", tab !== "register");
    if (tab === "register") $("#register-error").classList.add("hidden");
    const first = tab === "login" ? $("#login-username") : $("#register-username");
    setTimeout(() => first?.focus(), 40);
  };

  const login = async (form, path, body, onError) => {
    const button = $("button[type=submit]", form);
    setBusy(button, true, "请稍候…");
    try {
      const result = await api(path, { method: "POST", body: JSON.stringify(body) });
      state.token = result.token;
      state.user = result.username;
      localStorage.setItem("stockRadarToken", result.token);
      form.reset();
      await enterApp();
    } catch (error) {
      if (onError) onError(error);
      else toast(error.message, true);
    } finally {
      setBusy(button, false);
    }
  };

  const enterApp = async () => {
    showApp();
    $("#loading-cover").classList.remove("hidden");
    try {
      const [system, metadata, monitors, notification] = await Promise.all([
        api("/api/system"),
        api("/api/metadata"),
        api("/api/monitors"),
        api("/api/settings/notification"),
      ]);
      state.system = system;
      state.metadata = metadata;
      state.monitors = monitors;
      state.notification = notification;
      state.pollInterval = system.poll_interval_seconds;
      populateRuleSelects();
      renderAll();
      await loadAlerts(1, true);
      connectStream();
    } catch (error) {
      toast(error.message, true);
    } finally {
      $("#loading-cover").classList.add("hidden");
    }
  };

  const renderAll = () => {
    renderSystem();
    renderWatchlist();
    renderNotification();
    renderOnboarding();
    renderIntervalPicker();
  };

  const renderSystem = () => {
    const system = state.system;
    if (!system) return;
    const isOpen = !!system.market_open;
    const dataStatus = system.data_status || "unknown";
    const hasDataError = dataStatus === "error" || dataStatus === "partial";
    const marketPill = $("#market-pill");
    marketPill.textContent = system.market_status;
    marketPill.className = `pill ${hasDataError ? "pill-danger" : isOpen ? "pill-success" : "pill-neutral"}`;
    $(".status-strip").classList.toggle("has-error", hasDataError);
    $("#status-dot").className = `status-dot ${hasDataError ? "error" : isOpen ? "open" : ""}`;
    $("#status-text").textContent = hasDataError
      ? (dataStatus === "error" ? "行情数据不可用" : "部分行情缺失")
      : isOpen ? "交易时段，正在低频检查" : "非交易时段，等待开盘";
    $("#status-note").textContent = hasDataError
      ? (system.data_message || "行情源请求失败，请检查网络后重试")
      : isOpen
        ? "只在你设置的节奏里获取行情，不连续轮询"
        : `${system.market_status}，下一次检查将自动跳过`;
    $("#next-check").textContent = system.next_cycle_at ? fmtDateTime(system.next_cycle_at) : (isOpen ? "即将开始" : "开盘后");
    $("#interval-label").textContent = intervalText(system.poll_interval_seconds);
    $("#monitor-count").textContent = String(system.monitor_count);
    $("#last-check").textContent = system.last_cycle_at ? `上次检查 ${fmtRelative(system.last_cycle_at)}` : "尚未检查";
    $("#custom-interval").value = Math.round(system.poll_interval_seconds / 60);
  };

  const renderIntervalPicker = () => {
    $$("[data-interval]").forEach((button) => {
      button.classList.toggle("active", Number(button.dataset.interval) === Number(state.pollInterval));
    });
  };

  const renderNotification = () => {
    const notification = state.notification;
    if (!notification) return;
    const configured = !!notification.configured;
    const label = { wecom: "企业微信", bark: "Bark", webhook: "Webhook", none: "仅面板" }[notification.channel] || "未设置";
    const badge = $("#notification-state");
    const channelDisabled = notification.channel === "none";
    badge.textContent = channelDisabled ? "仅面板" : configured ? label : "未设置";
    badge.className = `pill ${channelDisabled ? "pill-neutral" : configured ? "pill-success" : "pill-warn"}`;
    $("#notification-copy").textContent = channelDisabled
      ? "当前仅写入面板，不发送外部通知。需要手机提醒时请选择企业微信、Bark 或 Webhook。"
      : configured
      ? `条件命中后会发到${label}；发送失败会自动重试。`
      : "目前提醒只会留在面板里。选择一种方式，条件命中时才会主动通知你。";
    $("#quick-test-notification").disabled = channelDisabled || !configured;
  };

  const renderNotificationError = () => {
    const node = $("#notification-last-error");
    const failed = state.alerts.find((alert) => alert.status === "failed" && alert.error);
    node.classList.toggle("hidden", !failed);
    node.textContent = failed ? `最近一次通知失败：${failed.error}` : "";
  };

  const renderOnboarding = () => {
    const hasMonitor = state.monitors.length > 0;
    $("#onboarding").classList.toggle("hidden", hasMonitor);
    if (!hasMonitor) {
      $("#onboarding-action").textContent = "开始添加股票";
      $("#onboarding-action").dataset.action = "add";
    }
  };

  const ruleSummary = (rule) => {
    const period = (rule.field.startsWith("macd_") || rule.field.startsWith("ma_")) ? `${periodLabel(rule.period)} ` : "";
    if (rule.field === "macd_cross_up") return `${period}MACD 金叉`;
    if (rule.field === "macd_cross_down") return `${period}MACD 死叉`;
    if (rule.field === "ma_cross_up") return `${period}MA5×MA20 金叉`;
    if (rule.field === "ma_cross_down") return `${period}MA5×MA20 死叉`;
    if (rule.field === "ma_price") return `${period}MA${rule.ma_period} ${operatorLabel(rule.operator)} ${Number(rule.threshold).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
    return `${period}${fieldLabel(rule.field)} ${operatorLabel(rule.operator)} ${Number(rule.threshold).toLocaleString("zh-CN", { maximumFractionDigits: 4 })}`;
  };

  const preflightMonitorCode = async (showErrors = false) => {
    const input = $("#monitor-code");
    const code = input.value.trim();
    const hint = $("#code-hint");
    if (!code) return null;
    input.classList.remove("input-invalid");
    hint.className = "field-hint";
    hint.textContent = "正在查询股票信息…";
    try {
      const result = await api("/api/monitors/preflight", { method: "POST", body: JSON.stringify({ code }) });
      if (result.instrument_type && result.instrument_type !== "stock") {
        toast("当前面板仅支持添加 A 股股票", true);
        return null;
      }
      if (result.code) input.value = result.code;
      if (!$("#monitor-name").value.trim()) $("#monitor-name").value = result.name;
      input.classList.add("input-valid");
      hint.className = "field-hint hint-valid";
      hint.textContent = `${result.name} · 最新价 ${Number(result.last_price).toFixed(2)} · ${Number(result.change_pct) >= 0 ? "+" : ""}${Number(result.change_pct).toFixed(2)}%`;
      return result;
    } catch (error) {
      input.classList.add("input-invalid");
      hint.className = "field-hint hint-error";
      hint.textContent = error.message;
      if (showErrors) toast(error.message, true);
      return null;
    }
  };

  let searchTimer = null;
  let searchSeq = 0;
  const hideInstrumentResults = () => {
    $("#instrument-results").classList.add("hidden");
    $("#instrument-results").innerHTML = "";
  };
  const showInstrumentHint = (message, isError = false) => {
    const hint = $("#code-hint");
    hint.className = `field-hint${isError ? " hint-error" : ""}`;
    hint.textContent = message;
  };
  const selectInstrument = async (code, name) => {
    $("#monitor-code").value = code;
    if (name && !$("#monitor-name").value.trim()) $("#monitor-name").value = name;
    hideInstrumentResults();
    await preflightMonitorCode(true);
  };
  const searchInstruments = async () => {
    const keyword = $("#monitor-code").value.trim();
    const results = $("#instrument-results");
    if (keyword.length < 1 || /^\d{6}(\.(SH|SZ|BJ))?$/i.test(keyword)) {
      hideInstrumentResults();
      return;
    }
    const seq = ++searchSeq;
    try {
      const items = await api(`/api/instruments/search?keyword=${encodeURIComponent(keyword)}`);
      if (seq !== searchSeq) return;
      if (!items.length) {
        hideInstrumentResults();
        showInstrumentHint(`没有找到“${keyword}”，可以改用 6 位代码，例如 600000`, true);
        return;
      }
      results.innerHTML = items.map((item) => `
        <button type="button" class="instrument-result" role="option" data-result-code="${escapeHtml(item.code)}" data-result-name="${escapeHtml(item.name)}">
          <b>${escapeHtml(item.name)}</b><span>${escapeHtml(item.code)}</span><small>${item.instrument_type === "index" ? "指数" : "A股"}</small>
        </button>`).join("");
      results.classList.remove("hidden");
    } catch (error) {
      if (seq !== searchSeq) return;
      hideInstrumentResults();
      showInstrumentHint(`搜索暂时不可用：${error.message}；可直接输入 6 位代码`, true);
    }
  };

  const renderWatchlist = () => {
    const watchlist = $("#watchlist");
    const empty = $("#watchlist-empty");
    empty.classList.toggle("hidden", state.monitors.length > 0);
    watchlist.classList.toggle("hidden", state.monitors.length === 0);
    if (!state.monitors.length) {
      watchlist.innerHTML = "";
      $("#monitor-count").textContent = "0";
      return;
    }
    watchlist.innerHTML = state.monitors.map((monitor) => {
      const quote = monitor.latest_quote;
      const change = quote ? quote.change_pct : null;
      const indicators = Object.values(monitor.indicators || {});
      const indicator = indicators[0];
      const macdText = indicator?.histogram != null
        ? `${indicator.crossed_up ? "金叉" : indicator.crossed_down ? "死叉" : "MACD"} ${Number(indicator.histogram).toFixed(3)}`
        : "等待行情";
      const rulePills = monitor.rules.length
        ? monitor.rules.map((rule) => `<span class="rule-pill ${rule.enabled ? "" : "disabled"}"><i></i>${escapeHtml(ruleSummary(rule))}</span>`).join("")
        : '<span class="muted">还没有提醒条件</span>';
      return `
        <div class="watch-row" data-monitor-id="${monitor.id}">
          <div class="stock-id">
            <span class="stock-symbol">${monitor.instrument_type === "index" ? "指数" : "A股"}</span>
            <span class="stock-text"><b>${escapeHtml(monitor.name || quote?.name || monitor.code)}</b><small>${escapeHtml(monitor.code)}</small></span>
          </div>
          <div class="price-cell"><b>${quote ? fmtPrice(quote.last_price) : "—"}</b><small>${quote ? escapeHtml(quote.source === "sina" ? "新浪行情" : "东方财富") : "暂无行情"}</small></div>
          <div class="change-cell"><b class="${toneClass(change)}">${fmtPct(change)}</b><small>${quote ? `成交 ${Number(quote.turnover / 100000000).toFixed(2)} 亿` : "等待检查"}</small></div>
          <div class="signal-cell"><b class="${indicator?.crossed_up ? "up" : indicator?.crossed_down ? "down" : "muted"}">${escapeHtml(macdText)}</b><small>${monitor.rules.filter((rule) => rule.enabled).length} 条条件</small></div>
          <button class="row-toggle" type="button" data-toggle-monitor="${monitor.id}" aria-label="展开规则" aria-expanded="false">⌄</button>
          <div class="watch-detail hidden" data-detail="${monitor.id}">
            <div class="rule-pill-row">${rulePills}</div>
            <div class="detail-actions">
              <button type="button" data-edit-monitor="${monitor.id}">编辑</button>
              <button type="button" data-add-rule="${monitor.id}">添加条件</button>
              <button type="button" data-delete-monitor="${monitor.id}">删除</button>
              ${monitor.note ? `<span class="muted">${escapeHtml(monitor.note)}</span>` : ""}
            </div>
          </div>
        </div>`;
    }).join("");
    $("#monitor-count").textContent = String(state.monitors.length);
  };

  const renderAlerts = (append = false) => {
    const list = $("#alert-list");
    $("#alert-empty").classList.toggle("hidden", state.alerts.length > 0);
    const rows = state.alerts.map((alert) => `
      <div class="alert-row">
        <span class="alert-mark">◉</span>
        <div class="alert-text">
          <b>${escapeHtml(alert.message.split("\n")[0])}</b>
          <p>${escapeHtml(alert.rule_summary || alert.message.split("\n")[1] || "")}</p>
          ${alert.status === "failed" && alert.error ? `<p class="alert-error">${escapeHtml(alert.error)}</p>` : ""}
        </div>
        <div class="alert-side">
          <span class="pill ${alert.status === "sent" ? "pill-success" : alert.status === "failed" ? "pill-danger" : "pill-warn"}">${alert.status === "sent" ? "已通知" : alert.status === "failed" ? "发送失败" : alert.status === "retrying" ? "重试中" : "面板提醒"}</span>
          <span class="alert-time">${fmtRelative(alert.created_at)}</span>
        </div>
      </div>`).join("");
    list.innerHTML = append ? list.innerHTML + rows : rows;
    $("#load-more-alerts").classList.toggle("hidden", state.alertPage >= state.alertTotalPages);
    renderNotificationError();
  };

  const loadAlerts = async (page = 1, replace = true) => {
    try {
      const result = await api(`/api/alerts?page=${page}&page_size=20`);
      state.alerts = replace ? result.items : [...state.alerts, ...result.items];
      state.alertPage = result.page;
      state.alertTotalPages = result.total_pages;
      renderAlerts(!replace);
    } catch (error) {
      toast(error.message, true);
    }
  };

  const connectStream = () => {
    if (state.stream) state.stream.close();
    if (!state.token) return;
    const stream = new EventSource("/api/events");
    state.stream = stream;
    stream.addEventListener("quotes_updated", async () => { await refreshMonitors(); });
    stream.addEventListener("cycle_completed", async () => {
      await refreshSystem();
      await refreshMonitors();
    });
    stream.addEventListener("alerts_updated", async () => { await Promise.all([loadAlerts(1, true), refreshSystem()]); });
    stream.onerror = () => {
      if (state.stream === stream) {
        stream.close();
        setTimeout(connectStream, 5000);
      }
    };
  };

  const refreshSystem = async () => {
    try {
      state.system = await api("/api/system");
      renderSystem();
    } catch (_) { /* transient */ }
  };

  const refreshMonitors = async () => {
    try {
      state.monitors = await api("/api/monitors");
      renderWatchlist();
      renderOnboarding();
    } catch (_) { /* transient */ }
  };

  const openDialog = (dialog) => {
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  };
  const closeDialog = (dialog) => {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  };

  const resetMonitorForm = () => {
    state.editingMonitorId = null;
    state.draftRules = [];
    $("#monitor-form").reset();
    $("#monitor-id").value = "";
    $("#monitor-enabled").checked = true;
    $("#monitor-code").classList.remove("input-valid", "input-invalid");
    hideInstrumentResults();
    $("#code-hint").className = "field-hint";
    $("#code-hint").textContent = "支持中文名、拼音首字母、6 位代码；指数可输入 1A0001 或 000001.SH。";
    $("#monitor-dialog-title").textContent = "添加股票";
    $("#rule-editor").classList.add("hidden");
    $("#monitor-form-error").classList.add("hidden");
    renderDraftRules();
  };

  const openMonitorDialog = (monitor = null) => {
    resetMonitorForm();
    if (monitor) {
      state.editingMonitorId = monitor.id;
      state.draftRules = monitor.rules.map((rule) => ({ ...rule, _existing: true }));
      $("#monitor-dialog-title").textContent = "编辑股票";
      $("#monitor-id").value = monitor.id;
      $("#monitor-code").value = monitor.code;
      $("#monitor-name").value = monitor.name || "";
      $("#monitor-note").value = monitor.note || "";
      $("#monitor-enabled").checked = monitor.enabled;
    }
    $("#rule-editor").classList.remove("hidden");
    renderDraftRules();
    openDialog($("#monitor-dialog"));
    setTimeout(() => $("#monitor-code").focus(), 80);
  };

  const renderDraftRules = () => {
    const list = $("#rule-list");
    if (!state.draftRules.length) {
      list.innerHTML = '<p class="muted" style="font-size:11.5px;margin:0 0 8px">还没有条件。可以先保存股票，之后再添加条件。</p>';
      return;
    }
    list.innerHTML = state.draftRules.map((rule, index) => `
      <div class="rule-edit-row">
        <span>${escapeHtml(ruleSummary(rule))}</span>
        <button type="button" data-remove-draft-rule="${index}">移除</button>
      </div>`).join("");
  };

  const makeRuleFromForm = () => {
    const field = $("#rule-field").value;
    const isCross = field === "macd_cross_up" || field === "macd_cross_down";
    const isMaCross = field === "ma_cross_up" || field === "ma_cross_down";
    const isIndicator = field.startsWith("macd_") || field.startsWith("ma_");
    return {
      field,
      operator: (isCross || isMaCross) ? "eq" : $("#rule-operator").value,
      threshold: (isCross || isMaCross) ? 1 : Number($("#rule-threshold").value),
      period: isIndicator ? $("#rule-period").value : "daily",
      ma_period: field === "ma_price" ? Number($("#rule-ma-period").value) : 20,
      cooldown_minutes: Number($("#rule-cooldown").value) || 0,
      consecutive_hits_required: Number($("#rule-consecutive").value) || 1,
      enabled: true,
      description: $("#rule-description").value.trim(),
    };
  };

  const patchRuleFormVisibility = () => {
    const field = $("#rule-field").value;
    const isCross = field === "macd_cross_up" || field === "macd_cross_down";
    const isMaCross = field === "ma_cross_up" || field === "ma_cross_down";
    const isIndicator = field.startsWith("macd_") || field.startsWith("ma_");
    const isMaPrice = field === "ma_price";
    $("#operator-field").classList.toggle("hidden", isCross || isMaCross);
    $("#threshold-field").classList.toggle("hidden", isCross || isMaCross);
    $("#ma-period-field").classList.toggle("hidden", !isMaPrice);
    $("#ma-period-field").classList.toggle("span-2", isMaPrice);
    $("#period-field").classList.toggle("hidden", !isIndicator);
    const hints = {
      last_price: "<b>价格到线就提醒。</b>比如现价 ≥ 1500，或跌到 ≤ 1350。",
      change_pct: "<b>按当日涨跌幅提醒。</b>例如涨幅 ≥ 5% 或跌幅 ≤ -3%（填 -3）。",
      macd_dif: "<b>DIF 是 MACD 的快线。</b>只在选中周期上计算，可设置大于、小于或等于某个值。",
      macd_dea: "<b>DEA 是 MACD 的信号线。</b>通常与 DIF 一起看，两者交叉会形成金叉或死叉。",
      macd_hist: "<b>MACD 柱是 DIF 与 DEA 的差值放大 2 倍。</b>柱子转正/转负常被用作动能变化的参考。",
      macd_cross_up: "<b>金叉：DIF 从下方上穿 DEA。</b>用上一根与当前这根 K 线判断是否刚发生；同一根不会重复提醒，需配合安静时间。",
      macd_cross_down: "<b>死叉：DIF 从上方下穿 DEA。</b>用上一根与当前这根 K 线判断是否刚发生；同一根不会重复提醒，需配合安静时间。",
      turnover_rate: "<b>换手率反映当天筹码交换活跃度。</b>数值单位为百分比，例如 3 表示 3%。",
      amplitude: "<b>振幅是日内高低波动幅度。</b>例如 5 表示当天最高最低相差 5%。",
      pe_ttm: "<b>市盈率 TTM 是滚动十二个月估值。</b>适合设置估值低于或高于某个值提醒。",
      ma_price: "<b>均线价格是所选周期的平均值。</b>可提醒股价站上或跌破 MA5/10/20/60。",
      ma_cross_up: "<b>均线金叉：MA5 从下向上穿过 MA20。</b>通常作为趋势转强参考，建议配合周线或日线观察。",
      ma_cross_down: "<b>均线死叉：MA5 从上向下穿过 MA20。</b>通常作为趋势转弱参考，建议配合周线或日线观察。",
      volume: "<b>成交量按行情源原始单位（手）。</b>不同股票的绝对值差异很大，建议先观察再设阈值。",
    };
    $("#rule-explainer").innerHTML = hints[field] || "<b>按行情字段设置一个阈值。</b>触发后进入提醒队列。";
  };

  const openRuleDialog = (index = null) => {
    state.editingRuleIndex = index;
    const rule = index == null ? null : state.draftRules[index];
    $("#rule-dialog-title").textContent = rule ? "编辑提醒条件" : "添加提醒条件";
    $("#rule-id").value = index == null ? "" : String(index);
    $("#rule-field").value = rule?.field || "last_price";
    $("#rule-operator").value = rule?.operator || "gte";
    $("#rule-threshold").value = rule?.threshold ?? "";
    $("#rule-period").value = rule?.period || "daily";
    $("#rule-ma-period").value = rule?.ma_period || 20;
    $("#rule-cooldown").value = rule?.cooldown_minutes ?? 30;
    $("#rule-consecutive").value = rule?.consecutive_hits_required ?? 1;
    $("#rule-description").value = rule?.description || "";
    $("#rule-form-error").classList.add("hidden");
    patchRuleFormVisibility();
    openDialog($("#rule-dialog"));
  };

  const switchChannel = (channel) => {
    state.activeChannel = channel;
    $$("[data-channel]").forEach((button) => button.classList.toggle("active", button.dataset.channel === channel));
    ["wecom", "bark", "webhook", "none"].forEach((name) => {
      $(`#channel-${name}`).classList.toggle("hidden", name !== channel);
    });
  };

  const notificationPayload = () => ({
    channel: state.activeChannel,
    webhook_url: state.activeChannel === "wecom" ? $("#wecom-webhook").value.trim() : state.activeChannel === "webhook" ? $("#generic-webhook").value.trim() : "",
    bark_url: state.activeChannel === "bark" ? $("#bark-url").value.trim() : "",
  });

  const savedNotificationPayload = () => {
    const notification = state.notification || {};
    return {
      channel: notification.channel || "wecom",
      webhook_url: notification.webhook_url || "",
      bark_url: notification.bark_url || "",
    };
  };

  const openNotificationDialog = (focusTest = false) => {
    const notification = state.notification || { channel: "wecom", webhook_url: "", bark_url: "" };
    state.activeChannel = notification.channel === "none" ? "none" : notification.channel || "wecom";
    $("#wecom-webhook").value = notification.channel === "wecom" ? notification.webhook_url : "";
    $("#bark-url").value = notification.channel === "bark" ? notification.bark_url : "";
    $("#generic-webhook").value = notification.channel === "webhook" ? notification.webhook_url : "";
    switchChannel(state.activeChannel);
    $("#notification-form-error").classList.add("hidden");
    openDialog($("#notification-dialog"));
    if (focusTest) setTimeout(() => $("#test-notification-btn").focus(), 80);
  };

  const savePollInterval = async (seconds) => {
    const safe = Math.max(60, Math.min(86400, Math.round(seconds)));
    try {
      state.system = await api("/api/system/poll-interval", { method: "PUT", body: JSON.stringify({ poll_interval_seconds: safe }) });
      state.pollInterval = state.system.poll_interval_seconds;
      renderSystem();
      renderIntervalPicker();
      toast(`检查间隔已改为 ${intervalText(safe)}`);
    } catch (error) {
      toast(error.message, true);
    }
  };

  const bindEvents = () => {
    $$("[data-auth-tab]").forEach((button) => button.addEventListener("click", () => switchAuthTab(button.dataset.authTab)));
    $("#login-form").addEventListener("submit", (event) => {
      event.preventDefault();
      login(event.currentTarget, "/api/auth/login", { username: $("#login-username").value.trim(), password: $("#login-password").value });
    });
    $("#register-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorNode = $("#register-error");
      errorNode.classList.add("hidden");
      if ($("#register-password").value !== $("#register-password-confirm").value) {
        errorNode.textContent = "两次输入的密码不一致";
        errorNode.classList.remove("hidden");
        return;
      }
      await login(event.currentTarget, "/api/auth/register", {
        username: $("#register-username").value.trim(),
        password: $("#register-password").value,
      }, (error) => {
        errorNode.textContent = error.message;
        errorNode.classList.remove("hidden");
      });
    });
    $("#logout-btn").addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* ignore */ }
      clearSession();
      showLogin();
      switchAuthTab("login");
      setTimeout(() => $("#login-username").focus(), 40);
    });
    $("#change-password-btn").addEventListener("click", () => {
      $("#password-form").reset();
      $("#password-form-error").classList.add("hidden");
      openDialog($("#password-dialog"));
      setTimeout(() => $("#password-current").focus(), 60);
    });
    $("#password-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorNode = $("#password-form-error");
      errorNode.classList.add("hidden");
      const currentPassword = $("#password-current").value;
      const newPassword = $("#password-new").value;
      if (newPassword !== $("#password-confirm").value) {
        errorNode.textContent = "两次输入的新密码不一致";
        errorNode.classList.remove("hidden");
        return;
      }
      const button = $("button[type=submit]", event.currentTarget);
      setBusy(button, true, "保存中…");
      try {
        const result = await api("/api/auth/change-password", {
          method: "POST",
          body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
        });
        state.token = result.token;
        localStorage.setItem("stockRadarToken", result.token);
        closeDialog($("#password-dialog"));
        toast("密码已修改，当前登录保持有效");
      } catch (error) {
        errorNode.textContent = error.message;
        errorNode.classList.remove("hidden");
      } finally {
        setBusy(button, false);
      }
    });

    $("#onboarding-action").addEventListener("click", () => openMonitorDialog());
    $("#add-monitor-btn").addEventListener("click", () => openMonitorDialog());
    $("#refresh-quotes-btn").addEventListener("click", async (event) => {
      setBusy(event.currentTarget, true, "刷新中");
      try {
        await api("/api/quotes/refresh", { method: "POST" });
        await refreshMonitors();
        toast("行情已刷新");
      } catch (error) { toast(error.message, true); }
      finally { setBusy(event.currentTarget, false); }
    });
    $("#run-once-btn").addEventListener("click", async (event) => {
      setBusy(event.currentTarget, true, "检查中…");
      try {
        const result = await api("/api/run-once", { method: "POST" });
        await Promise.all([refreshMonitors(), refreshSystem(), loadAlerts(1, true)]);
        toast(result.message || "检查完成");
      } catch (error) { toast(error.message, true); }
      finally { setBusy(event.currentTarget, false); }
    });
    $$("[data-interval]").forEach((button) => button.addEventListener("click", () => savePollInterval(Number(button.dataset.interval))));
    $("#custom-interval").addEventListener("change", (event) => savePollInterval(Number(event.target.value) * 60));
    $("#open-notification-settings").addEventListener("click", openNotificationDialog);
    $("#load-more-alerts").addEventListener("click", () => loadAlerts(state.alertPage + 1, false));
    $("#clear-alerts-btn").addEventListener("click", async () => {
      if (!confirm("清理所有已发送或失败的提醒记录？面板内保留的条件不会被删除。")) return;
      try {
        const result = await api("/api/alerts", { method: "DELETE" });
        await loadAlerts(1, true);
        toast(`已清理 ${result.cleared} 条记录`);
      } catch (error) { toast(error.message, true); }
    });

    $("#watchlist").addEventListener("click", async (event) => {
      const toggle = event.target.closest("[data-toggle-monitor]");
      if (toggle) {
        const detail = $(`[data-detail="${toggle.dataset.toggleMonitor}"]`);
        const open = detail.classList.toggle("hidden");
        toggle.setAttribute("aria-expanded", String(!open));
        toggle.textContent = open ? "⌄" : "⌃";
        return;
      }
      const edit = event.target.closest("[data-edit-monitor]");
      if (edit) {
        const monitor = state.monitors.find((item) => item.id === Number(edit.dataset.editMonitor));
        if (monitor) openMonitorDialog(monitor);
        return;
      }
      const addRule = event.target.closest("[data-add-rule]");
      if (addRule) {
        const monitor = state.monitors.find((item) => item.id === Number(addRule.dataset.addRule));
        if (monitor) openMonitorDialog(monitor);
        return;
      }
      const remove = event.target.closest("[data-delete-monitor]");
      if (remove) {
        const monitor = state.monitors.find((item) => item.id === Number(remove.dataset.deleteMonitor));
        if (!monitor || !confirm(`删除「${monitor.name || monitor.code}」及其全部条件与提醒记录？`)) return;
        try {
          await api(`/api/monitors/${monitor.id}`, { method: "DELETE" });
          await refreshMonitors();
          await loadAlerts(1, true);
          toast("已删除");
        } catch (error) { toast(error.message, true); }
      }
    });

    $("#monitor-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorNode = $("#monitor-form-error");
      errorNode.classList.add("hidden");
      if (!$("#monitor-code").value.trim()) { errorNode.textContent = "请输入股票代码或名称"; errorNode.classList.remove("hidden"); return; }
      const button = $("#save-monitor-btn");
      setBusy(button, true, "解析中…");
      try {
        const resolved = await preflightMonitorCode(false);
        if (!resolved) throw new Error($("#code-hint").textContent || "无法识别股票，请从候选列表选择");
        const payload = {
          code: resolved.code,
          name: $("#monitor-name").value.trim() || resolved.name || "",
          note: $("#monitor-note").value.trim(),
          enabled: $("#monitor-enabled").checked,
          instrument_type: resolved.instrument_type || "stock",
        };
        setBusy(button, true, "保存中…");
        let monitor;
        if (state.editingMonitorId) {
          monitor = await api(`/api/monitors/${state.editingMonitorId}`, { method: "PUT", body: JSON.stringify(payload) });
        } else {
          monitor = await api("/api/monitors", { method: "POST", body: JSON.stringify(payload) });
        }
        const existingIds = new Set(
          (state.editingMonitorId ? state.monitors.find((item) => item.id === state.editingMonitorId)?.rules || [] : [])
            .map((rule) => rule.id)
        );
        const keptIds = new Set();
        for (const rule of state.draftRules) {
          const { id, _existing, monitor_id, created_at, updated_at, last_triggered_at, last_trigger_value, current_consecutive_hits, ...payload } = rule;
          if (id && _existing) {
            await api(`/api/rules/${id}`, { method: "PUT", body: JSON.stringify(payload) });
            keptIds.add(id);
          } else {
            await api(`/api/monitors/${monitor.id}/rules`, { method: "POST", body: JSON.stringify(payload) });
          }
        }
        for (const id of existingIds) {
          if (!keptIds.has(id)) await api(`/api/rules/${id}`, { method: "DELETE" });
        }
        closeDialog($("#monitor-dialog"));
        await Promise.all([refreshMonitors(), refreshSystem()]);
        toast(state.editingMonitorId ? "已保存" : "股票已添加");
      } catch (error) {
        errorNode.textContent = error.message;
        errorNode.classList.remove("hidden");
      } finally {
        setBusy(button, false);
      }
    });

    $("#add-rule-btn").addEventListener("click", () => openRuleDialog());
    $("#monitor-code").addEventListener("blur", () => setTimeout(() => { if (!$("#instrument-results").contains(document.activeElement)) preflightMonitorCode(); }, 120));
    $("#monitor-code").addEventListener("input", (event) => {
      event.target.classList.remove("input-valid", "input-invalid");
      $("#code-hint").className = "field-hint";
      $("#code-hint").textContent = "支持中文名、拼音首字母、6 位代码；指数可输入 1A0001 或 000001.SH。";
      clearTimeout(searchTimer);
      if (!event.target.value.trim()) { hideInstrumentResults(); return; }
      searchTimer = setTimeout(searchInstruments, 220);
    });
    $("#instrument-results").addEventListener("mousedown", async (event) => {
      const item = event.target.closest("[data-result-code]");
      if (!item) return;
      event.preventDefault();
      await selectInstrument(item.dataset.resultCode, item.dataset.resultName);
    });
    $("#monitor-code").addEventListener("keydown", async (event) => {
      const results = $("#instrument-results");
      const options = $$("[data-result-code]", results);
      if (event.key === "Escape") {
        hideInstrumentResults();
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (results.classList.contains("hidden") || !options.length) return;
        event.preventDefault();
        const currentIndex = options.indexOf(document.activeElement);
        const nextIndex = event.key === "ArrowDown"
          ? (currentIndex < 0 ? 0 : (currentIndex + 1) % options.length)
          : (currentIndex <= 0 ? options.length - 1 : currentIndex - 1);
        options[nextIndex].focus();
        return;
      }
      if (event.key === "Enter" && !results.classList.contains("hidden") && options.length) {
        event.preventDefault();
        const target = options[0];
        await selectInstrument(target.dataset.resultCode, target.dataset.resultName);
      }
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#monitor-code") && !event.target.closest("#instrument-results")) hideInstrumentResults();
    });
    $("#rule-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-draft-rule]");
      if (!button) return;
      state.draftRules.splice(Number(button.dataset.removeDraftRule), 1);
      renderDraftRules();
    });
    $("#rule-field").addEventListener("change", patchRuleFormVisibility);
    $("#rule-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const errorNode = $("#rule-form-error");
      errorNode.classList.add("hidden");
      const rule = makeRuleFromForm();
      const isCross = ["macd_cross_up", "macd_cross_down", "ma_cross_up", "ma_cross_down"].includes(rule.field);
      const rawThreshold = $("#rule-threshold").value.trim();
      if (!isCross && rawThreshold === "") {
        errorNode.textContent = "请输入触发值";
        errorNode.classList.remove("hidden");
        return;
      }
      if (!isCross && !Number.isFinite(rule.threshold)) {
        errorNode.textContent = "请输入有效的触发值";
        errorNode.classList.remove("hidden");
        return;
      }
      if (!Number.isInteger(rule.consecutive_hits_required) || rule.consecutive_hits_required < 1 || rule.consecutive_hits_required > 100) {
        errorNode.textContent = "连续命中次数需为 1-100 的整数";
        errorNode.classList.remove("hidden");
        return;
      }
      if (!Number.isFinite(rule.cooldown_minutes) || rule.cooldown_minutes < 0 || rule.cooldown_minutes > 1440) {
        errorNode.textContent = "安静时间需为 0-1440 分钟";
        errorNode.classList.remove("hidden");
        return;
      }
      if (state.editingRuleIndex == null) state.draftRules.push(rule);
      else state.draftRules[state.editingRuleIndex] = { ...state.draftRules[state.editingRuleIndex], ...rule };
      closeDialog($("#rule-dialog"));
      renderDraftRules();
    });

    $$("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.closest("dialog")));
    });
    $$("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => { if (event.target === dialog) closeDialog(dialog); });
    });

    $$("[data-channel]").forEach((button) => button.addEventListener("click", () => switchChannel(button.dataset.channel)));
    $("#notification-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorNode = $("#notification-form-error");
      errorNode.classList.add("hidden");
      const payload = notificationPayload();
      if (payload.channel === "wecom" && !payload.webhook_url) {
        errorNode.textContent = "请填写企业微信 Webhook 地址";
        errorNode.classList.remove("hidden");
        return;
      }
      if (payload.channel === "bark" && !payload.bark_url) {
        errorNode.textContent = "请填写 Bark 推送地址";
        errorNode.classList.remove("hidden");
        return;
      }
      if (payload.channel === "webhook" && !payload.webhook_url) {
        errorNode.textContent = "请填写 Webhook 地址";
        errorNode.classList.remove("hidden");
        return;
      }
      try {
        state.notification = await api("/api/settings/notification", { method: "PUT", body: JSON.stringify(payload) });
        renderNotification();
        closeDialog($("#notification-dialog"));
        toast("通知设置已保存");
      } catch (error) {
        errorNode.textContent = error.message;
        errorNode.classList.remove("hidden");
      }
    });
    $("#test-notification-btn").addEventListener("click", async (event) => {
      const errorNode = $("#notification-form-error");
      errorNode.classList.add("hidden");
      const payload = notificationPayload();
      if (payload.channel === "none") {
        errorNode.textContent = "当前选择的是“仅面板”，不会发送外部消息";
        errorNode.classList.remove("hidden");
        return;
      }
      if (payload.channel === "wecom" && !payload.webhook_url) {
        errorNode.textContent = "请填写企业微信 Webhook 地址";
        errorNode.classList.remove("hidden");
        return;
      }
      if (payload.channel === "bark" && !payload.bark_url) {
        errorNode.textContent = "请填写 Bark 推送地址";
        errorNode.classList.remove("hidden");
        return;
      }
      if (payload.channel === "webhook" && !payload.webhook_url) {
        errorNode.textContent = "请填写 Webhook 地址";
        errorNode.classList.remove("hidden");
        return;
      }
      setBusy(event.currentTarget, true, "发送中…");
      try {
        await api("/api/settings/notification/test", { method: "POST", body: JSON.stringify(payload) });
        toast("测试消息已发送，请检查接收端");
      } catch (error) {
        errorNode.textContent = error.message;
        errorNode.classList.remove("hidden");
      } finally { setBusy(event.currentTarget, false); }
    });
    $("#quick-test-notification").addEventListener("click", async (event) => {
      const notification = state.notification;
      if (!notification || !notification.configured) {
        toast("请先配置并保存一种通知方式", true);
        openNotificationDialog(true);
        return;
      }
      setBusy(event.currentTarget, true, "发送中…");
      try {
        await api("/api/settings/notification/test", {
          method: "POST",
          body: JSON.stringify(savedNotificationPayload()),
        });
        toast("测试消息已发送，请检查接收端");
      } catch (error) {
        toast(`测试失败：${error.message}`, true);
      } finally {
        setBusy(event.currentTarget, false);
      }
    });
  };

  const populateRuleSelects = () => {
    if (!state.metadata) return;
    $("#rule-field").innerHTML = state.metadata.fields.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
    $("#rule-operator").innerHTML = state.metadata.operators.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
    $("#rule-period").innerHTML = state.metadata.periods.map((item) => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`).join("");
    patchRuleFormVisibility();
  };

  const boot = async () => {
    bindEvents();
    if (state.token) {
      try {
        await enterApp();
        return;
      } catch (_) {
        clearSession();
      }
    }
    showLogin();
    switchAuthTab("login");
    setTimeout(() => $("#login-username").focus(), 40);
  };

  window.addEventListener("DOMContentLoaded", boot);
})();
