/**
 * settings.js — 设置面板逻辑（全局设置 + 交易对管理）
 */

const SettingsManager = (() => {
  const IS_DEMO = window.location.pathname.startsWith('/demo');
  const APP_ENV = IS_DEMO ? 'paper' : 'live';
  let symbols = [];
  let modalInstance = null;
  let editingId = null;
  let monitorTimer = null;

  function _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  const STRATEGY_MODE_DESCS = {
    balanced: '严格大周期三线+斜率+MA200顺势 · 2.0×HTF_ATR波段止损 · 1.0R锁+0.35R后沿大周期均线追踪（1m/15m 建议确认K线=2~3）',
    breakout: '大周期均线强发散+MA200顺势 · 小周期顺势突破MA25 · HTF_MA60结构止损 · 1.2R锁+0.35R后沿大周期均线追踪',
    ribbon:   '1m连续N根收在MA7单侧且未破MA25 · 均线开口发散+波动率过滤+15m不逆势 · 阶梯锁盈后持有至MA7×MA25交叉平仓（建议确认K线=3）',
    classic:  '经典三线排列+斜率确认 · 小周期MA60结构止损 · 按交易对触发R/保本%与小周期均线追踪（适合 1h/4h 大周期）'
  };

  function updateStrategyModeDesc() {
    const sel = document.getElementById('strategyMode');
    const desc = document.getElementById('strategyModeDesc');
    if (sel && desc) {
      desc.textContent = STRATEGY_MODE_DESCS[sel.value] || STRATEGY_MODE_DESCS.balanced;
    }
  }

  // ─── Init ────────────────────────────────────────────────────────────
  function init() {
    modalInstance = new bootstrap.Modal(document.getElementById('symbolModal'));

    // slider label
    document.getElementById('symConfirm').addEventListener('input', (e) => {
      document.getElementById('symConfirmVal').textContent = e.target.value;
    });

    // global settings form
    document.getElementById('globalSettingsForm').addEventListener('submit', saveGlobalSettings);
    const modeSel = document.getElementById('strategyMode');
    if (modeSel) {
      modeSel.addEventListener('change', updateStrategyModeDesc);
    }

    // add symbol
    document.getElementById('btnAddSymbol').addEventListener('click', openAddModal);
    document.getElementById('btnSaveSymbol').addEventListener('click', saveSymbol);

    // test telegram
    const btnTestTg = document.getElementById('btnTestTg');
    if (btnTestTg) {
      btnTestTg.addEventListener('click', testTelegramNotification);
    }

    // server monitor polling (only while Settings tab is visible)
    const tabSettingsLink = document.querySelector('a[href="#tabSettings"]');
    if (tabSettingsLink) {
      tabSettingsLink.addEventListener('shown.bs.tab', startMonitorPolling);
      tabSettingsLink.addEventListener('hidden.bs.tab', stopMonitorPolling);
    }
    document.addEventListener('visibilitychange', () => {
      const pane = document.getElementById('tabSettings');
      if (document.hidden) {
        stopMonitorPolling();
      } else if (pane && pane.classList.contains('active')) {
        startMonitorPolling();
      }
    });
  }

  // ─── Server Resource Monitor ─────────────────────────────────────────
  function _fmtUptime(sec) {
    const s = Math.max(0, Number(sec) || 0);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
  }

  function _renderSparkline(svgId, values, color, avgVal) {
    const svg = document.getElementById(svgId);
    if (!svg) return;
    const W = 180;
    const H = 42;
    const pts = (values && values.length) ? values : [0];
    const norm = pts.length === 1 ? [pts[0], pts[0]] : pts;
    const step = W / (norm.length - 1);
    const coords = norm.map((v, i) => {
      const clamped = Math.max(0, Math.min(100, Number(v) || 0));
      const x = (i * step).toFixed(1);
      const y = (H - 4 - (clamped / 100) * (H - 8)).toFixed(1);
      return [x, y];
    });
    const lineD = coords.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x},${y}`).join(' ');
    const areaD = `${lineD} L${W},${H} L0,${H} Z`;
    const avgClamped = Math.max(0, Math.min(100, Number(avgVal) || 0));
    const avgY = (H - 4 - (avgClamped / 100) * (H - 8)).toFixed(1);
    const gradId = `${svgId}_grad`;

    svg.innerHTML = `
      <defs>
        <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${color}" stop-opacity="0.28"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      <path d="${areaD}" fill="url(#${gradId})"/>
      <line x1="0" y1="${avgY}" x2="${W}" y2="${avgY}" stroke="currentColor" stroke-opacity="0.25" stroke-width="1" stroke-dasharray="3 3"/>
      <path d="${lineD}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    `;
  }

  async function loadSystemMetrics() {
    try {
      const res = await fetch('/api/system/metrics');
      if (!res.ok) return;
      const d = await res.json();
      const hist = d.history || [];

      // CPU
      const cpu = d.cpu || {};
      const cpuEl = document.getElementById('monCpuPct');
      if (cpuEl) cpuEl.textContent = cpu.pct != null ? Number(cpu.pct).toFixed(1) : '—';
      const cpuSub = document.getElementById('monCpuSub');
      if (cpuSub) {
        const ghzPart = cpu.ghz ? ` · ${Number(cpu.ghz).toFixed(2)} GHz` : '';
        cpuSub.textContent = `${cpu.cores || 1} Core${ghzPart}`;
      }
      const cpuAvg = document.getElementById('monCpuAvg');
      const cpuPeak = document.getElementById('monCpuPeak');
      if (cpuAvg) cpuAvg.textContent = cpu.avg != null ? `${cpu.avg}%` : '—';
      if (cpuPeak) cpuPeak.textContent = cpu.peak != null ? `${cpu.peak}%` : '—';
      _renderSparkline('sparkCpu', hist.map(p => p.cpu), '#409eff', cpu.avg);

      // Memory
      const mem = d.memory || {};
      const memEl = document.getElementById('monMemPct');
      if (memEl) memEl.textContent = mem.pct != null ? Number(mem.pct).toFixed(1) : '—';
      const memSub = document.getElementById('monMemSub');
      if (memSub) memSub.textContent = `${Number(mem.used_mb || 0).toFixed(2)} MB / ${Number(mem.total_mb || 0).toFixed(2)} MB`;
      const memAvg = document.getElementById('monMemAvg');
      const memPeak = document.getElementById('monMemPeak');
      if (memAvg) memAvg.textContent = mem.avg != null ? `${mem.avg}%` : '—';
      if (memPeak) memPeak.textContent = mem.peak != null ? `${mem.peak}%` : '—';
      const memColor = (mem.pct || 0) >= 85 ? '#e6a23c' : '#409eff';
      _renderSparkline('sparkMem', hist.map(p => p.mem), memColor, mem.avg);

      // Swap
      const swap = d.swap || {};
      const swapEl = document.getElementById('monSwapPct');
      if (swapEl) swapEl.textContent = swap.pct != null ? Number(swap.pct).toFixed(1) : '—';
      const swapSub = document.getElementById('monSwapSub');
      if (swapSub) swapSub.textContent = `${Number(swap.used_gb || 0).toFixed(2)} GB / ${Number(swap.total_gb || 0).toFixed(2)} GB`;
      const swapAvg = document.getElementById('monSwapAvg');
      const swapPeak = document.getElementById('monSwapPeak');
      if (swapAvg) swapAvg.textContent = swap.avg != null ? `${swap.avg}%` : '—';
      if (swapPeak) swapPeak.textContent = swap.peak != null ? `${swap.peak}%` : '—';
      _renderSparkline('sparkSwap', hist.map(p => p.swap), '#409eff', swap.avg);

      // Disk
      const disk = d.disk || {};
      const diskEl = document.getElementById('monDiskPct');
      if (diskEl) diskEl.textContent = disk.pct != null ? Number(disk.pct).toFixed(1) : '—';
      const diskSub = document.getElementById('monDiskSub');
      if (diskSub) diskSub.textContent = `${Number(disk.used_gb || 0).toFixed(2)} GB / ${Number(disk.total_gb || 0).toFixed(2)} GB`;
      const diskFree = document.getElementById('monDiskFree');
      const diskAvg = document.getElementById('monDiskAvg');
      if (diskFree) diskFree.textContent = disk.free_gb != null ? `${Number(disk.free_gb).toFixed(2)} GB` : '—';
      if (diskAvg) diskAvg.textContent = disk.avg != null ? `${disk.avg}%` : '—';
      _renderSparkline('sparkDisk', hist.map(p => p.disk), '#409eff', disk.avg);

      // Runtime
      const rt = d.runtime || {};
      const appUp = document.getElementById('monAppUptime');
      const osUp = document.getElementById('monOsUptime');
      const procMem = document.getElementById('monProcMem');
      const procThr = document.getElementById('monProcThreads');
      if (appUp) appUp.textContent = _fmtUptime(rt.app_uptime_sec);
      if (osUp) osUp.textContent = _fmtUptime(rt.os_uptime_sec);
      if (procMem) procMem.textContent = `${Number(rt.proc_mem_mb || 0).toFixed(2)} MB`;
      if (procThr) procThr.textContent = rt.proc_threads ?? '—';
    } catch (err) {
      console.warn('获取系统监控指标失败', err);
    }
  }

  function startMonitorPolling() {
    stopMonitorPolling();
    loadSystemMetrics();
    monitorTimer = setInterval(loadSystemMetrics, 4000);
  }

  function stopMonitorPolling() {
    if (monitorTimer) {
      clearInterval(monitorTimer);
      monitorTimer = null;
    }
  }

  // ─── Test Telegram Notification ──────────────────────────────────────
  async function testTelegramNotification() {
    const btn = document.getElementById('btnTestTg');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>测试中…';

    try {
      // 先保存一次当前填写的代理、token 和 chat_id
      const payload = {
        http_proxy:       (document.getElementById('httpProxy')?.value || '').trim(),
        telegram_token:   document.getElementById('telegramToken').value.trim(),
        telegram_chat_id: document.getElementById('telegramChatId').value.trim(),
      };
      await fetch(`/api/settings?env=${APP_ENV}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const res = await fetch(`/api/settings/test-telegram?env=${APP_ENV}`, { method: 'POST' });
      const rawText = await res.text();
      let data = {};
      try {
        data = JSON.parse(rawText);
      } catch {
        const match = rawText.match(/<title>(.*?)<\/title>/i);
        throw new Error(`服务器网关异常 (HTTP ${res.status}): ${match ? match[1] : rawText.slice(0, 100)}`);
      }
      if (!res.ok) {
        throw new Error(data.detail || '测试发送失败');
      }

      if (data.chat_id) {
        document.getElementById('telegramChatId').value = data.chat_id;
      }
      alert('✅ ' + (data.message || 'Telegram 测试消息发送成功！'));
    } catch (err) {
      alert('❌ 发送失败:\n' + err.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = originalText;
    }
  }

  // ─── Load global settings ────────────────────────────────────────────
  function loadGlobalSettings(data) {
    if (!data) return;
    document.getElementById('binanceKey').value       = data.binance_key      || '';
    document.getElementById('binanceSecret').value    = data.binance_secret   || '';
    document.getElementById('httpProxy').value        = data.http_proxy       || '';
    document.getElementById('telegramToken').value    = data.telegram_token   || '';
    document.getElementById('telegramChatId').value   = data.telegram_chat_id || '';
    document.getElementById('maxDailyLoss').value     = data.max_daily_loss   || 5;
    const modeEl = document.getElementById('strategyMode');
    if (modeEl) {
      modeEl.value = data.strategy_mode || 'balanced';
      updateStrategyModeDesc();
    }
    const cdEl = document.getElementById('cooldownBars');
    if (cdEl) {
      cdEl.value = (data.cooldown_bars !== undefined && data.cooldown_bars !== '') ? data.cooldown_bars : '30';
    }
  }

  // ─── Save global settings ────────────────────────────────────────────
  async function saveGlobalSettings(e) {
    e.preventDefault();
    const btn = document.querySelector('#globalSettingsForm button[type="submit"]');
    const originalText = btn ? btn.innerHTML : '';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中…';
    }

    const payload = {
      http_proxy:       (document.getElementById('httpProxy')?.value || '').trim(),
      telegram_token:   document.getElementById('telegramToken').value.trim(),
      telegram_chat_id: document.getElementById('telegramChatId').value.trim(),
      max_daily_loss:   document.getElementById('maxDailyLoss').value.trim() || '5',
      cooldown_bars:    (document.getElementById('cooldownBars')?.value ?? '30').toString().trim() || '30'
    };
    if (!IS_DEMO) {
      payload.binance_key = document.getElementById('binanceKey').value.trim();
      payload.binance_secret = document.getElementById('binanceSecret').value.trim();
      payload.strategy_mode = document.getElementById('strategyMode')?.value || 'balanced';
    }

    try {
      const res = await fetch(`/api/settings?env=${APP_ENV}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const rawText = await res.text();
      let data = {};
      try {
        data = JSON.parse(rawText);
      } catch {
        const match = rawText.match(/<title>(.*?)<\/title>/i);
        throw new Error(`服务器网关异常 (HTTP ${res.status}): ${match ? match[1] : rawText.slice(0, 100)}`);
      }

      if (!res.ok) {
        throw new Error(data.detail || `HTTP ${res.status}: 数据库写入失败`);
      }

      // 重新加载回填确认
      loadGlobalSettings(data.settings || data);

      const msg = document.getElementById('settingsSaveMsg');
      if (msg) {
        msg.className = 'mt-2 text-success small fw-bold';
        msg.textContent = '✓ 设置已成功保存并持久化至数据库！';
        msg.style.display = 'block';
        setTimeout(() => { msg.style.display = 'none'; }, 4000);
      }
      alert('✅ 全局设置已成功保存！\n刷新页面或重启服务均会持久生效。');
    } catch (err) {
      console.error('保存设置失败', err);
      alert('❌ 保存设置失败:\n' + err.message + '\n\n提示：如果是在 Ubuntu 服务器上，请检查 ma-trader/data 目录的读写权限：\nsudo chown -R $USER:$USER data && chmod 700 data');
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalText;
      }
    }
  }

  // ─── Load symbols into sidebar + settings table ───────────────────────
  function loadSymbols(data) {
    symbols = data || [];
    renderSymbolTable();
    renderSidebarSymbols();
    updateBacktestSymbolSelect();
    updateStatsSymbolSelect();
  }

  // ─── Settings table ───────────────────────────────────────────────────
  function renderSymbolTable() {
    const tbody = document.getElementById('symbolTableBody');
    tbody.innerHTML = '';
    symbols.forEach(sym => {
      const tr = document.createElement('tr');
      const idNum = Number(sym.id) || 0;
      const trailR = sym.trailing_activate_r != null ? Number(sym.trailing_activate_r).toFixed(2) : '0.88';
      const beBufPct = sym.breakeven_buffer != null ? (Number(sym.breakeven_buffer) * 100).toFixed(2).replace(/0$/, '') : '0.3';
      const trailMa = (sym.trailing_ma || 'ma25').toUpperCase();
      tr.innerHTML = `
        <td class="fw-bold">${_esc(sym.symbol)}</td>
        <td>${_esc(sym.timeframe)}</td>
        <td>${_esc(sym.htf_timeframe)}</td>
        <td>${_esc(sym.confirm_candles)}</td>
        <td>${_esc(sym.leverage)}x</td>
        <td>${(Number(sym.risk_per_trade) * 100).toFixed(1)}%</td>
        <td>${_esc(sym.rr_ratio)}</td>
        <td class="small text-secondary">${trailR}R / +${beBufPct}% / ${_esc(trailMa)}</td>
        <td>${sym.enabled ? '<span style="color:#3fb950">✅</span>' : '<span style="color:#f85149">❌</span>'}</td>
        <td>
          <button class="btn btn-xs btn-outline-secondary me-1" onclick="SettingsManager.editSymbol(${idNum})">编辑</button>
          <button class="btn btn-xs btn-outline-danger" onclick="SettingsManager.deleteSymbol(${idNum})">删除</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  // ─── Sidebar symbols ──────────────────────────────────────────────────
  function renderSidebarSymbols() {
    const list = document.getElementById('symbolList');
    list.innerHTML = '';
    symbols.forEach(sym => {
      const safeSym = _esc(sym.symbol);
      const li = document.createElement('li');
      li.className = 'symbol-item';
      li.dataset.symbol = sym.symbol;
      li.innerHTML = `
        <div class="flex-fill">
          <div class="symbol-name">${safeSym}</div>
          <div class="symbol-price" id="price-${safeSym}">—</div>
        </div>
        <div class="symbol-dir flat" id="dir-${safeSym}">—</div>
      `;
      li.addEventListener('click', () => AppController.switchSymbol(sym.symbol));
      list.appendChild(li);
    });
    // Activate first
    if (symbols.length) {
      list.querySelector('.symbol-item')?.classList.add('active');
    }
  }

  function getSymbolConfig(symbolName) {
    if (!symbolName) return null;
    const target = String(symbolName).toUpperCase().trim();
    return symbols.find(s => s.symbol === target) || null;
  }

  // ─── Update other selects ──────────────────────────────────────────────
  function updateBacktestSymbolSelect() {
    const sel = document.getElementById('btSymbol');
    sel.innerHTML = symbols.map(s => `<option value="${_esc(s.symbol)}">${_esc(s.symbol)}</option>`).join('');
    if (typeof BacktestManager !== 'undefined' && BacktestManager.syncSymbolParams) {
      BacktestManager.syncSymbolParams(sel.value);
    }
  }

  function updateStatsSymbolSelect() {
    const sel = document.getElementById('statsSymbol');
    sel.innerHTML = '<option value="">全部交易对</option>' +
      symbols.map(s => `<option value="${_esc(s.symbol)}">${_esc(s.symbol)}</option>`).join('');
  }

  // ─── Add modal ────────────────────────────────────────────────────────
  function openAddModal() {
    editingId = null;
    document.getElementById('symbolModalTitle').textContent = '添加交易对';
    document.getElementById('symbolId').value  = '';
    document.getElementById('symSymbol').value = '';
    document.getElementById('symTf').value     = '1m';
    document.getElementById('symHtf').value    = '15m';
    document.getElementById('symConfirm').value = 3;
    document.getElementById('symConfirmVal').textContent = '3';
    document.getElementById('symLeverage').value = 8;
    document.getElementById('symRisk').value     = 1.2;
    document.getElementById('symRR').value       = 1.5;
    document.getElementById('symTrailR').value   = 0.88;
    document.getElementById('symBeBuf').value    = 0.3;
    document.getElementById('symTrailingMa').value = 'ma25';
    document.getElementById('symEnabled').checked = true;
    document.getElementById('symSymbol').disabled = false;
    modalInstance.show();
  }

  function editSymbol(id) {
    const sym = symbols.find(s => s.id === id);
    if (!sym) return;
    editingId = id;
    document.getElementById('symbolModalTitle').textContent = '编辑交易对';
    document.getElementById('symbolId').value   = id;
    document.getElementById('symSymbol').value  = sym.symbol;
    document.getElementById('symTf').value      = sym.timeframe;
    document.getElementById('symHtf').value     = sym.htf_timeframe;
    document.getElementById('symConfirm').value = sym.confirm_candles;
    document.getElementById('symConfirmVal').textContent = sym.confirm_candles;
    document.getElementById('symLeverage').value = sym.leverage;
    document.getElementById('symRisk').value     = (sym.risk_per_trade * 100).toFixed(1);
    document.getElementById('symRR').value       = sym.rr_ratio;
    document.getElementById('symTrailR').value   = sym.trailing_activate_r != null ? sym.trailing_activate_r : 0.88;
    document.getElementById('symBeBuf').value    = sym.breakeven_buffer != null ? Number((sym.breakeven_buffer * 100).toFixed(3)) : 0.3;
    document.getElementById('symTrailingMa').value = (sym.trailing_ma || 'ma25').toLowerCase();
    document.getElementById('symEnabled').checked = Boolean(sym.enabled);
    document.getElementById('symSymbol').disabled = true;
    modalInstance.show();
  }

  async function deleteSymbol(id) {
    if (!confirm('确定要删除该交易对吗？')) return;
    try {
      const res = await fetch(`/api/symbols/${id}`, { method: 'DELETE' });
      if (res.ok) {
        symbols = symbols.filter(s => s.id !== id);
        renderSymbolTable();
        renderSidebarSymbols();
      }
    } catch (err) {
      console.error('删除失败', err);
    }
  }

  async function saveSymbol() {
    const payload = {
      symbol:          document.getElementById('symSymbol').value.toUpperCase().trim(),
      is_paper:        IS_DEMO,
      timeframe:       document.getElementById('symTf').value,
      htf_timeframe:   document.getElementById('symHtf').value,
      confirm_candles: parseInt(document.getElementById('symConfirm').value),
      leverage:        parseInt(document.getElementById('symLeverage').value),
      risk_per_trade:  parseFloat(document.getElementById('symRisk').value) / 100,
      rr_ratio:        parseFloat(document.getElementById('symRR').value),
      trailing_activate_r: parseFloat(document.getElementById('symTrailR').value),
      breakeven_buffer:    parseFloat(document.getElementById('symBeBuf').value) / 100,
      trailing_ma:         document.getElementById('symTrailingMa')?.value || 'ma25',
      enabled:         Boolean(document.getElementById('symEnabled').checked)
    };

    try {
      let res;
      if (editingId) {
        res = await fetch(`/api/symbols/${editingId}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } else {
        res = await fetch('/api/symbols', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      }
      if (res.ok) {
        const data = await res.json();
        if (editingId) {
          symbols = symbols.map(s => s.id === editingId ? data : s);
        } else {
          symbols.push(data);
        }
        renderSymbolTable();
        renderSidebarSymbols();
        modalInstance.hide();
      } else {
        const err = await res.json().catch(() => ({}));
        const detail = Array.isArray(err.detail)
          ? err.detail.map(d => d.msg || JSON.stringify(d)).join('; ')
          : (err.detail || `HTTP ${res.status}`);
        alert('保存交易对失败: ' + detail);
      }
    } catch (err) {
      console.error('保存交易对失败', err);
    }
  }

  // ─── Live update sidebar price / direction ────────────────────────────
  function updateSymbolPrice(symbol, price) {
    const el = document.getElementById(`price-${symbol}`);
    if (el) el.textContent = price.toLocaleString();
  }

  function updateSymbolDirection(symbol, direction) {
    const el = document.getElementById(`dir-${symbol}`);
    if (!el) return;
    el.className = 'symbol-dir';
    if (direction === 'long_aligned')  { el.classList.add('long');  el.textContent = '▲'; }
    else if (direction === 'short_aligned') { el.classList.add('short'); el.textContent = '▼'; }
    else { el.classList.add('flat'); el.textContent = '—'; }
  }

  return {
    init, loadGlobalSettings, loadSymbols,
    editSymbol, deleteSymbol, getSymbolConfig,
    updateSymbolPrice, updateSymbolDirection
  };
})();
