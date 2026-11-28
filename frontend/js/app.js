/**
 * app.js — MA 自动交易系统主控制器 (AppController)
 * 协调图表、设置、回测、统计、实时行情与 WebSocket 通信
 */

const AppController = (() => {
  const IS_DEMO = window.location.pathname.startsWith('/demo');
  const APP_ENV = IS_DEMO ? 'paper' : 'live';

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
    paperModes: { balanced: false, breakout: false, classic: false, ribbon: false },
    paperModalOpen: false,
    ws: null,
    wsPingInterval: null,
    wsReconnectTimeout: null,
    klinesData: [],
    isLoadingHistory: false,
    hasMoreHistory: true,
    tradingMode: APP_ENV,
    tradesMode: APP_ENV,
    tradesStrategies: ['balanced', 'breakout', 'ribbon', 'classic'],
    statsStrategies: ['balanced', 'breakout', 'ribbon', 'classic'],
    archives: [],
    activeArchiveDetail: null,
    archiveDetailStrategies: ['balanced', 'breakout', 'ribbon', 'classic'],
    tradesPage: 1,
    tradesPageSize: 20,
    logs: [],
    logsTypeFilter: '',
    logsPage: 1,
    logsPageSize: 20,
    logsTotal: 0,
    logsTotalPages: 1,
    sysLogs: [],
    sysLogsLevelFilter: '',
    sysLogsKeyword: '',
    sysLogsPage: 1,
    sysLogsPageSize: 20,
    sysLogsTotal: 0,
    sysLogsTotalPages: 1
  };

  // ─── Initialization ────────────────────────────────────────────────
  async function init() {
    console.log('[App] Initializing MA Trader App...');

    // 0. Setup Theme (Day / Night mode)
    setupTheme();

    // 0.02 Apply single-environment layout (/ = live, /demo = paper)
    applyEnvironmentLayout();

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
    try { await loadArchivesList(); } catch (e) { console.warn(e); }
    try { await loadStats(); } catch (e) { console.warn(e); }
    try { await hydrateHomepageLogBox(); } catch (e) { console.warn(e); }

    // Ensure chart is loaded and resized once initial flex layout settles
    if (!state.klinesData || state.klinesData.length === 0) {
      try { await loadKlines(); } catch (e) { console.warn(e); }
    }
    if (typeof ChartManager !== 'undefined' && ChartManager.resize) {
      requestAnimationFrame(() => ChartManager.resize());
      setTimeout(() => ChartManager.resize(), 120);
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
    _bindStrategyToggleGroup('statsStrategyBtns', () => state.statsStrategies, (next) => {
      state.statsStrategies = next;
      loadStats();
    });

    // Tab switch triggers
    const tabChartLink = document.querySelector('a[href="#tabChart"]');
    if (tabChartLink) {
      tabChartLink.addEventListener('shown.bs.tab', () => {
        if (typeof ChartManager !== 'undefined' && ChartManager.resize) {
          ChartManager.resize();
          setTimeout(() => ChartManager.resize(), 60);
        }
      });
    }
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
    _bindStrategyToggleGroup('tradesStrategyBtns', () => state.tradesStrategies, (next) => {
      state.tradesStrategies = next;
      state.tradesPage = 1;
      renderTradesView();
    });
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

    // System Logs tab & filters
    const tabSysLogsLink = document.querySelector('a[href="#tabSystemLogs"]');
    if (tabSysLogsLink) {
      tabSysLogsLink.addEventListener('shown.bs.tab', loadSystemLogs);
    }
    const sysLevelWrap = document.getElementById('sysLogsLevelFilter');
    if (sysLevelWrap) {
      sysLevelWrap.querySelectorAll('button[data-sys-level]').forEach(btn => {
        btn.addEventListener('click', () => {
          state.sysLogsLevelFilter = btn.getAttribute('data-sys-level') || '';
          state.sysLogsPage = 1;
          sysLevelWrap.querySelectorAll('button[data-sys-level]').forEach(b => {
            b.className = (b === btn) ? 'btn btn-primary active' : 'btn btn-outline-secondary';
          });
          loadSystemLogs();
        });
      });
    }
    const sysKwInput = document.getElementById('sysLogKeyword');
    let sysKwTimer = null;
    if (sysKwInput) {
      sysKwInput.addEventListener('input', () => {
        clearTimeout(sysKwTimer);
        sysKwTimer = setTimeout(() => {
          state.sysLogsKeyword = sysKwInput.value.trim();
          state.sysLogsPage = 1;
          loadSystemLogs();
        }, 300);
      });
      sysKwInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          clearTimeout(sysKwTimer);
          state.sysLogsKeyword = sysKwInput.value.trim();
          state.sysLogsPage = 1;
          loadSystemLogs();
        }
      });
    }
    const btnSearchSysLogs = document.getElementById('btnSearchSysLogs');
    if (btnSearchSysLogs) {
      btnSearchSysLogs.addEventListener('click', () => {
        state.sysLogsKeyword = (document.getElementById('sysLogKeyword')?.value || '').trim();
        state.sysLogsPage = 1;
        loadSystemLogs();
      });
    }
    const btnResetSysLogKw = document.getElementById('btnResetSysLogKw');
    if (btnResetSysLogKw) {
      btnResetSysLogKw.addEventListener('click', () => {
        const input = document.getElementById('sysLogKeyword');
        if (input) input.value = '';
        state.sysLogsKeyword = '';
        state.sysLogsPage = 1;
        loadSystemLogs();
      });
    }
    const btnRefreshSysLogs = document.getElementById('btnRefreshSysLogs');
    if (btnRefreshSysLogs) btnRefreshSysLogs.addEventListener('click', loadSystemLogs);
    const btnClearSysLogs = document.getElementById('btnClearSysLogs');
    if (btnClearSysLogs) btnClearSysLogs.addEventListener('click', clearSystemLogs);

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

    // Clear & Archive trades buttons
    const btnClearTrades = document.getElementById('btnClearTrades');
    if (btnClearTrades) btnClearTrades.addEventListener('click', clearAllTrades);
    const btnArchiveTrades = document.getElementById('btnArchiveTrades');
    if (btnArchiveTrades) btnArchiveTrades.addEventListener('click', openArchiveConfirmModal);
    const btnConfirmArchiveAction = document.getElementById('btnConfirmArchiveAction');
    if (btnConfirmArchiveAction) btnConfirmArchiveAction.addEventListener('click', executeArchiveTrades);
    const btnViewArchives = document.getElementById('btnViewArchives');
    if (btnViewArchives) btnViewArchives.addEventListener('click', openArchivesModal);
    const btnTradesKline = document.getElementById('btnTradesKline');
    if (btnTradesKline) btnTradesKline.addEventListener('click', openTradesKlineModal);
    const btnArchiveDetailKline = document.getElementById('btnArchiveDetailKline');
    if (btnArchiveDetailKline) btnArchiveDetailKline.addEventListener('click', openArchiveKlineModal);
    _bindArchiveDetailStrategyBtns();
  }

  function _bindStrategyToggleGroup(containerId, getSelected, onUpdate) {
    const wrap = document.getElementById(containerId);
    if (!wrap) return;
    wrap.querySelectorAll('button[data-strategy]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.getAttribute('data-strategy');
        const cur = new Set(getSelected() || []);
        if (cur.has(key)) {
          cur.delete(key);
        } else {
          cur.add(key);
        }
        const next = ['balanced', 'breakout', 'ribbon', 'classic'].filter(k => cur.has(k));
        wrap.querySelectorAll('button[data-strategy]').forEach(b => {
          const bKey = b.getAttribute('data-strategy');
          const active = cur.has(bKey);
          b.className = active ? 'btn btn-sm btn-primary active' : 'btn btn-sm btn-outline-secondary';
          const icon = b.querySelector('.strat-check-icon');
          if (icon) icon.style.display = active ? 'inline-block' : 'none';
        });
        onUpdate(next);
      });
    });
  }

  function applyEnvironmentLayout() {
    document.title = IS_DEMO ? 'MA Trader — 模拟盘 (/demo)' : 'MA Trader — 实盘交易';

    const envBadge = document.getElementById('envBadge');
    if (envBadge) {
      envBadge.textContent = IS_DEMO ? '模拟盘' : '实盘';
      envBadge.className = IS_DEMO ? 'badge bg-primary' : 'badge bg-danger';
    }

    const switchBtn = document.getElementById('btnSwitchEnv');
    if (switchBtn) {
      if (IS_DEMO) {
        switchBtn.href = '/';
        switchBtn.className = 'btn btn-sm btn-outline-danger d-inline-flex align-items-center';
        switchBtn.title = '切换到实盘交易页面';
        switchBtn.innerHTML = '<i class="fa-solid fa-bolt me-1"></i><span class="d-none d-md-inline">前往实盘</span>';
      } else {
        switchBtn.href = '/demo';
        switchBtn.className = 'btn btn-sm btn-outline-primary d-inline-flex align-items-center';
        switchBtn.title = '切换到模拟盘演练页面';
        switchBtn.innerHTML = '<i class="fa-solid fa-flask me-1"></i><span class="d-none d-md-inline">前往模拟盘</span>';
      }
    }

    const paperToggle = document.getElementById('btnPaperToggle');
    const liveToggle  = document.getElementById('btnLiveToggle');
    if (paperToggle) {
      paperToggle.classList.toggle('d-none', !IS_DEMO);
      paperToggle.classList.toggle('d-inline-flex', IS_DEMO);
      paperToggle.hidden = !IS_DEMO;
    }
    if (liveToggle) {
      liveToggle.classList.toggle('d-none', IS_DEMO);
      liveToggle.classList.toggle('d-inline-flex', !IS_DEMO);
      liveToggle.hidden = IS_DEMO;
    }

    // Backtest Tab is only shown in /demo
    const navBt = document.getElementById('navBacktestItem');
    const tabBt = document.getElementById('tabBacktest');
    if (navBt) navBt.style.display = IS_DEMO ? '' : 'none';
    if (tabBt && !IS_DEMO) tabBt.classList.remove('active', 'show');

    // Sim order panel on homepage sidebar is only shown in /demo
    const simPanel = document.getElementById('simOrderPanel');
    if (simPanel) simPanel.style.display = IS_DEMO ? '' : 'none';

    // Trades mode tabs & Stats mode selector are locked to current environment
    const tradesModeTabs = document.getElementById('tradesModeTabs');
    if (tradesModeTabs) tradesModeTabs.style.display = 'none';
    const statsModeSel = document.getElementById('statsMode');
    if (statsModeSel) {
      statsModeSel.value = APP_ENV;
      statsModeSel.style.display = 'none';
    }

    // Settings visibility: hide Binance API keys & live strategy selector on /demo
    const bkGrp = document.getElementById('binanceKeyGroup');
    const bsGrp = document.getElementById('binanceSecretGroup');
    const smGrp = document.getElementById('strategyModeGroup');
    if (bkGrp) bkGrp.style.display = IS_DEMO ? 'none' : '';
    if (bsGrp) bsGrp.style.display = IS_DEMO ? 'none' : '';
    if (smGrp) smGrp.style.display = IS_DEMO ? 'none' : '';
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
      if (typeof TradeKlineModal !== 'undefined' && TradeKlineModal.updateTheme) {
        TradeKlineModal.updateTheme();
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
    const p = loadKlines();

    // Switch Binance real-time WebSocket stream
    connectBinanceStream(symbol, state.currentTimeframe);
    return p;
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
      const res = await fetch(`/api/settings?env=${APP_ENV}`);
      if (res.ok) {
        const data = await res.json();
        state.tradingMode = APP_ENV;
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
      const res = await fetch(`/api/symbols?is_paper=${IS_DEMO}`);
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
          await switchSymbol(symbols[0].symbol);
        } else {
          await switchSymbol(state.currentSymbol);
        }
      } else {
        await switchSymbol(state.currentSymbol || 'BTCUSDT');
      }
    } catch (err) {
      console.warn('[App] Load symbols error:', err);
    }
  }

  let _klinesInFlight = null;

  async function loadKlines(retryCount = 0) {
    if (!state.currentSymbol) return;
    const reqKey = `${state.currentSymbol}:${state.currentTimeframe}`;
    if (_klinesInFlight && _klinesInFlight.key === reqKey && retryCount === 0) {
      return _klinesInFlight.promise;
    }

    const runner = (async () => {
      try {
        const url = `/api/klines/${state.currentSymbol}?timeframe=${state.currentTimeframe}&limit=200`;
        const res = await fetch(url);
        if (!res.ok) {
          if (retryCount < 1) {
            await new Promise(r => setTimeout(r, 1200));
            return loadKlines(retryCount + 1);
          }
          return;
        }
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
        if (retryCount < 1) {
          await new Promise(r => setTimeout(r, 1200));
          return loadKlines(retryCount + 1);
        }
      } finally {
        if (_klinesInFlight && _klinesInFlight.key === reqKey) {
          _klinesInFlight = null;
        }
      }
    })();

    if (retryCount === 0) {
      _klinesInFlight = { key: reqKey, promise: runner };
    }
    return runner;
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
      const res = await fetch(`/api/trades?is_paper=${IS_DEMO}&limit=500`);
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
      const selectedStrats = state.statsStrategies || [];

      let url = `/api/stats?days=${days}&is_paper=${IS_DEMO}`;
      if (sym) url += `&symbol=${encodeURIComponent(sym)}`;
      if (selectedStrats.length === 0) {
        url += '&strategy_modes=none';
      } else if (selectedStrats.length < 4) {
        url += `&strategy_modes=${encodeURIComponent(selectedStrats.join(','))}`;
      }

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
      const res = await fetch(`/api/trading/status?is_paper=${IS_DEMO}`);
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

  // ─── Bot Start / Stop Toggle & Multi-Paper Control Modal ────────────
  let botConfirmModalInstance = null;

  const PAPER_STRATEGY_META = [
    { key: 'balanced', short: '方案A', name: '方案 A：高胜率均衡', desc: '双周期三线+极速单边+阶梯锁盈 · 独立$10,000模拟账户' },
    { key: 'breakout', short: '方案B', name: '方案 B：单边爆发',   desc: '大周期强发散+极速突破+阶梯锁盈 · 独立$10,000模拟账户' },
    { key: 'ribbon',   short: '方案C', name: '方案 C：双均线贴线', desc: '1m连续贴MA7单边+未破MA25+交叉离场 · 独立$10,000模拟账户' },
    { key: 'classic',  short: '经典',  name: '经典多均线',         desc: '三线排列+自定义触发R追踪 · 独立$10,000模拟账户' },
  ];

  function _strategyShortLabel(mode) {
    const m = String(mode || 'balanced').toLowerCase();
    if (m === 'breakout') return '方案B';
    if (m === 'ribbon') return '方案C';
    if (m === 'classic') return '经典';
    return '方案A';
  }

  function _renderPaperModalContent() {
    const titleEl = document.getElementById('botModalTitle');
    const bodyEl  = document.getElementById('botModalBody');
    const footerEl = document.getElementById('botModalFooter');
    if (!titleEl || !bodyEl || !footerEl) return;

    const pm = state.paperModes || { balanced: false, breakout: false, classic: false, ribbon: false };
    const totalSlots = PAPER_STRATEGY_META.length;
    const runningCount = PAPER_STRATEGY_META.filter(item => Boolean(pm[item.key])).length;

    titleEl.innerHTML = `<i class="fa-solid fa-flask text-primary"></i><span>模拟盘独立启停控制 (${runningCount}/${totalSlots} 运行中)</span>`;

    const rowsHtml = PAPER_STRATEGY_META.map(item => {
      const running = Boolean(pm[item.key]);
      const statusBadge = running
        ? '<span class="badge bg-success-subtle text-success border border-success-subtle">运行中</span>'
        : '<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle">已停止</span>';
      const btnCls = running ? 'btn-outline-danger' : 'btn-success';
      const btnIcon = running ? 'fa-stop' : 'fa-play';
      const btnText = running ? '停止' : '启动';
      return `
        <div class="d-flex align-items-center justify-content-between gap-2 p-2 rounded border border-secondary-subtle mb-2">
          <div class="me-2">
            <div class="d-flex align-items-center gap-2">
              <span class="fw-semibold small">${item.name}</span>
              ${statusBadge}
            </div>
            <div class="text-secondary" style="font-size:11px;margin-top:2px;">${item.desc}</div>
          </div>
          <button type="button" class="btn btn-sm ${btnCls} flex-shrink-0" data-paper-action="${running ? 'stop' : 'start'}" data-strategy-mode="${item.key}">
            <i class="fa-solid ${btnIcon} me-1"></i>${btnText}
          </button>
        </div>
      `;
    }).join('');

    bodyEl.innerHTML = `
      <div class="small text-secondary mb-2">
        四个模拟盘共享同一套实时行情与 K 线缓存（零重复内存与请求开销），各自独立持仓、独立风控与独立 $10,000 权益核算。
      </div>
      ${rowsHtml}
    `;

    bodyEl.querySelectorAll('button[data-paper-action]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const act = btn.getAttribute('data-paper-action');
        const sm = btn.getAttribute('data-strategy-mode');
        btn.disabled = true;
        if (act === 'stop') {
          await stopBot('paper', sm);
        } else {
          await startBot('paper', sm);
        }
        _renderPaperModalContent();
      });
    });

    footerEl.innerHTML = `
      <div class="d-flex w-100 justify-content-between align-items-center gap-2">
        <div class="d-flex gap-2">
          <button type="button" class="btn btn-outline-success btn-sm" id="btnStartAllPaper" ${runningCount === totalSlots ? 'disabled' : ''}>
            <i class="fa-solid fa-play me-1"></i>全部启动
          </button>
          <button type="button" class="btn btn-outline-danger btn-sm" id="btnStopAllPaper" ${runningCount === 0 ? 'disabled' : ''}>
            <i class="fa-solid fa-stop me-1"></i>全部停止
          </button>
        </div>
        <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">关闭</button>
      </div>
    `;

    const btnStartAll = document.getElementById('btnStartAllPaper');
    const btnStopAll  = document.getElementById('btnStopAllPaper');
    if (btnStartAll) {
      btnStartAll.addEventListener('click', async () => {
        btnStartAll.disabled = true;
        await startBot('paper', 'all');
        _renderPaperModalContent();
      });
    }
    if (btnStopAll) {
      btnStopAll.addEventListener('click', async () => {
        btnStopAll.disabled = true;
        await stopBot('paper', 'all');
        _renderPaperModalContent();
      });
    }
  }

  async function handleBotToggleClick(mode = 'paper') {
    const modalEl = document.getElementById('botConfirmModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    if (!botConfirmModalInstance) {
      botConfirmModalInstance = new bootstrap.Modal(modalEl);
      modalEl.addEventListener('hidden.bs.modal', () => {
        state.paperModalOpen = false;
      });
    }

    if (mode !== 'live') {
      state.paperModalOpen = true;
      _renderPaperModalContent();
      botConfirmModalInstance.show();
      return;
    }

    state.paperModalOpen = false;
    const titleEl  = document.getElementById('botModalTitle');
    const bodyEl   = document.getElementById('botModalBody');
    const footerEl = document.getElementById('botModalFooter');
    if (!titleEl || !bodyEl || !footerEl) return;

    footerEl.innerHTML = `
      <button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">取消</button>
      <button type="button" class="btn btn-danger btn-sm" id="btnConfirmBotAction"></button>
    `;
    const newBtnAction = document.getElementById('btnConfirmBotAction');
    const isRunning = state.liveRunning;

    if (isRunning) {
      titleEl.innerHTML = '<i class="fa-solid fa-stop text-danger"></i><span>停止实盘自动交易监控</span>';
      bodyEl.innerHTML = `
        <div class="text-center my-2">
          <span class="badge bg-danger fs-6 px-3 py-1 mb-2">实盘运行中</span>
          <div class="text-secondary small mt-1">
            停止后系统将立即暂停<b>实盘</b>后台监控与自动开仓（不影响模拟盘），当前未平仓订单将保持现有止损保护状态。
          </div>
        </div>
        <div class="alert alert-warning py-2 px-3 small mb-0 mt-3 d-flex align-items-center">
          <i class="fa-solid fa-triangle-exclamation text-warning me-2 fs-5"></i>
          <div>确定要停止实盘自动交易吗？</div>
        </div>
      `;
      newBtnAction.innerHTML = '<i class="fa-solid fa-stop me-1"></i>确认停止实盘';
      newBtnAction.onclick = async () => {
        botConfirmModalInstance.hide();
        await stopBot('live');
      };
    } else {
      titleEl.innerHTML = '<i class="fa-solid fa-triangle-exclamation text-danger"></i><span>启动自动交易（实盘）</span>';
      bodyEl.innerHTML = `
        <div class="text-center my-2">
          <span class="badge bg-danger fs-6 px-3 py-1 mb-2">⚠️ 实盘模式 (LIVE)</span>
          <div class="text-secondary small mt-2">
            开启后<b>实盘机器人</b>将按照全局设置的策略信号通过 Binance API <b>使用真实资金下单交易</b>（可与各模拟盘同时运行，共享行情流）。
          </div>
        </div>
        <div class="alert alert-danger py-2 px-3 small mb-0 mt-3 d-flex align-items-center">
          <i class="fa-solid fa-circle-exclamation text-danger me-2 fs-5"></i>
          <div class="fw-bold">请确认已评估市场风险，确定启动实盘？</div>
        </div>
      `;
      newBtnAction.innerHTML = '<i class="fa-solid fa-play me-1"></i>确认启动实盘';
      newBtnAction.onclick = async () => {
        botConfirmModalInstance.hide();
        await startBot('live');
      };
    }

    botConfirmModalInstance.show();
  }

  async function startBot(mode = 'paper', strategyMode = '') {
    const label = mode === 'live'
      ? '实盘'
      : (strategyMode === 'all' ? '全部模拟盘' : `模拟(${_strategyShortLabel(strategyMode || 'balanced')})`);
    let url = `/api/trading/start?mode=${encodeURIComponent(mode)}`;
    if (mode === 'paper' && strategyMode) {
      url += `&strategy_mode=${encodeURIComponent(strategyMode)}`;
    }
    try {
      const res = await fetch(url, { method: 'POST' });
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

  async function stopBot(mode = 'paper', strategyMode = '') {
    const label = mode === 'live'
      ? '实盘'
      : (strategyMode && strategyMode !== 'all' ? `模拟(${_strategyShortLabel(strategyMode)})` : '全部模拟盘');
    let url = `/api/trading/stop?mode=${encodeURIComponent(mode)}`;
    if (mode === 'paper' && strategyMode) {
      url += `&strategy_mode=${encodeURIComponent(strategyMode)}`;
    }
    try {
      const res = await fetch(url, { method: 'POST' });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        await checkBotStatus();
        appendLog(`${label}交易监控已停止`, 'warning');
        if (data && data.warning) {
          appendLog(`⚠️ ${data.warning}`, 'warning');
          alert(`⚠️ ${data.warning}`);
        }
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

    const simModeEl = document.getElementById('simStrategyMode');
    const simMode = simModeEl ? simModeEl.value : 'balanced';

    const payload = {
      symbol: state.currentSymbol,
      direction: direction,
      entry_price: entryPrice,
      strategy_mode: simMode
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
      // Delegate price resolution to backend so live & paper always use authoritative Futures price
      const res = await fetch(`/api/trades/${tradeId}/close`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
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

  let archiveConfirmModalInstance = null;
  let archivesModalInstance = null;

  async function loadArchivesList() {
    try {
      const res = await fetch(`/api/trades/archives?is_paper=${IS_DEMO}`);
      if (!res.ok) return [];
      const list = await res.json();
      state.archives = Array.isArray(list) ? list : [];
      const badge = document.getElementById('archiveCountBadge');
      if (badge) badge.textContent = state.archives.length;
      return state.archives;
    } catch (err) {
      console.warn('[App] Load archives error:', err);
      return [];
    }
  }

  function openArchiveConfirmModal() {
    const allTrades = state.trades || [];
    const openLive = allTrades.filter(t => !t.is_paper && t.status === 'open');
    if (openLive.length > 0) {
      alert(`当前仍有 ${openLive.length} 笔持仓中的【实盘】订单，请先平仓后再执行存档！`);
      return;
    }
    if (allTrades.length === 0) {
      alert('当前暂无可存档的交易记录。');
      return;
    }

    const paperCnt = allTrades.filter(t => Boolean(t.is_paper)).length;
    const liveCnt = allTrades.length - paperCnt;
    const openPaperCnt = allTrades.filter(t => Boolean(t.is_paper) && t.status === 'open').length;
    const closedPnl = allTrades
      .filter(t => t.status === 'closed')
      .reduce((acc, t) => acc + Number(t.pnl || 0), 0);
    const pnlSign = closedPnl >= 0 ? '+' : '';
    const pnlCls = closedPnl >= 0 ? 'text-success' : 'text-danger';

    const summaryEl = document.getElementById('archiveConfirmSummary');
    if (summaryEl) {
      const openTip = openPaperCnt > 0
        ? `<div class="mt-2 text-warning"><i class="fa-solid fa-triangle-exclamation me-1"></i>当前包含 <b>${openPaperCnt}</b> 笔持仓中的模拟单，确认存档时将自动按实时市价结算并一并归档。</div>`
        : '';
      summaryEl.innerHTML = `
        <div class="p-2 rounded border border-secondary-subtle bg-body-tertiary">
          <div>即将归档交易记录：<b>${allTrades.length}</b> 笔（模拟 <b>${paperCnt}</b> 笔 / 实盘 <b>${liveCnt}</b> 笔）</div>
          <div class="mt-1">已平仓累计净盈亏：<span class="${pnlCls} fw-bold">${pnlSign}$${closedPnl.toFixed(2)}</span></div>
          ${openTip}
        </div>
      `;
    }

    const titleInput = document.getElementById('archiveTitleInput');
    if (titleInput) titleInput.value = '';

    const modalEl = document.getElementById('archiveConfirmModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    if (!archiveConfirmModalInstance) {
      archiveConfirmModalInstance = new bootstrap.Modal(modalEl);
    }
    archiveConfirmModalInstance.show();
  }

  async function executeArchiveTrades() {
    const btn = document.getElementById('btnConfirmArchiveAction');
    const titleVal = document.getElementById('archiveTitleInput')?.value?.trim() || null;
    if (btn) btn.disabled = true;
    try {
      const res = await fetch(`/api/trades/archive?is_paper=${IS_DEMO}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: titleVal })
      });
      if (res.ok) {
        const arc = await res.json();
        if (archiveConfirmModalInstance) archiveConfirmModalInstance.hide();
        appendLog(`[交易存档] 已创建「${arc.title}」（共 ${arc.total_trades} 笔），当前状态已恢复初始值`, 'success');
        state.tradesPage = 1;
        await Promise.all([loadTrades(), loadStats(), loadArchivesList(), checkBotStatus()]);
      } else {
        const err = await res.json().catch(() => ({}));
        alert('存档失败: ' + (err.detail || res.statusText));
      }
    } catch (err) {
      alert('存档请求失败: ' + err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function _bindArchiveDetailStrategyBtns() {
    const wrap = document.getElementById('archiveDetailStrategyBtns');
    if (!wrap) return;
    wrap.querySelectorAll('button[data-arc-strat]').forEach(btn => {
      btn.addEventListener('click', () => {
        const k = btn.getAttribute('data-arc-strat');
        const cur = new Set(state.archiveDetailStrategies || []);
        if (cur.has(k)) cur.delete(k); else cur.add(k);
        state.archiveDetailStrategies = ['balanced', 'breakout', 'ribbon', 'classic'].filter(x => cur.has(x));
        wrap.querySelectorAll('button[data-arc-strat]').forEach(b => {
          const bk = b.getAttribute('data-arc-strat');
          const active = cur.has(bk);
          b.className = active ? 'btn btn-xs btn-primary active' : 'btn btn-xs btn-outline-secondary';
        });
        renderArchiveDetailPanel();
      });
    });
  }

  async function openArchivesModal() {
    const modalEl = document.getElementById('archivesModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;
    if (!archivesModalInstance) {
      archivesModalInstance = new bootstrap.Modal(modalEl);
    }
    archivesModalInstance.show();
    const list = await loadArchivesList();
    renderArchivesTable(list);
    if (list.length > 0) {
      await viewArchiveDetail(list[0].id);
    } else {
      state.activeArchiveDetail = null;
      const wrap = document.getElementById('archiveDetailWrap');
      if (wrap) wrap.style.display = 'none';
    }
  }

  function renderArchivesTable(list) {
    const tbody = document.getElementById('archivesTableBody');
    const countEl = document.getElementById('archivesListCount');
    if (countEl) countEl.textContent = `共 ${(list || []).length} 个存档`;
    if (!tbody) return;
    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center text-secondary py-3">暂无存档记录</td></tr>';
      return;
    }
    const activeId = state.activeArchiveDetail?.archive?.id;
    tbody.innerHTML = list.map(a => {
      const idNum = Number(a.id) || 0;
      const pnl = Number(a.total_pnl || 0);
      const pnlSign = pnl >= 0 ? '+' : '';
      const pnlCls = pnl >= 0 ? 'text-success' : 'text-danger';
      const rangeStr = `${_fmtTradeTime(a.start_time)} ~ ${_fmtTradeTime(a.end_time)}`;
      const isSelected = activeId === idNum;
      return `
        <tr class="${isSelected ? 'table-active' : ''}" style="cursor:pointer" onclick="AppController.viewArchiveDetail(${idNum})">
          <td class="text-secondary small">#${idNum}</td>
          <td class="fw-semibold">${_escapeHtml(a.title)}</td>
          <td class="small text-secondary font-monospace">${_fmtLogTime(a.created_at)}</td>
          <td class="small text-secondary">${_escapeHtml(rangeStr)}</td>
          <td>${Number(a.total_trades || 0)} <span class="text-secondary small">(模${Number(a.paper_trades || 0)}/实${Number(a.live_trades || 0)})</span></td>
          <td>${Number(a.win_rate || 0).toFixed(1)}%</td>
          <td class="${pnlCls} fw-bold">${pnlSign}$${pnl.toFixed(2)}</td>
          <td class="text-end pe-3 text-nowrap" onclick="event.stopPropagation()">
            <button class="btn btn-xs btn-outline-primary" onclick="AppController.viewArchiveDetail(${idNum})">查看明细</button>
            <button class="btn btn-xs btn-outline-danger ms-1" onclick="AppController.deleteArchive(${idNum})" title="删除此存档">
              <i class="fa-solid fa-trash-can"></i>
            </button>
          </td>
        </tr>
      `;
    }).join('');
  }

  async function viewArchiveDetail(archiveId) {
    try {
      const res = await fetch(`/api/trades/archives/${archiveId}`);
      if (!res.ok) return;
      const detail = await res.json();
      state.activeArchiveDetail = detail;
      renderArchivesTable(state.archives || []);
      renderArchiveDetailPanel();
    } catch (err) {
      console.warn('[App] Load archive detail error:', err);
    }
  }

  function renderArchiveDetailPanel() {
    const wrap = document.getElementById('archiveDetailWrap');
    const titleEl = document.getElementById('archiveDetailTitle');
    const metaEl = document.getElementById('archiveDetailMeta');
    const tbody = document.getElementById('archiveDetailTradesBody');
    if (!wrap || !tbody) return;

    const detail = state.activeArchiveDetail;
    if (!detail || !detail.archive) {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = 'block';
    const arc = detail.archive;
    const selStrats = state.archiveDetailStrategies || [];
    const trades = (detail.trades || []).filter(t => _matchTradeStrategy(t, selStrats));

    const subClosed = trades.filter(t => t.status === 'closed');
    const subWins = subClosed.filter(t => Number(t.pnl || 0) > 0).length;
    const subWr = subClosed.length > 0 ? (subWins / subClosed.length * 100).toFixed(1) : '0.0';
    const subPnl = subClosed.reduce((s, t) => s + Number(t.pnl || 0), 0);
    const subSign = subPnl >= 0 ? '+' : '';
    const subCls = subPnl >= 0 ? 'text-success' : 'text-danger';

    if (titleEl) titleEl.textContent = `#${arc.id} ${arc.title}`;
    if (metaEl) {
      metaEl.innerHTML = `筛选共 <b>${trades.length}</b> 笔 · 胜率 <b>${subWr}%</b> · 净盈亏 <span class="${subCls} fw-bold">${subSign}$${subPnl.toFixed(2)}</span>`;
    }

    if (trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="11" class="text-center text-secondary py-3">当前策略筛选下暂无归档明细</td></tr>';
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const isLong = (t.direction || '').toLowerCase() === 'long';
      const dirBadge = isLong
        ? '<span class="badge bg-success-subtle text-success border border-success-subtle">多</span>'
        : '<span class="badge bg-danger-subtle text-danger border border-danger-subtle">空</span>';
      const pnlVal = Number(t.pnl || 0);
      const pSign = pnlVal >= 0 ? '+' : '';
      const pCls = pnlVal >= 0 ? 'text-success' : 'text-danger';
      const pctStr = t.pnl_pct != null ? ` (${t.pnl_pct >= 0 ? '+' : ''}${Number(t.pnl_pct).toFixed(2)}%)` : '';
      const stratShort = _strategyShortLabel(t.strategy_mode);
      const typeBadge = t.is_paper
        ? `<span class="badge el-tag-paper">模拟·${stratShort}</span>`
        : `<span class="badge bg-danger-subtle text-danger border border-danger-subtle">实盘·${stratShort}</span>`;
      const reasonBadge = _renderCloseReasonBadge(t.close_reason);
      return `
        <tr>
          <td class="text-secondary small">#${Number(t.id) || 0}</td>
          <td><strong>${_escapeHtml(t.symbol)}</strong></td>
          <td>${dirBadge}</td>
          <td>${t.entry_price != null ? Number(t.entry_price).toFixed(2) : '—'}</td>
          <td>${t.exit_price != null ? Number(t.exit_price).toFixed(2) : '—'}</td>
          <td class="text-secondary small">${t.stop_loss != null ? Number(t.stop_loss).toFixed(2) : '—'}</td>
          <td class="${pCls} fw-bold">${pSign}$${pnlVal.toFixed(2)}${pctStr}</td>
          <td class="small text-secondary">${_fmtTradeTime(t.open_time)}</td>
          <td class="small text-secondary">${_fmtTradeTime(t.close_time)}</td>
          <td>${reasonBadge}</td>
          <td>${typeBadge}</td>
        </tr>
      `;
    }).join('');
  }

  async function deleteArchive(archiveId) {
    if (!confirm(`确定要永久删除存档 #${archiveId} 及其包含的全部归档交易明细吗？此操作不可恢复！`)) return;
    try {
      const res = await fetch(`/api/trades/archives/${archiveId}`, { method: 'DELETE' });
      if (res.ok) {
        const list = await loadArchivesList();
        if (state.activeArchiveDetail?.archive?.id === archiveId) {
          state.activeArchiveDetail = null;
        }
        renderArchivesTable(list);
        if (list.length > 0 && !state.activeArchiveDetail) {
          await viewArchiveDetail(list[0].id);
        } else {
          renderArchiveDetailPanel();
        }
      } else {
        const err = await res.json().catch(() => ({}));
        alert('删除存档失败: ' + (err.detail || res.statusText));
      }
    } catch (err) {
      alert('删除存档请求失败: ' + err.message);
    }
  }

  function openTradesKlineModal() {
    if (typeof TradeKlineModal === 'undefined') return;
    const allTrades = state.trades || [];
    const selectedStrats = state.tradesStrategies || [];
    const filtered = allTrades.filter(t =>
      (state.tradesMode === 'live' ? !t.is_paper : Boolean(t.is_paper)) && _matchTradeStrategy(t, selectedStrats)
    );
    const eqSym = document.getElementById('tradesEquitySymbol')?.value;
    const prefSym = (eqSym && eqSym !== 'ALL')
      ? eqSym
      : (filtered[0]?.symbol || state.currentSymbol || 'BTCUSDT');
    const symCfg = (state.symbols || []).find(s => s.symbol === prefSym);
    const tf = symCfg?.timeframe || state.currentTimeframe || '1m';
    const modeTitle = state.tradesMode === 'live' ? '实盘交易记录' : '模拟交易记录';
    TradeKlineModal.open({
      title: `${modeTitle} · K线买卖点复盘`,
      symbol: prefSym,
      timeframe: tf,
      trades: filtered,
    });
  }

  function openArchiveKlineModal() {
    if (typeof TradeKlineModal === 'undefined' || !state.activeArchiveDetail?.archive) return;
    const arc = state.activeArchiveDetail.archive;
    const selStrats = state.archiveDetailStrategies || [];
    const trades = (state.activeArchiveDetail.trades || []).filter(t => _matchTradeStrategy(t, selStrats));
    const prefSym = trades[0]?.symbol || state.currentSymbol || 'BTCUSDT';
    TradeKlineModal.open({
      title: `存档 #${arc.id} ${arc.title} · K线买卖点复盘`,
      symbol: prefSym,
      timeframe: '1m',
      trades,
    });
  }

  async function clearAllTrades() {
    if (!confirm('确定要清空当前未存档的全部交易记录吗？（已保存的历史存档不受影响）')) return;
    try {
      const res = await fetch(`/api/trades/clear?is_paper=${IS_DEMO}`, { method: 'POST' });
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
    const nextPaperModes = { ...(state.paperModes || { balanced: false, breakout: false, classic: false, ribbon: false }) };

    if (typeof statusOrBool === 'object' && statusOrBool !== null) {
      if (statusOrBool.paper_modes && typeof statusOrBool.paper_modes === 'object') {
        ['balanced', 'breakout', 'ribbon', 'classic'].forEach(k => {
          if (k in statusOrBool.paper_modes) nextPaperModes[k] = Boolean(statusOrBool.paper_modes[k]);
        });
        paperRunning = Object.values(nextPaperModes).some(Boolean);
      } else if (statusOrBool.paper_mode) {
        const pmKey = String(statusOrBool.paper_mode).toLowerCase();
        if (pmKey in nextPaperModes) {
          nextPaperModes[pmKey] = Boolean(statusOrBool.paper_mode_running);
        }
        paperRunning = Object.values(nextPaperModes).some(Boolean);
      } else if ('paper_running' in statusOrBool) {
        paperRunning = Boolean(statusOrBool.paper_running);
      }
      if ('live_running' in statusOrBool) liveRunning = Boolean(statusOrBool.live_running);
      if (!('paper_running' in statusOrBool) && !('live_running' in statusOrBool) && !statusOrBool.paper_modes && !statusOrBool.paper_mode) {
        paperRunning = Boolean(statusOrBool.running);
      }
    } else {
      paperRunning = Boolean(statusOrBool);
    }

    state.paperModes = nextPaperModes;
    const activePaperList = PAPER_STRATEGY_META.filter(m => Boolean(nextPaperModes[m.key]));
    const activeCount = activePaperList.length;
    const anyRunning = paperRunning || liveRunning;
    state.paperRunning = paperRunning;
    state.liveRunning = liveRunning;
    state.botRunning = anyRunning;

    const dot   = document.getElementById('statusDot');
    const label = document.getElementById('statusLabel');
    const paperBtn = document.getElementById('btnPaperToggle');
    const liveBtn  = document.getElementById('btnLiveToggle');

    const envRunning = IS_DEMO ? paperRunning : liveRunning;
    if (dot) dot.className = 'status-dot ' + (envRunning ? 'running' : 'stopped');
    if (label) {
      if (IS_DEMO) {
        const paperDesc = activeCount > 0
          ? `模拟(${activePaperList.map(m => m.short).join('/')})`
          : '模拟';
        label.textContent = paperRunning ? `${paperDesc}运行中` : '已停止';
      } else {
        label.textContent = liveRunning ? '实盘运行中' : '已停止';
      }
    }

    if (paperBtn) {
      const displayCls = IS_DEMO ? 'd-inline-flex align-items-center' : 'd-none';
      paperBtn.hidden = !IS_DEMO;
      if (paperRunning) {
        const cntSuffix = activeCount > 0 ? ` ${activeCount}/${PAPER_STRATEGY_META.length}` : '';
        paperBtn.className = `btn btn-sm btn-primary ${displayCls}`;
        paperBtn.innerHTML = `<i class="fa-solid fa-flask me-1"></i>模拟${cntSuffix}`;
        paperBtn.title = '模拟盘运行中，点击管理各策略启停';
      } else {
        paperBtn.className = `btn btn-sm btn-success ${displayCls}`;
        paperBtn.innerHTML = '<i class="fa-solid fa-play me-1"></i>模拟';
        paperBtn.title = '点击管理多策略模拟盘启停';
      }
    }

    if (liveBtn) {
      const displayCls = IS_DEMO ? 'd-none' : 'd-inline-flex align-items-center';
      liveBtn.hidden = IS_DEMO;
      if (liveRunning) {
        liveBtn.className = `btn btn-sm btn-danger ${displayCls}`;
        liveBtn.innerHTML = '<i class="fa-solid fa-stop me-1"></i>实盘';
        liveBtn.title = '实盘运行中，点击停止';
      } else {
        liveBtn.className = `btn btn-sm btn-outline-danger ${displayCls}`;
        liveBtn.innerHTML = '<i class="fa-solid fa-play me-1"></i>实盘';
        liveBtn.title = '点击启动实盘自动交易';
      }
    }

    if (state.paperModalOpen) {
      _renderPaperModalContent();
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

  function _matchTradeStrategy(trade, selectedList) {
    if (!Array.isArray(selectedList) || selectedList.length === 0) return false;
    const tm = String(trade.strategy_mode || 'balanced').toLowerCase();
    return selectedList.includes(tm);
  }

  function renderTradesView() {
    const allTrades = state.trades || [];
    const selectedStrats = state.tradesStrategies || [];
    const paperTrades = allTrades.filter(t => Boolean(t.is_paper) && _matchTradeStrategy(t, selectedStrats));
    const liveTrades = allTrades.filter(t => !t.is_paper && _matchTradeStrategy(t, selectedStrats));

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
      let url = `/api/logs?mode=${APP_ENV}&page=${state.logsPage}&page_size=${state.logsPageSize}`;
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
      open:    { label: '开仓',     cls: 'bg-primary-subtle text-primary border border-primary-subtle' },
      close:   { label: '平仓',     cls: 'bg-success-subtle text-success border border-success-subtle' },
      archive: { label: '交易存档', cls: 'bg-info-subtle text-info border border-info-subtle' },
      start:   { label: '系统启动', cls: 'bg-info-subtle text-info border border-info-subtle' },
      stop:    { label: '系统停止', cls: 'bg-warning-subtle text-warning border border-warning-subtle' }
    };

    tbody.innerHTML = logs.map(log => {
      const meta = typeMeta[log.log_type] || { label: _escapeHtml(log.log_type || '其他'), cls: 'bg-secondary-subtle text-secondary' };
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
      const res = await fetch(`/api/logs/clear?mode=${APP_ENV}`, { method: 'POST' });
      if (res.ok) {
        state.logsPage = 1;
        loadOperationLogs();
      }
    } catch (err) {
      console.error('[App] Clear logs error:', err);
    }
  }

  // ─── System Logs (persistent homepage logs with pagination + keyword filter) ─
  let _sysLogsRefreshTimer = null;
  function _scheduleSystemLogsRefresh() {
    const pane = document.getElementById('tabSystemLogs');
    if (!pane || !pane.classList.contains('active')) return;
    clearTimeout(_sysLogsRefreshTimer);
    _sysLogsRefreshTimer = setTimeout(() => loadSystemLogs(), 250);
  }

  async function hydrateHomepageLogBox() {
    const box = document.getElementById('logBox');
    if (!box || box.children.length > 0) return;
    try {
      const res = await fetch(`/api/system-logs?mode=${APP_ENV}&page=1&page_size=25`);
      if (!res.ok) return;
      const data = await res.json();
      const items = Array.isArray(data?.items) ? data.items.slice().reverse() : [];
      items.forEach(item => {
        const line = document.createElement('div');
        const lvl = String(item.level || 'info').replace(/[^a-z0-9_-]/gi, '');
        const timeText = _fmtLogTime(item.created_at).slice(6); // HH:MM:SS
        line.className = 'log-line ' + lvl;
        line.innerHTML = `<span class="text-secondary">[${_escapeHtml(timeText)}]</span> ${_escapeHtml(item.message)}`;
        box.appendChild(line);
      });
      box.scrollTop = box.scrollHeight;
    } catch (err) {
      console.warn('[App] Hydrate homepage logBox error:', err);
    }
  }

  async function loadSystemLogs() {
    try {
      let url = `/api/system-logs?mode=${APP_ENV}&page=${state.sysLogsPage}&page_size=${state.sysLogsPageSize}`;
      if (state.sysLogsLevelFilter) {
        url += `&level=${encodeURIComponent(state.sysLogsLevelFilter)}`;
      }
      if (state.sysLogsKeyword) {
        url += `&keyword=${encodeURIComponent(state.sysLogsKeyword)}`;
      }
      const res = await fetch(url);
      if (!res.ok) return;
      const data = await res.json();
      const items = Array.isArray(data?.items) ? data.items : [];
      state.sysLogs = items;
      state.sysLogsTotal = data?.total ?? items.length;
      state.sysLogsPage = data?.page ?? state.sysLogsPage;
      state.sysLogsTotalPages = data?.total_pages ?? 1;
      renderSystemLogs(state.sysLogs);
      renderSystemLogsPagination();
    } catch (err) {
      console.warn('[App] Load system logs error:', err);
    }
  }

  function goToSystemLogsPage(page) {
    const target = Math.max(1, Math.min(page, state.sysLogsTotalPages || 1));
    if (target === state.sysLogsPage) return;
    state.sysLogsPage = target;
    loadSystemLogs();
  }

  function renderSystemLogsPagination() {
    if (typeof window.renderTablePagination === 'function') {
      window.renderTablePagination({
        infoEl: document.getElementById('sysLogsPageInfo'),
        ulEl: document.getElementById('sysLogsPagination'),
        page: state.sysLogsPage || 1,
        totalPages: state.sysLogsTotalPages || 1,
        pageSize: state.sysLogsPageSize,
        total: state.sysLogsTotal ?? 0,
        onPageChange: goToSystemLogsPage
      });
    }
  }

  function _highlightKeyword(text, kw) {
    const escaped = _escapeHtml(text);
    if (!kw) return escaped;
    const escapedKw = _escapeHtml(kw).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return escaped.replace(new RegExp(`(${escapedKw})`, 'gi'), '<mark class="px-1 rounded">$1</mark>');
  }

  function renderSystemLogs(logs) {
    const tbody = document.getElementById('sysLogsTableBody');
    const countEl = document.getElementById('sysLogCount');
    if (countEl) countEl.textContent = `共 ${state.sysLogsTotal ?? (logs || []).length} 条`;
    if (!tbody) return;

    if (!logs || logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-secondary py-4">暂无系统日志</td></tr>';
      return;
    }

    const levelMeta = {
      info:    { label: '信息', cls: 'bg-info-subtle text-info border border-info-subtle' },
      success: { label: '成功', cls: 'bg-success-subtle text-success border border-success-subtle' },
      warning: { label: '警告', cls: 'bg-warning-subtle text-warning border border-warning-subtle' },
      error:   { label: '错误', cls: 'bg-danger-subtle text-danger border border-danger-subtle' }
    };
    const kw = state.sysLogsKeyword || '';

    tbody.innerHTML = logs.map(log => {
      const meta = levelMeta[log.level] || levelMeta.info;
      const symHtml = log.symbol
        ? `<span class="fw-semibold">${_highlightKeyword(log.symbol, kw)}</span>`
        : '<span class="text-secondary">—</span>';
      return `
        <tr>
          <td class="ps-3 py-3 text-secondary">#${Number(log.id) || 0}</td>
          <td class="py-3 text-secondary font-monospace small">${_fmtLogTime(log.created_at)}</td>
          <td class="py-3"><span class="badge ${meta.cls}">${meta.label}</span></td>
          <td class="py-3">${symHtml}</td>
          <td class="pe-3 py-3 font-monospace small">${_highlightKeyword(log.message, kw)}</td>
        </tr>
      `;
    }).join('');
  }

  async function clearSystemLogs() {
    if (!confirm('确定清空所有系统日志吗？此操作不可撤销。')) return;
    try {
      const res = await fetch(`/api/system-logs/clear?mode=${APP_ENV}`, { method: 'POST' });
      if (res.ok) {
        state.sysLogsPage = 1;
        loadSystemLogs();
        const box = document.getElementById('logBox');
        if (box) box.innerHTML = '';
      }
    } catch (err) {
      console.error('[App] Clear system logs error:', err);
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

  function _renderCloseReasonBadge(closeReason) {
    const reasonMap = {
      stop_loss:        '<span class="badge bg-danger-subtle text-danger border border-danger-subtle">初始止损</span>',
      profit_lock_stop: '<span class="badge bg-success-subtle text-success border border-success-subtle">阶梯锁盈</span>',
      trailing_stop:    '<span class="badge bg-success-subtle text-success border border-success-subtle">移动止损</span>',
      ma7_ma25_cross:   '<span class="badge bg-info-subtle text-info border border-info-subtle">MA7/25交叉</span>',
      ma7_trailing:     '<span class="badge bg-success-subtle text-success border border-success-subtle">MA7 止盈</span>',
      ma25_trailing:    '<span class="badge bg-success-subtle text-success border border-success-subtle">MA25 止盈</span>',
      ma60_trailing:    '<span class="badge bg-success-subtle text-success border border-success-subtle">MA60 止盈</span>',
      htf_ma25_trailing:'<span class="badge bg-success-subtle text-success border border-success-subtle">大周期MA25止盈</span>',
      htf_ma60_trailing:'<span class="badge bg-success-subtle text-success border border-success-subtle">大周期MA60止盈</span>',
      ma60_cross:       '<span class="badge bg-warning-subtle text-warning border border-warning-subtle">MA60 穿线</span>',
      take_profit:      '<span class="badge bg-success-subtle text-success border border-success-subtle">目标止盈</span>',
      exchange_closed:  '<span class="badge bg-warning-subtle text-warning border border-warning-subtle">交易所平仓</span>',
      archived:         '<span class="badge bg-primary-subtle text-primary border border-primary-subtle">存档结算</span>',
      manual:           '<span class="badge el-tag-paper">手动平仓</span>',
      time_stop:        '<span class="badge el-tag-paper">超时平仓</span>',
    };
    return reasonMap[closeReason] || (closeReason ? `<span class="badge el-tag-paper">${_escapeHtml(closeReason)}</span>` : '<span class="text-secondary">—</span>');
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

      const stratShort = _strategyShortLabel(t.strategy_mode);
      const typeBadge = t.is_paper
        ? `<span class="badge el-tag-paper" title="模拟策略: ${stratShort}">模拟·${stratShort}</span>`
        : `<span class="badge bg-danger-subtle text-danger border border-danger-subtle" title="实盘策略: ${stratShort}">实盘·${stratShort}</span>`;

      const reasonBadge = t.status === 'open'
        ? '<span class="text-secondary">—</span>'
        : _renderCloseReasonBadge(t.close_reason);

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
        <td class="text-secondary small">${(t.take_profit && Math.abs(t.take_profit - t.entry_price) > 1e-8) ? t.take_profit.toFixed(2) : 'MA25追踪'}</td>
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
    _scheduleSystemLogsRefresh();
  }

  // ─── Binance Official Realtime Kline WebSocket ─────────────────────
  let binanceWs = null;
  let currentBinanceStream = null;
  let binanceReconnectTimer = null;
  let binanceWsUseSpotFallback = false;

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
    const wsUrl = binanceWsUseSpotFallback
      ? `wss://stream.binance.com:9443/ws/${streamName}`
      : `wss://fstream.binance.com/ws/${streamName}`;
    console.log(`[Binance WS] Connecting real-time stream: ${wsUrl}`);

    try {
      binanceWs = new WebSocket(wsUrl);

      binanceWs.onopen = () => {
        if (!binanceWsUseSpotFallback) {
          console.log(`[Binance WS] ✅ Connected to official Futures live stream: ${streamName}`);
        } else {
          console.log(`[Binance WS] ⚠️ Connected to fallback stream: ${streamName}`);
          binanceWsUseSpotFallback = false;
        }
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
        console.warn(`[Binance WS] Stream error (${streamName}), switching endpoint on retry...`, err);
        binanceWsUseSpotFallback = !binanceWsUseSpotFallback;
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
        // Send token + environment ('live' | 'paper') in first JSON frame
        ws.send(JSON.stringify({ type: 'auth', token, env: APP_ENV }));
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
        let suffix = msg.blocked_by_wick ? '（反向长影线过滤，等待下一根确认）' : '';
        if (msg.cooldown_left_min && msg.cooldown_left_min > 0) {
          suffix += `（平仓冷却剩余 ${msg.cooldown_left_min}m，暂缓开仓）`;
        }
        const sigTag = msg.strategy_mode ? `·${_strategyShortLabel(msg.strategy_mode)}` : '';
        appendLog(`[信号${sigTag}] ${msg.symbol} ${msg.direction.toUpperCase()} 确认 ${msg.confirm}/${msg.max_confirm}${suffix}`, 'info');
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

      case 'trades_archived': {
        loadTrades();
        loadStats();
        loadArchivesList();
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
    openArchiveConfirmModal,
    openArchivesModal,
    viewArchiveDetail,
    deleteArchive,
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
