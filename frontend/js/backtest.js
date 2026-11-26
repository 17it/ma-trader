/**
 * backtest.js — 回测面板逻辑
 */

const BacktestManager = (() => {
  let historyRuns = [];
  let historyPage = 1;
  const historyPageSize = 20;

  function _toLocalInputVal(dateObj) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${dateObj.getFullYear()}-${pad(dateObj.getMonth() + 1)}-${pad(dateObj.getDate())}T${pad(dateObj.getHours())}:${pad(dateObj.getMinutes())}`;
  }

  function _toIsoWithOffset(localVal) {
    if (!localVal) return localVal;
    const d = new Date(localVal);
    if (isNaN(d.getTime())) return localVal;
    const pad = (n) => String(n).padStart(2, '0');
    const offsetMin = -d.getTimezoneOffset();
    const sign = offsetMin >= 0 ? '+' : '-';
    const absMin = Math.abs(offsetMin);
    const tz = `${sign}${pad(Math.floor(absMin / 60))}:${pad(absMin % 60)}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:00${tz}`;
  }

  function _formatForInput(val, isEnd = false) {
    if (!val) return '';
    const s = String(val).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
      return isEnd ? `${s}T23:59` : `${s}T00:00`;
    }
    const d = new Date(s);
    if (!isNaN(d.getTime())) return _toLocalInputVal(d);
    return s.slice(0, 16);
  }

  function syncSymbolParams(symbolName) {
    if (typeof SettingsManager === 'undefined' || !SettingsManager.getSymbolConfig) return;
    const cfg = SettingsManager.getSymbolConfig(symbolName);
    if (!cfg) return;
    const tfEl = document.getElementById('btTimeframe');
    const htfEl = document.getElementById('btHtf');
    const confEl = document.getElementById('btConfirm');
    const confLbl = document.getElementById('btConfirmLabel');
    const rrEl = document.getElementById('btRR');
    const trailREl = document.getElementById('btTrailR');
    const beEl = document.getElementById('btBeBuf');
    const tmaEl = document.getElementById('btTrailingMa');
    if (tfEl && cfg.timeframe) tfEl.value = cfg.timeframe;
    if (htfEl && cfg.htf_timeframe) htfEl.value = cfg.htf_timeframe;
    if (confEl && cfg.confirm_candles != null) {
      confEl.value = cfg.confirm_candles;
      if (confLbl) confLbl.textContent = cfg.confirm_candles;
    }
    if (rrEl && cfg.rr_ratio != null) rrEl.value = cfg.rr_ratio;
    if (trailREl && cfg.trailing_activate_r != null) trailREl.value = cfg.trailing_activate_r;
    if (beEl && cfg.breakeven_buffer != null) beEl.value = Number((cfg.breakeven_buffer * 100).toFixed(3));
    if (tmaEl && cfg.trailing_ma) tmaEl.value = String(cfg.trailing_ma).toLowerCase();
  }

  function init() {
    // Default date-time range: 1st day of current month 00:00 ~ now (down to minute)
    const now = new Date();
    const firstDay = new Date(now.getFullYear(), now.getMonth(), 1, 0, 0, 0);
    const startEl = document.getElementById('btStartDate');
    const endEl = document.getElementById('btEndDate');
    if (startEl) startEl.value = _toLocalInputVal(firstDay);
    if (endEl) endEl.value = _toLocalInputVal(now);

    // confirm slider label
    document.getElementById('btConfirm').addEventListener('input', (e) => {
      document.getElementById('btConfirmLabel').textContent = e.target.value;
    });

    // V2-B5: Auto-populate saved symbol parameters when switching symbol in backtest form
    const btSymEl = document.getElementById('btSymbol');
    if (btSymEl) {
      btSymEl.addEventListener('change', (e) => syncSymbolParams(e.target.value));
    }

    // run backtest
    document.getElementById('btnRunBacktest').addEventListener('click', runBacktest);

    // load history and resize chart on tab show
    const tabBacktestLink = document.querySelector('a[href="#tabBacktest"]');
    if (tabBacktestLink) {
      tabBacktestLink.addEventListener('shown.bs.tab', () => {
        loadHistory();
        setTimeout(() => BacktestChart.resize(), 60);
      });
    }

    // clear all history
    const btnClear = document.getElementById('btnClearBtHistory');
    if (btnClear) {
      btnClear.addEventListener('click', clearAllHistory);
    }
  }

  // ─── Run backtest ─────────────────────────────────────────────────────
  async function runBacktest() {
    const titleVal = document.getElementById('btTitle')?.value?.trim();
    const btModeVal = document.getElementById('btStrategyMode')?.value || '';
    const payload = {
      title:         titleVal || null,
      symbol:        document.getElementById('btSymbol').value,
      timeframe:     document.getElementById('btTimeframe').value,
      htf_timeframe: document.getElementById('btHtf').value,
      start_date:    _toIsoWithOffset(document.getElementById('btStartDate').value),
      end_date:      _toIsoWithOffset(document.getElementById('btEndDate').value),
      confirm_candles: parseInt(document.getElementById('btConfirm').value),
      rr_ratio:      parseFloat(document.getElementById('btRR').value),
      trailing_activate_r: parseFloat(document.getElementById('btTrailR')?.value || '0.88'),
      breakeven_buffer:    parseFloat(document.getElementById('btBeBuf')?.value || '0.3') / 100,
      trailing_ma:         document.getElementById('btTrailingMa')?.value || 'ma25',
      strategy_mode:       btModeVal || null,
      initial_capital: parseFloat(document.getElementById('btCapital').value)
    };

    document.getElementById('btProgress').style.display = 'block';
    document.getElementById('btResults').style.display  = 'none';
    document.getElementById('btnRunBacktest').disabled  = true;

    try {
      // Create backtest run
      const res = await fetch('/api/backtest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error('创建回测失败');
      const run = await res.json();

      // Poll for result
      await pollBacktest(run.id);
    } catch (err) {
      console.error('回测失败', err);
      alert('回测失败: ' + err.message);
    } finally {
      document.getElementById('btProgress').style.display = 'none';
      document.getElementById('btnRunBacktest').disabled  = false;
    }
  }

  async function pollBacktest(id, attempt = 0) {
    if (attempt > 300) throw new Error('回测请求超时（超过10分钟），请缩小回测时间区间重试');
    
    // Update progress label with elapsed seconds
    const pBar = document.querySelector('#btProgress .progress-bar');
    if (pBar) {
      const elapsed = attempt * 2;
      pBar.textContent = `后台非阻塞拉取数据与运算中... (${elapsed}s)`;
    }

    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch(`/api/backtest/${id}`);
    if (!res.ok) {
      throw new Error(`获取回测进度失败 (HTTP ${res.status})`);
    }
    const run = await res.json();
    if (run.status === 'done') {
      displayResults(run);
      await loadHistory();
    } else if (run.status === 'failed') {
      throw new Error(run.error_msg || '回测执行失败');
    } else {
      await pollBacktest(id, attempt + 1);
    }
  }

  // ─── Display results ──────────────────────────────────────────────────
  function displayResults(run) {
    const results = typeof run.results === 'string' ? JSON.parse(run.results) : run.results;
    if (!results) return;

    // Metric cards
    const metricsEl = document.getElementById('btMetrics');
    const pnlColor  = (results.total_pnl ?? 0) >= 0 ? 'text-green' : 'text-red';
    const ddColor   = 'text-red';

    const wr = results.win_rate;
    const wrVal = wr != null ? (wr > 1 ? wr : wr * 100).toFixed(1) : '—';

    const pnlPct = results.total_pnl_pct ?? results.pnl_pct;
    const pnlPctStr = pnlPct != null ? `(${(pnlPct >= 0 ? '+' : '')}${Number(pnlPct).toFixed(1)}%)` : '';

    metricsEl.innerHTML = `
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">总交易次数</div>
          <div class="bt-metric-value text-blue">${results.total_trades ?? '—'}</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">胜率</div>
          <div class="bt-metric-value text-green">${wrVal}%</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">总盈亏</div>
          <div class="bt-metric-value ${pnlColor}">${results.total_pnl != null ? (results.total_pnl >= 0 ? '+' : '') + '$' + Number(results.total_pnl).toFixed(2) : '—'}
            <small style="font-size:12px">${pnlPctStr}</small>
          </div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">最大回撤</div>
          <div class="bt-metric-value ${ddColor}">${results.max_drawdown != null ? Number(results.max_drawdown).toFixed(1) + '%' : '—'}</div>
        </div>
      </div>
    `;

    // Display results container FIRST so chart dimensions are properly computed
    document.getElementById('btResults').style.display = 'block';

    // Equity curve
    if (results.equity_curve && results.equity_curve.length) {
      const dates  = results.equity_curve.map(e => e.time || e.date || e.t || '');
      const equity = results.equity_curve.map(e => e.equity ?? e.v ?? 0);
      BacktestChart.render(dates, equity);
      setTimeout(() => BacktestChart.resize(), 50);
      setTimeout(() => BacktestChart.resize(), 200);
    }

    // Trade list
    const tbody = document.getElementById('btTradeBody');
    tbody.innerHTML = '';
    const tradeCountEl = document.getElementById('btTradeCount');
    if (tradeCountEl) {
      tradeCountEl.textContent = `共 ${(results.trades || []).length} 笔`;
    }

    (results.trades || []).forEach(tr => {
      const pnlColor = (tr.pnl || 0) >= 0 ? '#3fb950' : '#f85149';
      const dirIcon  = tr.direction === 'long' ? '<span style="color:#3fb950">▲ 多</span>' : '<span style="color:#f85149">▼ 空</span>';
      const entryTimeStr = formatTradeTime(tr.entry_time || tr.open_time || tr.time);
      const exitTimeStr  = formatTradeTime(tr.exit_time || tr.close_time);
      const tr2 = document.createElement('tr');
      tr2.innerHTML = `
        <td>${dirIcon}</td>
        <td class="text-secondary">${entryTimeStr}</td>
        <td>${tr.entry_price?.toFixed(4) ?? '—'}</td>
        <td class="text-secondary">${exitTimeStr}</td>
        <td>${tr.exit_price?.toFixed(4) ?? '—'}</td>
        <td style="color:${pnlColor};font-weight:600">${tr.pnl != null ? (tr.pnl >= 0 ? '+' : '') + tr.pnl.toFixed(2) : '—'}</td>
      `;
      tbody.appendChild(tr2);
    });
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatTradeTime(tStr) {
    if (!tStr) return '—';
    try {
      const d = new Date(tStr);
      if (isNaN(d.getTime())) return String(tStr);
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const h = String(d.getHours()).padStart(2, '0');
      const min = String(d.getMinutes()).padStart(2, '0');
      return `${m}-${day} ${h}:${min}`;
    } catch {
      return String(tStr);
    }
  }

  // ─── History ──────────────────────────────────────────────────────────
  async function loadHistory() {
    try {
      const res = await fetch('/api/backtest?limit=200');
      if (!res.ok) return;
      const runs = await res.json();
      historyRuns = Array.isArray(runs) ? runs : [];
      renderHistoryPage();
    } catch (err) {
      console.warn('加载历史回测失败', err);
    }
  }

  function renderHistoryPage() {
    const total = historyRuns.length;
    const totalPages = Math.max(1, Math.ceil(total / historyPageSize));
    historyPage = Math.max(1, Math.min(historyPage, totalPages));
    const startIdx = (historyPage - 1) * historyPageSize;
    const pageRuns = historyRuns.slice(startIdx, startIdx + historyPageSize);
    renderHistory(pageRuns);

    if (typeof window.renderTablePagination === 'function') {
      window.renderTablePagination({
        infoEl: document.getElementById('btHistoryPageInfo'),
        ulEl: document.getElementById('btHistoryPagination'),
        page: historyPage,
        totalPages,
        pageSize: historyPageSize,
        total,
        onPageChange: (p) => {
          historyPage = p;
          renderHistoryPage();
        }
      });
    }
  }

  function _fmtHistoryDt(val) {
    if (!val) return '—';
    const s = String(val).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
    const d = new Date(s);
    if (isNaN(d.getTime())) return s.replace('T', ' ').slice(0, 16);
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function renderHistory(runs) {
    const tbody = document.getElementById('btHistoryBody');
    tbody.innerHTML = '';
    if (!runs || runs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-center text-muted py-3">暂无历史记录</td></tr>';
      return;
    }
    runs.forEach(run => {
      let results = null;
      try { results = run.results ? JSON.parse(run.results) : null; } catch {}
      const statusBadge = {
        done:    '<span class="badge-closed">完成</span>',
        running: '<span class="badge-open">运行中</span>',
        pending: '<span style="color:#8b949e">等待</span>',
        failed:  '<span class="badge-stopped">失败</span>'
      }[run.status] || escapeHtml(run.status);

      const pnl    = results?.total_pnl;
      const pnlStr = pnl != null ? `<span style="color:${pnl >= 0 ? '#3fb950' : '#f85149'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>` : '—';
      const wr     = results?.win_rate;
      const wrVal  = wr != null ? (wr > 1 ? wr : wr * 100).toFixed(1) : '—';
      const wrStr  = wrVal !== '—' ? `${wrVal}%` : '—';

      const titleStr = run.title ? `<span class="fw-semibold">${escapeHtml(run.title)}</span>` : '<span class="text-secondary">—</span>';
      const rangeStr = escapeHtml(`${_fmtHistoryDt(run.start_date)} ~ ${_fmtHistoryDt(run.end_date)}`);
      const runIdNum = Number(run.id) || 0;

      const tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.innerHTML = `
        <td>#${runIdNum}</td>
        <td>${titleStr}</td>
        <td>${escapeHtml(run.symbol)}</td>
        <td>${escapeHtml(run.timeframe)}</td>
        <td class="small">${rangeStr}</td>
        <td>${statusBadge}</td>
        <td>${pnlStr}</td>
        <td>${wrStr}</td>
        <td class="text-end pe-3">
          <div class="d-inline-flex gap-1">
            <button class="btn btn-xs btn-outline-primary" onclick="BacktestManager.viewRun(${runIdNum})">查看</button>
            <button class="btn btn-xs btn-outline-danger" onclick="BacktestManager.deleteRun(event, ${runIdNum})" title="删除此记录">
              <i class="fa-solid fa-trash-can"></i>
            </button>
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function deleteRun(event, id) {
    if (event) event.stopPropagation();
    if (!confirm(`确定要删除回测记录 #${id} 吗？`)) return;
    try {
      const res = await fetch(`/api/backtest/${id}`, { method: 'DELETE' });
      if (!res.ok && res.status !== 204) throw new Error('删除失败');
      await loadHistory();
    } catch (err) {
      console.error('删除回测记录失败', err);
      alert('删除失败: ' + err.message);
    }
  }

  async function clearAllHistory() {
    if (!confirm('确定要清空所有回测历史记录吗？此操作不可恢复。')) return;
    try {
      const res = await fetch('/api/backtest', { method: 'DELETE' });
      if (!res.ok && res.status !== 204) throw new Error('清空失败');
      await loadHistory();
      // Hide results panel if cleared
      document.getElementById('btResults').style.display = 'none';
    } catch (err) {
      console.error('清空历史记录失败', err);
      alert('清空失败: ' + err.message);
    }
  }

  function fillParams(run) {
    if (!run) return;
    let params = {};
    if (run.params) {
      try {
        params = typeof run.params === 'string' ? JSON.parse(run.params) : run.params;
      } catch (e) {
        console.warn('解析 run.params 失败', e);
      }
    }

    // 交易对
    const symbol = run.symbol || params.symbol;
    if (symbol) {
      const el = document.getElementById('btSymbol');
      if (el) {
        let exists = Array.from(el.options).some(opt => opt.value === symbol);
        if (!exists) {
          const opt = document.createElement('option');
          opt.value = symbol;
          opt.textContent = symbol;
          el.appendChild(opt);
        }
        el.value = symbol;
      }
    }

    // 时间框架
    const tf = run.timeframe || params.timeframe;
    if (tf) {
      const el = document.getElementById('btTimeframe');
      if (el) el.value = tf;
    }

    // 高级框架
    const htf = params.htf_timeframe;
    if (htf) {
      const el = document.getElementById('btHtf');
      if (el) el.value = htf;
    }

    // 开始/结束时间
    const startDate = run.start_date || params.start_date;
    if (startDate) {
      const el = document.getElementById('btStartDate');
      if (el) el.value = _formatForInput(startDate, false);
    }
    const endDate = run.end_date || params.end_date;
    if (endDate) {
      const el = document.getElementById('btEndDate');
      if (el) el.value = _formatForInput(endDate, true);
    }

    // 回测标题
    const title = run.title ?? params.title ?? '';
    const titleEl = document.getElementById('btTitle');
    if (titleEl) titleEl.value = title;

    // 确认K线
    const confirmCandles = params.confirm_candles;
    if (confirmCandles != null) {
      const el = document.getElementById('btConfirm');
      const label = document.getElementById('btConfirmLabel');
      if (el) el.value = confirmCandles;
      if (label) label.textContent = confirmCandles;
    }

    // 盈亏比
    const rr = params.rr_ratio;
    if (rr != null) {
      const el = document.getElementById('btRR');
      if (el) el.value = rr;
    }

    // 追踪触发 R & 保本缓冲 %
    const trailR = params.trailing_activate_r;
    if (trailR != null) {
      const el = document.getElementById('btTrailR');
      if (el) el.value = trailR;
    }
    const beBuf = params.breakeven_buffer;
    if (beBuf != null) {
      const el = document.getElementById('btBeBuf');
      if (el) el.value = Number((beBuf * 100).toFixed(3));
    }
    const trailMa = params.trailing_ma;
    if (trailMa) {
      const el = document.getElementById('btTrailingMa');
      if (el) el.value = String(trailMa).toLowerCase();
    }
    const stMode = params.strategy_mode;
    const modeEl = document.getElementById('btStrategyMode');
    if (modeEl && stMode) {
      modeEl.value = String(stMode).toLowerCase();
    }

    // 初始资金
    const cap = params.initial_capital;
    if (cap != null) {
      const el = document.getElementById('btCapital');
      if (el) el.value = cap;
    }
  }

  async function viewRun(id) {
    try {
      const res = await fetch(`/api/backtest/${id}`);
      if (!res.ok) return;
      const run = await res.json();
      fillParams(run);
      displayResults(run);
      document.getElementById('btResults').scrollIntoView({ behavior: 'smooth' });
      setTimeout(() => BacktestChart.resize(), 80);
      setTimeout(() => BacktestChart.resize(), 300);
    } catch (err) {
      console.error('加载回测详情失败', err);
    }
  }

  return { init, loadHistory, viewRun, deleteRun, clearAllHistory, syncSymbolParams };
})();
