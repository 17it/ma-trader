/**
 * backtest.js — 回测面板逻辑
 */

const BacktestManager = (() => {

  function init() {
    // confirm slider label
    document.getElementById('btConfirm').addEventListener('input', (e) => {
      document.getElementById('btConfirmLabel').textContent = e.target.value;
    });

    // run backtest
    document.getElementById('btnRunBacktest').addEventListener('click', runBacktest);

    // load history on tab show
    document.querySelector('a[href="#tabBacktest"]').addEventListener('shown.bs.tab', loadHistory);
  }

  // ─── Run backtest ─────────────────────────────────────────────────────
  async function runBacktest() {
    const payload = {
      symbol:        document.getElementById('btSymbol').value,
      timeframe:     document.getElementById('btTimeframe').value,
      htf_timeframe: document.getElementById('btHtf').value,
      start_date:    document.getElementById('btStartDate').value,
      end_date:      document.getElementById('btEndDate').value,
      confirm_candles: parseInt(document.getElementById('btConfirm').value),
      rr_ratio:      parseFloat(document.getElementById('btRR').value),
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
    if (attempt > 60) throw new Error('回测超时');
    await new Promise(r => setTimeout(r, 2000));
    const res = await fetch(`/api/backtest/${id}`);
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
    const pnlColor  = results.total_pnl >= 0 ? 'text-green' : 'text-red';
    const ddColor   = 'text-red';

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
          <div class="bt-metric-value text-green">${results.win_rate != null ? (results.win_rate * 100).toFixed(1) + '%' : '—'}</div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">总盈亏</div>
          <div class="bt-metric-value ${pnlColor}">${results.total_pnl != null ? (results.total_pnl >= 0 ? '+' : '') + '$' + results.total_pnl.toFixed(2) : '—'}
            <small style="font-size:12px">(${results.pnl_pct != null ? (results.pnl_pct >= 0 ? '+' : '') + results.pnl_pct.toFixed(1) + '%' : ''})</small>
          </div>
        </div>
      </div>
      <div class="col-6 col-md-3">
        <div class="bt-metric-card">
          <div class="bt-metric-label">最大回撤</div>
          <div class="bt-metric-value ${ddColor}">${results.max_drawdown != null ? results.max_drawdown.toFixed(1) + '%' : '—'}</div>
        </div>
      </div>
    `;

    // Equity curve
    if (results.equity_curve && results.equity_curve.length) {
      const dates  = results.equity_curve.map(e => e.date || e.t);
      const equity = results.equity_curve.map(e => e.equity || e.v);
      BacktestChart.render(dates, equity);
    }

    // Trade list
    const tbody = document.getElementById('btTradeBody');
    tbody.innerHTML = '';
    (results.trades || []).forEach(tr => {
      const pnlColor = (tr.pnl || 0) >= 0 ? '#3fb950' : '#f85149';
      const dirIcon  = tr.direction === 'long' ? '<span style="color:#3fb950">▲ 多</span>' : '<span style="color:#f85149">▼ 空</span>';
      const tr2 = document.createElement('tr');
      tr2.innerHTML = `
        <td>${dirIcon}</td>
        <td>${tr.entry_price?.toFixed(4) ?? '—'}</td>
        <td>${tr.exit_price?.toFixed(4) ?? '—'}</td>
        <td style="color:${pnlColor}">${tr.pnl != null ? (tr.pnl >= 0 ? '+' : '') + tr.pnl.toFixed(2) : '—'}</td>
      `;
      tbody.appendChild(tr2);
    });

    document.getElementById('btResults').style.display = 'block';
  }

  // ─── History ──────────────────────────────────────────────────────────
  async function loadHistory() {
    try {
      const res = await fetch('/api/backtest?limit=20');
      if (!res.ok) return;
      const runs = await res.json();
      renderHistory(runs);
    } catch (err) {
      console.warn('加载历史回测失败', err);
    }
  }

  function renderHistory(runs) {
    const tbody = document.getElementById('btHistoryBody');
    tbody.innerHTML = '';
    if (!runs || runs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">暂无历史记录</td></tr>';
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
      }[run.status] || run.status;

      const pnl    = results?.total_pnl;
      const pnlStr = pnl != null ? `<span style="color:${pnl >= 0 ? '#3fb950' : '#f85149'}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}</span>` : '—';
      const wr     = results?.win_rate;
      const wrStr  = wr != null ? (wr * 100).toFixed(1) + '%' : '—';

      const tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.innerHTML = `
        <td>#${run.id}</td>
        <td>${run.symbol}</td>
        <td>${run.timeframe}</td>
        <td>${run.start_date} ~ ${run.end_date}</td>
        <td>${statusBadge}</td>
        <td>${pnlStr}</td>
        <td>${wrStr}</td>
        <td><button class="btn btn-xs btn-outline-secondary" onclick="BacktestManager.viewRun(${run.id})">查看</button></td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function viewRun(id) {
    try {
      const res = await fetch(`/api/backtest/${id}`);
      if (!res.ok) return;
      const run = await res.json();
      displayResults(run);
      document.getElementById('btResults').scrollIntoView({ behavior: 'smooth' });
    } catch (err) {
      console.error('加载回测详情失败', err);
    }
  }

  return { init, loadHistory, viewRun };
})();
