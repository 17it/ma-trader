/**
 * settings.js — 设置面板逻辑（全局设置 + 交易对管理）
 */

const SettingsManager = (() => {
  let symbols = [];
  let modalInstance = null;
  let editingId = null;

  function _esc(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
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
      // 先保存一次当前填写的代理、token 和 chat_id
      const payload = {
        http_proxy:       (document.getElementById('httpProxy')?.value || '').trim(),
        telegram_token:   document.getElementById('telegramToken').value.trim(),
        telegram_chat_id: document.getElementById('telegramChatId').value.trim(),
      };
      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const res = await fetch('/api/settings/test-telegram', { method: 'POST' });
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
      binance_key:      document.getElementById('binanceKey').value.trim(),
      binance_secret:   document.getElementById('binanceSecret').value.trim(),
      http_proxy:       (document.getElementById('httpProxy')?.value || '').trim(),
      telegram_token:   document.getElementById('telegramToken').value.trim(),
      telegram_chat_id: document.getElementById('telegramChatId').value.trim(),
      max_daily_loss:   document.getElementById('maxDailyLoss').value.trim() || '5'
    };

    try {
      const res = await fetch('/api/settings', {
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
      alert('❌ 保存设置失败:\n' + err.message + '\n\n提示：如果是在 Ubuntu 服务器上，请检查 ma-trader/data 目录的读写权限：\nsudo chown -R $USER:$USER data && chmod -R 775 data');
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
      tr.innerHTML = `
        <td class="fw-bold">${_esc(sym.symbol)}</td>
        <td>${_esc(sym.timeframe)}</td>
        <td>${_esc(sym.htf_timeframe)}</td>
        <td>${_esc(sym.confirm_candles)}</td>
        <td>${_esc(sym.leverage)}x</td>
        <td>${(Number(sym.risk_per_trade) * 100).toFixed(1)}%</td>
        <td>${_esc(sym.rr_ratio)}</td>
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

  // ─── Update other selects ──────────────────────────────────────────────
  function updateBacktestSymbolSelect() {
    const sel = document.getElementById('btSymbol');
    sel.innerHTML = symbols.map(s => `<option value="${_esc(s.symbol)}">${_esc(s.symbol)}</option>`).join('');
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
      timeframe:       document.getElementById('symTf').value,
      htf_timeframe:   document.getElementById('symHtf').value,
      confirm_candles: parseInt(document.getElementById('symConfirm').value),
      leverage:        parseInt(document.getElementById('symLeverage').value),
      risk_per_trade:  parseFloat(document.getElementById('symRisk').value) / 100,
      rr_ratio:        parseFloat(document.getElementById('symRR').value),
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
