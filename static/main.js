
// Farm Stall POS main.js — Teller-first UX, thin cards for Products, full Users management
// - All tabs can scroll (CSS in index.html), except Teller which stays one-screen.
// - Products: thin card list + filter + modal editor (Add/Update/Delete/Purchases/Settings).
// - Users: list + filter + edit (Add/Update/Delete/Active/Role).
// - On-demand scanner (ZXing), session auth, stats/transactions unchanged.

let STATE = {
  user: null,
  products: [],
  cart: {},                 // product_id -> { product_id, name, unit_price, qty }
  users: [],
  selectedUser: null,       // { username, role, active } from /api/users
  scanCooldown: false
};

// ---------- Helpers ----------
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

// Tiny beep for scan success
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

// ---------- Visibility & Auth UI ----------
function updateVisibility() {
  const tabs = document.getElementById('main-tabs');
  const contents = document.getElementById('tab-contents');
  const loginCard = document.getElementById('login-card');
  const authBar = document.getElementById('auth-bar');

  if (!STATE.user) {
    show(loginCard);
    hide(authBar);
    hide(tabs);
    hide(contents);
    return;
  }

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
    const s = document.getElementById('login-status'); if (s) s.textContent = '';
    hide(document.getElementById('btn-login'));
    show(document.getElementById('btn-logout'));
  } else {
    STATE.user = null;
    const s = document.getElementById('login-status'); if (s) s.textContent = '';
    show(document.getElementById('btn-login'));
    hide(document.getElementById('btn-logout'));
  }
  updateVisibility();
}

// ---------- Login / Logout ----------
document.getElementById('btn-login')?.addEventListener('click', async () => {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    await refreshMe();
    await loadProducts();    // prep for Products tab straight away
    await loadTransactions();
    if (STATE.user && STATE.user.role === 'admin') {
      await loadSettings();
      await loadStats();
      await loadUsers();     // prep Users tab too
    }
  } catch (e) {
    const s = document.getElementById('login-status');
    if (s) s.textContent = e.message;
  }
});

async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  STATE.user = null; STATE.products = []; STATE.cart = {};
  STATE.users = []; STATE.selectedUser = null;
  stopScanner(); // ensure camera is off
  await refreshMe();
}

document.getElementById('btn-logout')?.addEventListener('click', doLogout);
document.getElementById('btn-logout-top')?.addEventListener('click', doLogout);

// ---------- Products ----------
function renderLegacyProductsList(products) {
  // Keep legacy list compatible (hidden in UI) to avoid breaking previous flows
  const list = document.getElementById('products-list');
  if (!list) return;
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
      openProductEditor(p); // also open modal editor for convenience
    });
    list.appendChild(item);
  });
}

function renderProductsCards() {
  const wrap = document.getElementById('products-card-list');
  if (!wrap) return;
  const filterEl = document.getElementById('products-filter');
  const q = (filterEl?.value || '').trim().toLowerCase();

  const items = (STATE.products || []).filter(p =>
    !q ||
    p.name.toLowerCase().includes(q) ||
    String(p.id) === q ||
    (p.barcode && p.barcode.toLowerCase().includes(q))
  );

  wrap.innerHTML = '';
  if (items.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'text-muted';
    empty.textContent = q ? 'No products match your filter.' : 'No products yet.';
    wrap.appendChild(empty);
    return;
  }

  items.forEach(p => {
    const card = document.createElement('div');
    card.className = 'product-thin-card';

    const main = document.createElement('div');
    main.className = 'product-thin-main';
    const title = document.createElement('div');
    title.className = 'product-title'; title.textContent = p.name;
    const sub = document.createElement('div');
    sub.className = 'product-sub';
    sub.textContent = `#${p.id} • ${fmt(p.price)} • Stock ${p.stock_qty} • BAR ${p.barcode}`;

    main.appendChild(title); main.appendChild(sub);

    const actions = document.createElement('div');
    actions.className = 'product-actions d-flex gap-2';
    const btnEdit = document.createElement('button');
    btnEdit.className = 'btn btn-outline-primary'; btnEdit.textContent = 'Edit';
    btnEdit.onclick = () => openProductEditor(p);

    actions.appendChild(btnEdit);
    card.appendChild(main);
    card.appendChild(actions);
    wrap.appendChild(card);
  });
}

async function loadProducts() {
  if (!STATE.user) return;
  try {
    const products = await api('/api/products');
    STATE.products = products;

    // Legacy list (hidden) and new thin cards
    renderLegacyProductsList(products);
    renderProductsCards();

    // Product dropdown (Teller)
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



// ---------- New helpers (place under "Helpers") ----------
function nextLocalProductId() {
  // purely for UI display when creating a product (NOT for server)
  const maxId = Math.max(0, ...STATE.products.map(p => Number(p.id) || 0));
  return maxId + 1;
}

// Generate a numeric EAN-13 barcode using a prefix + id + random + checksum.
// Returns a 13-digit string.
function genBarcodeFromId(id) {
  // Prefix '200' marks "internal" codes in many stores (not a formal rule, but common practice).
  // Build 12 digits, then compute checksum as 13th.
  const base = `200${String(id).padStart(5,'0')}${String(Math.floor(Math.random()*100000)).padStart(5,'0')}`.slice(0,12);
  return base + ean13Checksum(base);
}

function ean13Checksum(code12) {
  // EAN-13 checksum: (10 - ((3*sum(odd positions) + sum(even positions)) % 10)) % 10
  let sum = 0;
  for (let i = 0; i < code12.length; i++) {
    const n = Number(code12[i]);
    sum += (i % 2 === 0) ? n : 3*n; // positions are 0-indexed here
  }
  const check = (10 - (sum % 10)) % 10;
  return String(check);
}
``


function openProductEditor(p) {
  // Prefill fields
  document.getElementById('p-id').value = p?.id ?? '';
  document.getElementById('p-name').value = p?.name ?? '';
  document.getElementById('p-price').value = p?.price ?? '';
  document.getElementById('p-barcode').value = p?.barcode ?? '';
  document.getElementById('p-stock').value = p?.stock_qty ?? '';
  const pid = document.getElementById('pur-product-id');
  if (pid) pid.value = p?.id ?? '';

  document.getElementById('productEditorTitle').textContent = p ? 'Edit Product' : 'New Product';
  // Show modal
  const modalEl = document.getElementById('productEditorModal');
  const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
  modal.show();
}

// New Product button: clear and open modal

document.getElementById('btn-new-product')?.addEventListener('click', () => {
  ['p-id','p-name','p-price','p-barcode','p-stock','pur-product-id','pur-qty','pur-price']
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });

  // show a client-side suggested ID for UX (do NOT rely on it server-side)
  const pidEl = document.getElementById('p-id');
  if (pidEl) pidEl.value = String(nextLocalProductId());

  // prefill a barcode if empty so tellers can print/scan immediately
  const bcEl = document.getElementById('p-barcode');
  if (bcEl) bcEl.value = genBarcodeFromId(pidEl?.value || nextLocalProductId());

  document.getElementById('productEditorTitle').textContent = 'New Product';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).show();
});


// Products filter
document.getElementById('products-filter')?.addEventListener('input', renderProductsCards);

// Product CRUD

document.getElementById('btn-add-product')?.addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  const price = parseFloat(document.getElementById('p-price').value);
  let barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = parseInt(document.getElementById('p-stock').value || '0', 10);

  // If user left barcode empty, generate one now
  if (!barcode) {
    // Try to build from the UI hint id; if not present, still safe
    const hinted = document.getElementById('p-id')?.value || nextLocalProductId();
    barcode = genBarcodeFromId(hinted);
    const bcEl = document.getElementById('p-barcode'); if (bcEl) bcEl.value = barcode;
  }

  try {
    // Do NOT include id; let the backend/database assign it
    await api('/api/products', {
      method: 'POST',
      body: JSON.stringify({ name, price, barcode, stock_qty })
    });
    await loadProducts();
    alert('Product added');
  } catch (e) { alert(e.message); }
});
``



document.getElementById('btn-update-product')?.addEventListener('click', async () => {
  const id = parseInt(document.getElementById('p-id').value || '0', 10);
  const name = document.getElementById('p-name').value.trim();
  const price = document.getElementById('p-price').value;
  let barcode = document.getElementById('p-barcode').value.trim();
  const stock_qty = document.getElementById('p-stock').value;

  if (!barcode && id) {
    barcode = genBarcodeFromId(id);
    const bcEl = document.getElementById('p-barcode'); if (bcEl) bcEl.value = barcode;
  }

  try {
    await api('/api/products/update', {
      method: 'POST',
      body: JSON.stringify({ id, name, price, barcode, stock_qty })
    });
    await loadProducts();
    alert('Product updated');
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-delete-product')?.addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  if (!name) return alert('Specify name to delete');
  try {
    await api(`/api/products/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await loadProducts();
    alert('Product deleted');
  } catch (e) { alert(e.message); }
});

// Purchases & Suggested price
document.getElementById('btn-add-purchase')?.addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('pur-product-id').value || '0', 10);
  const qty = parseInt(document.getElementById('pur-qty').value || '0', 10);
  const price = parseFloat(document.getElementById('pur-price').value || '0');
  try {
    await api('/api/purchases', { method: 'POST', body: JSON.stringify({ product_id: pid, qty_added: qty, purchase_price: price }) });
    await loadProducts();
    alert('Purchase recorded and stock updated');
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

// Settings
async function loadSettings() {
  try {
    const j = await api('/api/settings');
    const el = document.getElementById('markup-percent');
    if (el) el.value = j.markup_percent;
  } catch (e) {}
}

document.getElementById('btn-save-settings')?.addEventListener('click', async () => {
  const mp = parseFloat(document.getElementById('markup-percent').value || '0');
  try { await api('/api/settings', { method: 'POST', body: JSON.stringify({ markup_percent: mp }) }); alert('Settings saved'); }
  catch (e) { alert(e.message); }
});

// ---------- Users (admin) ----------
function renderUsersList() {
  const wrap = document.getElementById('users-list');
  if (!wrap) return;

  const q = (document.getElementById('users-filter')?.value || '').trim().toLowerCase();
  const items = (STATE.users || []).filter(u =>
    !q || u.username.toLowerCase().includes(q) || u.role.toLowerCase().includes(q)
  );

  wrap.innerHTML = '';
  if (items.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'list-group-item text-muted';
    empty.textContent = q ? 'No users match your filter.' : 'No users yet.';
    wrap.appendChild(empty);
    return;
  }

  items.forEach(u => {
    const item = document.createElement('div');
    item.className = 'list-group-item user-list-item';
    const left = document.createElement('div');
    left.innerHTML = `<strong>${u.username}</strong> <span class="user-meta">• ${u.role} • ${u.active ? 'active' : 'disabled'}</span>`;
    const right = document.createElement('div');
    const btnEdit = document.createElement('button');
    btnEdit.className = 'btn btn-outline-primary btn-sm';
    btnEdit.textContent = 'Edit';
    btnEdit.onclick = () => fillUserEditor(u);
    right.appendChild(btnEdit);
    item.appendChild(left); item.appendChild(right);
    wrap.appendChild(item);
  });
}

function fillUserEditor(u) {
  STATE.selectedUser = u;
  document.getElementById('u-username').value = u.username;
  document.getElementById('u-password').value = ''; // leave blank to keep
  document.getElementById('u-role').value = u.role;
  const act = document.getElementById('u-active'); if (act) act.checked = !!u.active;
}

async function loadUsers() {
  if (!STATE.user || STATE.user.role !== 'admin') return;
  try {
    const data = await api('/api/users');
    STATE.users = data || [];
    renderUsersList();
  } catch (e) {
    console.error('loadUsers', e);
  }
}

document.getElementById('users-filter')?.addEventListener('input', renderUsersList);
document.getElementById('btn-refresh-users')?.addEventListener('click', loadUsers);

document.getElementById('btn-add-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  const password = document.getElementById('u-password').value;
  const role = document.getElementById('u-role').value;
  const active = document.getElementById('u-active').checked;
  if (!username || !password) return alert('Username and password are required');
  try {
    await api('/api/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
    if (active === false) {
      // Immediately set inactive if requested
      await api('/api/users/update', { method: 'POST', body: JSON.stringify({ username, active }) });
    }
    await loadUsers();
    alert('User added');
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-update-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  if (!username) return alert('Select a user first');
  const password = document.getElementById('u-password').value;
  const role = document.getElementById('u-role').value;
  const active = document.getElementById('u-active').checked;
  const payload = { username, role, active };
  if (password) payload.password = password;
  try {
    await api('/api/users/update', { method: 'POST', body: JSON.stringify(payload) });
    await loadUsers();
    alert('User updated');
  } catch (e) { alert(e.message); }
});

document.getElementById('btn-delete-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  if (!username) return alert('Select a user first');
  if (!confirm(`Delete user "${username}"?`)) return;
  try {
    await api(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
    ['u-username','u-password'].forEach(id => { const el = document.getElementById(id); if (el) el.value=''; });
    document.getElementById('u-role').value = 'teller';
    document.getElementById('u-active').checked = true;
    STATE.selectedUser = null;
    await loadUsers();
    alert('User deleted');
  } catch (e) { alert(e.message); }
});

// ---------- Transactions ----------
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

// ---------- Teller: Cart ----------
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

// Teller search
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

// ---------- On-demand Scanner (ZXing) ----------
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

// ---------- Stats ----------
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

// ---------- Tab show events (render on demand) ----------
document.addEventListener('shown.bs.tab', async (evt) => {
  const target = evt.target?.getAttribute('data-bs-target'); // "#products", "#users", etc.
  if (!target || !STATE.user) return;

  if (target === '#products') {
    if (STATE.products.length === 0) await loadProducts();
    else renderProductsCards();
  } else if (target === '#users') {
    if (STATE.user.role !== 'admin') return;
    await loadUsers();
  } else if (target === '#transactions') {
    await loadTransactions();
  } else if (target === '#stats') {
    await loadStats();
  }
});

// ---------- App init ----------
let _didAutoLogout = false;
(async function init(){
  // Kiosk requirement from earlier: start with clean session once per page load
  if (!_didAutoLogout) {
    try { await api('/api/logout', { method: 'POST' }); } catch {}
    _didAutoLogout = true;
  }

  updateVisibility();
  await refreshMe();

  if (STATE.user) {
    await loadProducts();      // prepare Products right away
    await loadTransactions();
    if (STATE.user.role === 'admin') {
      await loadSettings();
      await loadStats();
      await loadUsers();       // prepare Users right away
    }
    // Do NOT auto-start the scanner
  }
})();
