'use strict';

// ── 时钟 ──────────────────────────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent = new Date().toLocaleTimeString('zh-CN');
}
setInterval(updateClock, 1000);
updateClock();

// ── 交易面板状态 ──────────────────────────────────────────────────────────
let _action    = 'open';    // 'open' | 'close'
let _direction = 'long';    // 'long' | 'short'
let _orderType = 'market';  // 'market' | 'limit'

function setAction(a) {
  _action = a;
  document.getElementById('btn-open').classList.toggle('active', a === 'open');
  document.getElementById('btn-close').classList.toggle('active', a === 'close');
  // 平仓时不需要止损价、止盈价、位移、联动止损
  const isOpen = a === 'open';
  document.getElementById('row-stop-price').style.display  = isOpen ? 'flex' : 'none';
  document.getElementById('row-tp-price').style.display    = isOpen ? 'flex' : 'none';
  document.getElementById('row-offset').style.display      = isOpen ? 'flex' : 'none';
  document.getElementById('row-auto-stop').style.display   = isOpen ? 'flex' : 'none';
}

function syncAutoStopLabel() {
  const cb  = document.getElementById('trade-auto-stop');
  const lbl = document.getElementById('auto-stop-label');
  if (!cb || !lbl) return;
  const checked = cb.checked;
  lbl.textContent = checked ? '已开启' : '已关闭';
  lbl.className   = 'toggle-label ' + (checked ? 'on' : 'off');
}

function setDirection(d) {
  _direction = d;
  document.getElementById('btn-long').classList.toggle('active', d === 'long');
  document.getElementById('btn-short').classList.toggle('active', d === 'short');
}

function setOrderType(t) {
  _orderType = t;
  document.getElementById('btn-mkt').classList.toggle('active', t === 'market');
  document.getElementById('btn-lmt').classList.toggle('active', t === 'limit');
  document.getElementById('row-limit-price').style.display = t === 'limit' ? 'flex' : 'none';
}

// ── 数据轮询 ──────────────────────────────────────────────────────────────
let _lastStatus = null;

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _lastStatus = await res.json();
    render(_lastStatus);
  } catch {
    setConnBadge({ connected: false, running: false, reconnecting: false });
  }
}
setInterval(fetchStatus, 1000);
fetchStatus();

// ── 渲染 ──────────────────────────────────────────────────────────────────
function render(data) {
  setConnBadge(data);
  renderAccount(data.account || {});
  renderContracts(data.contracts || []);
  syncSelects(data.contracts || []);
}

// ── 账户权益栏 ──────────────────────────────────────────────────────────
function renderAccount(acct) {
  // BASE = IB 将所有币种折算为账户基础货币的合并总权益，必须最优先。
  // 多币种账户（如同时跑 ES+HSI）若优先取 USD，港币部分权益会被丢弃。
  function pickPrimary(obj) {
    if (!obj || !Object.keys(obj).length) return { val: null, ccy: '' };
    if (obj['BASE'] != null) return { val: obj['BASE'], ccy: 'BASE' };
    if (obj['USD']  != null) return { val: obj['USD'],  ccy: 'USD'  };
    if (obj['HKD']  != null) return { val: obj['HKD'],  ccy: 'HKD'  };
    const k = Object.keys(obj)[0];
    return { val: obj[k], ccy: k };
  }

  const { val: nl,  ccy: nlCcy } = pickPrimary(acct.NetLiquidation);
  const { val: pnl }             = pickPrimary(acct.UnrealizedPnL);
  const { val: rpnl }            = pickPrimary(acct.RealizedPnL);
  const { val: af }              = pickPrimary(acct.AvailableFunds);

  // 账户净值（BASE 时不显示货币标签，单币种账户正常显示）
  const nlEl = document.getElementById('acct-net-liq');
  nlEl.textContent = nl != null
    ? `${fmtAcct(nl)}${nlCcy && nlCcy !== 'BASE' ? ' ' + nlCcy : ''}`
    : '—';

  // 持仓浮盈（UnrealizedPnL，仅当前未平仓头寸）
  setPnlEl('acct-unrealized-pnl', pnl);

  // 已实现盈亏（RealizedPnL，当前 IB session 内已平仓盈亏）
  setPnlEl('acct-realized-pnl', rpnl);

  // 可用资金
  document.getElementById('acct-avail-funds').textContent = af != null ? fmtAcct(af) : '—';
}

function setPnlEl(id, val) {
  const el = document.getElementById(id);
  if (val != null) {
    const n = parseFloat(val);
    el.textContent = (n >= 0 ? '+' : '') + fmtAcct(val);
    el.className = 'acct-val ' + (n > 0 ? 'pos' : n < 0 ? 'neg' : '');
  } else {
    el.textContent = '—';
    el.className = 'acct-val';
  }
}

function fmtAcct(v) {
  const n = parseFloat(v);
  if (isNaN(n)) return String(v);
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function setConnBadge(data) {
  const el = document.getElementById('conn-badge');
  if (data.connected && data.running) {
    el.textContent = '已连接';
    el.className = 'badge badge-online';
  } else if (data.reconnecting) {
    el.textContent = `重连中… (第${data.reconnect_attempt + 1}次)`;
    el.className = 'badge badge-reconnecting';
  } else {
    el.textContent = '离线';
    el.className = 'badge badge-offline';
  }
}

// ── 合约卡片 ──────────────────────────────────────────────────────────────
function renderContracts(contracts) {
  const grid = document.getElementById('contracts-grid');
  if (!contracts.length) { grid.innerHTML = '<div class="empty-hint">无启用合约</div>'; return; }

  const keys = contracts.map(c => `${c.symbol}@${c.exchange}`);
  [...grid.children].forEach(el => { if (!keys.includes(el.dataset.key)) el.remove(); });

  contracts.forEach(c => {
    const key = `${c.symbol}@${c.exchange}`;
    let card = grid.querySelector(`[data-key="${key}"]`);
    if (!card) {
      // 首次建卡：display 区（每秒刷新）+ controls 区（只建一次，不被轮询覆盖）
      card = document.createElement('div');
      card.className = 'contract-card';
      card.dataset.key = key;
      card.innerHTML = '<div class="card-display"></div>' + buildCardControls(c);
      grid.appendChild(card);
    }
    // 每秒只更新 display 区，controls 区保持不动
    card.querySelector('.card-display').innerHTML = buildCardDisplay(c);
    syncCardControlValues(card, c);
  });
}

function fmt(v, d = 2) {
  if (v == null) return '—';
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
}

// buildCardDisplay — 每秒刷新的显示区（状态、K线、信号）
function buildCardDisplay(c) {
  const posClass = c.position_side === 'long' ? 'pos-long' : c.position_side === 'short' ? 'pos-short' : 'pos-flat';
  const posLabel = c.position_side === 'long'  ? `多头 ${c.position}` :
                   c.position_side === 'short' ? `空头 ${Math.abs(c.position)}` : '平仓';

  // 持仓信息行
  let posInfoHTML = '';
  if (c.position_side !== 'flat') {
    const pnlClass = c.pnl_pts > 0 ? 'pnl-pos' : c.pnl_pts < 0 ? 'pnl-neg' : '';
    posInfoHTML = `
      <div class="pos-info-row">
        <div class="pos-info-cell">
          <div class="label">开仓均价</div>
          <div class="val">${fmt(c.entry_price, 2)}</div>
        </div>
        <div class="pos-info-cell">
          <div class="label">当前价</div>
          <div class="val">${fmt(c.current_price, 2)}</div>
        </div>
        <div class="pos-info-cell">
          <div class="label">止损价</div>
          <div class="val" style="color:var(--red)">${fmt(c.stop_price, 2)}</div>
        </div>
        <div class="pos-info-cell">
          <div class="label">止盈价</div>
          <div class="val" style="color:var(--green)">${fmt(c.take_profit_price, 2)}</div>
        </div>
        <div class="pos-info-cell">
          <div class="label">浮动盈亏(pt)</div>
          <div class="val ${pnlClass}">${fmt(c.pnl_pts, 2)}</div>
        </div>
      </div>`;
  }

  // K线数据行
  const kn = c.current_bar;
  const k1 = c.k1;
  const k2 = c.k2;
  const klineHTML = c.klines_ready && kn ? `
    <div class="kline-row">
      ${klineCell('Kn 价', kn.close)}
      ${klineCell('Kn 高', kn.high, 'var(--green)')}
      ${klineCell('K-1 高', k1?.high, 'var(--green)')} ${klineCell('K-2 高', k2?.high, 'var(--green)')}
    </div>
    <div class="kline-row">
      ${klineCell('成交量', kn.volume, null, 0)} ${klineCell('Kn 低', kn.low, 'var(--red)')}
      ${klineCell('K-1 低', k1?.low, 'var(--red)')} ${klineCell('K-2 低', k2?.low, 'var(--red)')}
    </div>`
    : `<div class="not-ready-hint">⏳ K 线缓冲区预热中（${c.bars_buffered}/2 根）</div>`;

  // 抄底摸顶当前目标价标签（只读展示）
  let reversalHTML = '';
  if (c.buy_target || c.sell_target) {
    const buyPart  = c.buy_target  ? `<span class="reversal-buy">抄底 ${fmt(c.buy_target)}</span>`   : '';
    const sellPart = c.sell_target ? `<span class="reversal-sell">摸顶 ${fmt(c.sell_target)}</span>` : '';
    const qtyPart  = `<span class="reversal-qty">${c.reversal_qty} 手</span>`;
    reversalHTML = `<div class="reversal-row">${buyPart}${sellPart}${qtyPart}</div>`;
  }

  // EMA 均线展示行（有值时才显示）
  let emaHTML = '';
  if (c.ema20 != null || c.ema40 != null || c.ema60 != null) {
    const emaStopOn = c.ema_stop_enabled === true;
    const emaRevOn  = c.ema_reversal_enabled === true;
    const emaColor = (emaStopOn || emaRevOn) ? 'var(--blue)' : 'var(--text-dim)';
    const emaParts = [
      c.ema20 != null ? `EMA20: ${fmt(c.ema20)}` : 'EMA20: —',
      c.ema40 != null ? `EMA40: ${fmt(c.ema40)}` : 'EMA40: —',
      c.ema60 != null ? `EMA60: ${fmt(c.ema60)}` : 'EMA60: —',
    ].join('　');
    // EMA反转策略状态
    let revStateHTML = '';
    if (emaRevOn) {
      const cp = c.current_price;
      const zone = (cp != null && c.ema20 != null && c.ema40 != null && c.ema60 != null)
        ? (cp > Math.max(c.ema20, c.ema40, c.ema60) ? 'LONG'
           : cp < Math.min(c.ema20, c.ema40, c.ema60) ? 'SHORT' : null)
        : null;
      const zoneLabel = zone === 'LONG' ? '<span style="color:var(--green)">多头区间</span>'
                      : zone === 'SHORT' ? '<span style="color:var(--red)">空头区间</span>'
                      : '<span style="color:var(--text-dim)">横盘区间</span>';
      const stateLabel = c.ema_trend_state === 'LONG'  ? '<span style="color:var(--green)">状态:多</span>'
                       : c.ema_trend_state === 'SHORT' ? '<span style="color:var(--red)">状态:空</span>'
                       : '<span style="color:var(--text-dim)">状态:—</span>';
      revStateHTML = `　${zoneLabel}　${stateLabel}`;
    }
    emaHTML = `<div class="reversal-row" style="color:${emaColor};font-size:12px">${emaParts}${revStateHTML}</div>`;
  }

  const sigOn = c.signal_enabled !== false;
  const sigText = sigOn ? (c.last_signal || '—') : '已暂停';
  const sigClass = !sigOn ? 'signal-paused' :
                   c.last_signal === 'CLOSE_LONG'  ? 'signal-close-long'  :
                   c.last_signal === 'CLOSE_SHORT' ? 'signal-close-short' : 'signal-none';
  const sigTime = c.last_signal_time ? ` @ ${c.last_signal_time.slice(11, 19)}` : '';

  // 静态止损展示行（设置了才显示）
  let staticStopHTML = '';
  if (c.static_long_stop || c.static_short_stop) {
    const lsPart  = c.static_long_stop  ? `<span style="color:var(--red)">多头止损: ${fmt(c.static_long_stop, 2)}</span>`  : '';
    const ssPart  = c.static_short_stop ? `<span style="color:var(--red)">空头止损: ${fmt(c.static_short_stop, 2)}</span>` : '';
    staticStopHTML = `<div class="reversal-row">${lsPart}${ssPart}</div>`;
  }

  return `
    <div class="card-header">
      <span class="card-title">${c.symbol}
        <small style="font-weight:400;font-size:12px;color:var(--text-dim)">${c.exchange} ${c.timeframe} ${c.expiry || '近月'}</small>
      </span>
      <span class="pos-badge ${posClass}">${posLabel}</span>
    </div>
    <div class="card-body">
      ${posInfoHTML}
      ${staticStopHTML}
      ${reversalHTML}
      ${klineHTML}
      ${emaHTML}
      <div class="signal-row ${sigClass}">双K止损信号: ${sigText}${sigTime}</div>
    </div>`;
}

function klineCell(label, val, color, d = 2) {
  return `<div class="kline-cell">
    <div class="label">${label}</div>
    <div class="val" style="${color ? `color:${color}` : ''}">${fmt(val, d)}</div>
  </div>`;
}

// buildCardControls — 只建一次的控制区（策略开关 + 抄底/摸顶）
function buildCardControls(c) {
  const key    = `${c.symbol}@${c.exchange}`;
  const safeId = key.replace('@', '-');
  const sigOn  = c.signal_enabled !== false;
  const emaOn  = c.ema_stop_enabled === true;
  const revOn  = c.ema_reversal_enabled === true;
  return `
    <div class="card-controls">
      <div class="ctrl-label">策略开关</div>
      <div class="ctrl-row ctrl-strategy">
        <span class="ctrl-strategy-item">
          <span class="ctrl-strategy-name">双K止损</span>
          <label class="toggle-switch toggle-xs">
            <input type="checkbox" id="cst-sig-${safeId}" ${sigOn ? 'checked' : ''}
                   onchange="setContractStrategy('${key}','signal')" />
            <span class="toggle-slider"></span>
          </label>
          <span id="cst-sig-lbl-${safeId}" class="toggle-label ${sigOn ? 'on' : 'off'}">${sigOn ? '已启用' : '已暂停'}</span>
        </span>
        <span class="ctrl-strategy-item">
          <span class="ctrl-strategy-name">均线止损</span>
          <label class="toggle-switch toggle-xs">
            <input type="checkbox" id="cst-ema-${safeId}" ${emaOn ? 'checked' : ''}
                   onchange="setContractStrategy('${key}','ema')" />
            <span class="toggle-slider"></span>
          </label>
          <span id="cst-ema-lbl-${safeId}" class="toggle-label ${emaOn ? 'on' : 'off'}">${emaOn ? '已启用' : '已暂停'}</span>
        </span>
        <span class="ctrl-strategy-item">
          <span class="ctrl-strategy-name">EMA反转开仓</span>
          <label class="toggle-switch toggle-xs">
            <input type="checkbox" id="cst-rev-${safeId}" ${revOn ? 'checked' : ''}
                   onchange="setContractStrategy('${key}','reversal')" />
            <span class="toggle-slider"></span>
          </label>
          <span id="cst-rev-lbl-${safeId}" class="toggle-label ${revOn ? 'on' : 'off'}">${revOn ? '已启用' : '已暂停'}</span>
        </span>
      </div>
      <div class="ctrl-row" id="cst-rev-qty-row-${safeId}" style="${revOn ? '' : 'display:none'}">
        <span class="ctrl-strategy-name" style="white-space:nowrap">EMA反转手数</span>
        <input id="cst-rev-qty-${safeId}" class="ctrl-input ctrl-qty" type="number" min="1" step="1"
               placeholder="${c.ema_reversal_qty ?? 1}" />
        <button class="btn btn-xs btn-secondary" onclick="setContractStrategy('${key}','reversal_qty')">确定</button>
      </div>
      <div class="ctrl-label" style="margin-top:4px">抄底 / 摸顶</div>
      <div class="ctrl-row">
        <input id="rc-buy-${safeId}"  class="ctrl-input" type="number" step="0.25" placeholder="抄底价" />
        <input id="rc-sell-${safeId}" class="ctrl-input" type="number" step="0.25" placeholder="摸顶价" />
        <input id="rc-qty-${safeId}"  class="ctrl-input ctrl-qty" type="number" min="1" step="1" placeholder="手数" />
        <button class="btn btn-xs btn-primary"   onclick="setCardReversal('${key}')">设置</button>
        <button class="btn btn-xs btn-secondary" onclick="clearCardReversal('${key}')">清空</button>
      </div>
      <div id="rc-msg-${safeId}" class="msg"></div>
    </div>`;
}

// 同步卡片控制区的动态值（placeholder + 策略开关状态）
function syncCardControlValues(card, c) {
  const key    = `${c.symbol}@${c.exchange}`;
  const safeId = key.replace('@', '-');

  // 抄底/摸顶输入框 placeholder
  const buyEl  = document.getElementById(`rc-buy-${safeId}`);
  const sellEl = document.getElementById(`rc-sell-${safeId}`);
  const qtyEl  = document.getElementById(`rc-qty-${safeId}`);
  if (buyEl  && document.activeElement !== buyEl)
    buyEl.placeholder  = c.buy_target  ? `抄底 ${fmt(c.buy_target)}`  : '抄底价';
  if (sellEl && document.activeElement !== sellEl)
    sellEl.placeholder = c.sell_target ? `摸顶 ${fmt(c.sell_target)}` : '摸顶价';
  if (qtyEl  && document.activeElement !== qtyEl && !qtyEl.value)
    qtyEl.placeholder  = `手数 (当前 ${c.reversal_qty})`;

  // 策略开关同步（以服务端状态为准）
  const sigOn  = c.signal_enabled !== false;
  const emaOn  = c.ema_stop_enabled === true;
  const revOn  = c.ema_reversal_enabled === true;
  const sigCb  = document.getElementById(`cst-sig-${safeId}`);
  const sigLbl = document.getElementById(`cst-sig-lbl-${safeId}`);
  const emaCb  = document.getElementById(`cst-ema-${safeId}`);
  const emaLbl = document.getElementById(`cst-ema-lbl-${safeId}`);
  const revCb  = document.getElementById(`cst-rev-${safeId}`);
  const revLbl = document.getElementById(`cst-rev-lbl-${safeId}`);
  const revQtyRow = document.getElementById(`cst-rev-qty-row-${safeId}`);
  const revQtyEl  = document.getElementById(`cst-rev-qty-${safeId}`);
  if (sigCb)  sigCb.checked = sigOn;
  if (sigLbl) { sigLbl.textContent = sigOn ? '已启用' : '已暂停'; sigLbl.className = 'toggle-label ' + (sigOn ? 'on' : 'off'); }
  if (emaCb)  emaCb.checked = emaOn;
  if (emaLbl) { emaLbl.textContent = emaOn ? '已启用' : '已暂停'; emaLbl.className = 'toggle-label ' + (emaOn ? 'on' : 'off'); }
  if (revCb)  revCb.checked = revOn;
  if (revLbl) { revLbl.textContent = revOn ? '已启用' : '已暂停'; revLbl.className = 'toggle-label ' + (revOn ? 'on' : 'off'); }
  if (revQtyRow) revQtyRow.style.display = revOn ? '' : 'none';
  if (revQtyEl && document.activeElement !== revQtyEl && !revQtyEl.value)
    revQtyEl.placeholder = `手数 (当前 ${c.ema_reversal_qty ?? 1})`;
}

// ── 下拉框同步 ───────────────────────────────────────────────────────────
function syncSelects(contracts) {
  ['trade-key', 'stop-key'].forEach(id => {
    const sel = document.getElementById(id);
    const cur = sel.value;
    const opts = contracts.map(c => {
      const key = `${c.symbol}@${c.exchange}`;
      const pos = c.position !== 0 ? ` [${c.position > 0 ? '多' : '空'} ${Math.abs(c.position)}]` : '';
      const stop = [
        c.static_long_stop  ? `多SL:${c.static_long_stop}`  : '',
        c.static_short_stop ? `空SL:${c.static_short_stop}` : '',
      ].filter(Boolean).join(' ');
      const stopStr = stop ? ` [${stop}]` : '';
      const tp   = c.take_profit_price ? ` TP:${c.take_profit_price}` : '';
      return `<option value="${key}"${key === cur ? ' selected' : ''}>${c.symbol}@${c.exchange}${pos}${stopStr}${tp}</option>`;
    });
    sel.innerHTML = '<option value="">— 选择合约 —</option>' + opts.join('');
    if (cur) sel.value = cur;
  });
}

// ── 统一交易提交 ──────────────────────────────────────────────────────────
async function submitTrade() {
  const key = document.getElementById('trade-key').value;
  if (!key) return showMsg('trade-msg', '请选择合约', false);
  const [symbol, exchange] = key.split('@');

  if (_action === 'open') {
    const qty = parseFloat(document.getElementById('trade-qty').value);
    if (!qty || qty <= 0) return showMsg('trade-msg', '手数必须大于 0', false);

    const body = { symbol, exchange, direction: _direction, qty, order_type: _orderType };

    if (_orderType === 'limit') {
      const lp = parseFloat(document.getElementById('trade-limit-price').value);
      if (lp > 0) {
        // 用户已填写限价，直接使用
        body.limit_price = lp;
      } else {
        // 限价为空：用市价 ± 位移自动计算
        const offset = parseFloat(document.getElementById('trade-offset').value) || 0;
        const contract = (_lastStatus?.contracts || []).find(c => `${c.symbol}@${c.exchange}` === key);
        const cp = contract?.current_price;
        if (!cp) return showMsg('trade-msg', '无法获取当前价，请手动填写限价', false);
        body.limit_price = _direction === 'long'
          ? Math.round((cp - offset) * 100) / 100
          : Math.round((cp + offset) * 100) / 100;
      }
    }

    const sp = parseFloat(document.getElementById('trade-stop-price').value);
    if (sp) body.stop_price = sp;

    const tp = parseFloat(document.getElementById('trade-tp-price').value);
    if (tp) body.take_profit_price = tp;

    const dir = _direction === 'long' ? '多' : '空';
    const lpLabel = body.limit_price ? `@${body.limit_price}` : '';
    const typeLabel = _orderType === 'market' ? '市价' : `限价${lpLabel}`;
    const extra = [sp ? '止损@' + sp : '', tp ? '止盈@' + tp : ''].filter(Boolean).join(' ');

    // 发送开仓请求（手动 fetch，以便根据结果决定是否追加联动止损）
    try {
      const res = await fetch('/api/trade/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) { showMsg('trade-msg', data.detail || '开仓失败', false); return; }

      // 开仓联动止损
      const autoStop = document.getElementById('trade-auto-stop')?.checked;
      if (autoStop) {
        const contract = (_lastStatus?.contracts || []).find(c => `${c.symbol}@${c.exchange}` === key);
        const kn = contract?.current_bar;
        const k1 = contract?.k1;
        if (kn && k1) {
          const stopBody = { symbol, exchange };
          let stopPrice;
          if (_direction === 'long') {
            stopPrice = Math.min(kn.low, k1.low);
            stopBody.long_stop = stopPrice;
          } else {
            stopPrice = Math.max(kn.high, k1.high);
            stopBody.short_stop = stopPrice;
          }
          const stopRes = await fetch('/api/trade/set_stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(stopBody),
          });
          if (stopRes.ok) {
            showMsg('trade-msg',
              `开仓已发送（${dir} ${qty} 手 ${typeLabel}${extra ? ' ' + extra : ''}）` +
              ` + 静态止损@${stopPrice}`, true);
          } else {
            const sd = await stopRes.json();
            showMsg('trade-msg',
              `开仓已发送，止损设置失败: ${sd.detail || '未知错误'}`, false);
          }
        } else {
          showMsg('trade-msg',
            `开仓已发送（${dir} ${qty} 手 ${typeLabel}），K线不足无法设联动止损（需 Kn+K-1）`, true);
        }
      } else {
        showMsg('trade-msg',
          `开仓已发送（${dir} ${qty} 手 ${typeLabel}${extra ? ' ' + extra : ''}）`, true);
      }
    } catch (e) {
      showMsg('trade-msg', `网络错误: ${e.message}`, false);
    }

  } else {
    const qty = parseFloat(document.getElementById('trade-qty').value);
    if (!qty || qty <= 0) return showMsg('trade-msg', '手数必须大于 0', false);
    const body = { symbol, exchange, qty, order_type: _orderType };

    if (_orderType === 'limit') {
      const lp = parseFloat(document.getElementById('trade-limit-price').value);
      if (!lp) return showMsg('trade-msg', '限价平仓必须填限价', false);
      body.limit_price = lp;
    }

    const typeLabel = _orderType === 'market' ? '市价' : '限价';
    await apiCall('/api/trade/close', body, 'trade-msg', `平仓已发送（${qty} 手 ${typeLabel}）`);
  }
}

// ── 静态止损设置 ──────────────────────────────────────────────────────────
async function submitSetStop() {
  const key = document.getElementById('stop-key').value;
  if (!key) return showMsg('stop-msg', '请选择合约', false);
  const ls = parseFloat(document.getElementById('stop-long').value);
  const ss = parseFloat(document.getElementById('stop-short').value);
  if (!ls && !ss) return showMsg('stop-msg', '请至少填写一个止损价', false);
  const [symbol, exchange] = key.split('@');
  const body = { symbol, exchange };
  if (ls > 0) body.long_stop  = ls;
  if (ss > 0) body.short_stop = ss;
  const parts = [ls > 0 ? `多头@${ls}` : '', ss > 0 ? `空头@${ss}` : ''].filter(Boolean);
  await apiCall('/api/trade/set_stop', body, 'stop-msg', `静态止损已设置: ${parts.join(' / ')}`);
}

async function submitCancelStop(side) {
  const key = document.getElementById('stop-key').value;
  if (!key) return showMsg('stop-msg', '请选择合约', false);
  const [symbol, exchange] = key.split('@');
  const label = side === 'long' ? '多头' : side === 'short' ? '空头' : '全部';
  await apiCall('/api/trade/cancel_stop', { symbol, exchange, side }, 'stop-msg', `${label}静态止损已撤销`);
}

// ── 抄底摸顶（卡片内联操作）────────────────────────────────────────────
async function setCardReversal(key) {
  const safeId = key.replace('@', '-');
  const [symbol, exchange] = key.split('@');
  const buy  = parseFloat(document.getElementById(`rc-buy-${safeId}`).value);
  const sell = parseFloat(document.getElementById(`rc-sell-${safeId}`).value);
  const qty  = parseFloat(document.getElementById(`rc-qty-${safeId}`).value);
  const body = { symbol, exchange };
  if (!isNaN(buy)  && buy  > 0) body.buy_target  = buy;
  if (!isNaN(sell) && sell > 0) body.sell_target = sell;
  if (!isNaN(qty)  && qty  > 0) body.qty = qty;
  if (!body.buy_target && !body.sell_target)
    return showMsg(`rc-msg-${safeId}`, '请至少填写一个目标价', false);
  const parts = [];
  if (body.buy_target)  parts.push(`抄底@${buy}`);
  if (body.sell_target) parts.push(`摸顶@${sell}`);
  await apiCall('/api/params/reversal', body, `rc-msg-${safeId}`, `已设置: ${parts.join(' / ')}`);
  // 设置成功后清空输入框
  document.getElementById(`rc-buy-${safeId}`).value  = '';
  document.getElementById(`rc-sell-${safeId}`).value = '';
}

async function clearCardReversal(key) {
  const safeId = key.replace('@', '-');
  const [symbol, exchange] = key.split('@');
  await apiCall('/api/params/reversal',
    { symbol, exchange, buy_target: 0, sell_target: 0 },
    `rc-msg-${safeId}`, '已清空');
}

// ── 策略参数 ──────────────────────────────────────────────────────────────
async function setContractStrategy(key, type) {
  const safeId = key.replace('@', '-');
  const [symbol, exchange] = key.split('@');
  const body = { symbol, exchange };
  let msg;
  if (type === 'signal') {
    const enabled = document.getElementById(`cst-sig-${safeId}`).checked;
    body.signal_enabled = enabled;
    const lbl = document.getElementById(`cst-sig-lbl-${safeId}`);
    if (lbl) { lbl.textContent = enabled ? '已启用' : '已暂停'; lbl.className = 'toggle-label ' + (enabled ? 'on' : 'off'); }
    msg = enabled ? '双K止损已启用' : '双K止损已暂停';
  } else if (type === 'ema') {
    const enabled = document.getElementById(`cst-ema-${safeId}`).checked;
    body.ema_stop_enabled = enabled;
    const lbl = document.getElementById(`cst-ema-lbl-${safeId}`);
    if (lbl) { lbl.textContent = enabled ? '已启用' : '已暂停'; lbl.className = 'toggle-label ' + (enabled ? 'on' : 'off'); }
    msg = enabled ? '均线止损已启用' : '均线止损已暂停';
  } else if (type === 'reversal') {
    const enabled = document.getElementById(`cst-rev-${safeId}`).checked;
    body.ema_reversal_enabled = enabled;
    const lbl = document.getElementById(`cst-rev-lbl-${safeId}`);
    if (lbl) { lbl.textContent = enabled ? '已启用' : '已暂停'; lbl.className = 'toggle-label ' + (enabled ? 'on' : 'off'); }
    const qtyRow = document.getElementById(`cst-rev-qty-row-${safeId}`);
    if (qtyRow) qtyRow.style.display = enabled ? '' : 'none';
    msg = enabled ? 'EMA反转开仓已启用' : 'EMA反转开仓已暂停';
  } else if (type === 'reversal_qty') {
    const qtyEl = document.getElementById(`cst-rev-qty-${safeId}`);
    const qty = parseFloat(qtyEl?.value);
    if (!qty || qty <= 0) return showMsg(`rc-msg-${safeId}`, '手数必须大于 0', false);
    body.ema_reversal_qty = qty;
    msg = `EMA反转手数已更新: ${qty}`;
    if (qtyEl) qtyEl.value = '';
  }
  await apiCall('/api/params/contract_strategy', body, `rc-msg-${safeId}`, msg);
}

async function saveStrategy() {
  const v = document.getElementById('param-cooldown').value;
  if (!v) return;
  await apiCall('/api/params/strategy', { signal_cooldown_sec: parseInt(v) }, 'strategy-msg', '冷却时间已保存');
}

// ── 通知测试 ──────────────────────────────────────────────────────────────
async function testNotify() {
  await apiCall('/api/notify/test', null, 'notify-msg', 'Telegram 消息已发送', 'POST', true);
}

// ── 通用请求工具 ──────────────────────────────────────────────────────────
async function apiCall(url, body, msgId, successText, method = 'POST', noBody = false) {
  try {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (!noBody && body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    const data = await res.json();
    if (res.ok) showMsg(msgId, successText, true);
    else showMsg(msgId, data.detail || '操作失败', false);
  } catch (e) {
    showMsg(msgId, `网络错误: ${e.message}`, false);
  }
}

function showMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  setTimeout(() => { el.textContent = ''; el.className = 'msg'; }, 5000);
}
