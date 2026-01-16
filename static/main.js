
// Farm Stall POS main.js — Teller-first UX & on-demand scanning
// - Preserves all existing endpoints & flows
// - Hides login card after login; shows compact Auth Bar
// - Forces clean start (no auto-login) on fresh page load
// - Product dropdown + search
// - Start/Stop camera on demand with ZXing; beep, cooldown & flash
// - Admin stats unchanged

let STATE = {
  user: null,
  products: [],
  cart: {}, // product_id -> {product_id, name, unit_price, qty}
  scanCooldown: false, // retained for compatibility (unused by new scanner)
};

// --- Helpers ---
function show(el) { el && el.classList.remove('hidden'); }
function hide(el) { el && el.classList.add('hidden'); }
function fmt(n)   { return (Math.round(n * 100) / 100).toFixed(2); }
async function api(path, opts = {}) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin'
  }, opts));
  if (!res.ok) {
    let err = 'Request failed';
    try { const j = await res.json(); err = j.error || JSON.stringify(j); } catch {}
    throw new Error(err);
  }
  try { return await res.json(); } catch { return {}; }
}

// Simple beep using Web Audio (tiny and instant)
function beep(durationMs = 120, frequency = 880) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = frequency;
    osc.connect(gain);
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0.1, ctx.currentTime);
    osc.start();
    setTimeout(() => { osc.stop(); ctx.close(); }, durationMs);
  } catch {}
}

// --- Visibility & Auth UI ---
function updateVisibility() {
  const tabs = document.getElementById('main-tabs');
  const contents = document.getElementById('tab-contents');
  const loginCard = document.getElementById('login-card');
  const authBar = document.getElementById('auth-bar');

  if (!STATE.user) {
    // Logged out
    show(loginCard);
    hide(authBar);
    hide(tabs);
    hide(contents);
    return;
  }

  // Logged in
  hide(loginCard);
  show(authBar);
  const au = document.getElementById('auth-user');
  if (au) au.textContent = `Logged in as ${STATE.user.username} (${STATE.user.role})`;
  show(tabs);
  show(contents);

  // Admin-only tabs

document.querySelectorAll('.admin-only').forEach(el => {
  if (STATE.user.role === 'admin') show(el); else hide(el);
});

}

async function refreshMe() {
  const me = await api('/api/me');
  if (me.logged_in) {
    STATE.user = { username: me.username, role: me.role };
    const s = document.getElementById('login-status');
    if (s) s.textContent = '';
    hide(document.getElementById('btn-login'));
    show(document.getElementById('btn-logout'));
  } else {
    STATE.user = null;
    const s = document.getElementById('login-status');
    if (s) s.textContent = '';
    show(document.getElementById('btn-login'));
    hide(document.getElementById('btn-logout'));
  }
  updateVisibility();
}

// --- Login / Logout ---
document.getElementById('btn-login')?.addEventListener('click', async () => {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    await refreshMe();
    await loadProducts();
    await loadTransactions();
    if (STATE.user && STATE.user.role === 'admin') {
      await loadSettings();
      await loadStats();
    }
    // Do not auto-start the scanner
  } catch (e) {
    const s = document.getElementById('login-status');
    if (s) s.textContent = e.message;
  }
});


async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  STATE.user = null; STATE.products = []; STATE.cart = {};
  stopScanner(); // ensure camera is off
  // Refresh session/UI state so Login button shows and tabs hide
  await refreshMe();
}


document.getElementById('btn-logout')?.addEventListener('click', doLogout);
document.getElementById('btn-logout-top')?.addEventListener('click', doLogout);

// --- Products ---
async function loadProducts() {
  if (!STATE.user) return;
  try {
    const products = await api('/api/products');
    STATE.products = products;

    // List (admin panel)
    const list = document.getElementById('products-list');
    if (list) {
      list.innerHTML = '';
      products.forEach(p => {
        const item = document.createElement('a');
        item.className = 'list-group-item list-group-item-action';
        item.textContent = `#${p.id} ${p.name} — ${fmt(p.price)} — BAR:${p.barcode} — Stock:${p.stock_qty}`;
        item.addEventListener('click', () => {
          document.getElementById('p-id').value = p.id;
          document.getElementById('p-name').value = p.name;
          document.getElementById('p-price').value = p.price;
          document.getElementById('p-barcode').value = p.barcode;
          document.getElementById('p-stock').value = p.stock_qty;
          const pid = document.getElementById('pur-product-id');
          if (pid) pid.value = p.id;
        });
        list.appendChild(item);
      });
    }
    
// --- Products filter (client-side match by name, id, barcode) ---
const productsFilter = document.getElementById('products-filter');
productsFilter?.addEventListener('input', () => {
  const q = productsFilter.value.trim().toLowerCase();
  const list = document.getElementById('products-list');
  if (!list) return;

  // Build a filtered array from STATE.products
  const filtered = (STATE.products || []).filter(p =>
    !q ||
    p.name.toLowerCase().includes(q) ||
    String(p.id) === q ||
    (p.barcode && p.barcode.toLowerCase().includes(q))
  );

  // Re-render list
  list.innerHTML = '';
  filtered.forEach(p => {
    const item = document.createElement('a');
    item.className = 'list-group-item list-group-item-action';
    item.textContent = `#${p.id} ${p.name} — ${fmt(p.price)} — BAR:${p.barcode} — Stock:${p.stock_qty}`;
    item.addEventListener('click', () => {
      document.getElementById('p-id').value = p.id;
      document.getElementById('p-name').value = p.name;
      document.getElementById('p-price').value = p.price;
      document.getElementById('p-barcode').value = p.barcode;
      document.getElementById('p-stock').value = p.stock_qty;
      const pid = document.getElementById('pur-product-id');
      if (pid) pid.value = p.id;
    });
    list.appendChild(item);
  });
});


  document.getElementById('btn-new-product')?.addEventListener('click', () => {
    const clearIds = [
      'p-id','p-name','p-price','p-barcode','p-stock',
      'pur-product-id','pur-qty','pur-price'
    ];
    clearIds.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    // Keep focus on name for quick add
    document.getElementById('p-name')?.focus();
  });

    // NEW: populate product dropdown on Teller toolbar
    const sel = document.getElementById('product-select');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = '<option value="">Select product…</option>';
      products.forEach(p => {
        const opt = document.createElement('option');
        opt.value = String(p.id);
        opt.textContent = `${p.name} — ${fmt(p.price)}`;
        sel.appendChild(opt);
      });
      if (prev) sel.value = prev;

      // One-time change handler (guard with a flag)
      if (!sel._boundChange) {
        sel.addEventListener('change', () => {
          const pid = parseInt(sel.value || '0', 10);
          const prod = STATE.products.find(x => x.id === pid);
          if (prod) addToCart(prod);
          sel.value = '';
        });
        sel._boundChange = true;
      }
    }

  } catch (e) {
    console.error('loadProducts', e);
  }
}

document.getElementById('btn-add-product')?.addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  const price = parseFloat(document.getElementById('p-price').value);
  const barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = parseInt(document.getElementById('p-stock').value || '0', 10);
  try {
    await api('/api/products', { method: 'POST', body: JSON.stringify({ name, price, barcode, stock_qty }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-update-product')?.addEventListener('click', async () => {
  const id = parseInt(document.getElementById('p-id').value || '0', 10);
  const name = document.getElementById('p-name').value.trim();
  const price = document.getElementById('p-price').value;
  const barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = document.getElementById('p-stock').value;
  try {
    await api('/api/products/update', { method: 'POST', body: JSON.stringify({ id, name, price, barcode, stock_qty }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-delete-product')?.addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  if (!name) return alert('Specify name to delete');
  try {
    await api(`/api/products/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

// --- Purchases & Suggested price (admin) ---
document.getElementById('btn-add-purchase')?.addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('pur-product-id').value || '0', 10);
  const qty = parseInt(document.getElementById('pur-qty').value || '0', 10);
  const price = parseFloat(document.getElementById('pur-price').value || '0');
  try {
    await api('/api/purchases', { method: 'POST', body: JSON.stringify({ product_id: pid, qty_added: qty, purchase_price: price }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-suggest-price')?.addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('pur-product-id').value || '0', 10);
  const markup = document.getElementById('markup-percent').value;
  try {
    const j = await api(`/api/products/${pid}/suggested_price${markup?`?markup=${markup}`:''}`);
    const out = document.getElementById('suggest-output');
    if (out) out.textContent = `WAC ${fmt(j.wac)} + ${j.markup_percent}% => Suggested ${fmt(j.suggested_price)}`;
  } catch (e) { alert(e.message); }
});

async function loadSettings() {
  try {
    const j = await api('/api/settings');
    const el = document.getElementById('markup-percent');
    if (el) el.value = j.markup_percent;
  } catch (e) {}
}

document.getElementById('btn-save-settings')?.addEventListener('click', async () => {
  const mp = parseFloat(document.getElementById('markup-percent').value || '0');
  try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ markup_percent: mp }) }); }
  catch (e) { alert(e.message); }
});

// --- Transactions ---
async function loadTransactions() {
  if (!STATE.user) return;
  try {
    const trs = await api('/api/transactions');
    const host = document.getElementById('transactions-list');
    if (!host) return;
    host.innerHTML = '';
    trs.forEach(t => {
      const card = document.createElement('div'); card.className = 'card mb-2';
      const body = document.createElement('div'); body.className = 'card-body';
      const h = document.createElement('div'); h.className = 'd-flex justify-content-between';
      h.innerHTML = `<strong>#${String(t.id).slice(0,8)}</strong><span>${new Date(t.date_time).toLocaleString()} — Total: ${fmt(t.total)}`;
      body.appendChild(h);
      const ul = document.createElement('ul'); ul.className = 'mt-2';
      t.lines.forEach(ln => {
        const li = document.createElement('li');
        li.textContent = `${ln.name} × ${ln.qty} @ ${fmt(ln.unit_price)} = ${fmt(ln.subtotal)}`;
        ul.appendChild(li);
      });
      body.appendChild(ul);
      card.appendChild(body); host.appendChild(card);
    });
  } catch (e) { console.error('loadTransactions', e); }
}
document.getElementById('btn-refresh-trans')?.addEventListener('click', loadTransactions);

// --- Cart ---
function renderCart() {
  const host = document.getElementById('cart'); if (!host) return;
  host.innerHTML = '';
  let total = 0;
  Object.values(STATE.cart).forEach(item => {
    const row = document.createElement('div'); row.className = 'list-group-item d-flex justify-content-between align-items-center';
    row.innerHTML = `<span>${item.name} × ${item.qty}</span><span>${fmt(item.unit_price)} ea</span>`;
    const btns = document.createElement('div');
    const plus = document.createElement('button'); plus.textContent = '+'; plus.className = 'btn btn-sm btn-outline-primary';
    plus.onclick = () => { item.qty += 1; renderCart(); };
    const minus = document.createElement('button'); minus.textContent = '−'; minus.className = 'btn btn-sm btn-outline-secondary ms-1';
    minus.onclick = () => { item.qty = Math.max(1, item.qty - 1); renderCart(); };
    const del = document.createElement('button'); del.textContent = 'Remove'; del.className = 'btn btn-sm btn-outline-danger ms-1';
    del.onclick = () => { delete STATE.cart[item.product_id]; renderCart(); };
    btns.appendChild(plus); btns.appendChild(minus); btns.appendChild(del);
    row.appendChild(btns);
    host.appendChild(row);
    total += item.qty * item.unit_price;
  });
  const t = document.getElementById('cart-total');
  if (t) t.textContent = fmt(total);
}

function addToCart(p) {
  const id = p.id;
  const existing = STATE.cart[id];
  if (existing) existing.qty += 1;
  else STATE.cart[id] = { product_id: id, name: p.name, unit_price: p.price, qty: 1 };
  renderCart();
}

document.getElementById('btn-checkout')?.addEventListener('click', async () => {
  const cart = Object.values(STATE.cart);
  if (cart.length === 0) return alert('Cart is empty');
  try {
    const j = await api('/api/transactions', { method: 'POST', body: JSON.stringify({ cart }) });
    STATE.cart = {}; renderCart();
    await loadTransactions();
    await loadProducts(); // reflect stock changes
    alert('Transaction #' + j.transaction_id + ' completed');
  } catch (e) { alert(e.message); }
});

// --- Search (unchanged behavior; IDs preserved) ---
const searchInput = document.getElementById('search');
searchInput?.addEventListener('input', () => {
  const q = searchInput.value.trim().toLowerCase();
  const host = document.getElementById('product-search-results'); if (!host) return;
  host.innerHTML = '';
  if (!q) return;
  const matches = STATE.products.filter(p => (
    p.name.toLowerCase().includes(q) ||
    String(p.id) === q ||
    p.barcode === q
  ));
  matches.forEach(p => {
    const a = document.createElement('a'); a.className = 'list-group-item list-group-item-action';
    a.textContent = `#${p.id} ${p.name} — ${fmt(p.price)} (stock ${p.stock_qty})`;
    a.onclick = () => addToCart(p);
    host.appendChild(a);
  });
});

// --- On-demand Scanner (ZXing) ---
let SCAN = { running: false, reader: null, controls: null, cooldown: false };

function flashOK() {
  const flash = document.getElementById('scanner-flash');
  if (!flash) return;
  flash.classList.add('ok');
  setTimeout(() => flash.classList.remove('ok'), 150);
}

async function startScanner() {
  if (SCAN.running) return;
  const panel = document.getElementById('scan-panel');
  const btnStart = document.getElementById('btn-start-scan');
  const btnStop  = document.getElementById('btn-stop-scan');
  const video    = document.getElementById('video');

  try {
    if (!window.ZXing || !ZXing.BrowserMultiFormatReader) throw new Error('Scanner library missing');
    panel.style.display = 'block';
    btnStart && btnStart.classList.add('hidden');
    btnStop  && btnStop.classList.remove('hidden');

    const codeReader = new ZXing.BrowserMultiFormatReader();
    SCAN.reader = codeReader;

    SCAN.controls = await codeReader.decodeFromVideoDevice(null, video, (result, err, ctrl) => {
      if (!result || SCAN.cooldown) return;

      const code = result.getText();
      flashOK(); beep(120, 880);

      SCAN.cooldown = true;
      setTimeout(() => SCAN.cooldown = false, 700);

      // Match exact barcode first; then fallback
      const p = STATE.products.find(x => x.barcode === code)
             || STATE.products.find(x => String(x.id) === code)
             || STATE.products.find(x => x.name.toLowerCase() === code.toLowerCase());
      if (p) addToCart(p);
    });

    SCAN.running = true;
  } catch (e) {
    console.warn('Scanner start failed', e);
    stopScanner();
  }
}

function stopScanner() {
  const panel = document.getElementById('scan-panel');
  const btnStart = document.getElementById('btn-start-scan');
  const btnStop  = document.getElementById('btn-stop-scan');
  try { SCAN.controls?.stop(); } catch {}
  try {
    const video = document.getElementById('video');
    const stream = video?.srcObject;
    if (stream) stream.getTracks().forEach(t => t.stop());
    if (video) video.srcObject = null;
  } catch {}
  SCAN = { running: false, reader: null, controls: null, cooldown: false };
  if (panel) panel.style.display = 'none';
  btnStop && btnStop.classList.add('hidden');
  btnStart && btnStart.classList.remove('hidden');
}

document.getElementById('btn-start-scan')?.addEventListener('click', startScanner);
document.getElementById('btn-stop-scan')?.addEventListener('click',  stopScanner);

// --- Stats (admin; unchanged) ---
function drawBarChart(canvas, labels, values) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const W = canvas.width, H = canvas.height;
  const max = Math.max(...values, 1);
  const pad = 40; const bw = (W - pad*2) / (values.length || 1) * 0.8;
  ctx.strokeStyle = '#333'; ctx.beginPath(); ctx.moveTo(pad, pad); ctx.lineTo(pad, H-pad); ctx.lineTo(W-pad, H-pad); ctx.stroke();
  values.forEach((v,i) => {
    const x = pad + (i+0.1) * (W - pad*2) / (values.length || 1);
    const h = (H - pad*2) * (v / max);
    const y = H - pad - h;
    ctx.fillStyle = '#2a6f3e'; ctx.fillRect(x, y, bw, h);
    ctx.fillStyle = '#000'; ctx.font = '12px sans-serif';
    ctx.fillText((labels[i] || '').slice(0,10), x, H - pad + 14);
    ctx.fillText(fmt(v), x, y - 4);
  });
}

async function loadStats() {
  try {
    const j = await api('/api/stats/today');
    const total = document.getElementById('stat-total');
    const items = document.getElementById('stat-items');
    const tx    = document.getElementById('stat-tx');
    const avg   = document.getElementById('stat-avg');
    if (total) total.textContent = fmt(j.total_sales_value);
    if (items) items.textContent = j.total_items_sold;
    if (tx) tx.textContent = j.transactions_count;
    if (avg) avg.textContent = j.avg_basket_size;

    const top = j.top_products || [];
    drawBarChart(document.getElementById('chart-top'), top.map(x=>x.name), top.map(x=>x.qty_sold));

    const hours = j.revenue_per_hour || [];
    drawBarChart(document.getElementById('chart-hourly'), hours.map(x=>String(x.hour)), hours.map(x=>x.revenue));
  } catch (e) { console.error('loadStats', e); }
}
document.getElementById('btn-refresh-stats')?.addEventListener('click', loadStats);

// --- Bootstrap (app init) ---
let _didAutoLogout = false;
(async function init(){
  // Kiosk requirement: never “auto-login” on page open.
  // Force a clean session once per page load.
  if (!_didAutoLogout) {
    try { await api('/api/logout', { method: 'POST' }); } catch {}
    _didAutoLogout = true;
  }

  updateVisibility();
  await refreshMe();

  if (STATE.user) {
    await loadProducts();
    await loadTransactions();
    if (STATE.user.role === 'admin') { await loadSettings(); await loadStats(); }
    // Do NOT auto-start the scanner
  }
})();
