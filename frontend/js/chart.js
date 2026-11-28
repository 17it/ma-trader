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
  let chartResizeObserver = null;
  let resizeRafId = null;

  function setOnReachStart(callback) {
    onReachStartCallback = callback;
  }

  function resize() {
    const klineEl = document.getElementById('klineChart');
    const volEl   = document.getElementById('volumeChart');
    if (klineEl && klineEl.clientWidth > 0 && klineEl.clientHeight > 0) {
      klineChart?.resize();
    }
    if (volEl && volEl.clientWidth > 0 && volEl.clientHeight > 0) {
      volumeChart?.resize();
    }
  }

  function _scheduleResize() {
    if (resizeRafId) cancelAnimationFrame(resizeRafId);
    resizeRafId = requestAnimationFrame(() => {
      resizeRafId = null;
      resize();
    });
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

    if (!chartResizeObserver && typeof ResizeObserver !== 'undefined') {
      chartResizeObserver = new ResizeObserver(() => {
        _scheduleResize();
      });
      chartResizeObserver.observe(klineEl);
      if (klineEl.parentElement) {
        chartResizeObserver.observe(klineEl.parentElement);
      }
    }

    window.addEventListener('resize', _scheduleResize);
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

    // Ensure ECharts canvas matches actual flex container dimensions even if init ran before layout settled
    const klineEl = document.getElementById('klineChart');
    if (klineEl && klineChart && (klineChart.getWidth() !== klineEl.clientWidth || klineChart.getHeight() !== klineEl.clientHeight)) {
      _scheduleResize();
    }
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

  return { init, render, resize, highlightTrade, appendCandle, prependHistory, setOnReachStart, updateTheme };
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
    else dailyChart.resize();
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
    requestAnimationFrame(() => dailyChart?.resize());
    setTimeout(() => dailyChart?.resize(), 60);
  }

  function renderSymbol(symbols, pnls) {
    lastSymbol = { symbols, pnls };
    const el = document.getElementById('symbolPnlChart');
    if (!el) return;
    const cfg = getThemeConfig();
    if (!symbolChart) symbolChart = echarts.init(el, cfg.echartsTheme);
    else symbolChart.resize();
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
    requestAnimationFrame(() => symbolChart?.resize());
    setTimeout(() => symbolChart?.resize(), 60);
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
  return { renderDaily, renderSymbol, updateTheme, resize };
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

// ─── Trade & Backtest K-line Modal (Binance-style B/S Badges + Trade Jumper) ──
const TradeKlineModal = (() => {
  let modalInstance = null;
  let mainChart = null;
  let volChart = null;
  let resizeObs = null;

  const TF_MS = {
    '1m': 60_000,
    '3m': 180_000,
    '5m': 300_000,
    '15m': 900_000,
    '30m': 1_800_000,
    '1h': 3_600_000,
    '2h': 7_200_000,
    '4h': 14_400_000,
    '1d': 86_400_000,
  };

  // Speech-bubble SVG paths matching Binance B (bottom-up pointer) & S (top-down pointer)
  const PIN_BUY_SVG  = 'path://M0,-10 L3.6,-5 L6,-5 Q10,-5 10,-1 L10,9 Q10,13 6,13 L-6,13 Q-10,13 -10,9 L-10,-1 Q-10,-5 -6,-5 L-3.6,-5 Z';
  const PIN_SELL_SVG = 'path://M-6,-13 L6,-13 Q10,-13 10,-9 L10,1 Q10,5 6,5 L3.6,5 L0,10 L-3.6,5 L-6,5 Q-10,5 -10,1 L-10,-9 Q-10,-13 -6,-13 Z';

  const REASON_LABELS = {
    stop_loss: '初始止损',
    profit_lock_stop: '阶梯锁盈',
    trailing_stop: '移动止损',
    ma7_ma25_cross: 'MA7/25交叉',
    ma7_trailing: 'MA7止盈',
    ma25_trailing: 'MA25止盈',
    ma60_trailing: 'MA60止盈',
    htf_ma25_trailing: '大周期MA25止盈',
    htf_ma60_trailing: '大周期MA60止盈',
    take_profit: '目标止盈',
    exchange_closed: '交易所平仓',
    archived: '存档结算',
    manual: '手动平仓',
    end_of_backtest: '回测结束结算',
  };

  const st = {
    title: '',
    allTrades: [],
    symbols: [],
    symbol: 'BTCUSDT',
    timeframe: '1m',
    baseRangeStartMs: null,
    baseRangeEndMs: null,
    bars: [],           // [[ts, o, h, l, c, v, ma7, ma25, ma60], ...]
    hasMoreLeft: false,
    requestedStartMs: 0,
    loadingMore: false,
    selectedTradeIdx: -1,
    zoomRange: null,    // { startValue, endValue } or null
  };

  function _esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _parseUtcMs(raw) {
    if (!raw) return NaN;
    if (typeof raw === 'number') return raw;
    const s = String(raw).trim();
    const norm = (s.includes('Z') || s.includes('+') || (s.lastIndexOf('-') > 9))
      ? s
      : (s.replace(' ', 'T') + 'Z');
    const ms = new Date(norm).getTime();
    return isNaN(ms) ? new Date(s).getTime() : ms;
  }

  function _fmtTime(ms) {
    if (!ms || isNaN(ms)) return '—';
    const d = new Date(ms);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function _normalizeTrades(trades, defaultSymbol) {
    return (trades || []).map((t, idx) => {
      const entryMs = _parseUtcMs(t.open_time || t.entry_time || t.time);
      const exitMs  = _parseUtcMs(t.close_time || t.exit_time);
      const dir = String(t.direction || 'long').toLowerCase() === 'short' ? 'short' : 'long';
      const status = t.status || (exitMs && !isNaN(exitMs) ? 'closed' : 'open');
      return {
        uid: idx,
        displayId: t.id != null ? `#${t.id}` : `#${idx + 1}`,
        symbol: String(t.symbol || defaultSymbol || 'BTCUSDT').toUpperCase(),
        direction: dir,
        entryMs,
        exitMs: status === 'open' ? NaN : exitMs,
        entryPrice: Number(t.entry_price ?? 0),
        exitPrice: t.exit_price != null ? Number(t.exit_price) : null,
        pnl: t.pnl != null ? Number(t.pnl) : null,
        pnlPct: t.pnl_pct != null ? Number(t.pnl_pct) : null,
        closeReason: t.close_reason || '',
        strategyMode: t.strategy_mode || '',
        status,
      };
    }).filter(t => !isNaN(t.entryMs))
      .sort((a, b) => a.entryMs - b.entryMs);
  }

  function _filteredTrades() {
    return st.allTrades.filter(t => t.symbol === st.symbol);
  }

  function _ensureCharts() {
    const mainEl = document.getElementById('klineModalMainChart');
    const volEl  = document.getElementById('klineModalVolChart');
    if (!mainEl || !volEl) return;
    const cfg = getThemeConfig();

    if (!mainChart) {
      mainChart = echarts.init(mainEl, cfg.echartsTheme, { renderer: 'canvas' });
      volChart  = echarts.init(volEl, cfg.echartsTheme, { renderer: 'canvas' });

      mainChart.on('datazoom', (evt) => {
        volChart?.dispatchAction({ type: 'dataZoom', ...evt });
        let startPct = evt.batch?.[0]?.start ?? evt.start;
        if (startPct !== undefined && startPct <= 1.5 && st.hasMoreLeft && !st.loadingMore) {
          _loadOlderHistory();
        }
      });

      if (!resizeObs && typeof ResizeObserver !== 'undefined') {
        resizeObs = new ResizeObserver(() => {
          if (mainEl.clientWidth > 0 && mainEl.clientHeight > 0) mainChart?.resize();
          if (volEl.clientWidth > 0 && volEl.clientHeight > 0) volChart?.resize();
        });
        resizeObs.observe(mainEl);
      }
    }
  }

  function _disposeCharts() {
    if (mainChart) { try { echarts.dispose(mainChart); } catch {} mainChart = null; }
    if (volChart)  { try { echarts.dispose(volChart); } catch {} volChart = null; }
  }

  function _bindDomOnce() {
    const modalEl = document.getElementById('tradeKlineModal');
    if (!modalEl || modalEl.dataset.bound === '1') return;
    modalEl.dataset.bound = '1';

    modalEl.addEventListener('shown.bs.modal', () => {
      _ensureCharts();
      mainChart?.resize();
      volChart?.resize();
      if (st.bars.length > 0) _renderChart();
    });

    const symSel = document.getElementById('klineModalSymbol');
    if (symSel) {
      symSel.addEventListener('change', (e) => {
        st.symbol = e.target.value;
        st.selectedTradeIdx = -1;
        st.zoomRange = null;
        _populateTradeSelector();
        _loadWindowForSymbol();
      });
    }

    document.querySelectorAll('#klineModalTfBtns button[data-km-tf]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tf = btn.getAttribute('data-km-tf');
        if (!tf || tf === st.timeframe) return;
        st.timeframe = tf;
        st.zoomRange = null;
        _syncTfButtons();
        _loadWindowForSymbol();
      });
    });

    const tradeSel = document.getElementById('klineModalTradeSelect');
    if (tradeSel) {
      tradeSel.addEventListener('change', (e) => {
        const idx = parseInt(e.target.value, 10);
        if (isNaN(idx) || idx < 0) {
          st.selectedTradeIdx = -1;
          _resetGlobalZoom();
        } else {
          jumpToTradeIndex(idx);
        }
      });
    }

    document.getElementById('btnKmPrevTrade')?.addEventListener('click', () => {
      const list = _filteredTrades();
      if (!list.length) return;
      const nextIdx = st.selectedTradeIdx <= 0 ? list.length - 1 : st.selectedTradeIdx - 1;
      jumpToTradeIndex(nextIdx);
    });

    document.getElementById('btnKmNextTrade')?.addEventListener('click', () => {
      const list = _filteredTrades();
      if (!list.length) return;
      const nextIdx = (st.selectedTradeIdx < 0 || st.selectedTradeIdx >= list.length - 1) ? 0 : st.selectedTradeIdx + 1;
      jumpToTradeIndex(nextIdx);
    });

    document.getElementById('btnKmResetZoom')?.addEventListener('click', () => {
      st.selectedTradeIdx = -1;
      const sel = document.getElementById('klineModalTradeSelect');
      if (sel) sel.value = '-1';
      _resetGlobalZoom();
    });
  }

  function _syncTfButtons() {
    document.querySelectorAll('#klineModalTfBtns button[data-km-tf]').forEach(b => {
      const active = b.getAttribute('data-km-tf') === st.timeframe;
      b.className = active ? 'btn btn-xs btn-primary active' : 'btn btn-xs btn-outline-secondary';
    });
  }

  function _populateSymbolSelector() {
    const sel = document.getElementById('klineModalSymbol');
    if (!sel) return;
    sel.innerHTML = st.symbols.map(s => `<option value="${_esc(s)}">${_esc(s)}</option>`).join('');
    sel.value = st.symbol;
    sel.disabled = st.symbols.length <= 1;
  }

  function _populateTradeSelector() {
    const sel = document.getElementById('klineModalTradeSelect');
    if (!sel) return;
    const list = _filteredTrades();
    const opts = [`<option value="-1">全部买卖点 (共 ${list.length} 笔)</option>`];
    list.forEach((t, i) => {
      const dirTxt = t.direction === 'long' ? '多' : '空';
      const pnlTxt = t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '持仓中';
      opts.push(`<option value="${i}">${_esc(t.displayId)} ${dirTxt} ${_fmtTime(t.entryMs)} (${pnlTxt})</option>`);
    });
    sel.innerHTML = opts.join('');
    sel.value = String(st.selectedTradeIdx);

    const prevBtn = document.getElementById('btnKmPrevTrade');
    const nextBtn = document.getElementById('btnKmNextTrade');
    if (prevBtn) prevBtn.disabled = list.length === 0;
    if (nextBtn) nextBtn.disabled = list.length === 0;
  }

  function _setLoading(loading, errMsg = '') {
    const overlay = document.getElementById('klineModalLoading');
    if (!overlay) return;
    if (loading) {
      overlay.classList.remove('d-none');
      overlay.classList.add('d-flex');
      overlay.innerHTML = '<div class="text-secondary small"><i class="fa-solid fa-spinner fa-spin me-2"></i>正在加载 K 线与买卖点数据...</div>';
    } else if (errMsg) {
      overlay.classList.remove('d-none');
      overlay.classList.add('d-flex');
      overlay.innerHTML = `<div class="text-danger small"><i class="fa-solid fa-circle-exclamation me-2"></i>${_esc(errMsg)}</div>`;
    } else {
      overlay.classList.remove('d-flex');
      overlay.classList.add('d-none');
    }
  }

  function _computeTargetWindow() {
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const list = _filteredTrades();
    const nowMs = Date.now();
    let minMs = st.baseRangeStartMs;
    let maxMs = st.baseRangeEndMs;

    if (list.length > 0) {
      const firstEntry = list[0].entryMs;
      const lastPoint = Math.max(...list.map(t => (!isNaN(t.exitMs) && t.exitMs > 0) ? t.exitMs : nowMs));
      minMs = minMs != null ? Math.min(minMs, firstEntry - 40 * stepMs) : (firstEntry - 60 * stepMs);
      maxMs = maxMs != null ? Math.max(maxMs, lastPoint + 25 * stepMs) : Math.min(nowMs + stepMs, lastPoint + 30 * stepMs);
    } else {
      maxMs = maxMs || nowMs;
      minMs = minMs || (maxMs - 240 * stepMs);
    }
    return { startMs: Math.max(0, Math.floor(minMs)), endMs: Math.floor(maxMs) };
  }

  async function _loadWindowForSymbol(customStartMs = null, customEndMs = null) {
    _setLoading(true);
    const { startMs, endMs } = (customStartMs != null && customEndMs != null)
      ? { startMs: customStartMs, endMs: customEndMs }
      : _computeTargetWindow();
    st.requestedStartMs = startMs;

    try {
      const url = `/api/klines-range/${encodeURIComponent(st.symbol)}?timeframe=${encodeURIComponent(st.timeframe)}&start_ms=${startMs}&end_ms=${endMs}&max_bars=6000`;
      const res = await fetch(url);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const body = await res.json();
      st.bars = Array.isArray(body.bars) ? body.bars : [];
      st.hasMoreLeft = Boolean(body.has_more_left);
      if (!st.bars.length) {
        _setLoading(false, '该时间段暂无可展示的 K 线数据');
        return;
      }
      _setLoading(false);
      _ensureCharts();
      _renderChart();
    } catch (err) {
      console.error('[TradeKlineModal] Load error:', err);
      _setLoading(false, err.message || '加载 K 线失败');
    }
  }

  async function _loadOlderHistory() {
    if (!st.hasMoreLeft || st.loadingMore || !st.bars.length) return;
    const earliestLoaded = st.bars[0][0];
    if (earliestLoaded <= st.requestedStartMs) {
      st.hasMoreLeft = false;
      return;
    }
    st.loadingMore = true;
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const endMs = earliestLoaded;
    const startMs = Math.max(st.requestedStartMs, endMs - 4000 * stepMs);
    try {
      const url = `/api/klines-range/${encodeURIComponent(st.symbol)}?timeframe=${encodeURIComponent(st.timeframe)}&start_ms=${startMs}&end_ms=${endMs}&max_bars=4500`;
      const res = await fetch(url);
      if (res.ok) {
        const body = await res.json();
        const older = (body.bars || []).filter(b => b[0] < earliestLoaded);
        if (older.length > 0) {
          const oldOpt = mainChart?.getOption();
          const dz = oldOpt?.dataZoom?.[0];
          const oldStartIdx = dz ? Math.round((dz.start / 100) * st.bars.length) : 0;
          const oldEndIdx   = dz ? Math.round((dz.end / 100) * st.bars.length) : st.bars.length - 1;
          st.bars = [...older, ...st.bars];
          st.hasMoreLeft = st.bars[0][0] > st.requestedStartMs + stepMs;
          st.zoomRange = {
            startValue: oldStartIdx + older.length,
            endValue: oldEndIdx + older.length,
          };
          _renderChart();
        } else {
          st.hasMoreLeft = false;
        }
      }
    } catch (err) {
      console.warn('[TradeKlineModal] Prepend history failed:', err);
    } finally {
      setTimeout(() => { st.loadingMore = false; }, 350);
    }
  }

  function _findBarIdx(tsArr, targetMs, stepMs) {
    if (!tsArr.length || isNaN(targetMs)) return -1;
    let lo = 0, hi = tsArr.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (tsArr[mid] <= targetMs) {
        ans = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    if (ans < 0) {
      return Math.abs(tsArr[0] - targetMs) <= stepMs * 2 ? 0 : -1;
    }
    if (targetMs - tsArr[ans] > stepMs * 3 && ans + 1 < tsArr.length && Math.abs(tsArr[ans + 1] - targetMs) < Math.abs(tsArr[ans] - targetMs)) {
      return ans + 1;
    }
    if (Math.abs(targetMs - tsArr[ans]) > stepMs * 6 && ans === tsArr.length - 1 && targetMs > tsArr[ans]) {
      return -1;
    }
    return ans;
  }

  async function jumpToTradeIndex(idx) {
    const list = _filteredTrades();
    if (idx < 0 || idx >= list.length) return;
    st.selectedTradeIdx = idx;
    const sel = document.getElementById('klineModalTradeSelect');
    if (sel) sel.value = String(idx);

    const tr = list[idx];
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const firstLoaded = st.bars.length ? st.bars[0][0] : Infinity;
    const lastLoaded  = st.bars.length ? st.bars[st.bars.length - 1][0] : -Infinity;
    const endRef = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? tr.exitMs : tr.entryMs;

    // If trade lies outside currently loaded window (e.g. multi-month 1m backtest), load centered window!
    if (tr.entryMs < firstLoaded || tr.entryMs > lastLoaded + stepMs * 2) {
      const winStart = Math.max(0, tr.entryMs - 1200 * stepMs);
      const winEnd   = Math.max(endRef + 1200 * stepMs, tr.entryMs + 2000 * stepMs);
      st.zoomRange = null;
      await _loadWindowForSymbol(winStart, winEnd);
    }

    const tsArr = st.bars.map(b => b[0]);
    const eIdx = _findBarIdx(tsArr, tr.entryMs, stepMs);
    const xIdx = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? _findBarIdx(tsArr, tr.exitMs, stepMs) : eIdx;
    if (eIdx >= 0 && mainChart) {
      const loIdx = Math.max(0, Math.min(eIdx, xIdx >= 0 ? xIdx : eIdx) - 35);
      const hiIdx = Math.min(st.bars.length - 1, Math.max(eIdx, xIdx >= 0 ? xIdx : eIdx) + 35);
      mainChart.dispatchAction({ type: 'dataZoom', startValue: loIdx, endValue: hiIdx });
      volChart?.dispatchAction({ type: 'dataZoom', startValue: loIdx, endValue: hiIdx });
    }
  }

  function _resetGlobalZoom() {
    if (!mainChart || !st.bars.length) return;
    const total = st.bars.length;
    const startVal = total > 220 ? total - 180 : 0;
    mainChart.dispatchAction({ type: 'dataZoom', startValue: startVal, endValue: total - 1 });
    volChart?.dispatchAction({ type: 'dataZoom', startValue: startVal, endValue: total - 1 });
  }

  function _renderChart() {
    if (!mainChart || !volChart || !st.bars.length) return;
    const cfg = getThemeConfig();
    const bars = st.bars;
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const tsArr = bars.map(b => b[0]);
    const times = bars.map(b => _fmtTime(b[0]));
    const ohlc  = bars.map(b => [b[1], b[4], b[3], b[2]]); // [open, close, low, high]
    const vols  = bars.map(b => b[5]);
    const ma7   = bars.map(b => b[6] != null ? b[6] : '-');
    const ma25  = bars.map(b => b[7] != null ? b[7] : '-');
    const ma60  = bars.map(b => b[8] != null ? b[8] : '-');

    const buyColor  = '#16c784';
    const sellColor = '#f6465d';

    // Build B/S markPoints, entry->exit dashed lines, and per-bar tooltip map
    const eventsByBar = new Map();
    const bottomStack = new Map();
    const topStack    = new Map();
    const markPoints  = [
      {
        name: '最高',
        type: 'max',
        valueDim: 'highest',
        symbol: 'circle',
        symbolSize: 4,
        itemStyle: { color: cfg.axisLabel },
        label: {
          show: true,
          position: 'left',
          distance: 6,
          color: cfg.tooltipText,
          fontSize: 11,
          formatter: (p) => `${Number(p.value).toLocaleString(undefined, { maximumFractionDigits: 2 })} —`,
        },
      },
      {
        name: '最低',
        type: 'min',
        valueDim: 'lowest',
        symbol: 'circle',
        symbolSize: 4,
        itemStyle: { color: cfg.axisLabel },
        label: {
          show: true,
          position: 'left',
          distance: 6,
          color: cfg.tooltipText,
          fontSize: 11,
          formatter: (p) => `${Number(p.value).toLocaleString(undefined, { maximumFractionDigits: 2 })} —`,
        },
      },
    ];
    const markLines = [];

    const trades = _filteredTrades();
    let matchedMarkers = 0;

    const pushEvent = (barIdx, info) => {
      if (!eventsByBar.has(barIdx)) eventsByBar.set(barIdx, []);
      eventsByBar.get(barIdx).push(info);
    };

    trades.forEach((tr) => {
      const eIdx = _findBarIdx(tsArr, tr.entryMs, stepMs);
      const xIdx = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? _findBarIdx(tsArr, tr.exitMs, stepMs) : -1;

      // Entry action: Long -> Buy (B below low), Short -> Sell (S above high)
      if (eIdx >= 0) {
        matchedMarkers++;
        const isBuy = tr.direction === 'long';
        const actionLabel = isBuy ? '买入开多' : '卖出开空';
        const stratSuffix = tr.strategyMode ? ` [${tr.strategyMode}]` : '';
        const detailHtml = `<span style="color:${isBuy ? buyColor : sellColor};font-weight:700">${isBuy ? 'B' : 'S'} ${actionLabel}</span> <b>${_esc(tr.displayId)}</b> @ <b>${tr.entryPrice.toFixed(2)}</b>${_esc(stratSuffix)}`;
        pushEvent(eIdx, detailHtml);

        if (isBuy) {
          const stk = bottomStack.get(eIdx) || 0;
          bottomStack.set(eIdx, stk + 1);
          markPoints.push({
            coord: [eIdx, bars[eIdx][3]],
            symbol: PIN_BUY_SVG,
            symbolSize: [20, 23],
            symbolOffset: [0, 15 + stk * 25],
            itemStyle: { color: buyColor },
            label: { show: true, formatter: 'B', color: '#fff', fontWeight: 'bold', fontSize: 11, offset: [0, 2] },
            tooltip: { formatter: () => `${_fmtTime(tr.entryMs)}<br/>${detailHtml}` },
          });
        } else {
          const stk = topStack.get(eIdx) || 0;
          topStack.set(eIdx, stk + 1);
          markPoints.push({
            coord: [eIdx, bars[eIdx][2]],
            symbol: PIN_SELL_SVG,
            symbolSize: [20, 23],
            symbolOffset: [0, -15 - stk * 25],
            itemStyle: { color: sellColor },
            label: { show: true, formatter: 'S', color: '#fff', fontWeight: 'bold', fontSize: 11, offset: [0, -2] },
            tooltip: { formatter: () => `${_fmtTime(tr.entryMs)}<br/>${detailHtml}` },
          });
        }
      }

      // Exit action: Long -> Sell (S above high), Short -> Buy (B below low)
      if (xIdx >= 0 && tr.exitPrice != null) {
        matchedMarkers++;
        const isBuy = tr.direction === 'short'; // closing a short = Buy!
        const actionLabel = isBuy ? '买入平空' : '卖出平多';
        const pnlSign = (tr.pnl || 0) >= 0 ? '+' : '';
        const pnlColor = (tr.pnl || 0) >= 0 ? buyColor : sellColor;
        const pnlStr = tr.pnl != null ? ` · <span style="color:${pnlColor};font-weight:700">${pnlSign}$${tr.pnl.toFixed(2)}</span>` : '';
        const rsnStr = tr.closeReason ? ` (${REASON_LABELS[tr.closeReason] || tr.closeReason})` : '';
        const detailHtml = `<span style="color:${isBuy ? buyColor : sellColor};font-weight:700">${isBuy ? 'B' : 'S'} ${actionLabel}</span> <b>${_esc(tr.displayId)}</b> @ <b>${tr.exitPrice.toFixed(2)}</b>${pnlStr}${_esc(rsnStr)}`;
        pushEvent(xIdx, detailHtml);

        if (isBuy) {
          const stk = bottomStack.get(xIdx) || 0;
          bottomStack.set(xIdx, stk + 1);
          markPoints.push({
            coord: [xIdx, bars[xIdx][3]],
            symbol: PIN_BUY_SVG,
            symbolSize: [20, 23],
            symbolOffset: [0, 15 + stk * 25],
            itemStyle: { color: buyColor },
            label: { show: true, formatter: 'B', color: '#fff', fontWeight: 'bold', fontSize: 11, offset: [0, 2] },
            tooltip: { formatter: () => `${_fmtTime(tr.exitMs)}<br/>${detailHtml}` },
          });
        } else {
          const stk = topStack.get(xIdx) || 0;
          topStack.set(xIdx, stk + 1);
          markPoints.push({
            coord: [xIdx, bars[xIdx][2]],
            symbol: PIN_SELL_SVG,
            symbolSize: [20, 23],
            symbolOffset: [0, -15 - stk * 25],
            itemStyle: { color: sellColor },
            label: { show: true, formatter: 'S', color: '#fff', fontWeight: 'bold', fontSize: 11, offset: [0, -2] },
            tooltip: { formatter: () => `${_fmtTime(tr.exitMs)}<br/>${detailHtml}` },
          });
        }

        if (eIdx >= 0) {
          const winLineColor = (tr.pnl || 0) >= 0 ? 'rgba(22, 199, 132, 0.55)' : 'rgba(246, 70, 93, 0.55)';
          markLines.push([
            { coord: [eIdx, tr.entryPrice], lineStyle: { color: winLineColor, width: 1.4, type: 'dashed' } },
            { coord: [xIdx, tr.exitPrice] },
          ]);
        }
      }
    });

    const statsEl = document.getElementById('klineModalStatsHint');
    if (statsEl) {
      const moreHint = st.hasMoreLeft ? ' · 向左拖动自动加载更早K线' : '';
      statsEl.textContent = `已加载 ${bars.length} 根 ${st.timeframe} K线 · 买卖标记 ${matchedMarkers} 处${moreHint}`;
    }

    // Default initial zoom: show crisp ~160 candles around latest trade/end
    let dzStartVal = 0;
    let dzEndVal = bars.length - 1;
    if (st.zoomRange) {
      dzStartVal = st.zoomRange.startValue;
      dzEndVal   = st.zoomRange.endValue;
      st.zoomRange = null;
    } else if (bars.length > 200) {
      const lastTr = trades.length ? trades[trades.length - 1] : null;
      const anchorMs = lastTr ? ((!isNaN(lastTr.exitMs) && lastTr.exitMs > 0) ? lastTr.exitMs : lastTr.entryMs) : bars[bars.length - 1][0];
      const anchorIdx = _findBarIdx(tsArr, anchorMs, stepMs);
      if (anchorIdx >= 0) {
        dzEndVal   = Math.min(bars.length - 1, anchorIdx + 30);
        dzStartVal = Math.max(0, dzEndVal - 160);
      } else {
        dzStartVal = bars.length - 160;
      }
    }

    mainChart.setOption({
      backgroundColor: cfg.bg,
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: cfg.tooltipBg,
        borderColor: cfg.tooltipBorder,
        textStyle: { color: cfg.tooltipText, fontSize: 12 },
        formatter(params) {
          const p0 = Array.isArray(params) ? params[0] : params;
          const idx = p0?.dataIndex;
          if (idx === undefined || !bars[idx]) return '';
          const b = bars[idx];
          const o = b[1], h = b[2], l = b[3], c = b[4], v = b[5];
          const diff = c - o;
          const diffHtml = `<span style="color:${diff >= 0 ? buyColor : sellColor}">${diff >= 0 ? '+' : ''}${diff.toFixed(2)}</span>`;
          const evs = eventsByBar.get(idx) || [];
          const evHtml = evs.length
            ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid ${cfg.tooltipBorder}">${evs.map(x => `<div style="margin-bottom:2px">${x}</div>`).join('')}</div>`
            : '';
          return `<div>
            <div style="color:${cfg.axisLabel};margin-bottom:3px">${times[idx]} (${st.symbol} · ${st.timeframe})</div>
            <div>开 <b>${o}</b> &nbsp;高 <b>${h}</b> &nbsp;低 <b>${l}</b> &nbsp;收 <b>${c}</b> ${diffHtml}</div>
            <div>成交量 <b>${v}</b></div>
            <div style="margin-top:3px">
              <span style="color:#f0b90b">MA7: ${ma7[idx]}</span> &nbsp;
              <span style="color:#ec4899">MA25: ${ma25[idx]}</span> &nbsp;
              <span style="color:#9b6dd6">MA60: ${ma60[idx]}</span>
            </div>
            ${evHtml}
          </div>`;
        },
      },
      grid: { left: 68, right: 18, top: 28, bottom: 52 },
      xAxis: {
        type: 'category',
        data: times,
        axisLine: { lineStyle: { color: cfg.axisLine } },
        axisLabel: { color: cfg.axisLabel, fontSize: 10 },
        splitLine: { show: true, lineStyle: { color: cfg.splitLine, width: 0.6 } },
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLine: { lineStyle: { color: cfg.axisLine } },
        axisLabel: { color: cfg.axisLabel, fontSize: 10 },
        splitLine: { lineStyle: { color: cfg.splitLine } },
      },
      dataZoom: [
        { type: 'inside', startValue: dzStartVal, endValue: dzEndVal },
        {
          type: 'slider',
          height: 20,
          bottom: 4,
          startValue: dzStartVal,
          endValue: dzEndVal,
          borderColor: cfg.sliderBorder,
          fillerColor: cfg.sliderFiller,
          handleStyle: { color: cfg.sliderHandle },
          textStyle: { color: cfg.axisLabel, fontSize: 10 },
          labelFormatter: () => '',
        },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: ohlc,
          itemStyle: {
            color: cfg.bg,            // hollow up candle (matches Image 1!)
            color0: sellColor,        // solid red down candle
            borderColor: buyColor,
            borderColor0: sellColor,
            borderWidth: 1.3,
          },
          markPoint: { data: markPoints, animation: false },
          markLine: { symbol: ['none', 'none'], silent: true, animation: false, data: markLines },
        },
        { name: 'MA7',  type: 'line', data: ma7,  smooth: true, symbol: 'none', lineStyle: { color: '#f0b90b', width: 1.3 } },
        { name: 'MA25', type: 'line', data: ma25, smooth: true, symbol: 'none', lineStyle: { color: '#ec4899', width: 1.3 } },
        { name: 'MA60', type: 'line', data: ma60, smooth: true, symbol: 'none', lineStyle: { color: '#9b6dd6', width: 1.2 } },
      ],
    }, true);

    const volColors = bars.map(b => b[4] >= b[1] ? buyColor : sellColor);
    volChart.setOption({
      backgroundColor: cfg.bg,
      animation: false,
      tooltip: { trigger: 'axis', backgroundColor: cfg.tooltipBg, borderColor: cfg.tooltipBorder, textStyle: { color: cfg.tooltipText, fontSize: 11 } },
      grid: { left: 68, right: 18, top: 4, bottom: 6 },
      xAxis: { type: 'category', data: times, show: false },
      yAxis: { type: 'value', show: false },
      dataZoom: [{ type: 'inside', startValue: dzStartVal, endValue: dzEndVal }],
      series: [{
        type: 'bar',
        data: vols,
        itemStyle: { color: (p) => volColors[p.dataIndex], opacity: 0.8 },
        barMaxWidth: 8,
      }],
    }, true);

    requestAnimationFrame(() => {
      mainChart?.resize();
      volChart?.resize();
    });
  }

  function open({ title, symbol, timeframe = '1m', trades = [], rangeStartMs = null, rangeEndMs = null }) {
    _bindDomOnce();
    const modalEl = document.getElementById('tradeKlineModal');
    if (!modalEl || typeof bootstrap === 'undefined') return;

    const normTrades = _normalizeTrades(trades, symbol);
    const symSet = new Set();
    if (symbol) symSet.add(String(symbol).toUpperCase());
    normTrades.forEach(t => symSet.add(t.symbol));
    const symList = Array.from(symSet);

    st.title = title || 'K线买卖点复盘';
    st.allTrades = normTrades;
    st.symbols = symList.length ? symList : ['BTCUSDT'];
    st.symbol = symbol ? String(symbol).toUpperCase() : (st.symbols.includes('BTCUSDT') ? 'BTCUSDT' : st.symbols[0]);
    st.timeframe = TF_MS[timeframe] ? timeframe : '1m';
    st.baseRangeStartMs = rangeStartMs;
    st.baseRangeEndMs = rangeEndMs;
    st.selectedTradeIdx = -1;
    st.zoomRange = null;

    const titleEl = document.getElementById('klineModalTitle');
    if (titleEl) titleEl.textContent = st.title;

    _populateSymbolSelector();
    _syncTfButtons();
    _populateTradeSelector();

    if (!modalInstance) {
      modalInstance = new bootstrap.Modal(modalEl);
    }
    modalInstance.show();
    _loadWindowForSymbol();
  }

  function updateTheme() {
    if (mainChart || volChart) {
      _disposeCharts();
      const modalEl = document.getElementById('tradeKlineModal');
      if (modalEl && modalEl.classList.contains('show') && st.bars.length) {
        _ensureCharts();
        _renderChart();
      }
    }
  }

  return { open, updateTheme, jumpToTradeIndex };
})();

// ─── Trades Equity Curve Chart ─────────────────────────────────────────
const TradesEquityChart = (() => {
  let chart = null;
  let lastData = null;

  function render(dates, equity, pnls = []) {
    lastData = { dates, equity, pnls };
    const el = document.getElementById('tradesEquityChart');
    if (!el) return;
    const cfg = getThemeConfig();

    if (!chart) {
      chart = echarts.init(el, cfg.echartsTheme);
    } else {
      chart.resize();
    }

    if (!dates || dates.length === 0) {
      chart.clear();
      chart.setOption({
        backgroundColor: 'transparent',
        title: {
          text: '暂无已平仓交易记录',
          left: 'center',
          top: 'middle',
          textStyle: { color: cfg.axisLabel, fontSize: 13, fontWeight: 'normal' }
        }
      });
      return;
    }

    chart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      title: { show: false },
      tooltip: {
        trigger: 'axis',
        backgroundColor: cfg.tooltipBg,
        borderColor: cfg.tooltipBorder,
        textStyle: { color: cfg.tooltipText, fontSize: 12 },
        formatter: (params) => {
          const p = params && params[0];
          if (!p) return '';
          const idx = p.dataIndex;
          const eqVal = Number(p.value).toFixed(2);
          const tradePnl = pnls[idx];
          let html = `<div style="font-weight:600;margin-bottom:4px;">${p.axisValue}</div>`;
          html += `<div>累计权益: <b>$${eqVal}</b></div>`;
          if (tradePnl !== null && tradePnl !== undefined) {
            const sign = tradePnl >= 0 ? '+' : '';
            const color = tradePnl >= 0 ? cfg.bullish : cfg.bearish;
            html += `<div>当笔盈亏: <span style="color:${color};font-weight:600;">${sign}$${Number(tradePnl).toFixed(2)}</span></div>`;
          }
          return html;
        }
      },
      grid: { left: 65, right: 18, top: 16, bottom: 42 },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: { color: cfg.axisLabel, fontSize: 10, rotate: 25 },
        axisLine: { lineStyle: { color: cfg.axisLine } }
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { color: cfg.axisLabel, fontSize: 10, formatter: '${value}' },
        splitLine: { lineStyle: { color: cfg.splitLine } }
      },
      series: [{
        name: '权益',
        type: 'line',
        data: equity,
        smooth: true,
        symbol: 'circle',
        symbolSize: 5,
        lineStyle: { color: cfg.equityLine, width: 2 },
        itemStyle: { color: cfg.equityLine },
        areaStyle: {
          color: {
            type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: cfg.equityAreaStart },
              { offset: 1, color: cfg.equityAreaEnd }
            ]
          }
        }
      }]
    }, true);

    requestAnimationFrame(() => chart?.resize());
    setTimeout(() => chart?.resize(), 60);
  }

  function resize() {
    if (chart) {
      chart.resize();
    } else if (lastData) {
      render(lastData.dates, lastData.equity, lastData.pnls);
    }
  }

  function updateTheme() {
    if (chart) {
      try { echarts.dispose(chart); } catch {}
      chart = null;
    }
    if (lastData) render(lastData.dates, lastData.equity, lastData.pnls);
  }

  window.addEventListener('resize', () => chart?.resize());
  return { render, updateTheme, resize };
})();
