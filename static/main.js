
// Farm Stall POS main.js — Updated
// - Robust Product CRUD
// - Transactions view ordered by id desc
// - Hide tabs before login; role-based visibility
// - Stock system and purchase recording
// - Suggested price helper (WAC + markup)
// - Admin-only stats with simple canvas charts

let STATE = {
  user: null,
  products: [],
  cart: {}, // product_id -> {product_id, name, unit_price, qty}
  scanCooldown: false,
};

// ---------- Helpers ----------
function show(el) { el.classList.remove('hidden'); }
function hide(el) { el.classList.add('hidden'); }
function fmt(n) { return (Math.round(n * 100) / 100).toFixed(2); }

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

function updateVisibility() {
  const tabs = document.getElementById('main-tabs');
  const contents = document.getElementById('tab-contents');
  if (!STATE.user) { hide(tabs); hide(contents); return; }
  show(tabs); show(contents);
  // Admin-only tabs
  document.querySelectorAll('.admin-only').forEach(el => {
    if (STATE.user.role === 'admin') show(el); else hide(el);
  });
}

async function refreshMe() {
  const me = await api('/api/me');
  if (me.logged_in) {
    STATE.user = { username: me.username, role: me.role };
    document.getElementById('login-status').textContent = `Logged in as ${me.username} (${me.role})`;
    hide(document.getElementById('btn-login'));
    show(document.getElementById('btn-logout'));
  } else {
    STATE.user = null;
    document.getElementById('login-status').textContent = '';
    show(document.getElementById('btn-login'));
    hide(document.getElementById('btn-logout'));
  }
  updateVisibility();
}

// ---------- Login/Logout ----------
document.getElementById('btn-login').addEventListener('click', async () => {
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
    initScanner();
  } catch (e) {
    document.getElementById('login-status').textContent = e.message;
  }
});

document.getElementById('btn-logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  STATE.user = null; STATE.products = []; STATE.cart = {};
  updateVisibility();
});

// ---------- Products ----------
async function loadProducts() {
  if (!STATE.user) return;
  try {
    const products = await api('/api/products');
    STATE.products = products;
    const list = document.getElementById('products-list');
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
        document.getElementById('pur-product-id').value = p.id;
      });
      list.appendChild(item);
    });
  } catch (e) { console.error('loadProducts', e); }
}

document.getElementById('btn-add-product').addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  const price = parseFloat(document.getElementById('p-price').value);
  const barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = parseInt(document.getElementById('p-stock').value || '0');
  try {
    await api('/api/products', { method: 'POST', body: JSON.stringify({ name, price, barcode, stock_qty }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-update-product').addEventListener('click', async () => {
  const id = parseInt(document.getElementById('p-id').value || '0');
  const name = document.getElementById('p-name').value.trim();
  const price = document.getElementById('p-price').value;
  const barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = document.getElementById('p-stock').value;
  try {
    await api('/api/products/update', { method: 'POST', body: JSON.stringify({ id, name, price, barcode, stock_qty }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-delete-product').addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  if (!name) return alert('Specify name to delete');
  try {
    await api(`/api/products/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

// ---------- Purchases & Suggested Price ----------
document.getElementById('btn-add-purchase').addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('pur-product-id').value || '0');
  const qty = parseInt(document.getElementById('pur-qty').value || '0');
  const price = parseFloat(document.getElementById('pur-price').value || '0');
  try {
    await api('/api/purchases', { method: 'POST', body: JSON.stringify({ product_id: pid, qty_added: qty, purchase_price: price }) });
    await loadProducts();
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-suggest-price').addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('pur-product-id').value || '0');
  const markup = document.getElementById('markup-percent').value;
  try {
    const j = await api(`/api/products/${pid}/suggested_price${markup?`?markup=${markup}`:''}`);
    const out = document.getElementById('suggest-output');
    out.textContent = `WAC ${fmt(j.wac)} + ${j.markup_percent}% => Suggested ${fmt(j.suggested_price)}`;
  } catch (e) { alert(e.message); }
});

async function loadSettings() {
  try {
    const j = await api('/api/settings');
    document.getElementById('markup-percent').value = j.markup_percent;
  } catch (e) {}
}

document.getElementById('btn-save-settings').addEventListener('click', async () => {
  const mp = parseFloat(document.getElementById('markup-percent').value || '0');
  try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ markup_percent: mp }) }); } catch (e) { alert(e.message); }
});

// ---------- Transactions ----------
async function loadTransactions() {
  if (!STATE.user) return;
  try {
    const trs = await api('/api/transactions');
    const host = document.getElementById('transactions-list');
    host.innerHTML = '';
    trs.forEach(t => {
      const card = document.createElement('div'); card.className = 'card mb-2';
      const body = document.createElement('div'); body.className = 'card-body';
      const h = document.createElement('div'); h.className = 'd-flex justify-content-between';
      h.innerHTML = `<strong>#${t.id}</strong><span>${new Date(t.date_time).toLocaleString()} — Total: ${fmt(t.total)}`;
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

document.getElementById('btn-refresh-trans').addEventListener('click', loadTransactions);

// ---------- Teller & Cart ----------
function renderCart() {
  const host = document.getElementById('cart'); host.innerHTML = '';
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
  document.getElementById('cart-total').textContent = fmt(total);
}

function addToCart(p) {
  const id = p.id;
  const existing = STATE.cart[id];
  if (existing) existing.qty += 1; else STATE.cart[id] = { product_id: id, name: p.name, unit_price: p.price, qty: 1 };
  renderCart();
}

document.getElementById('btn-checkout').addEventListener('click', async () => {
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

// ---------- Search ----------
const searchInput = document.getElementById('search');
searchInput.addEventListener('input', () => {
  const q = searchInput.value.trim().toLowerCase();
  const host = document.getElementById('product-search-results'); host.innerHTML = '';
  if (!q) return;
  const matches = STATE.products.filter(p => (
    p.name.toLowerCase().includes(q) || String(p.id) === q || p.barcode === q
  ));
  matches.forEach(p => {
    const a = document.createElement('a'); a.className = 'list-group-item list-group-item-action';
    a.textContent = `#${p.id} ${p.name} — ${fmt(p.price)} (stock ${p.stock_qty})`;
    a.onclick = () => addToCart(p);
    host.appendChild(a);
  });
});

// ---------- Scanner (ZXing placeholder) ----------
async function initScanner() {
  const video = document.getElementById('video');
  if (!video) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } });
    video.srcObject = stream; await video.play();
    // Hook in ZXing UMD here if you want real decoding.
    const scanLoop = async () => {
      if (!STATE.user) return; // stop when logged out
      setTimeout(scanLoop, 1000);
    };
    scanLoop();
  } catch (e) {
    console.warn('Scanner init failed', e);
  }
}

// ---------- Stats (simple canvas charts) ----------
function drawBarChart(canvas, labels, values) {
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const W = canvas.width, H = canvas.height;
  const max = Math.max(...values, 1);
  const pad = 40; const bw = (W - pad*2) / (values.length || 1) * 0.8;
  ctx.strokeStyle = '#333'; ctx.beginPath(); ctx.moveTo(pad, pad); ctx.lineTo(pad, H-pad); ctx.lineTo(W-pad, H-pad); ctx.stroke();
  values.forEach((v,i) => {
    const x = pad + (i+0.1) * (W - pad*2) / values.length;
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
    document.getElementById('stat-total').textContent = fmt(j.total_sales_value);
    document.getElementById('stat-items').textContent = j.total_items_sold;
    document.getElementById('stat-tx').textContent = j.transactions_count;
    document.getElementById('stat-avg').textContent = j.avg_basket_size;
    const top = j.top_products || []; const labels = top.map(x => x.name); const values = top.map(x => x.qty_sold);
    drawBarChart(document.getElementById('chart-top'), labels, values);
    const hours = j.revenue_per_hour || []; const hlabels = hours.map(x => String(x.hour)); const hvals = hours.map(x => x.revenue);
    drawBarChart(document.getElementById('chart-hourly'), hlabels, hvals);
  } catch (e) { console.error('loadStats', e); }
}

document.getElementById('btn-refresh-stats').addEventListener('click', loadStats);

// ---------- Bootstrap ----------
(async function init(){
  updateVisibility();   // hide tabs before login
  await refreshMe();
  if (STATE.user) {
    await loadProducts();
    await loadTransactions();
    if (STATE.user.role === 'admin') { await loadSettings(); await loadStats(); }
    initScanner();
  }
})();
