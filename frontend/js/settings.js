/**
 * settings.js — 设置面板逻辑（全局设置 + 交易对管理）
 */

const SettingsManager = (() => {
  let symbols = [];
  let modalInstance = null;
  let editingId = null;

  // ─── Init ────────────────────────────────────────────────────────────
  function init() {
    modalInstance = new bootstrap.Modal(document.getElementById('symbolModal'));

    // slider label
    document.getElementById('symConfirm').addEventListener('input', (e) => {
      document.getElementById('symConfirmVal').textContent = e.target.value;
    });

    // global settings form
    document.getElementById('globalSettingsForm').addEventListener('submit', saveGlobalSettings);

    // add symbol
    document.getElementById('btnAddSymbol').addEventListener('click', openAddModal);
    document.getElementById('btnSaveSymbol').addEventListener('click', saveSymbol);

    // test telegram
    const btnTestTg = document.getElementById('btnTestTg');
    if (btnTestTg) {
      btnTestTg.addEventListener('click', testTelegramNotification);
    }
  }

  // ─── Test Telegram Notification ──────────────────────────────────────
  async function testTelegramNotification() {
    const btn = document.getElementById('btnTestTg');
    const originalText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>测试中…';

    try {
      // 先保存一次当前填写的 token 和 chat_id
      const payload = {
        telegram_token:   document.getElementById('telegramToken').value.trim(),
        telegram_chat_id: document.getElementById('telegramChatId').value.trim(),
      };
      await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const res = await fetch('/api/settings/test-telegram', { method: 'POST' });
      const data = await res.json();
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
    const mode = data.trading_mode || 'paper';
    document.querySelector(`input[name="tradingMode"][value="${mode}"]`).checked = true;
  }

  // ─── Save global settings ────────────────────────────────────────────
  async function saveGlobalSettings(e) {
    e.preventDefault();
    const payload = {
      binance_key:      document.getElementById('binanceKey').value,
      binance_secret:   document.getElementById('binanceSecret').value,
      http_proxy:       (document.getElementById('httpProxy')?.value || '').trim(),
      telegram_token:   document.getElementById('telegramToken').value,
      telegram_chat_id: document.getElementById('telegramChatId').value,
      trading_mode:     document.querySelector('input[name="tradingMode"]:checked').value,
      max_daily_loss:   document.getElementById('maxDailyLoss').value
    };
    try {
      const res = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const msg = document.getElementById('settingsSaveMsg');
        msg.style.display = 'block';
        setTimeout(() => { msg.style.display = 'none'; }, 3000);
      }
    } catch (err) {
      console.error('保存设置失败', err);
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
      tr.innerHTML = `
        <td class="fw-bold">${sym.symbol}</td>
        <td>${sym.timeframe}</td>
        <td>${sym.htf_timeframe}</td>
        <td>${sym.confirm_candles}</td>
        <td>${sym.leverage}x</td>
        <td>${(sym.risk_per_trade * 100).toFixed(1)}%</td>
        <td>${sym.rr_ratio}</td>
        <td>${sym.enabled ? '<span style="color:#3fb950">✅</span>' : '<span style="color:#f85149">❌</span>'}</td>
        <td>
          <button class="btn btn-xs btn-outline-secondary me-1" onclick="SettingsManager.editSymbol(${sym.id})">编辑</button>
          <button class="btn btn-xs btn-outline-danger" onclick="SettingsManager.deleteSymbol(${sym.id})">删除</button>
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
      const li = document.createElement('li');
      li.className = 'symbol-item';
      li.dataset.symbol = sym.symbol;
      li.innerHTML = `
        <div class="flex-fill">
          <div class="symbol-name">${sym.symbol}</div>
          <div class="symbol-price" id="price-${sym.symbol}">—</div>
        </div>
        <div class="symbol-dir flat" id="dir-${sym.symbol}">—</div>
      `;
      li.addEventListener('click', () => AppController.switchSymbol(sym.symbol));
      list.appendChild(li);
    });
    // Activate first
    if (symbols.length) {
      list.querySelector('.symbol-item')?.classList.add('active');
    }
  }

  // ─── Update other selects ──────────────────────────────────────────────
  function updateBacktestSymbolSelect() {
    const sel = document.getElementById('btSymbol');
    sel.innerHTML = symbols.map(s => `<option value="${s.symbol}">${s.symbol}</option>`).join('');
  }

  function updateStatsSymbolSelect() {
    const sel = document.getElementById('statsSymbol');
    sel.innerHTML = '<option value="">全部交易对</option>' +
      symbols.map(s => `<option value="${s.symbol}">${s.symbol}</option>`).join('');
  }

  // ─── Add modal ────────────────────────────────────────────────────────
  function openAddModal() {
    editingId = null;
    document.getElementById('symbolModalTitle').textContent = '添加交易对';
    document.getElementById('symbolId').value  = '';
    document.getElementById('symSymbol').value = '';
    document.getElementById('symTf').value     = '1m';
    document.getElementById('symHtf').value    = '15m';
    document.getElementById('symConfirm').value = 2;
    document.getElementById('symConfirmVal').textContent = '2';
    document.getElementById('symLeverage').value = 5;
    document.getElementById('symRisk').value     = 1;
    document.getElementById('symRR').value       = 1.5;
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
      timeframe:       document.getElementById('symTf').value,
      htf_timeframe:   document.getElementById('symHtf').value,
      confirm_candles: parseInt(document.getElementById('symConfirm').value),
      leverage:        parseInt(document.getElementById('symLeverage').value),
      risk_per_trade:  parseFloat(document.getElementById('symRisk').value) / 100,
      rr_ratio:        parseFloat(document.getElementById('symRR').value),
      enabled:         true
    };

    try {
      let res;
      if (editingId) {
        res = await fetch(`/api/symbols/${editingId}`, {
          method: 'PUT',
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
    editSymbol, deleteSymbol,
    updateSymbolPrice, updateSymbolDirection
  };
})();
