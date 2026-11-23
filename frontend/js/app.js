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
    ws: null,
    wsPingInterval: null,
    wsReconnectTimeout: null,
    klinesData: [],
    isLoadingHistory: false,
    hasMoreHistory: true
  };

  // ─── Initialization ────────────────────────────────────────────────
  async function init() {
    console.log('[App] Initializing MA Trader App...');

    // 0. Setup Theme (Day / Night mode)
    setupTheme();

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

    // Bot Start / Stop
    const btnStart = document.getElementById('btnStart');
    const btnStop  = document.getElementById('btnStop');
    if (btnStart) btnStart.addEventListener('click', startBot);
    if (btnStop)  btnStop.addEventListener('click', stopBot);

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
      tabStatsLink.addEventListener('shown.bs.tab', loadStats);
    }
    const tabTradesLink = document.querySelector('a[href="#tabTrades"]');
    if (tabTradesLink) {
      tabTradesLink.addEventListener('shown.bs.tab', loadTrades);
    }
    const tabSettingsLink = document.querySelector('a[href="#tabSettings"]');
    if (tabSettingsLink) {
      tabSettingsLink.addEventListener('shown.bs.tab', loadSettings);
    }

    // Theme toggle (Day / Night mode)
    const btnThemeToggle = document.getElementById('btnThemeToggle');
    if (btnThemeToggle) {
      btnThemeToggle.addEventListener('click', toggleTheme);
    }

    // Simulate Trade Buttons (if present in DOM)
    const btnSimLong = document.getElementById('btnSimLong');
    const btnSimShort = document.getElementById('btnSimShort');
    if (btnSimLong) btnSimLong.addEventListener('click', () => simulateTrade('long'));
    if (btnSimShort) btnSimShort.addEventListener('click', () => simulateTrade('short'));
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
      const res = await fetch('/api/trades?limit=100');
      if (!res.ok) return;
      const trades = await res.json();
      state.trades = trades;

      renderTradesTable(trades);

      const countEl = document.getElementById('tradeCount');
      if (countEl) countEl.textContent = trades.length;

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
        updateBotUI(status.running);
      }
    } catch (err) {
      console.warn('[App] Check bot status error:', err);
    }
  }

  async function startBot() {
    try {
      const res = await fetch('/api/trading/start', { method: 'POST' });
      if (res.ok) {
        updateBotUI(true);
        appendLog('交易监控已启动', 'info');
      } else {
        const err = await res.json();
        alert('启动失败: ' + (err.detail || '未知错误'));
      }
    } catch (err) {
      console.error('启动交易失败', err);
    }
  }

  async function stopBot() {
    try {
      const res = await fetch('/api/trading/stop', { method: 'POST' });
      if (res.ok) {
        updateBotUI(false);
        appendLog('交易监控已停止', 'warning');
      }
    } catch (err) {
      console.error('停止交易失败', err);
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
    if (!confirm(`确定要平仓该笔交易 (ID: ${tradeId}) 吗？`)) return;
    try {
      const res = await fetch(`/api/trades/${tradeId}/close`, { method: 'POST' });
      if (res.ok) {
        appendLog(`[手动平仓] 订单 #${tradeId} 平仓成功`, 'info');
        await loadTrades();
        await loadStats();
      } else {
        const err = await res.json();
        alert('平仓失败: ' + (err.detail || '未知错误'));
      }
    } catch (err) {
      console.error('平仓失败', err);
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
  function updateBotUI(running) {
    state.botRunning = running;
    const dot   = document.getElementById('statusDot');
    const label = document.getElementById('statusLabel');
    const bStart = document.getElementById('btnStart');
    const bStop  = document.getElementById('btnStop');

    if (dot)   dot.className = 'status-dot ' + (running ? 'running' : 'stopped');
    if (label) label.textContent = running ? '运行中' : '已停止';
    if (bStart) bStart.disabled = running;
    if (bStop)  bStop.disabled  = !running;
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

  function renderTradesTable(trades) {
    const tbody = document.getElementById('tradesTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (!trades || trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="12" class="text-center text-secondary py-4">暂无交易记录</td></tr>';
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
      if (t.pnl !== null && t.pnl !== undefined) {
        const sign = t.pnl >= 0 ? '+' : '';
        const color = t.pnl >= 0 ? 'text-success' : 'text-danger';
        const pct = t.pnl_pct ? ` (${(t.pnl_pct * 100).toFixed(1)}%)` : '';
        pnlText = `<span class="${color} fw-bold">${sign}$${t.pnl.toFixed(2)}${pct}</span>`;
      }

      const typeBadge = t.is_paper
        ? '<span class="badge bg-secondary-subtle text-light border border-secondary" title="Paper Trading">模拟</span>'
        : '<span class="badge bg-danger-subtle text-danger border border-danger" title="Live Trading">实盘</span>';

      const timeStr = t.open_time ? t.open_time.replace('T', ' ').slice(5, 16) : '—';

      // Action column
      let actionBtn = `
        <button class="btn btn-xs btn-outline-secondary" onclick="AppController.highlightTrade(${t.id})">
          <i class="fa-solid fa-crosshairs"></i>
        </button>
      `;
      if (t.status === 'open') {
        actionBtn = `
          <button class="btn btn-xs btn-danger me-1" onclick="AppController.closeTrade(${t.id})" title="平仓">
            平仓
          </button>
          ${actionBtn}
        `;
      }

      tr.innerHTML = `
        <td><span class="text-secondary small">#${t.id}</span></td>
        <td><strong>${t.symbol}</strong></td>
        <td>${dirBadge}</td>
        <td>${t.entry_price?.toFixed(2) ?? '—'}</td>
        <td>${t.exit_price ? t.exit_price.toFixed(2) : '—'}</td>
        <td class="text-secondary small">${t.stop_loss?.toFixed(2) ?? '—'}</td>
        <td class="text-secondary small">${t.take_profit?.toFixed(2) ?? '—'}</td>
        <td>${pnlText}</td>
        <td>${statusBadge}</td>
        <td class="small text-secondary">${timeStr}</td>
        <td>${typeBadge}</td>
        <td class="text-end text-nowrap">${actionBtn}</td>
      `;
      tbody.appendChild(tr);
    });
  }

  function appendLog(msg, level = 'info') {
    const box = document.getElementById('logBox');
    if (!box) return;
    const time = new Date().toTimeString().split(' ')[0];
    const line = document.createElement('div');
    line.className = 'log-line ' + level;
    line.innerHTML = `<span class="text-secondary">[${time}]</span> ${msg}`;
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
    const wsUrl = `${protocol}//${window.location.host}/ws`;
    const badge = document.getElementById('wsBadge');

    try {
      const ws = new WebSocket(wsUrl);
      state.ws = ws;

      ws.onopen = () => {
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
        // Update sidebar
        if (typeof SettingsManager !== 'undefined' && SettingsManager.updateSymbolPrice) {
          SettingsManager.updateSymbolPrice(msg.symbol, msg.price);
        }
        // If current symbol, update price
        if (msg.symbol === state.currentSymbol) {
          updateCurrentPrice(msg.price);
        }
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
        updateBotUI(Boolean(msg.running));
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
    highlightTrade,
    loadTrades,
    loadStats,
    loadSymbols
  };
})();

// Auto-run on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  AppController.init();
});
