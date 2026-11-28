/**
 * chart.js — TradingView Lightweight Charts (K线图 + MA均线 + 成交量 + B/S买卖标注) & ECharts (统计/权益图)
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
    buyPin: '#16c784',
    sellPin: '#f6465d',
    equityAreaStart: isLight ? 'rgba(9,105,218,0.25)' : 'rgba(88,166,255,0.3)',
    equityAreaEnd: isLight ? 'rgba(9,105,218,0.02)' : 'rgba(88,166,255,0.02)',
    equityLine: isLight ? '#0969da' : '#58a6ff'
  };
}

function _fmtLwcLocalTime(unixSec) {
  if (unixSec == null || isNaN(unixSec)) return '';
  const d = new Date(Number(unixSec) * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _createLwcBundle(containerEl, cfg) {
  if (!containerEl || typeof LightweightCharts === 'undefined') return null;
  const chart = LightweightCharts.createChart(containerEl, {
    width: Math.max(containerEl.clientWidth || 0, 300),
    height: Math.max(containerEl.clientHeight || 0, 320),
    layout: {
      background: { type: 'solid', color: cfg.bg },
      textColor: cfg.axisLabel,
      fontSize: 11,
    },
    grid: {
      vertLines: { color: cfg.splitLine },
      horzLines: { color: cfg.splitLine },
    },
    crosshair: {
      mode: LightweightCharts.CrosshairMode.Normal,
      vertLine: { color: cfg.axisLabel, width: 1, style: 2, labelBackgroundColor: cfg.tooltipBorder },
      horzLine: { color: cfg.axisLabel, width: 1, style: 2, labelBackgroundColor: cfg.tooltipBorder },
    },
    localization: {
      timeFormatter: _fmtLwcLocalTime,
    },
    rightPriceScale: {
      borderColor: cfg.axisLine,
      scaleMargins: { top: 0.08, bottom: 0.22 },
    },
    timeScale: {
      borderColor: cfg.axisLine,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      barSpacing: 7,
      minBarSpacing: 1.5,
      tickMarkFormatter: (t) => _fmtLwcLocalTime(t),
    },
    handleScroll: {
      mouseWheel: true,
      pressedMouseMove: true,
      horzTouchDrag: true,
      vertTouchDrag: false,
    },
    handleScale: {
      mouseWheel: true,
      pinch: true,
      axisPressedMouseMove: { price: true, time: true },
      axisDoubleClickReset: { price: true, time: true },
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: cfg.bg,
    downColor: cfg.sellPin,
    borderUpColor: cfg.buyPin,
    borderDownColor: cfg.sellPin,
    wickUpColor: cfg.buyPin,
    wickDownColor: cfg.sellPin,
    priceLineVisible: true,
  });

  const volSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: '',
  });
  volSeries.priceScale().applyOptions({
    scaleMargins: { top: 0.80, bottom: 0 },
  });

  const ma7Series = chart.addLineSeries({
    color: '#f0b90b',
    lineWidth: 1.4,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  const ma25Series = chart.addLineSeries({
    color: '#ec4899',
    lineWidth: 1.4,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });
  const ma60Series = chart.addLineSeries({
    color: '#9b6dd6',
    lineWidth: 1.2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
  });

  return { chart, candleSeries, volSeries, ma7Series, ma25Series, ma60Series };
}

function _normBarsForLwc(klines) {
  const map = new Map();
  (klines || []).forEach((k) => {
    const rawT = Array.isArray(k) ? k[0] : (k.time ?? k.t ?? k.timestamp ?? 0);
    const ms = typeof rawT === 'string' ? new Date(rawT).getTime() : Number(rawT);
    if (!ms || isNaN(ms)) return;
    const sec = Math.floor(ms / 1000);
    const o = +(Array.isArray(k) ? k[1] : (k.open ?? k.o ?? 0));
    const h = +(Array.isArray(k) ? k[2] : (k.high ?? k.h ?? 0));
    const l = +(Array.isArray(k) ? k[3] : (k.low ?? k.l ?? 0));
    const c = +(Array.isArray(k) ? k[4] : (k.close ?? k.c ?? 0));
    const v = +(Array.isArray(k) ? k[5] : (k.volume ?? k.v ?? 0));
    const ma7 = Array.isArray(k) ? k[6] : k.ma7;
    const ma25 = Array.isArray(k) ? k[7] : k.ma25;
    const ma60 = Array.isArray(k) ? k[8] : k.ma60;
    map.set(sec, { time: sec, open: o, high: h, low: l, close: c, volume: v, ma7, ma25, ma60 });
  });
  return Array.from(map.values()).sort((a, b) => a.time - b.time);
}

function _computeMaLineData(bars, period, precomputedKey) {
  const out = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    sum += b.close;
    if (i >= period) sum -= bars[i - period].close;
    const pre = precomputedKey ? b[precomputedKey] : null;
    if (pre != null && pre !== '-' && !isNaN(Number(pre))) {
      out.push({ time: b.time, value: +Number(pre).toFixed(4) });
    } else if (i >= period - 1) {
      out.push({ time: b.time, value: +(sum / period).toFixed(4) });
    }
  }
  return out;
}

function _snapTimeToBarSec(secArr, targetMs, stepSec = 60) {
  if (!secArr.length || isNaN(targetMs)) return null;
  const targetSec = Math.floor(targetMs / 1000);
  let lo = 0, hi = secArr.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (secArr[mid] <= targetSec) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (ans < 0) return Math.abs(secArr[0] - targetSec) <= stepSec * 2 ? secArr[0] : null;
  if (targetSec - secArr[ans] > stepSec * 3 && ans + 1 < secArr.length && Math.abs(secArr[ans + 1] - targetSec) < Math.abs(secArr[ans] - targetSec)) {
    return secArr[ans + 1];
  }
  if (Math.abs(targetSec - secArr[ans]) > stepSec * 6 && ans === secArr.length - 1 && targetSec > secArr[ans]) {
    return null;
  }
  return secArr[ans];
}

// ─── Homepage Realtime K-line Chart Manager ────────────────────────────
const ChartManager = (() => {
  let bundle = null;
  let currentData = { klines: [], trades: [] };
  let lwcBars = [];
  let barMap = new Map();
  let maMap = new Map();
  let onReachStartCallback = null;
  let chartResizeObserver = null;
  let resizeRafId = null;
  let initialZoomApplied = false;

  function setOnReachStart(callback) {
    onReachStartCallback = callback;
  }

  function resize() {
    const klineEl = document.getElementById('klineChart');
    if (klineEl && bundle?.chart && klineEl.clientWidth > 0 && klineEl.clientHeight > 0) {
      bundle.chart.applyOptions({ width: klineEl.clientWidth, height: klineEl.clientHeight });
    }
  }

  function _scheduleResize() {
    if (resizeRafId) cancelAnimationFrame(resizeRafId);
    resizeRafId = requestAnimationFrame(() => {
      resizeRafId = null;
      resize();
    });
  }

  function _updateHud(bar, maVals) {
    const hud = document.getElementById('klineChartHud');
    if (!hud || !bar) return;
    const cfg = getThemeConfig();
    const diff = bar.close - bar.open;
    const pct = bar.open ? (diff / bar.open * 100) : 0;
    const cColor = diff >= 0 ? cfg.buyPin : cfg.sellPin;
    const sign = diff >= 0 ? '+' : '';
    const m = maVals || {};
    const hudBg = cfg.isLight ? 'rgba(255,255,255,0.85)' : 'rgba(13,17,23,0.72)';
    const hudBorder = cfg.isLight ? 'rgba(208,215,222,0.75)' : 'rgba(48,54,61,0.75)';
    hud.innerHTML = `
      <div style="background:${hudBg};backdrop-filter:blur(4px);padding:3px 8px;border-radius:4px;color:${cfg.tooltipText};border:1px solid ${hudBorder};">
        <span style="color:${cfg.axisLabel}">${_fmtLwcLocalTime(bar.time)}</span>
        <span class="ms-2">开 <b>${bar.open}</b></span>
        <span class="ms-2">高 <b>${bar.high}</b></span>
        <span class="ms-2">低 <b>${bar.low}</b></span>
        <span class="ms-2">收 <b style="color:${cColor}">${bar.close}</b> (${sign}${pct.toFixed(2)}%)</span>
        <span class="ms-2">量 <b>${bar.volume.toFixed(2)}</b></span>
        <span class="ms-2" style="color:#f0b90b">MA7:${m.ma7 ?? '—'}</span>
        <span class="ms-2" style="color:#ec4899">MA25:${m.ma25 ?? '—'}</span>
        <span class="ms-2" style="color:#9b6dd6">MA60:${m.ma60 ?? '—'}</span>
      </div>
    `;
  }

  function init() {
    const klineEl = document.getElementById('klineChart');
    if (!klineEl) return;

    if (bundle?.chart) {
      try { bundle.chart.remove(); } catch {}
      bundle = null;
    }

    const cfg = getThemeConfig();
    bundle = _createLwcBundle(klineEl, cfg);
    if (!bundle) return;

    bundle.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range && range.from < 8 && lwcBars.length > 0 && onReachStartCallback) {
        onReachStartCallback();
      }
    });

    bundle.chart.subscribeCrosshairMove((param) => {
      if (!lwcBars.length) return;
      const t = param?.time;
      const bar = (t != null && barMap.has(t)) ? barMap.get(t) : lwcBars[lwcBars.length - 1];
      _updateHud(bar, maMap.get(bar.time));
    });

    if (!chartResizeObserver && typeof ResizeObserver !== 'undefined') {
      chartResizeObserver = new ResizeObserver(() => _scheduleResize());
      chartResizeObserver.observe(klineEl);
      if (klineEl.parentElement) chartResizeObserver.observe(klineEl.parentElement);
    }
    window.addEventListener('resize', _scheduleResize);
  }

  function _applyMarkers(trades) {
    if (!bundle?.candleSeries || !lwcBars.length) return;
    const secArr = lwcBars.map(b => b.time);
    const markers = [];
    (trades || []).forEach((tr) => {
      const openMs = new Date(tr.open_time).getTime();
      const eSec = _snapTimeToBarSec(secArr, openMs, 60);
      if (eSec != null) {
        const isLong = String(tr.direction || '').toLowerCase() === 'long';
        markers.push({
          time: eSec,
          position: isLong ? 'belowBar' : 'aboveBar',
          color: isLong ? '#16c784' : '#f6465d',
          shape: isLong ? 'arrowUp' : 'arrowDown',
          text: `${isLong ? 'B' : 'S'} #${tr.id}`,
        });
      }
      if (tr.close_time) {
        const closeMs = new Date(tr.close_time).getTime();
        const xSec = _snapTimeToBarSec(secArr, closeMs, 60);
        if (xSec != null) {
          const isLong = String(tr.direction || '').toLowerCase() === 'long';
          markers.push({
            time: xSec,
            position: isLong ? 'aboveBar' : 'belowBar',
            color: isLong ? '#f6465d' : '#16c784',
            shape: isLong ? 'arrowDown' : 'arrowUp',
            text: `${isLong ? 'S' : 'B'} 平#${tr.id}`,
          });
        }
      }
    });
    markers.sort((a, b) => a.time - b.time);
    bundle.candleSeries.setMarkers(markers);
  }

  function _syncAllSeries(preserveRange = false) {
    if (!bundle) return;
    const cfg = getThemeConfig();
    const prevRange = preserveRange ? bundle.chart.timeScale().getVisibleLogicalRange() : null;

    lwcBars = _normBarsForLwc(currentData.klines);
    barMap = new Map(lwcBars.map(b => [b.time, b]));

    const ma7Data  = _computeMaLineData(lwcBars, 7, 'ma7');
    const ma25Data = _computeMaLineData(lwcBars, 25, 'ma25');
    const ma60Data = _computeMaLineData(lwcBars, 60, 'ma60');
    maMap = new Map();
    ma7Data.forEach(d  => { const x = maMap.get(d.time) || {}; x.ma7 = d.value; maMap.set(d.time, x); });
    ma25Data.forEach(d => { const x = maMap.get(d.time) || {}; x.ma25 = d.value; maMap.set(d.time, x); });
    ma60Data.forEach(d => { const x = maMap.get(d.time) || {}; x.ma60 = d.value; maMap.set(d.time, x); });

    bundle.candleSeries.setData(lwcBars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    bundle.volSeries.setData(lwcBars.map(b => ({
      time: b.time,
      value: b.volume,
      color: b.close >= b.open ? 'rgba(22, 199, 132, 0.55)' : 'rgba(246, 70, 93, 0.55)',
    })));
    bundle.ma7Series.setData(ma7Data);
    bundle.ma25Series.setData(ma25Data);
    bundle.ma60Series.setData(ma60Data);

    _applyMarkers(currentData.trades);

    if (lwcBars.length > 0) {
      const lastBar = lwcBars[lwcBars.length - 1];
      _updateHud(lastBar, maMap.get(lastBar.time));
      if (!preserveRange && !initialZoomApplied) {
        const total = lwcBars.length;
        bundle.chart.timeScale().setVisibleLogicalRange({
          from: Math.max(0, total - 120),
          to: total + 4,
        });
        initialZoomApplied = true;
      } else if (prevRange) {
        bundle.chart.timeScale().setVisibleLogicalRange(prevRange);
      }
    }
    _scheduleResize();
  }

  function render(klines, trades = []) {
    if (!bundle) init();
    const isFreshLoad = !currentData.klines.length && (klines || []).length > 0;
    currentData = { klines: klines || [], trades: trades || [] };
    if (!klines || !klines.length) {
      bundle?.candleSeries.setData([]);
      bundle?.volSeries.setData([]);
      bundle?.ma7Series.setData([]);
      bundle?.ma25Series.setData([]);
      bundle?.ma60Series.setData([]);
      return;
    }
    if (isFreshLoad) initialZoomApplied = false;
    _syncAllSeries(!isFreshLoad && initialZoomApplied);
  }

  function highlightTrade(tradeOrId) {
    if (!bundle?.chart || !lwcBars.length) return;
    let trade = tradeOrId;
    if (typeof tradeOrId === 'number' || typeof tradeOrId === 'string') {
      trade = (currentData.trades || []).find(t => t.id == tradeOrId);
    }
    if (!trade || !trade.open_time) return;
    const secArr = lwcBars.map(b => b.time);
    const eSec = _snapTimeToBarSec(secArr, new Date(trade.open_time).getTime(), 60);
    if (eSec == null) return;
    const idx = secArr.indexOf(eSec);
    if (idx < 0) return;
    bundle.chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, idx - 40),
      to: Math.min(lwcBars.length + 3, idx + 40),
    });
  }

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
      klines.push([candleTime, o, h, l, c, v]);
      if (klines.length > 2000) klines.shift();
    }

    if (!bundle) {
      render(klines, currentData.trades);
      return;
    }
    lwcBars = _normBarsForLwc(klines);
    if (!lwcBars.length) return;
    const tail = lwcBars[lwcBars.length - 1];
    barMap.set(tail.time, tail);
    bundle.candleSeries.update({ time: tail.time, open: tail.open, high: tail.high, low: tail.low, close: tail.close });
    bundle.volSeries.update({
      time: tail.time,
      value: tail.volume,
      color: tail.close >= tail.open ? 'rgba(22, 199, 132, 0.55)' : 'rgba(246, 70, 93, 0.55)',
    });
    const updMa = (period, series, key) => {
      if (lwcBars.length < period) return null;
      let s = 0;
      for (let j = lwcBars.length - period; j < lwcBars.length; j++) s += lwcBars[j].close;
      const val = +(s / period).toFixed(4);
      series.update({ time: tail.time, value: val });
      const cur = maMap.get(tail.time) || {};
      cur[key] = val;
      maMap.set(tail.time, cur);
      return val;
    };
    updMa(7, bundle.ma7Series, 'ma7');
    updMa(25, bundle.ma25Series, 'ma25');
    updMa(60, bundle.ma60Series, 'ma60');
    _updateHud(tail, maMap.get(tail.time));
  }

  function prependHistory(olderBars) {
    if (!olderBars || !olderBars.length) return false;
    if (!currentData.klines || !currentData.klines.length) {
      render(olderBars, currentData.trades);
      return true;
    }
    const firstCandle = currentData.klines[0];
    const firstTime = Array.isArray(firstCandle) ? firstCandle[0] : (firstCandle.time ?? firstCandle.t ?? 0);
    const firstTimeMs = typeof firstTime === 'string' ? new Date(firstTime).getTime() : Number(firstTime);

    const validOlder = olderBars.filter(bar => {
      const t = Array.isArray(bar) ? bar[0] : (bar.time ?? bar.t ?? 0);
      const ms = typeof t === 'string' ? new Date(t).getTime() : Number(t);
      return ms < firstTimeMs;
    });
    if (!validOlder.length) return false;

    const oldRange = bundle?.chart.timeScale().getVisibleLogicalRange();
    const prevLen = lwcBars.length;
    currentData.klines = [...validOlder, ...currentData.klines];
    _syncAllSeries(false);
    const added = lwcBars.length - prevLen;
    if (oldRange && added > 0 && bundle?.chart) {
      bundle.chart.timeScale().setVisibleLogicalRange({
        from: oldRange.from + added,
        to: oldRange.to + added,
      });
    }
    return true;
  }

  function updateTheme() {
    init();
    if (currentData.klines && currentData.klines.length) {
      _syncAllSeries(true);
    }
  }

  return { init, render, resize, highlightTrade, appendCandle, prependHistory, setOnReachStart, updateTheme };
})();

// ─── Stats charts (ECharts) ────────────────────────────────────────────
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

// ─── Backtest equity chart (ECharts) ───────────────────────────────────
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

// ─── Trades Equity Curve Chart (ECharts) ───────────────────────────────
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

// ─── Trade & Backtest K-line Modal (Lightweight Charts + Clean B/S Markers) ─
const TradeKlineModal = (() => {
  let modalInstance = null;
  let bundle = null;
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
    bars: [],
    lwcBars: [],
    barMap: new Map(),
    maMap: new Map(),
    eventsBySec: new Map(),
    hasMoreLeft: false,
    requestedStartMs: 0,
    loadingMore: false,
    selectedTradeIdx: -1,
    preserveLogicalRange: null,
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

  function _updateHud(bar) {
    const hud = document.getElementById('klineModalHud');
    if (!hud || !bar) return;
    const cfg = getThemeConfig();
    const diff = bar.close - bar.open;
    const pct = bar.open ? (diff / bar.open * 100) : 0;
    const cColor = diff >= 0 ? cfg.buyPin : cfg.sellPin;
    const sign = diff >= 0 ? '+' : '';
    const m = st.maMap.get(bar.time) || {};
    const evs = st.eventsBySec.get(bar.time) || [];
    const evHtml = evs.length
      ? `<div style="margin-top:3px;padding-top:3px;border-top:1px solid rgba(128,128,128,0.22);">${evs.join(' &nbsp;|&nbsp; ')}</div>`
      : '';
    const hudBg = cfg.isLight ? 'rgba(255,255,255,0.88)' : 'rgba(13,17,23,0.72)';
    const hudBorder = cfg.isLight ? 'rgba(208,215,222,0.75)' : 'rgba(48,54,61,0.75)';
    hud.innerHTML = `
      <div style="background:${hudBg};backdrop-filter:blur(4px);padding:4px 8px;border-radius:4px;color:${cfg.tooltipText};border:1px solid ${hudBorder};">
        <div>
          <span style="color:${cfg.axisLabel}">${_fmtLwcLocalTime(bar.time)}</span>
          <span class="ms-2">开 <b>${bar.open}</b></span>
          <span class="ms-2">高 <b>${bar.high}</b></span>
          <span class="ms-2">低 <b>${bar.low}</b></span>
          <span class="ms-2">收 <b style="color:${cColor}">${bar.close}</b> (${sign}${pct.toFixed(2)}%)</span>
          <span class="ms-2">量 <b>${bar.volume.toFixed(2)}</b></span>
          <span class="ms-2" style="color:#f0b90b">MA7:${m.ma7 ?? '—'}</span>
          <span class="ms-2" style="color:#ec4899">MA25:${m.ma25 ?? '—'}</span>
          <span class="ms-2" style="color:#9b6dd6">MA60:${m.ma60 ?? '—'}</span>
        </div>
        ${evHtml}
      </div>
    `;
  }

  function _ensureCharts() {
    const mainEl = document.getElementById('klineModalMainChart');
    if (!mainEl) return;
    if (!bundle) {
      bundle = _createLwcBundle(mainEl, getThemeConfig());
      if (!bundle) return;

      bundle.chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range && range.from < 10 && st.hasMoreLeft && !st.loadingMore) {
          _loadOlderHistory();
        }
      });

      bundle.chart.subscribeCrosshairMove((param) => {
        if (!st.lwcBars.length) return;
        const t = param?.time;
        const bar = (t != null && st.barMap.has(t)) ? st.barMap.get(t) : st.lwcBars[st.lwcBars.length - 1];
        _updateHud(bar);
      });

      if (!resizeObs && typeof ResizeObserver !== 'undefined') {
        resizeObs = new ResizeObserver(() => {
          if (mainEl.clientWidth > 0 && mainEl.clientHeight > 0 && bundle?.chart) {
            bundle.chart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight });
          }
        });
        resizeObs.observe(mainEl);
      }
    }
  }

  function _disposeCharts() {
    if (bundle?.chart) {
      try { bundle.chart.remove(); } catch {}
      bundle = null;
    }
  }

  function _bindDomOnce() {
    const modalEl = document.getElementById('tradeKlineModal');
    if (!modalEl || modalEl.dataset.bound === '1') return;
    modalEl.dataset.bound = '1';

    modalEl.addEventListener('shown.bs.modal', () => {
      _ensureCharts();
      const mainEl = document.getElementById('klineModalMainChart');
      if (mainEl && bundle?.chart && mainEl.clientWidth > 0) {
        bundle.chart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight });
      }
      if (st.bars.length > 0) _renderChart();
    });

    const symSel = document.getElementById('klineModalSymbol');
    if (symSel) {
      symSel.addEventListener('change', (e) => {
        st.symbol = e.target.value;
        st.selectedTradeIdx = -1;
        st.preserveLogicalRange = null;
        _populateTradeSelector();
        _loadWindowForSymbol();
      });
    }

    document.querySelectorAll('#klineModalTfBtns button[data-km-tf]').forEach(btn => {
      btn.addEventListener('click', () => {
        const tf = btn.getAttribute('data-km-tf');
        if (!tf || tf === st.timeframe) return;
        st.timeframe = tf;
        st.preserveLogicalRange = null;
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
          const prevRange = bundle?.chart.timeScale().getVisibleLogicalRange();
          const prevLen = st.lwcBars.length;
          st.bars = [...older, ...st.bars];
          st.hasMoreLeft = st.bars[0][0] > st.requestedStartMs + stepMs;
          const nextLwc = _normBarsForLwc(st.bars);
          const added = nextLwc.length - prevLen;
          st.preserveLogicalRange = prevRange
            ? { from: prevRange.from + Math.max(0, added), to: prevRange.to + Math.max(0, added) }
            : null;
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

  async function jumpToTradeIndex(idx) {
    const list = _filteredTrades();
    if (idx < 0 || idx >= list.length) return;
    st.selectedTradeIdx = idx;
    const sel = document.getElementById('klineModalTradeSelect');
    if (sel) sel.value = String(idx);

    const tr = list[idx];
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const stepSec = Math.max(60, Math.floor(stepMs / 1000));
    const firstLoaded = st.bars.length ? st.bars[0][0] : Infinity;
    const lastLoaded  = st.bars.length ? st.bars[st.bars.length - 1][0] : -Infinity;
    const endRef = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? tr.exitMs : tr.entryMs;

    if (tr.entryMs < firstLoaded || tr.entryMs > lastLoaded + stepMs * 2) {
      const winStart = Math.max(0, tr.entryMs - 1200 * stepMs);
      const winEnd   = Math.max(endRef + 1200 * stepMs, tr.entryMs + 2000 * stepMs);
      st.preserveLogicalRange = null;
      await _loadWindowForSymbol(winStart, winEnd);
    }

    const secArr = st.lwcBars.map(b => b.time);
    const eSec = _snapTimeToBarSec(secArr, tr.entryMs, stepSec);
    const xSec = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? _snapTimeToBarSec(secArr, tr.exitMs, stepSec) : eSec;
    if (eSec != null && bundle?.chart) {
      const eIdx = secArr.indexOf(eSec);
      const xIdx = xSec != null ? secArr.indexOf(xSec) : eIdx;
      const loIdx = Math.max(0, Math.min(eIdx, xIdx >= 0 ? xIdx : eIdx) - 35);
      const hiIdx = Math.min(st.lwcBars.length + 4, Math.max(eIdx, xIdx >= 0 ? xIdx : eIdx) + 35);
      bundle.chart.timeScale().setVisibleLogicalRange({ from: loIdx, to: hiIdx });
    }
  }

  function _resetGlobalZoom() {
    if (!bundle?.chart || !st.lwcBars.length) return;
    const total = st.lwcBars.length;
    bundle.chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, total - 160),
      to: total + 4,
    });
  }

  function _renderChart() {
    if (!bundle || !st.bars.length) return;
    const stepMs = TF_MS[st.timeframe] || 60_000;
    const stepSec = Math.max(60, Math.floor(stepMs / 1000));

    st.lwcBars = _normBarsForLwc(st.bars);
    st.barMap = new Map(st.lwcBars.map(b => [b.time, b]));
    const secArr = st.lwcBars.map(b => b.time);

    const ma7Data  = _computeMaLineData(st.lwcBars, 7, 'ma7');
    const ma25Data = _computeMaLineData(st.lwcBars, 25, 'ma25');
    const ma60Data = _computeMaLineData(st.lwcBars, 60, 'ma60');
    st.maMap = new Map();
    ma7Data.forEach(d  => { const x = st.maMap.get(d.time) || {}; x.ma7 = d.value; st.maMap.set(d.time, x); });
    ma25Data.forEach(d => { const x = st.maMap.get(d.time) || {}; x.ma25 = d.value; st.maMap.set(d.time, x); });
    ma60Data.forEach(d => { const x = st.maMap.get(d.time) || {}; x.ma60 = d.value; st.maMap.set(d.time, x); });

    bundle.candleSeries.setData(st.lwcBars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    bundle.volSeries.setData(st.lwcBars.map(b => ({
      time: b.time,
      value: b.volume,
      color: b.close >= b.open ? 'rgba(22, 199, 132, 0.55)' : 'rgba(246, 70, 93, 0.55)',
    })));
    bundle.ma7Series.setData(ma7Data);
    bundle.ma25Series.setData(ma25Data);
    bundle.ma60Series.setData(ma60Data);

    // Build clean Binance-style B/S markers (no dashed lines!)
    st.eventsBySec = new Map();
    const markers = [];
    let matchedMarkers = 0;
    const pushEv = (sec, html) => {
      if (!st.eventsBySec.has(sec)) st.eventsBySec.set(sec, []);
      st.eventsBySec.get(sec).push(html);
    };

    const trades = _filteredTrades();
    trades.forEach((tr) => {
      const eSec = _snapTimeToBarSec(secArr, tr.entryMs, stepSec);
      const xSec = (!isNaN(tr.exitMs) && tr.exitMs > 0) ? _snapTimeToBarSec(secArr, tr.exitMs, stepSec) : null;

      if (eSec != null) {
        matchedMarkers++;
        const isBuy = tr.direction === 'long';
        const actionLabel = isBuy ? '买入开多' : '卖出开空';
        const stratSuffix = tr.strategyMode ? ` [${tr.strategyMode}]` : '';
        pushEv(
          eSec,
          `<span style="color:${isBuy ? '#16c784' : '#f6465d'};font-weight:700">${isBuy ? 'B' : 'S'} ${actionLabel}</span> <b>${_esc(tr.displayId)}</b>@${tr.entryPrice.toFixed(2)}${_esc(stratSuffix)}`
        );
        markers.push({
          time: eSec,
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: isBuy ? '#16c784' : '#f6465d',
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text: `${isBuy ? 'B' : 'S'}`,
        });
      }

      if (xSec != null && tr.exitPrice != null) {
        matchedMarkers++;
        const isBuy = tr.direction === 'short'; // closing short = Buy
        const actionLabel = isBuy ? '买入平空' : '卖出平多';
        const pnlSign = (tr.pnl || 0) >= 0 ? '+' : '';
        const pnlColor = (tr.pnl || 0) >= 0 ? '#16c784' : '#f6465d';
        const pnlStr = tr.pnl != null ? ` <span style="color:${pnlColor};font-weight:700">${pnlSign}$${tr.pnl.toFixed(2)}</span>` : '';
        const rsnStr = tr.closeReason ? ` (${REASON_LABELS[tr.closeReason] || tr.closeReason})` : '';
        pushEv(
          xSec,
          `<span style="color:${isBuy ? '#16c784' : '#f6465d'};font-weight:700">${isBuy ? 'B' : 'S'} ${actionLabel}</span> <b>${_esc(tr.displayId)}</b>@${tr.exitPrice.toFixed(2)}${pnlStr}${_esc(rsnStr)}`
        );
        markers.push({
          time: xSec,
          position: isBuy ? 'belowBar' : 'aboveBar',
          color: isBuy ? '#16c784' : '#f6465d',
          shape: isBuy ? 'arrowUp' : 'arrowDown',
          text: `${isBuy ? 'B' : 'S'}`,
        });
      }
    });

    markers.sort((a, b) => a.time - b.time);
    bundle.candleSeries.setMarkers(markers);

    const statsEl = document.getElementById('klineModalStatsHint');
    if (statsEl) {
      const moreHint = st.hasMoreLeft ? ' · 向左拖动自动加载更早K线' : '';
      statsEl.textContent = `已加载 ${st.lwcBars.length} 根 ${st.timeframe} K线 · 买卖标记 ${matchedMarkers} 处${moreHint}`;
    }

    if (st.lwcBars.length > 0) {
      _updateHud(st.lwcBars[st.lwcBars.length - 1]);
      if (st.preserveLogicalRange) {
        bundle.chart.timeScale().setVisibleLogicalRange(st.preserveLogicalRange);
        st.preserveLogicalRange = null;
      } else {
        let anchorIdx = st.lwcBars.length - 1;
        if (trades.length) {
          const lastTr = trades[trades.length - 1];
          const anchorMs = (!isNaN(lastTr.exitMs) && lastTr.exitMs > 0) ? lastTr.exitMs : lastTr.entryMs;
          const aSec = _snapTimeToBarSec(secArr, anchorMs, stepSec);
          if (aSec != null) {
            const idx = secArr.indexOf(aSec);
            if (idx >= 0) anchorIdx = idx;
          }
        }
        const hi = Math.min(st.lwcBars.length + 4, anchorIdx + 30);
        const lo = Math.max(0, hi - 150);
        bundle.chart.timeScale().setVisibleLogicalRange({ from: lo, to: hi });
      }
    }

    const mainEl = document.getElementById('klineModalMainChart');
    if (mainEl && mainEl.clientWidth > 0) {
      bundle.chart.applyOptions({ width: mainEl.clientWidth, height: mainEl.clientHeight });
    }
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
    st.preserveLogicalRange = null;

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
    if (bundle) {
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
