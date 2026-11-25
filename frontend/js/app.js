/**
 * app.js — MA 自动交易系统主控制器 (AppController)
 * 协调图表、设置、回测、统计、实时行情与 WebSocket 通信
 */

const AppController = (() => {
  // ─── State ─────────────────────────────────────────────────────────
  const state = {
    currentSymbol: 'BTCUSDT',
    currentTimeframe: '1m',
    currentPrice: null,
    symbols: [],
    trades: [],
    botRunning: false,
    paperRunning: false,
    liveRunning: false,
    ws: null,
    wsPingInterval: null,
    wsReconnectTimeout: null,
    klinesData: [],
    isLoadingHistory: false,
    hasMoreHistory: true,
    tradingMode: 'paper',
    tradesMode: 'paper',
    tradesPage: 1,
    tradesPageSize: 20,
    logs: [],
    logsTypeFilter: '',
    logsPage: 1,
    logsPageSize: 20,
    logsTotal: 0,
    logsTotalPages: 1
  };

  // ─── Initialization ────────────────────────────────────────────────
  async function init() {
    console.log('[App] Initializing MA Trader App...');

    // 0. Setup Theme (Day / Night mode)
    setupTheme();

    // 0.05 Setup collapsible sidebar (collapsed by default on small screens)
    setupSidebar();

    // 0.1 Check Authentication (protect frontend dashboard)
    if (typeof AuthManager !== 'undefined') {
      AuthManager.init();
      const authed = await AuthManager.checkAuth();
      if (!authed) {
        console.log('[App] User not authenticated. Displaying login overlay...');
        return;
      }
    }

    // 1. Init Sub-managers
    if (typeof ChartManager !== 'undefined') {
      ChartManager.init();
      ChartManager.setOnReachStart(() => {
        loadMoreHistoryKlines();
      });
    }
    if (typeof SettingsManager !== 'undefined') SettingsManager.init();
    if (typeof BacktestManager !== 'undefined') BacktestManager.init();

    // 2. Setup DOM Event Listeners
    setupEventListeners();

    // 3. Start Clock
    updateClock();
    setInterval(updateClock, 1000);

    // 4. Initial HTTP Data Load (isolated so one failure does not block the chart)
    try { await loadSettings(); } catch (e) { console.warn(e); }
    try { await loadSymbols(); } catch (e) { console.warn(e); }
    try { await checkBotStatus(); } catch (e) { console.warn(e); }
    try { await loadTrades(); } catch (e) { console.warn(e); }
    try { await loadStats(); } catch (e) { console.warn(e); }

    // Ensure chart is loaded
    if (!state.klinesData || state.klinesData.length === 0) {
      try { await loadKlines(); } catch (e) { console.warn(e); }
    }

    // 5. Connect local WebSocket for trade alerts and bot status
    connectWS();

    // 6. Connect Binance official public Kline WebSocket for real-time live streaming (no polling)
    connectBinanceStream(state.currentSymbol, state.currentTimeframe);
  }

  // ─── DOM Event Listeners ───────────────────────────────────────────
  function setupEventListeners() {
    // Timeframe buttons
    document.querySelectorAll('#tfBtns .tf-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const tf = e.currentTarget.dataset.tf;
        switchTimeframe(tf);
      });
    });

    // Dual Bot Toggles (Paper & Live independent start / stop with Modal Confirm)
    const btnPaperToggle = document.getElementById('btnPaperToggle');
    const btnLiveToggle  = document.getElementById('btnLiveToggle');
    if (btnPaperToggle) btnPaperToggle.addEventListener('click', () => handleBotToggleClick('paper'));
    if (btnLiveToggle)  btnLiveToggle.addEventListener('click', () => handleBotToggleClick('live'));

    // Refresh symbols
    const btnRefSym = document.getElementById('btnRefreshSymbols');
    if (btnRefSym) btnRefSym.addEventListener('click', loadSymbols);

    // Refresh stats
    const btnRefStats = document.getElementById('btnRefreshStats');
    if (btnRefStats) btnRefStats.addEventListener('click', loadStats);

    // Stats filters change
    ['statsSymbol', 'statsDays', 'statsMode'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', loadStats);
    });

    // Tab switch triggers
    const tabStatsLink = document.querySelector('a[href="#tabStats"]');
    if (tabStatsLink) {
      tabStatsLink.addEventListener('shown.bs.tab', () => {
        loadStats();
        if (typeof StatsCharts !== 'undefined' && StatsCharts.resize) {
          setTimeout(() => StatsCharts.resize(), 60);
        }
      });
    }
    const tabTradesLink = document.querySelector('a[href="#tabTrades"]');
    if (tabTradesLink) {
      tabTradesLink.addEventListener('shown.bs.tab', () => {
        loadTrades();
        if (typeof TradesEquityChart !== 'undefined') {
          setTimeout(() => TradesEquityChart.resize(), 60);
        }
      });
    }
    const tradesEqSym = document.getElementById('tradesEquitySymbol');
    if (tradesEqSym) {
      tradesEqSym.addEventListener('change', () => {
        state.tradesPage = 1;
        renderTradesView();
      });
    }
    const tradesModeWrap = document.getElementById('tradesModeTabs');
    if (tradesModeWrap) {
      tradesModeWrap.querySelectorAll('button[data-mode]').forEach(btn => {
        btn.addEventListener('click', () => {
          state.tradesMode = btn.getAttribute('data-mode') || 'paper';
          state.tradesPage = 1;
          tradesModeWrap.querySelectorAll('button[data-mode]').forEach(b => {
            const isActive = b === btn;
            b.classList.toggle('active', isActive);
            if (isActive) {
              b.className = b.getAttribute('data-mode') === 'live'
                ? 'btn btn-danger active'
                : 'btn btn-primary active';
            } else {
              b.className = 'btn btn-outline-secondary';
            }
          });
          renderTradesView();
        });
      });
    }
    const tabSettingsLink = document.querySelector('a[href="#tabSettings"]');
    if (tabSettingsLink) {
      tabSettingsLink.addEventListener('shown.bs.tab', loadSettings);
    }

    const tabLogsLink = document.querySelector('a[href="#tabLogs"]');
    if (tabLogsLink) {
      tabLogsLink.addEventListener('shown.bs.tab', loadOperationLogs);
    }

    const logsTypeWrap = document.getElementById('logsTypeFilter');
    if (logsTypeWrap) {
      logsTypeWrap.querySelectorAll('button[data-log-type]').forEach(btn => {
        btn.addEventListener('click', () => {
          state.logsTypeFilter = btn.getAttribute('data-log-type') || '';
          state.logsPage = 1;
          logsTypeWrap.querySelectorAll('button[data-log-type]').forEach(b => {
            const isActive = b === btn;
            b.className = isActive ? 'btn btn-primary active' : 'btn btn-outline-secondary';
          });
          loadOperationLogs();
        });
      });
    }
    const btnRefreshLogs = document.getElementById('btnRefreshLogs');
    if (btnRefreshLogs) btnRefreshLogs.addEventListener('click', loadOperationLogs);
    const btnClearLogs = document.getElementById('btnClearLogs');
    if (btnClearLogs) btnClearLogs.addEventListener('click', clearOperationLogs);

    // Theme toggle (Day / Night mode)
    const btnThemeToggle = document.getElementById('btnThemeToggle');
    if (btnThemeToggle) {
      btnThemeToggle.addEventListener('click', toggleTheme);
    }

    // Sidebar collapse/expand toggle
    const btnToggleSidebar = document.getElementById('btnToggleSidebar');
    if (btnToggleSidebar) {
      btnToggleSidebar.addEventListener('click', toggleSidebar);
    }

    // Simulate Trade Buttons (if present in DOM)
    const btnSimLong = document.getElementById('btnSimLong');
    const btnSimShort = document.getElementById('btnSimShort');
    if (btnSimLong) btnSimLong.addEventListener('click', () => simulateTrade('long'));
    if (btnSimShort) btnSimShort.addEventListener('click', () => simulateTrade('short'));

    // Clear all trades button
    const btnClearTrades = document.getElementById('btnClearTrades');
    if (btnClearTrades) btnClearTrades.addEventListener('click', clearAllTrades);
  }

  // ─── Sidebar Collapse / Expand ─────────────────────────────────────
  function setupSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    const isSmallScreen = window.innerWidth < 1920;
    const saved = localStorage.getItem('ma_sidebar_collapsed');
    const shouldCollapse = isSmallScreen ? true : (saved === '1');
    sidebar.classList.toggle('collapsed', shouldCollapse);
  }

  function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    const collapsed = sidebar.classList.toggle('collapsed');
    if (window.innerWidth >= 1920) {
      localStorage.setItem('ma_sidebar_collapsed', collapsed ? '1' : '0');
    }
    setTimeout(() => {
      window.dispatchEvent(new Event('resize'));
      if (typeof ChartManager !== 'undefined' && ChartManager.resize) ChartManager.resize();
      if (typeof StatsCharts !== 'undefined' && StatsCharts.resize) StatsCharts.resize();
      if (typeof TradesEquityChart !== 'undefined' && TradesEquityChart.resize) TradesEquityChart.resize();
    }, 220);
  }

  // ─── Theme Management (Day / Night Mode) ────────────────────────────
  function setupTheme() {
    const savedTheme = localStorage.getItem('ma_trader_theme') || 'dark';
    applyTheme(savedTheme, false);
  }

  function toggleTheme() {
    const active = document.documentElement.getAttribute('data-bs-theme') || 'dark';
    const next = active === 'dark' ? 'light' : 'dark';
    localStorage.setItem('ma_trader_theme', next);
    applyTheme(next, true);
  }

  function applyTheme(theme, notifyCharts = true) {
    document.documentElement.setAttribute('data-bs-theme', theme);
    document.documentElement.classList.toggle('dark', theme === 'dark');
    const icon = document.getElementById('themeIcon');
    const label = document.getElementById('themeLabel');
    if (theme === 'light') {
      if (icon) icon.className = 'fa-solid fa-moon';
      if (label) label.textContent = '夜间模式';
    } else {
      if (icon) icon.className = 'fa-solid fa-sun';
      if (label) label.textContent = '日间模式';
    }

    if (notifyCharts) {
      if (typeof ChartManager !== 'undefined' && ChartManager.updateTheme) {
        ChartManager.updateTheme();
      }
      if (typeof StatsCharts !== 'undefined' && StatsCharts.updateTheme) {
        StatsCharts.updateTheme();
      }
      if (typeof BacktestChart !== 'undefined' && BacktestChart.updateTheme) {
        BacktestChart.updateTheme();
      }
      if (typeof TradesEquityChart !== 'undefined' && TradesEquityChart.updateTheme) {
        TradesEquityChart.updateTheme();
      }
    }
  }

  // ─── Clock ─────────────────────────────────────────────────────────
  function updateClock() {
    const el = document.getElementById('clockDisplay');
    if (!el) return;
    const now = new Date();
    el.textContent = now.toTimeString().split(' ')[0] + ' UTC';
  }

  // ─── Navigation & Switching ────────────────────────────────────────
  function switchSymbol(symbol) {
    if (!symbol) return;
    symbol = symbol.toUpperCase();
    state.currentSymbol = symbol;
    state.hasMoreHistory = true;
    state.isLoadingHistory = false;

    const label = document.getElementById('chartSymbolLabel');
    if (label) label.textContent = symbol;

    // Update active class in sidebar
    document.querySelectorAll('#symbolList .symbol-item').forEach(item => {
      if (item.dataset.symbol === symbol) {
        item.classList.add('active');
      } else {
        item.classList.remove('active');
      }
    });

    // Update simulation price placeholder
    const simInput = document.getElementById('simEntryPrice');
    if (simInput && state.currentPrice) {
      simInput.placeholder = state.currentPrice.toFixed(2);
    }

    // Reset indicator displays
    resetSignalDisplay();

    // Reload Klines base chart (one-off initial load)
    loadKlines();

    // Switch Binance real-time WebSocket stream
    connectBinanceStream(symbol, state.currentTimeframe);
  }

  function switchTimeframe(tf) {
    if (!tf) return;
    state.currentTimeframe = tf;
    state.hasMoreHistory = true;
    state.isLoadingHistory = false;

    document.querySelectorAll('#tfBtns .tf-btn').forEach(btn => {
      if (btn.dataset.tf === tf) {
        btn.classList.add('active');
      } else {
        btn.classList.remove('active');
      }
    });

    loadKlines();

    // Switch Binance real-time WebSocket stream
    connectBinanceStream(state.currentSymbol, tf);
  }

  // ─── HTTP API Calls ────────────────────────────────────────────────
  async function loadSettings() {
    try {
      const res = await fetch('/api/settings');
      if (res.ok) {
        const data = await res.json();
        const settingsObj = data.settings || data;
        if (settingsObj.trading_mode) {
          state.tradingMode = settingsObj.trading_mode;
        }
        if (typeof SettingsManager !== 'undefined') {
          SettingsManager.loadGlobalSettings(data);
        }
      }
    } catch (err) {
      console.warn('[App] Load settings error:', err);
    }
  }

  async function loadSymbols() {
    try {
      const res = await fetch('/api/symbols');
      if (!res.ok) return;
      const symbols = await res.json();
      state.symbols = symbols;

      if (typeof SettingsManager !== 'undefined') {
        SettingsManager.loadSymbols(symbols);
      }

      // If active symbol not in list or default, select first
      if (symbols.length > 0) {
        const found = symbols.find(s => s.symbol === state.currentSymbol);
        if (!found) {
          switchSymbol(symbols[0].symbol);
        } else {
          switchSymbol(state.currentSymbol);
        }
      } else {
        switchSymbol(state.currentSymbol || 'BTCUSDT');
      }
    } catch (err) {
      console.warn('[App] Load symbols error:', err);
    }
  }

  async function loadKlines() {
    if (!state.currentSymbol) return;
    try {
      const url = `/api/klines/${state.currentSymbol}?timeframe=${state.currentTimeframe}&limit=200`;
      const res = await fetch(url);
      if (!res.ok) return;
      const body = await res.json();
      state.klinesData = body.data || [];

      // Pass matching trades to chart
      const symbolTrades = state.trades.filter(t => t.symbol === state.currentSymbol);
      if (typeof ChartManager !== 'undefined') {
        ChartManager.render(state.klinesData, symbolTrades);
      }

      // Update current price & indicators from latest candle if available
      if (state.klinesData.length > 0) {
        const lastCandle = state.klinesData[state.klinesData.length - 1];
        const lastClose = Array.isArray(lastCandle) ? lastCandle[4] : (lastCandle.close ?? lastCandle.c);
        if (lastClose) {
          updateCurrentPrice(Number(lastClose));
        }

        // Also update initial MA readings from candle
        if (lastCandle && !Array.isArray(lastCandle)) {
          const m7  = lastCandle.ma7;
          const m25 = lastCandle.ma25;
          const m60 = lastCandle.ma60;
          if (m7 != null)  document.getElementById('ma7Val').textContent  = Number(m7).toFixed(1);
          if (m25 != null) document.getElementById('ma25Val').textContent = Number(m25).toFixed(1);
          if (m60 != null) document.getElementById('ma60Val').textContent = Number(m60).toFixed(1);

          const dir = document.getElementById('maDirection');
          if (dir && m7 != null && m25 != null && m60 != null) {
            if (m7 > m25 && m25 > m60) {
              dir.textContent = '▲ 多头排列';
              dir.className = 'direction-badge long';
            } else if (m7 < m25 && m25 < m60) {
              dir.textContent = '▼ 空头排列';
              dir.className = 'direction-badge short';
            } else {
              dir.textContent = '— 震荡/整理';
              dir.className = 'direction-badge flat';
            }
          }
        }
      }
    } catch (err) {
      console.warn('[App] Load klines error:', err);
    }
  }

  // ─── Lazy-load earlier history candles when user scrolls to leftmost edge ────
  async function loadMoreHistoryKlines() {
    if (state.isLoadingHistory || !state.hasMoreHistory) return;
    if (!state.klinesData || state.klinesData.length === 0) return;

    state.isLoadingHistory = true;
    try {
      const firstCandle = state.klinesData[0];
      const rawTime = Array.isArray(firstCandle) ? firstCandle[0] : (firstCandle.time ?? firstCandle.t);
      const oldestTs = typeof rawTime === 'string' ? new Date(rawTime).getTime() : Number(rawTime);
      if (!oldestTs || isNaN(oldestTs)) {
        state.isLoadingHistory = false;
        return;
      }

      const url = `/api/klines/${state.currentSymbol}?timeframe=${state.currentTimeframe}&limit=100&endTime=${oldestTs - 1}`;
      console.log(`[App] Requesting earlier history klines: endTime=${oldestTs - 1}`);

      const res = await fetch(url);
      if (!res.ok) {
        console.warn('[App] Load more history HTTP error:', res.status);
        state.isLoadingHistory = false;
        return;
      }

      const body = await res.json();
      const olderData = body.data || [];
      if (!olderData.length) {
        console.log('[App] No more history klines returned from exchange.');
        state.hasMoreHistory = false;
      } else {
        const added = ChartManager.prependHistory(olderData);
        if (added) {
          state.klinesData = [...olderData, ...state.klinesData];
          console.log(`[App] Loaded ${olderData.length} earlier candles. Total: ${state.klinesData.length}`);
        } else {
          state.hasMoreHistory = false;
        }
      }
    } catch (err) {
      console.warn('[App] Load more history exception:', err);
    } finally {
      // Small cooldown to prevent scroll bouncing
      setTimeout(() => {
        state.isLoadingHistory = false;
      }, 400);
    }
  }

  async function loadTrades() {
    try {
      const res = await fetch('/api/trades?limit=500');
      if (!res.ok) return;
      const trades = await res.json();
      state.trades = trades;

      renderTradesView();

      // Update chart markers if on chart tab
      if (typeof ChartManager !== 'undefined' && state.klinesData.length > 0) {
        const symbolTrades = trades.filter(t => t.symbol === state.currentSymbol);
        ChartManager.render(state.klinesData, symbolTrades);
      }
    } catch (err) {
      console.warn('[App] Load trades error:', err);
    }
  }

  async function loadStats() {
    try {
      const sym = document.getElementById('statsSymbol')?.value || '';
      const days = document.getElementById('statsDays')?.value || '30';
      const mode = document.getElementById('statsMode')?.value || '';

      let url = `/api/stats?days=${days}`;
      if (sym) url += `&symbol=${encodeURIComponent(sym)}`;
      if (mode) url += `&is_paper=${mode === 'paper'}`;

      const res = await fetch(url);
      if (!res.ok) return;
      const stats = await res.json();

      // Update Stat Cards
      const totalEl = document.getElementById('statTotal');
      const wrEl    = document.getElementById('statWinrate');
      const pnlEl   = document.getElementById('statPnl');
      const pfEl    = document.getElementById('statPF');

      if (totalEl) totalEl.textContent = stats.total_trades ?? 0;
      if (wrEl)    wrEl.textContent = `${(stats.win_rate ?? 0).toFixed(1)}%`;
      if (pnlEl) {
        const pnl = stats.total_pnl ?? 0;
        const sign = pnl >= 0 ? '+' : '';
        pnlEl.textContent = `${sign}$${pnl.toFixed(2)}`;
        pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'text-success' : 'text-danger');
      }
      if (pfEl) {
        const pf = stats.profit_factor;
        pfEl.textContent = (pf === null || pf === undefined) ? '—' : pf.toFixed(2);
      }

      // Render Charts
      if (typeof StatsCharts !== 'undefined') {
        const daily = stats.daily_pnl || [];
        const dates = daily.map(d => d.date);
        const pnls  = daily.map(d => d.pnl);
        StatsCharts.renderDaily(dates, pnls);

        const bySym = stats.by_symbol || {};
        const symNames = Object.keys(bySym);
        const symPnls  = symNames.map(k => bySym[k].pnl);
        StatsCharts.renderSymbol(symNames, symPnls);
      }
    } catch (err) {
      console.warn('[App] Load stats error:', err);
    }
  }

  async function checkBotStatus() {
    try {
      const res = await fetch('/api/trading/status');
      if (res.ok) {
        const status = await res.json();
        if (status.mode) {
          state.tradingMode = status.mode;
        }
        updateBotUI(status);
      }
    } catch (err) {
      console.warn('[App] Check bot status error:', err);
    }
  }

  // ─── Bot Start / Stop Toggle & Modal Confirm ────────────────────────
  let botConfirmModalInstance = null;

  async function handleBotToggleClick(mode = 'paper') {
    const modalEl = document.getElementById('botConfirmModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    if (!botConfirmModalInstance) {
      botConfirmModalInstance = new bootstrap.Modal(modalEl);
    }

    const titleEl = document.getElementById('botModalTitle');
    const bodyEl  = document.getElementById('botModalBody');
    const btnAction = document.getElementById('btnConfirmBotAction');

    // Remove previous click handlers
    const newBtnAction = btnAction.cloneNode(true);
    btnAction.parentNode.replaceChild(newBtnAction, btnAction);

    const isLive = (mode === 'live');
    const modeName = isLive ? '实盘' : '模拟';
    const isRunning = isLive ? state.liveRunning : state.paperRunning;

    // 1. If this mode is currently RUNNING -> Confirm to STOP
    if (isRunning) {
      titleEl.innerHTML = `<i class="fa-solid fa-stop text-danger"></i><span>停止${modeName}自动交易监控</span>`;
      bodyEl.innerHTML = `
        <div class="text-center my-2">
          <span class="badge ${isLive ? 'bg-danger' : 'bg-secondary'} fs-6 px-3 py-1 mb-2">${modeName}运行中</span>
          <div class="text-secondary small mt-1">
            停止后系统将立即暂停<b>${modeName}</b>后台监控与自动开仓（不影响另一模式），当前未平仓订单将保持现有止盈止损状态。
          </div>
        </div>
        <div class="alert alert-warning py-2 px-3 small mb-0 mt-3 d-flex align-items-center">
          <i class="fa-solid fa-triangle-exclamation text-warning me-2 fs-5"></i>
          <div>确定要停止${modeName}自动交易吗？</div>
        </div>
      `;

      newBtnAction.className = 'btn btn-danger btn-sm';
      newBtnAction.innerHTML = `<i class="fa-solid fa-stop me-1"></i>确认停止${modeName}`;
      newBtnAction.onclick = async () => {
        botConfirmModalInstance.hide();
        await stopBot(mode);
      };

      botConfirmModalInstance.show();
      return;
    }

    // 2. If currently STOPPED -> Confirm to START
    if (isLive) {
      titleEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation text-danger"></i><span>启动自动交易（实盘）</span>';
      bodyEl.innerHTML = `
        <div class="text-center my-2">
          <span class="badge bg-danger fs-6 px-3 py-1 mb-2">⚠️ 实盘模式 (LIVE)</span>
          <div class="text-secondary small mt-2">
            开启后<b>实盘机器人</b>将按照策略信号通过 Binance API <b>使用真实资金下单交易</b>（可与模拟盘同时运行，共享行情流）。
          </div>
        </div>
        <div class="alert alert-danger py-2 px-3 small mb-0 mt-3 d-flex align-items-center">
          <i class="fa-solid fa-circle-exclamation text-danger me-2 fs-5"></i>
          <div class="fw-bold">请确认已评估市场风险，确定启动实盘？</div>
        </div>
      `;
      newBtnAction.className = 'btn btn-danger btn-sm';
      newBtnAction.innerHTML = '<i class="fa-solid fa-play me-1"></i>确认启动实盘';
    } else {
      titleEl.innerHTML = '<i class="fa-solid fa-play text-success"></i><span>启动自动交易（模拟）</span>';
      bodyEl.innerHTML = `
        <div class="text-center my-2">
          <span class="badge bg-secondary fs-6 px-3 py-1 mb-2">模拟交易 (PAPER)</span>
          <div class="text-secondary small mt-2">
            开启后<b>模拟机器人</b>将在后台自动监控行情并记录虚拟订单，不涉及真实资产（可与实盘同时运行，共享行情流）。
          </div>
        </div>
        <div class="alert alert-info py-2 px-3 small mb-0 mt-3 d-flex align-items-center">
          <i class="fa-solid fa-circle-info text-info me-2 fs-5"></i>
          <div>确定启动模拟自动交易监控吗？</div>
        </div>
      `;
      newBtnAction.className = 'btn btn-success btn-sm';
      newBtnAction.innerHTML = '<i class="fa-solid fa-play me-1"></i>确认启动模拟';
    }

    newBtnAction.onclick = async () => {
      botConfirmModalInstance.hide();
      await startBot(mode);
    };

    botConfirmModalInstance.show();
  }

  async function startBot(mode = 'paper') {
    const label = mode === 'live' ? '实盘' : '模拟';
    try {
      const res = await fetch(`/api/trading/start?mode=${encodeURIComponent(mode)}`, { method: 'POST' });
      if (res.ok) {
        await checkBotStatus();
        appendLog(`${label}交易监控已启动`, 'info');
      } else {
        const err = await res.json().catch(() => ({}));
        alert(`启动${label}失败: ` + (err.detail || '未知错误'));
      }
    } catch (err) {
      console.error(`启动${label}交易失败`, err);
    }
  }

  async function stopBot(mode = 'paper') {
    const label = mode === 'live' ? '实盘' : '模拟';
    try {
      const res = await fetch(`/api/trading/stop?mode=${encodeURIComponent(mode)}`, { method: 'POST' });
      if (res.ok) {
        await checkBotStatus();
        appendLog(`${label}交易监控已停止`, 'warning');
      } else {
        const err = await res.json().catch(() => ({}));
        alert(`停止${label}失败: ` + (err.detail || '未知错误'));
      }
    } catch (err) {
      console.error(`停止${label}交易失败`, err);
    }
  }

  async function simulateTrade(direction) {
    const priceInput = document.getElementById('simEntryPrice');
    let entryPrice = priceInput ? parseFloat(priceInput.value) : NaN;
    if (isNaN(entryPrice) || entryPrice <= 0) {
      entryPrice = state.currentPrice;
    }

    if (!entryPrice || entryPrice <= 0) {
      alert('请等待当前价格加载，或手动输入入场价格！');
      return;
    }

    const payload = {
      symbol: state.currentSymbol,
      direction: direction,
      entry_price: entryPrice
    };

    try {
      const res = await fetch('/api/simulate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (res.ok) {
        const trade = await res.json();
        appendLog(`[模拟开仓] ${direction.toUpperCase()} ${state.currentSymbol} 价格: ${entryPrice}`, 'success');
        await loadTrades();
      } else {
        const err = await res.json();
        alert('模拟开仓失败: ' + (err.detail || '未知错误'));
      }
    } catch (err) {
      console.error('模拟开仓请求失败', err);
    }
  }

  async function closeTrade(tradeId) {
    const trade = (state.trades || []).find(t => t.id === tradeId);
    const modeTip = trade && !trade.is_paper
      ? `确定要市价平仓这笔【实盘】订单 (ID: ${tradeId} / ${trade.symbol}) 吗？将立即向交易所下单平仓！`
      : `确定要平仓该笔交易 (ID: ${tradeId}) 吗？`;
    if (!confirm(modeTip)) return;
    try {
      const payload = {};
      if (trade) {
        const curPrice = (state.prices && state.prices[trade.symbol]) || (trade.symbol === state.currentSymbol ? state.currentPrice : null);
        if (curPrice && curPrice > 0) payload.exit_price = curPrice;
      }
      const res = await fetch(`/api/trades/${tradeId}/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        appendLog(`[手动平仓] 订单 #${tradeId} 平仓成功`, 'info');
        await loadTrades();
        await loadStats();
      } else {
        const err = await res.json().catch(() => ({}));
        alert('平仓失败: ' + (err.detail || `HTTP ${res.status}`));
      }
    } catch (err) {
      console.error('平仓失败', err);
      alert('平仓请求失败: ' + err.message);
    }
  }

  async function deleteTrade(tradeId, status) {
    const tip = status === 'open'
      ? `订单 #${tradeId} 仍在持仓中，删除将同步移除监控状态，确定删除吗？`
      : `确定要删除交易记录 #${tradeId} 吗？`;
    if (!confirm(tip)) return;
    try {
      const res = await fetch(`/api/trades/${tradeId}/delete`, { method: 'POST' });
      if (res.ok) {
        appendLog(`[记录管理] 已删除交易记录 #${tradeId}`, 'info');
        await loadTrades();
        await loadStats();
      } else {
        const err = await res.json().catch(() => ({}));
        alert('删除失败: ' + (err.detail || res.statusText));
      }
    } catch (err) {
      alert('删除请求失败: ' + err.message);
    }
  }

  async function clearAllTrades() {
    if (!confirm('确定要清空全部交易记录吗？该操作不可恢复！')) return;
    try {
      const res = await fetch('/api/trades/clear', { method: 'POST' });
      if (res.ok) {
        appendLog('[记录管理] 已清空全部交易记录', 'info');
        await loadTrades();
        await loadStats();
      } else {
        const err = await res.json().catch(() => ({}));
        alert('清空失败: ' + (err.detail || res.statusText));
      }
    } catch (err) {
      alert('清空请求失败: ' + err.message);
    }
  }

  function highlightTrade(tradeId) {
    if (typeof ChartManager !== 'undefined') {
      ChartManager.highlightTrade(tradeId);
      // Switch to Chart Tab
      const tabLink = document.querySelector('a[href="#tabChart"]');
      if (tabLink) {
        bootstrap.Tab.getOrCreateInstance(tabLink).show();
      }
    }
  }

  // ─── UI Rendering ──────────────────────────────────────────────────
  function updateBotUI(statusOrBool) {
    let paperRunning = state.paperRunning;
    let liveRunning = state.liveRunning;
    if (typeof statusOrBool === 'object' && statusOrBool !== null) {
      if ('paper_running' in statusOrBool) paperRunning = Boolean(statusOrBool.paper_running);
      if ('live_running' in statusOrBool)  liveRunning  = Boolean(statusOrBool.live_running);
      if (!('paper_running' in statusOrBool) && !('live_running' in statusOrBool)) {
        paperRunning = Boolean(statusOrBool.running);
      }
    } else {
      paperRunning = Boolean(statusOrBool);
    }
    const anyRunning = paperRunning || liveRunning;
    state.paperRunning = paperRunning;
    state.liveRunning = liveRunning;
    state.botRunning = anyRunning;

    const dot   = document.getElementById('statusDot');
    const label = document.getElementById('statusLabel');
    const paperBtn = document.getElementById('btnPaperToggle');
    const liveBtn  = document.getElementById('btnLiveToggle');

    if (dot) dot.className = 'status-dot ' + (anyRunning ? 'running' : 'stopped');
    if (label) {
      if (paperRunning && liveRunning) label.textContent = '模拟+实盘运行中';
      else if (liveRunning)            label.textContent = '实盘运行中';
      else if (paperRunning)           label.textContent = '模拟运行中';
      else                             label.textContent = '已停止';
    }

    if (paperBtn) {
      if (paperRunning) {
        paperBtn.className = 'btn btn-sm btn-danger d-inline-flex align-items-center';
        paperBtn.innerHTML = '<i class="fa-solid fa-stop me-1"></i>模拟';
        paperBtn.title = '模拟盘运行中，点击停止';
      } else {
        paperBtn.className = 'btn btn-sm btn-success d-inline-flex align-items-center';
        paperBtn.innerHTML = '<i class="fa-solid fa-play me-1"></i>模拟';
        paperBtn.title = '点击启动模拟自动交易';
      }
    }

    if (liveBtn) {
      if (liveRunning) {
        liveBtn.className = 'btn btn-sm btn-danger d-inline-flex align-items-center';
        liveBtn.innerHTML = '<i class="fa-solid fa-stop me-1"></i>实盘';
        liveBtn.title = '实盘运行中，点击停止';
      } else {
        liveBtn.className = 'btn btn-sm btn-outline-danger d-inline-flex align-items-center';
        liveBtn.innerHTML = '<i class="fa-solid fa-play me-1"></i>实盘';
        liveBtn.title = '点击启动实盘自动交易';
      }
    }
  }

  function updateCurrentPrice(price) {
    if (!price) return;
    state.currentPrice = price;

    const el = document.getElementById('currentPrice');
    if (el) {
      el.textContent = price.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 2 });
    }

    const simInput = document.getElementById('simEntryPrice');
    if (simInput && !simInput.value) {
      simInput.placeholder = price.toFixed(2);
    }
  }

  function resetSignalDisplay() {
    const info = document.getElementById('signalInfo');
    const wrap = document.getElementById('signalProgressWrap');
    const bar  = document.getElementById('signalProgressBar');
    if (info) info.textContent = '无信号';
    if (wrap) wrap.style.setProperty('display', 'none', 'important');
    if (bar)  bar.style.width = '0%';
  }

  function renderTradesView() {
    const allTrades = state.trades || [];
    const paperTrades = allTrades.filter(t => Boolean(t.is_paper));
    const liveTrades = allTrades.filter(t => !t.is_paper);

    const pBadge = document.getElementById('paperTradeBadge');
    const lBadge = document.getElementById('liveTradeBadge');
    if (pBadge) pBadge.textContent = paperTrades.length;
    if (lBadge) lBadge.textContent = liveTrades.length;

    const filtered = state.tradesMode === 'live' ? liveTrades : paperTrades;
    const countEl = document.getElementById('tradeCount');
    if (countEl) countEl.textContent = filtered.length;

    const totalPages = Math.max(1, Math.ceil(filtered.length / state.tradesPageSize));
    state.tradesPage = Math.max(1, Math.min(state.tradesPage || 1, totalPages));
    const startIdx = (state.tradesPage - 1) * state.tradesPageSize;
    const pageTrades = filtered.slice(startIdx, startIdx + state.tradesPageSize);

    renderTradesTable(pageTrades);
    if (typeof window.renderTablePagination === 'function') {
      window.renderTablePagination({
        infoEl: document.getElementById('tradesPageInfo'),
        ulEl: document.getElementById('tradesPagination'),
        page: state.tradesPage,
        totalPages,
        pageSize: state.tradesPageSize,
        total: filtered.length,
        onPageChange: (p) => {
          state.tradesPage = p;
          renderTradesView();
        }
      });
    }
    updateTradesEquitySymbols(filtered);
    renderTradesEquityCurve(filtered);
  }

  function updateTradesEquitySymbols(trades) {
    const sel = document.getElementById('tradesEquitySymbol');
    if (!sel) return;
    const prev = sel.value;
    const symSet = new Set();
    (state.symbols || []).forEach(s => { if (s.symbol) symSet.add(s.symbol); });
    (trades || []).forEach(t => { if (t.symbol) symSet.add(t.symbol); });

    const opts = ['<option value="ALL">全部币种</option>'];
    Array.from(symSet).sort().forEach(sym => {
      const safe = _escapeHtml(sym);
      opts.push(`<option value="${safe}">${safe}</option>`);
    });
    sel.innerHTML = opts.join('');

    if (prev && (prev === 'ALL' || symSet.has(prev))) {
      sel.value = prev;
    } else if (symSet.size > 0) {
      // Default to first available symbol (or BTCUSDT if present)
      sel.value = symSet.has('BTCUSDT') ? 'BTCUSDT' : Array.from(symSet).sort()[0];
    } else {
      sel.value = 'ALL';
    }
  }

  function renderTradesEquityCurve(trades) {
    if (typeof TradesEquityChart === 'undefined') return;
    const sel = document.getElementById('tradesEquitySymbol');
    const summaryEl = document.getElementById('tradesEquitySummary');
    const selectedSymbol = sel ? sel.value : 'ALL';

    const closed = (trades || [])
      .filter(t => t.status === 'closed' && (selectedSymbol === 'ALL' || t.symbol === selectedSymbol))
      .slice()
      .sort((a, b) => new Date(a.close_time || a.open_time) - new Date(b.close_time || b.open_time));

    if (closed.length === 0) {
      if (summaryEl) summaryEl.textContent = '暂无已平仓订单';
      TradesEquityChart.render([], [], []);
      return;
    }

    const baseCapital = state.tradesMode === 'live' ? 0 : 10000;
    let equity = baseCapital;
    const dates = ['起始'];
    const equities = [baseCapital];
    const pnls = [null];

    closed.forEach(t => {
      const p = Number(t.pnl || 0);
      equity += p;
      const tsStr = _fmtTradeTime(t.close_time || t.open_time) || `#${t.id}`;
      dates.push(`#${t.id} ${tsStr}`);
      equities.push(Number(equity.toFixed(2)));
      pnls.push(p);
    });

    const totalPnl = equity - baseCapital;
    const sign = totalPnl >= 0 ? '+' : '';
    const colorClass = totalPnl >= 0 ? 'text-success' : 'text-danger';
    if (summaryEl) {
      if (state.tradesMode === 'live') {
        summaryEl.innerHTML = `实盘已平仓 ${closed.length} 笔 · 累计净盈亏 <span class="${colorClass} fw-bold">${sign}$${totalPnl.toFixed(2)}</span>`;
      } else {
        summaryEl.innerHTML = `模拟已平仓 ${closed.length} 笔 · 累计盈亏 <span class="${colorClass} fw-bold">${sign}$${totalPnl.toFixed(2)}</span> · 当前权益 <b>$${equity.toFixed(2)}</b>`;
      }
    }

    TradesEquityChart.render(dates, equities, pnls);
  }

  function _fmtLogTime(isoStr) {
    if (!isoStr) return '—';
    const s = String(isoStr).trim();
    const normalized = (s.includes('Z') || s.includes('+')) ? s : (s.replace(' ', 'T') + 'Z');
    const d = new Date(normalized);
    if (isNaN(d.getTime())) return s.replace('T', ' ').slice(5, 19);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function _escapeHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function loadOperationLogs() {
    try {
      let url = `/api/logs?page=${state.logsPage}&page_size=${state.logsPageSize}`;
      if (state.logsTypeFilter) {
        url += `&log_type=${encodeURIComponent(state.logsTypeFilter)}`;
      }
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const items = Array.isArray(data?.items) ? data.items : (Array.isArray(data) ? data : []);
      state.logs = items;
      state.logsTotal = data?.total ?? items.length;
      state.logsPage = data?.page ?? state.logsPage;
      state.logsTotalPages = data?.total_pages ?? 1;
      renderOperationLogs(state.logs);
      renderLogsPagination();
    } catch (err) {
      console.warn('[App] Load operation logs error:', err);
    }
  }

  function goToLogsPage(page) {
    const target = Math.max(1, Math.min(page, state.logsTotalPages || 1));
    if (target === state.logsPage) return;
    state.logsPage = target;
    loadOperationLogs();
  }

  function renderLogsPagination() {
    if (typeof window.renderTablePagination === 'function') {
      window.renderTablePagination({
        infoEl: document.getElementById('logsPageInfo'),
        ulEl: document.getElementById('logsPagination'),
        page: state.logsPage || 1,
        totalPages: state.logsTotalPages || 1,
        pageSize: state.logsPageSize,
        total: state.logsTotal ?? 0,
        onPageChange: goToLogsPage
      });
    }
  }

  function renderOperationLogs(logs) {
    const tbody = document.getElementById('logsTableBody');
    const countEl = document.getElementById('logCount');
    if (countEl) countEl.textContent = `共 ${state.logsTotal ?? (logs || []).length} 条`;
    if (!tbody) return;

    if (!logs || logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-secondary py-4">暂无操作日志</td></tr>';
      return;
    }

    const typeMeta = {
      open:  { label: '开仓',     cls: 'bg-primary-subtle text-primary border border-primary-subtle' },
      close: { label: '平仓',     cls: 'bg-success-subtle text-success border border-success-subtle' },
      start: { label: '系统启动', cls: 'bg-info-subtle text-info border border-info-subtle' },
      stop:  { label: '系统停止', cls: 'bg-warning-subtle text-warning border border-warning-subtle' }
    };

    tbody.innerHTML = logs.map(log => {
      const meta = typeMeta[log.log_type] || { label: log.log_type || '其他', cls: 'bg-secondary-subtle text-secondary' };
      const modeBadge = log.mode === 'live'
        ? '<span class="badge bg-danger-subtle text-danger">实盘</span>'
        : (log.mode === 'paper'
            ? '<span class="badge bg-secondary-subtle text-secondary">模拟</span>'
            : '<span class="text-secondary">—</span>');
      const symText = log.symbol ? `<span class="fw-semibold">${_escapeHtml(log.symbol)}</span>` : '<span class="text-secondary">—</span>';
      return `
        <tr>
          <td class="ps-3 py-3 text-secondary">#${log.id}</td>
          <td class="py-3 text-secondary font-monospace small">${_fmtLogTime(log.created_at)}</td>
          <td class="py-3"><span class="badge ${meta.cls}">${meta.label}</span></td>
          <td class="py-3">${modeBadge}</td>
          <td class="py-3">${symText}</td>
          <td class="py-3 fw-medium">${_escapeHtml(log.title)}</td>
          <td class="pe-3 py-3 text-secondary small">${_escapeHtml(log.message)}</td>
        </tr>
      `;
    }).join('');
  }

  async function clearOperationLogs() {
    if (!confirm('确定清空所有操作日志吗？此操作不可撤销。')) return;
    try {
      const res = await fetch('/api/logs/clear', { method: 'POST' });
      if (res.ok) {
        state.logsPage = 1;
        loadOperationLogs();
      }
    } catch (err) {
      console.error('[App] Clear logs error:', err);
    }
  }

  function _fmtTradeTime(isoStr) {
    if (!isoStr) return '—';
    const s = String(isoStr).trim();
    // Backend stores UTC timestamps (sometimes naive); ensure 'Z' so browser converts to local time
    const normalized = (s.includes('Z') || s.includes('+')) ? s : (s.replace(' ', 'T') + 'Z');
    const d = new Date(normalized);
    if (isNaN(d.getTime())) return s.replace('T', ' ').slice(5, 16);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function renderTradesTable(trades) {
    const tbody = document.getElementById('tradesTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!trades || trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="14" class="text-center text-secondary py-4">暂无交易记录</td></tr>';
      return;
    }

    trades.forEach(t => {
      const tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', (e) => {
        // avoid trigger when clicking action buttons
        if (e.target.tagName === 'BUTTON' || e.target.closest('button')) return;
        highlightTrade(t.id);
      });

      const isLong = (t.direction || '').toLowerCase() === 'long';
      const dirBadge = isLong
        ? '<span class="badge bg-success-subtle text-success border border-success-subtle">多</span>'
        : '<span class="badge bg-danger-subtle text-danger border border-danger-subtle">空</span>';

      let statusBadge = '<span class="badge bg-secondary">未知</span>';
      if (t.status === 'open') {
        statusBadge = '<span class="badge bg-primary">持仓中</span>';
      } else if (t.status === 'closed') {
        statusBadge = '<span class="badge bg-success">已平仓</span>';
      } else if (t.status === 'stopped') {
        statusBadge = '<span class="badge bg-danger">止损平仓</span>';
      }

      let pnlText = '—';
      if (t.status === 'open') {
        const curPrice = (state.prices && state.prices[t.symbol]) || (t.symbol === state.currentSymbol ? state.currentPrice : null);
        if (curPrice && t.entry_price && t.quantity) {
          const isLong = (t.direction || '').toLowerCase() === 'long';
          const diff = isLong ? (curPrice - t.entry_price) : (t.entry_price - curPrice);
          const unrealizedPnl = diff * t.quantity;
          const unrealizedPct = (unrealizedPnl / (t.entry_price * t.quantity)) * 100;
          const sign = unrealizedPnl >= 0 ? '+' : '';
          const color = unrealizedPnl >= 0 ? 'text-success' : 'text-danger';
          pnlText = `<span class="${color} fw-bold open-pnl-cell" data-trade-id="${t.id}" data-symbol="${t.symbol}" data-dir="${t.direction}" data-entry="${t.entry_price}" data-qty="${t.quantity}">${sign}$${unrealizedPnl.toFixed(2)} (${sign}${unrealizedPct.toFixed(1)}%)</span> <span class="badge bg-primary-subtle text-primary border border-primary-subtle ms-1" style="font-size:10px;">持仓浮动</span>`;
        } else {
          pnlText = `<span class="text-secondary open-pnl-cell" data-trade-id="${t.id}" data-symbol="${t.symbol}" data-dir="${t.direction}" data-entry="${t.entry_price}" data-qty="${t.quantity}">浮动中...</span> <span class="badge bg-primary-subtle text-primary border border-primary-subtle ms-1" style="font-size:10px;">持仓浮动</span>`;
        }
      } else if (t.pnl !== null && t.pnl !== undefined) {
        const sign = t.pnl >= 0 ? '+' : '';
        const color = t.pnl >= 0 ? 'text-success' : 'text-danger';
        const pct = t.pnl_pct !== null && t.pnl_pct !== undefined ? ` (${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%)` : '';
        pnlText = `<span class="${color} fw-bold">${sign}$${t.pnl.toFixed(2)}${pct}</span>`;
      }

      const typeBadge = t.is_paper
        ? '<span class="badge el-tag-paper" title="Paper Trading">模拟</span>'
        : '<span class="badge bg-danger-subtle text-danger border border-danger-subtle" title="Live Trading">实盘</span>';

      const reasonMap = {
        stop_loss:        '<span class="badge bg-danger-subtle text-danger border border-danger-subtle">初始止损</span>',
        profit_lock_stop: '<span class="badge bg-success-subtle text-success border border-success-subtle">阶梯锁盈</span>',
        trailing_stop:    '<span class="badge bg-success-subtle text-success border border-success-subtle">移动止损</span>',
        ma7_trailing:     '<span class="badge bg-success-subtle text-success border border-success-subtle">MA7 止盈</span>',
        ma60_cross:       '<span class="badge bg-warning-subtle text-warning border border-warning-subtle">MA60 穿线</span>',
        take_profit:      '<span class="badge bg-success-subtle text-success border border-success-subtle">目标止盈</span>',
        exchange_closed:  '<span class="badge bg-warning-subtle text-warning border border-warning-subtle">交易所平仓</span>',
        manual:           '<span class="badge el-tag-paper">手动平仓</span>',
        time_stop:        '<span class="badge el-tag-paper">超时平仓</span>',
      };
      const reasonBadge = t.status === 'open'
        ? '<span class="text-secondary">—</span>'
        : (reasonMap[t.close_reason] || (t.close_reason ? `<span class="badge el-tag-paper">${_escapeHtml(t.close_reason)}</span>` : '<span class="text-secondary">—</span>'));

      const openTimeStr = _fmtTradeTime(t.open_time);
      const closeTimeStr = t.status === 'open' ? '—' : _fmtTradeTime(t.close_time);
      const safeSym = _escapeHtml(t.symbol);
      const safeStatus = _escapeHtml(t.status);
      const idNum = Number(t.id) || 0;

      // Action column
      let actionBtn = `
        <button class="btn btn-xs btn-outline-secondary" onclick="AppController.highlightTrade(${idNum})" title="定位到图表">
          <i class="fa-solid fa-crosshairs"></i>
        </button>
        <button class="btn btn-xs btn-outline-danger ms-1" onclick="AppController.deleteTrade(${idNum}, '${safeStatus}')" title="删除记录">
          <i class="fa-solid fa-trash-can"></i>
        </button>
      `;
      if (t.status === 'open') {
        actionBtn = `
          <button class="btn btn-xs btn-danger me-1" onclick="AppController.closeTrade(${idNum})" title="平仓">
            平仓
          </button>
          ${actionBtn}
        `;
      }

      tr.innerHTML = `
        <td><span class="text-secondary small">#${idNum}</span></td>
        <td><strong>${safeSym}</strong></td>
        <td>${dirBadge}</td>
        <td>${t.entry_price?.toFixed(2) ?? '—'}</td>
        <td>${t.exit_price ? t.exit_price.toFixed(2) : '—'}</td>
        <td class="text-secondary small">${t.stop_loss?.toFixed(2) ?? '—'}</td>
        <td class="text-secondary small">${t.take_profit?.toFixed(2) ?? '—'}</td>
        <td>${pnlText}</td>
        <td>${statusBadge}</td>
        <td class="small text-secondary">${openTimeStr}</td>
        <td class="small text-secondary">${closeTimeStr}</td>
        <td>${reasonBadge}</td>
        <td>${typeBadge}</td>
        <td class="text-end text-nowrap">${actionBtn}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function updateOpenTradesPnl(symbol, price) {
    if (!symbol || !price) return;
    const cells = document.querySelectorAll(`.open-pnl-cell[data-symbol="${symbol}"]`);
    cells.forEach(el => {
      const entry = parseFloat(el.getAttribute('data-entry'));
      const qty = parseFloat(el.getAttribute('data-qty'));
      const dir = (el.getAttribute('data-dir') || '').toLowerCase();
      if (!isNaN(entry) && !isNaN(qty) && entry > 0 && qty > 0) {
        const isLong = dir === 'long';
        const diff = isLong ? (price - entry) : (entry - price);
        const unrealizedPnl = diff * qty;
        const unrealizedPct = (unrealizedPnl / (entry * qty)) * 100;
        const sign = unrealizedPnl >= 0 ? '+' : '';
        const color = unrealizedPnl >= 0 ? 'text-success' : 'text-danger';
        el.className = `${color} fw-bold open-pnl-cell`;
        el.textContent = `${sign}$${unrealizedPnl.toFixed(2)} (${sign}${unrealizedPct.toFixed(1)}%)`;
      }
    });
  }

  function appendLog(msg, level = 'info') {
    const box = document.getElementById('logBox');
    if (!box) return;
    const time = new Date().toTimeString().split(' ')[0];
    const line = document.createElement('div');
    line.className = 'log-line ' + String(level || 'info').replace(/[^a-z0-9_-]/gi, '');
    line.innerHTML = `<span class="text-secondary">[${time}]</span> ${_escapeHtml(msg)}`;
    box.appendChild(line);

    // Keep max 100 lines
    while (box.children.length > 100) {
      box.removeChild(box.firstChild);
    }
    box.scrollTop = box.scrollHeight;
  }

  // ─── Binance Official Realtime Kline WebSocket ─────────────────────
  let binanceWs = null;
  let currentBinanceStream = null;
  let binanceReconnectTimer = null;

  function connectBinanceStream(symbol, timeframe) {
    if (!symbol) return;
    const cleanSym = symbol.replace('/', '').replace(':USDT', '').toLowerCase();
    const streamName = `${cleanSym}@kline_${timeframe || '1m'}`;

    if (binanceWs && currentBinanceStream === streamName && binanceWs.readyState === WebSocket.OPEN) {
      return;
    }

    if (binanceWs) {
      try {
        binanceWs.onclose = null;
        binanceWs.onerror = null;
        binanceWs.close();
      } catch (_) {}
      binanceWs = null;
    }
    clearTimeout(binanceReconnectTimer);

    currentBinanceStream = streamName;
    console.log(`[Binance WS] Connecting real-time stream: ${streamName}`);

    // Try standard stream or fallback fstream
    const wsUrl = `wss://stream.binance.com:9443/ws/${streamName}`;
    try {
      binanceWs = new WebSocket(wsUrl);

      binanceWs.onopen = () => {
        console.log(`[Binance WS] ✅ Connected to official live stream: ${streamName}`);
      };

      binanceWs.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg && msg.e === 'kline' && msg.k) {
            const k = msg.k;
            const candle = {
              time: k.t,
              open: parseFloat(k.o),
              high: parseFloat(k.h),
              low: parseFloat(k.l),
              close: parseFloat(k.c),
              volume: parseFloat(k.v),
              isClosed: k.x,
            };

            // 1. Update current price display immediately
            updateCurrentPrice(candle.close);

            // 2. Update sidebar price if matching
            if (typeof SettingsManager !== 'undefined' && SettingsManager.updateSymbolPrice) {
              SettingsManager.updateSymbolPrice(state.currentSymbol, candle.close);
            }

            // 3. Update indicators (MA7 / MA25 / MA60 / Direction badge)
            updateIndicatorsFromLastBar(candle.close);

            // 4. Update live chart bar incrementally
            if (typeof ChartManager !== 'undefined') {
              ChartManager.appendCandle(candle);
            }
          }
        } catch (err) {
          console.warn('[Binance WS] Parse error:', err);
        }
      };

      binanceWs.onerror = (err) => {
        console.warn(`[Binance WS] Stream error (${streamName}), trying fstream...`, err);
        try {
          binanceWs.close();
        } catch (_) {}
      };

      binanceWs.onclose = () => {
        console.log(`[Binance WS] Stream closed (${streamName}). Reconnecting in 3s...`);
        clearTimeout(binanceReconnectTimer);
        binanceReconnectTimer = setTimeout(() => {
          connectBinanceStream(state.currentSymbol, state.currentTimeframe);
        }, 3000);
      };
    } catch (e) {
      console.warn('[Binance WS] Connection failed:', e);
    }
  }

  function updateIndicatorsFromLastBar(liveClose) {
    if (!state.klinesData || state.klinesData.length < 7) return;
    const closes = state.klinesData.map(k => Array.isArray(k) ? +k[4] : +(k.close ?? k.c ?? 0));
    if (liveClose) closes[closes.length - 1] = liveClose;
    const n = closes.length;
    const m7  = closes.slice(-7).reduce((a, b) => a + b, 0) / 7;
    const m25 = n >= 25 ? (closes.slice(-25).reduce((a, b) => a + b, 0) / 25) : null;
    const m60 = n >= 60 ? (closes.slice(-60).reduce((a, b) => a + b, 0) / 60) : null;

    const el7  = document.getElementById('ma7Val');
    const el25 = document.getElementById('ma25Val');
    const el60 = document.getElementById('ma60Val');
    const dir  = document.getElementById('maDirection');

    if (el7 && m7 != null)   el7.textContent  = m7.toFixed(1);
    if (el25 && m25 != null) el25.textContent = m25.toFixed(1);
    if (el60 && m60 != null) el60.textContent = m60.toFixed(1);

    if (dir && m7 != null && m25 != null && m60 != null) {
      if (m7 > m25 && m25 > m60) {
        dir.textContent = '▲ 多头排列';
        dir.className = 'direction-badge long';
      } else if (m7 < m25 && m25 < m60) {
        dir.textContent = '▼ 空头排列';
        dir.className = 'direction-badge short';
      } else {
        dir.textContent = '— 震荡/整理';
        dir.className = 'direction-badge flat';
      }
    }
  }

  // ─── WebSocket ─────────────────────────────────────────────────────
  function connectWS() {
    if (state.ws) {
      try { state.ws.close(); } catch (_) {}
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = (typeof AuthManager !== 'undefined' && AuthManager.getToken) ? AuthManager.getToken() : '';
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    const badge = document.getElementById('wsBadge');

    try {
      const ws = new WebSocket(wsUrl);
      state.ws = ws;

      ws.onopen = () => {
        // Send token in the first JSON frame instead of URL query string
        ws.send(JSON.stringify({ type: 'auth', token }));
        console.log('[WS] Connected to MA Trader stream');
        if (badge) {
          badge.className = 'badge bg-success-subtle text-success border border-success-subtle connected';
          badge.textContent = '已连接';
        }

        // Heartbeat ping
        clearInterval(state.wsPingInterval);
        state.wsPingInterval = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, 20000);
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          handleWSMessage(msg);
        } catch (err) {
          console.warn('[WS] Parse message error:', err, event.data);
        }
      };

      ws.onerror = (err) => {
        console.warn('[WS] Connection error:', err);
      };

      ws.onclose = () => {
        console.log('[WS] Disconnected. Reconnecting in 3s...');
        if (badge) {
          badge.className = 'badge bg-warning-subtle text-warning border border-warning-subtle';
          badge.textContent = '重连中…';
        }
        clearInterval(state.wsPingInterval);
        clearTimeout(state.wsReconnectTimeout);
        state.wsReconnectTimeout = setTimeout(connectWS, 3000);
      };
    } catch (e) {
      console.error('[WS] Setup failed:', e);
      clearTimeout(state.wsReconnectTimeout);
      state.wsReconnectTimeout = setTimeout(connectWS, 5000);
    }
  }

  function handleWSMessage(msg) {
    if (!msg || !msg.type) return;

    switch (msg.type) {
      case 'ticker': {
        state.prices = state.prices || {};
        state.prices[msg.symbol] = msg.price;
        // Update sidebar
        if (typeof SettingsManager !== 'undefined' && SettingsManager.updateSymbolPrice) {
          SettingsManager.updateSymbolPrice(msg.symbol, msg.price);
        }
        // If current symbol, update price
        if (msg.symbol === state.currentSymbol) {
          updateCurrentPrice(msg.price);
        }
        // Real-time floating PnL update for open trades
        updateOpenTradesPnl(msg.symbol, msg.price);
        break;
      }

      case 'ma_update': {
        if (typeof SettingsManager !== 'undefined' && SettingsManager.updateSymbolDirection) {
          SettingsManager.updateSymbolDirection(msg.symbol, msg.direction);
        }
        if (msg.symbol === state.currentSymbol) {
          const m7  = document.getElementById('ma7Val');
          const m25 = document.getElementById('ma25Val');
          const m60 = document.getElementById('ma60Val');
          const dir = document.getElementById('maDirection');

          if (m7)  m7.textContent  = msg.ma7?.toFixed(1)  ?? '—';
          if (m25) m25.textContent = msg.ma25?.toFixed(1) ?? '—';
          if (m60) m60.textContent = msg.ma60?.toFixed(1) ?? '—';

          if (dir) {
            if (msg.direction === 'long_aligned') {
              dir.textContent = '▲ 多头排列';
              dir.className = 'direction-badge long';
            } else if (msg.direction === 'short_aligned') {
              dir.textContent = '▼ 空头排列';
              dir.className = 'direction-badge short';
            } else {
              dir.textContent = '— 震荡/无方向';
              dir.className = 'direction-badge flat';
            }
          }
        }
        break;
      }

      case 'signal': {
        if (msg.symbol === state.currentSymbol) {
          const info = document.getElementById('signalInfo');
          const wrap = document.getElementById('signalProgressWrap');
          const bar  = document.getElementById('signalProgressBar');

          const isLong = msg.direction === 'long';
          const dirText = isLong ? '多头信号' : '空头信号';
          const maxConf = msg.max_confirm || 2;
          const pct = Math.min(100, Math.round((msg.confirm / maxConf) * 100));

          if (info) {
            info.innerHTML = `<span class="${isLong ? 'text-success' : 'text-danger'} fw-bold">${dirText}</span> 确认 ${msg.confirm}/${maxConf}`;
          }
          if (wrap) wrap.style.setProperty('display', 'flex', 'important');
          if (bar) {
            bar.style.width = `${pct}%`;
            bar.className = 'progress-bar signal-progress-bar ' + (isLong ? 'bg-success' : 'bg-danger');
          }
        }
        appendLog(`[信号] ${msg.symbol} ${msg.direction.toUpperCase()} 确认 ${msg.confirm}/${msg.max_confirm}`, 'info');
        break;
      }

      case 'signal_reset': {
        if (msg.symbol === state.currentSymbol) {
          resetSignalDisplay();
        }
        break;
      }

      case 'trade_opened': {
        const trade = msg.trade;
        appendLog(`[开仓通知] ${trade.direction.toUpperCase()} ${trade.symbol} 价格:${trade.entry_price} 止损:${trade.stop_loss} 止盈:${trade.take_profit}`, 'success');
        loadTrades();
        break;
      }

      case 'trade_closed': {
        const trade = msg.trade;
        const pnl = trade.pnl ? `${trade.pnl >= 0 ? '+' : ''}$${trade.pnl.toFixed(2)}` : '';
        appendLog(`[平仓通知] ${trade.symbol} (${trade.close_reason || '平仓'}) 盈亏:${pnl}`, trade.pnl >= 0 ? 'success' : 'danger');
        loadTrades();
        loadStats();
        break;
      }

      case 'bot_status': {
        updateBotUI(msg);
        break;
      }

      case 'operation_log': {
        const tabLogsPane = document.getElementById('tabLogs');
        if (tabLogsPane && tabLogsPane.classList.contains('active')) {
          loadOperationLogs();
        }
        break;
      }

      case 'log': {
        appendLog(msg.message || '', msg.level || 'info');
        break;
      }
    }
  }

  // ─── Public API ────────────────────────────────────────────────────
  return {
    init,
    switchSymbol,
    switchTimeframe,
    startBot,
    stopBot,
    simulateTrade,
    closeTrade,
    deleteTrade,
    clearAllTrades,
    highlightTrade,
    loadTrades,
    loadStats,
    loadSymbols
  };
})();

// Shared Element-style pagination renderer for Logs / Trades / Backtest History
window.renderTablePagination = function ({ infoEl, ulEl, page, totalPages, pageSize, total, onPageChange }) {
  const curPage = Math.max(1, page || 1);
  const maxPage = Math.max(1, totalPages || 1);
  const count = total ?? 0;

  if (infoEl) {
    infoEl.textContent = `第 ${curPage} / ${maxPage} 页 · 共 ${count} 条 · 每页 ${pageSize} 条`;
  }
  if (!ulEl) return;

  const buildItem = (label, targetPage, disabled, active) => {
    const cls = `el-page-btn${active ? ' active' : ''}`;
    if (disabled || active) {
      return `<li><button type="button" class="${cls}" ${disabled ? 'disabled' : ''}>${label}</button></li>`;
    }
    return `<li><button type="button" class="${cls}" data-page="${targetPage}">${label}</button></li>`;
  };

  const parts = [];
  parts.push(buildItem('上一页', curPage - 1, curPage <= 1, false));

  const pages = [];
  for (let p = 1; p <= maxPage; p++) {
    if (p === 1 || p === maxPage || Math.abs(p - curPage) <= 2) {
      pages.push(p);
    } else if (pages[pages.length - 1] !== '...') {
      pages.push('...');
    }
  }
  for (const p of pages) {
    if (p === '...') {
      parts.push('<li><span class="el-page-ellipsis">…</span></li>');
    } else {
      parts.push(buildItem(String(p), p, false, p === curPage));
    }
  }

  parts.push(buildItem('下一页', curPage + 1, curPage >= maxPage, false));
  ulEl.innerHTML = parts.join('');
  ulEl.querySelectorAll('button[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = parseInt(btn.getAttribute('data-page'), 10);
      if (!isNaN(target) && typeof onPageChange === 'function') {
        onPageChange(target);
      }
    });
  });
};

// Auto-run on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  AppController.init();
});
