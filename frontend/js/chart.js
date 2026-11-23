/**
 * chart.js — ECharts K线图 + MA均线 + 成交量 + 交易标注 (支持日间/夜间模式)
 */

function getThemeConfig() {
  const isLight = document.documentElement.getAttribute('data-bs-theme') === 'light';
  return {
    isLight,
    echartsTheme: isLight ? null : 'dark',
    bg: isLight ? '#ffffff' : '#0d1117',
    tooltipBg: isLight ? '#ffffff' : '#161b22',
    tooltipBorder: isLight ? '#d0d7de' : '#30363d',
    tooltipText: isLight ? '#1f2328' : '#e6edf3',
    axisLine: isLight ? '#d0d7de' : '#30363d',
    axisLabel: isLight ? '#656d76' : '#8b949e',
    splitLine: isLight ? '#eaeef2' : '#1c2128',
    sliderBorder: isLight ? '#d0d7de' : '#30363d',
    sliderFiller: isLight ? 'rgba(9,105,218,0.1)' : 'rgba(88,166,255,0.1)',
    sliderHandle: isLight ? '#0969da' : '#58a6ff',
    bullish: isLight ? '#1a7f37' : '#3fb950',
    bearish: isLight ? '#cf222e' : '#f85149',
    equityAreaStart: isLight ? 'rgba(9,105,218,0.25)' : 'rgba(88,166,255,0.3)',
    equityAreaEnd: isLight ? 'rgba(9,105,218,0.02)' : 'rgba(88,166,255,0.02)',
    equityLine: isLight ? '#0969da' : '#58a6ff'
  };
}

const ChartManager = (() => {
  let klineChart = null;
  let volumeChart = null;
  let currentData = { klines: [], trades: [] };
  let currentZoom = null;
  let onReachStartCallback = null;

  function setOnReachStart(callback) {
    onReachStartCallback = callback;
  }

  // ─── Init ───────────────────────────────────────────────────────────
  function init() {
    const klineEl  = document.getElementById('klineChart');
    const volEl    = document.getElementById('volumeChart');
    if (!klineEl || !volEl) return;

    const cfg = getThemeConfig();
    if (klineChart) {
      try { echarts.dispose(klineChart); } catch {}
      klineChart = null;
    }
    if (volumeChart) {
      try { echarts.dispose(volumeChart); } catch {}
      volumeChart = null;
    }

    klineChart  = echarts.init(klineEl,  cfg.echartsTheme, { renderer: 'canvas' });
    volumeChart = echarts.init(volEl,    cfg.echartsTheme, { renderer: 'canvas' });

    // Link zoom between charts and remember user's current zoom window
    klineChart.on('datazoom', (evt) => {
      let start = 0;
      let end = 100;
      if (evt.batch && evt.batch.length > 0) {
        currentZoom = { start: evt.batch[0].start, end: evt.batch[0].end };
        start = evt.batch[0].start ?? 0;
        end = evt.batch[0].end ?? 100;
      } else if (evt.start !== undefined && evt.end !== undefined) {
        currentZoom = { start: evt.start, end: evt.end };
        start = evt.start;
        end = evt.end;
      }
      volumeChart?.dispatchAction({ type: 'dataZoom', ...evt });

      // When user drags / scrolls to the leftmost edge (history start), trigger load more
      if (start <= 0.8 && onReachStartCallback) {
        onReachStartCallback();
      }
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
    currentData = { klines: klines || [], trades: trades || [] };

    if (!klines || !klines.length) {
      if (klineChart) klineChart.clear();
      if (volumeChart) volumeChart.clear();
      return;
    }

    // Support all candle formats: [t,o,h,l,c,v] or {time,open,high,low,close,volume} or {t,o,h,l,c,v}
    const norm = klines.map(k => {
      if (Array.isArray(k)) return k;
      const t = k.time ?? k.t ?? k.timestamp ?? 0;
      const o = k.open ?? k.o ?? 0;
      const h = k.high ?? k.h ?? 0;
      const l = k.low ?? k.l ?? 0;
      const c = k.close ?? k.c ?? 0;
      const v = k.volume ?? k.v ?? 0;
      return [t, o, h, l, c, v];
    });

    const times  = norm.map(k => {
      const d = new Date(k[0]);
      return isNaN(d.getTime()) ? String(k[0]) : d.toLocaleString('zh-CN', { hour12: false });
    });
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
    const cfg = getThemeConfig();
    const tooltip = {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: cfg.tooltipBg,
      borderColor: cfg.tooltipBorder,
      textStyle: { color: cfg.tooltipText, fontSize: 12 },
      formatter(params) {
        const i = params[0]?.dataIndex;
        if (i === undefined) return '';
        const k = norm[i];
        const o = +k[1], h = +k[2], l = +k[3], c = +k[4], v = +k[5];
        const pnl = c >= o ? `<span style="color:${cfg.bullish}">+${(c-o).toFixed(2)}</span>` : `<span style="color:${cfg.bearish}">${(c-o).toFixed(2)}</span>`;
        return `<div>
          <div style="color:${cfg.axisLabel};margin-bottom:4px">${times[i]}</div>
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
      backgroundColor: cfg.bg,
      animation: false,
      tooltip,
      grid: { left: 60, right: 10, top: 10, bottom: 60 },
      xAxis: {
        type: 'category',
        data: times,
        axisLine: { lineStyle: { color: cfg.axisLine } },
        axisLabel: { color: cfg.axisLabel, fontSize: 10, interval: Math.floor(norm.length / 8) },
        splitLine: { show: false }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLine: { lineStyle: { color: cfg.axisLine } },
        axisLabel: { color: cfg.axisLabel, fontSize: 10 },
        splitLine: { lineStyle: { color: cfg.splitLine } }
      },
      dataZoom: [
        {
          type: 'inside',
          start: currentZoom?.start ?? Math.max(0, 100 - 10000 / norm.length),
          end: currentZoom?.end ?? 100
        },
        {
          type: 'slider',
          height: 20,
          bottom: 0,
          start: currentZoom?.start ?? Math.max(0, 100 - 10000 / norm.length),
          end: currentZoom?.end ?? 100,
          borderColor: cfg.sliderBorder,
          fillerColor: cfg.sliderFiller,
          handleStyle: { color: cfg.sliderHandle },
          textStyle: { color: cfg.axisLabel, fontSize: 10 },
          labelFormatter: () => ''
        }
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: ohlc,
          itemStyle: {
            color: cfg.bullish,       // bullish
            color0: cfg.bearish,      // bearish
            borderColor: cfg.bullish,
            borderColor0: cfg.bearish
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
    const volColors = norm.map(k => +k[4] >= +k[1] ? cfg.bullish : cfg.bearish);
    volumeChart.setOption({
      backgroundColor: cfg.bg,
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: cfg.tooltipBg, borderColor: cfg.tooltipBorder, textStyle: { color: cfg.tooltipText, fontSize: 12 } },
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
  function highlightTrade(tradeOrId) {
    if (!klineChart) return;
    const { klines, trades } = currentData;
    let trade = tradeOrId;
    if (typeof tradeOrId === 'number' || typeof tradeOrId === 'string') {
      trade = (trades || []).find(t => t.id == tradeOrId);
    }
    if (!trade || !trade.open_time) return;

    const norm = klines.map(k => {
      if (Array.isArray(k)) return k;
      return [k.time ?? k.t ?? 0, k.open ?? k.o ?? 0, k.high ?? k.h ?? 0, k.low ?? k.l ?? 0, k.close ?? k.c ?? 0, k.volume ?? k.v ?? 0];
    });
    const entryTime = new Date(trade.open_time).getTime();
    const idx = norm.findIndex(k => Math.abs(k[0] - entryTime) < 90000);
    if (idx < 0) return;
    klineChart.dispatchAction({ type: 'dataZoom', startValue: Math.max(0, idx - 20), endValue: Math.min(norm.length - 1, idx + 20) });
  }

  // ─── Append a new live candle / update last from WebSocket ─────────
  function appendCandle(candle) {
    if (!candle) return;
    if (!currentData.klines || !currentData.klines.length) {
      render([candle], currentData.trades);
      return;
    }
    const { klines } = currentData;
    const last = klines[klines.length - 1];

    const t = candle.time ?? candle.t ?? candle.timestamp ?? (Array.isArray(candle) ? candle[0] : 0);
    const o = +(candle.open ?? candle.o ?? (Array.isArray(candle) ? candle[1] : 0));
    const h = +(candle.high ?? candle.h ?? (Array.isArray(candle) ? candle[2] : 0));
    const l = +(candle.low ?? candle.l ?? (Array.isArray(candle) ? candle[3] : 0));
    const c = +(candle.close ?? candle.c ?? (Array.isArray(candle) ? candle[4] : 0));
    const v = +(candle.volume ?? candle.v ?? (Array.isArray(candle) ? candle[5] : 0));

    const candleTime = typeof t === 'string' ? new Date(t).getTime() : Number(t);
    const rawLastTime = Array.isArray(last) ? last[0] : (last.time ?? last.t);
    const lastTime = typeof rawLastTime === 'string' ? new Date(rawLastTime).getTime() : Number(rawLastTime);

    // If candle belongs to same period/timestamp, update it in-place
    if (lastTime && Math.abs(candleTime - lastTime) < 30000) {
      if (Array.isArray(last)) {
        last[1] = o;
        last[2] = Math.max(+last[2], h);
        last[3] = Math.min(+last[3], l);
        last[4] = c;
        last[5] = v;
      } else {
        last.high = Math.max(last.high ?? h, h);
        last.low = Math.min(last.low ?? l, l);
        last.close = c;
        last.volume = v;
      }
    } else if (candleTime > lastTime) {
      // New candle opened, append and keep window size (allow up to 2000 candles for smooth history navigation)
      klines.push([candleTime, o, h, l, c, v]);
      if (klines.length > 2000) klines.shift();
    }

    render(klines, currentData.trades);
  }

  // ─── Prepend older history candles (Lazy-loading on scroll to start) 
  function prependHistory(olderBars) {
    if (!olderBars || !olderBars.length) return false;
    if (!currentData.klines || !currentData.klines.length) {
      render(olderBars, currentData.trades);
      return true;
    }

    const firstCandle = currentData.klines[0];
    const firstTime = Array.isArray(firstCandle) ? firstCandle[0] : (firstCandle.time ?? firstCandle.t ?? 0);
    const firstTimeMs = typeof firstTime === 'string' ? new Date(firstTime).getTime() : Number(firstTime);

    // Only keep bars older than our earliest candle
    const validOlder = olderBars.filter(bar => {
      const t = Array.isArray(bar) ? bar[0] : (bar.time ?? bar.t ?? 0);
      const ms = typeof t === 'string' ? new Date(t).getTime() : Number(t);
      return ms < firstTimeMs;
    });

    if (!validOlder.length) return false;

    const oldLen = currentData.klines.length;
    const addLen = validOlder.length;
    const newLen = oldLen + addLen;

    // Prepend older candles
    currentData.klines = [...validOlder, ...currentData.klines];

    // Maintain current visible window position smoothly without jump
    const oldStart = currentZoom?.start ?? 0;
    const oldEnd = currentZoom?.end ?? 100;
    const idxStart = (oldStart / 100) * oldLen + addLen;
    const idxEnd = (oldEnd / 100) * oldLen + addLen;

    currentZoom = {
      start: Math.max(0, Math.min(100, (idxStart / newLen) * 100)),
      end: Math.max(0, Math.min(100, (idxEnd / newLen) * 100))
    };

    render(currentData.klines, currentData.trades);
    return true;
  }

  // ─── Update theme ──────────────────────────────────────────────────
  function updateTheme() {
    init();
    if (currentData.klines && currentData.klines.length) {
      render(currentData.klines, currentData.trades);
    }
  }

  return { init, render, highlightTrade, appendCandle, prependHistory, setOnReachStart, updateTheme };
})();

// ─── Stats charts ─────────────────────────────────────────────────────
const StatsCharts = (() => {
  let dailyChart = null;
  let symbolChart = null;
  let lastDaily = null;
  let lastSymbol = null;

  function renderDaily(dates, pnls) {
    lastDaily = { dates, pnls };
    const el = document.getElementById('dailyPnlChart');
    if (!el) return;
    const cfg = getThemeConfig();
    if (!dailyChart) dailyChart = echarts.init(el, cfg.echartsTheme);
    dailyChart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: cfg.tooltipBg, borderColor: cfg.tooltipBorder, textStyle: { color: cfg.tooltipText } },
      grid: { left: 50, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: cfg.axisLabel, fontSize: 10, rotate: 30 }, axisLine: { lineStyle: { color: cfg.axisLine } } },
      yAxis: { type: 'value', axisLabel: { color: cfg.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: cfg.splitLine } } },
      series: [{
        type: 'bar',
        data: pnls.map(v => ({ value: v, itemStyle: { color: v >= 0 ? cfg.bullish : cfg.bearish } })),
        barMaxWidth: 20
      }]
    });
  }

  function renderSymbol(symbols, pnls) {
    lastSymbol = { symbols, pnls };
    const el = document.getElementById('symbolPnlChart');
    if (!el) return;
    const cfg = getThemeConfig();
    if (!symbolChart) symbolChart = echarts.init(el, cfg.echartsTheme);
    symbolChart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: cfg.tooltipBg, borderColor: cfg.tooltipBorder, textStyle: { color: cfg.tooltipText } },
      grid: { left: 60, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'value', axisLabel: { color: cfg.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: cfg.splitLine } } },
      yAxis: { type: 'category', data: symbols, axisLabel: { color: cfg.axisLabel, fontSize: 10 } },
      series: [{
        type: 'bar',
        data: pnls.map(v => ({ value: v, itemStyle: { color: v >= 0 ? cfg.bullish : cfg.bearish } })),
        barMaxWidth: 20
      }]
    });
  }

  function updateTheme() {
    if (dailyChart) {
      try { echarts.dispose(dailyChart); } catch {}
      dailyChart = null;
    }
    if (symbolChart) {
      try { echarts.dispose(symbolChart); } catch {}
      symbolChart = null;
    }
    if (lastDaily) renderDaily(lastDaily.dates, lastDaily.pnls);
    if (lastSymbol) renderSymbol(lastSymbol.symbols, lastSymbol.pnls);
  }

  function resize() {
    dailyChart?.resize();
    symbolChart?.resize();
  }

  window.addEventListener('resize', resize);
  return { renderDaily, renderSymbol, updateTheme };
})();

// ─── Backtest equity chart ─────────────────────────────────────────────
const BacktestChart = (() => {
  let chart = null;
  let lastData = null;

  function render(dates, equity) {
    lastData = { dates, equity };
    const el = document.getElementById('btEquityChart');
    if (!el) return;
    const cfg = getThemeConfig();

    if (!chart) {
      chart = echarts.init(el, cfg.echartsTheme);
    } else {
      chart.resize();
    }

    chart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: cfg.tooltipBg, borderColor: cfg.tooltipBorder, textStyle: { color: cfg.tooltipText } },
      grid: { left: 60, right: 10, top: 10, bottom: 40 },
      xAxis: { type: 'category', data: dates, axisLabel: { color: cfg.axisLabel, fontSize: 10, rotate: 30 }, axisLine: { lineStyle: { color: cfg.axisLine } } },
      yAxis: { type: 'value', axisLabel: { color: cfg.axisLabel, fontSize: 10 }, splitLine: { lineStyle: { color: cfg.splitLine } } },
      series: [{
        type: 'line', data: equity, smooth: true, symbol: 'none',
        lineStyle: { color: cfg.equityLine, width: 2 },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: cfg.equityAreaStart }, { offset: 1, color: cfg.equityAreaEnd }] } }
      }]
    });

    // Ensure chart takes full available container dimensions after render / tab switch
    requestAnimationFrame(() => chart?.resize());
    setTimeout(() => chart?.resize(), 50);
    setTimeout(() => chart?.resize(), 250);
  }

  function resize() {
    if (chart) {
      chart.resize();
    } else if (lastData) {
      render(lastData.dates, lastData.equity);
    }
  }

  function updateTheme() {
    if (chart) {
      try { echarts.dispose(chart); } catch {}
      chart = null;
    }
    if (lastData) render(lastData.dates, lastData.equity);
  }

  window.addEventListener('resize', () => chart?.resize());
  return { render, updateTheme, resize };
})();
