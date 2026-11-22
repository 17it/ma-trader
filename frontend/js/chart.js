/**
 * chart.js — ECharts K线图 + MA均线 + 成交量 + 交易标注
 */

const ChartManager = (() => {
  let klineChart = null;
  let volumeChart = null;
  let currentData = { klines: [], trades: [] };

  // ─── Init ───────────────────────────────────────────────────────────
  function init() {
    const klineEl  = document.getElementById('klineChart');
    const volEl    = document.getElementById('volumeChart');
    if (!klineEl || !volEl) return;

    klineChart  = echarts.init(klineEl,  'dark', { renderer: 'canvas' });
    volumeChart = echarts.init(volEl,    'dark', { renderer: 'canvas' });

    // Link zoom between charts
    klineChart.on('datazoom', (evt) => {
      volumeChart.dispatchAction({ type: 'dataZoom', ...evt });
    });

    window.addEventListener('resize', () => {
      klineChart?.resize();
      volumeChart?.resize();
    });
  }

  // ─── Helper: compute MA ─────────────────────────────────────────────
  function calcMA(data, period) {
    return data.map((_, i) => {
      if (i < period - 1) return '-';
      const slice = data.slice(i - period + 1, i + 1);
      return +(slice.reduce((s, c) => s + c[1], 0) / period).toFixed(4);
    });
  }

  // ─── Render klines ──────────────────────────────────────────────────
  function render(klines, trades = []) {
    if (!klineChart) init();
    currentData = { klines, trades };

    // klines = [{t, o, h, l, c, v}, ...]  OR  [[t,o,h,l,c,v], ...]
    const norm = klines.map(k => Array.isArray(k) ? k : [k.t, k.o, k.h, k.l, k.c, k.v]);

    const times  = norm.map(k => new Date(k[0]).toLocaleString('zh-CN', { hour12: false }));
    const ohlc   = norm.map(k => [+k[1], +k[4], +k[3], +k[2]]);  // ECharts: [o,c,l,h]
    const vols   = norm.map(k => +k[5]);
    const closes = norm.map(k => +k[4]);

    const ma7  = calcMA(norm.map(k => [0, +k[4]]), 7);
    const ma25 = calcMA(norm.map(k => [0, +k[4]]), 25);
    const ma60 = calcMA(norm.map(k => [0, +k[4]]), 60);

    // ── Trade markers ─────────────────────────────────────────────────
    const markPoints = { data: [] };
    if (trades && trades.length) {
      trades.forEach(tr => {
        const entryTime = new Date(tr.open_time).getTime();
        const idx = norm.findIndex(k => Math.abs(k[0] - entryTime) < 90000);
        if (idx < 0) return;

        if (tr.direction === 'long') {
          markPoints.data.push({
            coord: [idx, +norm[idx][3] * 0.9995],
            name: `▲ 开多 #${tr.id}`,
            symbol: 'triangle',
            symbolSize: 12,
            itemStyle: { color: '#3fb950' },
            label: { show: false }
          });
        } else if (tr.direction === 'short') {
          markPoints.data.push({
            coord: [idx, +norm[idx][2] * 1.0005],
            name: `▼ 开空 #${tr.id}`,
            symbol: 'triangle',
            symbolSize: 12,
            symbolRotate: 180,
            itemStyle: { color: '#f85149' },
            label: { show: false }
          });
        }

        // close marker
        if (tr.close_time) {
          const closeTime = new Date(tr.close_time).getTime();
          const ci = norm.findIndex(k => Math.abs(k[0] - closeTime) < 90000);
          if (ci >= 0) {
            markPoints.data.push({
              coord: [ci, +norm[ci][4]],
              name: `× 平仓 #${tr.id}`,
              symbol: 'circle',
              symbolSize: 8,
              itemStyle: { color: '#58a6ff' },
              label: { show: false }
            });
          }
        }
      });
    }

    // ── Tooltip formatter ─────────────────────────────────────────────
    const tooltip = {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#161b22',
      borderColor: '#30363d',
      textStyle: { color: '#e6edf3', fontSize: 12 },
      formatter(params) {
        const i = params[0]?.dataIndex;
        if (i === undefined) return '';
        const k = norm[i];
        const o = +k[1], h = +k[2], l = +k[3], c = +k[4], v = +k[5];
        const pnl = c >= o ? `<span style="color:#3fb950">+${(c-o).toFixed(2)}</span>` : `<span style="color:#f85149">${(c-o).toFixed(2)}</span>`;
        return `<div>
          <div style="color:#8b949e;margin-bottom:4px">${times[i]}</div>
          <div>开 <b>${o}</b>  高 <b>${h}</b>  低 <b>${l}</b>  收 <b>${c}</b> ${pnl}</div>
          <div>量 <b>${v.toFixed(2)}</b></div>
          <div style="margin-top:4px">
            <span style="color:#f0c040">MA7: ${ma7[i] !== '-' ? ma7[i] : '—'}</span> &nbsp;
            <span style="color:#e77ab4">MA25: ${ma25[i] !== '-' ? ma25[i] : '—'}</span> &nbsp;
            <span style="color:#9b6dd6">MA60: ${ma60[i] !== '-' ? ma60[i] : '—'}</span>
          </div>
        </div>`;
      }
    };

    // ── Kline chart option ────────────────────────────────────────────
    klineChart.setOption({
      backgroundColor: '#0d1117',
      animation: false,
      tooltip,
      grid: { left: 60, right: 10, top: 10, bottom: 60 },
      xAxis: {
        type: 'category',
        data: times,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { color: '#8b949e', fontSize: 10, interval: Math.floor(norm.length / 8) },
        splitLine: { show: false }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLine: { lineStyle: { color: '#30363d' } },
        axisLabel: { color: '#8b949e', fontSize: 10 },
        splitLine: { lineStyle: { color: '#1c2128' } }
      },
      dataZoom: [
        { type: 'inside', start: Math.max(0, 100 - 10000 / norm.length), end: 100 },
        { type: 'slider', height: 20, bottom: 0, borderColor: '#30363d',
          fillerColor: 'rgba(88,166,255,0.1)', handleStyle: { color: '#58a6ff' },
          textStyle: { color: '#8b949e', fontSize: 10 }, labelFormatter: () => '' }
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: ohlc,
          itemStyle: {
            color: '#3fb950',       // bullish
            color0: '#f85149',      // bearish
            borderColor: '#3fb950',
            borderColor0: '#f85149'
          },
          markPoint: markPoints
        },
        {
          name: 'MA7', type: 'line', data: ma7,
          smooth: true, symbol: 'none', lineStyle: { color: '#f0c040', width: 1.2 }
        },
        {
          name: 'MA25', type: 'line', data: ma25,
          smooth: true, symbol: 'none', lineStyle: { color: '#e77ab4', width: 1.2 }
        },
        {
          name: 'MA60', type: 'line', data: ma60,
          smooth: true, symbol: 'none', lineStyle: { color: '#9b6dd6', width: 1.2 }
        }
      ]
    }, true);

    // ── Volume chart ──────────────────────────────────────────────────
    const volColors = norm.map(k => +k[4] >= +k[1] ? '#3fb950' : '#f85149');
    volumeChart.setOption({
      backgroundColor: '#0d1117',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: { color: '#e6edf3', fontSize: 12 } },
      grid: { left: 60, right: 10, top: 4, bottom: 4 },
      xAxis: { type: 'category', data: times, show: false },
      yAxis: { type: 'value', show: false, scale: false },
      dataZoom: [{ type: 'inside', start: Math.max(0, 100 - 10000 / norm.length), end: 100 }],
      series: [{
        type: 'bar',
        data: vols,
        itemStyle: { color: (p) => volColors[p.dataIndex] },
        barMaxWidth: 6
      }]
    }, true);
  }

  // ─── Highlight a trade's candle ─────────────────────────────────────
  function highlightTrade(trade) {
    if (!klineChart) return;
    const { klines } = currentData;
    const norm = klines.map(k => Array.isArray(k) ? k : [k.t, k.o, k.h, k.l, k.c, k.v]);
    const entryTime = new Date(trade.open_time).getTime();
    const idx = norm.findIndex(k => Math.abs(k[0] - entryTime) < 90000);
    if (idx < 0) return;
    klineChart.dispatchAction({ type: 'dataZoom', startValue: Math.max(0, idx - 20), endValue: Math.min(norm.length - 1, idx + 20) });
  }

  // ─── Append a new live candle / update last ─────────────────────────
  function appendCandle(candle) {
    // candle: {t,o,h,l,c,v}
    const { klines } = currentData;
    const last = klines[klines.length - 1];
    const candleTime = typeof candle.t === 'string' ? new Date(candle.t).getTime() : candle.t;
    if (last && Math.abs((Array.isArray(last) ? last[0] : last.t) - candleTime) < 90000) {
      klines[klines.length - 1] = candle;
    } else {
      klines.push(candle);
      if (klines.length > 500) klines.shift();
    }
    render(klines, currentData.trades);
  }

  return { init, render, highlightTrade, appendCandle };
})();

// ─── Stats charts ─────────────────────────────────────────────────────
const StatsCharts = (() => {
  let dailyChart = null;
  let symbolChart = null;

  function renderDaily(dates, pnls) {
    if (!dailyChart) dailyChart = echarts.init(document.getElementById('dailyPnlChart'), 'dark');
    dailyChart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: { color: '#e6edf3' } },
      grid: { left: 50, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: '#8b949e', fontSize: 10, rotate: 30 }, axisLine: { lineStyle: { color: '#30363d' } } },
      yAxis: { type: 'value', axisLabel: { color: '#8b949e', fontSize: 10 }, splitLine: { lineStyle: { color: '#1c2128' } } },
      series: [{
        type: 'bar',
        data: pnls.map(v => ({ value: v, itemStyle: { color: v >= 0 ? '#3fb950' : '#f85149' } })),
        barMaxWidth: 20
      }]
    });
  }

  function renderSymbol(symbols, pnls) {
    if (!symbolChart) symbolChart = echarts.init(document.getElementById('symbolPnlChart'), 'dark');
    symbolChart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: { color: '#e6edf3' } },
      grid: { left: 60, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'value', axisLabel: { color: '#8b949e', fontSize: 10 }, splitLine: { lineStyle: { color: '#1c2128' } } },
      yAxis: { type: 'category', data: symbols, axisLabel: { color: '#8b949e', fontSize: 10 } },
      series: [{
        type: 'bar',
        data: pnls.map(v => ({ value: v, itemStyle: { color: v >= 0 ? '#3fb950' : '#f85149' } })),
        barMaxWidth: 20
      }]
    });
  }

  function resize() {
    dailyChart?.resize();
    symbolChart?.resize();
  }

  window.addEventListener('resize', resize);
  return { renderDaily, renderSymbol };
})();

// ─── Backtest equity chart ─────────────────────────────────────────────
const BacktestChart = (() => {
  let chart = null;

  function render(dates, equity) {
    if (!chart) chart = echarts.init(document.getElementById('btEquityChart'), 'dark');
    chart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: '#161b22', borderColor: '#30363d', textStyle: { color: '#e6edf3' } },
      grid: { left: 60, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: '#8b949e', fontSize: 10, rotate: 30 }, axisLine: { lineStyle: { color: '#30363d' } } },
      yAxis: { type: 'value', axisLabel: { color: '#8b949e', fontSize: 10 }, splitLine: { lineStyle: { color: '#1c2128' } } },
      series: [{
        type: 'line', data: equity, smooth: true, symbol: 'none',
        lineStyle: { color: '#58a6ff', width: 2 },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(88,166,255,0.3)' }, { offset: 1, color: 'rgba(88,166,255,0.02)' }] } }
      }]
    });
  }

  window.addEventListener('resize', () => chart?.resize());
  return { render };
})();
