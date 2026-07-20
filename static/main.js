// Farm Stall POS main.js - v1.5.0
//
// Structure: 15 sections separated by ═══ dividers.
// Shared globals exported at module level for future ES module split:
//   STATE, api(), toast(), show(), hide(), displayQty(), displayCost(),
//   _globalMarkupPct, loadProducts(), loadStats(), openProductEditor()
//
// To split into ES modules: each section becomes static/modules/<name>.js
// with: import { STATE, api, toast, show, hide, displayQty, displayCost } from '../main.js'
// Requires a build step (Vite/esbuild) or native <script type="module"> loading.

// ═══════════════════════════════════════════════════════
// STATE
// ═══════════════════════════════════════════════════════
let STATE = {
  user:             null,
  products:         [],     // all products (full detail)
  cart:             {},     // key -> { product_id, name, unit_price, qty, is_weight, display_label }
  scanHistory:      [],     // product_ids in order added (for undo)
  users:            [],
  currentTx:        null,
  receiveProductId: null,   // stock item being received
  productsSubTab:   'active',  // 'active' | 'ingredients' | 'recipes' | 'specials' | 'archived' | 'categories'
  categories:       [],     // [{id, name, product_count}]
  selectedCategoryIds: new Set(),  // active category filters on the products tab
  customers:        [],
  activeCustomer:   null,   // customer detected at till
  customerPollInterval: null,  // interval ID for till customer polling
  _cartDiscount:    null,   // {type:'pct'|'amt', value:number} - admin cart-wide discount
  _receiptPrinterId: null,  // LabelPrinter.id to use for receipts (null = auto-detect USB)
  _selectedProductIds: new Set(),  // product IDs checked in the products tab
  transactions:       [],           // cached transaction list for client-side filter
  _cartCount:         {},   // productId -> times added to cart (persisted in localStorage)
  _productSupplierMap: {},  // productId -> lowercased supplier names string (built from stock batches)
  _selectedBatchData:  [],  // [{id, product_name, supplier_id, supplier_name, base_cost_total, consumed_pct, updated_at}]
};

// Load persisted cart counts from localStorage
try {
  const saved = localStorage.getItem('farmpos_cart_counts');
  if (saved) STATE._cartCount = JSON.parse(saved);
} catch {}

function _bumpCartCount(productId) {
  STATE._cartCount[productId] = (STATE._cartCount[productId] || 0) + 1;
  try { localStorage.setItem('farmpos_cart_counts', JSON.stringify(STATE._cartCount)); } catch {}
}

// ── Additional Costs Helpers ─────────────────────────────────────────────────

const _COST_TYPE_LABELS = {
  shipping: 'Shipping', labour: 'Labour', utilities: 'Utilities',
  packaging: 'Packaging', other: 'Other'
};

function _readAdditionalCosts(wrapEl) {
  const rows = wrapEl ? wrapEl.querySelectorAll('[data-addl-cost-row]') : [];
  const result = [];
  for (const row of rows) {
    const label  = row.querySelector('[data-addl-label]')?.value?.trim() || '';
    const type   = row.querySelector('[data-addl-type]')?.value || 'other';
    const amount = parseFloat(row.querySelector('[data-addl-amount]')?.value || '0');
    const ref    = row.querySelector('[data-addl-ref]')?.value?.trim() || null;
    if (!label || !amount) continue;
    result.push({ label, type, amount, ...(ref ? { invoice_ref: ref } : {}) });
  }
  return result;
}

function _addlCostsTotal(costs) {
  return costs.reduce((s, c) => s + (parseFloat(c.amount) || 0), 0);
}

function _renderAdditionalCostsBlock(wrapEl, existing) {
  if (!wrapEl) return;
  const typeOpts = Object.entries(_COST_TYPE_LABELS)
    .map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  wrapEl.innerHTML = `
    <div class="border rounded p-2">
      <div class="d-flex align-items-center mb-2">
        <span class="small fw-semibold">Additional Costs</span>
        <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" style="font-size:11px" data-addl-add-row>
          <i class="bi bi-plus-lg"></i> Add Cost
        </button>
      </div>
      <div data-addl-rows></div>
    </div>
  `;
  const rowsEl = wrapEl.querySelector('[data-addl-rows]');

  const addRow = (label = '', type = 'shipping', amount = '', ref = '') => {
    const row = document.createElement('div');
    row.className = 'd-flex gap-1 mb-1 align-items-center';
    row.dataset.addlCostRow = '1';
    row.innerHTML = `
      <input type="text" class="form-control form-control-sm" style="flex:2" placeholder="Label e.g. Courier" data-addl-label value="${escapeHtml(String(label))}">
      <select class="form-select form-select-sm" style="flex:1;min-width:90px" data-addl-type>
        ${typeOpts}
      </select>
      <div class="input-group input-group-sm" style="flex:1;min-width:80px">
        <span class="input-group-text">R</span>
        <input type="number" step="0.01" class="form-control" placeholder="0.00" data-addl-amount value="${amount}">
      </div>
      <input type="text" class="form-control form-control-sm" style="flex:1;min-width:70px" placeholder="Ref (opt)" data-addl-ref value="${escapeHtml(String(ref))}">
      <button type="button" class="btn btn-outline-danger btn-sm" data-addl-remove><i class="bi bi-trash3"></i></button>
    `;
    row.querySelector('[data-addl-type]').value = type;
    row.querySelector('[data-addl-remove]').onclick = () => row.remove();
    rowsEl.appendChild(row);
  };

  (existing || []).forEach(c => addRow(c.label, c.type, c.amount, c.invoice_ref || ''));
  wrapEl.querySelector('[data-addl-add-row]').onclick = () => addRow();
}

function _checkOverheadWarning(addlTotal, baseTotal) {
  if (baseTotal > 0 && addlTotal > 0 && (addlTotal / baseTotal) > 0.5) {
    const pct = Math.round((addlTotal / baseTotal) * 100);
    toast(`Additional costs (R${addlTotal.toFixed(2)}) are ${pct}% of inventory value. Double-check before saving.`, 'warning', 6000);
  }
}

// ═══════════════════════════════════════════════════════
// UNIT SYSTEM
// ═══════════════════════════════════════════════════════
const UNITS = {
  weight: { base: 'g',    display: ['g', 'kg'],         toBase: { g: 1,    kg: 1000 } },
  volume: { base: 'ml',   display: ['ml', 'L'],          toBase: { ml: 1,   L:  1000 } },
  count:  { base: 'unit', display: ['unit'],              toBase: { unit: 1 } },
};

function toBase(qty, unit, unitType) {
  const conv = UNITS[unitType]?.toBase[unit];
  return conv ? qty * conv : qty;
}

function displayQty(qty_base, unitType) {
  if (!unitType || !UNITS[unitType]) return `${+qty_base.toFixed(4)}`;
  const thresholds = { weight: [1000, 'kg'], volume: [1000, 'L'], count: [Infinity, ''] };
  const [threshold, bigUnit] = thresholds[unitType] || [Infinity, ''];
  if (qty_base >= threshold) return `${+(qty_base / threshold).toFixed(3)}${bigUnit}`;
  const base = UNITS[unitType].base;
  return `${+qty_base.toFixed(qty_base < 1 ? 3 : 2)}${base}`;
}

// Returns cost per the same display unit that displayQty would use for qty_base
function displayCost(cost_per_base, qty_base, unitType) {
  if (!unitType || !UNITS[unitType]) return { cost: cost_per_base, unit: '' };
  const thresholds = { weight: [1000, 'kg'], volume: [1000, 'L'], count: [Infinity, ''] };
  const [threshold, bigUnit] = thresholds[unitType] || [Infinity, ''];
  if (qty_base >= threshold) return { cost: cost_per_base * threshold, unit: bigUnit };
  return { cost: cost_per_base, unit: UNITS[unitType].base };
}

// Build unit dropdown options including package unit
function buildUnitOptions(unitType, packageSize, packageUnit) {
  const opts = [];
  if (packageSize && packageUnit) {
    opts.push({ value: packageUnit, label: `${packageUnit} (${displayQty(packageSize, unitType)} each)`, conv: packageSize });
  }
  if (UNITS[unitType]) {
    UNITS[unitType].display.forEach(u => {
      if (u !== packageUnit) opts.push({ value: u, label: u, conv: UNITS[unitType].toBase[u] });
    });
  }
  return opts;
}

// ═══════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════
function show(el) { el && el.classList.remove('hidden'); }
function hide(el) { el && el.classList.add('hidden'); }
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmt(n)   { return (Math.round(n * 100) / 100).toFixed(2); }
function fmtQty(n) { return n % 1 === 0 ? String(n) : n.toFixed(3); }
function isAdmin() { const r = STATE.user?.roles || [STATE.user?.role]; return r.includes('admin'); }

// Returns the URL for a product image variant ('thumb'|'small'|'') or null if no image.
function imgVariant(image_url, variant) {
  if (!image_url) return null;
  const base   = image_url.replace(/\.jpg$/i, '');
  const suffix = variant ? '_' + variant : '';
  return `/static/product_images/${base}${suffix}.jpg`;
}

// ── Multi-image editor state ──────────────────────────────────────────────────
let _editingImages = [];   // [{id, filename, is_primary, display_order}]  - already saved
let _pendingFiles  = [];   // File[] - selected but not yet uploaded

function renderImageList() {
  const host = document.getElementById('product-images-list');
  if (!host) return;
  if (!_editingImages.length && !_pendingFiles.length) {
    host.innerHTML = '<div class="text-muted small">No photos yet.</div>';
    return;
  }

  const pid = parseInt(document.getElementById('p-id')?.value || '0', 10);
  host.innerHTML = '';

  _editingImages.forEach((img, idx) => {
    const row = document.createElement('div');
    row.className = 'd-flex align-items-center gap-2 mb-1 p-2 rounded border' + (img.is_primary ? ' border-warning bg-light' : '');
    row.dataset.imgId = img.id;

    const thumb = document.createElement('img');
    thumb.src = imgVariant(img.filename, 'thumb');
    thumb.style.cssText = 'width:48px;height:48px;object-fit:cover;border-radius:4px;flex-shrink:0';
    row.appendChild(thumb);

    const label = document.createElement('span');
    label.className = 'flex-grow-1 small' + (img.is_primary ? ' fw-bold' : '');
    label.innerHTML = img.is_primary ? '<i class="bi bi-star-fill me-1"></i>Primary' : img.filename.split('_').slice(0,2).join('_');
    row.appendChild(label);

    // ↑ button
    if (idx > 0) {
      const btnUp = document.createElement('button');
      btnUp.type = 'button'; btnUp.className = 'btn btn-sm btn-outline-secondary'; btnUp.innerHTML = '<i class="bi bi-arrow-up"></i>';
      btnUp.onclick = () => _moveImage(idx, -1, pid);
      row.appendChild(btnUp);
    }
    // ↓ button
    if (idx < _editingImages.length - 1) {
      const btnDown = document.createElement('button');
      btnDown.type = 'button'; btnDown.className = 'btn btn-sm btn-outline-secondary'; btnDown.innerHTML = '<i class="bi bi-arrow-down"></i>';
      btnDown.onclick = () => _moveImage(idx, 1, pid);
      row.appendChild(btnDown);
    }
    // ⭐ set primary
    if (!img.is_primary) {
      const btnPri = document.createElement('button');
      btnPri.type = 'button'; btnPri.className = 'btn btn-sm btn-outline-warning'; btnPri.title = 'Set as primary';
      btnPri.innerHTML = '<i class="bi bi-star-fill"></i>';
      btnPri.onclick = () => _setPrimary(img.id, pid);
      row.appendChild(btnPri);
    }
    // ✕ delete
    const btnDel = document.createElement('button');
    btnDel.type = 'button'; btnDel.className = 'btn btn-sm btn-outline-danger'; btnDel.innerHTML = '<i class="bi bi-x-lg"></i>';
    btnDel.onclick = () => _deleteImage(img.id, pid);
    row.appendChild(btnDel);

    host.appendChild(row);
  });

  // Pending files - queued for upload on save
  if (_pendingFiles.length) {
    const divider = document.createElement('div');
    divider.className = 'text-muted small mt-2 mb-1';
    divider.textContent = `${_pendingFiles.length} photo${_pendingFiles.length > 1 ? 's' : ''} queued - will upload on save`;
    host.appendChild(divider);

    _pendingFiles.forEach((file, idx) => {
      const row = document.createElement('div');
      row.className = 'd-flex align-items-center gap-2 mb-1 p-2 rounded border border-dashed';
      row.style.borderStyle = 'dashed';

      const thumb = document.createElement('img');
      thumb.style.cssText = 'width:48px;height:48px;object-fit:cover;border-radius:4px;flex-shrink:0;opacity:.7';
      const reader = new FileReader();
      reader.onload = e => { thumb.src = e.target.result; };
      reader.readAsDataURL(file);
      row.appendChild(thumb);

      const label = document.createElement('span');
      label.className = 'flex-grow-1 small text-muted';
      label.textContent = file._name || file.name || 'photo';
      row.appendChild(label);

      const btnDel = document.createElement('button');
      btnDel.type = 'button'; btnDel.className = 'btn btn-sm btn-outline-danger'; btnDel.innerHTML = '<i class="bi bi-x-lg"></i>';
      btnDel.onclick = () => { _pendingFiles.splice(idx, 1); renderImageList(); };
      row.appendChild(btnDel);

      host.appendChild(row);
    });
  }
}

async function _moveImage(idx, dir, pid) {
  const other = idx + dir;
  if (other < 0 || other >= _editingImages.length) return;
  [_editingImages[idx], _editingImages[other]] = [_editingImages[other], _editingImages[idx]];
  _editingImages.forEach((img, i) => img.display_order = i);
  renderImageList();
  if (pid) {
    try {
      await api(`/api/products/${pid}/images/reorder`, {
        method: 'POST',
        body: JSON.stringify(_editingImages.map(img => ({id: img.id, display_order: img.display_order})))
      });
    } catch (e) { toast('Reorder failed: ' + e.message, 'warning'); }
  }
}

async function _setPrimary(imgId, pid) {
  _editingImages.forEach(img => img.is_primary = (img.id === imgId));
  renderImageList();
  if (pid) {
    try {
      await api(`/api/products/${pid}/images/${imgId}/primary`, { method: 'POST' });
    } catch (e) { toast('Set primary failed: ' + e.message, 'warning'); }
  }
}

async function _deleteImage(imgId, pid) {
  if (!confirm('Remove this photo?')) return;
  _editingImages = _editingImages.filter(img => img.id !== imgId);
  _editingImages.forEach((img, i) => img.display_order = i);
  if (_editingImages.length && !_editingImages.some(img => img.is_primary)) {
    _editingImages[0].is_primary = true;
  }
  renderImageList();
  if (pid) {
    try {
      await api(`/api/products/${pid}/images/${imgId}`, { method: 'DELETE' });
      await loadProducts();
    } catch (e) { toast('Delete failed: ' + e.message, 'warning'); }
  }
}

async function api(path, opts = {}, timeoutMs = 10000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
    }, opts));
    if (res.status === 401) {
      // Session expired — persist cart so it survives the re-login page load
      _saveCartToSession();
      toast('Session expired — please log in again', 'warning', 5000);
      setTimeout(() => location.reload(), 1500);
      throw new Error('Session expired');
    }
    if (!res.ok) {
      let err = 'Request failed';
      try { const j = await res.json(); err = j.error || JSON.stringify(j); } catch {}
      throw new Error(err);
    }
    try { return await res.json(); } catch { return {}; }
  } catch(e) {
    if (e.name === 'AbortError') throw new Error('Request timed out');
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function _saveCartToSession() {
  try {
    if (Object.keys(STATE.cart || {}).length > 0)
      sessionStorage.setItem('farmpos_cart_backup', JSON.stringify(STATE.cart));
  } catch {}
}

function _restoreCartFromSession() {
  try {
    const saved = sessionStorage.getItem('farmpos_cart_backup');
    if (saved) {
      STATE.cart = JSON.parse(saved);
      sessionStorage.removeItem('farmpos_cart_backup');
      renderCart();
      toast('Cart restored from previous session', 'info', 4000);
    }
  } catch {}
}

function toast(msg, type = 'success', durationMs = 3000) {
  const c = document.getElementById('toast-container');
  if (!c) return;
  const el = document.createElement('div');
  el.className = `pos-toast ${type}`;
  el.textContent = msg;
  c.appendChild(el);
  setTimeout(() => el.remove(), durationMs);
}

function beep(durationMs = 120, frequency = 880) {
  try {
    const ctx  = new (window.AudioContext || window.webkitAudioContext)();
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine'; osc.frequency.value = frequency;
    osc.connect(gain); gain.connect(ctx.destination);
    gain.gain.setValueAtTime(0.1, ctx.currentTime);
    osc.start();
    setTimeout(() => { osc.stop(); ctx.close(); }, durationMs);
  } catch {}
}

function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

// EAN-13 barcode generation
function ean13Check(code12) {
  let s = 0;
  for (let i = 0; i < 12; i++) s += Number(code12[i]) * (i % 2 === 0 ? 1 : 3);
  return String((10 - s % 10) % 10);
}
function genBarcode(id) {
  // Deterministic EAN-13 from product_code: 1 + PPPPP + 000000 + check
  // product_code is server-assigned; use id as fallback for preview before save
  const p = STATE.products.find(x => x.id === id);
  const code = p?.product_code || id;
  const core = `1${String(code).padStart(5,'0')}000000`;
  return core + ean13Check(core);
}
function nextLocalId() {
  return Math.max(0, ...STATE.products.map(p => Number(p.id) || 0)) + 1;
}

// ═══════════════════════════════════════════════════════
// VISIBILITY & AUTH
// ═══════════════════════════════════════════════════════
function updateVisibility() {
  const loginCard = document.getElementById('login-card');
  const authBar   = document.getElementById('auth-bar');
  const tabs      = document.getElementById('main-tabs');
  const contents  = document.getElementById('tab-contents');
  if (!STATE.user) {
    show(loginCard); hide(authBar); hide(tabs); hide(contents);
    return;
  }
  hide(loginCard); show(authBar);
  const au = document.getElementById('auth-user');
  const roles = STATE.user.roles || [STATE.user.role];
  const isAdmin = roles.includes('admin');
  const isDev   = roles.includes('developer');
  const isTeller = roles.includes('teller');
  const roleLabels = roles.map(r => `<span class="badge ${r==='admin'?'bg-danger':r==='developer'?'bg-info text-dark':'bg-secondary'} ms-1">${r}</span>`).join('');
  if (au) au.innerHTML = `${STATE.user.username} ${roleLabels}`;
  show(tabs); show(contents);
  _initTellerSize(); // tabs now visible — retry sequence handles font/layout settling
  // pos-only: Teller/Transactions/Kitchen - hidden for pure developer (no admin/teller)
  const showPos = !isDev || isAdmin || isTeller;
  document.querySelectorAll('.pos-only').forEach(el =>
    showPos ? show(el) : hide(el));
  document.querySelectorAll('.admin-only').forEach(el =>
    isAdmin ? show(el) : hide(el));
  document.querySelectorAll('.teller-only').forEach(el =>
    (isTeller || (!isAdmin && !isDev)) ? show(el) : hide(el));
  document.querySelectorAll('.dev-only').forEach(el =>
    isDev ? show(el) : hide(el));
  // Developer-only users land on Recognition tab, not Teller
  if (isDev && !isAdmin && !isTeller) {
    const recTab = document.querySelector('[data-bs-target="#recognition-settings"]');
    if (recTab) recTab.click();
  }
}

async function refreshMe() {
  const me = await api('/api/me');
  if (me.logged_in) {
    STATE.user = { username: me.username, role: me.role, roles: me.roles || [me.role] };
    hide(document.getElementById('btn-login'));
    const s = document.getElementById('login-status'); if (s) s.textContent = '';
  } else {
    STATE.user = null;
    show(document.getElementById('btn-login'));
  }
  updateVisibility();
  _checkBackupHealth();
}

// Backup-health banner (ISSUE gap): /api/health surfaces a backup_warning string when
// off-box backups are stale/failing or disk is filling. Show it to admins only; the
// owner reporting "there's a yellow warning" is a zero-infra alert channel.
async function _checkBackupHealth() {
  try {
    const isAdmin = STATE.user && (STATE.user.roles || []).includes('admin');
    const b = document.getElementById('backup-warning-banner');
    if (!b) return;
    if (!isAdmin) { hide(b); return; }
    const h = await api('/api/health');
    if (h && h.backup_warning) {
      b.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>Backup warning: ${h.backup_warning} - tell your support contact.`;
      show(b);
    } else {
      hide(b);
    }
  } catch (e) { /* health check is best-effort; never block the UI */ }
}

// ═══════════════════════════════════════════════════════
// LOGIN / LOGOUT
// ═══════════════════════════════════════════════════════
document.getElementById('btn-login')?.addEventListener('click', async () => {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ username, password }) });
    await refreshMe();
    // Always land on the Teller tab after login
    const tellerTab = document.querySelector('[data-bs-target="#teller"]');
    if (tellerTab) bootstrap.Tab.getOrCreateInstance(tellerTab).show();
    setTimeout(_focusTrap, 400);
    initSerialSupport();
    await loadProducts();
    _restoreCartFromSession();  // restore cart if session expired mid-sale
    await loadTransactions();
    await loadSpecials();
    startKitchenBadgePoll();  // badge visible to all users
    startCustomerVisitPoll(); // greet returning customers on teller screen
    const _loginRoles = STATE.user?.roles || [STATE.user?.role];
    if (_loginRoles.includes('admin')) {
      await loadSettings();
      _populateStatsProductFilter();
      await loadStats();
      await loadUsers();
      await loadIngredients();  // pre-load cost map for recipe editor
      await loadSuppliers();    // pre-load for receive stock dropdown
      await loadSpecials();
      startKitchenBadgePoll();  // keep badge count live across all tabs
    }
  } catch (e) {
    const s = document.getElementById('login-status');
    if (s) s.textContent = e.message;
  }
});

async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  STATE.user = null; STATE.products = []; STATE.cart = {}; STATE.scanHistory = [];
  STATE.users = [];
  _statsData = null;
  stopScanner();
  // Deactivate all tab panes so no stale content shows after re-login
  document.querySelectorAll('#tab-contents .tab-pane').forEach(p => {
    p.classList.remove('show', 'active');
  });
  document.querySelectorAll('#main-tabs .nav-link').forEach(b => {
    b.classList.remove('active');
  });
  await refreshMe();
}
document.getElementById('btn-logout-top')?.addEventListener('click', doLogout);

// ═══════════════════════════════════════════════════════
// PRODUCTS
// ═══════════════════════════════════════════════════════
async function loadProducts() {
  if (!STATE.user) return;
  try {
    STATE.products = await api('/api/products?full=1');
    await loadCategories();
    renderProductsCards();
    renderTellerGrid();
  } catch (e) { console.error('loadProducts', e); }
}

async function loadCategories() {
  if (!STATE.user) return;
  try {
    STATE.categories = await api('/api/categories');
    // Drop any selected filters whose category no longer exists
    const live = new Set(STATE.categories.map(c => c.id));
    STATE.selectedCategoryIds.forEach(id => { if (!live.has(id)) STATE.selectedCategoryIds.delete(id); });
  } catch (e) { console.error('loadCategories', e); STATE.categories = []; }
}

function renderTellerGrid(q = '') {
  const host = document.getElementById('teller-product-grid');
  if (!host) return;
  const lq = q.trim().toLowerCase();
  let items = STATE.products.filter(p =>
    p.is_for_sale !== false && !p.is_archived && (
      !lq ||
      p.name.toLowerCase().includes(lq) ||
      String(p.id) === lq ||
      (p.barcode && p.barcode.toLowerCase().includes(lq)) ||
      (STATE._productSupplierMap[p.id] || '').includes(lq)
    )
  );
  // Sort by cart frequency descending (most-ordered first); ties fall back to name
  items = items.slice().sort((a, b) => {
    const ca = STATE._cartCount[a.id] || 0;
    const cb = STATE._cartCount[b.id] || 0;
    return cb - ca || a.name.localeCompare(b.name);
  });
  host.innerHTML = '';
  if (!items.length) {
    host.innerHTML = '<div class="text-muted small py-1">No products match.</div>';
    return;
  }
  items.forEach(p => {
    const tile = document.createElement('div');
    tile.className = 'tpt';
    const priceStr = p.sold_by_weight
      ? (p.price_per_unit != null ? `R${fmt(parseFloat(p.price_per_unit) * 1000)}/kg` : 'by weight')
      : (p.price != null ? `R${fmt(p.price)}` : '');
    const isProducedRecipe = p.product_type === 'recipe' && p.is_produced;
    const stockLeft = isProducedRecipe ? (p.stock_level ?? p.stock_qty ?? 0) : 0;
    tile.innerHTML = `
      ${p.image_url
        ? `<img class="tpt-img" src="${imgVariant(p.image_url, 'small')}" loading="lazy" decoding="async" alt="">`
        : `<div class="tpt-ph"><i class="bi bi-box-seam"></i></div>`}
      <div class="tpt-info">
        <span class="tpt-name" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
        ${priceStr ? `<div class="tpt-price">${priceStr}</div>` : ''}
        ${isProducedRecipe ? `<div class="tpt-stock">${stockLeft} left</div>` : ''}
        ${p.sync_to_scale && p.product_code ? `<div class="tpt-plu">PLU ${p.product_code}</div>` : ''}
      </div>
      ${isProducedRecipe && (p.batch_size || 1) > 1 ? `<button class="tpt-sell-all">Full (${p.batch_size}${p.stock_unit ? ' ' + p.stock_unit + 's' : ''})</button>` : ''}`;
    tile.querySelector('.tpt-sell-all')?.addEventListener('click', e => {
      e.stopPropagation();
      addToCartQty(p, p.batch_size || 1);
    });
    tile.addEventListener('click', () => addToCart(p));
    host.appendChild(tile);
  });
}

let _productSort = { col: null, dir: 1 };

function _productSortKey(p, col) {
  switch (col) {
    case 'name':    return p.name.toLowerCase();
    case 'stock':   return typeof p.stock_level === 'number' ? p.stock_level : (p.stock_qty || 0);
    case 'price':   return parseFloat(p.price || p.price_per_unit || 0);
    case 'barcode': return (p.barcode || '').toLowerCase();
    case 'flags': {
      const fl = [p.is_produced, p.is_prepared, p.sync_to_scale, p.is_for_sale, p.is_available_online];
      const bitmask = fl.reduce((acc, f, i) => acc + (f ? (1 << (4 - i)) : 0), 0);
      return fl.filter(Boolean).length * 32 + bitmask; // same combo → same key, count drives ordering
    }
    case 'cogs': case 'markup': case 'margin': {
      const m = calcProductMargins(p);
      if (!m) return -Infinity;
      if (col === 'cogs')   return parseFloat(m.costLabel.replace(/[^0-9.]/g, '')) || 0;
      if (col === 'markup') return parseFloat(m.markup) || 0;
      return parseFloat(m.margin) || 0;
    }
    default: return 0;
  }
}

function renderProductsCards() {
  const wrap = document.getElementById('products-card-list');
  if (!wrap) return;
  const q = (document.getElementById('products-filter')?.value || '').trim().toLowerCase();

  const tab = STATE.productsSubTab;

  const selectedCats = STATE.selectedCategoryIds;
  const catFilterActive = selectedCats.size > 0;
  const UNCATEGORISED = 0;  // sentinel for "no category" pill

  let items = STATE.products.filter(p => {
    const matchesSearch = !q ||
      p.name.toLowerCase().includes(q) ||
      String(p.id) === q ||
      (p.barcode?.toLowerCase().includes(q));
    if (!matchesSearch) return false;
    // Category filter (multi-select; matches any selected)
    if (catFilterActive) {
      const key = p.category_id || UNCATEGORISED;
      if (!selectedCats.has(key)) return false;
    }
    if (tab === 'archived')     return p.is_archived === true;
    if (tab === 'ingredients')  return p.is_archived !== true && p.is_for_sale === false;
    if (tab === 'recipes')      return p.is_archived !== true && p.product_type === 'recipe' && p.is_for_sale !== false;
    // 'active' (Single Items) = for sale, not archived, not a recipe
    return p.is_archived !== true && p.is_for_sale !== false && p.product_type !== 'recipe';
  });

  // Apply column sort
  if (_productSort.col) {
    items.sort((a, b) => {
      const ka = _productSortKey(a, _productSort.col);
      const kb = _productSortKey(b, _productSort.col);
      const cmp = typeof ka === 'string' ? ka.localeCompare(kb) : (ka === kb ? 0 : ka < kb ? -1 : 1);
      return cmp * _productSort.dir;
    });
  }

  renderCategoryFilterPills();

  // Update count badges
  const singleCount   = STATE.products.filter(p => !p.is_archived && p.is_for_sale !== false && p.product_type !== 'recipe').length;
  const ingCount      = STATE.products.filter(p => !p.is_archived && p.is_for_sale === false).length;
  const recipeCount   = STATE.products.filter(p => !p.is_archived && p.product_type === 'recipe').length;
  const specialsCount = (STATE.specials || []).length;
  const archivedCount = STATE.products.filter(p => p.is_archived).length;
  const setBadge = (id, n) => { const el = document.getElementById(id); if (el) { el.textContent = n; el.style.display = n > 0 ? '' : 'none'; } };
  setBadge('single-count-badge',     singleCount);
  setBadge('ingredients-count-badge', ingCount);
  setBadge('recipes-count-badge',    recipeCount);
  setBadge('specials-count-badge',   specialsCount);
  setBadge('archived-count-badge',   archivedCount);
  setBadge('categories-count-badge', (STATE.categories || []).length);

  wrap.innerHTML = '';
  if (items.length === 0) {
    const msg = q ? 'No products match.' : 'No products yet.';
    wrap.innerHTML = `<div class="text-muted py-2">${msg}</div>`;
    return;
  }

  // Select-all checkbox + sortable column headers
  const _sc = _productSort.col, _sd = _productSort.dir;
  const _sh = (col, label) => {
    const active = _sc === col;
    const icon   = active ? (_sd === 1 ? 'bi-caret-up-fill' : 'bi-caret-down-fill') : 'bi-caret-up-fill';
    return `<div class="pr-sort-hdr${active ? ' psh-active' : ''}" data-sort="${col}">${label}<i class="bi ${icon} psh-ic"></i></div>`;
  };
  const hdr = document.createElement('div');
  hdr.className = 'product-col-header';
  hdr.innerHTML = `
    <div><input type="checkbox" id="pr-select-all" title="Select all"></div>
    ${_sh('name','Product')}${_sh('stock','Stock')}${_sh('price','Price')}${_sh('barcode','Barcode')}
    ${_sh('cogs','COGS')}${_sh('markup','Markup')}${_sh('margin','Margin')}${_sh('flags','Flags')}<div></div>
  `;
  hdr.querySelector('#pr-select-all').addEventListener('change', e => {
    const checked = e.target.checked;
    wrap.querySelectorAll('.pr-row-check').forEach(cb => {
      if (cb.checked !== checked) cb.click();
    });
  });
  hdr.querySelectorAll('.pr-sort-hdr').forEach(el => {
    el.addEventListener('click', () => {
      const col = el.dataset.sort;
      if (_productSort.col === col) { _productSort.dir *= -1; }
      else { _productSort.col = col; _productSort.dir = 1; }
      renderProductsCards();
    });
  });
  wrap.appendChild(hdr);

  items.forEach(p => {
    const isStockItem = p.product_type === 'stock_item' || (p.product_type === 'recipe' && p.is_produced);
    const typeLabel   = { stock_item: '<i class="bi bi-box-seam"></i>', recipe: '<i class="bi bi-layers"></i>' }[p.product_type] || '';

    // Price
    let priceDisplay = '';
    if (p.sold_by_weight && p.price_per_unit != null) {
      const bigUnit = p.unit_type === 'volume' ? 'L' : 'kg';
      const conv    = UNITS[p.unit_type]?.toBase[bigUnit] || 1;
      priceDisplay  = `R${fmt(parseFloat(p.price_per_unit) * conv)}/${bigUnit}`;
    } else if (p.price != null) {
      priceDisplay  = `R${fmt(p.price)}`;
    }

    // Stock
    let stockHtml   = '<span class="text-muted">—</span>';
    let stockMobile = '';
    if (isStockItem) {
      let level;
      if (p.product_type === 'recipe' && p.is_produced) {
        const n = p.stock_level ?? 0;
        const lbl = p.stock_unit || 'unit';
        level = `${n} ${n === 1 ? lbl : lbl + 's'}`;
      } else {
        level = displayQty(p.stock_level || 0, p.unit_type);
      }
      const low   = p.low_stock ? ' <span class="badge bg-warning text-dark" style="font-size:10px">LOW</span>' : '';
      stockHtml   = level + low;
      stockMobile = level + (p.low_stock ? ' <i class="bi bi-exclamation-triangle"></i>' : '');
    }

    const margins = calcProductMargins(p);

    const row = document.createElement('div');
    const isSelected = STATE._selectedProductIds.has(p.id);
    row.className = `product-row${p.is_archived ? ' is-archived' : ''}${isSelected ? ' selected' : ''}`;
    row.dataset.productId = p.id;

    row.innerHTML = `
      <div class="pr-check"><input type="checkbox" class="pr-row-check" ${isSelected ? 'checked' : ''}></div>
      <div class="pr-name">
        ${p.image_url ? `<img class="pr-thumb" src="${imgVariant(p.image_url, 'thumb')}" loading="lazy" decoding="async" alt="">` : ''}
        <div class="pr-name-block">
          <span class="pr-name-text" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
          ${typeLabel ? `<span class="text-muted ms-1" style="font-size:11px">${typeLabel}</span>` : ''}
          <div class="pr-mobile-meta">
            <div class="pr-mm-top">
              <div class="pr-mm-left">
                ${stockMobile ? `<span class="pr-mm-stock">${stockMobile}</span>` : ''}
              </div>
              <div class="pr-mm-right">
                ${priceDisplay ? `<span class="pr-mm-price text-success fw-semibold">${priceDisplay}</span>` : ''}
                ${margins ? `<span class="pr-mm-cogs">${escapeHtml(margins.costLabel)}</span>` : ''}
                ${margins ? `<span class="pr-mm-margin">${margins.margin}%</span>` : ''}
              </div>
            </div>
            ${p.barcode ? `<span class="pr-mm-barcode">${escapeHtml(p.barcode)}</span>` : ''}
          </div>
        </div>
      </div>
      <div class="pr-stock">${stockHtml}</div>
      <div class="pr-price ${priceDisplay ? 'text-success' : 'text-muted'}">${priceDisplay || '—'}</div>
      <div class="pr-barcode">
        <span class="pr-barcode-num">${escapeHtml(p.barcode || '—')}</span>
        ${p.barcode ? `<svg id="bc-${p.id}" class="pr-barcode-svg"></svg>` : ''}
      </div>
      <div class="pr-cogs">${margins ? escapeHtml(margins.costLabel) : '<span class="text-muted">—</span>'}</div>
      <div class="pr-markup">${margins ? margins.markup + '%' : '<span class="text-muted">—</span>'}</div>
      <div class="pr-margin">${margins ? margins.margin + '%' : '<span class="text-muted">—</span>'}</div>
      <div class="pr-flags">
        ${p.is_produced        ? `<i class="bi bi-fire pf-icon text-warning"      title="Batch produced"></i>`          : ''}
        ${p.is_prepared        ? `<i class="bi bi-clock pf-icon text-danger"       title="Made to order (kitchen)"></i>` : ''}
        ${p.sync_to_scale      ? `<i class="bi bi-speedometer2 pf-icon text-info"  title="Synced to scale"></i>`         : ''}
        ${p.is_for_sale        ? `<i class="bi bi-shop pf-icon text-primary"       title="Available at till"></i>`       : ''}
        ${p.is_available_online ? `<i class="bi bi-globe pf-icon text-success"     title="Available online"></i>`        : ''}
      </div>
      <div class="pr-more-wrap">
        <button class="pr-more-btn" title="Actions">⋮</button>
        <div class="pr-more-menu">
          <button data-menu="edit"><i class="bi bi-pencil me-1"></i>Edit</button>
          <button data-menu="label"><i class="bi bi-tag me-1"></i>Print Label</button>
          ${isStockItem ? `
            <button data-menu="receive"><i class="bi bi-box-arrow-in-down me-1"></i>Receive</button>
            <button data-menu="stocktake"><i class="bi bi-clipboard-check me-1"></i>Stocktake</button>
            <button data-menu="writeoff"><i class="bi bi-trash3 me-1"></i>Write Off</button>
            <button data-menu="batch-history"><i class="bi bi-clock-history me-1"></i>Batch History</button>
          ` : ''}
          ${p.product_type === 'recipe' && p.is_produced ? `
            <button data-menu="produce"><i class="bi bi-fire me-1 text-warning"></i>Produce batch</button>
            <button data-menu="writeoff-produced"><i class="bi bi-trash3 me-1"></i>Write Off</button>
          ` : ''}
          ${p.is_archived
            ? `<button data-menu="restore"><i class="bi bi-arrow-clockwise me-1"></i>Restore</button>`
            : `<button data-menu="archive"><i class="bi bi-archive me-1"></i>Archive</button>`}
        </div>
      </div>
      <div class="pr-body"></div>
    `;

    // Checkbox: toggle selection
    const checkbox = row.querySelector('.pr-row-check');
    checkbox.addEventListener('click', e => {
      e.stopPropagation();
      if (checkbox.checked) {
        STATE._selectedProductIds.add(p.id);
        row.classList.add('selected');
      } else {
        STATE._selectedProductIds.delete(p.id);
        row.classList.remove('selected');
      }
      _updateSelectionBar();
    });

    // ⋮ button: open/close context menu
    const moreBtn  = row.querySelector('.pr-more-btn');
    const moreMenu = row.querySelector('.pr-more-menu');
    moreBtn.addEventListener('click', e => {
      e.stopPropagation();
      // Close any other open menus
      document.querySelectorAll('.pr-more-menu.open').forEach(m => { if (m !== moreMenu) m.classList.remove('open'); });
      moreMenu.classList.toggle('open');
    });
    document.addEventListener('click', () => moreMenu.classList.remove('open'), { capture: false });

    const stockItem = isStockItem ? (STATE._stockItems?.[p.id] || {
      id: p.id, name: p.name, unit_type: p.unit_type, base_unit: p.base_unit,
      package_size: p.package_size, package_unit: p.package_unit,
      sell_packages: [], batches: [], stock_level: p.stock_level || 0,
    }) : null;

    moreMenu.querySelector('[data-menu="edit"]')?.addEventListener('click',     e => { e.stopPropagation(); moreMenu.classList.remove('open'); openProductEditor(p); });
    moreMenu.querySelector('[data-menu="label"]')?.addEventListener('click',    e => { e.stopPropagation(); moreMenu.classList.remove('open'); openLabelPrintModal(p); });
    moreMenu.querySelector('[data-menu="archive"]')?.addEventListener('click',  e => { e.stopPropagation(); moreMenu.classList.remove('open'); openArchiveModal(p); });
    moreMenu.querySelector('[data-menu="restore"]')?.addEventListener('click',  e => { e.stopPropagation(); moreMenu.classList.remove('open'); openRestoreModal(p); });
    moreMenu.querySelector('[data-menu="produce"]')?.addEventListener('click',         e => { e.stopPropagation(); moreMenu.classList.remove('open'); openProduceModal(p); });
    moreMenu.querySelector('[data-menu="writeoff-produced"]')?.addEventListener('click', e => { e.stopPropagation(); moreMenu.classList.remove('open'); openWriteoffModal(p); });
    moreMenu.querySelector('[data-menu="batch-history"]')?.addEventListener('click',    e => { e.stopPropagation(); moreMenu.classList.remove('open'); openBatchHistoryModal(p); });
    if (isStockItem && stockItem) {
      moreMenu.querySelector('[data-menu="receive"]')?.addEventListener('click',   e => { e.stopPropagation(); moreMenu.classList.remove('open'); openReceiveStockModal(stockItem); });
      moreMenu.querySelector('[data-menu="stocktake"]')?.addEventListener('click', e => { e.stopPropagation(); moreMenu.classList.remove('open'); openStocktakeModal(stockItem); });
      moreMenu.querySelector('[data-menu="writeoff"]')?.addEventListener('click',  e => { e.stopPropagation(); moreMenu.classList.remove('open'); openWriteoffModal(stockItem); });
    }

    // Row click: expand detail (not on checkbox or ⋮)
    const body = row.querySelector('.pr-body');
    row.addEventListener('click', e => {
      if (e.target.closest('.pr-check') || e.target.closest('.pr-more-wrap')) return;
      const open = body.classList.toggle('open');
      row.classList.toggle('expanded', open && !isSelected);
      if (!open || body.children.length > 0) return;
      if (isStockItem) {
        const sd = STATE._stockItems?.[p.id];
        if (sd) body.appendChild(_buildStockBody(sd, p));
        else body.insertAdjacentHTML('beforeend', '<div class="text-muted small py-2">No batch data loaded.</div>');
      } else {
        body.insertAdjacentHTML('beforeend', '<div class="text-muted small py-2">No stock batches — use the ⋮ menu for actions.</div>');
      }
    });

    wrap.appendChild(row);
  });

  // Store items for barcode rendering - will render when tab is visible
  wrap._pendingBarcodeItems = items;

  // Render immediately if tab is visible, otherwise defer to tab show event
  const productsPane = document.getElementById('products');
  if (productsPane && productsPane.classList.contains('active')) {
    _renderBarcodes(items);
  }
}

// ═══════════════════════════════════════════════════════
// PRODUCT SELECTION BAR + BULK ACTIONS
// ═══════════════════════════════════════════════════════

function _updateSelectionBar() {
  const count = STATE._selectedProductIds.size;
  const bar   = document.getElementById('products-sel-bar');
  const cntEl = document.getElementById('products-sel-count');
  if (!bar) return;

  if (count === 0) { bar.classList.add('d-none'); return; }
  bar.classList.remove('d-none');
  cntEl.textContent = `${count} product${count > 1 ? 's' : ''} selected`;

  // Only show stock-specific actions when at least one stock_item is selected
  const selectedProds     = (STATE.products || []).filter(p => STATE._selectedProductIds.has(p.id));
  const anyStockItems     = selectedProds.some(p => p.product_type === 'stock_item');
  const anyProducedRecipes = selectedProds.some(p => p.product_type === 'recipe' && p.is_produced);
  ['receive', 'stocktake', 'writeoff'].forEach(a => {
    const btn = bar.querySelector(`[data-bulk-action="${a}"]`);
    if (btn) btn.style.display = anyStockItems ? '' : 'none';
  });
  const produceBtn = bar.querySelector('[data-bulk-action="produce"]');
  if (produceBtn) produceBtn.style.display = anyProducedRecipes ? '' : 'none';
}

// Wire selection bar (main.js loads at end of body, DOM is ready)
document.getElementById('btn-clear-sel')?.addEventListener('click', () => {
  STATE._selectedProductIds.clear();
  _updateSelectionBar();
  document.querySelectorAll('.pr-row-check').forEach(cb => { cb.checked = false; });
  document.querySelectorAll('.product-row.selected').forEach(r => r.classList.remove('selected'));
  const selAll = document.getElementById('pr-select-all');
  if (selAll) selAll.checked = false;
});
document.querySelectorAll('[data-bulk-action]').forEach(btn => {
  btn.addEventListener('click', () => _openBulkAction(btn.dataset.bulkAction));
});

function _buildUnitOptionsHtml(unitType, packageSize, packageUnit) {
  return buildUnitOptions(unitType, packageSize, packageUnit)
    .map(o => `<option value="${escapeHtml(o.value)}" data-conv="${o.conv}">${escapeHtml(o.label)}</option>`)
    .join('');
}

function _openBulkAction(action) {
  const products = (STATE.products || []).filter(p => STATE._selectedProductIds.has(p.id));
  if (!products.length) return toast('No products selected', 'warning');

  if (action === 'produce') {
    const producible = products.filter(p => p.product_type === 'recipe' && p.is_produced);
    if (!producible.length) return toast('No batch-produced recipes selected', 'warning');
    if (producible.length === 1) { openProduceModal(producible[0]); return; }
    openMultiProduceModal(producible);
    return;
  }

  if (action === 'edit') {
    if (products.length === 1) { openProductEditor(products[0]); return; }
    openBulkEditor();
    return;
  }

  if (action === 'labels') {
    openBulkLabelModal(products.map(p => p.id));
    return;
  }

  const builders = {
    receive:   _buildBulkReceive,
    stocktake: _buildBulkStocktake,
    writeoff:  _buildBulkWriteoff,
    archive:   _buildBulkArchive,
  };
  if (builders[action]) {
    builders[action](products);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).show();
  }
}

function _checkBulkReceiveSupplierMix() {
  const sels = [...document.querySelectorAll('#bulk-action-body [data-field="supplier"]')];
  const ids  = sels.map(s => s.value).filter(Boolean);
  const unique = new Set(ids);
  const warn = document.getElementById('bulk-receive-supplier-warning');
  if (warn) {
    if (unique.size > 1) warn.classList.remove('hidden');
    else warn.classList.add('hidden');
  }
}

function _buildBulkReceive(products) {
  const items = products.filter(p => p.product_type === 'stock_item');
  if (!items.length) return toast('No stock items in selection', 'warning');
  document.getElementById('bulk-action-title').textContent = `Receive Stock — ${items.length} product${items.length > 1 ? 's' : ''}`;
  const btn = document.getElementById('btn-bulk-confirm');
  btn.textContent = 'Receive All';
  btn.className   = 'btn btn-success';

  const supOptions = `<option value="">— No supplier —</option>` +
    (_suppliers || []).map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');

  document.getElementById('bulk-action-body').innerHTML = items.map(p => `
    <div class="bulk-section" data-section-pid="${p.id}">
      <div class="bulk-section-title">${escapeHtml(p.name)}</div>
      <div class="row g-2">
        <div class="col-12 col-sm-6">
          <label class="form-label small mb-1">Supplier</label>
          <select class="form-select form-select-sm" data-field="supplier" data-pid="${p.id}">${supOptions}</select>
        </div>
        <div class="col-8 col-sm-4">
          <label class="form-label small mb-1">Quantity</label>
          <div class="input-group input-group-sm">
            <input type="number" class="form-control" data-field="qty" step="0.01" min="0" placeholder="0">
            <select class="form-select" data-field="unit" style="max-width:85px">${_buildUnitOptionsHtml(p.unit_type, p.package_size, p.package_unit)}</select>
          </div>
        </div>
        <div class="col-4 col-sm-2">
          <label class="form-label small mb-1">Total R</label>
          <input type="number" class="form-control form-control-sm" data-field="price" step="0.01" min="0" placeholder="0">
        </div>
      </div>
      <div class="text-muted small mt-1" data-field="preview"></div>
    </div>
  `).join('') + `
    <div class="border-top mt-3 pt-2" id="bulk-receive-addl-costs-section">
      <div id="bulk-receive-supplier-warning" class="alert alert-info py-1 small hidden">
        <i class="bi bi-info-circle me-1"></i>
        Items from multiple suppliers detected. Costs entered here will be split across <strong>all</strong> items.
        For supplier-specific shipping, save the receive first then use the
        <strong>Suppliers tab → Recent Batches → Apply Costs</strong>.
      </div>
      <div class="small text-muted mb-1"><i class="bi bi-info-circle me-1"></i>Additional costs are split proportionally by line total across all items.</div>
      <div id="bulk-receive-addl-costs-wrap"></div>
    </div>`;

  document.querySelectorAll('#bulk-action-body .bulk-section').forEach(section => {
    const pid = parseInt(section.dataset.sectionPid);
    const p   = items.find(x => x.id === pid);
    const qtyEl   = section.querySelector('[data-field="qty"]');
    const priceEl = section.querySelector('[data-field="price"]');
    const unitEl  = section.querySelector('[data-field="unit"]');
    const preEl   = section.querySelector('[data-field="preview"]');
    const update  = () => {
      const qty   = parseFloat(qtyEl.value) || 0;
      const price = parseFloat(priceEl.value) || 0;
      const conv  = parseFloat(unitEl.options[unitEl.selectedIndex]?.dataset?.conv || 1);
      const base  = qty * conv;
      preEl.textContent = (base > 0 && price > 0)
        ? `≈ R${(price / base).toFixed(4)} / ${p.base_unit || 'unit'} for ${displayQty(base, p.unit_type)}`
        : '';
    };
    qtyEl.addEventListener('input', update);
    priceEl.addEventListener('input', update);
    unitEl.addEventListener('change', update);
    section.querySelector('[data-field="supplier"]')?.addEventListener('change', _checkBulkReceiveSupplierMix);
  });

  _renderAdditionalCostsBlock(document.getElementById('bulk-receive-addl-costs-wrap'), []);

  btn.onclick = async () => {
    const sections = [...document.querySelectorAll('#bulk-action-body .bulk-section')];

    // Collect line data first (validate all before sending any)
    const lineData = [];
    for (const section of sections) {
      const pid   = parseInt(section.dataset.sectionPid);
      const p     = items.find(x => x.id === pid);
      const qty   = parseFloat(section.querySelector('[data-field="qty"]').value) || 0;
      const price = parseFloat(section.querySelector('[data-field="price"]').value) || 0;
      const unit  = section.querySelector('[data-field="unit"]').value;
      const sid   = parseInt(section.querySelector('[data-field="supplier"]').value) || null;
      if (qty <= 0 || price <= 0) return toast(`${p.name}: qty and cost required`, 'warning');
      lineData.push({ pid, p, qty, price, unit, sid });
    }

    // Split run-level additional costs proportionally by line price
    const addlWrap  = document.getElementById('bulk-receive-addl-costs-wrap');
    const addlCosts = addlWrap ? _readAdditionalCosts(addlWrap) : [];
    const addlTotal = _addlCostsTotal(addlCosts);
    const linePrices = lineData.map(l => l.price);
    const totalBase  = linePrices.reduce((s, p) => s + p, 0);

    // Compute per-line shares (last item absorbs rounding remainder)
    const shares = addlCosts.length ? (() => {
      if (totalBase <= 0) return linePrices.map(() => addlTotal / lineData.length);
      const raw = linePrices.map(p => parseFloat(((p / totalBase) * addlTotal).toFixed(2)));
      const diff = parseFloat((addlTotal - raw.reduce((s, x) => s + x, 0)).toFixed(2));
      raw[raw.length - 1] = parseFloat((raw[raw.length - 1] + diff).toFixed(2));
      return raw;
    })() : linePrices.map(() => 0);

    if (addlCosts.length) _checkOverheadWarning(addlTotal, totalBase);

    const errs = [], ok = [];
    for (let i = 0; i < lineData.length; i++) {
      const { pid, p, qty, price, unit, sid } = lineData[i];
      // Build per-batch cost entries with this line's proportional share
      const batchAddl = addlCosts.length && shares[i] ? addlCosts.map(c => ({
        ...c,
        amount: parseFloat(((c.amount / addlTotal) * shares[i]).toFixed(2)),
        source: 'bulk_receive',
      })) : [];
      try {
        await api('/api/stock/receive', { method: 'POST', body: JSON.stringify({
          product_id: pid, qty, unit, total_price: price, supplier_id: sid,
          additional_costs: batchAddl,
        }) });
        ok.push(p.name);
      } catch (e) { errs.push(`${p.name}: ${e.message}`); }
    }
    if (ok.length) toast(`Received: ${ok.join(', ')}`, 'success', 5000);
    if (errs.length) toast(errs.join(' | '), 'error', 7000);
    if (ok.length) {
      bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).hide();
      STATE._selectedProductIds.clear(); _updateSelectionBar();
      await loadIngredients(); await loadProducts();
    }
  };
}

function _buildBulkStocktake(products) {
  const items = products.filter(p => p.product_type === 'stock_item');
  if (!items.length) return toast('No stock items in selection', 'warning');
  document.getElementById('bulk-action-title').textContent = `Stocktake — ${items.length} product${items.length > 1 ? 's' : ''}`;
  const btn = document.getElementById('btn-bulk-confirm');
  btn.textContent = 'Apply Stocktake';
  btn.className   = 'btn btn-primary';

  document.getElementById('bulk-action-body').innerHTML = items.map(p => `
    <div class="bulk-section" data-section-pid="${p.id}">
      <div class="bulk-section-title">${escapeHtml(p.name)} <span class="text-muted fw-normal" style="font-size:13px">System: ${displayQty(p.stock_level || 0, p.unit_type)}</span></div>
      <div class="row g-2 align-items-end">
        <div class="col-7">
          <label class="form-label small mb-1">Actual count</label>
          <div class="input-group input-group-sm">
            <input type="number" class="form-control" data-field="qty" step="0.01" min="0" placeholder="0">
            <select class="form-select" data-field="unit" style="max-width:85px">${_buildUnitOptionsHtml(p.unit_type, p.package_size, p.package_unit)}</select>
          </div>
        </div>
        <div class="col-12">
          <div class="small text-muted" data-field="preview"></div>
        </div>
        <div class="col-12">
          <input type="text" class="form-control form-control-sm" data-field="reason" placeholder="Reason / note (required)">
        </div>
      </div>
    </div>
  `).join('');

  document.querySelectorAll('#bulk-action-body .bulk-section').forEach(section => {
    const pid    = parseInt(section.dataset.sectionPid);
    const p      = items.find(x => x.id === pid);
    const qtyEl  = section.querySelector('[data-field="qty"]');
    const unitEl = section.querySelector('[data-field="unit"]');
    const preEl  = section.querySelector('[data-field="preview"]');
    const update = () => {
      const qty  = parseFloat(qtyEl.value) || 0;
      const conv = parseFloat(unitEl.options[unitEl.selectedIndex]?.dataset?.conv || 1);
      const base = qty * conv;
      const diff = base - (p.stock_level || 0);
      if (qty <= 0) { preEl.textContent = ''; return; }
      if (Math.abs(diff) < 0.001) preEl.innerHTML = '<i class="bi bi-check-lg me-1"></i>Matches system';
      else if (diff < 0) preEl.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>Will remove ${displayQty(Math.abs(diff), p.unit_type)}`;
      else preEl.innerHTML = `<i class="bi bi-info-circle me-1"></i>Will add ${displayQty(diff, p.unit_type)}`;
    };
    qtyEl.addEventListener('input', update);
    unitEl.addEventListener('change', update);
  });

  btn.onclick = async () => {
    const errs = [], ok = [];
    for (const section of document.querySelectorAll('#bulk-action-body .bulk-section')) {
      const pid    = parseInt(section.dataset.sectionPid);
      const p      = items.find(x => x.id === pid);
      const qty    = parseFloat(section.querySelector('[data-field="qty"]').value) || 0;
      const unitEl = section.querySelector('[data-field="unit"]');
      const unit   = unitEl.value;
      const reason = section.querySelector('[data-field="reason"]').value.trim();
      const conv   = parseFloat(unitEl.options[unitEl.selectedIndex]?.dataset?.conv || 1);
      const base   = qty * conv;
      if (qty < 0) { errs.push(`${p.name}: enter actual count`); continue; }
      if (!reason) { errs.push(`${p.name}: reason required`); continue; }
      try {
        await api('/api/stock/adjust', { method: 'POST', body: JSON.stringify({ product_id: pid, actual_qty: base, unit: p.base_unit || 'g', reason }) });
        ok.push(p.name);
      } catch (e) { errs.push(`${p.name}: ${e.message}`); }
    }
    if (ok.length) toast(`Stocktake applied: ${ok.join(', ')}`, 'success', 5000);
    if (errs.length) toast(errs.join(' | '), 'error', 7000);
    if (ok.length) {
      bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).hide();
      STATE._selectedProductIds.clear(); _updateSelectionBar();
      await loadIngredients(); await loadProducts();
    }
  };
}

function _buildBulkWriteoff(products) {
  const items = products.filter(p => p.product_type === 'stock_item');
  if (!items.length) return toast('No stock items in selection', 'warning');
  document.getElementById('bulk-action-title').textContent = `Write Off — ${items.length} product${items.length > 1 ? 's' : ''}`;
  const btn = document.getElementById('btn-bulk-confirm');
  btn.textContent = 'Apply Write-offs';
  btn.className   = 'btn btn-danger';

  document.getElementById('bulk-action-body').innerHTML = items.map(p => `
    <div class="bulk-section" data-section-pid="${p.id}">
      <div class="bulk-section-title">${escapeHtml(p.name)} <span class="text-muted fw-normal" style="font-size:13px">Available: ${displayQty(p.stock_level || 0, p.unit_type)}</span></div>
      <div class="row g-2">
        <div class="col-7">
          <label class="form-label small mb-1">Qty to write off</label>
          <div class="input-group input-group-sm">
            <input type="number" class="form-control" data-field="qty" step="0.01" min="0" placeholder="0">
            <select class="form-select" data-field="unit" style="max-width:85px">${_buildUnitOptionsHtml(p.unit_type, p.package_size, p.package_unit)}</select>
          </div>
        </div>
        <div class="col-12">
          <input type="text" class="form-control form-control-sm" data-field="reason" placeholder="Reason (required, e.g. Expired)">
        </div>
      </div>
    </div>
  `).join('');

  btn.onclick = async () => {
    const errs = [], ok = [];
    for (const section of document.querySelectorAll('#bulk-action-body .bulk-section')) {
      const pid    = parseInt(section.dataset.sectionPid);
      const p      = items.find(x => x.id === pid);
      const qty    = parseFloat(section.querySelector('[data-field="qty"]').value) || 0;
      const unit   = section.querySelector('[data-field="unit"]').value;
      const reason = section.querySelector('[data-field="reason"]').value.trim();
      if (qty <= 0) { errs.push(`${p.name}: enter qty`); continue; }
      if (!reason)  { errs.push(`${p.name}: reason required`); continue; }
      try {
        await api('/api/stock/writeoff', { method: 'POST', body: JSON.stringify({ product_id: pid, qty, unit, reason }) });
        ok.push(p.name);
      } catch (e) { errs.push(`${p.name}: ${e.message}`); }
    }
    if (ok.length) toast(`Written off: ${ok.join(', ')}`, 'warning', 5000);
    if (errs.length) toast(errs.join(' | '), 'error', 7000);
    if (ok.length) {
      bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).hide();
      STATE._selectedProductIds.clear(); _updateSelectionBar();
      await loadIngredients(); await loadProducts();
    }
  };
}

function _buildBulkLabels(products) {
  document.getElementById('bulk-action-title').textContent = `Print Labels — ${products.length} product${products.length > 1 ? 's' : ''}`;
  const btn = document.getElementById('btn-bulk-confirm');
  btn.innerHTML = '<i class="bi bi-tags me-1"></i>Print All';
  btn.className   = 'btn btn-primary';

  document.getElementById('bulk-action-body').innerHTML = `
    <p class="text-muted small mb-3">Set the number of labels for each product, then click Print All.</p>
    ${products.map(p => `
      <div class="d-flex align-items-center gap-3 mb-2 bulk-section" data-section-pid="${p.id}">
        <span style="flex:1;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(p.name)}</span>
        <div style="display:flex;align-items:center;gap:6px">
          <label class="form-label mb-0 small">Labels</label>
          <input type="number" class="form-control form-control-sm" data-field="qty" value="1" min="1" max="200" step="1" style="width:70px">
        </div>
      </div>
    `).join('')}
  `;

  btn.onclick = async () => {
    const errs = [], ok = [];
    for (const section of document.querySelectorAll('#bulk-action-body .bulk-section')) {
      const pid = parseInt(section.dataset.sectionPid);
      const p   = products.find(x => x.id === pid);
      const qty = parseInt(section.querySelector('[data-field="qty"]').value) || 1;
      try {
        await api('/api/labels/print', { method: 'POST', body: JSON.stringify({ product_id: pid, copies: qty }) });
        ok.push(`${p.name} ×${qty}`);
      } catch (e) { errs.push(`${p.name}: ${e.message}`); }
    }
    if (ok.length) toast(`Printed: ${ok.join(', ')}`, 'success', 5000);
    if (errs.length) toast(errs.join(' | '), 'error', 7000);
    if (ok.length) bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).hide();
  };
}

function _buildBulkArchive(products) {
  document.getElementById('bulk-action-title').textContent = `Archive ${products.length} product${products.length > 1 ? 's' : ''}`;
  const btn = document.getElementById('btn-bulk-confirm');
  btn.textContent = 'Archive All';
  btn.className   = 'btn btn-warning text-dark';

  document.getElementById('bulk-action-body').innerHTML = `
    <p>Archive the following products? They will be hidden from sales but their data is preserved.</p>
    <ul class="small">${products.map(p => `<li>${escapeHtml(p.name)}</li>`).join('')}</ul>
  `;

  btn.onclick = async () => {
    const errs = [], ok = [];
    for (const p of products) {
      try {
        await api(`/api/products/${p.id}/archive`, { method: 'POST' });
        ok.push(p.name);
      } catch (e) { errs.push(`${p.name}: ${e.message}`); }
    }
    if (ok.length) toast(`Archived: ${ok.join(', ')}`, 'success', 5000);
    if (errs.length) toast(errs.join(' | '), 'error', 7000);
    if (ok.length) {
      bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkActionModal')).hide();
      STATE._selectedProductIds.clear(); _updateSelectionBar();
      await loadProducts();
    }
  };
}

// ═══════════════════════════════════════════════════════
// CATEGORIES - autocomplete, filter pills, management
// ═══════════════════════════════════════════════════════

// ── Editor autocomplete (fuzzy, case-insensitive; free text = new category) ──
function hideCategorySuggestions() {
  const box = document.getElementById('p-category-suggestions');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

function _categoryMatches(query) {
  const q = (query || '').trim().toLowerCase();
  const cats = STATE.categories || [];
  if (!q) return cats.slice(0, 8);
  // Substring (contains) match, case-insensitive - "veg" → "Vegetables"
  return cats.filter(c => c.name.toLowerCase().includes(q)).slice(0, 8);
}

function renderCategorySuggestions() {
  const input = document.getElementById('p-category');
  const box   = document.getElementById('p-category-suggestions');
  if (!input || !box) return;
  const val     = input.value.trim();
  const matches = _categoryMatches(val);
  const exact   = (STATE.categories || []).some(c => c.name.toLowerCase() === val.toLowerCase());

  let html = matches.map(c =>
    `<button type="button" class="list-group-item list-group-item-action py-1" data-cat-pick="${escapeHtml(c.name)}">
       ${escapeHtml(c.name)} <span class="text-muted small">(${c.product_count})</span>
     </button>`).join('');
  if (val && !exact) {
    html += `<button type="button" class="list-group-item list-group-item-action py-1 text-success" data-cat-pick="${escapeHtml(val)}">
       + Create “${escapeHtml(val)}”
     </button>`;
  }
  if (!html) { hideCategorySuggestions(); return; }
  box.innerHTML = html;
  box.style.display = '';
  box.querySelectorAll('[data-cat-pick]').forEach(btn => {
    btn.addEventListener('mousedown', e => {   // mousedown beats input blur
      e.preventDefault();
      input.value = btn.dataset.catPick;
      hideCategorySuggestions();
    });
  });
}

(function wireCategoryAutocomplete() {
  const input = document.getElementById('p-category');
  if (!input || input._catBound) return;
  input.addEventListener('input', renderCategorySuggestions);
  input.addEventListener('focus', renderCategorySuggestions);
  input.addEventListener('blur', () => setTimeout(hideCategorySuggestions, 150));
  input._catBound = true;
})();

// ── Multi-select filter pills above the product cards ──
function renderCategoryFilterPills() {
  const wrap = document.getElementById('products-category-filter');
  if (!wrap) return;
  const tab = STATE.productsSubTab;
  // Only show the filter on product-listing sub-tabs
  if (tab === 'specials' || tab === 'categories') { wrap.style.display = 'none'; return; }

  const cats = STATE.categories || [];
  const uncatCount = STATE.products.filter(p => !p.category_id && !p.is_archived).length;
  if (cats.length === 0) { wrap.style.display = 'none'; return; }

  const sel = STATE.selectedCategoryIds;
  const pill = (id, label, count, active) =>
    `<button type="button" class="btn btn-sm ${active ? 'btn-primary' : 'btn-outline-secondary'}" data-cat-filter="${id}">
       ${escapeHtml(label)}${count != null ? ` <span class="badge ${active ? 'bg-light text-dark' : 'bg-secondary'}">${count}</span>` : ''}
     </button>`;

  let html = pill('all', 'All', null, sel.size === 0);
  html += cats.map(c => pill(c.id, c.name, c.product_count, sel.has(c.id))).join('');
  if (uncatCount > 0) html += pill('0', 'Uncategorised', uncatCount, sel.has(0));

  wrap.innerHTML = html;
  wrap.style.display = 'flex';
  wrap.querySelectorAll('[data-cat-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      const v = btn.dataset.catFilter;
      if (v === 'all') { sel.clear(); }
      else {
        const id = parseInt(v, 10);
        if (sel.has(id)) sel.delete(id); else sel.add(id);
      }
      renderProductsCards();
    });
  });
}

// ── Management view (rename / delete / merge) ──
function renderCategoriesManage() {
  const wrap = document.getElementById('categories-manage');
  if (!wrap) return;
  const cats = STATE.categories || [];
  const uncatCount = STATE.products.filter(p => !p.category_id).length;

  const options = cats.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join('');

  let rows = cats.map(c => `
    <tr data-cat-row="${c.id}">
      <td><input class="form-control form-control-sm" value="${escapeHtml(c.name)}" data-cat-name="${c.id}"></td>
      <td class="text-center align-middle">${c.product_count}</td>
      <td class="text-end">
        <button class="btn btn-outline-primary btn-sm" data-cat-save="${c.id}">Save</button>
        <button class="btn btn-outline-danger btn-sm" data-cat-del="${c.id}" data-cat-del-name="${escapeHtml(c.name)}" data-cat-del-count="${c.product_count}">Delete</button>
      </td>
    </tr>`).join('');
  if (!rows) rows = `<tr><td colspan="3" class="text-muted">No categories yet - add one below or type a new category when editing a product.</td></tr>`;

  wrap.innerHTML = `
    <div class="d-flex flex-wrap gap-2 align-items-end mb-3">
      <div>
        <label class="form-label small mb-1">New category</label>
        <input id="new-category-name" class="form-control form-control-sm" placeholder="Category name" style="width:220px">
      </div>
      <button id="btn-add-category" class="btn btn-success btn-sm">+ Add</button>
    </div>
    <div class="table-responsive">
      <table class="table table-sm align-middle">
        <thead><tr><th>Name</th><th class="text-center" style="width:90px">Products</th><th class="text-end" style="width:170px">Actions</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="text-muted small mb-3">${uncatCount} product${uncatCount === 1 ? '' : 's'} with no category. Deleting a category leaves its products uncategorised.</div>
    ${cats.length >= 2 ? `
    <div class="border-top pt-3">
      <h6 class="mb-2">Merge categories</h6>
      <div class="d-flex flex-wrap gap-2 align-items-end">
        <div><label class="form-label small mb-1">Move all products from</label>
          <select id="merge-source" class="form-select form-select-sm" style="width:200px">${options}</select></div>
        <div><label class="form-label small mb-1">into</label>
          <select id="merge-target" class="form-select form-select-sm" style="width:200px">${options}</select></div>
        <button id="btn-merge-categories" class="btn btn-outline-warning btn-sm">Merge</button>
      </div>
      <div class="form-text">The source category is removed; its products move to the target.</div>
    </div>` : ''}
  `;

  wrap.querySelector('#btn-add-category')?.addEventListener('click', async () => {
    const name = wrap.querySelector('#new-category-name')?.value?.trim();
    if (!name) { toast('Category name required', 'warning'); return; }
    await _categoryApi('/api/categories', { name });
  });
  wrap.querySelectorAll('[data-cat-save]').forEach(btn => btn.addEventListener('click', async () => {
    const id = parseInt(btn.dataset.catSave, 10);
    const name = wrap.querySelector(`[data-cat-name="${id}"]`)?.value?.trim();
    if (!name) { toast('Category name required', 'warning'); return; }
    await _categoryApi('/api/categories/update', { id, name });
  }));
  wrap.querySelectorAll('[data-cat-del]').forEach(btn => btn.addEventListener('click', async () => {
    const id = parseInt(btn.dataset.catDel, 10);
    const n  = parseInt(btn.dataset.catDelCount, 10) || 0;
    const msg = n > 0
      ? `Delete “${btn.dataset.catDelName}”? ${n} product${n === 1 ? '' : 's'} will become uncategorised.`
      : `Delete “${btn.dataset.catDelName}”?`;
    if (!confirm(msg)) return;
    await _categoryApi('/api/categories/delete', { id });
  }));
  wrap.querySelector('#btn-merge-categories')?.addEventListener('click', async () => {
    const source_id = parseInt(wrap.querySelector('#merge-source')?.value, 10);
    const target_id = parseInt(wrap.querySelector('#merge-target')?.value, 10);
    if (source_id === target_id) { toast('Pick two different categories', 'warning'); return; }
    if (!confirm('Merge these categories? This cannot be undone.')) return;
    await _categoryApi('/api/categories/merge', { source_id, target_id });
  });
}

async function _categoryApi(url, body) {
  try {
    await api(url, { method: 'POST', body: JSON.stringify(body) });
    await loadProducts();          // refresh products (category_name may have changed) + categories
    renderCategoriesManage();
    toast('Categories updated', 'success');
  } catch (e) {
    toast(e?.message || 'Category update failed', 'danger');
  }
}

let _archiveProduct = null;
let _archivePreview = null;

async function openArchiveModal(p) {
  _archiveProduct = p;
  _archivePreview = null;
  const body = document.getElementById('archive-modal-body');
  body.innerHTML = '<div class="text-muted small">Checking recipes…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('archiveProductModal')).show();

  try {
    const data = await api(`/api/products/${p.id}/archive/preview`);
    _archivePreview = data;
    const affected = data.affected_recipes || [];

    // Stock decision for products with remaining stock (stock_item or simple with stock_qty > 0)
    // Use live stock_level from the preview response - always authoritative
    const stockLevel  = data.stock_level || 0;
    const hasRemainingStock = p.product_type === 'stock_item' && stockLevel > 0;
    const stockDisplay = displayQty(stockLevel, p.unit_type);
    const stockAction = hasRemainingStock
      ? `<div class="alert alert-info py-2 mb-3">
          <strong><i class="bi bi-box-seam me-1"></i>${stockDisplay} remaining in stock.</strong> What should happen to it?
          <div class="mt-2">
            <div class="form-check">
              <input class="form-check-input" type="radio" name="archive-stock-action" id="stock-action-keep" value="keep" checked>
              <label class="form-check-label" for="stock-action-keep">Keep stock - still visible in Archived tab</label>
            </div>
            <div class="form-check">
              <input class="form-check-input" type="radio" name="archive-stock-action" id="stock-action-writeoff" value="writeoff">
              <label class="form-check-label" for="stock-action-writeoff">Write off all remaining stock</label>
            </div>
          </div>
        </div>`
      : '';

    if (affected.length === 0) {
      body.innerHTML = `<p>Archive <strong>${p.name}</strong>?</p>
        ${stockAction}
        <p class="text-muted small">It is not used in any active recipes. It will be moved to the Archived tab.</p>`;
    } else {
      let html = `<p class="mb-1">Archive <strong>${p.name}</strong>?</p>
        ${stockAction}
        <div class="alert alert-warning py-2 small mb-3">
          <i class="bi bi-exclamation-triangle me-1"></i>Used in ${affected.length} active recipe${affected.length>1?'s':''}. Choose what to do with each one.
        </div>`;

      affected.forEach(r => {
        // Build unit options for the current ingredient so we can show qty in friendly units
        const unitOpts = _archiveGetUnitOpts(r.current_unit_type, r.current_base_unit);
        const currentQtyDisplay = r.current_qty_base;

        html += `<div class="border rounded p-2 mb-3" id="archive-recipe-block-${r.recipe_id}">
          <div class="fw-semibold mb-2">${r.recipe_name}
            <span class="text-muted fw-normal small ms-1">(currently uses ${r.current_qty_base}${r.current_base_unit} of ${p.name})</span>
          </div>

          <div class="mb-2">
            <label class="form-label small mb-1">Action</label>
            <select class="form-select form-select-sm archive-action-sel" data-recipe-id="${r.recipe_id}" onchange="toggleArchiveReplaceForm(${r.recipe_id})">
              <option value="archive">Archive this recipe too</option>
              <option value="remove">Remove ${p.name} from this recipe (keep recipe active)</option>
              <option value="replace">Replace ${p.name} with another ingredient</option>
            </select>
          </div>

          <div class="border rounded p-2 bg-light hidden" id="archive-replace-form-${r.recipe_id}">
            <div class="row g-2 mb-2">
              <div class="col-12">
                <label class="form-label small mb-1">Replacement ingredient</label>
                <select class="form-select form-select-sm" id="archive-rep-ing-${r.recipe_id}" onchange="updateArchiveRepUnits(${r.recipe_id})">
                  <option value="">- select -</option>
                  ${r.replacements.map(c =>
                    `<option value="${c.id}" data-unit-type="${c.unit_type||''}" data-base-unit="${c.base_unit||''}" data-pkg-size="${c.package_size||''}" data-pkg-unit="${c.package_unit||''}">${c.name}</option>`
                  ).join('')}
                </select>
              </div>
            </div>
            <div class="row g-2">
              <div class="col-5">
                <label class="form-label small mb-1">Quantity</label>
                <input type="number" step="0.01" min="0.001" class="form-control form-control-sm"
                  id="archive-rep-qty-${r.recipe_id}" value="${currentQtyDisplay}" placeholder="qty">
              </div>
              <div class="col-7">
                <label class="form-label small mb-1">Unit</label>
                <select class="form-select form-select-sm" id="archive-rep-unit-${r.recipe_id}">
                  ${unitOpts.map(o => `<option value="${o.value}" data-conv="${o.conv}">${o.label}</option>`).join('')}
                </select>
              </div>
            </div>
          </div>
        </div>`;
      });

      body.innerHTML = html;
    }
  } catch(e) {
    body.innerHTML = `<div class="text-danger small">${e.message}</div>`;
  }
}

function _archiveGetUnitOpts(unitType, baseUnit) {
  return buildUnitOptions(unitType, null, null);
}

function toggleArchiveReplaceForm(recipeId) {
  const sel  = document.querySelector(`.archive-action-sel[data-recipe-id="${recipeId}"]`);
  const form = document.getElementById(`archive-replace-form-${recipeId}`);
  if (!form) return;
  if (sel?.value === 'replace') show(form);
  else hide(form);
}

function updateArchiveRepUnits(recipeId) {
  const ingSel  = document.getElementById(`archive-rep-ing-${recipeId}`);
  const unitSel = document.getElementById(`archive-rep-unit-${recipeId}`);
  if (!ingSel || !unitSel) return;
  const opt = ingSel.options[ingSel.selectedIndex];
  if (!opt?.value) return;
  const unitType = opt.dataset.unitType || 'weight';
  const baseUnit = opt.dataset.baseUnit || 'g';
  const pkgSize  = opt.dataset.pkgSize || null;
  const pkgUnit  = opt.dataset.pkgUnit || null;
  unitSel.innerHTML = '';
  buildUnitOptions(unitType, pkgSize, pkgUnit).forEach(o => {
    const el = document.createElement('option');
    el.value = o.value; el.textContent = o.label; el.dataset.conv = o.conv;
    unitSel.appendChild(el);
  });
}

document.getElementById('btn-archive-confirm')?.addEventListener('click', async () => {
  if (!_archiveProduct) return;
  const affected = _archivePreview?.affected_recipes || [];
  const replacements = {};
  affected.forEach(r => {
    const actionSel = document.querySelector(`.archive-action-sel[data-recipe-id="${r.recipe_id}"]`);
    const action = actionSel?.value || 'archive';
    if (action === 'remove') {
      replacements[r.recipe_id] = 'remove';
    } else if (action === 'replace') {
      const ingId   = document.getElementById(`archive-rep-ing-${r.recipe_id}`)?.value;
      const qtyDisp = parseFloat(document.getElementById(`archive-rep-qty-${r.recipe_id}`)?.value || '0');
      const unitSel = document.getElementById(`archive-rep-unit-${r.recipe_id}`);
      const conv    = parseFloat(unitSel?.options[unitSel?.selectedIndex]?.dataset?.conv || 1);
      const qtyBase = qtyDisp * conv;
      if (!ingId) return toast(`Select a replacement for "${r.recipe_name}"`, 'warning');
      replacements[r.recipe_id] = { ingredient_id: parseInt(ingId), qty_base: qtyBase };
    }
    // action === 'archive' → no entry in replacements → backend cascades
  });
  const stockAction = document.querySelector('input[name="archive-stock-action"]:checked')?.value || 'keep';
  try {
    const result = await api(`/api/products/${_archiveProduct.id}/archive`, {
      method: 'POST',
      body: JSON.stringify({ replacements, stock_action: stockAction }),
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('archiveProductModal')).hide();
    await loadProducts();
    const cascaded = result.cascaded_recipe_ids?.length || 0;
    const removed  = Object.values(replacements).filter(v => v === 'remove').length;
    const replaced = Object.values(replacements).filter(v => v !== 'remove' && typeof v === 'object').length;
    const parts = [`"${_archiveProduct.name}" archived.`];
    if (cascaded)  parts.push(`${cascaded} recipe${cascaded>1?'s':''} also archived.`);
    if (removed)   parts.push(`${removed} recipe${removed>1?'s had':' had'} the ingredient removed.`);
    if (replaced)  parts.push(`${replaced} recipe${replaced>1?'s had':' had'} the ingredient replaced.`);
    toast(parts.join(' '), 'warning', 5000);
  } catch(e) { toast(e.message, 'error'); }
});

let _restoreProduct = null;

async function openRestoreModal(p) {
  _restoreProduct = p;
  const body = document.getElementById('restore-modal-body');
  body.innerHTML = '<div class="text-muted small">Checking…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('restoreProductModal')).show();

  // Preview only - find which cascade-archived recipes could be restored alongside this product
  try {
    const restorable = _getRestorableRecipes(p);

    if (restorable.length === 0) {
      body.innerHTML = `<p>Restore <strong>${p.name}</strong> to the active catalogue?</p>
        <p class="text-muted small">No recipes need attention.</p>`;
    } else {
      let html = `<p>Restore <strong>${p.name}</strong>?</p>
        <p class="text-muted small">The following recipes were archived when this ingredient was archived. Tick the ones you want to restore too - only recipes where all other ingredients are active can be restored.</p>`;
      restorable.forEach(r => {
        html += `<div class="form-check">
          <input class="form-check-input" type="checkbox" id="restore-recipe-${r.id}" value="${r.id}" checked>
          <label class="form-check-label" for="restore-recipe-${r.id}">${r.name}</label>
        </div>`;
      });
      body.innerHTML = html;
    }
  } catch(e) {
    body.innerHTML = `<div class="text-danger small">${e.message}</div>`;
  }
}

function _getRestorableRecipes(p) {
  // From client-side STATE: find cascade-archived recipes that used this ingredient
  // and whose all other ingredients are now active
  return STATE.products.filter(r =>
    r.is_archived &&
    r.archived_reason === 'cascade' &&
    r.recipe_lines?.some(l => l.ingredient_id === p.id) &&
    r.recipe_lines?.every(l =>
      l.ingredient_id === p.id ||
      STATE.products.find(ing => ing.id === l.ingredient_id && !ing.is_archived)
    )
  ).map(r => ({ id: r.id, name: r.name }));
}

document.getElementById('btn-restore-confirm')?.addEventListener('click', async () => {
  if (!_restoreProduct) return;
  const checkboxes = document.querySelectorAll('#restore-modal-body input[type=checkbox]:checked');
  const restoreIds = [...checkboxes].map(cb => parseInt(cb.value));
  try {
    await api(`/api/products/${_restoreProduct.id}/restore`, {
      method: 'POST',
      body: JSON.stringify({ restore_recipes: restoreIds }),
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('restoreProductModal')).hide();
    await loadProducts();
    toast(`"${_restoreProduct.name}" restored.`, 'success', 2000);
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('products-filter')?.addEventListener('input', () => {
  renderProductsCards();
  setTimeout(() => {
    const wrap = document.getElementById('products-card-list');
    if (wrap?._pendingBarcodeItems) _renderBarcodes(wrap._pendingBarcodeItems);
  }, 50);
});

// ── Batch-produce modal ────────────────────────────────────────────────────────
let _produceProduct = null;

function openProduceModal(p) {
  _produceProduct = p;
  document.getElementById('produce-modal-name').textContent = p.name;
  document.getElementById('produce-batches-input').value = '1';
  _updateProducePreview();
  const existingOverhead = p.last_overhead_costs
    ? (() => { try { return JSON.parse(p.last_overhead_costs); } catch { return []; } })()
    : [];
  _renderAdditionalCostsBlock(document.getElementById('produce-addl-costs-wrap'), existingOverhead);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('produceModal')).show();
}

function _updateProducePreview() {
  const p = _produceProduct; if (!p) return;
  const batches = parseInt(document.getElementById('produce-batches-input')?.value || '1', 10) || 1;
  const units     = Math.round((p.batch_size || 1) * batches);
  const unitLabel = p.stock_unit || 'unit';
  const lines   = (p.recipe_lines || []).map(l =>
    `<li>${escapeHtml(l.ingredient_name || String(l.ingredient_id))}: ${displayQty(l.qty_base * batches, l.unit_type)}</li>`
  ).join('');
  document.getElementById('produce-modal-preview').innerHTML = `
    <div class="mb-1"><strong>${units}</strong> ${escapeHtml(unitLabel)}${units !== 1 ? 's' : ''} will be added to stock.</div>
    ${lines ? `<div class="mt-1">Ingredients consumed:</div><ul class="mb-0 ps-3">${lines}</ul>` : ''}
  `;
}

document.getElementById('produce-batches-input')?.addEventListener('input', _updateProducePreview);

document.getElementById('btn-produce-confirm')?.addEventListener('click', async () => {
  if (!_produceProduct) return;
  const batches = parseInt(document.getElementById('produce-batches-input').value || '1', 10) || 1;
  const btn = document.getElementById('btn-produce-confirm');
  btn.disabled = true;
  try {
    const prodAddlWrap  = document.getElementById('produce-addl-costs-wrap');
    const prodAddlCosts = prodAddlWrap ? _readAdditionalCosts(prodAddlWrap) : [];
    const r = await api(`/api/products/${_produceProduct.id}/produce`, {
      method: 'POST',
      body: JSON.stringify({ batches, additional_costs: prodAddlCosts }),
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('produceModal')).hide();
    toast(`Produced ${r.units_added} unit${r.units_added !== 1 ? 's' : ''} — stock now ${r.new_stock}`, 'success', 3000);
    await loadProducts();
    loadIngredients();
  } catch(e) {
    toast(e.message, 'danger');
  } finally {
    btn.disabled = false;
  }
});

// ── Multi-produce modal (bulk selection) ──────────────────────────────────────
function openMultiProduceModal(producible) {
  const body = document.getElementById('multi-produce-body');
  body.innerHTML = producible.map(p => `
    <div class="d-flex align-items-center gap-2 mb-2">
      <div class="flex-fill fw-semibold small">${escapeHtml(p.name)}</div>
      <div class="text-muted small" style="white-space:nowrap">${p.stock_level ?? p.stock_qty ?? 0} in stock</div>
      <input type="number" min="1" step="1" value="1"
        class="form-control form-control-sm multi-produce-batches"
        style="max-width:72px" data-pid="${p.id}" placeholder="batches">
    </div>
  `).join('');
  bootstrap.Modal.getOrCreateInstance(document.getElementById('multiProduceModal')).show();
}

document.getElementById('btn-multi-produce-confirm')?.addEventListener('click', async () => {
  const inputs = document.querySelectorAll('.multi-produce-batches');
  const items  = [...inputs].map(inp => ({ pid: parseInt(inp.dataset.pid), batches: parseInt(inp.value || '1') || 1 }));
  const btn    = document.getElementById('btn-multi-produce-confirm');
  btn.disabled = true;
  let totalUnits = 0, errors = [];
  for (const { pid, batches } of items) {
    try {
      const r = await api(`/api/products/${pid}/produce`, { method: 'POST', body: JSON.stringify({ batches }) });
      totalUnits += r.units_added;
    } catch(e) {
      const p = STATE.products.find(x => x.id === pid);
      errors.push(`${p?.name || pid}: ${e.message}`);
    }
  }
  btn.disabled = false;
  bootstrap.Modal.getOrCreateInstance(document.getElementById('multiProduceModal')).hide();
  if (errors.length) toast(`Errors: ${errors.join('; ')}`, 'danger', 5000);
  else toast(`Produced ${totalUnits} total unit${totalUnits !== 1 ? 's' : ''}`, 'success', 3000);
  await loadProducts();
  loadIngredients();
});

function _renderBarcodes(items) {
  if (!window.JsBarcode) return;   // library not available

  items.forEach(p => {
    if (!p.barcode) return;
    const el = document.getElementById(`bc-${p.id}`);
    if (!el || el.tagName.toLowerCase() !== 'svg') return;

    const digits = /^\d+$/.test(p.barcode);
    const format = digits && p.barcode.length === 13 ? 'EAN13'
                 : digits && p.barcode.length === 8  ? 'EAN8'
                 : 'CODE128';
    try {
      JsBarcode(el, p.barcode, {
        format,
        width:        1.5,
        height:       32,
        displayValue: false,
        margin:       2,
        lineColor:    '#222',
        background:   'transparent',
      });
    } catch {
      const span = document.createElement('span');
      span.className = 'font-monospace';
      span.style.cssText = 'font-size:11px;color:#555';
      span.textContent = p.barcode;
      el.replaceWith(span);
    }
  });
}

// Products sub-tab switching (Active / Ingredients / Archived / Specials)
document.getElementById('products-sub-tabs')?.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-products-sub]');
  if (!btn) return;
  STATE.productsSubTab = btn.dataset.productsSub;
  document.querySelectorAll('[data-products-sub]').forEach(b => {
    b.classList.toggle('active', b.dataset.productsSub === STATE.productsSubTab);
  });

  const isSpecials   = STATE.productsSubTab === 'specials';
  const isCategories = STATE.productsSubTab === 'categories';
  const isProductList = !isSpecials && !isCategories;

  const specialsList = document.getElementById('specials-list');
  const catManage    = document.getElementById('categories-manage');
  const cardList     = document.getElementById('products-card-list');
  const newProduct   = document.getElementById('btn-new-product');
  const newSpecial   = document.getElementById('btn-new-special');
  const filterInput  = document.getElementById('products-filter');
  const catFilter    = document.getElementById('products-category-filter');
  if (specialsList) specialsList.classList.toggle('hidden', !isSpecials);
  if (catManage)    catManage.classList.toggle('hidden', !isCategories);
  if (cardList)     cardList.style.display = isProductList ? '' : 'none';
  if (catFilter && !isProductList) catFilter.style.display = 'none';
  if (newProduct)   newProduct.classList.toggle('hidden', !isProductList);
  if (newSpecial)   newSpecial.classList.toggle('hidden', !isSpecials);
  if (filterInput)  filterInput.classList.toggle('hidden', !isProductList);

  if (isProductList) {
    // Hide +New Product button on archived tab - you can't create archived products
    if (newProduct) newProduct.classList.toggle('hidden', STATE.productsSubTab === 'archived');
    renderProductsCards();
    // Reload ingredients data when switching to recipes sub-tab so costs are current
    if (STATE.productsSubTab === 'recipes') loadIngredients();
    setTimeout(() => {
      const wrap = document.getElementById('products-card-list');
      if (wrap?._pendingBarcodeItems) _renderBarcodes(wrap._pendingBarcodeItems);
    }, 50);
  } else if (isSpecials) {
    renderSpecialsList();
  } else {
    renderCategoriesManage();
  }
});

// ═══════════════════════════════════════════════════════
// PRODUCT EDITOR MODAL
// ═══════════════════════════════════════════════════════
let _recipeLines  = [];   // [{ingredient_id, ingredient_name, qty_base, unit_type, base_unit}]
let _sellPackages = [];   // [{name, qty_base, price, barcode, id?}]
let _editingProductId = null;

function openProductEditor(p) {
  _editingProductId = p?.id ?? null;
  _recipeLines  = (p?.recipe_lines  || []).map(l => ({ ...l }));
  _sellPackages = (p?.sell_packages || []).map(l => ({ ...l }));

  document.getElementById('p-id').value        = p?.id ?? '';
  document.getElementById('p-name').value      = p?.name ?? '';
  const _bc = document.getElementById('p-barcode');
  if (_bc) {
    _bc.value = p?.barcode ?? '';
    _bc.dataset.autoGenerated = p ? '0' : '1'; // existing=locked, new=auto
  }
  document.getElementById('p-stock').value     = p?.stock_qty ?? '';
  document.getElementById('p-type').value      = p?.product_type ?? 'stock_item';
  document.getElementById('p-unit-type').value = p?.unit_type ?? 'weight';

  // Scale fields
  const syncEl = document.getElementById('p-sync-to-scale');
  if (syncEl) syncEl.checked = p ? !!p.sync_to_scale : !!p?.sold_by_weight;
  // PLU field
  const pluEl = document.getElementById('p-product-code');
  if (pluEl) { pluEl.value = p?.product_code || ''; pluEl.dataset.original = p?.product_code || ''; }
  document.getElementById('p-plu-conflict')?.classList.add('hidden');
  const _st = document.getElementById('p-scale-tare'); if (_st) _st.value = p?.scale_tare || '';
  const _ssl = document.getElementById('p-scale-shelf-life'); if (_ssl) _ssl.value = p?.scale_shelf_life || '';
  const _sm1 = document.getElementById('p-scale-msg1'); if (_sm1) _sm1.value = p?.scale_msg1 || '';
  const _sm2 = document.getElementById('p-scale-msg2'); if (_sm2) _sm2.value = p?.scale_msg2 || '';
  if (document.getElementById('p-scale-open-price')) document.getElementById('p-scale-open-price').checked = !!p?.scale_open_price;
  if (document.getElementById('p-scale-prohibit')) document.getElementById('p-scale-prohibit').checked = !!p?.scale_prohibit;
  // Show sync status if editing
  const statusRow = document.getElementById('scale-sync-status-row');
  if (statusRow && p) {
    if (p.scale_last_sync_status) {
      statusRow.textContent = `Last sync: ${p.scale_last_sync_status}${p.scale_last_synced_at ? ' at ' + new Date(p.scale_last_synced_at).toLocaleString() : ''}${p.scale_last_sync_error ? ' - ' + p.scale_last_sync_error : ''}`;
      statusRow.classList.remove('hidden');
    } else { statusRow.classList.add('hidden'); }
  } else if (statusRow) { statusRow.classList.add('hidden'); }
  document.getElementById('p-low-stock').value = p?.low_stock_threshold ?? '';
  document.getElementById('p-is-for-sale').checked          = p?.is_for_sale !== false;
  document.getElementById('p-is-prepared').checked          = !!p?.is_prepared;
  const _isProducedEl = document.getElementById('p-is-produced');
  if (_isProducedEl) _isProducedEl.checked = !!p?.is_produced;
  const _batchSizeEl = document.getElementById('p-batch-size');
  if (_batchSizeEl) _batchSizeEl.value = p?.batch_size ?? 1;
  const _stockUnitEl = document.getElementById('p-stock-unit');
  if (_stockUnitEl) _stockUnitEl.value = p?.stock_unit ?? '';
  const _onlineEl = document.getElementById('p-is-available-online');
  if (_onlineEl) _onlineEl.checked = !!p?.is_available_online;

  // Description
  const descEl = document.getElementById('p-description');
  if (descEl) descEl.value = p?.description ?? '';

  // Category (autocomplete text input - shows the current category name)
  const catEl = document.getElementById('p-category');
  if (catEl) catEl.value = p?.category_name ?? '';
  hideCategorySuggestions();

  // Multi-image list
  const _fileInp = document.getElementById('p-image-files');
  if (_fileInp) {
    _fileInp.value = '';
    // Accumulate selections across multiple picker opens
    if (!_fileInp._boundChange) {
      _fileInp.addEventListener('change', () => {
        // Copy File objects into Blobs immediately so they stay valid after input is cleared
        for (const f of _fileInp.files) {
          const blob = f.slice(0, f.size, f.type);
          blob._name = f.name;   // preserve original name for error messages
          _pendingFiles.push(blob);
        }
        _fileInp.value = '';   // reset so same file can be re-picked
        renderImageList();
      });
      _fileInp._boundChange = true;
    }
  }
  _pendingFiles  = [];
  _editingImages = (p?.images || []).slice().sort((a, b) => a.display_order - b.display_order);
  renderImageList();

  // Single price field: weight/volume stock_items use price_per_unit, everything else uses price
  const isWeightOrVolume = (p?.unit_type === 'weight' || p?.unit_type === 'volume')
                        && p?.product_type === 'stock_item';

  if (isWeightOrVolume && p?.price_per_unit != null) {
    // Display in the larger friendly unit by default (kg or L), unless very small
    // e.g. price_per_unit=0.005/g → display as R5/kg
    const ut      = p.unit_type;
    const bigUnit = ut === 'weight' ? 'kg' : 'L';
    const conv    = UNITS[ut]?.toBase[bigUnit] || 1;  // 1000
    const priceInBigUnit = parseFloat(p.price_per_unit) * conv;
    document.getElementById('p-price').value = +priceInBigUnit.toFixed(4);
    // Set the unit selector to kg/L after sections are built
    document.getElementById('p-price').dataset.displayUnit = bigUnit;
  } else {
    document.getElementById('p-price').value = p?.price ?? '';
    document.getElementById('p-price').dataset.displayUnit = '';
  }

  // Show package size in the unit it was entered
  const pkgSizeUnit = p?.package_size_unit || UNITS[p?.unit_type]?.base || 'g';
  const pkgSizeDisplay = (p?.package_size != null && p?.package_size_unit)
    ? p.package_size / (UNITS[p.unit_type]?.toBase[p.package_size_unit] || 1)
    : (p?.package_size ?? '');
  document.getElementById('p-pkg-size').value = pkgSizeDisplay !== '' ? +pkgSizeDisplay.toFixed(6) : '';
  document.getElementById('p-pkg-unit').value = p?.package_unit ?? '';

  document.getElementById('productEditorTitle').textContent = p ? `Edit - ${p.name}` : 'New Product';
  // Reset calculator
  hide(document.getElementById('calc-result'));
  initCalcMarkup(p);
  updateProductTypeSections(p?.product_type ?? 'stock_item');
  // Sync batch-size visibility after type sections are built
  const _ypEl = document.getElementById('p-is-produced');
  const _yuEl = document.getElementById('row-batch-size');
  if (_ypEl && _yuEl) { _ypEl.checked ? show(_yuEl) : hide(_yuEl); }

  // Restore price display unit after sections are built
  const savedPriceUnit = document.getElementById('p-price')?.dataset?.displayUnit;
  if (savedPriceUnit) {
    const priceUnitSel = document.getElementById('p-price-unit');
    if (priceUnitSel && [...priceUnitSel.options].some(o => o.value === savedPriceUnit)) {
      priceUnitSel.value = savedPriceUnit;
    }
  }

  // Set package size unit after dropdown is built by updateProductTypeSections
  if (pkgSizeUnit) {
    const pkgUnitSel = document.getElementById('p-pkg-size-unit');
    if (pkgUnitSel && [...pkgUnitSel.options].some(o => o.value === pkgSizeUnit)) {
      pkgUnitSel.value = pkgSizeUnit;
    }
  }
  updatePkgSizeBaseDisplay();
  renderRecipeLines();
  renderPackagesTable();
  { const h = document.getElementById('purchase-history-list'); if (h) h.innerHTML = ''; }
  const isEdit = !!p?.id;
  document.getElementById('btn-add-product')   ?.classList.toggle('hidden', isEdit);
  document.getElementById('btn-update-product') ?.classList.toggle('hidden', !isEdit);
  document.getElementById('btn-delete-product') ?.classList.toggle('hidden', !isEdit);
  // Calculator only makes sense when editing - needs existing stock batches to compute cost
  const calcSection = document.getElementById('section-calc');
  if (calcSection) calcSection.style.display = isEdit ? '' : 'none';
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal'));
  modal.show();
  // Re-apply barcode after modal show so scanner input during open doesn't stick
  if (!p) {
    document.getElementById('productEditorModal')?.addEventListener('shown.bs.modal', () => {
      const bc = document.getElementById('p-barcode');
      if (bc && bc.dataset.autoGenerated !== '0') {
        const type = document.getElementById('p-type')?.value || 'stock_item';
        updateProductTypeSections(type);
      }
    }, { once: true });
  }
}

document.getElementById('btn-new-product')?.addEventListener('click', () => {
  openProductEditor(null);
});

// PLU conflict check on input
document.getElementById('p-product-code')?.addEventListener('input', async function() {
  const val = parseInt(this.value, 10);
  const conflictEl = document.getElementById('p-plu-conflict');
  const original = parseInt(this.dataset.original || '0', 10);
  if (!val || val === original) { conflictEl?.classList.add('hidden'); return; }
  // Check against loaded products
  const conflict = STATE.products.find(p => p.product_code === val && p.id !== parseInt(document.getElementById('p-id')?.value));
  if (conflict) {
    if (conflictEl) { conflictEl.textContent = `PLU ${val} already used by "${conflict.name}"`; conflictEl.classList.remove('hidden'); }
  } else {
    conflictEl?.classList.add('hidden');
  }
  // Update barcode preview
  const type = document.getElementById('p-type')?.value;
  updateProductTypeSections(type);
});

// Clear auto-generated flag only when user manually types (not scanner or programmatic)
document.getElementById('p-barcode')?.addEventListener('input', function(e) {
  // InputType 'insertText' = single keystroke; scanner pastes as rapid insertText bursts
  // but we can detect scanner by checking if value looks like a barcode typed very fast
  // Simplest heuristic: only lock if user typed a single character at a time
  if (e.inputType === 'insertText' && e.data?.length === 1) {
    this.dataset.autoGenerated = '0';
  }
  // If scanner pasted a barcode (rapid multi-char), re-trigger auto logic instead
  if (this.dataset.autoGenerated !== '0') {
    const type = document.getElementById('p-type')?.value || 'stock_item';
    updateProductTypeSections(type);
  }
});

// btn-remove-image removed - image deletion now handled per-image via renderImageList()

document.getElementById('p-type')?.addEventListener('change', (e) => {
  updateProductTypeSections(e.target.value);
});

document.getElementById('p-unit-type')?.addEventListener('change', () => {
  updateProductTypeSections(document.getElementById('p-type').value);
  renderPackagesTable();
});

document.getElementById('p-is-for-sale')?.addEventListener('change', (e) => {
  updateProductTypeSections(document.getElementById('p-type').value);
  // Inform user what unchecking means
  if (!e.target.checked) {
    toast('This product will be moved to the Ingredients tab - it stays available for recipes but won\'t appear at the till.', 'info', 4000);
  }
});

document.getElementById('p-pkg-size')?.addEventListener('input', updatePkgSizeBaseDisplay);
document.getElementById('p-pkg-size-unit')?.addEventListener('change', updatePkgSizeBaseDisplay);

function updatePkgSizeBaseDisplay() {
  const sizeEl    = document.getElementById('p-pkg-size');
  const unitSel   = document.getElementById('p-pkg-size-unit');
  const displayEl = document.getElementById('pkg-size-base-display');
  if (!sizeEl || !unitSel || !displayEl) return;

  const qty      = parseFloat(sizeEl.value) || 0;
  const unit     = unitSel.value;
  const unitType = document.getElementById('p-unit-type')?.value || 'weight';
  const conv     = UNITS[unitType]?.toBase[unit] || 1;
  const base     = qty * conv;
  const baseUnit = UNITS[unitType]?.base || unit;

  if (qty > 0 && conv !== 1) {
    displayEl.textContent = `= ${displayQty(base, unitType)} per package`;
  } else {
    displayEl.textContent = '';
  }
}

function updateProductTypeSections(type) {
  const isStockItem = type === 'stock_item';
  const isRecipe    = type === 'recipe';

  const el = id => document.getElementById(id);

  // Show unit-type selector for stock items and produced recipes
  const _isProducedCheck = isRecipe && !!document.getElementById('p-is-produced')?.checked;
  (isStockItem || _isProducedCheck) ? show(el('section-unit-type-early')) : hide(el('section-unit-type-early'));

  isStockItem ? show(el('section-stock-item')) : hide(el('section-stock-item'));
  isRecipe    ? show(el('section-recipe'))     : hide(el('section-recipe'));
  if (!isRecipe) hide(el('row-batch-size'));
  hide(el('row-stock-qty'));
  hide(el('section-purchase'));
  show(el('is-for-sale-row'));

  const isForSale = el('p-is-for-sale')?.checked;
  const unitType  = el('p-unit-type')?.value || 'weight';
  const baseUnit  = UNITS[unitType]?.base || 'unit';

  // Derive sold_by_weight automatically: weight/volume = true, count = false
  const autoSoldByWeight = isStockItem && (unitType === 'weight' || unitType === 'volume');

  // Sync hidden input so buildProductPayload still reads it
  const sbwInput = el('p-sold-by-weight');
  if (sbwInput) sbwInput.value = autoSoldByWeight ? '1' : '0';

  // Price field: hide only for internal-only ingredients
  if (isStockItem && !isForSale) {
    hide(el('price-row'));
  } else {
    show(el('price-row'));
  }

  // Update price field label and unit selector
  const priceLabel   = el('price-row-label');
  const priceSuffix  = el('price-unit-suffix');
  const priceUnitSel = el('p-price-unit');

  if (autoSoldByWeight) {
    if (priceLabel) priceLabel.textContent = 'Selling price';

    // Build unit dropdown for the price unit
    if (priceUnitSel) {
      const prevPriceUnit = priceUnitSel.value;
      priceUnitSel.innerHTML = '';
      (UNITS[unitType]?.display || [baseUnit]).forEach(u => {
        const opt = document.createElement('option');
        opt.value = u; opt.textContent = `per ${u}`;
        priceUnitSel.appendChild(opt);
      });
      if (prevPriceUnit && [...priceUnitSel.options].some(o => o.value === prevPriceUnit)) {
        priceUnitSel.value = prevPriceUnit;
      }
      hide(priceSuffix);
      show(priceUnitSel);
    }
  } else {
    if (priceLabel) priceLabel.textContent = 'Selling price';
    if (priceSuffix) { hide(priceSuffix); }
    if (priceUnitSel) { hide(priceUnitSel); }
  }

  // Update unit-type dependent dropdowns

  // Rebuild low stock unit dropdown
  const lowStockUnitSel = el('p-low-stock-unit');
  if (lowStockUnitSel) {
    const prevLow = lowStockUnitSel.value;
    lowStockUnitSel.innerHTML = '';
    (UNITS[unitType]?.display || [baseUnit]).forEach(u => {
      const opt = document.createElement('option'); opt.value = u; opt.textContent = u;
      lowStockUnitSel.appendChild(opt);
    });
    if (prevLow && [...lowStockUnitSel.options].some(o => o.value === prevLow)) lowStockUnitSel.value = prevLow;
  }

  // Rebuild package size unit dropdown based on unit type
  const pkgSizeUnitSel = el('p-pkg-size-unit');
  if (pkgSizeUnitSel) {
    const prevVal = pkgSizeUnitSel.value;
    pkgSizeUnitSel.innerHTML = '';
    (UNITS[unitType]?.display || [baseUnit]).forEach(u => {
      const opt = document.createElement('option');
      opt.value = u; opt.textContent = u;
      pkgSizeUnitSel.appendChild(opt);
    });
    // Restore previous selection if still valid
    if (prevVal && [...pkgSizeUnitSel.options].some(o => o.value === prevVal)) {
      pkgSizeUnitSel.value = prevVal;
    }
  }
  updatePkgSizeBaseDisplay();

  // Scale section: only relevant for weight/volume stock items
  const scaleSection = el('section-scale');
  if (scaleSection) autoSoldByWeight ? show(scaleSection) : hide(scaleSection);

  // Barcode field: hidden for weight/volume (scale generates dynamically)
  const barcodeRow = el('row-barcode');
  if (barcodeRow) autoSoldByWeight ? hide(barcodeRow) : show(barcodeRow);

  // PLU range hint
  const pluHint = el('p-plu-range-hint');
  if (pluHint) {
    if (autoSoldByWeight && isStockItem) {
      pluHint.textContent = el('p-unit-type')?.value === 'volume' ? 'range 30000-39999' : 'range 1-19999';
    } else if (type === 'recipe') {
      pluHint.textContent = 'range 40000-49999';
    } else {
      pluHint.textContent = 'range 20000-29999';
    }
  }

  // Scale barcode preview (weight/volume only)
  const previewEl = el('p-scale-barcode-preview');
  const previewVal = el('p-scale-barcode-value');
  if (previewEl && previewVal) {
    if (autoSoldByWeight) {
      const pluCode = parseInt(el('p-product-code')?.value || '0', 10);
      if (pluCode > 0) {
        const pluStr = String(pluCode).padStart(4, '0');
        previewVal.textContent = `20${pluStr}VVVVVVC  (where VVVVVV = total price cents at print time)`;
      } else {
        previewVal.textContent = 'Enter PLU number to see preview';
      }
      show(previewEl);
    } else {
      hide(previewEl);
    }
  }

  // Update barcode preview based on product type - always show a value
  const barcodeInput = el('p-barcode');
  if (barcodeInput && barcodeInput.dataset.autoGenerated !== '0') {
    if (autoSoldByWeight) {
      // Weight/volume: find next code in the correct range, show scale barcode preview
      // (value part is 00000 - scale encodes actual weight/volume at print time)
      const isVolume = unitType === 'volume';
      const lo = isVolume ? 30000 : 1;
      const hi = isVolume ? 39999 : 19999;
      const usedCodes = new Set(STATE.products
        .filter(p => p.product_code >= lo && p.product_code <= hi)
        .map(p => p.product_code));
      let nextCode = lo;
      while (usedCodes.has(nextCode) && nextCode <= hi) nextCode++;
      const core = `20${String(nextCode).padStart(5,'0')}00000`;
      barcodeInput.value = core + ean13Check(core);
      barcodeInput.placeholder = '';
      barcodeInput.title = 'Preview - scale encodes actual weight/volume at print time';
    } else {
      // Fixed/recipe: deterministic EAN from next available code
      const lo = type === 'recipe' ? 40000 : 20000;
      const hi = lo + 9999;
      const usedCodes = new Set(STATE.products
        .filter(p => p.product_code >= lo && p.product_code <= hi)
        .map(p => p.product_code));
      let nextCode = lo;
      while (usedCodes.has(nextCode) && nextCode <= hi) nextCode++;
      const core = `1${String(nextCode).padStart(5,'0')}000000`;
      barcodeInput.value = core + ean13Check(core);
      barcodeInput.placeholder = '';
      barcodeInput.title = '';
    }
  }
}

// ── Recipe Lines ──
// Recursively calculate the cost for any product (stock_item or recipe) × qty
// Uses STATE._stockCostMap (FIFO cost per base unit) for stock items
function getIngredientCost(productId, qty, _depth = 0) {
  if (_depth > 10 || !productId) return 0;
  const p = STATE.products.find(x => x.id === productId);
  if (!p) return 0;
  if (p.product_type === 'stock_item') {
    return parseFloat(qty) * (STATE._stockCostMap?.[productId] || 0);
  }
  if (p.product_type === 'recipe') {
    const perUnit = p.is_produced ? (p.batch_size || 1) : 1;
    const lines = p.recipe_lines || [];
    return lines.reduce((sum, rl) => {
      return sum + getIngredientCost(rl.ingredient_id, rl.qty_base * qty / perUnit, _depth + 1);
    }, 0);
  }
  return 0;
}

// Returns { markup, margin } as rounded % strings, or null if cost/price unavailable
function calcProductMargins(p) {
  let cost = null;
  let price = null;

  if (p.product_type === 'stock_item') {
    const costPerBase = STATE._stockCostMap?.[p.id];
    if (costPerBase == null) return null;
    if (p.sold_by_weight) {
      // Both cost and price are per base unit
      cost  = costPerBase;
      price = parseFloat(p.price_per_unit);
    } else {
      // Sold per package - cost is costPerBase × package_size (stored in base units)
      // Count products always sell one base unit at a time; package_size is a receiving
      // convenience only (e.g. 1 box = 6 units) and must NOT multiply the per-unit COGS.
      const pkgBase = p.unit_type === 'count' ? 1 : (parseFloat(p.package_size) || null);
      if (!pkgBase) return null;
      cost  = costPerBase * pkgBase;
      price = parseFloat(p.price);
    }
  } else if (p.product_type === 'recipe') {
    cost  = getIngredientCost(p.id, 1);
    price = parseFloat(p.price);
  } else {
    return null;
  }

  if (!cost || !price || isNaN(cost) || isNaN(price) || cost <= 0 || price <= 0) return null;

  const markup = ((price - cost) / cost * 100).toFixed(1);
  const margin = ((price - cost) / price * 100).toFixed(1);

  // Format cost label - for by-weight products show per kg/L, for fixed products show per unit
  let costLabel;
  if (p.product_type === 'stock_item' && p.sold_by_weight) {
    const bigUnit = p.unit_type === 'volume' ? 'L' : 'kg';
    const conv    = UNITS[p.unit_type]?.toBase[bigUnit] || 1;
    costLabel = `R${fmt(cost * conv)}/${bigUnit}`;
  } else {
    costLabel = `R${fmt(cost)}`;
  }

  return { markup, margin, costLabel };
}

function renderRecipeLines() {
  const tbody = document.getElementById('recipe-lines-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  let totalCost = 0;

  _recipeLines.forEach((line, idx) => {
    const ingr     = STATE.products.find(p => p.id === line.ingredient_id);
    const unitType = ingr?.unit_type || line.unit_type || 'weight';
    const baseUnit = ingr?.base_unit || line.base_unit || 'g';
    const unitOpts = UNITS[unitType]?.display || [baseUnit];

    // Calculate line cost - works for both stock_item and recipe ingredients
    const qty      = parseFloat(line.qty_base_display || line.qty_base || 0);
    const unit     = line.unit || baseUnit;
    const qtyBase  = ingr?.product_type === 'recipe' ? qty : toBase(qty, unit, unitType);
    const lineCost = getIngredientCost(ingr?.id, qtyBase);
    totalCost += lineCost;

    const tr = document.createElement('tr');
    tr.className = 'recipe-line-row';

    // Ingredient selector - stock items AND other recipes (for bundles/specials)
    const currentProductId = parseInt(document.getElementById('p-id')?.value || '0');
    let ingSelHTML = `<select class="form-select form-select-sm" data-rl-idx="${idx}" data-rl-field="ingredient_id">`;
    ingSelHTML += '<option value="">- select -</option>';
    // Group: stock items first, then recipes
    const ingStockItems = STATE.products.filter(p => p.product_type === 'stock_item' && !p.is_archived);
    const ingRecipes    = STATE.products.filter(p => p.product_type === 'recipe' && p.id !== currentProductId && !p.is_archived);
    if (ingStockItems.length) {
      ingSelHTML += `<optgroup label="Stock Items">`;
      ingStockItems.forEach(p => {
        ingSelHTML += `<option value="${p.id}" ${p.id === line.ingredient_id ? 'selected' : ''}>${p.name}</option>`;
      });
      ingSelHTML += `</optgroup>`;
    }
    if (ingRecipes.length) {
      ingSelHTML += `<optgroup label="Recipes (for bundles)">`;
      ingRecipes.forEach(p => {
        ingSelHTML += `<option value="${p.id}" ${p.id === line.ingredient_id ? 'selected' : ''}>${p.name}</option>`;
      });
      ingSelHTML += `</optgroup>`;
    }
    ingSelHTML += '</select>';

    // Unit selector: disabled for recipe ingredients (show stock_unit label instead)
    const isRecipeIngredient = ingr?.product_type === 'recipe';
    let unitSelHTML;
    if (isRecipeIngredient) {
      const label = ingr?.stock_unit || ingr?.base_unit || 'unit';
      unitSelHTML = `<span class="form-control form-control-sm text-muted" style="width:auto;background:#f8f9fa">${escapeHtml(label)}</span>`;
    } else {
      unitSelHTML = `<select class="form-select form-select-sm" data-rl-idx="${idx}" data-rl-field="unit" style="width:auto">`;
      unitOpts.forEach(u => { unitSelHTML += `<option value="${u}" ${u === (line.unit || baseUnit) ? 'selected' : ''}>${u}</option>`; });
      unitSelHTML += '</select>';
    }

    tr.innerHTML = `
      <td>${ingSelHTML}</td>
      <td><input type="number" step="0.01" min="0.01" value="${line.qty_base_display || line.qty_base || ''}" class="form-control form-control-sm" data-rl-idx="${idx}" data-rl-field="qty_display" style="width:80px"></td>
      <td>${unitSelHTML}</td>
      <td class="small text-muted">${lineCost > 0 ? `R${lineCost.toFixed(4)}` : '-'}</td>
      <td><button class="btn btn-outline-danger btn-sm" data-rl-remove="${idx}"><i class="bi bi-x-lg"></i></button></td>
    `;
    tbody.appendChild(tr);
  });

  // Bind changes
  tbody.querySelectorAll('[data-rl-idx]').forEach(el => {
    el.addEventListener('change', () => {
      const idx   = parseInt(el.dataset.rlIdx);
      const field = el.dataset.rlField;
      if (field === 'ingredient_id') {
        _recipeLines[idx].ingredient_id = parseInt(el.value) || null;
        const ingr = STATE.products.find(p => p.id === _recipeLines[idx].ingredient_id);
        _recipeLines[idx].unit_type = ingr?.unit_type;
        _recipeLines[idx].base_unit = ingr?.base_unit;
        // Default qty to 1 for recipe ingredients (portions)
        if (ingr?.product_type === 'recipe') {
          _recipeLines[idx].qty_base_display = 1;
          _recipeLines[idx].qty_base = 1;
        }
        renderRecipeLines();
      } else if (field === 'qty_display') {
        _recipeLines[idx].qty_base_display = parseFloat(el.value) || 0;
      } else if (field === 'unit') {
        _recipeLines[idx].unit = el.value;
      }
    });
    el.addEventListener('input', () => {
      const field = el.dataset.rlField;
      if (field === 'qty_display') {
        const idx = parseInt(el.dataset.rlIdx);
        _recipeLines[idx].qty_base_display = parseFloat(el.value) || 0;
        // Update only the cost cell - avoid full re-render which destroys focus
        const ingr    = STATE.products.find(p => p.id === _recipeLines[idx].ingredient_id);
        const unitType = ingr?.unit_type || _recipeLines[idx].unit_type || 'weight';
        const unit     = _recipeLines[idx].unit || ingr?.base_unit || UNITS[unitType]?.base || 'g';
        const qtyBase  = ingr?.product_type === 'recipe'
          ? _recipeLines[idx].qty_base_display
          : toBase(_recipeLines[idx].qty_base_display, unit, unitType);
        const cost     = getIngredientCost(ingr?.id, qtyBase);
        const row      = el.closest('tr');
        if (row) {
          const costCell = row.querySelector('td:nth-child(4)');
          if (costCell) costCell.textContent = cost > 0 ? `R${cost.toFixed(4)}` : '-';
        }
        // Recalc total
        let total = 0;
        _recipeLines.forEach((l, i) => {
          const g = STATE.products.find(p => p.id === l.ingredient_id);
          const ut = g?.unit_type || l.unit_type || 'weight';
          const u  = l.unit || g?.base_unit || UNITS[ut]?.base || 'g';
          const qb = g?.product_type === 'recipe' ? (l.qty_base_display || 0) : toBase(l.qty_base_display || 0, u, ut);
          total += getIngredientCost(g?.id, qb);
        });
        const totEl = document.getElementById('recipe-cost-total');
        if (totEl) totEl.textContent = total > 0 ? `Est. ingredient cost: R${total.toFixed(4)}` : '';
      }
    });
  });
  tbody.querySelectorAll('[data-rl-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      _recipeLines.splice(parseInt(btn.dataset.rlRemove), 1);
      renderRecipeLines();
    });
  });

  const totEl = document.getElementById('recipe-cost-total');
  if (totEl) totEl.textContent = totalCost > 0 ? `Est. ingredient cost: R${totalCost.toFixed(4)}` : '';
}

function getRecipeLinesForSubmit() {
  return _recipeLines
    .filter(l => l.ingredient_id && (l.qty_base_display > 0 || l.qty_base > 0))
    .map(l => {
      const ingr = STATE.products.find(p => p.id === l.ingredient_id);
      let qty_base;
      if (ingr?.product_type === 'recipe') {
        // Recipe ingredient: qty is "portions" - no unit conversion needed
        qty_base = parseFloat(l.qty_base_display || l.qty_base || 1);
      } else {
        const unitType   = ingr?.unit_type || l.unit_type || 'weight';
        const unit       = l.unit || ingr?.base_unit || UNITS[unitType]?.base || 'unit';
        const qtyDisplay = parseFloat(l.qty_base_display || l.qty_base || 0);
        qty_base         = toBase(qtyDisplay, unit, unitType);
      }
      return { ingredient_id: l.ingredient_id, qty_base };
    });
}

document.getElementById('btn-add-recipe-line')?.addEventListener('click', () => {
  _recipeLines.push({ ingredient_id: null, qty_base: 0, qty_base_display: 0, unit: 'g', unit_type: 'weight', base_unit: 'g' });
  renderRecipeLines();
});

document.getElementById('p-is-produced')?.addEventListener('change', () => {
  const on = document.getElementById('p-is-produced').checked;
  on ? show(document.getElementById('row-batch-size')) : hide(document.getElementById('row-batch-size'));
  on ? show(document.getElementById('section-unit-type-early')) : hide(document.getElementById('section-unit-type-early'));
});

// Pre-fill calc markup from settings when modal opens
function initCalcMarkup(product) {
  const calcMarkup = document.getElementById('calc-markup');
  if (!calcMarkup) return;
  // Prefer saved product margin, fall back to global markup setting
  if (product?.margin_pct != null) {
    calcMarkup.value = product.margin_pct;
  } else {
    calcMarkup.value = _globalMarkupPct;
  }
}

document.getElementById('btn-calc-price')?.addEventListener('click', async () => {
  const id     = parseInt(document.getElementById('p-id').value || '0', 10);
  const type   = document.getElementById('p-type').value;
  const markup = parseFloat(document.getElementById('calc-markup').value || '40') || 40;

  const resultEl     = document.getElementById('calc-result');
  const avgCostEl    = document.getElementById('calc-avg-cost');
  const suggestedEl  = document.getElementById('calc-suggested');
  const breakdownEl  = document.getElementById('calc-breakdown-table');
  const suggestionsEl= document.getElementById('calc-suggestions-row');

  // If not saved yet, calculate live from current recipe lines in the editor
  if (!id) {
    const lines = getRecipeLinesForSubmit();
    if (lines.length === 0) return toast('Add at least one ingredient first', 'warning');
    let totalCost = 0;
    const breakdown = [];
    lines.forEach(ln => {
      const ingr = STATE.products.find(p => p.id === ln.ingredient_id);
      const cost = getIngredientCost(ln.ingredient_id, ln.qty_base);
      totalCost += cost;
      breakdown.push({ label: ingr?.name || `#${ln.ingredient_id}`, line_cost: cost });
    });
    if (totalCost === 0) return toast('No stock prices found - receive stock for ingredients first', 'warning');
    const suggestedPrice = totalCost * (1 + markup / 100);
    show(resultEl);
    avgCostEl.textContent   = `R${totalCost.toFixed(4)}`;
    suggestedEl.textContent = `→ R${fmt(suggestedPrice)} at ${markup}% markup`;
    document.getElementById('btn-calc-apply').dataset.price = suggestedPrice.toFixed(2);
    breakdownEl.innerHTML = '';
    breakdown.forEach(l => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="text-muted small">${l.label}</td><td class="text-end small">R${l.line_cost.toFixed(4)}</td>`;
      breakdownEl.appendChild(tr);
    });
    const sEl = document.getElementById('calc-suggestions-row');
    if (sEl) sEl.innerHTML = [20,30,40,50,60,70,100,150,200].map(pct => {
      const p = totalCost * (1 + pct/100);
      return `<button class="btn btn-outline-secondary btn-sm me-1 mb-1" data-apply-price="${p.toFixed(2)}">${pct}% → R${fmt(p)}</button>`;
    }).join('');
    sEl?.querySelectorAll('[data-apply-price]').forEach(btn => {
      btn.addEventListener('click', () => applyCalculatedPrice(parseFloat(btn.dataset.applyPrice)));
    });
    return;
  }

  try {
    const j = await api(`/api/products/${id}/fifo_price?markup=${markup}`);

    if (j.warning) { toast(j.warning, 'warning', 4000); return; }

    show(resultEl);
    avgCostEl.textContent   = `R${j.avg_cost.toFixed(4)}`;
    suggestedEl.textContent = `→ R${fmt(j.suggested_price)} at ${markup}% markup`;
    document.getElementById('btn-calc-apply').dataset.price = j.suggested_price;

    // Breakdown table
    breakdownEl.innerHTML = '';
    j.lines.forEach(l => {
      const tr = document.createElement('tr');
      const detail = l.line_cost != null
        ? `${l.qty_per_sale}${l.base_unit} × R${l.avg_cost_per_unit.toFixed(4)} = R${l.line_cost.toFixed(4)}`
        : `avg R${l.avg_cost_per_unit.toFixed(4)}/${l.base_unit || 'unit'} (${l.total_qty} available)`;
      tr.innerHTML = `<td class="text-muted small">${l.label}</td><td class="text-end small">${detail}</td>`;
      breakdownEl.appendChild(tr);
    });

    // Save the margin on the product immediately
    await api('/api/products/update', {
      method: 'POST',
      body: JSON.stringify({ id, margin_pct: markup })
    });

  } catch (e) { toast(e.message, 'error'); }
});

function applyCalculatedPrice(price) {
  const productType      = document.getElementById('p-type')?.value;
  const unitType         = document.getElementById('p-unit-type')?.value || 'weight';
  const isWeightOrVolume = productType === 'stock_item' && (unitType === 'weight' || unitType === 'volume');
  const el = document.getElementById('p-price');
  if (!el) return;

  if (isWeightOrVolume) {
    const priceUnit    = document.getElementById('p-price-unit')?.value || UNITS[unitType]?.base || 'unit';
    const conv         = UNITS[unitType]?.toBase[priceUnit] || 1;
    const displayPrice = price * conv;
    el.value = displayPrice.toFixed(4);
    toast(`Selling price set to R${displayPrice.toFixed(4)} per ${priceUnit}`, 'success', 2000);
  } else {
    el.value = price.toFixed(2);
    toast(`Selling price set to R${price.toFixed(2)}`, 'success', 2000);
  }
}

document.getElementById('btn-calc-apply')?.addEventListener('click', (e) => {
  const price = parseFloat(e.currentTarget.dataset.price || '0');
  if (price > 0) applyCalculatedPrice(price);
});

// ── Sell Packages ──
function renderPackagesTable() {
  const tbody   = document.getElementById('packages-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  const unitType   = document.getElementById('p-unit-type')?.value || 'weight';
  const baseUnit   = UNITS[unitType]?.base || 'unit';
  const unitOpts   = UNITS[unitType]?.display || [baseUnit];

  _sellPackages.forEach((pkg, idx) => {
    const tr = document.createElement('tr');
    let unitSel = `<select class="form-select form-select-sm" data-pkg-idx="${idx}" data-pkg-field="unit" style="width:auto">`;
    unitOpts.forEach(u => { unitSel += `<option value="${u}" ${u === (pkg.unit || baseUnit) ? 'selected' : ''}>${u}</option>`; });
    unitSel += '</select>';

    tr.innerHTML = `
      <td><input type="text" value="${pkg.name || ''}" class="form-control form-control-sm" data-pkg-idx="${idx}" data-pkg-field="name" placeholder="100g Bag"></td>
      <td><input type="number" step="0.01" value="${pkg.qty_display || pkg.qty_base || ''}" class="form-control form-control-sm" data-pkg-idx="${idx}" data-pkg-field="qty_display" style="width:70px"></td>
      <td>${unitSel}</td>
      <td><div class="input-group input-group-sm"><span class="input-group-text">R</span><input type="number" step="0.01" value="${pkg.price || ''}" class="form-control" data-pkg-idx="${idx}" data-pkg-field="price" style="width:75px"></div></td>
      <td><input type="text" value="${pkg.barcode || ''}" class="form-control form-control-sm" data-pkg-idx="${idx}" data-pkg-field="barcode" placeholder="auto" style="width:100px"></td>
      <td><button class="btn btn-outline-danger btn-sm" data-pkg-remove="${idx}"><i class="bi bi-x-lg"></i></button></td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll('[data-pkg-idx]').forEach(el => {
    el.addEventListener('input', () => {
      const idx   = parseInt(el.dataset.pkgIdx);
      const field = el.dataset.pkgField;
      if (field === 'qty_display') {
        _sellPackages[idx].qty_display = parseFloat(el.value) || 0;
      } else {
        _sellPackages[idx][field] = el.value;
      }
    });
    el.addEventListener('change', () => {
      const idx   = parseInt(el.dataset.pkgIdx);
      const field = el.dataset.pkgField;
      if (field === 'unit') _sellPackages[idx].unit = el.value;
    });
  });
  tbody.querySelectorAll('[data-pkg-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      _sellPackages.splice(parseInt(btn.dataset.pkgRemove), 1);
      renderPackagesTable();
    });
  });
}

document.getElementById('btn-add-package')?.addEventListener('click', () => {
  const unitType = document.getElementById('p-unit-type')?.value || 'weight';
  _sellPackages.push({ name: '', qty_base: 0, qty_display: 0, unit: UNITS[unitType]?.base || 'unit', price: 0, barcode: '' });
  renderPackagesTable();
});

function getSellPackagesForSubmit() {
  const unitType = document.getElementById('p-unit-type')?.value || 'weight';
  return _sellPackages
    .filter(pkg => pkg.name?.trim() && (pkg.qty_display > 0 || pkg.qty_base > 0))
    .map(pkg => {
      const unit     = pkg.unit || UNITS[unitType]?.base || 'unit';
      const qty_base = toBase(parseFloat(pkg.qty_display || pkg.qty_base || 0), unit, unitType);
      return {
        id:       pkg.id || null,
        name:     pkg.name.trim(),
        qty_base,
        price:    parseFloat(pkg.price) || 0,
        barcode:  pkg.barcode?.trim() || '',
      };
    });
}

// ── Multi-image upload helper ──
async function _uploadProductImagesIfSelected(pid) {
  if (!_pendingFiles.length) return;
  const fd = new FormData();
  for (const f of _pendingFiles) fd.append('images[]', f, f._name || 'photo.jpg');
  const res = await fetch(`/api/products/${pid}/images`, {
    method: 'POST', body: fd, credentials: 'same-origin'
  });
  _pendingFiles = [];
  if (res.ok) {
    const data = await res.json().catch(() => ({}));
    if (data.errors?.length) {
      data.errors.forEach(e => toast(`${e.file}: ${e.error}`, 'warning'));
    }
  } else {
    let err = 'Image upload failed';
    try { const j = await res.json(); err = j.error || err; } catch {}
    toast(err, 'warning');
  }
}

// ── Save / Update / Delete ──
document.getElementById('btn-add-product')?.addEventListener('click', async () => {
  const payload = buildProductPayload();
  if (!payload || payload._blocked) return;
  try {
    const result = await api('/api/products', { method: 'POST', body: JSON.stringify(payload) });
    if (result?.id) await _uploadProductImagesIfSelected(result.id);
    await loadProducts();
    loadIngredients();
    toast('Product added');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
    // If opened from a purchase run line, auto-select the new product in that line
    if (_pendingPurchaseLine && result?.id) {
      const supplierProductIds = new Set((_currentSupplierProducts || []).map(p => p.id));
      supplierProductIds.add(result.id); // new product belongs to this supplier run
      const sel = _pendingPurchaseLine.querySelector('[data-product-select]');
      if (sel) {
        sel.innerHTML = _buildProductOptions(supplierProductIds, false);
        sel.value = result.id;
        sel.dispatchEvent(new Event('change'));
      }
      _pendingPurchaseLine = null;
    }
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-update-product')?.addEventListener('click', async () => {
  const id = parseInt(document.getElementById('p-id').value || '0', 10);
  if (!id) return toast('No product selected', 'warning');
  const payload = buildProductPayload();
  if (!payload || payload._blocked) return;
  payload.id = id;

  // Detect is_for_sale change for a meaningful toast
  const prev = STATE.products.find(p => p.id === id);
  const wasForSale = prev?.is_for_sale !== false;
  const nowForSale = payload.is_for_sale !== false;

  try {
    await api('/api/products/update', { method: 'POST', body: JSON.stringify(payload) });
    await _uploadProductImagesIfSelected(id);
    await loadProducts();
    loadIngredients();
    let msg = 'Product updated.';
    if (wasForSale && !nowForSale)  msg = `"${payload.name}" moved to the Ingredients tab.`;
    if (!wasForSale && nowForSale)  msg = `"${payload.name}" moved back to For Sale.`;
    toast(msg, 'success', 2500);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-delete-product')?.addEventListener('click', async () => {
  const name = document.getElementById('p-name').value.trim();
  const id   = parseInt(document.getElementById('p-id').value || '0', 10);
  if (!name) return toast('No product name', 'warning');
  if (!confirm(`Delete "${name}"? This cannot be undone.`)) return;
  try {
    await api(`/api/products/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await loadProducts();
    loadIngredients();
    toast('Product deleted');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
  } catch (e) {
    // Deletion blocked - offer to hide instead
    if (e.message.includes('historical references')) {
      if (confirm(`"${name}" has historical records and cannot be deleted.\n\nHide it instead? (It will no longer appear at the till or in active lists, but history is preserved.)`)) {
        try {
          await api('/api/products/update', { method: 'POST', body: JSON.stringify({ id, is_for_sale: false }) });
          await loadProducts();
          loadIngredients();
          toast(`"${name}" hidden`, 'warning', 2500);
          bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
        } catch (e2) { toast(e2.message, 'error'); }
      }
    } else {
      toast(e.message, 'error');
    }
  }
});

function buildProductPayload() {
  const type         = document.getElementById('p-type').value;
  const name         = document.getElementById('p-name').value.trim();
  const isNew        = !document.getElementById('p-id')?.value;
  const bcInput      = document.getElementById('p-barcode');
  // For new products: only send barcode if user manually typed one (not auto-preview)
  // Server always derives barcode from product_code for new products
  const barcode      = (isNew && bcInput?.dataset.autoGenerated !== '0')
                       ? null
                       : (bcInput?.value.trim() || null);
  const priceVal     = document.getElementById('p-price').value;
  const price        = priceVal !== '' ? parseFloat(priceVal) : null;
  const stock_qty    = parseInt(document.getElementById('p-stock').value || '0', 10);
  const unitType     = document.getElementById('p-unit-type').value;
  const lowStock     = document.getElementById('p-low-stock').value || null;
  const pkgSize      = document.getElementById('p-pkg-size').value || null;
  const pkgSizeUnit  = document.getElementById('p-pkg-size-unit')?.value || null;
  const pkgUnit      = document.getElementById('p-pkg-unit').value?.trim() || null;
  const isForSale          = document.getElementById('p-is-for-sale').checked;
  const isAvailableOnline  = document.getElementById('p-is-available-online')?.checked || false;
  // sold_by_weight is now auto-derived from unit type (hidden input set by updateProductTypeSections)
  const soldByWeight = document.getElementById('p-sold-by-weight')?.value === '1';

  if (!name) { toast('Product name required', 'warning'); return null; }

  // For weight/volume stock items: convert price from chosen display unit to per-base-unit
  let finalPrice        = price;
  let finalPricePerUnit = null;
  if (type === 'stock_item' && soldByWeight) {
    finalPrice = null;  // no fixed selling price
    // Convert: user entered R5/kg → store as R0.005/g
    const priceUnit = document.getElementById('p-price-unit')?.value || UNITS[unitType]?.base || unitType;
    const conv = UNITS[unitType]?.toBase[priceUnit] || 1;
    finalPricePerUnit = price != null ? price / conv : null;
  }

  // Scale fields
  const scaleTareRaw = document.getElementById('p-scale-tare')?.value;
  const scaleShelfRaw = document.getElementById('p-scale-shelf-life')?.value;
  const scaleMsg1Raw = document.getElementById('p-scale-msg1')?.value?.trim() || null;
  const scaleMsg2Raw = document.getElementById('p-scale-msg2')?.value?.trim() || null;

  return {
    name, barcode,
    price:       finalPrice,
    product_type: type,
    unit_type:    unitType,
    is_for_sale:         isForSale,
    is_available_online: isAvailableOnline,
    sold_by_weight: soldByWeight,
    price_per_unit: finalPricePerUnit,
    low_stock_threshold: lowStock ? (() => {
      const ut  = document.getElementById('p-unit-type')?.value || 'weight';
      const lu  = document.getElementById('p-low-stock-unit')?.value || UNITS[ut]?.base || 'g';
      return toBase(parseFloat(lowStock), lu, ut);
    })() : null,
    package_size:      pkgSize ? parseFloat(pkgSize) : null,
    package_size_unit: pkgSizeUnit,
    package_unit:      pkgUnit,
    margin_pct:    document.getElementById('calc-markup')?.value ? parseFloat(document.getElementById('calc-markup').value) : null,
    is_prepared:   document.getElementById('p-is-prepared')?.checked || false,
    description:   document.getElementById('p-description')?.value?.trim() || null,
    // Category name - backend resolves/auto-creates; '' clears the category
    category:      document.getElementById('p-category')?.value?.trim() || '',
    recipe_lines:  type === 'recipe'     ? getRecipeLinesForSubmit()  : [],
    sell_packages: type === 'stock_item' ? getSellPackagesForSubmit() : [],
    // Block save if PLU conflict
    ...((() => {
      const conflictEl = document.getElementById('p-plu-conflict');
      if (conflictEl && !conflictEl.classList.contains('hidden')) {
        toast(conflictEl.textContent, 'danger'); return { _blocked: true };
      }
      return {};
    })()),
    // PLU (product_code) - only send if explicitly set
    product_code: (() => { const v = parseInt(document.getElementById('p-product-code')?.value || '0', 10); return v > 0 ? v : undefined; })(),
    // Scale settings
    sync_to_scale:     document.getElementById('p-sync-to-scale')?.checked || false,
    scale_tare:        scaleTareRaw ? parseFloat(scaleTareRaw) : null,
    scale_shelf_life:  scaleShelfRaw ? parseInt(scaleShelfRaw) : null,
    scale_msg1:        scaleMsg1Raw || null,
    scale_msg2:        scaleMsg2Raw || null,
    scale_open_price:  document.getElementById('p-scale-open-price')?.checked || false,
    scale_prohibit:    document.getElementById('p-scale-prohibit')?.checked || false,
    // Batch-produce (recipe only)
    ...(type === 'recipe' ? {
      is_produced:  document.getElementById('p-is-produced')?.checked || false,
      batch_size:  parseInt(document.getElementById('p-batch-size')?.value || '1', 10) || 1,
      stock_unit:  document.getElementById('p-stock-unit')?.value.trim() || null,
    } : {}),
  };
}

// ── Legacy purchase (simple products) ──
async function loadPurchaseHistory(pid) {
  const host = document.getElementById('purchase-history-list');
  if (!host) return;
  try {
    const rows = await api(`/api/purchases?product_id=${pid}`);
    if (!rows.length) { host.innerHTML = '<div class="text-muted small">No purchase history.</div>'; return; }
    host.innerHTML = `
      <div class="small fw-bold text-muted mb-1">Purchase history</div>
      <table class="table table-sm" style="font-size:12px">
        <thead class="table-light"><tr><th>Date</th><th class="text-end">Qty</th><th class="text-end">Price/unit</th><th></th></tr></thead>
        <tbody>${rows.map(r => `
          <tr>
            <td>${new Date(r.date_time).toLocaleDateString('en-ZA')}</td>
            <td class="text-end">${r.qty_added}</td>
            <td class="text-end">R${fmt(r.purchase_price)}</td>
            <td class="text-end"><button class="btn btn-outline-danger btn-sm py-0 px-1"
              data-delete-purchase-id="${r.id}"
              data-delete-purchase-qty="${r.qty_added}"
              data-delete-purchase-pid="${pid}"
              title="Delete this purchase"><i class="bi bi-trash"></i></button></td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  } catch (e) { host.innerHTML = ''; }
}

document.getElementById('btn-add-purchase')?.addEventListener('click', async () => {
  const pid   = parseInt(document.getElementById('p-id').value || '0', 10);
  const qty   = parseInt(document.getElementById('pur-qty').value || '0', 10);
  const price = parseFloat(document.getElementById('pur-price').value || '0');
  try {
    await api('/api/purchases', { method: 'POST', body: JSON.stringify({ product_id: pid, qty_added: qty, purchase_price: price }) });
    await loadProducts();
    await loadPurchaseHistory(pid);
    toast('Purchase recorded, stock updated');
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('productEditorModal')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-delete-purchase-id]');
  if (!btn) return;
  const purId = btn.dataset.deletePurchaseId;
  const qty   = btn.dataset.deletePurchaseQty;
  const pid   = btn.dataset.deletePurchasePid;
  if (!confirm(`Delete purchase of ${qty} units? This will subtract ${qty} from stock.`)) return;
  try {
    const res = await api(`/api/purchases/${purId}`, { method: 'DELETE' });
    const stockEl = document.getElementById('p-stock');
    if (stockEl && res.new_stock_qty != null) stockEl.value = res.new_stock_qty;
    await loadProducts();
    await loadPurchaseHistory(parseInt(pid));
    toast('Purchase deleted, stock updated', 'success', 2500);
  } catch (err) { toast(err.message || 'Delete failed', 'danger'); }
});

document.getElementById('btn-suggest-price')?.addEventListener('click', async () => {
  const pid = parseInt(document.getElementById('p-id').value || '0', 10);
  try {
    const j = await api(`/api/products/${pid}/suggested_price?markup=${_globalMarkupPct}`);
    const out = document.getElementById('suggest-output');
    if (out) out.textContent = `WAC R${j.wac.toFixed(4)} + ${j.markup_percent}% → R${j.suggested_price}`;
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// STOCK TAB
// ═══════════════════════════════════════════════════════
async function loadIngredients() {
  if (!isAdmin()) return;
  try {
    const data = await api('/api/stock/ingredients');
    STATE._stockCostMap      = {};
    STATE._stockItems        = {};
    STATE._productSupplierMap = {};
    data.forEach(item => {
      STATE._stockItems[item.id] = item;
      const oldestWithStock = item.batches
        .slice()
        .sort((a, b) => new Date(a.purchased_at) - new Date(b.purchased_at))
        .find(b => b.qty_remaining_base > 0);
      if (oldestWithStock) STATE._stockCostMap[item.id] = oldestWithStock.cost_per_base_unit;
      // Build supplier name index for teller search
      const names = [...new Set(item.batches.map(b => b.supplier_name).filter(Boolean))];
      if (names.length) STATE._productSupplierMap[item.id] = names.join(' ').toLowerCase();
    });
    // Refresh any already-rendered product cards so stock levels update
    renderProductsCards();
    // Prune any selected batches that no longer exist in the data
    const allBatchIds = new Set(data.flatMap(item => item.batches.map(b => b.id)));
    STATE._selectedBatchData = STATE._selectedBatchData.filter(b => allBatchIds.has(b.id));
    _refreshBatchSelectionBar();
  } catch (e) { console.error('loadIngredients', e); }
}

// Build the expandable stock body for a stock_item product.
// item  = object from /api/stock/ingredients (.batches, .sell_packages, .unit_type, etc.)
// prod  = product object from STATE.products (optional, used for sale price)
function _buildStockBody(item, prod) {
  const wrap = document.createElement('div');

  if (item.batches && item.batches.length > 0) {
    // Rich batch table
    const table = document.createElement('table');
    table.className = 'table table-sm table-hover mb-2';
    table.style.fontSize = '12px';
    const hasCustomOrder = item.batches.some(b => b.sort_order != null);
    const productId = item.id;

    // Header: show reset button if any batch has a custom order
    const tableHeader = document.createElement('div');
    tableHeader.className = 'd-flex align-items-center justify-content-between mb-1';
    tableHeader.innerHTML = `
      <span class="small fw-bold text-muted">Stock batches${hasCustomOrder ? ' <span class="badge bg-warning text-dark ms-1" style="font-size:10px"><i class="bi bi-arrow-down-up"></i> Custom order</span>' : ''}</span>
      ${hasCustomOrder ? `<button class="btn btn-outline-secondary btn-sm py-0 px-2" style="font-size:11px" data-reset-batch-order="${productId}"><i class="bi bi-clock-history"></i> Reset to FIFO</button>` : ''}
    `;
    wrap.appendChild(tableHeader);

    table.innerHTML = `
      <thead class="table-light">
        <tr>
          <th style="width:20px"><input type="checkbox" class="batch-select-all" title="Select all batches for this product"></th>
          <th style="width:28px"></th>
          <th>Date</th>
          <th>Supplier</th>
          <th class="text-end">Bought</th>
          <th class="text-end">Left</th>
          <th class="text-end">Stock Value</th>
          <th class="text-end">COGS/unit</th>
          <th class="text-end">Sale/unit</th>
          <th></th>
        </tr>
      </thead>
      <tbody></tbody>`;
    const tbody = table.querySelector('tbody');

    item.batches.forEach((b, idx) => {
      const _qtyLabel = (n) => item.stock_unit ? `${n} ${item.stock_unit}${n !== 1 ? 's' : ''}` : displayQty(n, item.unit_type);
      const remaining  = _qtyLabel(b.qty_remaining_base);
      const purchased  = _qtyLabel(b.qty_purchased_base);
      const date       = new Date(b.purchased_at).toLocaleDateString('en-ZA');
      const supplier   = b.supplier_name || '-';
      const stockValue = (b.cost_per_base_unit * b.qty_remaining_base);
      const totalCost  = (b.cost_per_base_unit * b.qty_purchased_base).toFixed(2);
      const { cost: costPerDisplay, unit: displayUnit } = displayCost(b.cost_per_base_unit, b.qty_remaining_base, item.unit_type);
      const cogsStr    = `R${costPerDisplay < 0.01 ? costPerDisplay.toFixed(4) : costPerDisplay.toFixed(2)}/${displayUnit}`;

      // Sale value per display unit
      let saleStr = '-';
      if (prod) {
        if (prod.sold_by_weight && prod.price_per_unit != null) {
          const { cost: salePer, unit: saleUnit } = displayCost(parseFloat(prod.price_per_unit), b.qty_remaining_base, item.unit_type);
          saleStr = `R${fmt(salePer)}/${saleUnit}`;
        } else if (prod.price != null) {
          saleStr = `R${fmt(prod.price)}`;
        }
      }

      const isFirst  = idx === 0;
      const isLast   = idx === item.batches.length - 1;
      const rowStyle = b.sort_order != null ? ' style="background:rgba(255,193,7,0.08)"' : '';

      // Overhead breakdown for display
      const bAddlData = b.additional_costs ? (() => { try { return JSON.parse(b.additional_costs); } catch { return []; } })() : [];
      const bHasAddl  = bAddlData.length > 0;
      const bAddlHtml = bHasAddl ? (() => {
        const addlTot = bAddlData.reduce((s, c) => s + parseFloat(c.amount || 0), 0);
        const pct = b.base_cost_total > 0 ? Math.round((addlTot / b.base_cost_total) * 100) : 0;
        const tags = bAddlData.map(c => `<span class="badge bg-secondary" style="font-size:10px">${_COST_TYPE_LABELS[c.type] || c.type} R${parseFloat(c.amount).toFixed(2)}</span>`).join(' ');
        return `<div class="text-muted" style="font-size:11px;margin-top:2px">${tags} <span class="text-warning">(+${pct}% overhead)</span></div>`;
      })() : '';

      const alreadySel = STATE._selectedBatchData.some(x => x.id === b.id);
      const tr = document.createElement('tr');
      tr.setAttribute('data-batch-row-id', b.id);
      tr.innerHTML = `
        <td class="pe-0">
          <input type="checkbox" class="batch-select-chk" title="Select for cost apply"
            data-batch-id="${b.id}"
            data-product-name="${escapeHtml(item.name)}"
            data-supplier-id="${b.supplier_id || ''}"
            data-supplier-name="${escapeHtml(b.supplier_name || '')}"
            data-base-cost-total="${b.base_cost_total || 0}"
            data-consumed-pct="${b.consumed_pct || 0}"
            data-updated-at="${b.updated_at || ''}"
            data-addl-costs="${escapeHtml(b.additional_costs || '')}"
            ${alreadySel ? 'checked' : ''}>
        </td>
        <td class="pe-0">
          <div class="d-flex flex-column gap-0" style="line-height:1">
            <button class="btn btn-link btn-sm p-0 lh-1 text-muted" title="Move up" style="font-size:13px"
              data-reorder-batch="${b.id}" data-reorder-action="move_up" ${isFirst ? 'disabled' : ''}><i class="bi bi-chevron-up"></i></button>
            <button class="btn btn-link btn-sm p-0 lh-1 text-muted" title="Move down" style="font-size:13px"
              data-reorder-batch="${b.id}" data-reorder-action="move_down" ${isLast ? 'disabled' : ''}><i class="bi bi-chevron-down"></i></button>
          </div>
        </td>
        <td${rowStyle}>${date}${b.sort_order != null && isFirst ? ' <i class="bi bi-arrow-up-circle-fill text-warning" title="Used next"></i>' : ''}</td>
        <td${rowStyle}>${b.supplier_name ? `<span class="badge bg-info text-dark">${supplier}</span>` : `<span class="text-muted">${supplier}</span>`}</td>
        <td class="text-end"${rowStyle}>${purchased}</td>
        <td class="text-end"${rowStyle}><strong>${remaining}</strong></td>
        <td class="text-end text-muted"${rowStyle}>R${fmt(stockValue)}</td>
        <td class="text-end text-muted"${rowStyle}>${cogsStr}${bAddlHtml}</td>
        <td class="text-end text-success"${rowStyle}>${saleStr}</td>
        <td class="text-end"${rowStyle}>
          <div class="d-flex gap-1 justify-content-end">
            ${!isFirst ? `<button class="btn btn-outline-warning btn-sm py-0 px-1" title="Use this batch next"
              data-reorder-batch="${b.id}" data-reorder-action="use_next"><i class="bi bi-arrow-bar-up"></i></button>` : ''}
            <button class="btn btn-outline-secondary btn-sm py-0 px-1"
              data-edit-batch-id="${b.id}"
              data-edit-batch-date="${b.purchased_at.slice(0,10)}"
              data-edit-batch-supplier="${b.supplier_id || ''}"
              data-edit-batch-total="${totalCost}"
              data-edit-batch-qty-base="${b.qty_purchased_base}"
              data-edit-batch-qty-remaining="${b.qty_remaining_base}"
              data-edit-batch-unit="${item.unit_type}"
              data-edit-batch-base-unit="${item.base_unit || 'g'}"
              data-edit-batch-package-size="${item.package_size || ''}"
              data-edit-batch-package-unit="${item.package_unit || ''}"
              data-edit-batch-addl-costs="${escapeHtml(b.additional_costs || '')}"
              data-edit-batch-base-cost-total="${b.base_cost_total || ''}"
              data-edit-batch-consumed-pct="${b.consumed_pct || 0}"
              data-edit-batch-updated-at="${b.updated_at || ''}"><i class="bi bi-pencil"></i></button>
            ${Math.abs(b.qty_remaining_base - b.qty_purchased_base) < 0.0001 ? `<button class="btn btn-outline-danger btn-sm py-0 px-1" title="Delete batch"
              data-delete-batch-id="${b.id}"
              data-delete-batch-date="${new Date(b.purchased_at).toLocaleDateString('en-ZA')}"
              data-delete-batch-product="${item.name}"><i class="bi bi-trash"></i></button>` : ''}
          </div>
        </td>`;
      tbody.appendChild(tr);
    });

    wrap.appendChild(table);

    // Wire select-all checkbox for this product's batch table
    const selectAll = table.querySelector('.batch-select-all');
    if (selectAll) {
      selectAll.addEventListener('change', () => {
        table.querySelectorAll('.batch-select-chk').forEach(chk => {
          chk.checked = selectAll.checked;
          _updateBatchSelection(chk);
        });
        _refreshBatchSelectionBar();
      });
    }
    table.querySelectorAll('.batch-select-chk').forEach(chk => {
      chk.addEventListener('change', () => {
        _updateBatchSelection(chk);
        _refreshBatchSelectionBar();
      });
    });

  } else {
    wrap.innerHTML = '<div class="small text-muted pb-2">No stock received yet.</div>';
  }

  if (item.sell_packages?.length > 0) {
    const pkgDiv = document.createElement('div');
    pkgDiv.innerHTML = `<div class="small fw-bold mb-1 text-muted">Packages:</div>`;
    item.sell_packages.forEach(pkg => {
      pkgDiv.innerHTML += `<div class="small">• ${pkg.name} - ${displayQty(pkg.qty_base, item.unit_type)} @ R${fmt(pkg.price || 0)}</div>`;
    });
    wrap.appendChild(pkgDiv);
  }
  return wrap;
}

// ── Batch multi-selection + Apply Costs ──────────────────────────────────────

function _updateBatchSelection(chk) {
  const id = parseInt(chk.dataset.batchId);
  if (chk.checked) {
    if (!STATE._selectedBatchData.some(x => x.id === id)) {
      STATE._selectedBatchData.push({
        id,
        product_name:    chk.dataset.productName    || '',
        supplier_id:     chk.dataset.supplierId     ? parseInt(chk.dataset.supplierId) : null,
        supplier_name:   chk.dataset.supplierName   || '',
        base_cost_total: parseFloat(chk.dataset.baseCostTotal || 0),
        consumed_pct:    parseFloat(chk.dataset.consumedPct   || 0),
        updated_at:      chk.dataset.updatedAt      || null,
        additional_costs: chk.dataset.addlCosts     || null,
      });
    }
  } else {
    STATE._selectedBatchData = STATE._selectedBatchData.filter(x => x.id !== id);
  }
}

function _refreshBatchSelectionBar() {
  const bar      = document.getElementById('batch-selection-bar');
  const countEl  = document.getElementById('batch-sel-count');
  const supEl    = document.getElementById('batch-sel-suppliers');
  if (!bar) return;
  const n = STATE._selectedBatchData.length;
  if (n === 0) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  if (countEl) countEl.textContent = n;
  if (supEl) {
    const groups = _groupBatchesBySupplier(STATE._selectedBatchData);
    const parts = groups.map(g => `${g.supplier_name} (${g.batches.length})`);
    supEl.textContent = parts.join(' · ');
  }
}

document.getElementById('btn-clear-batch-sel')?.addEventListener('click', () => {
  STATE._selectedBatchData = [];
  document.querySelectorAll('.batch-select-chk').forEach(c => c.checked = false);
  document.querySelectorAll('.batch-select-all').forEach(c => c.checked = false);
  _refreshBatchSelectionBar();
});

function _groupBatchesBySupplier(batches) {
  const map = new Map();
  batches.forEach(b => {
    const key  = b.supplier_id || '__none__';
    const name = b.supplier_name || 'No Supplier';
    if (!map.has(key)) map.set(key, { supplier_id: b.supplier_id || null, supplier_name: name, batches: [] });
    map.get(key).batches.push(b);
  });
  // Sort: named suppliers first, then "No Supplier"
  return [...map.values()].sort((a, b) => {
    if (!a.supplier_id && b.supplier_id) return 1;
    if (a.supplier_id && !b.supplier_id) return -1;
    return a.supplier_name.localeCompare(b.supplier_name);
  });
}

document.getElementById('btn-apply-costs-to-batches')?.addEventListener('click', () => {
  _openApplyBatchCostsModal(STATE._selectedBatchData);
});

function _openApplyBatchCostsModal(batches) {
  if (!batches.length) return toast('Select at least one batch first', 'warning');
  const groups = _groupBatchesBySupplier(batches);
  const modal  = bootstrap.Modal.getOrCreateInstance(document.getElementById('applyBatchCostsModal'));
  const body   = document.getElementById('apply-batch-costs-body');

  let html = '';
  groups.forEach((g, gi) => {
    const groupBase = g.batches.reduce((s, b) => s + (b.base_cost_total || 0), 0);
    const hasNullBase = g.batches.some(b => !b.base_cost_total);
    const hasConsumed = g.batches.some(b => b.consumed_pct > 0);

    html += `<div class="border rounded mb-3 p-2" data-cost-group="${gi}">
      <div class="d-flex align-items-center mb-2">
        <span class="fw-semibold small"><i class="bi bi-truck me-1 text-primary"></i>${escapeHtml(g.supplier_name)}</span>
        <span class="ms-2 text-muted small">${g.batches.length} batch(es) · base value R${fmt(groupBase)}</span>
      </div>`;

    // Batch list with consumed warnings + existing overhead
    g.batches.forEach(b => {
      const existingAddl  = b.additional_costs
        ? (() => { try { return JSON.parse(b.additional_costs); } catch { return []; } })()
        : [];
      const existingTotal = existingAddl.reduce((s, c) => s + parseFloat(c.amount || 0), 0);
      const existingHtml  = existingAddl.length
        ? `<span class="badge bg-secondary ms-1" style="font-size:10px" title="${existingAddl.map(c => c.label + ' R' + c.amount).join(', ')}">already R${existingTotal.toFixed(2)} overhead</span>`
        : '';
      const consumedWarn  = b.consumed_pct > 0
        ? `<span class="badge bg-warning text-dark ms-1" style="font-size:10px" title="Previous sales NOT affected">${Math.round(b.consumed_pct)}% consumed</span>`
        : '';
      html += `<div class="d-flex align-items-center gap-2 mb-1 ps-1" style="font-size:12px">
        <i class="bi bi-box-seam text-muted"></i>
        <span>${escapeHtml(b.product_name)}${consumedWarn}${existingHtml}</span>
        <span class="ms-auto text-muted">base R${fmt(b.base_cost_total || 0)}</span>
        <span class="badge bg-secondary" data-group-alloc="${gi}-${b.id}" style="min-width:60px;text-align:right">—</span>
      </div>`;
    });

    if (hasNullBase) {
      html += `<div class="alert alert-info py-1 small mb-2">
        <i class="bi bi-info-circle me-1"></i>
        Some batches have no base cost recorded (received before this feature). Costs will be split equally across those batches.
      </div>`;
    }
    if (hasConsumed) {
      html += `<div class="alert alert-warning py-1 small mb-2">
        <i class="bi bi-exclamation-triangle-fill me-1"></i>
        Some batches are partially consumed. Costs apply to remaining stock only.
        <strong>Previous sales and historical profit reports will NOT change.</strong>
      </div>`;
    }

    // Per-group cost block
    html += `<div id="group-costs-wrap-${gi}" class="mt-2"></div>
      <div class="mt-1 text-muted" style="font-size:11px" id="group-total-${gi}"></div>
    </div>`;
  });

  body.innerHTML = `<div class="alert alert-light py-2 mb-2 d-flex align-items-center gap-3" style="position:sticky;top:0;z-index:10;border-bottom:1px solid #dee2e6">
    <span class="small fw-semibold">Session total allocated:</span>
    <span class="fw-bold" id="apply-costs-grand-total">R0.00</span>
  </div>` + html;

  // Render an addl costs block per group
  groups.forEach((g, gi) => {
    _renderAdditionalCostsBlock(document.getElementById(`group-costs-wrap-${gi}`), []);
    // Wire live preview
    const wrap = document.getElementById(`group-costs-wrap-${gi}`);
    wrap?.addEventListener('input', () => _updateGroupAllocPreview(g, gi));
  });
  document.getElementById('apply-batch-costs-status').textContent = '';
  modal.show();
}

function _updateGroupAllocPreview(group, gi) {
  const wrap     = document.getElementById(`group-costs-wrap-${gi}`);
  const totEl    = document.getElementById(`group-total-${gi}`);
  if (!wrap) return;
  const addlCosts = _readAdditionalCosts(wrap);
  const addlTotal = _addlCostsTotal(addlCosts);
  if (totEl) totEl.textContent = addlTotal ? `Total overhead for this group: R${addlTotal.toFixed(2)}` : '';

  // Compute proportional shares and update allocation badges
  const groupBase = group.batches.reduce((s, b) => s + (b.base_cost_total || 0), 0);
  const linePrices = group.batches.map(b => b.base_cost_total || 0);
  const shares = (() => {
    if (!addlTotal) return linePrices.map(() => 0);
    if (groupBase <= 0) return linePrices.map(() => addlTotal / group.batches.length);
    const raw = linePrices.map(p => parseFloat(((p / groupBase) * addlTotal).toFixed(2)));
    const diff = parseFloat((addlTotal - raw.reduce((s, x) => s + x, 0)).toFixed(2));
    raw[raw.length - 1] = parseFloat((raw[raw.length - 1] + diff).toFixed(2));
    return raw;
  })();

  group.batches.forEach((b, bi) => {
    const badge = document.querySelector(`[data-group-alloc="${gi}-${b.id}"]`);
    if (badge) badge.textContent = shares[bi] ? `+R${shares[bi].toFixed(2)}` : '—';
  });

  // Update grand total across all groups
  const allGroups = _groupBatchesBySupplier(STATE._selectedBatchData);
  let grandTotal = 0;
  allGroups.forEach((grp, idx) => {
    const w = document.getElementById(`group-costs-wrap-${idx}`);
    if (w) grandTotal += _addlCostsTotal(_readAdditionalCosts(w));
  });
  const grandEl = document.getElementById('apply-costs-grand-total');
  if (grandEl) grandEl.textContent = `R${grandTotal.toFixed(2)}`;
}

document.getElementById('btn-apply-batch-costs-confirm')?.addEventListener('click', async () => {
  const groups  = _groupBatchesBySupplier(STATE._selectedBatchData);
  const statusEl = document.getElementById('apply-batch-costs-status');
  const btn      = document.getElementById('btn-apply-batch-costs-confirm');

  // Validate all groups have at least one cost
  let hasAnyCost = false;
  groups.forEach((g, gi) => {
    const wrap = document.getElementById(`group-costs-wrap-${gi}`);
    if (wrap && _readAdditionalCosts(wrap).length) hasAnyCost = true;
  });
  if (!hasAnyCost) return toast('Add at least one cost to a group', 'warning');

  btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Applying…';

  let totalApplied = 0, errors = [];

  for (let gi = 0; gi < groups.length; gi++) {
    const g    = groups[gi];
    const wrap = document.getElementById(`group-costs-wrap-${gi}`);
    const addlCosts = wrap ? _readAdditionalCosts(wrap) : [];
    if (!addlCosts.length) continue;  // skip groups with no costs entered

    const batchIds = g.batches.map(b => b.id);
    try {
      await api('/api/stock/batches/apply-costs', {
        method: 'POST',
        body: JSON.stringify({ batch_ids: batchIds, additional_costs: addlCosts }),
      });
      totalApplied += batchIds.length;
    } catch (e) {
      errors.push(`${g.supplier_name}: ${e.message}`);
    }
  }

  btn.disabled = false;
  if (errors.length) {
    toast(`Errors: ${errors.join('; ')}`, 'error', 7000);
    if (statusEl) statusEl.textContent = '';
  } else {
    bootstrap.Modal.getOrCreateInstance(document.getElementById('applyBatchCostsModal')).hide();
    toast(`Costs applied to ${totalApplied} batch(es)`, 'success', 3000);
    STATE._selectedBatchData = [];
    _refreshBatchSelectionBar();
    await loadIngredients();
  }
});

function renderStockList(items) {
  // kept for backward compat - no longer used for display, data goes via STATE._stockItems
}

// ── Batch reorder (delegated off products-card-list) ──
document.getElementById('products-card-list')?.addEventListener('click', async (e) => {
  // Move-up / move-down / use-next buttons
  const reorderBtn = e.target.closest('[data-reorder-batch]');
  if (reorderBtn) {
    e.stopPropagation();
    const batchId = reorderBtn.dataset.reorderBatch;
    const action  = reorderBtn.dataset.reorderAction;
    const pid     = reorderBtn.closest('.product-row')?.dataset.productId;
    try {
      await api(`/api/stock/batches/${batchId}/reorder`, { method: 'POST', body: JSON.stringify({ action }) });
      await loadIngredients();
      if (pid) document.querySelector(`.product-row[data-product-id="${pid}"]`)?.click();
    } catch (err) { toast(err.message || 'Reorder failed', 'danger'); }
    return;
  }
  // Reset to FIFO button
  const resetBtn = e.target.closest('[data-reset-batch-order]');
  if (resetBtn) {
    e.stopPropagation();
    const pid = resetBtn.dataset.resetBatchOrder;
    try {
      await api(`/api/stock/products/${pid}/reset_batch_order`, { method: 'POST' });
      await loadIngredients();
      document.querySelector(`.product-row[data-product-id="${pid}"]`)?.click();
    } catch (err) { toast(err.message || 'Reset failed', 'danger'); }
    return;
  }
});

// ── Edit Batch (delegated off products-card-list since batch rows are dynamic) ──
function _updateEditBatchPreview() {
  const qtyEl    = document.getElementById('edit-batch-qty-purchased');
  const unitSel  = document.getElementById('edit-batch-unit');
  const baseDisp = document.getElementById('edit-batch-qty-base-display');
  if (!qtyEl || !unitSel || !baseDisp) return;
  const qty  = parseFloat(qtyEl.value) || 0;
  const conv = parseFloat(unitSel.options[unitSel.selectedIndex]?.dataset?.conv || '1');
  const unitType = unitSel.dataset.unitType || 'unit';
  baseDisp.textContent = qty > 0 ? `= ${displayQty(qty * conv, unitType)}` : '';
}

document.getElementById('edit-batch-qty-purchased')?.addEventListener('input', _updateEditBatchPreview);
document.getElementById('edit-batch-unit')?.addEventListener('change', _updateEditBatchPreview);

document.getElementById('products-card-list')?.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-edit-batch-id]');
  if (!btn) return;
  e.stopPropagation();
  const batchId      = btn.dataset.editBatchId;
  const date         = btn.dataset.editBatchDate;
  const supplierId   = btn.dataset.editBatchSupplier;
  const total        = btn.dataset.editBatchTotal;
  const qtyBase      = parseFloat(btn.dataset.editBatchQtyBase || '0');
  const qtyRemaining = parseFloat(btn.dataset.editBatchQtyRemaining || '0');
  const unitType     = btn.dataset.editBatchUnit || 'unit';
  const baseUnit     = btn.dataset.editBatchBaseUnit || 'g';
  const packageSize  = btn.dataset.editBatchPackageSize || '';
  const packageUnit  = btn.dataset.editBatchPackageUnit || '';
  const addlCostsJson   = btn.dataset.editBatchAddlCosts   || '[]';
  const consumedPct     = parseFloat(btn.dataset.editBatchConsumedPct || '0');
  const editBatchUpdAt  = btn.dataset.editBatchUpdatedAt   || '';

  // Build unit dropdown (same options as receive modal)
  const unitSel = document.getElementById('edit-batch-unit');
  unitSel.innerHTML = '';
  unitSel.dataset.unitType = unitType;
  const opts = buildUnitOptions(unitType, packageSize ? parseFloat(packageSize) : null, packageUnit || null);
  opts.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value; opt.textContent = o.label; opt.dataset.conv = o.conv;
    unitSel.appendChild(opt);
  });

  // Pre-select the "big" unit (kg/L) and show qty in that unit
  const bigUnit = unitType === 'weight' ? 'kg' : unitType === 'volume' ? 'L' : baseUnit;
  const bigOpt  = [...unitSel.options].find(o => o.value === bigUnit);
  if (bigOpt) { bigOpt.selected = true; }
  const conv       = parseFloat(unitSel.options[unitSel.selectedIndex]?.dataset?.conv || '1');
  const qtyDisplay = conv > 1 ? +(qtyBase / conv).toFixed(6) : qtyBase;

  document.getElementById('edit-batch-id').value            = batchId;
  document.getElementById('edit-batch-date').value          = date;
  document.getElementById('edit-batch-total-price').value   = total;
  document.getElementById('edit-batch-qty-purchased').value = qtyDisplay;
  document.getElementById('edit-batch-qty-purchased').dataset.qtyRemaining = qtyRemaining;
  document.getElementById('edit-batch-qty-base-display').textContent = '';

  const supSel = document.getElementById('edit-batch-supplier');
  supSel.innerHTML = '<option value="">- No supplier -</option>';
  (_suppliers || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id; opt.textContent = s.name;
    if (String(s.id) === String(supplierId)) opt.selected = true;
    supSel.appendChild(opt);
  });

  // Optimistic lock timestamp and reason
  document.getElementById('edit-batch-updated-at').value = editBatchUpdAt;
  const ebReasonEl = document.getElementById('edit-batch-reason');
  if (ebReasonEl) ebReasonEl.value = '';

  // Consumed warning
  const ebWarnEl = document.getElementById('edit-batch-consumed-warning');
  if (ebWarnEl) {
    if (consumedPct > 0) {
      ebWarnEl.innerHTML = `<i class="bi bi-exclamation-triangle-fill me-1"></i><strong>${Math.round(consumedPct)}% consumed.</strong> Adding costs now updates only the remaining ${Math.round(100 - consumedPct)}%. <strong>Previous sales and historical profit reports will NOT change.</strong>`;
      show(ebWarnEl);
    } else {
      hide(ebWarnEl);
    }
  }

  // Populate additional costs block
  let existingAddl = [];
  try { existingAddl = JSON.parse(addlCostsJson) || []; } catch {}
  _renderAdditionalCostsBlock(document.getElementById('edit-batch-addl-costs-wrap'), existingAddl);

  bootstrap.Modal.getOrCreateInstance(document.getElementById('editBatchModal')).show();
});

document.getElementById('btn-edit-batch-confirm')?.addEventListener('click', async () => {
  const batchId      = document.getElementById('edit-batch-id').value;
  const supplierId   = document.getElementById('edit-batch-supplier').value || null;
  const date         = document.getElementById('edit-batch-date').value;
  const totalPrice   = parseFloat(document.getElementById('edit-batch-total-price').value || '0');
  const qtyInput     = document.getElementById('edit-batch-qty-purchased');
  const unitSel      = document.getElementById('edit-batch-unit');
  const qtyDisplay   = parseFloat(qtyInput.value || '0');
  const conv         = parseFloat(unitSel.options[unitSel.selectedIndex]?.dataset?.conv || '1');
  const qtyBase      = qtyDisplay * conv;
  const qtyRemaining = parseFloat(qtyInput.dataset.qtyRemaining || '0');
  const unitType     = unitSel.dataset.unitType || 'unit';

  if (!totalPrice || totalPrice <= 0) return toast('Enter a valid total price', 'warning');
  if (!qtyDisplay || qtyDisplay <= 0) return toast('Enter a valid quantity', 'warning');
  if (qtyBase < qtyRemaining) return toast(`Cannot reduce below already-consumed qty (${displayQty(qtyRemaining, unitType)} used)`, 'warning');

  const ebAddlWrap  = document.getElementById('edit-batch-addl-costs-wrap');
  const ebAddlCosts = ebAddlWrap ? _readAdditionalCosts(ebAddlWrap) : [];
  const ebReason    = document.getElementById('edit-batch-reason')?.value?.trim() || null;
  const ebUpdatedAt = document.getElementById('edit-batch-updated-at')?.value || null;
  if (ebAddlCosts.length) _checkOverheadWarning(_addlCostsTotal(ebAddlCosts), totalPrice);

  try {
    const ebBody = {
      supplier_id:        supplierId ? parseInt(supplierId) : null,
      purchased_at:       date,
      total_price:        totalPrice,
      qty_purchased_base: qtyBase,
      additional_costs:   ebAddlCosts,
    };
    if (ebReason)    ebBody.cost_adjustment_reason = ebReason;
    if (ebUpdatedAt) ebBody.updated_at = ebUpdatedAt;

    await api(`/api/stock/batches/${batchId}`, { method: 'PATCH', body: JSON.stringify(ebBody) });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editBatchModal')).hide();
    toast('Batch updated', 'success', 2000);
    await loadIngredients();
  } catch (e) {
    if (e.message?.includes('409') || e.status === 409) {
      toast('Batch was updated by someone else — reload and retry', 'warning', 5000);
    } else {
      toast(e.message, 'error');
    }
  }
});

// ── Delete Batch ──
document.getElementById('products-card-list')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-delete-batch-id]');
  if (!btn) return;
  e.stopPropagation();
  const batchId = btn.dataset.deleteBatchId;
  const date    = btn.dataset.deleteBatchDate;
  const product = btn.dataset.deleteBatchProduct;
  if (!confirm(`Delete stock batch for "${product}" received on ${date}?\n\nThis cannot be undone.`)) return;
  try {
    await api(`/api/stock/batches/${batchId}`, { method: 'DELETE' });
    toast('Batch deleted', 'success', 2000);
    const pid = btn.closest('.product-row')?.dataset.productId;
    await loadIngredients();
    if (pid) document.querySelector(`.product-row[data-product-id="${pid}"]`)?.click();
  } catch (err) { toast(err.message || 'Delete failed', 'danger'); }
});

// ── Receive Stock Modal ──
function openReceiveStockModal(item) {
  STATE.receiveProductId = item.id;
  document.getElementById('receive-product-id').value  = item.id;
  document.getElementById('receive-product-name').textContent = item.name;
  document.getElementById('receive-qty').value          = '';
  document.getElementById('receive-total-price').value  = '';
  document.getElementById('receive-qty-base-display').textContent = '';
  hide(document.getElementById('receive-cost-preview'));
  _renderAdditionalCostsBlock(document.getElementById('receive-addl-costs-wrap'), []);

  // Build unit dropdown
  const unitSel = document.getElementById('receive-unit');
  unitSel.innerHTML = '';
  const opts = buildUnitOptions(item.unit_type, item.package_size, item.package_unit);
  opts.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value; opt.textContent = o.label;
    opt.dataset.conv = o.conv;
    unitSel.appendChild(opt);
  });

  // Store item context for live preview
  unitSel.dataset.unitType    = item.unit_type;
  unitSel.dataset.baseUnit    = item.base_unit;
  unitSel.dataset.packageSize = item.package_size || '';
  unitSel.dataset.packageUnit = item.package_unit || '';

  // Reset supplier dropdown and quick-add form
  const sel = document.getElementById('receive-supplier');
  if (sel) sel.value = '';
  document.getElementById('quick-supplier-form')?.classList.add('hidden');
  document.getElementById('quick-sup-name').value  = '';
  document.getElementById('quick-sup-phone').value = '';
  document.getElementById('quick-sup-email').value = '';

  bootstrap.Modal.getOrCreateInstance(document.getElementById('receiveStockModal')).show();
  document.getElementById('receive-qty').focus();
}

function updateReceivePreview() {
  const qtyEl     = document.getElementById('receive-qty');
  const priceEl   = document.getElementById('receive-total-price');
  const unitSel   = document.getElementById('receive-unit');
  const preview   = document.getElementById('receive-cost-preview');
  const baseLabel = document.getElementById('receive-qty-base-display');

  const qty        = parseFloat(qtyEl.value) || 0;
  const totalPrice = parseFloat(priceEl.value) || 0;
  const selectedOpt = unitSel.options[unitSel.selectedIndex];
  const conv        = parseFloat(selectedOpt?.dataset?.conv || 1);
  const qty_base    = qty * conv;
  const unitType    = unitSel.dataset.unitType;
  const baseUnit    = unitSel.dataset.baseUnit || 'g';

  if (qty_base > 0) {
    baseLabel.textContent = `= ${displayQty(qty_base, unitType)}`;
  } else {
    baseLabel.textContent = '';
  }

  if (qty_base > 0 && totalPrice > 0) {
    const addlWrap  = document.getElementById('receive-addl-costs-wrap');
    const addlCosts = addlWrap ? _readAdditionalCosts(addlWrap) : [];
    const addlTotal = _addlCostsTotal(addlCosts);
    const totalCost = totalPrice + addlTotal;
    const cpuFull   = totalCost / qty_base;
    const { cost: cpuFullDisp, unit: cpuFullUnit } = displayCost(cpuFull, qty_base, unitType);
    const overheadNote = addlTotal > 0 ? ` (incl. R${addlTotal.toFixed(2)} overhead)` : '';
    show(preview);
    preview.textContent = `Cost: R${cpuFullDisp < 0.01 ? cpuFullDisp.toFixed(4) : cpuFullDisp.toFixed(2)}/${cpuFullUnit}${overheadNote}`;
  } else {
    hide(preview);
  }
}

document.getElementById('receive-qty')?.addEventListener('input', updateReceivePreview);
document.getElementById('receive-total-price')?.addEventListener('input', updateReceivePreview);
document.getElementById('receive-unit')?.addEventListener('change', updateReceivePreview);
document.getElementById('receiveStockModal')?.addEventListener('input', e => {
  if (e.target.closest('[data-addl-cost-row]')) updateReceivePreview();
});

document.getElementById('btn-receive-confirm')?.addEventListener('click', async () => {
  const pid        = parseInt(document.getElementById('receive-product-id').value || '0', 10);
  const qty        = parseFloat(document.getElementById('receive-qty').value || '0');
  const totalPrice = parseFloat(document.getElementById('receive-total-price').value || '0');
  const unitSel    = document.getElementById('receive-unit');
  const unit       = unitSel.value;

  if (!pid || qty <= 0)         return toast('Enter a valid quantity', 'warning');
  if (totalPrice <= 0)          return toast('Enter the total amount paid', 'warning');

  const supplier_id = parseInt(document.getElementById('receive-supplier')?.value || '0') || null;

  const recAddlWrap  = document.getElementById('receive-addl-costs-wrap');
  const recAddlCosts = recAddlWrap ? _readAdditionalCosts(recAddlWrap) : [];
  if (recAddlCosts.length) _checkOverheadWarning(_addlCostsTotal(recAddlCosts), totalPrice);

  try {
    const j = await api('/api/stock/receive', {
      method: 'POST',
      body: JSON.stringify({ product_id: pid, qty, unit, total_price: totalPrice, supplier_id, additional_costs: recAddlCosts })
    });
    toast(`Stock received - R${j.cost_per_base_unit}/unit (${j.qty_base.toFixed(2)} ${j.base_unit})`, 'success', 4000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('receiveStockModal')).hide();
    await loadIngredients();
    await loadProducts();
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// STOCKTAKE MODAL
// ═══════════════════════════════════════════════════════
let _stocktakeItem = null;

function _buildStocktakeUnitSelect(unitType, packageSize, packageUnit, selectedUnit) {
  const sel = document.createElement('select');
  sel.className = 'form-select form-select-sm stocktake-row-unit';
  if (_stocktakeItem?.product_type === 'recipe' && _stocktakeItem?.is_produced && _stocktakeItem?.stock_unit) {
    const lbl = _stocktakeItem.stock_unit;
    const opt = document.createElement('option');
    opt.value = 'unit'; opt.textContent = lbl; opt.dataset.conv = 1; opt.selected = true;
    sel.appendChild(opt);
    return sel;
  }
  buildUnitOptions(unitType, packageSize, packageUnit).forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value; opt.textContent = o.label; opt.dataset.conv = o.conv;
    if (o.value === selectedUnit) opt.selected = true;
    sel.appendChild(opt);
  });
  return sel;
}

function _addStocktakeRow(defaultUnit) {
  if (!_stocktakeItem) return;
  const rows = document.getElementById('stocktake-rows');
  const rowEl = document.createElement('div');
  rowEl.className = 'd-flex gap-2 align-items-center mb-2 stocktake-row';

  const qtyInput = document.createElement('input');
  qtyInput.type = 'number'; qtyInput.step = '0.01'; qtyInput.min = '0';
  qtyInput.className = 'form-control form-control-sm stocktake-row-qty';
  qtyInput.placeholder = '0';
  qtyInput.style.width = '90px';

  const unitSel = _buildStocktakeUnitSelect(
    _stocktakeItem.unit_type,
    _stocktakeItem.package_size,
    _stocktakeItem.package_unit,
    defaultUnit
  );

  const removeBtn = document.createElement('button');
  removeBtn.type = 'button';
  removeBtn.className = 'btn btn-outline-danger btn-sm';
  removeBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
  removeBtn.style.display = rows.children.length === 0 ? 'none' : ''; // hide on first row
  removeBtn.onclick = () => { rowEl.remove(); _updateStocktakePreview(); };

  qtyInput.addEventListener('input', _updateStocktakePreview);
  unitSel.addEventListener('change', _updateStocktakePreview);

  rowEl.appendChild(qtyInput);
  rowEl.appendChild(unitSel);
  rowEl.appendChild(removeBtn);
  rows.appendChild(rowEl);

  // Show remove button on first row now that a second exists
  if (rows.children.length > 1) {
    rows.children[0].querySelector('button')?.style && (rows.children[0].querySelector('button').style.display = '');
  }

  qtyInput.focus();
}

function _stocktakeTotalBase() {
  let total = 0;
  document.querySelectorAll('.stocktake-row').forEach(row => {
    const qty  = parseFloat(row.querySelector('.stocktake-row-qty')?.value) || 0;
    const sel  = row.querySelector('.stocktake-row-unit');
    const conv = parseFloat(sel?.options[sel?.selectedIndex]?.dataset?.conv || 1);
    total += qty * conv;
  });
  return total;
}

function _updateStocktakePreview() {
  if (!_stocktakeItem) return;
  const actual  = _stocktakeTotalBase();
  const system  = _stocktakeItem.stock_level || 0;
  const diff    = actual - system;
  const preview = document.getElementById('stocktake-diff-preview');
  const totEl   = document.getElementById('stocktake-total-preview');

  const _isProducedRecipe = _stocktakeItem.product_type === 'recipe' && _stocktakeItem.is_produced && !!_stocktakeItem.stock_unit;
  const _fmtStocktakeQty = (n) => {
    if (_isProducedRecipe) {
      const lbl = _stocktakeItem.stock_unit;
      return `${n} ${lbl}${n !== 1 ? 's' : ''}`;
    }
    return displayQty(n, _stocktakeItem.unit_type);
  };

  // Show running total when multiple rows
  const rowCount = document.querySelectorAll('.stocktake-row').length;
  if (rowCount > 1 && actual > 0) {
    show(totEl);
    totEl.textContent = `Total counted: ${_fmtStocktakeQty(actual)}`;
  } else {
    hide(totEl);
  }

  if (actual === 0) { hide(preview); return; }

  show(preview);
  if (Math.abs(diff) < 0.001) {
    preview.className = 'alert alert-success py-2 small';
    preview.innerHTML = '<i class="bi bi-check-lg me-1"></i>Matches system - no adjustment needed';
  } else if (diff < 0) {
    preview.className = 'alert alert-warning py-2 small';
    preview.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>System will deduct ${_fmtStocktakeQty(Math.abs(diff))} (unexplained loss)`;
  } else {
    preview.className = 'alert alert-info py-2 small';
    preview.innerHTML = `<i class="bi bi-info-circle me-1"></i>System will add ${_fmtStocktakeQty(diff)} (found more than expected)`;
  }
}

function openStocktakeModal(item) {
  _stocktakeItem = item;
  document.getElementById('stocktake-product-id').value = item.id;
  document.getElementById('stocktake-product-name').textContent = item.name;
  document.getElementById('stocktake-reason').value = '';
  hide(document.getElementById('stocktake-diff-preview'));
  hide(document.getElementById('stocktake-total-preview'));

  const _isProduced = item.product_type === 'recipe' && item.is_produced && !!item.stock_unit;
  if (_isProduced) {
    const n = item.stock_level || 0;
    const lbl = item.stock_unit;
    document.getElementById('stocktake-system-qty').textContent = `${n} ${lbl}${n !== 1 ? 's' : ''}`;
  } else {
    document.getElementById('stocktake-system-qty').textContent = displayQty(item.stock_level || 0, item.unit_type);
  }

  // Reset rows - start with one blank row using base unit
  document.getElementById('stocktake-rows').innerHTML = '';
  _addStocktakeRow(_isProduced ? 'unit' : (UNITS[item.unit_type]?.base || 'unit'));

  bootstrap.Modal.getOrCreateInstance(document.getElementById('stocktakeModal')).show();
}

document.getElementById('btn-stocktake-add-row')?.addEventListener('click', () => {
  if (!_stocktakeItem) return;
  const isProduced = _stocktakeItem.product_type === 'recipe' && _stocktakeItem.is_produced && !!_stocktakeItem.stock_unit;
  const defaultUnit = isProduced ? 'unit'
    : (_stocktakeItem.package_unit || UNITS[_stocktakeItem.unit_type]?.base || 'unit');
  _addStocktakeRow(defaultUnit);
});

document.getElementById('btn-stocktake-confirm')?.addEventListener('click', async () => {
  const pid    = parseInt(document.getElementById('stocktake-product-id').value || '0', 10);
  const reason = document.getElementById('stocktake-reason').value.trim();
  const total  = _stocktakeTotalBase();

  if (total < 0)  return toast('Enter the actual quantity counted', 'warning');
  if (!reason)    return toast('Please enter a reason or note', 'warning');

  // Send total in base units directly
  const _isProducedConfirm = _stocktakeItem?.product_type === 'recipe' && _stocktakeItem?.is_produced && !!_stocktakeItem?.stock_unit;
  const baseUnit = _isProducedConfirm ? 'unit' : (_stocktakeItem?.base_unit || UNITS[_stocktakeItem?.unit_type]?.base || 'unit');
  try {
    const j = await api('/api/stock/adjust', {
      method: 'POST',
      body: JSON.stringify({ product_id: pid, actual_qty: total, unit: baseUnit, reason })
    });
    const _diffN = Math.abs(j.difference);
    const _lbl = _stocktakeItem?.stock_unit || 'unit';
    const diffDisplay = _isProducedConfirm
      ? `${_diffN} ${_lbl}${_diffN !== 1 ? 's' : ''}`
      : displayQty(_diffN, _stocktakeItem?.unit_type);
    const msg = j.difference === 0
      ? 'No change - stock levels match'
      : j.difference < 0
        ? `Adjusted: removed ${diffDisplay} (loss recorded)`
        : `Adjusted: added ${diffDisplay} (surplus recorded)`;
    toast(msg, 'success', 4000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('stocktakeModal')).hide();
    await loadIngredients();
    await loadProducts();
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// WRITE-OFF MODAL
// ═══════════════════════════════════════════════════════
let _writeoffItem = null;

function openWriteoffModal(item) {
  _writeoffItem = item;
  document.getElementById('writeoff-product-id').value        = item.id;
  document.getElementById('writeoff-product-name').textContent = item.name;
  document.getElementById('writeoff-qty').value               = '';
  document.getElementById('writeoff-reason').value            = '';
  hide(document.getElementById('writeoff-cost-preview'));

  const isProduced = item.product_type === 'recipe' && item.is_produced && !!item.stock_unit;
  const unitSel    = document.getElementById('writeoff-unit');
  unitSel.innerHTML = '';

  if (isProduced) {
    const stock = item.stock_level ?? item.stock_qty ?? 0;
    const lbl = item.stock_unit;
    document.getElementById('writeoff-available').textContent = `${stock} ${lbl}${stock !== 1 ? 's' : ''}`;
    const opt = document.createElement('option');
    opt.value = 'unit'; opt.textContent = lbl; opt.dataset.conv = 1;
    unitSel.appendChild(opt);
  } else {
    document.getElementById('writeoff-available').textContent = displayQty(item.stock_level || 0, item.unit_type);
    buildUnitOptions(item.unit_type, item.package_size, item.package_unit).forEach(o => {
      const opt = document.createElement('option');
      opt.value = o.value; opt.textContent = o.label; opt.dataset.conv = o.conv;
      unitSel.appendChild(opt);
    });
  }

  bootstrap.Modal.getOrCreateInstance(document.getElementById('writeoffModal')).show();
  setTimeout(() => document.getElementById('writeoff-qty').focus(), 300);
}

function updateWriteoffPreview() {
  if (!_writeoffItem) return;
  if (_writeoffItem.product_type === 'recipe' && _writeoffItem.is_produced) return;
  const qty     = parseFloat(document.getElementById('writeoff-qty').value) || 0;
  const unitSel = document.getElementById('writeoff-unit');
  const conv    = parseFloat(unitSel.options[unitSel.selectedIndex]?.dataset?.conv || 1);
  const qty_base = qty * conv;
  const preview  = document.getElementById('writeoff-cost-preview');

  if (qty_base <= 0) { hide(preview); return; }

  // Estimate cost from batches (oldest first)
  const batches = (_writeoffItem.batches || []).slice().reverse(); // oldest first
  let remaining = qty_base, estCost = 0;
  for (const b of batches) {
    if (remaining <= 0) break;
    const take = Math.min(b.qty_remaining_base, remaining);
    estCost   += take * b.cost_per_base_unit;
    remaining -= take;
  }

  show(preview);
  preview.textContent = `Estimated cost written off: R${estCost.toFixed(4)} for ${displayQty(qty_base, _writeoffItem.unit_type)}`;
}

document.getElementById('writeoff-qty')?.addEventListener('input', updateWriteoffPreview);
document.getElementById('writeoff-unit')?.addEventListener('change', updateWriteoffPreview);

document.getElementById('btn-writeoff-confirm')?.addEventListener('click', async () => {
  const pid    = parseInt(document.getElementById('writeoff-product-id').value || '0', 10);
  const qty    = parseFloat(document.getElementById('writeoff-qty').value || '0');
  const unit   = document.getElementById('writeoff-unit').value;
  const reason = document.getElementById('writeoff-reason').value.trim();

  if (qty <= 0)   return toast('Enter a valid quantity', 'warning');
  if (!reason)    return toast('Please enter a reason (e.g. Cheese expired)', 'warning');

  try {
    const j = await api('/api/stock/writeoff', {
      method: 'POST',
      body: JSON.stringify({ product_id: pid, qty, unit, reason })
    });
    const isProduced = _writeoffItem?.product_type === 'recipe' && _writeoffItem?.is_produced && !!_writeoffItem?.stock_unit;
    const _woLbl = _writeoffItem?.stock_unit || 'unit';
    const msg = isProduced
      ? `Written off: ${j.qty_written_off} ${_woLbl}${j.qty_written_off !== 1 ? 's' : ''}`
      : `Written off: ${displayQty(j.qty_written_off, _writeoffItem?.unit_type)} - Cost: R${j.cost_written_off.toFixed(4)}`;
    toast(msg, 'warning', 5000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('writeoffModal')).hide();
    await loadIngredients();
    await loadProducts();
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// BATCH HISTORY MODAL
// ═══════════════════════════════════════════════════════

function openBatchHistoryModal(product) {
  const modal = document.getElementById('batchHistoryModal');
  if (!modal) return;
  modal.dataset.productId   = product.id;
  modal.dataset.productName = product.name;
  modal.dataset.baseUnit    = product.base_unit || '';
  modal.dataset.unitType    = product.unit_type || '';
  document.getElementById('bh-product-name').textContent = product.name;

  // Default date range: last 30 days
  const today = new Date();
  const prior = new Date(today); prior.setDate(prior.getDate() - 30);
  document.getElementById('bh-start').value = prior.toISOString().slice(0, 10);
  document.getElementById('bh-end').value   = today.toISOString().slice(0, 10);

  bootstrap.Modal.getOrCreateInstance(modal).show();
  loadBatchHistory();
}

async function loadBatchHistory() {
  const modal   = document.getElementById('batchHistoryModal');
  const pid     = modal?.dataset.productId;
  const start   = document.getElementById('bh-start').value;
  const end     = document.getElementById('bh-end').value;
  const body    = document.getElementById('bh-table-body');
  const summary = document.getElementById('bh-summary');
  if (!pid || !body) return;

  body.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3"><div class="spinner-border spinner-border-sm me-2"></div>Loading...</td></tr>';
  summary.innerHTML = '';

  try {
    const batches = await api(`/api/stock/products/${pid}/batch-history?start=${start}&end=${end}`);
    const baseUnit = modal.dataset.baseUnit;
    const unitType = modal.dataset.unitType;

    if (!batches.length) {
      body.innerHTML = '<tr><td colspan="8" class="text-center text-muted py-3">No batches found in this date range.</td></tr>';
      return;
    }

    let totalPurchased = 0, totalConsumed = 0, totalValue = 0, totalCostConsumed = 0;
    body.innerHTML = batches.map(b => {
      const pctConsumed = b.qty_purchased_base > 0 ? (b.qty_consumed_base / b.qty_purchased_base * 100) : 0;
      const statusBadge = b.status === 'active'
        ? `<span class="badge bg-success">Active</span>`
        : `<span class="badge bg-secondary">Consumed</span>`;
      const isProduced = !!b.produce_ref;
      const costLabel = isProduced
        ? `R${(b.produce_cost ?? 0).toFixed(4)} <span class="text-muted" style="font-size:10px">(produced)</span>`
        : `R${b.cost_per_base_unit.toFixed(4)}/${baseUnit}`;
      totalPurchased    += b.qty_purchased_base;
      totalConsumed     += b.qty_consumed_base;
      totalValue        += b.total_value;
      totalCostConsumed += b.cost_consumed;
      return `<tr>
        <td class="text-nowrap">${b.purchased_at.slice(0,10)}</td>
        <td>${b.supplier_name || '<span class="text-muted">—</span>'}</td>
        <td>${displayQty(b.qty_purchased_base, unitType)}</td>
        <td>${displayQty(b.qty_consumed_base, unitType)}</td>
        <td>${displayQty(b.qty_remaining_base, unitType)}</td>
        <td class="text-nowrap">${costLabel}</td>
        <td class="text-nowrap">R${b.total_value.toFixed(2)}</td>
        <td>${statusBadge}</td>
      </tr>`;
    }).join('');

    summary.innerHTML = `
      <div class="d-flex gap-3 flex-wrap small text-muted mt-2">
        <span><strong class="text-body">${batches.length}</strong> batch${batches.length !== 1 ? 'es' : ''}</span>
        <span>Purchased: <strong class="text-body">${displayQty(totalPurchased, unitType)}</strong></span>
        <span>Consumed: <strong class="text-body">${displayQty(totalConsumed, unitType)}</strong></span>
        <span>Remaining: <strong class="text-body">${displayQty(totalPurchased - totalConsumed, unitType)}</strong></span>
        <span>Total cost purchased: <strong class="text-body">R${totalValue.toFixed(2)}</strong></span>
        <span>Total cost consumed: <strong class="text-body">R${totalCostConsumed.toFixed(2)}</strong></span>
      </div>`;
  } catch (err) {
    body.innerHTML = `<tr><td colspan="8" class="text-danger py-2">${err.message || 'Failed to load'}</td></tr>`;
  }
}

document.getElementById('bh-filter-btn')?.addEventListener('click', loadBatchHistory);
document.getElementById('bh-start')?.addEventListener('keydown', e => { if (e.key === 'Enter') loadBatchHistory(); });
document.getElementById('bh-end')?.addEventListener('keydown',   e => { if (e.key === 'Enter') loadBatchHistory(); });


// ═══════════════════════════════════════════════════════
// SUPPLIERS
// ═══════════════════════════════════════════════════════
let _suppliers = [];
let _editingSupplierId = null;
let _currentSupplier = null;
let _purchaseRunLines = [];
let _currentSupplierProducts = [];

async function loadSuppliers() {
  const roles = STATE.user?.roles || [STATE.user?.role];
  if (!roles.includes('admin')) return;
  try {
    _suppliers = await api('/api/suppliers');
    renderSuppliersList();
    populateSupplierDropdowns();
  } catch (e) { console.error('loadSuppliers', e); }
}

function renderSuppliersList() {
  const host = document.getElementById('suppliers-list'); if (!host) return;
  host.innerHTML = '';
  if (_suppliers.length === 0) {
    host.innerHTML = '<div class="list-group-item text-muted">No suppliers yet.</div>';
    return;
  }
  _suppliers.forEach(s => {
    const item = document.createElement('div');
    item.className = 'list-group-item list-group-item-action';
    item.dataset.supplierId = s.id;
    item.style.cursor = 'pointer';
    const contactBits = [
      s.phone   ? `<i class="bi bi-telephone me-1"></i>${s.phone}`   : '',
      s.email   ? `<i class="bi bi-envelope me-1"></i>${s.email}`    : '',
      s.website ? `<i class="bi bi-globe me-1"></i>${s.website}`     : '',
    ].filter(Boolean).join('  ');
    item.innerHTML = `
      <strong>${s.name}</strong>
      ${contactBits ? `<div class="small text-muted">${contactBits}</div>` : ''}
      ${s.notes     ? `<div class="small text-muted fst-italic">${s.notes}</div>` : ''}
    `;
    item.addEventListener('click', () => openSupplierDetail(s));
    host.appendChild(item);
  });
}

function openSupplierDetail(supplier) {
  _currentSupplier = supplier;

  // Highlight active supplier in list
  document.querySelectorAll('#suppliers-list .list-group-item').forEach(el => {
    el.classList.toggle('active', el.dataset.supplierId === String(supplier.id));
  });

  // Detail replaces the form - form only shows when adding/editing
  hide(document.getElementById('supplier-edit-panel'));
  show(document.getElementById('supplier-detail-panel'));
  hide(document.getElementById('purchase-run-panel'));

  // Populate detail
  document.getElementById('supplier-detail-name').textContent = supplier.name;
  const rows = [
    supplier.phone   ? `<div><span class="text-muted" style="width:70px;display:inline-block">Phone</span> <a href="tel:${supplier.phone}">${supplier.phone}</a></div>` : '',
    supplier.email   ? `<div><span class="text-muted" style="width:70px;display:inline-block">Email</span> <a href="mailto:${supplier.email}">${supplier.email}</a></div>` : '',
    supplier.website ? `<div><span class="text-muted" style="width:70px;display:inline-block">Website</span> <a href="${supplier.website}" target="_blank" rel="noopener">${supplier.website}</a></div>` : '',
    supplier.notes   ? `<div class="mt-1 fst-italic text-muted small">${supplier.notes}</div>` : '',
  ].filter(Boolean).join('');
  document.getElementById('supplier-detail-contact').innerHTML = rows || '<span class="text-muted small">No contact details</span>';

  loadSupplierProducts(supplier.id);
  loadSupplierDocs(supplier.id);
  loadSupplierBatches(supplier.id);
}

async function loadSupplierDocs(sid) {
  const host = document.getElementById('supplier-docs-list');
  if (!host) return;
  host.innerHTML = '<div class="list-group-item text-muted small">Loading...</div>';
  try {
    const docs = await api(`/api/suppliers/${sid}/documents`);
    if (!docs.length) {
      host.innerHTML = '<div class="list-group-item text-muted small">No documents yet.</div>';
      return;
    }
    host.innerHTML = docs.map(d => `
      <div class="list-group-item d-flex align-items-center gap-2 py-1 px-2" data-doc-id="${d.id}">
        <span class="small flex-fill text-truncate" title="${escapeHtml(d.original_name)}">${escapeHtml(d.original_name)}</span>
        <span class="small text-muted">${d.uploaded_at || ''}</span>
        <a href="/api/suppliers/${sid}/documents/${d.id}/download" class="btn btn-outline-secondary btn-sm py-0 px-1" title="Download"><i class="bi bi-download"></i></a>
        <button class="btn btn-outline-danger btn-sm py-0 px-1 btn-doc-delete" data-doc-id="${d.id}" title="Delete"><i class="bi bi-x-lg"></i></button>
      </div>`).join('');
    host.querySelectorAll('.btn-doc-delete').forEach(btn => {
      btn.addEventListener('click', async () => {
        if (!confirm('Delete this document?')) return;
        const did = btn.dataset.docId;
        await api(`/api/suppliers/${sid}/documents/${did}`, { method: 'DELETE' });
        loadSupplierDocs(sid);
      });
    });
  } catch (e) {
    host.innerHTML = '<div class="list-group-item text-danger small">Failed to load documents.</div>';
  }
}

document.getElementById('supplier-doc-upload-input')?.addEventListener('change', async function () {
  const file = this.files[0];
  if (!file || !_currentSupplier) return;
  const fd = new FormData();
  fd.append('file', file);
  this.value = '';
  try {
    const res = await fetch(`/api/suppliers/${_currentSupplier.id}/documents`, { method: 'POST', body: fd, credentials: 'same-origin' });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      toast(j.error || 'Upload failed', 'error');
      return;
    }
    loadSupplierDocs(_currentSupplier.id);
    toast('Document uploaded', 'success');
  } catch (e) {
    toast('Upload failed', 'error');
  }
});

async function loadSupplierProducts(sid) {
  const host = document.getElementById('supplier-products-list');
  if (!host) return;
  host.innerHTML = '<span class="text-muted small">Loading...</span>';
  try {
    const products = await api(`/api/suppliers/${sid}/products`);
    _currentSupplierProducts = products;
    const countEl = document.getElementById('supplier-products-count');
    if (countEl) countEl.textContent = products.length ? `${products.length} product${products.length > 1 ? 's' : ''}` : '';
    if (products.length === 0) {
      host.innerHTML = '<span class="text-muted small">No products on record yet.</span>';
      return;
    }
    host.innerHTML = `
      <table class="table table-sm table-hover mb-0">
        <thead class="table-light"><tr><th>Name</th><th>Type</th><th>Last Received</th></tr></thead>
        <tbody>
          ${products.map(p => `<tr>
            <td>${p.name}</td>
            <td><span class="badge bg-secondary" style="font-size:10px">${p.product_type}</span></td>
            <td class="small text-muted">${p.last_received || '-'}</td>
          </tr>`).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    host.innerHTML = `<span class="text-danger small">Error: ${e.message}</span>`;
  }
}

// Toggle products collapse
document.getElementById('supplier-products-toggle')?.addEventListener('click', () => {
  const body    = document.getElementById('supplier-products-collapse');
  const chevron = document.getElementById('supplier-products-chevron');
  if (!body) return;
  const collapsed = body.classList.toggle('hidden');
  if (chevron) chevron.textContent = collapsed ? '▶' : '▼';
});

// Toggle batches collapse
document.getElementById('supplier-batches-toggle')?.addEventListener('click', () => {
  const body    = document.getElementById('supplier-batches-collapse');
  const chevron = document.getElementById('supplier-batches-chevron');
  if (!body) return;
  const collapsed = body.classList.toggle('hidden');
  if (chevron) chevron.textContent = collapsed ? '▶' : '▼';
});

async function loadSupplierBatches(supplierId) {
  const wrap = document.getElementById('supplier-recent-batches-wrap');
  if (!wrap) return;
  try {
    const runs = await api(`/api/suppliers/${supplierId}/batches`);
    if (!runs.length) {
      wrap.innerHTML = '<div class="text-muted small">No recent batches from this supplier.</div>';
      return;
    }

    let html = `
      <div class="d-flex align-items-center mb-2">
        <span class="small fw-semibold">Recent Deliveries</span>
        <button type="button" class="btn btn-outline-primary btn-sm ms-auto" id="btn-apply-costs-to-selected" disabled>
          <i class="bi bi-receipt me-1"></i>Allocate Invoice Costs
        </button>
      </div>`;

    runs.forEach((run, ri) => {
      const runLabel   = run.run_id ? `Run #${run.run_id}` : 'Single receive';
      const hasConsumed = run.batches.some(b => b.consumed_pct > 0);
      html += `<div class="border rounded mb-2" id="supplier-run-${ri}">
        <div class="d-flex align-items-center gap-2 px-2 py-1" style="background:#f8f9fa;cursor:pointer;border-radius:4px 4px 0 0"
             onclick="this.nextElementSibling.classList.toggle('hidden')">
          <input type="checkbox" class="supplier-run-chk" data-run-idx="${ri}"
            onclick="event.stopPropagation(); _selectSupplierRun(${ri}, this.checked); _updateApplyCostsBtn(document.getElementById('supplier-recent-batches-wrap'))">
          <span class="small fw-semibold">${runLabel}</span>
          <span class="badge bg-light text-dark border">${run.date}</span>
          <span class="text-muted small">${run.batch_count} item${run.batch_count !== 1 ? 's' : ''}</span>
          <span class="ms-auto text-muted small">R${fmt(run.base_total)}</span>
          ${hasConsumed ? `<span class="badge bg-warning text-dark" style="font-size:10px">partial sales</span>` : ''}
          <i class="bi bi-chevron-down" style="font-size:11px"></i>
        </div>
        <div class="px-2 pb-1">`;

      run.batches.forEach(b => {
        const addlData = b.additional_costs ? (() => { try { return JSON.parse(b.additional_costs); } catch { return []; } })() : [];
        const addlTot  = addlData.reduce((s, c) => s + parseFloat(c.amount || 0), 0);
        const addlStr  = addlTot > 0
          ? `<span class="badge bg-secondary ms-1" style="font-size:10px">${addlData.map(c => (_COST_TYPE_LABELS[c.type] || c.type)).join(', ')} R${addlTot.toFixed(2)}</span>`
          : '';
        const cWarn    = b.consumed_pct > 0 ? `<span class="badge bg-warning text-dark ms-1" style="font-size:10px">${Math.round(b.consumed_pct)}% sold</span>` : '';
        html += `<div class="d-flex align-items-center gap-2 py-1" style="font-size:12px;border-top:1px solid #f0f0f0">
          <input type="checkbox" class="supplier-batch-chk"
            data-batch-id="${b.id}"
            data-base-cost="${b.base_cost_total || 0}"
            data-consumed="${b.consumed_pct || 0}"
            data-addl-costs="${escapeHtml(b.additional_costs || '')}"
            onchange="_updateApplyCostsBtn(document.getElementById('supplier-recent-batches-wrap'))">
          <span class="flex-fill">${escapeHtml(b.product_name)}${cWarn}${addlStr}</span>
          <span class="text-muted">R${fmt(b.base_cost_total || 0)}</span>
        </div>`;
      });

      html += `</div></div>`;
    });

    html += `
      <div id="apply-costs-panel" class="border rounded p-2 hidden mt-2">
        <div class="small fw-semibold mb-1"><i class="bi bi-receipt me-1"></i>Allocate Invoice Costs to Selected Batches</div>
        <div id="apply-costs-addl-wrap"></div>
        <div class="mt-2">
          <label class="form-label small">Note (optional)</label>
          <input id="apply-costs-reason" type="text" class="form-control form-control-sm" placeholder="e.g. Correction — delivery fee omitted at receipt">
        </div>
        <div id="apply-costs-summary" class="mt-2 small text-muted"></div>
        <div class="mt-2 d-flex gap-2">
          <button class="btn btn-warning btn-sm" id="btn-apply-costs-confirm">Apply</button>
          <button class="btn btn-outline-secondary btn-sm" id="btn-apply-costs-cancel">Cancel</button>
        </div>
      </div>`;

    wrap.innerHTML = html;

    document.getElementById('btn-apply-costs-to-selected')?.addEventListener('click', () => {
      const panel = document.getElementById('apply-costs-panel');
      if (!panel) return;
      show(panel);
      _renderAdditionalCostsBlock(document.getElementById('apply-costs-addl-wrap'), []);
      _updateApplyCostsSummary(wrap);
      panel.addEventListener('input', e => {
        if (e.target.closest('[data-addl-cost-row]')) _updateApplyCostsSummary(wrap);
      });
    });

    document.getElementById('btn-apply-costs-cancel')?.addEventListener('click', () => {
      hide(document.getElementById('apply-costs-panel'));
    });

    document.getElementById('btn-apply-costs-confirm')?.addEventListener('click', async () => {
      const acAddlWrap  = document.getElementById('apply-costs-addl-wrap');
      const acAddlCosts = acAddlWrap ? _readAdditionalCosts(acAddlWrap) : [];
      if (!acAddlCosts.length) return toast('Add at least one cost', 'warning');
      const acReason     = document.getElementById('apply-costs-reason')?.value?.trim() || null;
      const selectedChks = [...wrap.querySelectorAll('.supplier-batch-chk:checked')];
      const selectedIds  = selectedChks.map(c => parseInt(c.dataset.batchId));
      if (!selectedIds.length) return toast('Select at least one batch', 'warning');

      const consumedOnes = selectedChks.filter(c => parseFloat(c.dataset.consumed) > 0);
      if (consumedOnes.length > 0) {
        const msg = `${consumedOnes.length} selected batch(es) are partially consumed. Costs will apply only to remaining stock. Previous sales will NOT change. Continue?`;
        if (!confirm(msg)) return;
      }

      try {
        const acBody = { batch_ids: selectedIds, additional_costs: acAddlCosts };
        if (acReason) acBody.cost_adjustment_reason = acReason;
        await api('/api/stock/batches/apply-costs', { method: 'POST', body: JSON.stringify(acBody) });
        toast(`Costs allocated to ${selectedIds.length} batch(es)`, 'success', 3000);
        hide(document.getElementById('apply-costs-panel'));
        loadSupplierBatches(supplierId);
        loadIngredients();
      } catch (e) { toast(e.message, 'error'); }
    });
  } catch (e) {
    wrap.innerHTML = `<span class="text-danger small">Error: ${e.message}</span>`;
  }
}

function _selectSupplierRun(runIdx, checked) {
  const runEl = document.getElementById(`supplier-run-${runIdx}`);
  if (!runEl) return;
  runEl.querySelectorAll('.supplier-batch-chk').forEach(c => { c.checked = checked; });
}

function _updateApplyCostsBtn(wrap) {
  const selected = wrap.querySelectorAll('.supplier-batch-chk:checked').length;
  const btn = document.getElementById('btn-apply-costs-to-selected');
  if (btn) {
    btn.disabled   = selected === 0;
    btn.textContent = selected > 0 ? `Apply costs to ${selected} selected` : 'Apply costs to selected';
  }
  const panel = document.getElementById('apply-costs-panel');
  if (panel && !panel.classList.contains('hidden')) _updateApplyCostsSummary(wrap);
}

function _updateApplyCostsSummary(wrap) {
  const summaryEl = document.getElementById('apply-costs-summary');
  if (!summaryEl) return;
  const acAddlWrap  = document.getElementById('apply-costs-addl-wrap');
  const acAddlCosts = acAddlWrap ? _readAdditionalCosts(acAddlWrap) : [];
  const addlTotal   = _addlCostsTotal(acAddlCosts);
  const selectedChks = [...wrap.querySelectorAll('.supplier-batch-chk:checked')];
  const totalBase   = selectedChks.reduce((s, c) => s + parseFloat(c.dataset.baseCost || 0), 0);
  const count       = selectedChks.length;
  if (!count || !addlTotal) { summaryEl.textContent = ''; return; }
  summaryEl.innerHTML = `Splitting R${addlTotal.toFixed(2)} across ${count} batch(es) (base inventory value R${totalBase.toFixed(2)})`;
}

function populateSupplierDropdowns() {
  // Receive stock modal dropdown
  const sel = document.getElementById('receive-supplier');
  if (sel) {
    const prev = sel.value;
    sel.innerHTML = '<option value="">- No supplier -</option>';
    _suppliers.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id; opt.textContent = s.name;
      sel.appendChild(opt);
    });
    if (prev) sel.value = prev;
  }
}

function clearSupplierForm() {
  _editingSupplierId = null;
  _currentSupplier = null;
  _currentSupplierProducts = [];
  ['sup-id','sup-name','sup-phone','sup-email','sup-website','sup-notes'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  document.getElementById('supplier-form-title').textContent = 'Add Supplier';
  show(document.getElementById('supplier-edit-panel'));
  hide(document.getElementById('supplier-detail-panel'));
  document.querySelectorAll('#suppliers-list .list-group-item').forEach(el => el.classList.remove('active'));
}

document.getElementById('btn-clear-supplier')?.addEventListener('click', clearSupplierForm);
document.getElementById('btn-refresh-suppliers')?.addEventListener('click', loadSuppliers);
document.getElementById('btn-new-supplier')?.addEventListener('click', () => {
  clearSupplierForm();
  hide(document.getElementById('supplier-detail-panel'));
  show(document.getElementById('supplier-edit-panel'));
  document.getElementById('sup-name')?.focus();
});

document.getElementById('btn-save-supplier')?.addEventListener('click', async () => {
  const id      = _editingSupplierId;
  const name    = document.getElementById('sup-name').value.trim();
  const phone   = document.getElementById('sup-phone').value.trim();
  const email   = document.getElementById('sup-email').value.trim();
  const website = document.getElementById('sup-website').value.trim();
  const notes   = document.getElementById('sup-notes').value.trim();
  if (!name) return toast('Supplier name required', 'warning');
  try {
    let savedId = id;
    if (id) {
      await api(`/api/suppliers/${id}`, { method: 'POST', body: JSON.stringify({ name, phone, email, website, notes }) });
      toast('Supplier updated');
    } else {
      const r = await api('/api/suppliers', { method: 'POST', body: JSON.stringify({ name, phone, email, website, notes }) });
      savedId = r.id;
      toast('Supplier added');
    }
    clearSupplierForm();
    await loadSuppliers();
    // Re-open the saved supplier's detail
    const saved = _suppliers.find(s => s.id === savedId);
    if (saved) openSupplierDetail(saved);
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-delete-supplier')?.addEventListener('click', async () => {
  if (!_editingSupplierId) return toast('Select a supplier to delete', 'warning');
  const name = document.getElementById('sup-name').value.trim();
  if (!confirm(`Delete supplier "${name}"? Past purchases will keep the supplier name.`)) return;
  try {
    await api(`/api/suppliers/${_editingSupplierId}`, { method: 'DELETE' });
    toast('Supplier deleted');
    clearSupplierForm();
    await loadSuppliers();
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-supplier-edit')?.addEventListener('click', () => {
  if (!_currentSupplier) return;
  _editingSupplierId = _currentSupplier.id;
  document.getElementById('sup-id').value      = _currentSupplier.id;
  document.getElementById('sup-name').value    = _currentSupplier.name;
  document.getElementById('sup-phone').value   = _currentSupplier.phone   || '';
  document.getElementById('sup-email').value   = _currentSupplier.email   || '';
  document.getElementById('sup-website').value = _currentSupplier.website || '';
  document.getElementById('sup-notes').value   = _currentSupplier.notes   || '';
  document.getElementById('supplier-form-title').textContent = `Edit - ${_currentSupplier.name}`;
  show(document.getElementById('supplier-edit-panel'));
  hide(document.getElementById('supplier-detail-panel'));
  document.getElementById('sup-name')?.focus();
});

// Purchase Run
document.getElementById('btn-supplier-new-run')?.addEventListener('click', () => {
  const dateInput = document.getElementById('purchase-run-date');
  if (dateInput) dateInput.value = todayISO();
  _purchaseRunLines = [];
  document.getElementById('purchase-run-lines').innerHTML = '';
  show(document.getElementById('purchase-run-panel'));
  addPurchaseLine();
  // Pre-populate addl costs from last run
  const prAddlWrap = document.getElementById('purchase-run-addl-costs-wrap');
  if (prAddlWrap && _currentSupplier) {
    const existing = _currentSupplier.last_run_costs
      ? (() => { try { return JSON.parse(_currentSupplier.last_run_costs); } catch { return []; } })()
      : [];
    _renderAdditionalCostsBlock(prAddlWrap, existing);
    prAddlWrap.addEventListener('input', _updatePurchaseRunCostPreview);
  }
});

document.getElementById('btn-cancel-purchase-run')?.addEventListener('click', () => {
  hide(document.getElementById('purchase-run-panel'));
  _purchaseRunLines = [];
});

document.getElementById('btn-add-purchase-line')?.addEventListener('click', addPurchaseLine);

// Track which purchase line is waiting for a new product to be created
let _pendingPurchaseLine = null;

function _buildProductOptions(supplierProductIds, showAll = false) {
  const active = STATE.products.filter(p => !p.is_archived);
  const own    = active.filter(p => supplierProductIds.has(p.id));
  if (!showAll && own.length > 0) {
    // Default: only show products previously bought from this supplier
    const opts = own.map(p => `<option value="${p.id}">${p.name} (${p.product_type})</option>`).join('');
    return `<option value="">- Select product -</option>${opts}`;
  }
  // Show all products: supplier's own first, then the rest
  const rest = active.filter(p => !supplierProductIds.has(p.id));
  const sep  = own.length ? `<option disabled>── Other products ──</option>` : '';
  const opts = (arr) => arr.map(p => `<option value="${p.id}">${p.name} (${p.product_type})</option>`).join('');
  return `<option value="">- Select product -</option>${opts(own)}${sep}${opts(rest)}`;
}

function addPurchaseLine() {
  const container = document.getElementById('purchase-run-lines');
  if (!container) return;

  const supplierProductIds = new Set(
    (_currentSupplierProducts || []).map(p => p.id)
  );
  const hasSupplierProducts = supplierProductIds.size > 0;

  const line = document.createElement('div');
  line.className = 'border rounded p-2 mb-2';
  line.dataset.lineId = Date.now() + Math.random();

  line.innerHTML = `
    <div class="d-flex gap-2 align-items-center mb-2">
      <span class="small fw-semibold text-muted">Item</span>
      <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" data-create-product-btn>+ Create New Product</button>
      <button class="btn btn-sm btn-outline-danger" data-remove-line><i class="bi bi-x-lg"></i></button>
    </div>
    <div class="mb-2">
      <select class="form-select form-select-sm" data-product-select>
        ${_buildProductOptions(supplierProductIds)}
      </select>
      ${hasSupplierProducts ? `<a href="#" class="d-block mt-1 small text-muted text-end" data-toggle-all-products>Not on list? Show all products</a>` : ''}
    </div>
    <div class="row g-2">
      <div class="col-4"><input type="number" step="0.01" min="0.01" class="form-control form-control-sm" placeholder="Qty" data-qty></div>
      <div class="col-4">
        <select class="form-select form-select-sm" data-unit>
          <option value="unit">unit</option>
          <option value="g">g</option>
          <option value="kg">kg</option>
          <option value="ml">ml</option>
          <option value="L">L</option>
        </select>
      </div>
      <div class="col-4"><input type="number" step="0.01" min="0" class="form-control form-control-sm" placeholder="Total R" data-price></div>
    </div>
    <div class="small mt-1" data-line-addl-preview></div>
  `;

  container.appendChild(line);

  const unitSel = line.querySelector('[data-unit]');
  const UNIT_OPTIONS = {
    weight: [['g','g'],['kg','kg']],
    volume: [['ml','ml'],['L','L']],
    count:  [['unit','unit']],
  };

  function updateUnitsForProduct(pid) {
    const p = STATE.products.find(pr => pr.id === parseInt(pid));
    const type = p ? (p.unit_type || 'count') : null;
    const opts = UNIT_OPTIONS[type] || [['unit','unit'],['g','g'],['kg','kg'],['ml','ml'],['L','L']];
    unitSel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  }

  line.querySelector('[data-product-select]')?.addEventListener('change', e => {
    updateUnitsForProduct(e.target.value);
  });
  line.querySelector('[data-price]')?.addEventListener('input', _updatePurchaseRunCostPreview);

  // "Create New Product" - open the full product editor modal and come back
  line.querySelector('[data-create-product-btn]')?.addEventListener('click', () => {
    _pendingPurchaseLine = line;
    openProductEditor(null);
  });

  line.querySelector('[data-remove-line]')?.addEventListener('click', () => { line.remove(); _updatePurchaseRunCostPreview(); });

  // Toggle between supplier-only products and all products
  const toggleLink = line.querySelector('[data-toggle-all-products]');
  const productSel = line.querySelector('[data-product-select]');
  let _showingAllProducts = false;
  toggleLink?.addEventListener('click', e => {
    e.preventDefault();
    _showingAllProducts = !_showingAllProducts;
    const currentVal = productSel.value;
    productSel.innerHTML = _buildProductOptions(supplierProductIds, _showingAllProducts);
    if (currentVal) productSel.value = currentVal;
    toggleLink.textContent = _showingAllProducts ? 'Show supplier products only' : 'Not on list? Show all products';
  });
}

function _updatePurchaseRunCostPreview() {
  const lines    = [...document.querySelectorAll('#purchase-run-lines [data-line-id]')];
  const addlWrap = document.getElementById('purchase-run-addl-costs-wrap');
  const addlCosts = addlWrap ? _readAdditionalCosts(addlWrap) : [];
  const addlTotal = _addlCostsTotal(addlCosts);

  const linePrices = lines.map(l => parseFloat(l.querySelector('[data-price]')?.value || 0));
  const totalBase  = linePrices.reduce((s, p) => s + p, 0);

  const shares = (() => {
    if (!addlTotal || !lines.length) return linePrices.map(() => 0);
    if (totalBase <= 0) return linePrices.map(() => addlTotal / lines.length);
    const raw  = linePrices.map(p => parseFloat(((p / totalBase) * addlTotal).toFixed(2)));
    const diff = parseFloat((addlTotal - raw.reduce((s, x) => s + x, 0)).toFixed(2));
    raw[raw.length - 1] = parseFloat((raw[raw.length - 1] + diff).toFixed(2));
    return raw;
  })();

  lines.forEach((lineEl, i) => {
    const previewEl = lineEl.querySelector('[data-line-addl-preview]');
    if (!previewEl) return;
    const share = shares[i];
    const price = linePrices[i];
    if (!addlTotal || !share) { previewEl.innerHTML = ''; return; }
    const pct = price > 0 ? Math.round((share / price) * 100) : 0;
    previewEl.innerHTML = addlCosts.map(c => {
      const cShare = parseFloat(((c.amount / addlTotal) * share).toFixed(2));
      return `<span class="badge bg-secondary me-1" style="font-size:10px">${_COST_TYPE_LABELS[c.type] || c.type} +R${cShare.toFixed(2)}</span>`;
    }).join('') + `<span class="text-warning small">(+${pct}% overhead)</span>`;
  });
}

document.getElementById('btn-submit-purchase-run')?.addEventListener('click', async () => {
  if (!_currentSupplier) return toast('No supplier selected', 'error');

  const container = document.getElementById('purchase-run-lines');
  const lineElements = container.querySelectorAll('[data-line-id]');

  const lines = [];
  for (const lineEl of lineElements) {
    const productId = parseInt(lineEl.querySelector('[data-product-select]')?.value || 0);
    const qty       = parseFloat(lineEl.querySelector('[data-qty]')?.value || 0);
    const price     = parseFloat(lineEl.querySelector('[data-price]')?.value || 0);
    const unit      = lineEl.querySelector('[data-unit]')?.value || 'unit';

    if (!productId) return toast('Please select a product for all lines', 'warning');
    if (qty <= 0)   return toast('Quantity must be greater than 0', 'warning');
    if (price < 0)  return toast('Price cannot be negative', 'warning');
    lines.push({ product_id: productId, qty, unit, total_price: price });
  }

  if (lines.length === 0) return toast('Add at least one item', 'warning');

  const dateVal     = document.getElementById('purchase-run-date')?.value;
  const prAddlWrap  = document.getElementById('purchase-run-addl-costs-wrap');
  const prAddlCosts = prAddlWrap ? _readAdditionalCosts(prAddlWrap) : [];
  if (prAddlCosts.length) {
    const baseTotal = lines.reduce((s, l) => s + l.total_price, 0);
    _checkOverheadWarning(_addlCostsTotal(prAddlCosts), baseTotal);
  }
  const body = { lines, date: dateVal || todayISO(), additional_costs: prAddlCosts };

  try {
    const result = await api(`/api/suppliers/${_currentSupplier.id}/purchase_run`, {
      method: 'POST',
      body: JSON.stringify(body)
    });

    let msg = `Purchase run saved: ${result.batches_created} batches created`;
    if (result.created_products?.length > 0) {
      msg += `, ${result.created_products.length} new products created`;
    }
    toast(msg, 'success', 5000);

    hide(document.getElementById('purchase-run-panel'));
    _purchaseRunLines = [];

    // Reload products and supplier products
    await loadProducts();
    await loadSupplierProducts(_currentSupplier.id);
  } catch (e) {
    toast(e.message, 'error');
  }
});

// Quick-add supplier from receive modal
document.getElementById('btn-quick-add-supplier')?.addEventListener('click', () => {
  const form = document.getElementById('quick-supplier-form');
  if (form) form.classList.toggle('hidden');
});

document.getElementById('btn-quick-sup-save')?.addEventListener('click', async () => {
  const name  = document.getElementById('quick-sup-name').value.trim();
  const phone = document.getElementById('quick-sup-phone').value.trim();
  const email = document.getElementById('quick-sup-email').value.trim();
  if (!name) return toast('Supplier name required', 'warning');
  try {
    const j = await api('/api/suppliers', { method: 'POST', body: JSON.stringify({ name, phone, email }) });
    await loadSuppliers();
    // Select the new supplier in the dropdown
    const sel = document.getElementById('receive-supplier');
    if (sel) sel.value = j.id || '';
    // Hide the form
    document.getElementById('quick-supplier-form')?.classList.add('hidden');
    document.getElementById('quick-sup-name').value  = '';
    document.getElementById('quick-sup-phone').value = '';
    document.getElementById('quick-sup-email').value = '';
    toast(`Supplier "${name}" added`, 'success');
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// ADJUSTMENT HISTORY
// ═══════════════════════════════════════════════════════
(function initAdjDateFilter() {
  const el = document.getElementById('adj-date-filter');
  if (el && !el.value) el.value = todayISO();
})();

document.getElementById('btn-load-adjustments')?.addEventListener('click', loadAdjustments);

async function loadAdjustments() {
  const type    = document.getElementById('adj-type-filter')?.value;
  const dateVal = document.getElementById('adj-date-filter')?.value;
  const host    = document.getElementById('adjustments-list');
  if (!host) return;

  try {
    const params = new URLSearchParams();
    if (type)    params.set('type', type);
    if (dateVal) { params.set('start', dateVal); params.set('end', dateVal); }
    const data = await api(`/api/stock/adjustments?${params.toString()}`);

    if (data.length === 0) {
      host.innerHTML = '<div class="text-muted">No adjustments found.</div>';
      return;
    }

    host.innerHTML = '';
    data.forEach(r => {
      const isWriteoff = r.adjustment_type === 'writeoff';
      const sign       = r.qty_change_base >= 0 ? '+' : '';
      const colour     = isWriteoff ? 'text-danger' : (r.qty_change_base < 0 ? 'text-warning' : 'text-success');
      const typeLabel  = isWriteoff ? '<i class="bi bi-trash3 me-1"></i>Write-off' : '<i class="bi bi-clipboard-check me-1"></i>Stocktake';
      const costStr    = isWriteoff && r.cost_written_off != null ? `R${fmt(r.cost_written_off)} written off` : '';

      const row = document.createElement('div');
      row.className = 'border-bottom py-2 d-flex justify-content-between align-items-start gap-2';
      row.innerHTML = `
        <div class="flex-fill">
          <span class="badge bg-light text-dark me-1">${typeLabel}</span>
          <strong>${r.product_name}</strong>
          <span class="${colour} ms-2 fw-semibold">${sign}${displayQty(Math.abs(r.qty_change_base), null)}${r.base_unit}</span>
          ${costStr ? `<span class="text-danger small ms-2">(${costStr})</span>` : ''}
          <div class="text-muted small mt-1">${r.reason}</div>
        </div>
        <div class="text-end text-muted small flex-shrink-0">
          <div>${new Date(r.adjusted_at).toLocaleString('en-ZA')}</div>
          ${r.adjusted_by ? `<div>${r.adjusted_by}</div>` : ''}
          ${isWriteoff ? `<button class="btn btn-outline-secondary btn-sm mt-1 py-0 px-2" data-editwo-id="${r.id}" data-editwo-product="${r.product_name}" data-editwo-qty="${Math.abs(r.qty_change_base)}" data-editwo-unit="${r.base_unit}" data-editwo-reason="${r.reason}" data-editwo-unit-type=""><i class="bi bi-pencil me-1"></i>Edit</button>` : ''}
          <button class="btn btn-outline-danger btn-sm mt-1 py-0 px-2"
            data-delete-adj-id="${r.id}"
            data-delete-adj-product="${r.product_name}"
            data-delete-adj-type="${r.adjustment_type}"
            data-delete-adj-qty="${Math.abs(r.qty_change_base)}"
            data-delete-adj-unit="${r.base_unit}"
            data-delete-adj-sign="${r.qty_change_base >= 0 ? '+' : '-'}"><i class="bi bi-trash"></i></button>
        </div>
      `;
      host.appendChild(row);
    });

    // Wire up edit buttons
    host.querySelectorAll('[data-editwo-id]').forEach(btn => {
      btn.addEventListener('click', () => openEditWriteoffModal({
        id:          btn.dataset.editwoId,
        product_name: btn.dataset.editwoProduct,
        qty_base:    parseFloat(btn.dataset.editwoQty),
        base_unit:   btn.dataset.editwoUnit,
        reason:      btn.dataset.editwoReason,
        product_id:  null, // looked up in modal open
      }));
    });
  } catch (e) { toast(e.message, 'error'); }
}

// ── Edit Write-off ──
let _editWoAdj = null;

function openEditWriteoffModal(r) {
  _editWoAdj = r;
  document.getElementById('editwo-adj-id').value  = r.id;
  document.getElementById('editwo-product-name').textContent = `Product: ${r.product_name}  ·  Original write-off: ${displayQty(r.qty_base, null)}${r.base_unit}`;
  document.getElementById('editwo-reason').value  = r.reason || '';
  hide(document.getElementById('editwo-preview'));

  // Find the product in STATE to build unit options
  const prod = STATE.products.find(p => p.name === r.product_name);
  const unitSel = document.getElementById('editwo-unit');
  unitSel.innerHTML = '';
  if (prod) {
    buildUnitOptions(prod.unit_type, prod.package_size, prod.package_unit).forEach(o => {
      const opt = document.createElement('option');
      opt.value = o.value; opt.textContent = o.label; opt.dataset.conv = o.conv;
      if (o.value === r.base_unit) opt.selected = true;
      unitSel.appendChild(opt);
    });
    _editWoAdj._prod = prod;
  } else {
    // Fallback: just show the base unit
    const opt = document.createElement('option');
    opt.value = r.base_unit; opt.textContent = r.base_unit; opt.dataset.conv = '1';
    unitSel.appendChild(opt);
  }

  // Pre-fill qty in the base unit
  document.getElementById('editwo-qty').value = r.qty_base;

  bootstrap.Modal.getOrCreateInstance(document.getElementById('editWriteoffModal')).show();
  setTimeout(() => document.getElementById('editwo-qty').focus(), 300);
}

document.getElementById('editwo-qty')?.addEventListener('input', () => {
  if (!_editWoAdj) return;
  const qty  = parseFloat(document.getElementById('editwo-qty').value) || 0;
  const sel  = document.getElementById('editwo-unit');
  const conv = parseFloat(sel.options[sel.selectedIndex]?.dataset?.conv || 1);
  const newBase = qty * conv;
  const oldBase = _editWoAdj.qty_base;
  const diff = newBase - oldBase;
  const preview = document.getElementById('editwo-preview');
  if (!qty) { hide(preview); return; }
  show(preview);
  if (Math.abs(diff) < 0.001) {
    preview.className = 'alert alert-success py-2 small';
    preview.innerHTML = '<i class="bi bi-check-lg me-1"></i>Same quantity - no change to stock';
  } else if (diff > 0) {
    preview.className = 'alert alert-warning py-2 small';
    const prod = _editWoAdj._prod;
    preview.textContent = `Will write off an additional ${displayQty(diff, prod?.unit_type)}${_editWoAdj.base_unit}`;
  } else {
    preview.className = 'alert alert-info py-2 small';
    const prod = _editWoAdj._prod;
    preview.textContent = `Will restore ${displayQty(Math.abs(diff), prod?.unit_type)}${_editWoAdj.base_unit} back to stock`;
  }
});
document.getElementById('editwo-unit')?.addEventListener('change', () => document.getElementById('editwo-qty')?.dispatchEvent(new Event('input')));

document.getElementById('btn-editwo-confirm')?.addEventListener('click', async () => {
  if (!_editWoAdj) return;
  const adjId  = document.getElementById('editwo-adj-id').value;
  const qty    = parseFloat(document.getElementById('editwo-qty').value || '');
  const unit   = document.getElementById('editwo-unit').value;
  const reason = document.getElementById('editwo-reason').value.trim();
  if (!qty || qty <= 0) return toast('Enter a valid quantity', 'warning');
  try {
    await api(`/api/stock/adjustments/${adjId}`, {
      method: 'PATCH',
      body: JSON.stringify({ qty, unit, reason }),
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editWriteoffModal')).hide();
    toast('Write-off corrected', 'success', 2500);
    await loadAdjustments();
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
});

// ── Delete Adjustment ──
document.getElementById('adjustments-list')?.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-delete-adj-id]');
  if (!btn) return;
  const adjId   = btn.dataset.deleteAdjId;
  const product = btn.dataset.deleteAdjProduct;
  const type    = btn.dataset.deleteAdjType;
  const qty     = btn.dataset.deleteAdjQty;
  const unit    = btn.dataset.deleteAdjUnit;
  const sign    = btn.dataset.deleteAdjSign;
  const action  = sign === '+' ? 'consume back via FIFO' : 'restore to stock';
  if (!confirm(`Delete this ${type} for "${product}" (${sign}${qty}${unit})?\n\nThis will ${action}. Cannot be undone.`)) return;
  try {
    await api(`/api/stock/adjustments/${adjId}`, { method: 'DELETE' });
    toast('Adjustment deleted and stock reversed', 'success', 2500);
    await loadAdjustments();
    await loadIngredients();
  } catch (err) { toast(err.message || 'Delete failed', 'danger'); }
});

// ═══════════════════════════════════════════════════════
// CART
// ═══════════════════════════════════════════════════════
function renderCart() {
  const host = document.getElementById('cart'); if (!host) return;
  host.innerHTML = '';
  let total = 0;
  const _admin = isAdmin();

  Object.values(STATE.cart).forEach(item => {
    const row = document.createElement('div');
    row.className = 'list-group-item px-2 py-2';

    const basePrice       = item.is_weight ? parseFloat(item._display_total || 0) : parseFloat(item.unit_price);
    const discountedPrice = applyItemDiscount(basePrice, item._discount);
    const hasDiscount     = item._discount && discountedPrice < basePrice;
    const prod = STATE.products.find(pr => pr.id === item.product_id);

    // Top row: thumbnail + label + price
    const topRow = document.createElement('div');
    topRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:6px';

    if (prod?.image_url) {
      const img = document.createElement('img');
      img.src = imgVariant(prod.image_url, 'thumb');
      img.loading = 'lazy'; img.decoding = 'async';
      img.style.cssText = 'width:36px;height:36px;object-fit:cover;border-radius:4px;flex-shrink:0';
      topRow.appendChild(img);
    }

    const label = item.is_weight ? item.name : `${item.name} × ${fmtQty(item.qty)}`;
    const labelSpan = document.createElement('span');
    labelSpan.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.9rem';
    labelSpan.innerHTML = label + (hasDiscount
      ? ` <span class="text-muted text-decoration-line-through small">R${fmt(basePrice)}</span>`
      : '');
    topRow.appendChild(labelSpan);

    const priceSpan = document.createElement('span');
    priceSpan.style.cssText = 'white-space:nowrap;font-weight:600;font-size:.9rem';
    priceSpan.className = hasDiscount ? 'text-success' : '';
    priceSpan.textContent = `R${fmt(discountedPrice)}`;
    if (hasDiscount) {
      priceSpan.title = item._discount.type === 'pct'
        ? `${item._discount.value}% off`
        : `R${fmt(item._discount.value)} off`;
    }
    topRow.appendChild(priceSpan);

    // Bottom row: action buttons
    const btns = document.createElement('div');
    btns.style.cssText = 'display:flex;flex-wrap:wrap;gap:4px';

    if (!item.is_weight) {
      const pricePerUnit = (item.subs || item.extras?.length)
        ? item.unit_price
        : parseFloat(prod?.price || 0);

      const plus = document.createElement('button'); plus.textContent = '+'; plus.className = 'btn btn-sm btn-outline-primary';
      plus.onclick = () => {
        item.qty += 1;
        if (!(item.subs || item.extras?.length)) item.unit_price = pricePerUnit * item.qty;
        item._special_applied = null;
        STATE.scanHistory.push(item.product_id);
        reapplySpecials();
      };
      const minus = document.createElement('button'); minus.textContent = '−'; minus.className = 'btn btn-sm btn-outline-secondary';
      minus.onclick = () => {
        item.qty = Math.max(1, item.qty - 1);
        if (!(item.subs || item.extras?.length)) item.unit_price = pricePerUnit * item.qty;
        item._special_applied = null;
        reapplySpecials();
      };
      btns.appendChild(plus); btns.appendChild(minus);

      if (prod?.product_type === 'recipe') {
        const cust = document.createElement('button');
        cust.textContent = 'Customise';
        cust.className = 'btn btn-sm btn-outline-info';
        cust.onclick = () => openSubsModal(prod, item._key);
        btns.appendChild(cust);
      }
    }

    if (_admin) {
      const discBtn = document.createElement('button');
      discBtn.className = 'btn btn-sm ' + (hasDiscount ? 'btn-success' : 'btn-outline-success');
      discBtn.innerHTML = hasDiscount ? '%<i class="bi bi-check-lg ms-1"></i>' : '%';
      discBtn.title = hasDiscount ? 'Edit item discount' : 'Add item discount';
      discBtn.onclick = () => openDiscountModal(item._key);
      btns.appendChild(discBtn);
    }

    const del = document.createElement('button'); del.textContent = 'Remove'; del.className = 'btn btn-sm btn-outline-danger';
    del.onclick = () => { delete STATE.cart[item._key]; renderCart(); };
    btns.appendChild(del);

    row.appendChild(topRow); row.appendChild(btns);
    host.appendChild(row);
    total += discountedPrice;
  });

  // Apply cart-wide discount
  const cartDisc = STATE._cartDiscount;
  const finalTotal = applyItemDiscount(total, cartDisc);
  const t = document.getElementById('cart-total');
  if (t) t.textContent = fmt(finalTotal);

  // Show/hide cart discount label
  const discLabel = document.getElementById('cart-discount-label');
  if (discLabel) {
    if (cartDisc && finalTotal < total) {
      const saving = total - finalTotal;
      const desc   = cartDisc.type === 'pct' ? `${cartDisc.value}% cart discount` : `R${fmt(cartDisc.value)} cart discount`;
      discLabel.textContent = `−R${fmt(saving)} (${desc})`;
      show(discLabel);
    } else {
      hide(discLabel);
    }
  }
}

function addToCart(p) {
  if (p.sold_by_weight) {
    openWeightModal(p);
    return;
  }
  // Fixed price product (recipes included - customise button in cart)
  const key      = String(p.id);
  const existing = STATE.cart[key];
  if (existing && !existing.subs && !existing.extras) {
    existing.qty += 1;
    existing.unit_price = parseFloat(p.price || 0) * existing.qty;
    toast(`${p.name} ×${existing.qty}`, 'info', 1200);
  } else if (!existing) {
    STATE.cart[key] = {
      _key: key, product_id: p.id, name: p.name,
      unit_price: parseFloat(p.price || 0), qty: 1,
      is_weight: false
    };
    toast(`Added: ${p.name}`, 'success', 1200);
  } else {
    // Customised entry already exists - add a fresh uncustomised one with unique key
    const newKey = `${p.id}__${Date.now()}`;
    STATE.cart[newKey] = {
      _key: newKey, product_id: p.id, name: p.name,
      unit_price: parseFloat(p.price || 0), qty: 1,
      is_weight: false
    };
    toast(`Added: ${p.name}`, 'success', 1200);
  }
  _bumpCartCount(p.id);
  STATE.scanHistory.push(p.id);
  renderCart();
  detectAndOfferSpecials();
  const srch = document.getElementById('search');
  if (srch && srch.value) { srch.value = ''; renderTellerGrid(); }
}

function addToCartQty(p, qty) {
  if (!qty || qty <= 0) return;
  const key = String(p.id);
  const existing = STATE.cart[key];
  const newQty = existing ? existing.qty + qty : qty;
  STATE.cart[key] = {
    ...(existing || {}),
    _key: key, product_id: p.id, name: p.name,
    unit_price: parseFloat(p.price || 0) * newQty, qty: newQty, is_weight: false,
  };
  toast(`${p.name} ×${qty}`, 'success', 1500);
  _bumpCartCount(p.id);
  STATE.scanHistory.push(p.id);
  renderCart();
  detectAndOfferSpecials();
  const srch = document.getElementById('search');
  if (srch && srch.value) { srch.value = ''; renderTellerGrid(); }
}

// ── Undo last scan ──
document.getElementById('btn-undo-last')?.addEventListener('click', () => {
  if (STATE.scanHistory.length === 0) { toast('Nothing to undo', 'warning'); return; }
  const lastId = STATE.scanHistory.pop();

  // Find the most recently added cart item for this product
  const matchingKeys = Object.keys(STATE.cart).filter(k => STATE.cart[k].product_id === lastId);
  if (matchingKeys.length === 0) return;
  const key  = matchingKeys[matchingKeys.length - 1];
  const item = STATE.cart[key];
  if (!item) return;

  if (item.is_weight) {
    delete STATE.cart[key];
    toast(`Removed: ${item.name}`, 'warning', 1500);
  } else if (item.qty > 1) {
    item.qty -= 1;
    item.unit_price = parseFloat(STATE.products.find(p => p.id === lastId)?.price || 0) * item.qty;
    toast(`Undone: ${item.name} ×${item.qty}`, 'warning', 1500);
  } else {
    delete STATE.cart[key];
    toast(`Removed: ${item.name}`, 'warning', 1500);
  }
  renderCart();
});

// ── Discounts (admin only) ──
// _discountTarget = null (cart-wide) or a cart item key (per-item)
let _discountTarget = null;

function openDiscountModal(itemKey) {
  if (!isAdmin()) return;
  _discountTarget = itemKey || null;

  const isCart  = _discountTarget === null;
  const item    = isCart ? null : STATE.cart[_discountTarget];
  const current = isCart ? STATE._cartDiscount : item?._discount;

  document.getElementById('discount-modal-title').textContent = isCart ? 'Cart Discount' : `Discount - ${item?.name}`;
  document.getElementById('discount-modal-desc').textContent  = isCart
    ? 'Apply a discount to the entire cart total.'
    : `Discounting: ${item?.name}`;

  // Restore previous discount values if any
  const typePct = document.getElementById('discount-type-pct');
  const typeAmt = document.getElementById('discount-type-amt');
  if (current) {
    (current.type === 'pct' ? typePct : typeAmt).checked = true;
    document.getElementById('discount-value').value = current.value;
  } else {
    typePct.checked = true;
    document.getElementById('discount-value').value = '';
  }
  updateDiscountSymbol();
  updateDiscountPreview();

  const removeBtn = document.getElementById('btn-remove-discount');
  current ? show(removeBtn) : hide(removeBtn);

  bootstrap.Modal.getOrCreateInstance(document.getElementById('discountModal')).show();
  setTimeout(() => document.getElementById('discount-value').focus(), 300);
}

function updateDiscountSymbol() {
  const type = document.querySelector('input[name="discount-type"]:checked')?.value || 'pct';
  document.getElementById('discount-symbol').textContent = type === 'pct' ? '%' : 'R';
  updateDiscountPreview();
}

function updateDiscountPreview() {
  const type  = document.querySelector('input[name="discount-type"]:checked')?.value || 'pct';
  const val   = parseFloat(document.getElementById('discount-value').value) || 0;
  const prev  = document.getElementById('discount-preview');
  if (!val) { prev.textContent = ''; return; }

  const isCart = _discountTarget === null;
  const base   = isCart
    ? parseFloat(document.getElementById('cart-total').textContent || '0')
    : (() => {
        const item = STATE.cart[_discountTarget];
        return item ? (item.is_weight ? parseFloat(item._display_total || 0) : parseFloat(item.unit_price)) : 0;
      })();

  const saving = type === 'pct' ? base * val / 100 : Math.min(val, base);
  prev.textContent = saving > 0 ? `Saves R${fmt(saving)} → R${fmt(base - saving)}` : '';
}

document.getElementById('discount-value')?.addEventListener('input', updateDiscountPreview);
document.querySelectorAll('input[name="discount-type"]').forEach(r => r.addEventListener('change', updateDiscountSymbol));

document.getElementById('btn-apply-discount')?.addEventListener('click', () => {
  const type = document.querySelector('input[name="discount-type"]:checked')?.value || 'pct';
  const val  = parseFloat(document.getElementById('discount-value').value);
  if (!val || val <= 0) return toast('Enter a discount value', 'warning');
  if (type === 'pct' && val > 100) return toast('Percentage cannot exceed 100%', 'warning');

  const discount = { type, value: val };

  if (_discountTarget === null) {
    // Cart-wide discount
    STATE._cartDiscount = discount;
  } else {
    // Per-item discount
    const item = STATE.cart[_discountTarget];
    if (!item) return;
    item._discount = discount;
  }

  bootstrap.Modal.getOrCreateInstance(document.getElementById('discountModal')).hide();
  renderCart();
  toast('Discount applied', 'success', 1500);
});

document.getElementById('btn-remove-discount')?.addEventListener('click', () => {
  if (_discountTarget === null) {
    STATE._cartDiscount = null;
  } else {
    const item = STATE.cart[_discountTarget];
    if (item) delete item._discount;
  }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('discountModal')).hide();
  renderCart();
  toast('Discount removed', 'warning', 1500);
});

document.getElementById('btn-cart-discount')?.addEventListener('click', () => openDiscountModal(null));

// Calculate discounted price for a single cart item
function applyItemDiscount(basePrice, discount) {
  if (!discount) return basePrice;
  const saving = discount.type === 'pct'
    ? basePrice * discount.value / 100
    : Math.min(discount.value, basePrice);
  return Math.max(0, basePrice - saving);
}

// ── Checkout ──
document.getElementById('btn-checkout')?.addEventListener('click', async () => {
  const cart = Object.values(STATE.cart);
  if (cart.length === 0) return toast('Cart is empty', 'warning');

  // Pre-calculate cart subtotal for cart-wide discount pro-ration
  const cartSubtotal = cart.reduce((sum, item) => {
    const base = item.is_weight ? parseFloat(item._display_total || 0) : parseFloat(item.unit_price);
    return sum + applyItemDiscount(base, item._discount);
  }, 0);

  const payload = cart.map(item => {
    // Get base unit price (per-unit for backend)
    const baseTotalPrice = item.is_weight ? parseFloat(item._display_total || 0) : parseFloat(item.unit_price);
    const baseUnitPrice  = (item.is_weight || item.subs || item.extras?.length)
      ? item.unit_price
      : item.unit_price / item.qty;

    // Apply per-item discount to unit price
    const afterItemDisc = item._discount
      ? applyItemDiscount(baseTotalPrice, item._discount) / (item.is_weight ? 1 : item.qty)
      : baseUnitPrice;

    // Apply cart-wide discount pro-rata across items
    let finalUnitPrice = afterItemDisc;
    if (STATE._cartDiscount && cartSubtotal > 0) {
      const itemShare  = (item.is_weight ? parseFloat(item._display_total || 0) : applyItemDiscount(baseTotalPrice, item._discount)) / cartSubtotal;
      const cartSaving = cartSubtotal - applyItemDiscount(cartSubtotal, STATE._cartDiscount);
      finalUnitPrice   = Math.max(0, afterItemDisc - (cartSaving * itemShare) / (item.is_weight ? 1 : item.qty));
    }

    return {
      product_id:    item.product_id,
      qty:           item.qty,
      unit_price:    finalUnitPrice,
      ...(item.subs            ? { subs:           item.subs                                      } : {}),
      ...(item.extras          ? { extras:         item.extras                                    } : {}),
      ...(item._discount       ? { item_discount:  item._discount                                 } : {}),
      ...(item._special_applied ? { special_name: (STATE.specials.find(s => s.id === item._special_applied)?.name || '') } : {}),
    };
  });

  const payMethod = document.querySelector('input[name="pay-method"]:checked')?.value || 'card';

  // Split payment validation
  let cashTendered = null, cardAmount = null;
  if (payMethod === 'split') {
    cashTendered = parseFloat(document.getElementById('split-cash-input')?.value) || 0;
    cardAmount   = parseFloat(document.getElementById('split-card-input')?.value) || 0;
    const splitTotal = Math.round((cashTendered + cardAmount) * 100);
    const cartTotal  = Math.round(cartSubtotal * 100);
    if (splitTotal < cartTotal) {
      return toast(`Split total R${fmt(cashTendered + cardAmount)} is less than cart total R${fmt(cartSubtotal)}`, 'warning');
    }
    if (cashTendered <= 0 || cardAmount <= 0) {
      return toast('Enter amounts for both Cash and Card in a split payment', 'warning');
    }
  }

  const requestBody = {
    cart: payload,
    payment_method: payMethod,
    ...(cashTendered != null        ? { cash_tendered: cashTendered } : {}),
    ...(cardAmount != null          ? { card_amount:   cardAmount   } : {}),
    ...(STATE.activeCustomer?.customer_id ? { customer_id:   STATE.activeCustomer.customer_id } : {}),
    ...(STATE._cartDiscount               ? { cart_discount: STATE._cartDiscount               } : {}),
  };

  try {
    const j = await api('/api/transactions', { method: 'POST', body: JSON.stringify(requestBody) });
    STATE.cart = {}; STATE.scanHistory = []; STATE._cartDiscount = null; renderCart();
    // Reset split inputs for the next sale
    document.getElementById('split-cash-input').value = '';
    document.getElementById('split-card-input').value = '';
    document.getElementById('split-balance').textContent = '';
    await loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    await loadProducts();
    const kitchenMsg = j.kitchen_orders > 0 ? ` - ${j.kitchen_orders} kitchen order${j.kitchen_orders > 1 ? 's' : ''} queued` : '';
    toast(`Sale complete - #${String(j.transaction_id).slice(0,8)}${kitchenMsg}`, 'success', 4000);
    // Auto-print receipt if enabled in settings
    try {
      const s = await api('/api/settings');
      if (s.auto_print_receipt === 'true') printReceipt(j.transaction_id);
    } catch(e) { /* non-blocking */ }
    if (j.kitchen_orders > 0) {
      // Update badge immediately
      const badge = document.getElementById('kitchen-badge');
      if (badge) {
        const current = parseInt(badge.textContent || '0') + j.kitchen_orders;
        badge.textContent = current;
        show(badge);
      }
      // Refresh queue if kitchen tab is open
      const kitchenPane = document.getElementById('kitchen');
      if (kitchenPane?.classList.contains('active')) loadKitchenOrders();
    }
  } catch (e) { toast(e.message, 'error'); }
});

// ── Split payment UI ─────────────────────────────────────────────────────────
document.querySelectorAll('input[name="pay-method"]').forEach(radio => {
  radio.addEventListener('change', () => {
    const splitRow = document.getElementById('split-payment-row');
    if (!splitRow) return;
    if (radio.value === 'split' && radio.checked) {
      splitRow.classList.remove('hidden');
      // Pre-fill: put full total into card, 0 into cash so teller only edits one field
      const total = parseFloat(document.getElementById('cart-total')?.textContent || '0');
      const cashEl = document.getElementById('split-cash-input');
      const cardEl = document.getElementById('split-card-input');
      if (cashEl && cardEl && !cashEl.value && !cardEl.value) {
        cashEl.value = '';
        cardEl.value = total > 0 ? fmt(total) : '';
      }
      _updateSplitBalance();
      document.getElementById('split-cash-input')?.focus();
    } else if (radio.checked) {
      splitRow.classList.add('hidden');
      document.getElementById('split-cash-input').value = '';
      document.getElementById('split-card-input').value = '';
    }
  });
});

function _updateSplitBalance() {
  const total     = parseFloat(document.getElementById('cart-total')?.textContent || '0');
  const cash      = parseFloat(document.getElementById('split-cash-input')?.value) || 0;
  const card      = parseFloat(document.getElementById('split-card-input')?.value) || 0;
  const remaining = Math.round((total - cash - card) * 100) / 100;
  const el = document.getElementById('split-balance');
  if (!el) return;
  if (remaining > 0.001) {
    el.textContent = `R${fmt(remaining)} still unallocated`;
    el.className = 'small text-danger';
  } else if (remaining < -0.001) {
    el.textContent = `R${fmt(Math.abs(remaining))} over total`;
    el.className = 'small text-warning';
  } else {
    el.innerHTML = '<i class="bi bi-check-lg me-1"></i>Balanced';
    el.className = 'small text-success';
  }
}

// Auto-fill the other field when one is edited
document.getElementById('split-cash-input')?.addEventListener('input', () => {
  const total = parseFloat(document.getElementById('cart-total')?.textContent || '0');
  const cash  = parseFloat(document.getElementById('split-cash-input').value) || 0;
  const cardEl = document.getElementById('split-card-input');
  const remainder = Math.round((total - cash) * 100) / 100;
  cardEl.value = remainder >= 0 ? fmt(remainder) : '0.00';
  _updateSplitBalance();
});

document.getElementById('split-card-input')?.addEventListener('input', () => {
  const total = parseFloat(document.getElementById('cart-total')?.textContent || '0');
  const card  = parseFloat(document.getElementById('split-card-input').value) || 0;
  const cashEl = document.getElementById('split-cash-input');
  const remainder = Math.round((total - card) * 100) / 100;
  cashEl.value = remainder >= 0 ? fmt(remainder) : '0.00';
  _updateSplitBalance();
});

document.getElementById('split-reset-btn')?.addEventListener('click', () => {
  document.getElementById('split-cash-input').value = '';
  document.getElementById('split-card-input').value = '';
  _updateSplitBalance();
  document.getElementById('split-cash-input')?.focus();
});

// ═══════════════════════════════════════════════════════
// WEIGHT ENTRY MODAL
// ═══════════════════════════════════════════════════════
let _weightProduct = null;

function openWeightModal(p) {
  _weightProduct = p;
  document.getElementById('weight-modal-title').textContent = p.name;
  document.getElementById('weight-qty').value                = '';
  document.getElementById('weight-price-preview').textContent = '';

  // Show the PLU the teller must key into the scale for this weighed product
  const pluCallout = document.getElementById('weight-plu-callout');
  const pluNumber  = document.getElementById('weight-plu-number');
  if (pluCallout && pluNumber) {
    if (p.product_code != null) {
      pluNumber.textContent = p.product_code;
      pluCallout.classList.remove('d-none');
    } else {
      pluCallout.classList.add('d-none');
    }
  }

  const unitSel = document.getElementById('weight-unit');
  unitSel.innerHTML = '';
  const opts = buildUnitOptions(p.unit_type || 'weight', p.package_size, p.package_unit);
  opts.forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value; opt.textContent = o.label;
    opt.dataset.conv = o.conv;
    unitSel.appendChild(opt);
  });

  bootstrap.Modal.getOrCreateInstance(document.getElementById('weightModal')).show();
  setTimeout(() => document.getElementById('weight-qty').focus(), 300);
}

function updateWeightPreview() {
  if (!_weightProduct) return;
  const qty       = parseFloat(document.getElementById('weight-qty').value) || 0;
  const unitSel   = document.getElementById('weight-unit');
  const selectedOpt = unitSel.options[unitSel.selectedIndex];
  const conv      = parseFloat(selectedOpt?.dataset?.conv || 1);
  const qty_base  = qty * conv;
  const pricePerUnit = parseFloat(_weightProduct.price_per_unit || 0);
  const total     = qty_base * pricePerUnit;
  const preview   = document.getElementById('weight-price-preview');
  preview.textContent = qty_base > 0
    ? `${displayQty(qty_base, _weightProduct.unit_type || 'weight')} = R${fmt(total)}`
    : '';
}

document.getElementById('weight-qty')?.addEventListener('input', updateWeightPreview);
document.getElementById('weight-unit')?.addEventListener('change', updateWeightPreview);

document.getElementById('btn-weight-add')?.addEventListener('click', () => {
  if (!_weightProduct) return;
  const qty     = parseFloat(document.getElementById('weight-qty').value) || 0;
  const unitSel = document.getElementById('weight-unit');
  const selectedOpt = unitSel.options[unitSel.selectedIndex];
  const conv    = parseFloat(selectedOpt?.dataset?.conv || 1);
  const qty_base = qty * conv;

  if (qty_base <= 0) return toast('Enter a valid quantity', 'warning');

  const pricePerUnit = parseFloat(_weightProduct.price_per_unit || 0);
  const total        = qty_base * pricePerUnit;
  const label        = `${_weightProduct.name} ${displayQty(qty_base, _weightProduct.unit_type || 'weight')}`;
  const key          = `${_weightProduct.id}_${Date.now()}`;

  STATE.cart[key] = {
    _key:       key,
    product_id: _weightProduct.id,
    name:       label,
    unit_price: pricePerUnit,   // price per base unit - backend multiplies by qty
    qty:        qty_base,
    is_weight:  true,
    _display_total: total,      // for cart display only
  };
  _bumpCartCount(_weightProduct.id);
  STATE.scanHistory.push(_weightProduct.id);
  renderCart();
  detectAndOfferSpecials();
  toast(`Added: ${label} - R${fmt(total)}`, 'success', 1500);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('weightModal')).hide();
});

// ═══════════════════════════════════════════════════════
// TELLER SEARCH
// ═══════════════════════════════════════════════════════
document.getElementById('search')?.addEventListener('input', function() {
  renderTellerGrid(this.value);
});

// ═══════════════════════════════════════════════════════
// USB / BLUETOOTH BARCODE SCANNER (keyboard wedge)
// ═══════════════════════════════════════════════════════
// Variable weight barcode parser (BC-4000 scale labels)
// Format: PP IIII VVVVVV C  (13 digits) - confirmed from printed label
//   PP     = prefix 20 (variable weight)
//   IIII   = product_code (4 digits, e.g. 0007 = product_code 7)
//   VVVVVV = total price in cents (e.g. 003072 = R30.72)
//   C      = EAN-13 check digit
//   Weight is derived: total_price_rands / price_per_unit_rands_per_g
function handleScannedCode(code) {
  // Variable weight scale label: 13 digits starting with 20
  const isScaleLabel = /^20\d{11}$/.test(code) && parseInt(code.substring(6, 12), 10) > 0;
  if (isScaleLabel) {
    const productCode = parseInt(code.substring(2, 6), 10);         // 4-digit item code
    const totalCents  = parseInt(code.substring(6, 12), 10);        // 6-digit price in cents
    const totalRands  = totalCents / 100;
    const p = STATE.products.find(x => x.product_code === productCode && x.sold_by_weight);
    if (p && totalRands > 0) {
      const pricePerUnit = parseFloat(p.price_per_unit || 0);       // R per gram
      const qty_base = pricePerUnit > 0 ? totalRands / pricePerUnit : 0;  // grams
      if (qty_base <= 0) { toast(`Cannot calculate weight for ${p.name}`, 'warning'); return false; }
      const total = totalRands;
      const label = `${p.name} ${displayQty(qty_base, p.unit_type || 'weight')}`;
      const key = `${p.id}_${Date.now()}`;
      STATE.cart[key] = {
        _key: key, product_id: p.id, name: label,
        unit_price: pricePerUnit, qty: qty_base,
        is_weight: true, _display_total: total,
      };
      STATE.scanHistory.push(p.id);
      renderCart();
      detectAndOfferSpecials();
      beep(80, 880); flashOK();
      toast(`Added: ${label} - R${fmt(total)}`, 'success', 1500);
      _releaseScanFocus();
      return true;
    }
  }
  // Fixed barcode or PLU number lookup
  const p = STATE.products.find(x => x.barcode === code)
         || STATE.products.find(x => String(x.id) === code)
         || STATE.products.find(x => x.name.toLowerCase() === code.toLowerCase());
  if (p) { beep(80, 880); flashOK(); addToCart(p); _releaseScanFocus(); return true; }
  toast(`Barcode not found: ${code}`, 'warning');
  return false;
}

// After a scan on non-teller tabs, blur any input that grabbed focus so the next
// scan is caught by the global keydown handler without needing a tap first.
function _releaseScanFocus() {
  const active = document.querySelector('.tab-pane.active');
  if (!active || active.id === 'teller') return;
  setTimeout(() => {
    const el = document.activeElement;
    if (el && ['INPUT','TEXTAREA','SELECT'].includes(el.tagName)) el.blur();
  }, 200);
}

let _scanBuffer = '', _scanBufferTimer = null;

// Barcode trap: hidden input kept focused on the Teller tab for HID scanner input.
// On other tabs the global keydown handler catches scanner events directly.
function _focusTrap() {
  const trap = document.getElementById('barcode-trap');
  if (!trap || !STATE.user) return;
  const active = document.querySelector('.tab-pane.active');
  if (!active || active.id !== 'teller') return;
  // Don't steal focus from inputs the user intentionally tapped
  const tag = document.activeElement?.tagName;
  if (['INPUT','TEXTAREA','SELECT'].includes(tag) && document.activeElement.id !== 'barcode-trap') return;
  trap.focus({ preventScroll: true });
}

document.getElementById('barcode-trap')?.addEventListener('keydown', (e) => {
  if (!STATE.user) return;
  if (e.key === 'Enter') {
    const code = _scanBuffer.trim();
    _scanBuffer = ''; clearTimeout(_scanBufferTimer);
    if (code.length >= 3) handleScannedCode(code);
    return;
  }
  if (e.key.length === 1) {
    _scanBuffer += e.key;
    clearTimeout(_scanBufferTimer);
    _scanBufferTimer = setTimeout(() => { _scanBuffer = ''; }, 500);
  }
});

// Re-focus trap when user taps anywhere on the teller screen (not on an input)
document.getElementById('teller-screen')?.addEventListener('click', _focusTrap);
document.getElementById('teller-screen')?.addEventListener('touchend', _focusTrap);

// Re-focus trap when search field is cleared/blurred (user finished manual search)
document.getElementById('search')?.addEventListener('blur', () => setTimeout(_focusTrap, 100));

// Also handle #search Enter for cases where user manually typed a barcode
document.getElementById('search')?.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const code = this.value.trim();
  this.value = '';
  const host = document.getElementById('product-search-results');
  if (host) host.innerHTML = '';
  if (code.length >= 3) handleScannedCode(code);
  setTimeout(_focusTrap, 100);
});

// Desktop/USB: global keydown fallback when no input is focused (any tab)
document.addEventListener('keydown', (e) => {
  const tag = document.activeElement?.tagName;
  if (['INPUT','TEXTAREA','SELECT'].includes(tag)) return;
  if (!STATE.user) return;

  if (e.key === 'Enter') {
    const code = _scanBuffer.trim();
    _scanBuffer = ''; clearTimeout(_scanBufferTimer);
    if (code.length >= 3) handleScannedCode(code);
    return;
  }
  if (e.key.length === 1) {
    _scanBuffer += e.key;
    clearTimeout(_scanBufferTimer);
    _scanBufferTimer = setTimeout(() => { _scanBuffer = ''; }, 500);
  }
});

// ═══════════════════════════════════════════════════════
// CAMERA SCANNER
// ═══════════════════════════════════════════════════════
let SCAN = { running: false, reader: null, controls: null, cooldown: false };

function flashOK() {
  const f = document.getElementById('scanner-flash'); if (!f) return;
  f.classList.add('ok'); setTimeout(() => f.classList.remove('ok'), 150);
}

async function startScanner() {
  if (SCAN.running) return;
  const panel = document.getElementById('scan-panel');
  const video = document.getElementById('video');
  try {
    if (!window.ZXing || !ZXing.BrowserMultiFormatReader) throw new Error('Scanner library missing');
    panel.style.display = 'block';
    document.getElementById('btn-start-scan')?.classList.add('hidden');
    document.getElementById('btn-stop-scan')?.classList.remove('hidden');
    const codeReader  = new ZXing.BrowserMultiFormatReader();
    SCAN.reader       = codeReader;
    SCAN.controls     = await codeReader.decodeFromVideoDevice(null, video, (result) => {
      if (!result || SCAN.cooldown) return;
      const code = result.getText();
      flashOK(); beep(120, 880);
      SCAN.cooldown = true; setTimeout(() => SCAN.cooldown = false, 1500);
      handleScannedCode(code);
    });
    SCAN.running = true;
  } catch (e) { console.warn('Scanner error', e); stopScanner(); }
}

function stopScanner() {
  try { SCAN.controls?.stop(); } catch {}
  try {
    const video  = document.getElementById('video');
    const stream = video?.srcObject;
    if (stream) stream.getTracks().forEach(t => t.stop());
    if (video)  video.srcObject = null;
  } catch {}
  SCAN = { running: false, reader: null, controls: null, cooldown: false };
  const panel = document.getElementById('scan-panel'); if (panel) panel.style.display = 'none';
  document.getElementById('btn-stop-scan')?.classList.add('hidden');
  document.getElementById('btn-start-scan')?.classList.remove('hidden');
}

document.getElementById('btn-start-scan')?.addEventListener('click', startScanner);
document.getElementById('btn-stop-scan')?.addEventListener('click',  stopScanner);

// ═══════════════════════════════════════════════════════
// WEB SERIAL / BLE SCANNER
// ═══════════════════════════════════════════════════════
const _NUS_SERVICE  = '6e400001-b5a3-f393-e0a9-e50e24dcca9e';  // Nordic UART
const _NUS_TX_CHAR  = '6e400003-b5a3-f393-e0a9-e50e24dcca9e';
// HM-10 / HC-08 style (common on budget scanners)
const _HM10_SERVICE = '0000ffe0-0000-1000-8000-00805f9b34fb';
const _HM10_CHAR    = '0000ffe1-0000-1000-8000-00805f9b34fb';

let SERIAL = {
  connected: false,
  type: null,           // 'webserial' | 'webbluetooth'
  port: null,
  device: null,
  reader: null,
  characteristic: null,
  buffer: ''
};

function initSerialSupport() {
  if (/iPhone|iPad|iPod/i.test(navigator.userAgent)) return;
  const btn = document.getElementById('btn-connect-scanner');
  if (btn) btn.style.display = '';  // always show - error on click if APIs unavailable
}

function _setScannerDot(state, label) {
  const btn = document.getElementById('btn-connect-scanner');
  if (!btn) return;
  const dot = document.getElementById('scanner-status-dot');
  if (state === 'connecting') {
    if (dot) dot.style.background = '#D97706';
    return;
  }
  if (state) {
    if (dot) dot.style.background = '#16A34A';
    btn.innerHTML = `<span id="scanner-status-dot" style="position:absolute;top:4px;right:4px;width:8px;height:8px;border-radius:50%;background:#16A34A"></span><i class="bi bi-${label === 'USB' ? 'usb-plug' : 'broadcast'} me-1"></i>${label}`;
    btn.classList.replace('btn-outline-primary', 'btn-primary');
  } else {
    btn.innerHTML = `<span id="scanner-status-dot" style="position:absolute;top:4px;right:4px;width:8px;height:8px;border-radius:50%;background:#6c757d"></span><i class="bi bi-upc-scan me-1"></i>Scanner`;
    btn.classList.replace('btn-primary', 'btn-outline-primary');
  }
}

async function _connectWebSerial() {
  const port = await navigator.serial.requestPort();
  await port.open({ baudRate: 9600 });
  SERIAL.port = port;
  SERIAL.type = 'webserial';
  SERIAL.connected = true;
  _setScannerDot(true, 'USB');
  toast('USB scanner connected', 'success', 1500);
  beep(60, 1200);
  _readWebSerial();
}

async function _readWebSerial() {
  const port = SERIAL.port;
  if (!port || !port.readable) return;
  const decoder = new TextDecoderStream();
  port.readable.pipeTo(decoder.writable).catch(() => {});
  const reader = decoder.readable.getReader();
  SERIAL.reader = reader;
  try {
    while (SERIAL.connected) {
      const { value, done } = await reader.read();
      if (done) break;
      SERIAL.buffer += value;
      if (SERIAL.buffer.length > 1000) SERIAL.buffer = '';
      const lines = SERIAL.buffer.split(/\r\n|\r|\n/);
      SERIAL.buffer = lines.pop() || '';
      for (const line of lines) {
        const code = line.trim();
        if (code.length >= 3) handleScannedCode(code);
      }
    }
  } catch (e) {
    if (SERIAL.connected) { toast('USB scanner disconnected', 'warning'); await disconnectScanner(); }
  } finally {
    try { reader.releaseLock(); } catch {}
  }
}

function _onBLEData(e) {
  const text = new TextDecoder('utf-8').decode(e.target.value);
  SERIAL.buffer += text;
  if (SERIAL.buffer.length > 1000) SERIAL.buffer = '';
  const lines = SERIAL.buffer.split(/\r\n|\r|\n/);
  SERIAL.buffer = lines.pop() || '';
  for (const line of lines) {
    const code = line.trim();
    if (code.length >= 3) handleScannedCode(code);
  }
}

function _onBLEDisconnect() {
  if (SERIAL.connected) { toast('Bluetooth scanner disconnected', 'warning'); disconnectScanner(); }
}

async function _connectWebBluetooth() {
  // Accept all BLE devices - budget scanners use various UART service UUIDs
  const device = await navigator.bluetooth.requestDevice({
    acceptAllDevices: true,
    optionalServices: [_NUS_SERVICE, _HM10_SERVICE]
  });
  device.addEventListener('gattserverdisconnected', _onBLEDisconnect);
  const server = await device.gatt.connect();

  // Try Nordic UART first, then HM-10 style
  let char = null;
  try {
    const svc = await server.getPrimaryService(_NUS_SERVICE);
    char = await svc.getCharacteristic(_NUS_TX_CHAR);
  } catch {
    const svc = await server.getPrimaryService(_HM10_SERVICE);
    char = await svc.getCharacteristic(_HM10_CHAR);
  }

  SERIAL.device = device;
  SERIAL.characteristic = char;
  SERIAL.type = 'webbluetooth';
  SERIAL.connected = true;
  await char.startNotifications();
  char.addEventListener('characteristicvaluechanged', _onBLEData);
  _setScannerDot(true, 'BLE');
  toast('Bluetooth scanner connected: ' + (device.name || 'BLE Scanner'), 'success', 2000);
  beep(60, 1200);
}

async function disconnectScanner() {
  if (SERIAL.type === 'webserial') {
    try { if (SERIAL.reader) { await SERIAL.reader.cancel(); } } catch {}
    try { if (SERIAL.port) { await SERIAL.port.close(); } } catch {}
  } else if (SERIAL.type === 'webbluetooth') {
    try {
      if (SERIAL.characteristic) {
        await SERIAL.characteristic.stopNotifications();
        SERIAL.characteristic.removeEventListener('characteristicvaluechanged', _onBLEData);
      }
      if (SERIAL.device) {
        SERIAL.device.removeEventListener('gattserverdisconnected', _onBLEDisconnect);
        if (SERIAL.device.gatt?.connected) SERIAL.device.gatt.disconnect();
      }
    } catch {}
  }
  SERIAL = { connected: false, type: null, port: null, device: null, reader: null, characteristic: null, buffer: '' };
  _setScannerDot(false);
}

async function connectScanner() {
  if (SERIAL.connected) { await disconnectScanner(); return; }
  if (!navigator.serial && !navigator.bluetooth) {
    toast('Bluetooth/Serial not supported in this browser. Open the POS in Chrome for scanner support.', 'warning', 6000);
    return;
  }
  _setScannerDot('connecting');
  if (navigator.serial) {
    try { await _connectWebSerial(); return; } catch (e) {
      if (e.name !== 'NotFoundError') { toast('USB scanner error: ' + e.message, 'danger'); _setScannerDot(false); return; }
      // User cancelled USB picker - fall through to BLE
    }
  }
  if (navigator.bluetooth) {
    try { await _connectWebBluetooth(); } catch (e) {
      if (e.name !== 'NotFoundError') toast('Bluetooth error: ' + e.message, 'danger');
      _setScannerDot(false);
    }
  }
}

document.getElementById('btn-connect-scanner')?.addEventListener('click', connectScanner);
window.addEventListener('DOMContentLoaded', initSerialSupport);

// ═══════════════════════════════════════════════════════
// TRANSACTIONS
// ═══════════════════════════════════════════════════════
function initTxDatePickers() {
  const s = document.getElementById('tx-start');
  const e = document.getElementById('tx-end');
  if (s && !s.value) {
    const d = new Date(); d.setDate(d.getDate() - 6);
    s.value = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  }
  if (e && !e.value) e.value = todayISO();
}

let _txFilter = {};

async function loadTransactions(start, end) {
  if (!STATE.user) return;
  try {
    let url = '/api/transactions';
    if (isAdmin() && (start || end)) {
      const p = new URLSearchParams();
      if (start) p.set('start', start);
      if (end)   p.set('end', end);
      url += '?' + p.toString();
    }
    const trs = await api(url);
    STATE.transactions = trs;
    _applyTxFilter();
  } catch (e) { console.error('loadTransactions', e); }
}

function _applyTxFilter() {
  let list = STATE.transactions || [];
  const f = _txFilter;
  if (f.q) {
    const q = f.q.toLowerCase();
    list = list.filter(t =>
      (t.id || '').toLowerCase().includes(q) ||
      (t.teller || '').toLowerCase().includes(q) ||
      t.lines?.some(l => (l.name || '').toLowerCase().includes(q))
    );
  }
  if (f.user)   list = list.filter(t => t.teller === f.user);
  if (f.minRev != null && f.minRev !== '') list = list.filter(t => t.total >= parseFloat(f.minRev));
  if (f.maxRev != null && f.maxRev !== '') list = list.filter(t => t.total <= parseFloat(f.maxRev));
  if (f.status === 'flagged')  list = list.filter(t => t.flagged && !t.flag_resolved);
  if (f.hasDiscount) list = list.filter(t => t.lines?.some(l => l.discount));
  renderTransactions(list);
}

function _txAdvancedFilterInit() {
  document.getElementById('tx-adv-search')?.addEventListener('input',  _txReadFilter);
  document.getElementById('tx-adv-user')?.addEventListener('change',   _txReadFilter);
  document.getElementById('tx-adv-min')?.addEventListener('input',     _txReadFilter);
  document.getElementById('tx-adv-max')?.addEventListener('input',     _txReadFilter);
  document.querySelectorAll('[name="tx-adv-status"]').forEach(r => r.addEventListener('change', _txReadFilter));
  document.getElementById('tx-adv-discount')?.addEventListener('change', _txReadFilter);
  document.getElementById('btn-tx-adv-clear')?.addEventListener('click', _txClearFilter);
}

function _txReadFilter() {
  _txFilter = {
    q:           document.getElementById('tx-adv-search')?.value?.trim() || '',
    user:        document.getElementById('tx-adv-user')?.value || '',
    minRev:      document.getElementById('tx-adv-min')?.value,
    maxRev:      document.getElementById('tx-adv-max')?.value,
    status:      document.querySelector('[name="tx-adv-status"]:checked')?.value || 'all',
    hasDiscount: document.getElementById('tx-adv-discount')?.checked,
  };
  _applyTxFilter();
}

function _txClearFilter() {
  _txFilter = {};
  ['tx-adv-search','tx-adv-min','tx-adv-max'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const allRadio = document.getElementById('tx-adv-status-all'); if (allRadio) allRadio.checked = true;
  const disc = document.getElementById('tx-adv-discount'); if (disc) disc.checked = false;
  const userSel = document.getElementById('tx-adv-user'); if (userSel) userSel.value = '';
  _applyTxFilter();
}

_txAdvancedFilterInit();

function renderTransactions(trs) {
  const host = document.getElementById('transactions-list');
  if (!host) return;

  if (!trs.length) {
    const s = document.getElementById('tx-start')?.value;
    const e = document.getElementById('tx-end')?.value;
    const rangeHint = (s || e) ? ` for ${s || '?'} – ${e || '?'}` : '';
    host.innerHTML = `<div class="text-muted small p-3">No transactions found${rangeHint}. Adjust the date range above to search further back.</div>`;
    return;
  }

  const admin = isAdmin();

  const hdr = document.createElement('div');
  hdr.className = 'tx-hdr';
  hdr.innerHTML = `
    <div>#</div>
    <div>Date &amp; Time</div>
    <div>User</div>
    <div>Items</div>
    <div>Revenue</div>
    <div class="${admin ? '' : 'd-none'}">COGS</div>
    <div class="${admin ? '' : 'd-none'}">Margin</div>
    <div>Actions</div>
  `;
  host.innerHTML = '';
  host.appendChild(hdr);

  trs.forEach(t => {
    const sid       = String(t.id).slice(0, 8);
    const dtStr     = new Date(t.date_time).toLocaleString('en-ZA', { day:'2-digit', month:'short', year:'numeric', hour:'2-digit', minute:'2-digit' });
    const isVoided  = !!t.voided;
    const isFlagged = t.flagged && !t.flag_resolved;
    const isReviewed = t.flagged && t.flag_resolved;

    const row = document.createElement('div');
    row.className = `tx-row${isVoided ? ' voided' : ''}`;
    row.dataset.saleId = t.id;

    let badges = '';
    if (isFlagged)   badges += `<span class="badge bg-warning text-dark ms-1" style="font-size:10px"><i class="bi bi-flag-fill"></i></span>`;
    if (isReviewed)  badges += `<span class="badge bg-secondary ms-1" style="font-size:10px"><i class="bi bi-check-lg"></i></span>`;
    if (isVoided)    badges += `<span class="badge bg-danger ms-1" style="font-size:10px">Voided</span>`;
    if (t.is_return) badges += `<span class="badge bg-info text-dark ms-1" style="font-size:10px">Return</span>`;

    const receiptBtn = `<button class="tx-act" data-act="receipt" title="View receipt"><i class="bi bi-receipt"></i></button>`;
    const editBtn    = admin ? `<button class="tx-act" data-act="edit" title="Edit transaction"><i class="bi bi-pencil"></i></button>` : '';
    const voidBtn    = admin && !isVoided ? `<button class="tx-act text-danger" data-act="void" title="Void transaction"><i class="bi bi-x-circle"></i></button>` : '';
    const expandBtn  = `<button class="tx-expand-btn" title="Expand">▼</button>`;

    row.innerHTML = `
      <div class="tx-id fw-semibold">#${escapeHtml(sid)}${badges}</div>
      <div class="tx-dt small text-muted">${dtStr}</div>
      <div class="tx-user small">${escapeHtml(t.teller || '—')}</div>
      <div class="tx-items small text-center">${t.lines?.length ?? 0}</div>
      <div class="tx-rev fw-semibold">R${fmt(t.total)}</div>
      <div class="tx-cogs small ${admin ? '' : 'd-none'}">${admin && t.cogs != null ? 'R'+fmt(t.cogs) : '—'}</div>
      <div class="tx-margin small ${admin ? '' : 'd-none'}">${admin && t.margin_pct != null ? t.margin_pct+'%' : '—'}</div>
      <div class="tx-actions">${receiptBtn}${editBtn}${voidBtn}${expandBtn}</div>
      <div class="tx-body" style="display:none"></div>
    `;

    row.querySelector('[data-act="receipt"]')?.addEventListener('click', e => { e.stopPropagation(); previewReceipt(t.id); });
    if (admin) {
      row.querySelector('[data-act="edit"]')?.addEventListener('click',  e => { e.stopPropagation(); openTxModal(t); });
      row.querySelector('[data-act="void"]')?.addEventListener('click',  e => { e.stopPropagation(); openTxModal(t); });
    }

    function _toggleTxBody() {
      const body      = row.querySelector('.tx-body');
      const expBtn    = row.querySelector('.tx-expand-btn');
      const isOpen    = body.style.display !== 'none';
      if (isOpen) {
        body.style.display = 'none';
        row.classList.remove('expanded');
        if (expBtn) expBtn.style.transform = '';
      } else {
        body.style.display = '';
        row.classList.add('expanded');
        if (expBtn) expBtn.style.transform = 'rotate(180deg)';
        _renderTxBody(body, t, admin);
      }
    }
    row.addEventListener('click', e => { if (e.target.closest('.tx-actions') || e.target.closest('.tx-body')) return; _toggleTxBody(); });
    row.querySelector('.tx-expand-btn')?.addEventListener('click', e => { e.stopPropagation(); _toggleTxBody(); });

    host.appendChild(row);
  });
}

function _renderTxBody(body, t, admin) {
  const profit = (t.total - (t.cogs || 0)).toFixed(2);
  const hasManualDiscount = t.lines?.some(ln => ln.discount?.item || ln.discount?.cart);
  const discNote = (t.discount_by && hasManualDiscount)
    ? `<span class="text-success small ms-2">Discount applied by ${escapeHtml(t.discount_by)}</span>` : '';

  const flagNote = t.flagged && t.flag_note
    ? `<div class="small px-2 py-1 rounded mb-2 ${t.flag_resolved ? 'bg-light text-muted' : 'bg-warning bg-opacity-25'}">
         <strong>${t.flag_resolved ? '<i class="bi bi-check-lg me-1"></i>Reviewed' : '<i class="bi bi-flag me-1"></i>Flag note'}:</strong> ${escapeHtml(t.flag_note)}
         ${admin && !t.flag_resolved ? `<button class="btn btn-outline-success btn-sm ms-2 py-0 px-1" onclick="resolveFlag('${t.id}')"><i class="bi bi-check-lg me-1"></i>Resolve</button>` : ''}
       </div>` : '';

  const profitHtml = (admin && t.cogs != null)
    ? `<span>Profit: <strong class="text-success">R${fmt(profit)}</strong></span>`
    : '';
  const summaryHtml = (profitHtml || discNote)
    ? `<div class="d-flex gap-3 small mb-2 text-muted">${profitHtml}${discNote}</div>`
    : '';

  const lines = t.lines || [];
  const lineRows = lines.map((ln, i) => {
    let disc = '';
    if (ln.discount) {
      const parts = [];
      if (ln.discount.special)  parts.push(`Special: ${ln.discount.special}`);
      if (ln.discount.item) { const d = ln.discount.item; parts.push(d.type==='pct' ? `${d.value}% off` : `R${fmt(d.value)} off`); }
      if (ln.discount.cart) { const d = ln.discount.cart; parts.push(d.type==='pct' ? `${d.value}% cart` : `R${fmt(d.value)} cart`); }
      if (parts.length) disc = `<span class="text-success" style="font-size:11px">${parts.join(' + ')}</span>`;
    }
    const lineProfitF = (admin && ln.cogs != null) ? ln.subtotal - ln.cogs : null;
    const cogsCell    = admin && ln.cogs   != null ? `<div class="small text-muted">R${fmt(ln.cogs)}</div>`   : `<div class="${admin ? 'small text-muted' : 'd-none'}">—</div>`;
    const profitCell  = admin && lineProfitF != null ? `<div class="small text-success fw-semibold">R${fmt(lineProfitF)}</div>` : `<div class="${admin ? 'small text-muted' : 'd-none'}">—</div>`;
    const marginCell  = admin && ln.margin != null ? `<div class="small ${ln.margin >= 50 ? 'text-success' : ln.margin < 20 ? 'text-danger' : 'text-muted'}">${ln.margin}%</div>` : `<div class="${admin ? 'small text-muted' : 'd-none'}">—</div>`;
    return `
      <div class="tx-line-row" data-line-idx="${i}">
        <div><input type="checkbox" class="form-check-input tx-line-chk" data-idx="${i}"></div>
        <div class="small fw-semibold" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(ln.name)}">${escapeHtml(ln.name)}</div>
        <div class="small text-center">${fmtQty(ln.qty)}</div>
        <div class="small text-muted">R${fmt(ln.unit_price)}</div>
        <div class="small fw-semibold">R${fmt(ln.subtotal)}</div>
        ${cogsCell}
        ${profitCell}
        ${marginCell}
        <div>${disc}</div>
      </div>`;
  }).join('');

  body.innerHTML = `
    <div class="tx-body-inner p-2">
      ${summaryHtml}
      ${flagNote}
      <div class="tx-lines-wrap">
        <div class="tx-line-hdr">
          <div></div><div>Product</div><div class="text-center">Qty</div>
          <div>Unit Price</div><div>Total</div>
          <div class="${admin ? '' : 'd-none'}">COGS</div>
          <div class="${admin ? '' : 'd-none'}">Profit</div>
          <div class="${admin ? '' : 'd-none'}">Margin</div>
          <div>Discount</div>
        </div>
        ${lineRows}
      </div>
      <div class="tx-item-sel-bar d-none">
        <span class="tx-item-count small fw-semibold">0 items selected</span>
        <button class="btn btn-warning" data-item-action="flag"><i class="bi bi-flag me-1"></i>Flag Items</button>
        ${isAdmin() ? `<button class="btn btn-outline-secondary" data-item-action="return"><i class="bi bi-arrow-return-left me-1"></i>Return Items</button>` : ''}
        <button class="btn btn-link tx-clear-sel px-2"><i class="bi bi-x-lg me-1"></i>Clear</button>
      </div>
    </div>
  `;

  const selBar    = body.querySelector('.tx-item-sel-bar');
  const countSpan = body.querySelector('.tx-item-count');

  function _updateItemSel() {
    const n = body.querySelectorAll('.tx-line-chk:checked').length;
    if (n > 0) { selBar.classList.remove('d-none'); countSpan.textContent = `${n} item${n!==1?'s':''} selected`; }
    else        { selBar.classList.add('d-none'); }
  }

  body.querySelectorAll('.tx-line-chk').forEach(chk => chk.addEventListener('change', _updateItemSel));
  body.querySelector('.tx-clear-sel')?.addEventListener('click', () => {
    body.querySelectorAll('.tx-line-chk').forEach(c => c.checked = false);
    _updateItemSel();
  });
  body.querySelector('[data-item-action="flag"]')?.addEventListener('click', () => {
    const sel = [...body.querySelectorAll('.tx-line-chk:checked')].map(c => t.lines[parseInt(c.dataset.idx)]).filter(Boolean);
    openFlagModal(t, sel);
  });
  body.querySelector('[data-item-action="return"]')?.addEventListener('click', () => {
    const sel = [...body.querySelectorAll('.tx-line-chk:checked')].map(c => t.lines[parseInt(c.dataset.idx)]).filter(Boolean);
    openReturnModal(t, sel);
  });
}

document.getElementById('btn-refresh-trans')?.addEventListener('click', () => {
  if (isAdmin()) {
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
  } else {
    loadTransactions();
  }
});
document.getElementById('btn-tx-filter')?.addEventListener('click', () => {
  _txClearFilter();
  loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
});
document.getElementById('btn-tx-today')?.addEventListener('click', () => {
  _txClearFilter();
  const t = todayISO();
  const s = document.getElementById('tx-start'); if (s) s.value = t;
  const e = document.getElementById('tx-end');   if (e) e.value = t;
  loadTransactions(t, t);
});

document.getElementById('btn-tx-yesterday')?.addEventListener('click', () => {
  _txClearFilter();
  const d = new Date(); d.setDate(d.getDate() - 1);
  const y = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const s = document.getElementById('tx-start'); if (s) s.value = y;
  const e = document.getElementById('tx-end');   if (e) e.value = y;
  loadTransactions(y, y);
});

document.getElementById('btn-tx-week')?.addEventListener('click', () => {
  _txClearFilter();
  const now = new Date();
  const day = now.getDay() || 7;
  const mon = new Date(now); mon.setDate(now.getDate() - day + 1);
  const start = `${mon.getFullYear()}-${String(mon.getMonth()+1).padStart(2,'0')}-${String(mon.getDate()).padStart(2,'0')}`;
  const end   = todayISO();
  const s = document.getElementById('tx-start'); if (s) s.value = start;
  const e = document.getElementById('tx-end');   if (e) e.value = end;
  loadTransactions(start, end);
});

// ── Transaction Void/Edit Modal ──
let _txModalLines = [];

function openTxModal(t) {
  STATE.currentTx = t;
  _txModalLines   = t.lines.map(l => ({ ...l }));
  document.getElementById('txModalTitle').textContent = `Transaction #${String(t.id).slice(0,8)}`;
  const meta = document.getElementById('tx-modal-meta');
  if (meta) meta.textContent = `${new Date(t.date_time).toLocaleString('en-ZA')} - R${fmt(t.total)}${t.teller ? ' - ' + t.teller : ''}`;
  document.getElementById('tx-void-reason').value = '';
  renderTxEditTable();
  bootstrap.Modal.getOrCreateInstance(document.getElementById('txModal')).show();
}

function renderTxEditTable() {
  const tbody = document.getElementById('tx-edit-body'); if (!tbody) return;
  tbody.innerHTML = '';
  let total = 0;
  _txModalLines.forEach((ln, idx) => {
    total += ln.qty * ln.unit_price;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${ln.name}</td>
      <td><input type="number" min="0.001" step="0.001" value="${fmtQty(ln.qty)}" class="form-control form-control-sm" data-idx="${idx}" data-field="qty"></td>
      <td><input type="number" step="0.01" min="0" value="${fmt(ln.unit_price)}" class="form-control form-control-sm" data-idx="${idx}" data-field="unit_price"></td>
      <td class="align-middle small">R${fmt(ln.qty * ln.unit_price)}</td>
      <td><button class="btn btn-outline-danger btn-sm" data-remove="${idx}"><i class="bi bi-x-lg"></i></button></td>
    `;
    tbody.appendChild(tr);
  });
  const totalEl = document.getElementById('tx-edit-total');
  if (totalEl) totalEl.textContent = `Total: R${fmt(total)}`;

  tbody.querySelectorAll('input[data-idx]').forEach(inp => {
    inp.addEventListener('input', () => {
      const idx   = parseInt(inp.dataset.idx);
      const field = inp.dataset.field;
      _txModalLines[idx][field] = parseFloat(inp.value) || 0;
      renderTxEditTable();
    });
  });
  tbody.querySelectorAll('[data-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      _txModalLines.splice(parseInt(btn.dataset.remove), 1);
      renderTxEditTable();
    });
  });
}

document.getElementById('btn-tx-save-edit')?.addEventListener('click', async () => {
  if (!STATE.currentTx) return;
  const lines = _txModalLines.filter(l => l.qty > 0).map(l => ({
    product_id: l.product_id, qty: l.qty, unit_price: l.unit_price
  }));
  if (lines.length === 0) return toast('Transaction must have at least one item', 'warning');
  try {
    await api(`/api/transactions/${STATE.currentTx.id}/edit`, {
      method: 'POST', body: JSON.stringify({ lines })
    });
    toast('Transaction updated');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('txModal')).hide();
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    await loadProducts();
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-tx-void')?.addEventListener('click', async () => {
  if (!STATE.currentTx) return;
  const reason = document.getElementById('tx-void-reason').value.trim();
  if (!reason) return toast('Please enter a void reason', 'warning');
  try {
    await api(`/api/transactions/${STATE.currentTx.id}/void`, {
      method: 'POST', body: JSON.stringify({ reason })
    });
    toast('Transaction voided - stock restored', 'warning');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('txModal')).hide();
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    await loadProducts();
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
});

// ── Print Receipt ────────────────────────────────────────────────────────────
async function printReceipt(saleId) {
  // Resolve configured receipt printer (falls back to default/USB auto-detect)
  const printerId = STATE._receiptPrinterId || null;
  try {
    const result = await api(`/api/transactions/${saleId}/print-receipt`, {
      method: 'POST',
      body: JSON.stringify({ printer_id: printerId }),
    });
    toast('Receipt sent to printer', 'success', 3000);
  } catch (e) {
    toast('Receipt print failed: ' + e.message, 'error');
  }
}

async function previewReceipt(saleId) {
  // Browser preview as fallback — opens a formatted window for visual check
  try {
    const r = await api(`/api/transactions/${saleId}/receipt`);
    const lines = r.lines.map(ln =>
      `${ln.name.substring(0,20).padEnd(20)} x${fmt(ln.qty).padStart(6)}  R${fmt(ln.subtotal).padStart(7)}`
    ).join('\n');
    const pm     = r.payment_method ? r.payment_method.toUpperCase() : '';
    const change = r.change != null && r.change > 0 ? `\nChange:    R${fmt(r.change)}` : '';
    const vat    = r.vat_registered ? `\nVAT (${r.vat_rate}%): R${fmt(r.vat_amount)}` : '';
    const vatNum = r.vat_registered && r.vat_number ? `VAT No: ${r.vat_number}\n` : '';
    const content = [
      r.store_name || 'Farm Stall',
      r.store_legal ? `(${r.store_legal})` : '',
      vatNum,
      new Date(r.date_time).toLocaleString('en-ZA'),
      `Receipt: #${String(saleId).slice(0,8)}`,
      '─'.repeat(42),
      lines,
      '─'.repeat(42),
      `TOTAL:     R${fmt(r.total)}`,
      vat,
      `Payment:   ${pm}`,
      r.cash_tendered != null ? `Tendered:  R${fmt(r.cash_tendered)}` : '',
      change,
      '',
      r.footer || 'Thank you for your purchase!',
    ].filter(l => l !== null && l !== undefined).join('\n');
    const win = window.open('', '_blank', 'width=420,height=700');
    if (!win) { toast('Pop-up blocked — allow pop-ups for receipt preview', 'warning'); return; }
    win.document.write(`<!DOCTYPE html><html><head><title>Receipt</title></head><body>
      <pre style="font-family:'Courier New',monospace;font-size:13px;padding:20px;white-space:pre">${content}</pre>
      <script>window.print();<\/script></body></html>`);
    win.document.close();
  } catch (e) { toast('Could not load receipt: ' + e.message, 'error'); }
}

// ── Return Items (ISSUE-32) ──────────────────────────────────────────────────
let _returnTx = null;

function openReturnModal(t, selectedLines) {
  _returnTx = t;
  document.getElementById('return-modal-title').textContent = `#${String(t.id).slice(0,8)}`;
  document.getElementById('return-modal-meta').textContent =
    `${new Date(t.date_time).toLocaleString('en-ZA')} - R${fmt(t.total)}${t.teller ? ' - ' + t.teller : ''}`;
  document.getElementById('return-reason').value = '';

  // If specific lines were selected (from item selection toolbar), show only those;
  // otherwise show all lines (legacy behaviour: return button on the whole transaction).
  const linesToShow = (selectedLines?.length) ? selectedLines : t.lines;

  const tbody = document.getElementById('return-body');
  tbody.innerHTML = '';
  linesToShow.forEach((ln, idx) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHtml(ln.name)}</td>
      <td class="text-muted">${fmtQty(ln.qty)}</td>
      <td><input type="number" class="form-control form-control-sm return-qty-input"
           data-idx="${idx}" data-pid="${ln.product_id}"
           data-max="${ln.qty}" data-price="${ln.unit_price}"
           min="0" max="${ln.qty}" step="0.001" value="${fmtQty(ln.qty)}"></td>
      <td class="return-refund-cell small">R${fmt(ln.qty * ln.unit_price)}</td>
    `;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('.return-qty-input').forEach(inp => inp.addEventListener('input', _updateReturnTotal));
  _updateReturnTotal();
  bootstrap.Modal.getOrCreateInstance(document.getElementById('returnModal')).show();
}

function _updateReturnTotal() {
  let total = 0;
  document.querySelectorAll('.return-qty-input').forEach(inp => {
    const qty   = Math.min(parseFloat(inp.value) || 0, parseFloat(inp.dataset.max));
    const price = parseFloat(inp.dataset.price) || 0;
    const refund = qty * price;
    total += refund;
    const cell = inp.closest('tr').querySelector('.return-refund-cell');
    if (cell) cell.textContent = `R${fmt(refund)}`;
  });
  const el = document.getElementById('return-total');
  if (el) el.textContent = `Total refund: R${fmt(total)}`;
}

document.getElementById('btn-return-confirm')?.addEventListener('click', async () => {
  if (!_returnTx) return;
  const reason = document.getElementById('return-reason').value.trim();
  if (!reason) return toast('Return reason required', 'warning');

  const lines = [];
  document.querySelectorAll('.return-qty-input').forEach(inp => {
    const qty = parseFloat(inp.value) || 0;
    if (qty > 0) lines.push({ product_id: parseInt(inp.dataset.pid), qty });
  });
  if (lines.length === 0) return toast('Select at least one item to return', 'warning');

  try {
    await api(`/api/transactions/${_returnTx.id}/return`, {
      method: 'POST', body: JSON.stringify({ lines, reason })
    });
    toast('Return processed - stock restored', 'success');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('returnModal')).hide();
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    await loadProducts();
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
});

// ── Close Till / Z-Report (ISSUE-33) ────────────────────────────────────────
let _tillSummary = null;

async function openCloseTillModal() {
  try {
    _tillSummary = await api('/api/till/sessions/summary');
    document.getElementById('ct-period-start').textContent =
      new Date(_tillSummary.period_start).toLocaleString('en-ZA');
    document.getElementById('ct-cash-sales').textContent  = `R${fmt(_tillSummary.cash_sales)}`;
    document.getElementById('ct-card-sales').textContent  = `R${fmt(_tillSummary.card_sales)}`;
    document.getElementById('ct-total-sales').textContent = `R${fmt(_tillSummary.total_sales)}`;
    document.getElementById('ct-void-total').textContent  = `R${fmt(_tillSummary.void_total)}`;
    const refundRow = document.getElementById('ct-refunds-row');
    if (refundRow && _tillSummary.cash_refunds > 0) {
      document.getElementById('ct-cash-refunds').textContent = `- R${fmt(_tillSummary.cash_refunds)}`;
      refundRow.style.removeProperty('display');
    }
    const vatRow = document.getElementById('ct-vat-row');
    if (vatRow && _tillSummary.vat_registered) {
      document.getElementById('ct-vat-amount').textContent = `R${fmt(_tillSummary.vat_amount)}`;
      vatRow.style.removeProperty('display');
    }
    document.getElementById('ct-opening-float').value     = fmt(_tillSummary.suggested_opening_float);
    document.getElementById('ct-counted-cash').value      = '';
    document.getElementById('ct-over-under').classList.add('hidden');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('closeTillModal')).show();
  } catch (e) { toast(e.message, 'error'); }
}

document.getElementById('ct-counted-cash')?.addEventListener('input', () => {
  if (!_tillSummary) return;
  const counted  = parseFloat(document.getElementById('ct-counted-cash').value) || 0;
  const float_   = parseFloat(document.getElementById('ct-opening-float').value) || 0;
  const expected = float_ + _tillSummary.cash_sales - (_tillSummary.cash_refunds || 0);
  const diff     = counted - expected;
  const el       = document.getElementById('ct-over-under');
  if (document.getElementById('ct-counted-cash').value === '') { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  el.className = `alert py-2 small ${diff < -0.01 ? 'alert-danger' : diff > 0.01 ? 'alert-warning' : 'alert-success'}`;
  el.innerHTML = `Expected cash: R${fmt(expected)} | ${diff >= 0 ? 'Over' : 'Short'}: R${fmt(Math.abs(diff))}${Math.abs(diff) < 0.01 ? ' <i class="bi bi-check-lg ms-1"></i>Balanced' : ''}`;
});

document.getElementById('btn-close-till-confirm')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  if (btn.disabled) return;
  const counted = document.getElementById('ct-counted-cash').value.trim();
  if (!counted) return toast('Enter the counted cash amount', 'warning');
  const payload = {
    counted_cash: parseFloat(counted),
    opening_float: parseFloat(document.getElementById('ct-opening-float').value) || 0,
    opened_at: _tillSummary?.period_start,
    notes: document.getElementById('ct-notes').value.trim(),
  };
  btn.disabled = true;
  try {
    const res = await api('/api/till/sessions', { method: 'POST', body: JSON.stringify(payload) });
    const diff = res.over_under;
    const msg  = `Till closed. ${diff >= 0 ? 'Over' : 'Short'} R${fmt(Math.abs(diff))}.`;
    toast(msg, Math.abs(diff) < 0.01 ? 'success' : 'warning', 5000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('closeTillModal')).hide();
  } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
});

// ═══════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════
// FLAG TRANSACTION
// ═══════════════════════════════════════════════════════
function openFlagModal(t, selectedLines) {
  document.getElementById('flag-sale-id').value = t.id;
  let note = t.flag_note || '';
  if (selectedLines?.length) {
    const names = selectedLines.map(l => l.name).join(', ');
    note = note || `Items: ${names}`;
  }
  document.getElementById('flag-note').value = note;
  // Default to 'other' reason
  const otherRb = document.getElementById('flag-reason-other');
  if (otherRb) {
    document.querySelectorAll('[name="flag-reason"]').forEach(r => r.checked = false);
    otherRb.checked = true;
    if (note.toLowerCase().startsWith('pricing')) { const rb = document.getElementById('flag-reason-pricing_issue'); if (rb) { otherRb.checked = false; rb.checked = true; } }
    if (note.toLowerCase().startsWith('fraud'))   { const rb = document.getElementById('flag-reason-fraud');         if (rb) { otherRb.checked = false; rb.checked = true; } }
  }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('flagModal')).show();
  setTimeout(() => document.getElementById('flag-note').focus(), 300);
}

document.getElementById('btn-flag-submit')?.addEventListener('click', async () => {
  const saleId = document.getElementById('flag-sale-id').value;
  const reason = document.querySelector('[name="flag-reason"]:checked')?.value || '';
  const notes  = document.getElementById('flag-note').value.trim();
  const note   = [reason && reason !== 'other' ? reason.replace(/_/g, ' ') : '', notes].filter(Boolean).join(' — ') || notes || reason;
  if (!note) return toast('Please select a reason or add a note', 'warning');
  try {
    await api(`/api/transactions/${saleId}/flag`, {
      method: 'POST',
      body: JSON.stringify({ note })
    });
    toast('Transaction flagged for admin review', 'warning', 3000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('flagModal')).hide();
    // Refresh transactions
    if (isAdmin()) {
      loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    } else {
      loadTransactions();
    }
  } catch (e) { toast(e.message, 'error'); }
});

async function resolveFlag(saleId) {
  try {
    await api(`/api/transactions/${saleId}/flag`, {
      method: 'POST',
      body: JSON.stringify({ resolve: true })
    });
    toast('Flag resolved', 'success', 2000);
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
  } catch (e) { toast(e.message, 'error'); }
}

// USERS
// ═══════════════════════════════════════════════════════
function renderUsersList() {
  const wrap = document.getElementById('users-list'); if (!wrap) return;
  const q = (document.getElementById('users-filter')?.value || '').trim().toLowerCase();
  const items = STATE.users.filter(u => !q || u.username.toLowerCase().includes(q) || u.role.toLowerCase().includes(q));
  wrap.innerHTML = '';
  if (items.length === 0) {
    wrap.innerHTML = `<div class="list-group-item text-muted">${q ? 'No users match.' : 'No users yet.'}</div>`;
    return;
  }
  items.forEach(u => {
    const item = document.createElement('div'); item.className = 'list-group-item user-list-item';
    const left = document.createElement('div');
    const roleList = (u.roles || [u.role]).map(r =>
      `<span class="badge ${r==='admin'?'bg-danger':r==='developer'?'bg-info text-dark':'bg-secondary'} ms-1">${r}</span>`
    ).join('');
    left.innerHTML = `<strong>${u.username}</strong> ${roleList} <span class="user-meta ms-1">• ${u.active ? 'active' : 'disabled'}</span>`;
    const right   = document.createElement('div');
    const btnEdit = document.createElement('button');
    btnEdit.className = 'btn btn-outline-primary btn-sm'; btnEdit.textContent = 'Edit';
    btnEdit.onclick = () => fillUserEditor(u);
    right.appendChild(btnEdit); item.appendChild(left); item.appendChild(right);
    wrap.appendChild(item);
  });
}

function fillUserEditor(u) {
  document.getElementById('u-username').value = u.username;
  document.getElementById('u-password').value = '';
  const userRoles = u.roles || (u.role ? u.role.split(',').map(r=>r.trim()) : ['teller']);
  ['admin','teller','developer'].forEach(r => {
    const cb = document.getElementById(`u-role-${r}`); if (cb) cb.checked = userRoles.includes(r);
  });
  const act = document.getElementById('u-active'); if (act) act.checked = !!u.active;
  _setUserFormMode('edit');
}

function _setUserFormMode(mode) {
  const addActions  = document.getElementById('user-add-actions');
  const editActions = document.getElementById('user-edit-actions');
  const hint        = document.getElementById('user-form-hint');
  if (mode === 'edit') {
    hide(addActions); show(editActions);
    if (hint) hint.textContent = 'Editing user - click "+ New User" to create a new one.';
  } else {
    show(addActions); hide(editActions);
    if (hint) hint.textContent = 'Fill in the fields above to add a new user.';
  }
}

function _clearUserForm() {
  ['u-username','u-password'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  ['admin','teller','developer'].forEach(r => { const cb = document.getElementById(`u-role-${r}`); if(cb) cb.checked = r==='teller'; });
  const act = document.getElementById('u-active'); if (act) act.checked = true;
  _setUserFormMode('add');
}

function getSelectedRoles() {
  return ['admin','teller','developer']
    .filter(r => document.getElementById(`u-role-${r}`)?.checked)
    .join(',') || 'teller';
}

async function loadUsers() {
  if (!STATE.user?.roles?.includes('admin')) return;
  try {
    STATE.users = await api('/api/users') || [];
    renderUsersList();
    // Populate the tx advanced-filter user select
    const sel = document.getElementById('tx-adv-user');
    if (sel) {
      const current = sel.value;
      sel.innerHTML = '<option value="">All employees</option>';
      STATE.users.filter(u => u.active).forEach(u => {
        const o = document.createElement('option');
        o.value = u.username; o.textContent = u.username;
        sel.appendChild(o);
      });
      if (current) sel.value = current;
    }
  } catch (e) { console.error('loadUsers', e); }
}

document.getElementById('users-filter')?.addEventListener('input', renderUsersList);
document.getElementById('btn-refresh-users')?.addEventListener('click', loadUsers);
document.getElementById('btn-clear-user-form')?.addEventListener('click', _clearUserForm);

document.getElementById('btn-add-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  const password = document.getElementById('u-password').value;
  const role     = getSelectedRoles();
  const active   = document.getElementById('u-active').checked;
  if (!username || !password) return toast('Username and password required', 'warning');
  try {
    await api('/api/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
    if (!active) await api('/api/users/update', { method: 'POST', body: JSON.stringify({ username, active }) });
    _clearUserForm();
    await loadUsers(); toast('User added');
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-update-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  if (!username) return toast('Select a user first', 'warning');
  const password = document.getElementById('u-password').value;
  const role     = getSelectedRoles();
  const active   = document.getElementById('u-active').checked;
  const payload  = { username, role, active };
  if (password) payload.password = password;
  try { await api('/api/users/update', { method: 'POST', body: JSON.stringify(payload) }); await loadUsers(); toast('User updated'); }
  catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-delete-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  if (!username) return toast('Select a user first', 'warning');
  if (!confirm(`Delete user "${username}"?`)) return;
  try {
    await api(`/api/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
    _clearUserForm();
    await loadUsers(); toast('User deleted');
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// STATS
// ═══════════════════════════════════════════════════════

let _statsData    = null;
let _statsChartTab = 'daily';

function _statsSetDates(start, end) {
  const s = document.getElementById('stats-start');
  const e = document.getElementById('stats-end');
  if (s) s.value = start;
  if (e) e.value = end;
}

function _initStatsPresets() {
  const t = todayISO();
  _statsSetDates(t, t);

  document.querySelectorAll('[data-stats-preset]').forEach(btn => {
    btn.addEventListener('click', () => {
      const now  = new Date();
      const pad  = n => String(n).padStart(2, '0');
      const iso  = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
      const preset = btn.dataset.statsPreset;
      let start, end = t;
      if (preset === 'today') {
        start = t;
      } else if (preset === 'yesterday') {
        const y = new Date(now); y.setDate(y.getDate() - 1);
        start = end = iso(y);
      } else if (preset === 'week') {
        const day = now.getDay() || 7;
        const d = new Date(now); d.setDate(now.getDate() - day + 1);
        start = iso(d);
      } else if (preset === 'last-week') {
        const day = now.getDay() || 7;
        const mon = new Date(now); mon.setDate(now.getDate() - day + 1 - 7);
        const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
        start = iso(mon); end = iso(sun);
      } else if (preset === 'month') {
        start = `${now.getFullYear()}-${pad(now.getMonth()+1)}-01`;
      } else if (preset === 'last-month') {
        const lm = new Date(now.getFullYear(), now.getMonth() - 1, 1);
        const lme = new Date(now.getFullYear(), now.getMonth(), 0);
        start = iso(lm); end = iso(lme);
      } else if (preset === 'year') {
        start = `${now.getFullYear()}-01-01`;
      } else if (preset === 'last-year') {
        const y = now.getFullYear() - 1;
        start = `${y}-01-01`; end = `${y}-12-31`;
      }
      _statsSetDates(start, end);
      loadStats();
    });
  });
}

// drilldown state - which canvas+slice is active for click hits
let _chartClickHandlers = {};

function drawBarChart(canvas, labels, values, opts = {}) {
  if (!canvas) return;
  const parent = canvas.parentElement;

  // Compute bottom padding from actual label lengths so nothing gets clipped
  const maxLabelLen = labels.length ? Math.max(...labels.map(l => String(l || '').length)) : 0;
  const shouldRotate = labels.length > 6 || maxLabelLen > 6;
  const rotatedPx = shouldRotate ? Math.ceil(maxLabelLen * 5.5 * Math.sin(Math.PI / 4)) + 8 : 0;
  const padL = opts.yLabel ? 74 : 60;
  const padR = 20;
  const padT = 30;
  const padB = opts.xLabel
    ? Math.max(64, rotatedPx + 20)
    : Math.max(50, rotatedPx + 12);

  // Grow canvas (and parent) tall enough to show all labels
  const minH = padT + 160 + padB;
  canvas.width  = parent?.clientWidth  || 800;
  canvas.height = Math.max(parent?.clientHeight || 320, minH);
  if (parent && canvas.height > (parent.clientHeight || 0)) {
    parent.style.minHeight = canvas.height + 'px';
  }

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const W = canvas.width, H = canvas.height;
  const max = Math.max(...values, 1);
  const n   = values.length || 1;
  const bw  = Math.max(4, ((W - padL - padR) / n) * 0.6);
  const color = opts.color || '#2a6f3e';
  const color2 = opts.color2;

  // Store bar rects for click detection
  const barRects = [];

  // Axes
  ctx.strokeStyle = '#ccc'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, padT); ctx.lineTo(padL, H - padB); ctx.lineTo(W - padR, H - padB); ctx.stroke();

  // Y-axis label (rotated)
  if (opts.yLabel) {
    ctx.save();
    ctx.fillStyle = '#555'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center';
    ctx.translate(13, padT + (H - padT - padB) / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText(opts.yLabel, 0, 0);
    ctx.restore();
  }

  // X-axis label
  if (opts.xLabel) {
    ctx.fillStyle = '#555'; ctx.font = '11px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText(opts.xLabel, padL + (W - padL - padR) / 2, H - 4);
  }

  // Y gridlines + labels
  ctx.fillStyle = '#888'; ctx.font = '11px sans-serif'; ctx.textAlign = 'right';
  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const y = H - padB - f * (H - padT - padB);
    ctx.strokeStyle = '#eee'; ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
    ctx.fillStyle = '#888';
    const val = max * f;
    ctx.fillText(val >= 1000 ? `${(val/1000).toFixed(1)}k` : fmt(val), padL - 4, y + 4);
  });

  values.forEach((v, i) => {
    const slotW = (W - padL - padR) / n;
    const x = padL + i * slotW + (slotW - bw) / 2;
    const barH = (H - padT - padB) * (v / max);
    const y = H - padB - barH;
    ctx.fillStyle = color2 && i % 2 === 1 ? color2 : color;
    ctx.fillRect(x, y, bw, barH);
    barRects.push({ x, y, w: bw, h: barH, label: labels[i], value: v, index: i });

    if (barH > 14) {
      ctx.fillStyle = '#fff'; ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'center';
      const num = v >= 1000 ? `${(v/1000).toFixed(1)}k` : fmt(v);
      const label = `${opts.valuePrefix || ''}${num}${opts.valueSuffix || ''}`;
      ctx.fillText(label, x + bw / 2, y + 12);
    }

    ctx.fillStyle = '#555'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    const lbl = String(labels[i] || '');
    ctx.save(); ctx.translate(x + bw / 2, H - padB + 6);
    if (shouldRotate) { ctx.rotate(-Math.PI / 4); ctx.textAlign = 'right'; }
    ctx.fillText(lbl, 0, 0);
    ctx.restore();
  });

  // Attach click handler if drilldown callback provided
  if (opts.onBarClick) {
    const id = canvas.id;
    if (_chartClickHandlers[id]) canvas.removeEventListener('click', _chartClickHandlers[id]);
    canvas.style.cursor = 'pointer';
    _chartClickHandlers[id] = (e) => {
      const rect = canvas.getBoundingClientRect();
      const scaleX = canvas.width / rect.width;
      const scaleY = canvas.height / rect.height;
      const mx = (e.clientX - rect.left) * scaleX;
      const my = (e.clientY - rect.top)  * scaleY;
      const hit = barRects.find(b => mx >= b.x && mx <= b.x + b.w && my >= b.y && my <= b.y + b.h);
      if (hit) opts.onBarClick(hit.label, hit.value, hit.index);
    };
    canvas.addEventListener('click', _chartClickHandlers[id]);
  }
}

function _renderDrilldownTransactions(data, opts = {}) {
  const { summary, transactions } = data;
  const s = summary;
  let html = '';

  // ── Summary cards ──
  html += `<div class="row g-2 mb-3">
    <div class="col-6 col-md-3"><div class="card text-center py-2 px-1">
      <div class="text-muted" style="font-size:11px">Total Revenue</div>
      <div class="fw-bold text-success fs-6">R${fmt(s.total_revenue)}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card text-center py-2 px-1">
      <div class="text-muted" style="font-size:11px">Transactions</div>
      <div class="fw-bold fs-6">${s.tx_count}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card text-center py-2 px-1">
      <div class="text-muted" style="font-size:11px">Avg Sale Value</div>
      <div class="fw-bold fs-6">R${fmt(s.avg_tx_value)}</div>
    </div></div>
    <div class="col-6 col-md-3"><div class="card text-center py-2 px-1">
      <div class="text-muted" style="font-size:11px">Peak Hour</div>
      <div class="fw-bold fs-6">${s.peak_hour != null ? `${s.peak_hour}:00` : '-'}</div>
    </div></div>
  </div>`;

  // ── Top products ──
  if (s.top_products?.length) {
    html += `<div class="mb-3">
      <div class="fw-semibold small mb-1">Top Products</div>
      <table class="table table-sm table-borderless mb-0">
        <thead class="table-light"><tr><th>Product</th><th class="text-end">Qty sold</th><th class="text-end">Revenue</th></tr></thead>
        <tbody>${s.top_products.map(p => `<tr>
          <td>${p.product}</td>
          <td class="text-end">${p.qty}</td>
          <td class="text-end fw-semibold text-success">R${fmt(p.revenue)}</td>
        </tr>`).join('')}</tbody>
      </table></div>`;
  }

  // ── Teller breakdown (only when more than one teller) ──
  if (s.teller_breakdown?.length > 1) {
    html += `<div class="mb-3">
      <div class="fw-semibold small mb-1">By Teller</div>
      <table class="table table-sm table-borderless mb-0">
        <thead class="table-light"><tr><th>Teller</th><th class="text-end">Sales</th><th class="text-end">Revenue</th></tr></thead>
        <tbody>${s.teller_breakdown.map(t => `<tr>
          <td>${t.teller}</td>
          <td class="text-end">${t.tx_count}</td>
          <td class="text-end fw-semibold">R${fmt(t.revenue)}</td>
        </tr>`).join('')}</tbody>
      </table></div>`;
  }

  // ── Context-specific insight ──
  if (opts.context === 'best' || opts.context === 'worst') {
    const label = opts.context === 'best' ? 'Why was this the best day?' : 'Why was this the worst day?';
    const largest = s.largest_sale;
    html += `<div class="mb-3 p-2 rounded" style="background:${opts.context==='best'?'#f0faf0':'#fff8f0'}">
      <div class="fw-semibold small mb-1">${label}</div>
      <ul class="mb-0 small">
        ${s.top_products.length ? `<li>Best-seller: <strong>${s.top_products[0].product}</strong> - R${fmt(s.top_products[0].revenue)}</li>` : ''}
        ${s.peak_hour != null ? `<li>Busiest hour: <strong>${s.peak_hour}:00–${s.peak_hour+1}:00</strong></li>` : ''}
        ${largest ? `<li>Largest single sale: <strong>R${fmt(largest.total)}</strong> (#${largest.sale_id} by ${largest.teller})</li>` : ''}
        ${s.teller_breakdown.length === 1 ? `<li>All sales by: <strong>${s.teller_breakdown[0].teller}</strong></li>` : ''}
      </ul>
    </div>`;
  }

  // ── Transaction list ──
  html += `<div class="fw-semibold small mb-1 mt-2">All Transactions</div>`;
  if (!transactions.length) {
    html += '<div class="text-muted small">No transactions.</div>';
  } else {
    html += `<table class="table table-sm table-hover">
      <thead class="table-light sticky-top">
        <tr>
          <th>Sale #</th><th>Time</th><th>Teller</th><th>Items</th><th class="text-end">Total</th>
        </tr>
      </thead>
      <tbody>`;
    transactions.forEach(t => {
      const lines = t.lines.map(l =>
        `<tr class="table-secondary">
          <td colspan="2" class="ps-4 text-muted">${l.product}</td>
          <td class="text-muted">${l.qty % 1 === 0 ? l.qty : l.qty.toFixed(2)} × R${fmt(l.unit_price)}</td>
          <td></td>
          <td class="text-end text-muted">R${fmt(l.line_total)}</td>
        </tr>`
      ).join('');
      html += `<tr class="sale-row" style="cursor:pointer" data-sale="${t.sale_id}">
        <td class="fw-semibold">#${t.sale_id}</td>
        <td style="font-size:11px">${t.date_time.replace('T',' ').slice(0,16)}</td>
        <td>${t.teller}</td>
        <td class="text-muted">${Math.round(t.item_count)} item${Math.round(t.item_count)!==1?'s':''}</td>
        <td class="text-end fw-semibold text-success">R${fmt(t.total)}</td>
      </tr>${lines}`;
    });
    html += '</tbody></table>';
  }
  return html;
}

function _statsFilterParams() {
  const start     = document.getElementById('stats-start')?.value || todayISO();
  const end       = document.getElementById('stats-end')?.value   || todayISO();
  const productId = document.getElementById('stats-product-filter')?.value || '';
  const userId    = document.getElementById('stats-user-filter')?.value    || '';
  const p = new URLSearchParams({ start, end });
  if (productId) p.set('product_id', productId);
  if (userId)    p.set('user_id',    userId);
  return p;
}

async function openDrilldown(title, type, value, opts = {}) {
  document.getElementById('drilldown-title').textContent = title;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-center text-muted p-3">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    if (type && type !== 'range') { params.set('type', type); params.set('value', value ?? ''); }
    const data = await api(`/api/stats/drilldown?${params}`);
    if (!data.transactions?.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-3 text-center">No transactions found for this period.</div>';
      return;
    }
    document.getElementById('drilldown-body').innerHTML = _renderDrilldownTransactions(data, opts);
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openTillDrilldown(title, openedIso, closedIso) {
  document.getElementById('drilldown-title').textContent = title;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-center text-muted p-3">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ start: openedIso.slice(0, 10), end: closedIso.slice(0, 10) });
    const data = await api(`/api/stats/drilldown?${params}`);
    if (!data.transactions?.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-3 text-center">No transactions found for this till session.</div>';
      return;
    }
    document.getElementById('drilldown-body').innerHTML = _renderDrilldownTransactions(data);
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openSupplierDrilldown(supplierName) {
  document.getElementById('drilldown-title').textContent = `Stock purchases - ${supplierName}`;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    params.set('supplier', supplierName);
    const batches = await api(`/api/stats/drilldown/supplier?${params}`);
    if (!batches.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No stock purchases found.</div>';
      return;
    }
    const totalSpend = batches.reduce((s, b) => s + b.total_cost, 0);
    let html = `<div class="small text-muted mb-2">${batches.length} batch${batches.length!==1?'es':''} · Total spent R${fmt(totalSpend)}</div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Date</th><th>Product</th><th>Qty</th><th>Cost/unit</th><th class="text-end">Total</th><th>Remaining</th></tr></thead><tbody>`;
    batches.forEach(b => {
      html += `<tr>
        <td style="font-size:12px">${b.date.replace('T',' ').slice(0,16)}</td>
        <td>${b.product}</td>
        <td>${b.qty_base.toFixed(2)}</td>
        <td>R${b.cost_per_unit.toFixed(6)}</td>
        <td class="text-end fw-semibold">R${fmt(b.total_cost)}</td>
        <td class="text-muted">${b.remaining.toFixed(2)}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openKitchenDrilldown() {
  document.getElementById('drilldown-title').textContent = 'Kitchen Orders';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    const orders = await api(`/api/stats/drilldown/kitchen?${params}`);
    if (!orders.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No kitchen orders found.</div>';
      return;
    }
    const fmtWait = s => s != null ? (s >= 60 ? `${Math.floor(s/60)}m ${s%60}s` : `${s}s`) : '-';
    const completed = orders.filter(o => o.status === 'completed').length;
    const avgWait = orders.filter(o => o.wait_seconds != null).reduce((s,o,_,a) => s + o.wait_seconds/a.length, 0);
    let html = `<div class="small text-muted mb-2">${orders.length} order${orders.length!==1?'s':''} · ${completed} completed · Avg wait ${fmtWait(Math.round(avgWait))}</div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Time</th><th>Product</th><th>Qty</th><th>Teller</th><th>Status</th><th>Wait</th><th>Notes</th></tr></thead><tbody>`;
    orders.forEach(o => {
      const statusColor = o.status === 'completed' ? 'text-success' : o.status === 'cancelled' ? 'text-danger' : 'text-warning';
      html += `<tr>
        <td style="font-size:11px">${o.queued_at?.replace('T',' ').slice(0,16) || '-'}</td>
        <td>${o.product}</td>
        <td>${o.qty}</td>
        <td>${o.teller}</td>
        <td class="${statusColor} fw-semibold">${o.status}</td>
        <td>${fmtWait(o.wait_seconds)}</td>
        <td class="text-muted small">${o.notes || ''}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openWriteoffDrilldown() {
  document.getElementById('drilldown-title').textContent = 'Stock Write-offs';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    const items = await api(`/api/stats/drilldown/writeoffs?${params}`);
    if (!items.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No write-offs found.</div>';
      return;
    }
    const total = items.reduce((s, w) => s + w.cost, 0);
    let html = `<div class="small text-muted mb-2">${items.length} write-off${items.length!==1?'s':''} · Total loss R${fmt(total)}</div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Date</th><th>Product</th><th>Qty written off</th><th class="text-end text-danger">Cost lost</th><th>By</th></tr></thead><tbody>`;
    items.forEach(w => {
      html += `<tr>
        <td style="font-size:11px">${w.date?.replace('T',' ').slice(0,16) || '-'}</td>
        <td>${w.product}</td>
        <td>${Math.abs(w.qty_change).toFixed(2)} ${w.base_unit}</td>
        <td class="text-end text-danger fw-semibold">R${fmt(w.cost)}</td>
        <td>${w.by}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openProfitDrilldown() {
  document.getElementById('drilldown-title').textContent = 'Profit Breakdown by Product';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    const items = await api(`/api/stats/drilldown/profit?${params}`);
    if (!items.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No data found.</div>';
      return;
    }
    const _admin      = isAdmin();
    const totalRev    = items.reduce((s, i) => s + i.revenue, 0);
    const totalProfit = items.reduce((s, i) => s + i.profit, 0);
    const overallMargin = totalRev > 0 ? (totalProfit / totalRev * 100).toFixed(1) : '-';
    let html = `<div class="row g-2 mb-3">
      <div class="${_admin ? 'col-4' : 'col-12'}"><div class="card border-success text-center py-2"><div class="small text-muted">Revenue</div><div class="fw-bold text-success">R${fmt(totalRev)}</div></div></div>
      ${_admin ? `
      <div class="col-4"><div class="card border-success text-center py-2"><div class="small text-muted">Gross Profit</div><div class="fw-bold text-success">R${fmt(totalProfit)}</div></div></div>
      <div class="col-4"><div class="card border-warning text-center py-2"><div class="small text-muted">Margin</div><div class="fw-bold text-warning">${overallMargin}%</div></div></div>
      ` : ''}
    </div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Product</th><th class="text-end">Qty</th><th class="text-end">Revenue</th>${_admin ? '<th class="text-end">COGS</th><th class="text-end text-success">Profit</th><th class="text-end text-warning">Margin</th>' : ''}</tr></thead><tbody>`;
    items.forEach(i => {
      const profitColor = i.profit >= 0 ? 'text-success' : 'text-danger';
      html += `<tr>
        <td>${i.product}</td>
        <td class="text-end">${i.qty_sold}</td>
        <td class="text-end">R${fmt(i.revenue)}</td>
        ${_admin ? `
        <td class="text-end text-muted">R${fmt(i.cogs)}</td>
        <td class="text-end fw-semibold ${profitColor}">R${fmt(i.profit)}</td>
        <td class="text-end text-warning">${i.margin != null ? i.margin + '%' : '-'}</td>
        ` : ''}
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openVoidedDrilldown() {
  document.getElementById('drilldown-title').textContent = 'Voided Transactions';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    params.set('voided', '1');
    const data = await api(`/api/stats/drilldown?${params}`);
    if (!data.transactions?.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No voided transactions found.</div>';
      return;
    }
    document.getElementById('drilldown-body').innerHTML = _renderDrilldownTransactions(data, {});
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

async function openCustomerListDrilldown(type) {
  const titles = { new: 'New Customers', returning: 'Returning Customers', all: 'Customers' };
  document.getElementById('drilldown-title').textContent = titles[type] || 'Customers';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = _statsFilterParams();
    params.set('type', type);
    const data = await api(`/api/stats/drilldown/customer-list?${params}`);
    const custs = data.customers || [];
    if (!custs.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No customers found.</div>';
      return;
    }
    const total = custs.reduce((s, c) => s + c.revenue, 0);
    let html = `<div class="small text-muted mb-2">${custs.length} customer${custs.length!==1?'s':''} · Total revenue R${fmt(total)}</div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Customer</th><th>Visits</th><th class="text-end">Revenue</th><th>Type</th><th>First Purchase</th></tr></thead><tbody>`;
    custs.forEach(c => {
      html += `<tr>
        <td>${c.name}</td>
        <td>${c.visits}</td>
        <td class="text-end fw-semibold">R${fmt(c.revenue)}</td>
        <td><span class="badge ${c.is_new ? 'bg-success' : 'bg-secondary'}">${c.is_new ? 'New' : 'Returning'}</span></td>
        <td class="small text-muted">${c.first_purchase ? c.first_purchase.slice(0,10) : '-'}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

function switchChartTab(tab) { _showChartTab(tab); }

let _topProductsGroupBy = 'product';

function _showChartTab(tab) {
  _statsChartTab = tab;
  ['daily','hourly','minute','top','top-rev','top-profit','suppliers','channels','customers','dow'].forEach(id => {
    const c = document.getElementById(`chart-${id}`);
    if (c) c.style.display = 'none';
  });
  document.querySelectorAll('[data-chart-tab]').forEach(b => {
    b.className = b.dataset.chartTab === tab
      ? 'btn btn-sm btn-primary'
      : 'btn btn-sm btn-outline-secondary';
  });
  const hint = document.getElementById('chart-click-hint');
  const drillableTabs = ['daily','hourly','minute','top-qty','top-rev','top-profit'];
  if (hint) hint.style.display = drillableTabs.includes(tab) ? '' : 'none';
  const groupByBar = document.getElementById('top-products-groupby');
  if (groupByBar) groupByBar.classList.toggle('d-none', !['top-rev','top-profit'].includes(tab));

  // Legend strip — cleared each time, populated per tab below
  const _legend = document.getElementById('stats-chart-legend');
  if (_legend) _legend.innerHTML = '';
  const _sw = (color, label) =>
    `<span class="d-inline-flex align-items-center gap-1"><span style="display:inline-block;width:12px;height:12px;border-radius:2px;background:${color};flex-shrink:0"></span><span>${label}</span></span>`;

  if (!_statsData) return;
  const j = _statsData;

  if (tab === 'daily') {
    const c = document.getElementById('chart-daily');
    c.style.display = '';
    const dayData = j.revenue_per_day;
    drawBarChart(c, dayData.map(d => d.date.slice(5)), dayData.map(d => d.revenue), {
      color: '#2a6f3e', color2: '#4caf7d', valuePrefix: 'R',
      yLabel: 'Revenue (R)', xLabel: 'Date',
      onBarClick: (lbl, val, i) => openDrilldown(`Sales on ${dayData[i].date}`, 'day', dayData[i].date),
    });
    if (_legend) _legend.innerHTML = `<span class="text-muted">Total sales revenue for each day — click a bar to see the transactions</span>`;

  } else if (tab === 'hourly') {
    const c = document.getElementById('chart-hourly');
    c.style.display = '';
    const hours = Array.from({length: 24}, (_, i) => i);
    const hourMap = Object.fromEntries((j.revenue_per_hour || []).map(x => [x.hour, x.revenue]));
    drawBarChart(c, hours.map(h => `${h}:00`), hours.map(h => hourMap[h] || 0), {
      color: '#1976d2', valuePrefix: 'R',
      yLabel: 'Revenue (R)', xLabel: 'Hour of day',
      onBarClick: (lbl, val, i) => { if (val > 0) openDrilldown(`Sales at ${i}:00`, 'hour', i); },
    });
    if (_legend) _legend.innerHTML = `<span class="text-muted">Revenue totalled by hour across all days in the selected range — click a bar to drill down</span>`;

  } else if (tab === 'minute') {
    const c = document.getElementById('chart-minute');
    c.style.display = '';
    const mins = j.revenue_per_minute || [];
    drawBarChart(c, mins.map(x => x.minute), mins.map(x => x.revenue), {
      color: '#00838f', valuePrefix: 'R',
      yLabel: 'Revenue (R)', xLabel: 'Time (hh:mm)',
      onBarClick: (lbl) => { if (lbl) openDrilldown(`Sales at ${lbl}`, 'minute', lbl); },
    });
    if (_legend) _legend.innerHTML = `<span class="text-muted">Revenue by minute — shows the busiest moments across the selected period</span>`;

  } else if (tab === 'top-qty') {
    const c = document.getElementById('chart-top');
    c.style.display = '';
    const products = j.top_products || [];
    const qtyVals = products.map(x => x.stat_unit_size ? x.normalized_qty : x.qty_sold);
    const qtyLabels = products.map(x => {
      if (x.stat_unit_size) return `${x.normalized_qty} portions`;
      const unit = x.base_unit || 'units';
      return `${x.qty_sold} ${unit}`;
    });
    drawBarChart(c, products.map(x => x.name), qtyVals, {
      color: '#e65100', valueSuffix: '',
      yLabel: 'Qty sold', xLabel: 'Product',
      tooltipFormatter: (val, i) => qtyLabels[i] || String(val),
      onBarClick: (lbl, val, i) => openDrilldown(`Sales of ${products[i]?.name}`, 'product', products[i]?.product_id),
    });
    if (_legend) _legend.innerHTML = `<span class="text-muted">Units (or estimated portions for weight/volume items) sold per product — click a bar to drill down</span>`;

  } else if (tab === 'top-rev') {
    const c = document.getElementById('chart-top-rev');
    c.style.display = '';
    const products = j.top_by_revenue || [];
    if (_topProductsGroupBy === 'category') {
      const catMap = {};
      products.forEach(p => { const k = p.category_name || 'Uncategorised'; catMap[k] = (catMap[k] || 0) + p.revenue; });
      const cats = Object.entries(catMap).sort((a, b) => b[1] - a[1]);
      drawBarChart(c, cats.map(x => x[0]), cats.map(x => x[1]), {
        color: '#7b1fa2', valuePrefix: 'R', yLabel: 'Revenue (R)', xLabel: 'Category',
      });
      if (_legend) _legend.innerHTML = `<span class="text-muted">Revenue grouped by product category</span>`;
    } else {
      drawBarChart(c, products.map(x => x.name), products.map(x => x.revenue), {
        color: '#7b1fa2', valuePrefix: 'R', yLabel: 'Revenue (R)', xLabel: 'Product',
        onBarClick: (lbl, val, i) => openDrilldown(`Sales of ${products[i]?.name}`, 'product', products[i]?.product_id),
      });
      if (_legend) _legend.innerHTML = `<span class="text-muted">Revenue generated per product — click a bar to drill down into those transactions</span>`;
    }

  } else if (tab === 'top-profit') {
    const c = document.getElementById('chart-top-profit');
    c.style.display = '';
    const products = j.top_by_profit || [];
    if (_topProductsGroupBy === 'category') {
      const catMap = {};
      products.forEach(p => { const k = p.category_name || 'Uncategorised'; catMap[k] = (catMap[k] || 0) + p.profit; });
      const cats = Object.entries(catMap).sort((a, b) => b[1] - a[1]);
      drawBarChart(c, cats.map(x => x[0]), cats.map(x => x[1]), {
        color: '#2e7d32', valuePrefix: 'R', yLabel: 'Profit (R)', xLabel: 'Category',
      });
      if (_legend) _legend.innerHTML = `<span class="text-muted">Gross profit grouped by product category</span>`;
    } else {
      drawBarChart(c, products.map(x => x.name), products.map(x => x.profit), {
        color: '#2e7d32', valuePrefix: 'R', yLabel: 'Profit (R)', xLabel: 'Product',
        onBarClick: (lbl, val, i) => openDrilldown(`Sales of ${products[i]?.name}`, 'product', products[i]?.product_id),
        tooltipFormatter: (val, i) => `R${fmt(val)} profit (${products[i]?.margin ?? '—'}% margin)`,
      });
      if (_legend) _legend.innerHTML = `<span class="text-muted">Gross profit per product (revenue minus COGS) — only products with tracked stock costs appear</span>`;
    }

  } else if (tab === 'suppliers') {
    const c = document.getElementById('chart-suppliers');
    c.style.display = '';
    const sups = j.supplier_breakdown || [];
    drawBarChart(c, sups.map(x => x.supplier), sups.map(x => x.total_cost), {
      color: '#5d4037', valuePrefix: 'R',
      yLabel: 'Purchase cost (R)', xLabel: 'Supplier',
      onBarClick: (lbl) => openSupplierDrilldown(lbl),
    });
    if (_legend) _legend.innerHTML = `<span class="text-muted">Total amount spent on stock purchases per supplier in the selected period</span>`;

  } else if (tab === 'channels') {
    const c = document.getElementById('chart-channels');
    c.style.display = '';
    api(`/api/stats/drilldown/channels?${_statsFilterParams()}`).then(d => {
      const daily = d.daily || [];
      if (!daily.length) { drawBarChart(c, [], [], {}); return; }

      // Aggregate totals for the legend
      const totalOnline    = daily.reduce((s, x) => s + x.online_rev,    0);
      const totalInstore   = daily.reduce((s, x) => s + x.instore_rev,   0);
      const cntOnline      = daily.reduce((s, x) => s + x.online_count,  0);
      const cntInstore     = daily.reduce((s, x) => s + x.instore_count, 0);

      // Interleave bars: even index = Online (green), odd index = In-store (blue)
      // Date label on the Online bar, empty on the In-store bar (keeps it readable)
      const barLabels = [], barValues = [];
      daily.forEach(x => {
        barLabels.push(x.date.slice(5), '');
        barValues.push(x.online_rev, x.instore_rev);
      });

      drawBarChart(c, barLabels, barValues, {
        color: '#4caf7d', color2: '#1976d2', valuePrefix: 'R',
        yLabel: 'Revenue (R)', xLabel: 'Date',
      });

      if (_legend) _legend.innerHTML =
        _sw('#4caf7d', `Online: R${fmt(totalOnline)} &nbsp;(${cntOnline} order${cntOnline !== 1 ? 's' : ''})`) +
        `<span class="text-muted mx-1">|</span>` +
        _sw('#1976d2', `In-store: R${fmt(totalInstore)} &nbsp;(${cntInstore} transaction${cntInstore !== 1 ? 's' : ''})`) +
        `<span class="text-muted ms-2">— each date shows two bars side by side</span>`;
    }).catch(() => {});

  } else if (tab === 'customers') {
    const c = document.getElementById('chart-customers');
    c.style.display = '';
    api(`/api/stats/drilldown/customers?${_statsFilterParams()}`).then(d => {
      const daily = d.new_vs_returning_daily || [];
      const freq  = d.frequency_distribution || {};
      if (!daily.length && !freq.once) { drawBarChart(c, [], [], {}); return; }
      const freqLabels = ['Once', '2–5×', '6+×'];
      const freqVals   = [freq.once || 0, freq.two_to_five || 0, freq.six_plus || 0];
      drawBarChart(c, freqLabels, freqVals, {
        color: '#1976d2', color2: '#42a5f5',
        yLabel: 'Unique customers', xLabel: 'Visit frequency',
      });
      if (_legend) _legend.innerHTML = `<span class="text-muted">How many unique customers visited <b>once</b>, <b>2–5 times</b>, or <b>6+ times</b> in the selected period</span>`;
    }).catch(() => {});

  } else if (tab === 'dow') {
    const c = document.getElementById('chart-dow');
    c.style.display = '';
    const dayData = (_statsData && _statsData.revenue_per_day) || [];
    if (!dayData.length) { drawBarChart(c, [], [], {}); return; }

    const DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    const rev  = [0,0,0,0,0,0,0];
    const txs  = [0,0,0,0,0,0,0];
    const days = [0,0,0,0,0,0,0];

    dayData.forEach(d => {
      const jsDay = new Date(d.date + 'T00:00:00').getDay();
      const idx   = jsDay === 0 ? 6 : jsDay - 1;
      rev[idx]  += d.revenue || 0;
      txs[idx]  += d.tx_count || 0;
      days[idx] += 1;
    });

    // Average per occurrence — avoids bias from unequal weekday counts
    const avgRev = rev.map((r, i) => days[i] > 0 ? Math.round(r / days[i] * 100) / 100 : 0);
    const avgTx  = txs.map((t, i) => days[i] > 0 ? Math.round(t / days[i] * 10) / 10  : 0);
    const labels = DOW.map((d, i) => days[i] > 0 ? `${d} (${days[i]}×)` : d);

    drawBarChart(c, labels, avgRev, {
      color: '#7b1fa2', color2: '#ba68c8', valuePrefix: 'R',
      yLabel: 'Avg revenue (R)', xLabel: 'Day of week',
      onBarClick: (lbl, val, i) => {
        if (avgTx[i] === 0) return;
        toast(`${DOW[i]}: avg R${fmt(avgRev[i])} revenue, avg ${avgTx[i]} transactions (${days[i]} occurrence${days[i]!==1?'s':''})`, 'info', 5000);
      },
    });

    if (_legend) _legend.innerHTML =
      `<span class="text-muted"><b>Average</b> revenue per weekday occurrence — bars show the mean across all matching days in the range. ` +
      `The number in brackets (e.g. <em>Mon (4×)</em>) is how many Mondays fall in the selected period. ` +
      `Click a bar for transaction detail. Total revenue ÷ occurrences = bar height.</span>`;
  }
}

function _populateStatsProductFilter() {
  const sel = document.getElementById('stats-product-filter');
  if (!sel || !STATE.products?.length) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All products</option>';
  STATE.products
    .filter(p => !p.is_archived && p.is_for_sale !== false)
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name;
      if (String(p.id) === String(current)) opt.selected = true;
      sel.appendChild(opt);
    });
}

async function loadStats() {
  const start     = document.getElementById('stats-start')?.value || todayISO();
  const end       = document.getElementById('stats-end')?.value   || todayISO();
  const productId = document.getElementById('stats-product-filter')?.value || '';
  const userId    = document.getElementById('stats-user-filter')?.value    || '';
  const label     = document.getElementById('stats-period-label');

  const dateLabel    = start === end ? start : `${start} → ${end}`;
  const productLabel = productId
    ? ` · ${STATE.products.find(p => String(p.id) === productId)?.name || 'Product'}`
    : '';
  if (label) label.textContent = dateLabel + productLabel;
  _updateExportFilterLabel();

  // Supplier chart only makes sense without a user filter (purchases aren't user-attributed)
  const empSection   = document.getElementById('stats-section-employees');
  const suppChartBtn = document.querySelector('[data-chart-tab="suppliers"]');
  if (userId && !productId) {
    if (suppChartBtn) suppChartBtn.style.display = 'none';
    if (_statsChartTab === 'suppliers') _statsChartTab = 'daily';
  } else {
    if (suppChartBtn) suppChartBtn.style.display = '';
  }
  // Employee table and kitchen row are always shown (queries now apply active filters)
  if (empSection) empSection.style.display = '';

  // Active filter chips
  const chipArea = document.getElementById('stats-active-filters');
  if (chipArea) {
    chipArea.innerHTML = '';
    const addChip = (text, onClear) => {
      const chip = document.createElement('span');
      chip.className = 'badge bg-primary d-flex align-items-center gap-1';
      chip.style.fontSize = '13px';
      chip.innerHTML = `${text} <span style="cursor:pointer;font-size:15px;line-height:1" title="Clear filter">×</span>`;
      chip.querySelector('span').onclick = onClear;
      chipArea.appendChild(chip);
    };
    if (userId) {
      // Look up name from STATE.users first, then fall back to the stats employee_stats payload,
      // then the employee table DOM — avoids "Employee #4" when users aren't loaded for the role.
      const fromUsers     = STATE.users.find(u => String(u.id) === String(userId))?.username;
      const fromStats     = _statsData?.employee_stats?.find(e => String(e.user_id) === String(userId))?.name;
      const fromDom       = document.querySelector(`[data-emp-id="${userId}"] .emp-name`)?.textContent?.trim();
      const empName       = fromUsers || fromStats || fromDom || `Employee #${userId}`;
      addChip(`Employee: ${empName}`, () => {
        const el = document.getElementById('stats-user-filter');
        if (el) el.value = '';
        loadStats();
      });
    }
    if (productId) {
      const pname = `Product: ${STATE.products.find(p => String(p.id) === productId)?.name || productId}`;
      addChip(pname, () => {
        const el = document.getElementById('stats-product-filter');
        if (el) el.value = '';
        loadStats();
      });
    }
    chipArea.style.display = (userId || productId) ? '' : 'none';
  }

  const params = new URLSearchParams({ start, end });
  if (productId) params.set('product_id', productId);
  if (userId)    params.set('user_id',    userId);

  try {
    const j = await api(`/api/stats?${params}`);
    _statsData = j;
    const el = id => document.getElementById(id);

    const cardClick = (cardEl, fn) => {
      if (!cardEl) return;
      cardEl.closest('.card').style.cursor = 'pointer';
      cardEl.closest('.card').onclick = fn;
    };

    el('stat-total')  && (el('stat-total').textContent  = `R${fmt(j.total_sales_value)}`);
    cardClick(el('stat-total'), () => openDrilldown(
      j.filtered_product_name ? `Transactions with ${j.filtered_product_name}` : 'All transactions',
      j.filtered_product_id   ? 'product' : 'range',
      j.filtered_product_id   || null
    ));

    el('stat-profit') && (el('stat-profit').textContent = `R${fmt(j.gross_profit)}`);
    el('stat-margin-sub') && (el('stat-margin-sub').textContent = j.gross_margin != null ? `${j.gross_margin}% margin` : '');
    cardClick(el('stat-profit'), () => openProfitDrilldown());

    el('stat-cogs')   && (el('stat-cogs').textContent   = j.total_cogs > 0 ? `R${fmt(j.total_cogs)}` : '-');
    cardClick(el('stat-cogs'), () => openProfitDrilldown());

    el('stat-margin') && (el('stat-margin').textContent = j.gross_margin != null ? `${j.gross_margin}%` : '-');
    cardClick(el('stat-margin'), () => openProfitDrilldown());

    el('stat-tx')     && (el('stat-tx').textContent     = j.transactions_count);
    cardClick(el('stat-tx'), () => openDrilldown(
      j.filtered_product_name ? `Transactions with ${j.filtered_product_name}` : 'All transactions',
      j.filtered_product_id   ? 'product' : 'range',
      j.filtered_product_id   || null
    ));

    el('stat-avg')    && (el('stat-avg').textContent    = `R${fmt(j.avg_basket_value)}`);
    cardClick(el('stat-avg'), () => openDrilldown(
      j.filtered_product_name ? `Transactions with ${j.filtered_product_name}` : 'All transactions',
      j.filtered_product_id   ? 'product' : 'range',
      j.filtered_product_id   || null
    ));

    el('stat-items')  && (el('stat-items').textContent  = j.total_items_sold);
    cardClick(el('stat-items'), () => openDrilldown(
      j.filtered_product_name ? `Transactions with ${j.filtered_product_name}` : 'All transactions',
      j.filtered_product_id   ? 'product' : 'range',
      j.filtered_product_id   || null
    ));

    if (el('stat-writeoff-cost')) {
      el('stat-writeoff-cost').textContent = j.total_writeoff_cost > 0 ? `R${fmt(j.total_writeoff_cost)}` : '-';
      cardClick(el('stat-writeoff-cost'), () => openWriteoffDrilldown());
    }
    if (el('stat-writeoff-count-sub')) {
      el('stat-writeoff-count-sub').textContent = j.writeoff_count > 0 ? `${j.writeoff_count} write-offs` : '';
    }
    if (el('stat-kitchen-count')) {
      el('stat-kitchen-count').textContent = j.kitchen_orders_today > 0 ? j.kitchen_orders_today : '-';
      cardClick(el('stat-kitchen-count'), () => openKitchenDrilldown());
    }
    if (el('stat-avg-wait')) {
      const waitSecs = j.avg_completed_wait ?? j.avg_wait_seconds;
      if (waitSecs != null) {
        const m = Math.floor(waitSecs / 60), s = Math.round(waitSecs % 60);
        el('stat-avg-wait').textContent = m > 0 ? `${m}m ${s}s` : `${s}s`;
      } else {
        el('stat-avg-wait').textContent = '-';
      }
      cardClick(el('stat-avg-wait'), () => openKitchenDrilldown());
    }

    // ── New customer / channel cards ──
    if (el('stat-new-customers'))      { el('stat-new-customers').textContent      = j.new_customers ?? '-';                                           cardClick(el('stat-new-customers'),      () => openCustomerListDrilldown('new')); }
    if (el('stat-returning-customers')){ el('stat-returning-customers').textContent = j.returning_customers ?? '-';                                   cardClick(el('stat-returning-customers'), () => openCustomerListDrilldown('returning')); }
    if (el('stat-repeat-rate'))        { el('stat-repeat-rate').textContent        = j.repeat_customer_rate != null ? j.repeat_customer_rate + '%' : '-'; cardClick(el('stat-repeat-rate'), () => switchChartTab('customers')); }
    if (el('stat-rev-per-customer'))   { el('stat-rev-per-customer').textContent   = j.revenue_per_customer != null ? `R${fmt(j.revenue_per_customer)}` : '-'; }
    if (el('stat-online-rev'))         { el('stat-online-rev').textContent         = j.online_revenue  != null ? `R${fmt(j.online_revenue)}`  : '-';  cardClick(el('stat-online-rev'),         () => switchChartTab('channels')); }
    if (el('stat-instore-rev'))        { el('stat-instore-rev').textContent        = j.instore_revenue != null ? `R${fmt(j.instore_revenue)}` : '-';  cardClick(el('stat-instore-rev'),        () => switchChartTab('channels')); }
    if (el('stat-void-rate'))          { el('stat-void-rate').textContent          = j.void_receipt_rate != null ? j.void_receipt_rate + '%' : '-';   cardClick(el('stat-void-rate'),          () => openVoidedDrilldown()); }

    if (el('stat-best-day') && j.best_day) {
      el('stat-best-day').textContent     = j.best_day.date;
      el('stat-best-day-val').textContent = `R${fmt(j.best_day.revenue)} · ${j.best_day.tx_count} sales`;
      cardClick(el('stat-best-day'), () => openDrilldown(`Best day - ${j.best_day.date}`, 'day', j.best_day.date, { context: 'best' }));
    }
    const worstCard = el('stat-worst-day')?.closest('.card');
    if (j.worst_day) {
      if (worstCard) worstCard.style.display = '';
      if (el('stat-worst-day')) {
        el('stat-worst-day').textContent     = j.worst_day.date;
        el('stat-worst-day-val').textContent = `R${fmt(j.worst_day.revenue)} · ${j.worst_day.tx_count} sales`;
        cardClick(el('stat-worst-day'), () => openDrilldown(`Worst day - ${j.worst_day.date}`, 'day', j.worst_day.date, { context: 'worst' }));
      }
    } else {
      if (worstCard) worstCard.style.display = 'none';
    }

    _showChartTab(_statsChartTab);

    // Employee performance table
    const empWrap = document.getElementById('employee-stats-table');
    if (empWrap) {
      const emps = j.employee_stats || [];
      if (!emps.length) {
        empWrap.innerHTML = '<div class="text-muted small">No employee data for this period.</div>';
      } else {
        const fmtMins = m => m >= 60 ? `${Math.floor(m/60)}h ${Math.round(m%60)}m` : `${Math.round(m)}m`;
        const fmtTime = iso => iso ? iso.replace('T',' ').slice(0,16) : '-';
        const COLS = 11;
        let rows = '';
        emps.forEach(e => {
          rows += `
            <tr class="emp-summary-row" style="cursor:pointer" data-emp-id="${e.user_id}" data-emp-name="${e.name}">
              <td class="fw-semibold">
                <span class="emp-toggle me-1 text-muted" style="font-size:11px">▶</span>${e.name}
              </td>
              <td class="text-end text-success">R${fmt(e.revenue)}</td>
              <td class="text-end">${e.transactions}</td>
              <td class="text-end">R${fmt(e.avg_tx_value)}</td>
              <td class="text-end">${e.items_sold}</td>
              <td class="text-end">${e.session_count || '-'}</td>
              <td class="text-end">${e.session_minutes ? fmtMins(e.session_minutes) : '-'}</td>
              <td class="text-end">${e.revenue_per_hour != null ? `R${fmt(e.revenue_per_hour)}` : '-'}</td>
              <td class="text-end">${e.tx_per_hour != null ? e.tx_per_hour.toFixed(1) : '-'}</td>
              <td class="text-end" style="font-size:11px">${e.first_sale ? e.first_sale.replace('T',' ').slice(0,16) : '-'}</td>
              <td class="text-end" style="font-size:11px">${e.last_sale  ? e.last_sale.replace('T',' ').slice(0,16)  : '-'}</td>
            </tr>
            <tr class="emp-detail-row d-none" data-detail-for="${e.user_id}">
              <td colspan="${COLS}" class="p-0 ps-3 pb-2">
                <table class="table table-sm table-bordered mb-0" style="font-size:12px;background:#f8f9fa">
                  <thead class="table-secondary">
                    <tr>
                      <th>#</th>
                      <th>Login</th>
                      <th>Logout</th>
                      <th>Last Activity</th>
                      <th class="text-end">Duration</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${(e.sessions || []).map((s, i) => `
                      <tr>
                        <td class="text-muted">${i + 1}</td>
                        <td>${fmtTime(s.login)}</td>
                        <td>${s.logout ? fmtTime(s.logout) : '-'}</td>
                        <td>${s.last_active ? fmtTime(s.last_active) : '-'}</td>
                        <td class="text-end">${fmtMins(s.duration_min)}</td>
                        <td>${s.open ? '<span class="badge bg-success">Active</span>' : '<span class="badge bg-secondary">Closed</span>'}</td>
                      </tr>`).join('')}
                    ${!(e.sessions || []).length ? `<tr><td colspan="6" class="text-muted text-center">No sessions</td></tr>` : ''}
                  </tbody>
                </table>
              </td>
            </tr>`;
        });
        empWrap.innerHTML = `
          <div class="table-responsive">
          <table class="table table-sm table-hover align-middle mb-0">
            <thead class="table-light">
              <tr>
                <th>Employee</th>
                <th class="text-end">Revenue</th>
                <th class="text-end">Transactions</th>
                <th class="text-end">Avg Sale</th>
                <th class="text-end">Items</th>
                <th class="text-end">Sessions</th>
                <th class="text-end">Time Logged In</th>
                <th class="text-end">R / hour</th>
                <th class="text-end">Sales / hour</th>
                <th class="text-end">First Sale</th>
                <th class="text-end">Last Sale</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
          </div>`;

        empWrap.querySelectorAll('.emp-summary-row').forEach(row => {
          row.addEventListener('click', () => {
            // If already filtered by this employee, toggle the session detail row
            const currentFilter = document.getElementById('stats-user-filter')?.value;
            if (currentFilter && currentFilter === row.dataset.empId) {
              const detail = empWrap.querySelector(`[data-detail-for="${row.dataset.empId}"]`);
              const toggle = row.querySelector('.emp-toggle');
              const open   = detail.classList.toggle('d-none');
              toggle.textContent = open ? '▶' : '▼';
              return;
            }
            // Filter all stats by this employee
            const userFilter = document.getElementById('stats-user-filter');
            if (userFilter) userFilter.value = row.dataset.empId;
            loadStats();
          });
        });
      }
    }

    // ── Till Sessions (Z-Reports) ──
    const tillWrap    = document.getElementById('till-sessions-table');
    const tillCards   = document.getElementById('till-summary-cards');
    const sessions    = j.till_sessions || [];
    if (tillCards) {
      if (sessions.length) {
        tillCards.style.display = '';
        const ouColor = n => n > 0 ? 'text-success' : n < 0 ? 'text-danger' : '';
        const fmtOU   = n => `${n >= 0 ? '+' : ''}R${fmt(Math.abs(n))}`;
        document.getElementById('till-stat-count')?.setAttribute('class', 'mb-0');
        document.getElementById('till-stat-count').textContent  = sessions.length;
        document.getElementById('till-stat-cash').textContent   = `R${fmt(j.till_cash_counted)}`;
        const netEl = document.getElementById('till-stat-net-ou');
        if (netEl) { netEl.textContent = fmtOU(j.till_net_over_under); netEl.className = `mb-0 ${ouColor(j.till_net_over_under)}`; }
        const avgEl = document.getElementById('till-stat-avg-ou');
        if (avgEl) { avgEl.textContent = fmtOU(j.till_avg_over_under); avgEl.className = `mb-0 ${ouColor(j.till_avg_over_under)}`; }
      } else {
        tillCards.style.display = 'none';
      }
    }
    if (tillWrap) {
      if (!sessions.length) {
        tillWrap.innerHTML = '<div class="text-muted small">No till closes in this period.</div>';
      } else {
        const ouClass = n => n > 0 ? 'text-success' : n < 0 ? 'text-danger fw-semibold' : '';
        const fmtOU   = n => `${n >= 0 ? '+' : ''}R${fmt(Math.abs(n))}`;
        const fmtDT   = iso => iso ? iso.replace('T', ' ').slice(0, 16) : '-';
        let rows = '';
        sessions.forEach(s => {
          rows += `<tr style="cursor:pointer" class="till-session-row"
                       data-opened="${s.opened_at}" data-closed="${s.closed_at}">
            <td style="font-size:12px;white-space:nowrap">${fmtDT(s.closed_at)}</td>
            <td>${escapeHtml(s.closed_by)}</td>
            <td style="font-size:11px;color:#666;white-space:nowrap">${fmtDT(s.opened_at)} → ${fmtDT(s.closed_at)}</td>
            <td class="text-end">R${fmt(s.pos_cash_sales)}</td>
            <td class="text-end">R${fmt(s.pos_card_sales)}</td>
            <td class="text-end fw-semibold">R${fmt(s.pos_total_sales)}</td>
            <td class="text-end text-muted">R${fmt(s.opening_float)}</td>
            <td class="text-end text-muted">R${fmt(s.expected_cash)}</td>
            <td class="text-end">R${fmt(s.counted_cash)}</td>
            <td class="text-end ${ouClass(s.over_under)}">${fmtOU(s.over_under)}</td>
            <td class="text-muted" style="font-size:11px">${escapeHtml(s.notes)}</td>
          </tr>`;
        });
        tillWrap.innerHTML = `<div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0">
          <thead class="table-light"><tr>
            <th>Closed</th><th>By</th><th>Session Period</th>
            <th class="text-end">Cash</th><th class="text-end">Card</th><th class="text-end">Total</th>
            <th class="text-end">Opening Float</th><th class="text-end">Expected Cash</th><th class="text-end">Counted</th>
            <th class="text-end">Over/Under</th><th>Notes</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table></div>`;
        tillWrap.querySelectorAll('.till-session-row').forEach(row => {
          row.addEventListener('click', () => {
            openTillDrilldown(
              `Transactions: ${row.dataset.opened.replace('T',' ').slice(0,16)} → ${row.dataset.closed.replace('T',' ').slice(0,16)}`,
              row.dataset.opened,
              row.dataset.closed
            );
          });
        });
      }
    }

    // Fire overhead stats in parallel — non-blocking
    loadOverheadStats(start, end, productId).catch(e => console.error('overhead stats', e));

  } catch (e) { console.error('loadStats', e); toast('Could not load stats', 'error'); }
}

async function loadOverheadStats(start, end, productId) {
  const wrap = document.getElementById('overhead-stats-body');
  if (!wrap) return;

  const params = new URLSearchParams({ start, end });
  if (productId) params.set('product_id', productId);

  const j = await api(`/api/stats/overhead?${params}`);
  const byType = j.by_type    || [];
  const bySup  = j.by_supplier || [];
  const byProd = j.by_produce  || [];

  if (!byType.length && !bySup.length && !byProd.length) {
    wrap.innerHTML = '<div class="text-muted small">No overhead costs recorded in this period.</div>';
    return;
  }

  let html = '';

  if (byType.length) {
    const typeTotal = byType.reduce((s, r) => s + parseFloat(r.total || 0), 0);
    html += `<div class="mb-3"><div class="small fw-semibold mb-1">By Type</div>
      <div class="table-responsive"><table class="table table-sm table-hover align-middle mb-0">
        <thead class="table-light"><tr>
          <th>Type</th><th class="text-end">Total</th><th class="text-end">Batches</th><th class="text-end">% of period</th>
        </tr></thead><tbody>`;
    byType.forEach(r => {
      const pct = typeTotal > 0 ? ((r.total / typeTotal) * 100).toFixed(1) : 0;
      html += `<tr style="cursor:pointer" onclick="openOverheadTypeDrilldown('${r.type}')">
        <td>${_COST_TYPE_LABELS[r.type] || r.type}</td>
        <td class="text-end fw-semibold">R${fmt(r.total)}</td>
        <td class="text-end">${r.count}</td>
        <td class="text-end">${pct}%</td>
      </tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  if (bySup.length) {
    html += `<div class="mb-3"><div class="small fw-semibold mb-1">By Supplier</div>
      <div class="table-responsive"><table class="table table-sm align-middle mb-0">
        <thead class="table-light"><tr>
          <th>Supplier</th><th class="text-end">Base Cost</th><th class="text-end">Overhead</th><th class="text-end">Uplift %</th>
        </tr></thead><tbody>`;
    bySup.forEach(r => {
      html += `<tr>
        <td>${escapeHtml(r.supplier_name || 'Unknown')}</td>
        <td class="text-end">R${fmt(r.base_cost)}</td>
        <td class="text-end text-warning fw-semibold">R${fmt(r.overhead_total)}</td>
        <td class="text-end">${r.uplift_pct != null ? r.uplift_pct.toFixed(1) + '%' : '-'}</td>
      </tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  if (byProd.length) {
    html += `<div class="mb-3"><div class="small fw-semibold mb-1">Produce Overhead</div>
      <div class="table-responsive"><table class="table table-sm align-middle mb-0">
        <thead class="table-light"><tr>
          <th>Product</th><th class="text-end">Ingredient Cost</th><th class="text-end">Overhead</th><th class="text-end">Total</th><th class="text-end">Overhead %</th>
        </tr></thead><tbody>`;
    byProd.forEach(r => {
      html += `<tr>
        <td>${escapeHtml(r.product_name)}</td>
        <td class="text-end">R${fmt(r.ingredient_cost)}</td>
        <td class="text-end text-warning">R${fmt(r.overhead)}</td>
        <td class="text-end fw-semibold">R${fmt(r.total)}</td>
        <td class="text-end">${r.overhead_pct != null ? r.overhead_pct.toFixed(1) + '%' : '-'}</td>
      </tr>`;
    });
    html += `</tbody></table></div></div>`;
  }

  wrap.innerHTML = html;
}

async function openOverheadTypeDrilldown(type) {
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal'));
  const label = _COST_TYPE_LABELS[type] || type;
  document.getElementById('drilldown-title').textContent = `Overhead — ${label}`;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted small">Loading…</div>';
  modal.show();
  try {
    const start  = document.getElementById('stats-start')?.value || todayISO();
    const end    = document.getElementById('stats-end')?.value   || todayISO();
    const params = new URLSearchParams({ type, start, end });
    const rows   = await api(`/api/stats/drilldown/overhead-type?${params}`);
    if (!rows.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted small">No records found.</div>';
      return;
    }
    let html = `<div class="table-responsive"><table class="table table-sm align-middle mb-0">
      <thead class="table-light"><tr>
        <th>Date</th><th>Product</th><th>Supplier</th><th>Label</th><th class="text-end">Amount</th>
      </tr></thead><tbody>`;
    rows.forEach(r => {
      html += `<tr>
        <td style="white-space:nowrap;font-size:12px">${r.date}</td>
        <td>${escapeHtml(r.product_name || '-')}</td>
        <td>${escapeHtml(r.supplier_name || '-')}</td>
        <td class="text-muted" style="font-size:12px">${escapeHtml(r.label || '')}</td>
        <td class="text-end fw-semibold">R${fmt(r.amount)}</td>
      </tr>`;
    });
    html += `</tbody></table></div>`;
    document.getElementById('drilldown-body').innerHTML = html;
  } catch (e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger small">${e.message}</div>`;
  }
}

document.querySelectorAll('[data-chart-tab]').forEach(btn => {
  btn.addEventListener('click', () => _showChartTab(btn.dataset.chartTab));
});
document.getElementById('top-products-groupby')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-groupby]');
  if (!btn) return;
  _topProductsGroupBy = btn.dataset.groupby;
  document.querySelectorAll('[data-groupby]').forEach(b => {
    b.className = b.dataset.groupby === _topProductsGroupBy
      ? 'btn btn-xs btn-primary'
      : 'btn btn-xs btn-outline-secondary';
    b.style.cssText = 'font-size:11px;padding:2px 7px';
  });
  _showChartTab(_statsChartTab);
});
document.getElementById('btn-refresh-stats')?.addEventListener('click', loadStats);
document.getElementById('stats-product-filter')?.addEventListener('change', loadStats);
document.getElementById('stats-start')?.addEventListener('change', loadStats);
document.getElementById('stats-end')?.addEventListener('change', loadStats);
document.getElementById('stats-start')?.addEventListener('input', loadStats);
document.getElementById('stats-end')?.addEventListener('input', loadStats);
_initStatsPresets();

// ── Exports - all use the active stats filters ──
function _exportParams() {
  const s         = document.getElementById('stats-start')?.value || todayISO();
  const e         = document.getElementById('stats-end')?.value   || todayISO();
  const productId = document.getElementById('stats-product-filter')?.value || '';
  const userId    = document.getElementById('stats-user-filter')?.value    || '';
  const p = new URLSearchParams({ start: s, end: e });
  if (productId) p.set('product_id', productId);
  if (userId)    p.set('user_id',    userId);
  return p;
}

function _updateExportFilterLabel() {
  const label = document.getElementById('export-filter-label');
  if (!label) return;
  const s         = document.getElementById('stats-start')?.value || todayISO();
  const e         = document.getElementById('stats-end')?.value   || todayISO();
  const productId = document.getElementById('stats-product-filter')?.value || '';
  const userId    = document.getElementById('stats-user-filter')?.value    || '';
  const dateStr   = s === e ? s : `${s} → ${e}`;
  const prodStr   = productId
    ? ` · ${STATE.products.find(p => String(p.id) === productId)?.name || 'Product'}`
    : '';
  const userStr   = userId && _statsData?.filtered_user_name
    ? ` · ${_statsData.filtered_user_name}`
    : '';
  label.textContent = `(${dateStr}${prodStr}${userStr})`;
}

document.getElementById('btn-export-csv')?.addEventListener('click', () => {
  window.open(`/admin/export/transactions?${_exportParams()}`, '_blank', 'noopener');
});
document.getElementById('btn-export-profit')?.addEventListener('click', () => {
  window.open(`/admin/export/profit?${_exportParams()}`, '_blank', 'noopener');
});
document.getElementById('btn-export-writeoffs')?.addEventListener('click', () => {
  window.open(`/admin/export/writeoffs?${_exportParams()}`, '_blank', 'noopener');
});
document.getElementById('btn-export-suppliers')?.addEventListener('click', () => {
  window.open(`/admin/export/suppliers?${_exportParams()}`, '_blank', 'noopener');
});
document.getElementById('btn-export-staff')?.addEventListener('click', () => {
  window.open(`/admin/export/staff?${_exportParams()}`, '_blank', 'noopener');
});
document.getElementById('btn-export-till-sessions')?.addEventListener('click', () => {
  window.open(`/admin/export/till-sessions?${_exportParams()}`, '_blank', 'noopener');
});

// ═══════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════
// DEPLOY SCHEDULER (QA only)
// ═══════════════════════════════════════════════════════

async function loadDeployStatus() {
  try {
    const d = await api('/api/deploy-schedule/status');
    const banner = document.getElementById('deploy-status-banner');
    if (banner) {
      const pending = d.pending_schedule;
      const last = d.last_deploy;
      banner.className = 'alert mb-3 ' + (pending ? 'alert-info' : 'alert-secondary');
      banner.innerHTML = `
        <strong>Current:</strong> ${d.current_env.toUpperCase()}
        ${pending ? `&nbsp;|&nbsp; <strong>⏰ Next deploy:</strong> ${new Date(pending.scheduled_at).toLocaleString()} - ${pending.description || 'no description'}
          <button class="btn btn-danger btn-sm ms-2" onclick="cancelSchedule(${pending.id})">Cancel</button>` : '&nbsp;|&nbsp; No pending schedule'}
        ${last ? `<br><small class="text-muted">Last deploy: ${new Date(last.executed_at).toLocaleString()} - <span class="${last.status === 'done' ? 'text-success' : 'text-danger'}">${last.status}</span></small>` : ''}
      `;
    }
    const tbody = document.getElementById('deploy-history-body');
    if (tbody) {
      const rows = await api('/api/deploy-schedule');
      tbody.innerHTML = rows.map(r => `
        <tr>
          <td class="small">${new Date(r.scheduled_at).toLocaleString()}</td>
          <td>${r.description || '-'}</td>
          <td><span class="badge ${r.status === 'done' ? 'bg-success' : r.status === 'failed' ? 'bg-danger' : r.status === 'pending' ? 'bg-primary' : r.status === 'running' ? 'bg-warning text-dark' : 'bg-secondary'}">${r.status}</span></td>
          <td class="small">${r.executed_at ? new Date(r.executed_at).toLocaleString() : '-'}</td>
          <td>${r.status === 'pending' ? `<button class="btn btn-outline-danger btn-sm" onclick="cancelSchedule(${r.id})">Cancel</button>` : ''}</td>
        </tr>
      `).join('');
    }
  } catch (e) { toast('Deploy status error: ' + e.message, 'danger'); }
}

async function scheduleDeploySubmit() {
  const dt = document.getElementById('schedule-datetime')?.value;
  const desc = document.getElementById('schedule-description')?.value?.trim();
  if (!dt) { toast('Please select a date and time', 'warning'); return; }
  const scheduledAt = new Date(dt).toISOString();
  if (new Date(scheduledAt) <= new Date()) { toast('Scheduled time must be in the future', 'warning'); return; }
  try {
    await api('/api/deploy-schedule', { method: 'POST', body: JSON.stringify({ scheduled_at: scheduledAt, description: desc }) });
    toast(`Deploy scheduled for ${new Date(scheduledAt).toLocaleString()}`, 'success');
    loadDeployStatus();
  } catch (e) { toast('Schedule failed: ' + e.message, 'danger'); }
}

async function cancelSchedule(id) {
  if (!confirm('Cancel this scheduled deploy?')) return;
  try {
    await api(`/api/deploy-schedule/${id}`, { method: 'DELETE' });
    toast('Schedule cancelled', 'success');
    loadDeployStatus();
  } catch (e) { toast('Cancel failed: ' + e.message, 'danger'); }
}

async function deployNow() {
  if (!confirm('Deploy QA code to PROD now? PROD will restart (~30s downtime).')) return;
  try {
    const d = await api('/api/deploy-schedule/execute', { method: 'POST' });
    toast('Deploy started - check status for progress', 'info', 5000);
    setTimeout(loadDeployStatus, 3000);
  } catch (e) { toast('Deploy failed: ' + e.message, 'danger'); }
}

async function rollbackNow() {
  if (!confirm('Roll PROD back to the PREVIOUS image? PROD restarts (~30s downtime). The database is NOT changed.')) return;
  try {
    const d = await api('/api/deploy-schedule/rollback', { method: 'POST' });
    toast('Rollback queued - runs within 60s via host. Check status for progress.', 'info', 5000);
    setTimeout(loadDeployStatus, 3000);
  } catch (e) { toast('Rollback failed: ' + e.message, 'danger'); }
}

// ═══════════════════════════════════════════════════════
// CSV PRODUCT IMPORT
// ═══════════════════════════════════════════════════════

let _importPreviewData = null;

function openImportModal() {
  _importPreviewData = null;
  document.getElementById('import-file').value = '';
  document.getElementById('import-preview-section').style.display = 'none';
  document.getElementById('import-loading').style.display = 'none';
  document.getElementById('btn-import-valid').style.display = 'none';
  document.getElementById('btn-import-strict').style.display = 'none';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('importModal')).show();
}

document.getElementById('import-file')?.addEventListener('change', async function() {
  if (!this.files[0]) return;
  await previewImport();
});

async function previewImport() {
  const fileEl = document.getElementById('import-file');
  if (!fileEl.files[0]) return;

  document.getElementById('import-loading').style.display = '';
  document.getElementById('import-preview-section').style.display = 'none';
  document.getElementById('btn-import-valid').style.display = 'none';
  document.getElementById('btn-import-strict').style.display = 'none';

  try {
    const formData = new FormData();
    formData.append('file', fileEl.files[0]);
    const allowName = document.getElementById('import-allow-name-match').checked;
    const resp = await fetch(`/api/products/import?mode=preview&allow_name_match=${allowName}`, {
      method: 'POST', body: formData,
    });
    const data = await resp.json();
    if (!resp.ok) { toast(data.error || 'Preview failed', 'danger'); return; }

    _importPreviewData = data;
    renderImportPreview(data);
  } catch (e) {
    toast('Preview failed: ' + e.message, 'danger');
  } finally {
    document.getElementById('import-loading').style.display = 'none';
  }
}

function renderImportPreview(data) {
  const s = data.summary;
  const summaryEl = document.getElementById('import-summary');
  summaryEl.innerHTML = `
    <strong>${data.rows?.length || 0} rows parsed</strong> -
    <span class="text-success"><i class="bi bi-circle-fill me-1"></i>${s.create} create</span>
    <span class="text-warning ms-2"><i class="bi bi-circle-fill me-1"></i>${s.update} update</span>
    <span class="text-secondary ms-2"><i class="bi bi-circle me-1"></i>${s.unchanged} unchanged</span>
    <span class="text-danger ms-2"><i class="bi bi-circle-fill me-1"></i>${s.error} errors</span>
    ${s.skip ? `<span class="text-muted ms-2"><i class="bi bi-skip-forward me-1"></i>${s.skip} skip</span>` : ''}
  `;

  const dupWarn = document.getElementById('import-duplicate-warning');
  if (data.duplicate_warning) {
    dupWarn.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>' + data.duplicate_warning;
    dupWarn.style.display = '';
  } else {
    dupWarn.style.display = 'none';
  }

  const tbody = document.getElementById('import-preview-body');
  tbody.innerHTML = (data.rows || []).map(r => {
    const actionClass = r.action === 'error' ? 'table-danger' : r.action === 'update' ? 'table-warning' :
      r.action === 'create' ? 'table-success' : '';
    const actionBadge = r.action === 'error' ? '<span class="badge bg-danger">Error</span>'
      : r.action === 'update' ? '<span class="badge bg-warning text-dark">Update</span>'
      : r.action === 'create' ? '<span class="badge bg-success">Create</span>'
      : r.action === 'unchanged' ? '<span class="badge bg-secondary">Unchanged</span>'
      : '<span class="badge bg-light text-dark">Skip</span>';
    const detail = r.action === 'error'
      ? `<span class="text-danger">${r.error}</span>`
      : r.changes ? Object.entries(r.changes).map(([k,v]) => `<small><b>${k}:</b> ${v}</small>`).join(' &nbsp; ')
      : '';
    const warnings = (r.warnings || []).length ? `<small class="text-warning ms-1"><i class="bi bi-exclamation-triangle me-1"></i>${r.warnings.join(', ')}</small>` : '';
    return `<tr class="${actionClass}">
      <td class="text-muted small">${r.row}</td>
      <td>${r.name || ''}</td>
      <td>${actionBadge}</td>
      <td>${detail}${warnings}</td>
    </tr>`;
  }).join('');

  document.getElementById('import-preview-section').style.display = '';
  if (s.create + s.update > 0) {
    document.getElementById('btn-import-valid').style.display = '';
    document.getElementById('btn-import-strict').style.display = '';
  }
}

async function doImport(mode) {
  const fileEl = document.getElementById('import-file');
  if (!fileEl.files[0]) return;
  if (!confirm(`${mode === 'strict' ? 'Strict import (all-or-nothing)' : 'Import valid rows'}?\nErrors will be ${mode === 'strict' ? 'rejected (nothing saved)' : 'skipped'}.`)) return;

  const formData = new FormData();
  formData.append('file', fileEl.files[0]);
  const allowName = document.getElementById('import-allow-name-match').checked;

  try {
    const resp = await fetch(`/api/products/import?mode=${mode}&allow_name_match=${allowName}`, {
      method: 'POST', body: formData,
    });
    const data = await resp.json();
    if (!resp.ok) { toast(data.error || 'Import failed', 'danger'); return; }

    const s = data.summary;
    toast(`Import done: ${s.create} created, ${s.update} updated, ${s.error} errors (${data.duration_ms}ms)`,
      s.error > 0 ? 'warning' : 'success', 5000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('importModal')).hide();
    await loadProducts();
  } catch (e) {
    toast('Import failed: ' + e.message, 'danger');
  }
}

// ── Opening Stock Import (ISSUE-34) ─────────────────────────────────────────
let _osPreviewData = null;

function openOpeningStockModal() {
  _osPreviewData = null;
  document.getElementById('os-import-file').value = '';
  document.getElementById('os-import-preview').classList.add('hidden');
  document.getElementById('btn-os-import').classList.add('hidden');
  document.getElementById('os-import-body').innerHTML = '';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('openingStockModal')).show();
}

document.getElementById('os-import-file')?.addEventListener('change', () => {
  _osPreviewData = null;
  document.getElementById('os-import-preview').classList.add('hidden');
  document.getElementById('btn-os-import').classList.add('hidden');
  document.getElementById('os-import-body').innerHTML = '';
});

document.getElementById('btn-os-preview')?.addEventListener('click', async () => {
  const fileEl = document.getElementById('os-import-file');
  if (!fileEl.files[0]) return toast('Select a CSV file first', 'warning');
  const formData = new FormData();
  formData.append('file', fileEl.files[0]);
  try {
    const resp = await fetch('/api/stock/opening-import?mode=preview', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) { toast(data.error || 'Preview failed', 'danger'); return; }
    _osPreviewData = data;

    const summary = document.getElementById('os-import-summary');
    summary.textContent = `${data.rows_ok} row(s) OK, ${data.rows_error} error(s)`;
    summary.className = data.rows_error > 0 ? 'mb-2 small fw-bold text-warning' : 'mb-2 small fw-bold text-success';

    const errDiv = document.getElementById('os-import-errors');
    if (data.errors && data.errors.length) {
      errDiv.innerHTML = data.errors.map(e => `Row ${e.row}: ${e.error}`).join('<br>');
      errDiv.classList.remove('hidden');
    } else {
      errDiv.classList.add('hidden');
    }

    const tbody = document.getElementById('os-import-body');
    tbody.innerHTML = data.rows.map(r => `
      <tr>
        <td>${r.row}</td><td>${r.product_code}</td><td>${r.name}</td>
        <td>${r.qty_base?.toFixed(3)}</td><td>${r.base_unit || ''}</td>
        <td>R${fmt(r.cost_per_base_unit)}</td>
        <td>${r.received_at ? r.received_at.slice(0,10) : '-'}</td>
      </tr>`).join('');
    document.getElementById('os-import-preview').classList.remove('hidden');
    if (data.rows_ok > 0 && data.rows_error === 0) {
      document.getElementById('os-import-count').textContent = data.rows_ok;
      document.getElementById('btn-os-import').classList.remove('hidden');
    } else {
      document.getElementById('btn-os-import').classList.add('hidden');
    }
  } catch (e) { toast('Preview error: ' + e.message, 'danger'); }
});

document.getElementById('btn-os-import')?.addEventListener('click', async () => {
  if (!_osPreviewData) return;
  if (!confirm(`Import ${_osPreviewData.rows_ok} opening stock row(s)? This cannot be undone.`)) return;
  const fileEl = document.getElementById('os-import-file');
  const formData = new FormData();
  formData.append('file', fileEl.files[0]);
  try {
    const resp = await fetch('/api/stock/opening-import?mode=import', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) { toast(data.error || 'Import failed', 'danger'); return; }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('openingStockModal')).hide();
    await loadIngredients();
    const runId = data.run_id;
    if (runId) {
      const toastEl = document.createElement('div');
      toastEl.className = 'toast align-items-center text-bg-success border-0 show position-fixed bottom-0 end-0 m-3';
      toastEl.style.zIndex = '9999';
      toastEl.innerHTML = `
        <div class="d-flex">
          <div class="toast-body">Opening stock imported: ${data.rows_imported} batch(es) created.
            <button class="btn btn-sm btn-outline-light ms-2" data-undo-import>Undo</button>
          </div>
          <button type="button" class="btn-close btn-close-white me-2 m-auto" data-dismiss-toast></button>
        </div>`;
      document.body.appendChild(toastEl);
      toastEl.querySelector('[data-dismiss-toast]').addEventListener('click', () => toastEl.remove());
      toastEl.querySelector('[data-undo-import]').addEventListener('click', async () => {
        if (!confirm(`Undo this import? All ${data.rows_imported} unconsumed batch(es) will be deleted.`)) return;
        try {
          const r = await api(`/api/stock/opening-import/${runId}`, { method: 'DELETE' });
          toast(`Undone: ${r.deleted} batch(es) deleted${r.skipped_consumed ? `, ${r.skipped_consumed} skipped (already used)` : ''}`, 'success', 4000);
          await loadIngredients();
        } catch (err) { toast(err.message || 'Undo failed', 'danger'); }
        toastEl.remove();
      });
      setTimeout(() => toastEl.remove(), 15000);
    } else {
      toast(`Opening stock imported: ${data.rows_imported} batch(es) created`, 'success', 5000);
    }
  } catch (e) { toast('Import error: ' + e.message, 'danger'); }
});

// ═══════════════════════════════════════════════════════
// SCALE MONITOR
// ═══════════════════════════════════════════════════════

let _scaleKeyboardData = [];
let _scaleAdvertData   = [];
let _scaleProducts     = [];  // for keyboard preset product picker

async function _updateScaleBanner() {
  try {
    const d = await api('/api/scale/status');
    document.getElementById('scale-ip-display').textContent = `${d.scale_ip}:${d.scale_port}`;
    document.getElementById('scale-reachable-display').innerHTML = d.scale_reachable ? '<i class="bi bi-check-circle-fill text-success me-1"></i>Yes' : '<i class="bi bi-x-circle-fill text-danger me-1"></i>No';
    document.getElementById('scale-insync-count').textContent = d.products_in_sync;
    document.getElementById('scale-pending-count').textContent = d.products_pending;
    document.getElementById('scale-error-count').textContent = d.products_error;
    const lr = d.last_run;
    document.getElementById('scale-last-run').textContent = lr
      ? `${lr.status} - ${new Date(lr.started_at).toLocaleTimeString()} (${lr.products_sent} sent, ${lr.products_failed} failed)`
      : 'Never';
    document.getElementById('scale-status-banner').className =
      `alert mb-3 d-flex gap-4 flex-wrap align-items-center ${d.scale_reachable ? 'alert-success' : 'alert-warning'}`;
    return d;
  } catch (e) {
    toast('Scale status error: ' + e.message, 'danger');
    return null;
  }
}

async function loadScaleTab() {
  await _updateScaleBanner();
  await Promise.all([loadScaleStatus(), loadScaleSyncSource()]);
}

async function loadScaleStatus() {
  try {
    const d = await api('/api/scale/status');
    _scaleProducts = d.products;
    document.getElementById('scale-products-body').innerHTML = d.products.map(p => {
      const badge = p.validation_error
        ? `<span class="badge bg-danger">Error</span>`
        : p.in_sync
          ? `<span class="badge bg-success">In Sync</span>`
          : `<span class="badge bg-warning text-dark">Pending</span>`;
      return `<tr class="${p.validation_error ? 'table-danger' : p.pending_change ? 'table-warning' : ''}">
        <td>${p.product_code || '-'}</td>
        <td>${p.name}</td>
        <td>${p.sold_by_weight ? `R${((p.price_per_unit||0)*1000).toFixed(2)}/kg` : `R${(p.price||0).toFixed(2)}`}</td>
        <td>${p.scale_tare || 0}g</td>
        <td>${p.scale_shelf_life || 0}d</td>
        <td>${badge}</td>
        ${isAdmin() ? `<td class="small text-muted">${p.last_synced_at ? new Date(p.last_synced_at).toLocaleString() : 'Never'}</td>
        <td class="small text-danger">${p.validation_error || p.last_sync_error || ''}</td>
        <td><button class="btn btn-outline-secondary btn-sm" onclick="scaleProductSync(${p.id})" title="Queue for sync"><i class="bi bi-arrow-up"></i></button></td>` : ''}
      </tr>`;
    }).join('');
  } catch (e) { toast('Scale status error: ' + e.message, 'danger'); }
}

async function loadScaleContents() {
  try {
    const d = await api('/api/scale/contents');
    const badge = document.getElementById('scale-plu-count-badge');
    badge.textContent = d.plu_count_on_scale !== null
      ? `- ${d.plu_count_on_scale} PLUs on scale, ${d.plu_count_tracked} tracked in POS`
      : `- ${d.plu_count_tracked} tracked (scale not reachable)`;

    document.getElementById('scale-contents-body').innerHTML = d.plus.map(p => {
      const isOrphan = !p.sync_to_scale || p.is_archived;
      const price = p.sold_by_weight
        ? `R${((p.price_per_unit||0)*1000).toFixed(2)}/kg`
        : `R${(p.price||0).toFixed(2)}`;
      const statusBadge = p.sync_status === 'removed'
        ? `<span class="badge bg-secondary">Removed</span>`
        : p.sync_status === 'ok'
          ? `<span class="badge bg-success">Synced</span>`
          : p.sync_status === 'error'
            ? `<span class="badge bg-danger">Error</span>`
            : isOrphan
              ? `<span class="badge bg-warning text-dark">Orphan</span>`
              : `<span class="badge bg-info text-dark">Pending</span>`;
      return `<tr class="${p.sync_status === 'removed' ? 'table-secondary' : isOrphan ? 'table-warning' : ''}">
        <td><strong>${p.product_code}</strong></td>
        <td>${p.name}</td>
        <td>-</td>
        <td>${price}</td>
        <td>${p.scale_tare || 0}g</td>
        <td>${p.scale_shelf_life || 0}d</td>
        <td>${statusBadge}</td>
        <td>${p.sync_status !== 'removed' ? `<button class="btn btn-outline-danger btn-sm" onclick="scaleDeletePlu(${p.product_code}, ${p.id})" title="Delete from scale"><i class="bi bi-x-lg"></i></button>` : ''}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8" class="text-muted text-center">No PLUs tracked - click "Read from Scale" to get live data</td></tr>';
  } catch (e) { toast('Scale contents error: ' + e.message, 'danger'); }
}


async function scaleDeletePlu(pluNo, productId) {
  if (!confirm(`Delete PLU ${pluNo} from scale? This overwrites it with a blank prohibited record.`)) return;
  try {
    const d = await api('/api/scale/delete-plu', {method:'POST', body: JSON.stringify({plu_no: pluNo, product_id: productId})});
    toast(`PLU ${pluNo} deleted from scale`, 'success');
    loadScaleContents();
  } catch (e) { toast('Delete failed: ' + e.message, 'danger'); }
}

async function loadScaleKeyboard() {
  try {
    const d = await api('/api/scale/keyboard');
    _scaleKeyboardData = d.slots;
    _renderKeyboardGrid();
  } catch (e) { toast('Keyboard load error: ' + e.message, 'danger'); }
}

function _renderKeyboardGrid() {
  const grid = document.getElementById('scale-keyboard-grid');
  grid.innerHTML = _scaleKeyboardData.map((slot, i) => {
    const prod = slot.product;
    const label = prod ? prod.name.substring(0, 10) : '';
    const plu   = prod ? `PLU ${prod.product_code}` : '';
    const cls   = prod ? 'btn-primary' : 'btn-outline-secondary';
    return `<button class="btn btn-sm ${cls}" style="width:90px;height:54px;font-size:10px;line-height:1.2;overflow:hidden"
      onclick="scaleKeyAssign(${i})" title="Key ${slot.key_id}${prod ? ': '+prod.name : ' (empty)'}">
      <div style="font-size:9px;color:#aaa">K${slot.key_id}</div>
      <div>${label}</div>
      <div style="font-size:9px">${plu}</div>
    </button>`;
  }).join('');
}

function scaleKeyAssign(idx) {
  const slot = _scaleKeyboardData[idx];
  // Build picker from scale products
  const prods = _scaleProducts.length
    ? _scaleProducts
    : [];
  const options = prods.map(p =>
    `<option value="${p.id}" ${slot.plu_no === p.id ? 'selected' : ''}>PLU ${p.product_code} - ${p.name}</option>`
  ).join('');
  const html = `
    <select id="kb-picker-select" class="form-select mb-2">
      <option value="">- Empty (clear key) -</option>
      ${options}
    </select>
    <button class="btn btn-primary btn-sm" onclick="scaleKeyConfirm(${idx})">Assign</button>
    <button class="btn btn-outline-secondary btn-sm ms-1" onclick="document.getElementById('kb-picker-modal-body').innerHTML=''">Cancel</button>
  `;
  // Simple inline modal using existing modal if available, else alert-style
  let el = document.getElementById('kb-picker-modal-body');
  if (!el) {
    el = document.createElement('div');
    el.id = 'kb-picker-modal-body';
    el.className = 'p-2 border rounded bg-white shadow mb-2';
    document.getElementById('scale-keyboard-grid').before(el);
  }
  el.innerHTML = `<strong>Key ${slot.key_id}</strong><br>${html}`;
}

function scaleKeyConfirm(idx) {
  const sel = document.getElementById('kb-picker-select');
  const productId = sel.value ? parseInt(sel.value) : null;
  const prod = productId ? _scaleProducts.find(p => p.id === productId) : null;
  _scaleKeyboardData[idx].plu_no  = productId;
  _scaleKeyboardData[idx].product = prod ? {id: prod.id, name: prod.name, product_code: prod.product_code} : null;
  document.getElementById('kb-picker-modal-body').innerHTML = '';
  _renderKeyboardGrid();
}

async function saveScaleKeyboard() {
  try {
    const d = await api('/api/scale/keyboard', {method:'POST', body: JSON.stringify({slots: _scaleKeyboardData})});
    toast(`Keyboard layout saved (${d.saved} slots). Scale will sync on next cycle.`, 'success');
  } catch (e) { toast('Save failed: ' + e.message, 'danger'); }
}

async function loadScaleAdverts() {
  try {
    const d = await api('/api/scale/adverts');
    _scaleAdvertData = d.slots;
    _renderAdvertList();
  } catch (e) { toast('Advert load error: ' + e.message, 'danger'); }
}

function _renderAdvertList() {
  document.getElementById('scale-adverts-list').innerHTML = _scaleAdvertData.map((slot, i) =>
    `<div class="col-md-6 col-lg-4">
      <div class="card card-body p-2">
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="badge bg-secondary">#${slot.slot}</span>
          <input type="checkbox" class="form-check-input" id="adv-en-${i}" ${slot.enabled ? 'checked' : ''}
            onchange="_scaleAdvertData[${i}].enabled = this.checked">
          <label class="form-check-label small" for="adv-en-${i}">Enabled</label>
        </div>
        <input type="text" class="form-control form-control-sm" maxlength="100"
          value="${slot.text.replace(/"/g,'&quot;')}"
          placeholder="Message text..."
          oninput="_scaleAdvertData[${i}].text = this.value">
      </div>
    </div>`
  ).join('');
}

async function saveScaleAdverts() {
  try {
    const d = await api('/api/scale/adverts', {method:'POST', body: JSON.stringify({slots: _scaleAdvertData})});
    toast(`Adverts saved (${d.saved} slots). Scale will sync on next cycle.`, 'success');
  } catch (e) { toast('Save failed: ' + e.message, 'danger'); }
}

async function scaleTestConnection() {
  try {
    const d = await api('/api/scale/test-connection', {method:'POST'});
    toast(d.reachable ? `Scale reachable at ${d.ip}:${d.port}` : `Scale NOT reachable at ${d.ip}:${d.port}`, d.reachable ? 'success' : 'warning');
  } catch (e) { toast('Connection test failed: ' + e.message, 'danger'); }
}

async function scalePreview() {
  try {
    const d = await api('/api/scale/preview', {method:'POST'});
    document.getElementById('scale-preview-result').style.display = '';
    document.getElementById('scale-preview-body').innerHTML = `
      <div class="d-flex gap-3 flex-wrap mb-2">
        <span class="badge bg-primary fs-6">Send: ${d.will_send.length}</span>
        <span class="badge bg-secondary fs-6">Skip: ${d.will_skip.length}</span>
        <span class="badge bg-danger fs-6">Delete orphans: ${d.will_delete.length}</span>
        <span class="badge bg-warning text-dark fs-6">Errors: ${d.will_error.length}</span>
      </div>
      ${d.will_send.length ? `<div class="mb-1"><strong>Will send:</strong> ${d.will_send.map(p=>`PLU ${p.product_code} ${p.name} (${p.reason})`).join(', ')}</div>` : ''}
      ${d.will_delete.length ? `<div class="mb-1 text-danger"><strong>Will delete:</strong> ${d.will_delete.map(p=>`PLU ${p.product_code} ${p.name}`).join(', ')}</div>` : ''}
      ${d.will_error.length ? `<div class="mb-1 text-danger"><strong>Errors:</strong> ${d.will_error.map(p=>`PLU ${p.product_code||'?'} ${p.name}: ${p.error}`).join(', ')}</div>` : ''}`;
  } catch (e) { toast('Preview failed: ' + e.message, 'danger'); }
}

async function scaleProductSync(productId) {
  try {
    const d = await api(`/api/scale/products/${productId}/sync`, {method:'POST'});
    toast(`PLU ${d.product_code} (${d.name}) queued for sync`, 'success');
    loadScaleStatus();
  } catch (e) { toast('Sync failed: ' + e.message, 'danger'); }
}

async function scaleForceResync() {
  if (!confirm('This will mark all scale products as needing resync. Continue?')) return;
  try {
    const d = await api('/api/scale/force-resync', {method:'POST'});
    toast(`Marked ${d.products_marked} products for resync`, 'success');
    loadScaleStatus();
  } catch (e) { toast('Force resync failed: ' + e.message, 'danger'); }
}

async function loadScaleSyncSource() {
  try {
    const d = await api('/api/scale/sync-source');
    _applyScaleSyncSource(d.source);
  } catch (e) { console.warn('Could not load sync source', e); }
}

function _applyScaleSyncSource(source) {
  const labels = { prod: 'Syncing from PROD', qa: 'Syncing from QA', none: 'Sync disabled' };
  const note = { prod: 'Scale receives live product data', qa: 'Scale receives QA test data', none: 'Scale sync is paused' };
  document.getElementById('sync-source-note').textContent = note[source] || '';
  ['prod','qa','none'].forEach(s => {
    const btn = document.getElementById(`sync-src-${s}`);
    if (!btn) return;
    btn.classList.toggle('active', s === source);
    if (s === source) {
      btn.classList.remove('btn-outline-success','btn-outline-primary','btn-outline-secondary');
      btn.classList.add(s === 'prod' ? 'btn-success' : s === 'qa' ? 'btn-primary' : 'btn-secondary');
    } else {
      btn.classList.remove('btn-success','btn-primary','btn-secondary');
      btn.classList.add(s === 'prod' ? 'btn-outline-success' : s === 'qa' ? 'btn-outline-primary' : 'btn-outline-secondary');
    }
  });
}

async function setScaleSyncSource(source) {
  try {
    const d = await api('/api/scale/sync-source', {method:'POST', body: JSON.stringify({source})});
    _applyScaleSyncSource(d.source);
    toast(`Scale sync source set to: ${source.toUpperCase()}`, 'success');
  } catch (e) { toast('Failed to set sync source: ' + e.message, 'danger'); }
}

// Load sync runs when that tab is clicked
document.addEventListener('DOMContentLoaded', () => {
  document.querySelector('[href="#scale-tab-runs"]')?.addEventListener('click', async () => {
    try {
      const runs = await api('/api/scale/sync-runs');
      document.getElementById('scale-runs-body').innerHTML = runs.map(r => `
        <tr>
          <td class="small">${new Date(r.started_at).toLocaleString()}</td>
          <td>${r.run_type}</td>
          <td><span class="badge ${r.status==='ok'?'bg-success':r.status==='running'?'bg-primary':'bg-danger'}">${r.status}</span></td>
          <td>${r.products_sent}</td>
          <td>${r.products_failed}</td>
          <td>${r.orphans_detected}</td>
          <td class="small text-danger">${r.error_message||''}</td>
        </tr>`).join('');
    } catch (e) { toast('Runs load error: ' + e.message, 'danger'); }
  });
});

// ═══════════════════════════════════════════════════════
// SETTINGS
// ═══════════════════════════════════════════════════════
let _globalMarkupPct = 40;  // default; overwritten by loadSettings

async function loadSettings() {
  try {
    const j = await api('/api/settings');
    _globalMarkupPct = parseFloat(j.markup_percent) || 40;
    // Load receipt printer preference into STATE
    if (j.receipt_printer_id) {
      STATE._receiptPrinterId = parseInt(j.receipt_printer_id) || null;
    }
  } catch {}
}

// ═══════════════════════════════════════════════════════
// KITCHEN QUEUE
// ═══════════════════════════════════════════════════════
let _kitchenRefreshTimer = null;
let _kitchenTimerInterval = null;
let _kitchenOrders = [];         // current live queue

async function loadKitchenOrders() {
  try {
    _kitchenOrders = await api('/api/kitchen/orders');
    renderKitchenQueue(_kitchenOrders);
    updateKitchenBadge();
  } catch (e) { console.error('loadKitchenOrders', e); }
}

async function loadKitchenHistory() {
  try {
    const data = await api(`/api/kitchen/orders?include_completed=1&date=${todayISO()}`);
    renderKitchenHistory(data);
  } catch (e) { console.error('loadKitchenHistory', e); }
}

function updateKitchenBadge() {
  const count  = _kitchenOrders.filter(o => o.status === 'pending').length;
  const badge  = document.getElementById('kitchen-badge');
  if (!badge) return;
  badge.textContent = count;
  count > 0 ? show(badge) : hide(badge);
}

function fmtWait(seconds) {
  if (seconds == null) return '';
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function _kitchenIngHtml(ingredients) {
  if (!ingredients || !ingredients.length) return '';
  return `<ul class="kitchen-ingredient-list list-unstyled mb-0">` +
    ingredients.map(i => {
      const qtyDisplay = i.qty >= 1000 && i.base_unit === 'ml'
        ? `${(i.qty/1000).toFixed(2)}L`
        : i.qty >= 1000 && i.base_unit === 'g'
        ? `${(i.qty/1000).toFixed(2)}kg`
        : `${i.qty % 1 === 0 ? i.qty : i.qty.toFixed(1)}${i.base_unit}`;
      if (i.removed)     return `<li style="background:#f8d7da;border-radius:4px;padding:2px 6px;font-weight:700;color:#842029;text-decoration:line-through"><i class="bi bi-x-lg me-1"></i>NO ${i.name}</li>`;
      if (i.extra)       return `<li style="background:#d1e7dd;border-radius:4px;padding:2px 6px;font-weight:700;color:#0a3622">+ EXTRA: ${i.name} - <strong>${qtyDisplay}</strong></li>`;
      if (i.substituted) {
        const origNote = i.original_name ? ` <span style="text-decoration:line-through;opacity:.6">${i.original_name}</span>` : '';
        return `<li style="background:#fff3cd;border-radius:4px;padding:2px 6px;font-weight:700;color:#856404"><i class="bi bi-flag me-1"></i>SWAP: ${i.name}${origNote} - <strong>${qtyDisplay}</strong></li>`;
      }
      return `<li>• ${i.name} - <strong>${qtyDisplay}</strong></li>`;
    }).join('') + `</ul>`;
}

function renderKitchenQueue(orders) {
  const host = document.getElementById('kitchen-queue-list');
  if (!host) return;

  if (orders.length === 0) {
    host.innerHTML = `
      <div class="text-center py-5 text-muted">
        <div style="font-size:3rem"><i class="bi bi-check-circle-fill text-success"></i></div>
        <div class="mt-2 fw-bold">Queue is empty</div>
        <div class="small">No pending orders</div>
      </div>`;
    return;
  }

  // Group orders by sale_id, preserving queue order (first order in group sets priority)
  const groupMap = new Map();
  orders.forEach(o => {
    if (!groupMap.has(o.sale_id)) groupMap.set(o.sale_id, []);
    groupMap.get(o.sale_id).push(o);
  });
  const groups = [...groupMap.entries()]; // [[sale_id, [orders]], ...]

  host.innerHTML = '';
  groups.forEach(([saleId, grpOrders], grpIdx) => {
    const card = document.createElement('div');
    const justMoved = _kitchenLastMovedId === saleId;
    // Use the worst (max) wait in the group so the timer reflects how long the customer has waited
    const serverWait = Math.max(...grpOrders.map(o => o.wait_seconds || 0));
    const urgent     = serverWait > 600;
    const teller     = grpOrders[0].teller || '';
    const saleShort  = String(saleId).slice(0, 8);

    card.className = `kitchen-card status-pending${justMoved ? ' kitchen-moved' : ''}`;
    card.dataset.saleId    = saleId;
    card.dataset.waitStart = serverWait;
    card.dataset.loadedAt  = Date.now();

    // Build one item row per order in the group
    const itemsHtml = grpOrders.map(o => `
      <div class="mb-2" style="border-bottom:1px solid rgba(0,0,0,.07);padding-bottom:6px">
        <div class="d-flex align-items-center gap-2 mb-1">
          <span class="kitchen-product-name">${o.product_name}</span>
          <span class="kitchen-qty-badge">×${o.qty % 1 === 0 ? o.qty : o.qty.toFixed(1)}</span>
        </div>
        ${_kitchenIngHtml(o.ingredients)}
        ${o.notes ? `<div class="small text-info mt-1"><i class="bi bi-sticky me-1"></i>${o.notes}</div>` : ''}
      </div>`).join('');

    card.innerHTML = `
      <div class="d-flex justify-content-between align-items-start gap-2">
        <div class="d-flex align-items-start gap-3">
          <div class="kitchen-move-btns">
            <button class="btn btn-outline-secondary" data-ko-move="up"   data-sale-id="${saleId}" title="Move up"   ${grpIdx === 0 ? 'disabled' : ''}>▲</button>
            <button class="btn btn-outline-secondary" data-ko-move="down" data-sale-id="${saleId}" title="Move down" ${grpIdx === groups.length-1 ? 'disabled' : ''}>▼</button>
          </div>
          <div style="flex:1">
            <div class="d-flex align-items-center gap-2 mb-2">
              <span class="badge bg-secondary">Queue #${grpIdx + 1}</span>
              <span class="badge bg-primary" title="Order ID">#${saleShort}</span>
              ${teller ? `<span class="badge bg-light text-dark border"><i class="bi bi-person me-1"></i>${teller}</span>` : ''}
            </div>
            ${itemsHtml}
          </div>
        </div>
        <div class="text-end" style="min-width:70px">
          <div class="kitchen-timer ${urgent ? 'urgent' : ''}" data-timer-sale="${saleId}">
            ${fmtWait(serverWait)}
          </div>
          <div class="small text-muted">waiting</div>
        </div>
      </div>
      <div class="kitchen-actions">
        <button class="btn btn-success btn-lg-touch flex-fill" data-ko-done-sale="${saleId}"><i class="bi bi-check-lg me-1"></i>Done - whole order</button>
        <button class="btn btn-outline-danger" data-ko-cancel-sale="${saleId}"><i class="bi bi-x-lg me-1"></i>Cancel</button>
      </div>
    `;

    host.appendChild(card);
  });

  // Bind action buttons
  host.querySelectorAll('[data-ko-done-sale]').forEach(btn => {
    btn.addEventListener('click', () => kitchenSaleAction(btn.dataset.koDoneSale, 'completed'));
  });
  host.querySelectorAll('[data-ko-cancel-sale]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('Cancel the entire order for this customer?')) return;
      kitchenSaleAction(btn.dataset.koCancelSale, 'cancelled');
    });
  });
  host.querySelectorAll('[data-ko-move]').forEach(btn => {
    btn.addEventListener('click', () => kitchenSaleMove(btn.dataset.saleId, btn.dataset.koMove));
  });

  // Start live timers
  startKitchenTimers();
}

function renderKitchenHistory(orders) {
  const host = document.getElementById('kitchen-completed-list');
  if (!host) return;
  const done = orders.filter(o => o.status === 'completed' || o.status === 'cancelled');
  if (done.length === 0) {
    host.innerHTML = '<div class="text-muted small">No completed orders today.</div>';
    return;
  }
  host.innerHTML = '';
  done.forEach(o => {
    const wait = o.wait_seconds != null ? fmtWait(o.wait_seconds) : '-';
    const item = document.createElement('div');
    item.className = `d-flex justify-content-between align-items-center py-2 border-bottom small ${o.status === 'cancelled' ? 'text-muted' : ''}`;
    item.innerHTML = `
      <span>${o.status === 'cancelled' ? '<i class="bi bi-x-lg me-1"></i>' : '<i class="bi bi-check-lg me-1"></i>'}<strong>${o.product_name}</strong> ×${o.qty % 1 === 0 ? o.qty : o.qty.toFixed(1)}</span>
      <span>${new Date(o.queued_at).toLocaleTimeString('en-ZA', {hour:'2-digit',minute:'2-digit'})}</span>
      <span class="text-muted"><i class="bi bi-clock me-1"></i>${wait}</span>
    `;
    host.appendChild(item);
  });
}

async function kitchenSaleAction(saleId, status) {
  try {
    const j = await api(`/api/kitchen/orders/sale/${saleId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status })
    });
    if (status === 'completed' && j.wait_seconds != null) {
      toast(`Done! Order ready in ${fmtWait(j.wait_seconds)}`, 'success', 3000);
    } else if (status === 'cancelled') {
      toast('Order cancelled', 'warning', 2000);
    }
    await loadKitchenOrders();
  } catch (e) { toast(e.message, 'error'); }
}

let _kitchenLastMovedId = null;

async function kitchenSaleMove(saleId, direction) {
  try {
    await api(`/api/kitchen/orders/sale/${saleId}/move`, {
      method: 'POST',
      body: JSON.stringify({ direction })
    });
    _kitchenLastMovedId = saleId;
    await loadKitchenOrders();
    _kitchenLastMovedId = null;
  } catch (e) { toast(e.message, 'error'); }
}

function startKitchenTimers() {
  if (_kitchenTimerInterval) clearInterval(_kitchenTimerInterval);
  _kitchenTimerInterval = setInterval(() => {
    document.querySelectorAll('[data-timer-sale]').forEach(timerEl => {
      const card = timerEl.closest('[data-sale-id]');
      if (!card) return;
      const waitStart = parseInt(card.dataset.waitStart || '0');
      const loadedAt  = parseInt(card.dataset.loadedAt  || Date.now());
      const elapsed   = waitStart + Math.floor((Date.now() - loadedAt) / 1000);
      timerEl.textContent = fmtWait(elapsed);
      if (elapsed > 600) timerEl.classList.add('urgent');
    });
  }, 1000);
}

// Background badge poll - runs every 30s regardless of which tab is active
let _kitchenBadgePollTimer = null;
function startKitchenBadgePoll() {
  if (_kitchenBadgePollTimer) return;
  _kitchenBadgePollTimer = setInterval(async () => {
    try {
      const j = await api('/api/kitchen/orders/count');
      const badge = document.getElementById('kitchen-badge');
      if (!badge) return;
      badge.textContent = j.count;
      j.count > 0 ? show(badge) : hide(badge);
    } catch {}
  }, 30000);
}

document.getElementById('btn-refresh-kitchen')?.addEventListener('click', loadKitchenOrders);

let _showingKitchenHistory = false;
document.getElementById('btn-kitchen-history')?.addEventListener('click', async () => {
  _showingKitchenHistory = !_showingKitchenHistory;
  const btn  = document.getElementById('btn-kitchen-history');
  const hist = document.getElementById('kitchen-history-list');
  if (_showingKitchenHistory) {
    btn.textContent = 'Hide History';
    btn.classList.replace('btn-outline-secondary', 'btn-secondary');
    show(hist);
    await loadKitchenHistory();
  } else {
    btn.textContent = 'Completed Today';
    btn.classList.replace('btn-secondary', 'btn-outline-secondary');
    hide(hist);
  }
});

// ═══════════════════════════════════════════════════════
// TAB EVENTS
// ═══════════════════════════════════════════════════════
document.addEventListener('shown.bs.tab', async (evt) => {
  const target = evt.target?.getAttribute('data-bs-target');
  if (!target || !STATE.user) return;

  // Stop kitchen auto-refresh when leaving kitchen tab
  if (target !== '#kitchen' && _kitchenRefreshTimer) {
    clearInterval(_kitchenRefreshTimer);
    _kitchenRefreshTimer = null;
  }
  // Stop kitchen timers when not on kitchen tab
  if (target !== '#kitchen' && _kitchenTimerInterval) {
    clearInterval(_kitchenTimerInterval);
    _kitchenTimerInterval = null;
  }

  if (target === '#products') {
    if (STATE.products.length === 0) await loadProducts(); else renderProductsCards();
    loadIngredients();  // populates Stock Overview (expanded by default) and cost map
    setTimeout(() => {
      const wrap = document.getElementById('products-card-list');
      if (wrap?._pendingBarcodeItems) _renderBarcodes(wrap._pendingBarcodeItems);
    }, 200);
  } else if (target === '#users') {
    if (STATE.user.role !== 'admin') return;
    await loadUsers();
  } else if (target === '#transactions') {
    initTxDatePickers();
    if (isAdmin()) {
      await loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    } else {
      await loadTransactions();
    }
  } else if (target === '#kitchen') {
    await loadKitchenOrders();
    // Auto-refresh every 15s while kitchen tab is active
    if (_kitchenRefreshTimer) clearInterval(_kitchenRefreshTimer);
    _kitchenRefreshTimer = setInterval(loadKitchenOrders, 15000);
  } else if (target === '#suppliers') {
    await loadSuppliers();
    if (_kitchenRefreshTimer) { clearInterval(_kitchenRefreshTimer); _kitchenRefreshTimer = null; }
  } else if (target === '#stats') {
    _populateStatsProductFilter();
    // Only auto-load if no data has been loaded yet - preserve the user's selected range
    if (!_statsData) await loadStats();
  }
});

// ═══════════════════════════════════════════════════════
// APP INIT
// ═══════════════════════════════════════════════════════
(async function init() {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  updateVisibility();
  await refreshMe();
  if (STATE.user) {
    await loadProducts();
    await loadTransactions();
    if (isAdmin()) {
      await loadSettings();
      await loadStats();
      await loadUsers();
      await loadSpecials();
    }
  }
})();

// ═══════════════════════════════════════════════════════
// SPECIALS
// ═══════════════════════════════════════════════════════
STATE.specials = [];

async function loadSpecials() {
  try {
    STATE.specials = await api('/api/specials');
    if (isAdmin()) renderSpecialsList();
    // Refresh badges now that specials count is known
    const setBadge = (id, n) => { const el = document.getElementById(id); if (el) { el.textContent = n; el.style.display = n > 0 ? '' : 'none'; } };
    setBadge('specials-count-badge', STATE.specials.length);
  }
  catch (e) { console.error('loadSpecials', e); }
}

function renderSpecialsList() {
  const host = document.getElementById('specials-list');
  if (!host) return;
  host.innerHTML = '';
  if (!STATE.specials.length) {
    host.innerHTML = '<div class="text-muted small p-2">No specials defined yet. Click "+ New Special" to create one.</div>';
    return;
  }
  STATE.specials.forEach(s => {
    const card = document.createElement('div');
    card.className = 'product-thin-card mb-2';
    const lineNames = s.lines.map(l => `${l.qty}× ${l.product_name}`).join(', ');
    const scheduledNow = specialIsScheduledNow(s);
    let scheduleText = '';
    if ((s.schedule || []).length > 0) {
      scheduleText = s.schedule.map(row => {
        const dayStr = (row.days || []).map(d => DAY_NAMES[d]).join('/');
        const timeStr = row.all_day !== false ? 'all day' : `${row.start || '?'}–${row.end || '?'}`;
        return `${dayStr} ${timeStr}`;
      }).join(' · ');
    }
    const activeBadge = !s.active
      ? '<span class="badge bg-secondary ms-1">Inactive</span>'
      : (s.schedule?.length > 0 && !scheduledNow ? '<span class="badge bg-warning text-dark ms-1">Outside schedule</span>' : '');
    card.innerHTML = `
      <div class="product-thin-main">
        <div class="product-title">${s.name}${activeBadge}</div>
        <div class="product-sub">R${fmt(s.special_price)} - ${lineNames || 'No products set'}</div>
        ${scheduleText ? `<div class="small text-muted"><i class="bi bi-clock me-1"></i>${scheduleText}</div>` : ''}
      </div>
      <div class="product-actions">
        <button class="btn btn-outline-primary btn-sm">Edit</button>
      </div>`;
    card.querySelector('button').onclick = () => openSpecialEditor(s);
    host.appendChild(card);
  });
}

document.getElementById('btn-new-special')?.addEventListener('click', () => openSpecialEditor(null));

let _specialLines    = [];
let _scheduleRows    = [];   // [{days:[0..6], all_day:true, start:'', end:''}]

const DAY_NAMES = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];

function openSpecialEditor(s) {
  _specialLines = (s?.lines    || []).map(l => ({ ...l }));
  _scheduleRows = (s?.schedule || []).map(r => ({ ...r }));
  document.getElementById('special-id').value    = s?.id ?? '';
  document.getElementById('special-name').value  = s?.name ?? '';
  document.getElementById('special-price').value = s?.special_price ?? '';
  document.getElementById('special-active').checked = s?.active !== false;
  document.getElementById('special-editor-title').textContent = s ? `Edit - ${s.name}` : 'New Special';
  const delBtn = document.getElementById('btn-delete-special');
  s ? show(delBtn) : hide(delBtn);
  renderScheduleRows();
  renderSpecialLines();
  bootstrap.Modal.getOrCreateInstance(document.getElementById('specialEditorModal')).show();
}

function renderScheduleRows() {
  const host = document.getElementById('schedule-rows');
  if (!host) return;
  host.innerHTML = '';
  if (_scheduleRows.length === 0) {
    host.innerHTML = '<p class="small text-success mb-0">Always active - no time restrictions.</p>';
    return;
  }
  _scheduleRows.forEach((row, idx) => {
    const div = document.createElement('div');
    div.className = 'd-flex align-items-center gap-2 mb-2 flex-wrap';

    // Day toggles
    const daysDiv = document.createElement('div');
    daysDiv.className = 'd-flex gap-1';
    DAY_NAMES.forEach((name, d) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-sm ' + ((row.days || []).includes(d) ? 'btn-primary' : 'btn-outline-secondary');
      btn.textContent = name;
      btn.onclick = () => {
        const days = row.days || [];
        if (days.includes(d)) row.days = days.filter(x => x !== d);
        else row.days = [...days, d].sort();
        renderScheduleRows();
      };
      daysDiv.appendChild(btn);
    });
    div.appendChild(daysDiv);

    // All day checkbox
    const allDayLabel = document.createElement('label');
    allDayLabel.className = 'd-flex align-items-center gap-1 small text-nowrap ms-1';
    const allDayCb = document.createElement('input');
    allDayCb.type = 'checkbox';
    allDayCb.className = 'form-check-input mt-0';
    allDayCb.checked = row.all_day !== false;
    allDayCb.onchange = () => { row.all_day = allDayCb.checked; renderScheduleRows(); };
    allDayLabel.appendChild(allDayCb);
    allDayLabel.appendChild(document.createTextNode('All day'));
    div.appendChild(allDayLabel);

    // Time fields (hidden when all_day)
    if (!row.all_day) {
      const timeWrap = document.createElement('div');
      timeWrap.className = 'd-flex align-items-center gap-1 small';
      timeWrap.innerHTML = `
        <span>from</span>
        <input type="time" class="form-control form-control-sm" style="width:110px" value="${row.start || ''}">
        <span>to</span>
        <input type="time" class="form-control form-control-sm" style="width:110px" value="${row.end || ''}">`;
      const [, startEl, , endEl] = timeWrap.children;
      startEl.onchange = () => { row.start = startEl.value; };
      endEl.onchange   = () => { row.end   = endEl.value; };
      div.appendChild(timeWrap);
    }

    // Remove button
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'btn btn-outline-danger btn-sm ms-auto';
    removeBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
    removeBtn.onclick = () => { _scheduleRows.splice(idx, 1); renderScheduleRows(); };
    div.appendChild(removeBtn);

    host.appendChild(div);
  });
}

document.getElementById('btn-add-schedule-row')?.addEventListener('click', () => {
  _scheduleRows.push({ days: [0,1,2,3,4,5,6], all_day: true, start: '', end: '' });
  renderScheduleRows();
});

// Returns true if a special is currently within one of its scheduled windows.
// Empty schedule = always active.
function specialIsScheduledNow(special) {
  const schedule = special.schedule || [];
  if (schedule.length === 0) return true;
  const now  = new Date();
  const day  = (now.getDay() + 6) % 7;   // JS: 0=Sun → convert to 0=Mon
  const hhmm = now.getHours() * 60 + now.getMinutes();
  return schedule.some(row => {
    if (!(row.days || []).includes(day)) return false;
    if (row.all_day !== false) return true;
    const start = row.start ? parseInt(row.start.split(':')[0]) * 60 + parseInt(row.start.split(':')[1]) : 0;
    const end   = row.end   ? parseInt(row.end.split(':')[0])   * 60 + parseInt(row.end.split(':')[1])   : 1440;
    return hhmm >= start && hhmm < end;
  });
}

function renderSpecialLines() {
  const tbody = document.getElementById('special-lines-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  const forSaleProducts = STATE.products.filter(p => p.is_for_sale && !p.is_archived);
  _specialLines.forEach((line, idx) => {
    const tr = document.createElement('tr');
    let selHTML = `<select class="form-select form-select-sm" data-sl-idx="${idx}" data-sl-field="product_id">
      <option value="">- select -</option>`;
    forSaleProducts.forEach(p => {
      selHTML += `<option value="${p.id}" ${p.id === line.product_id ? 'selected' : ''}>${p.name}</option>`;
    });
    selHTML += '</select>';
    tr.innerHTML = `
      <td>${selHTML}</td>
      <td><input type="number" min="1" value="${line.qty || 1}" class="form-control form-control-sm" style="width:70px" data-sl-idx="${idx}" data-sl-field="qty"></td>
      <td><button class="btn btn-outline-danger btn-sm" data-sl-remove="${idx}"><i class="bi bi-x-lg"></i></button></td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('[data-sl-idx]').forEach(el => {
    el.addEventListener('change', () => {
      const idx   = parseInt(el.dataset.slIdx);
      const field = el.dataset.slField;
      if (field === 'product_id') _specialLines[idx].product_id = parseInt(el.value) || null;
      if (field === 'qty') _specialLines[idx].qty = parseInt(el.value) || 1;
    });
  });
  tbody.querySelectorAll('[data-sl-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      _specialLines.splice(parseInt(btn.dataset.slRemove), 1);
      renderSpecialLines();
    });
  });
}

document.getElementById('btn-add-special-line')?.addEventListener('click', () => {
  _specialLines.push({ product_id: null, qty: 1 });
  renderSpecialLines();
});

document.getElementById('btn-save-special')?.addEventListener('click', async () => {
  const id    = document.getElementById('special-id').value;
  const name  = document.getElementById('special-name').value.trim();
  const price = parseFloat(document.getElementById('special-price').value);
  const active = document.getElementById('special-active').checked;
  const lines = _specialLines.filter(l => l.product_id);
  if (!name)       return toast('Special name required', 'warning');
  if (isNaN(price)) return toast('Special price required', 'warning');
  if (!lines.length) return toast('Add at least one product', 'warning');
  const payload = { name, special_price: price, active, schedule: _scheduleRows, lines };
  try {
    if (id) {
      await api(`/api/specials/${id}`, { method: 'POST', body: JSON.stringify(payload) });
    } else {
      await api('/api/specials', { method: 'POST', body: JSON.stringify(payload) });
    }
    await loadSpecials();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('specialEditorModal')).hide();
    toast('Special saved', 'success');
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-delete-special')?.addEventListener('click', async () => {
  const id = document.getElementById('special-id').value;
  if (!id || !confirm('Delete this special?')) return;
  try {
    await api(`/api/specials/${id}`, { method: 'DELETE' });
    await loadSpecials();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('specialEditorModal')).hide();
    toast('Special deleted', 'warning');
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// COMBO / SPECIAL DETECTION - auto-applies, no prompt
// ═══════════════════════════════════════════════════════

function _basePrice(item) {
  const p = STATE.products.find(pr => pr.id === item.product_id);
  return parseFloat(p?.price || 0);
}

function _resetCartPrices() {
  Object.values(STATE.cart).forEach(item => {
    if (item.is_weight) return;
    const base = _basePrice(item);
    if (base > 0) item.unit_price = base * item.qty;
    item._special_applied = null;
    item._allocated_units = 0;
    item._discounted_subtotal = 0;
  });
}

function reapplySpecials() {
  _resetCartPrices();
  detectAndOfferSpecials();
}

// Exhaustive search for the combination of specials (and how many times each applies)
// that maximises total customer savings, subject to each product unit being used by
// at most one special.  For a typical farm-stall scenario (< 15 specials, small qty)
// this runs in well under a millisecond.
function _computeOptimalSpecials(cartQtyMap) {
  const active = (STATE.specials || []).filter(s => s.active && s.lines.length > 0 && specialIsScheduledNow(s));
  if (!active.length) return [];

  function productBase(pid) {
    return parseFloat(STATE.products.find(p => p.id === pid)?.price || 0);
  }

  function savingsPerApp(special) {
    return special.lines.reduce((s, r) => s + productBase(r.product_id) * r.qty, 0)
           - parseFloat(special.special_price);
  }

  function maxFit(special, remaining) {
    return Math.floor(Math.min(...special.lines.map(r => (remaining[r.product_id] || 0) / r.qty)));
  }

  function deduct(special, t, remaining) {
    const r = { ...remaining };
    special.lines.forEach(req => { r[req.product_id] = (r[req.product_id] || 0) - req.qty * t; });
    return r;
  }

  let bestSavings = 0;
  let bestAssignment = [];

  function search(idx, remaining, currentAssignment, currentSavings) {
    if (idx === active.length) {
      if (currentSavings > bestSavings) {
        bestSavings = currentSavings;
        bestAssignment = currentAssignment.slice();
      }
      return;
    }
    const special = active[idx];
    const max = maxFit(special, remaining);
    const sav = savingsPerApp(special);
    for (let t = max; t >= 0; t--) {
      search(
        idx + 1,
        t > 0 ? deduct(special, t, remaining) : remaining,
        [...currentAssignment, { special, times: t }],
        currentSavings + sav * t
      );
    }
  }

  search(0, { ...cartQtyMap }, [], 0);
  return bestAssignment.filter(a => a.times > 0);
}

function detectAndOfferSpecials() {
  if (!STATE.specials?.length) { renderCart(); return; }

  const cartQtyMap = {};
  Object.values(STATE.cart).forEach(c => {
    if (!c.is_weight) cartQtyMap[c.product_id] = (cartQtyMap[c.product_id] || 0) + c.qty;
  });

  // Find the globally optimal non-conflicting assignment of specials to product units
  const assignment = _computeOptimalSpecials(cartQtyMap);

  assignment.forEach(({ special, times }) => {
    // Compute savings directly from the special definition (not from post-application price diffs)
    const sav = special.lines.reduce((s, r) => s + parseFloat(STATE.products.find(p => p.id === r.product_id)?.price || 0) * r.qty, 0)
                - parseFloat(special.special_price);
    applySpecial(special, times);
    if (sav * times > 0.005) toast(`"${special.name}" ×${times} - saving R${fmt(sav * times)}`, 'success', 3000);
  });

  renderCart();
}

function applySpecial(special, times) {
  // Discount exactly (req.qty × times) units per required product.
  // Uses _allocated_units / _discounted_subtotal so that a product appearing in two
  // different specials (e.g. Coke in both "2×Coke" and "Coke+Cream Soda") is split
  // correctly - each unit is counted towards at most one special.
  const totalBaseForSpecial = special.lines.reduce((s, l) => {
    return s + parseFloat(STATE.products.find(p => p.id === l.product_id)?.price || 0) * l.qty;
  }, 0);

  special.lines.forEach(req => {
    let remaining = req.qty * times;
    const base = parseFloat(STATE.products.find(p => p.id === req.product_id)?.price || 0);
    const productShare     = totalBaseForSpecial > 0 ? (base * req.qty) / totalBaseForSpecial : 1 / special.lines.length;
    const discPricePerUnit = (special.special_price * productShare) / req.qty;

    Object.keys(STATE.cart).forEach(k => {
      if (remaining <= 0) return;
      const item = STATE.cart[k];
      if (item.product_id !== req.product_id || item.is_weight) return;

      // Only consume units not yet allocated to another special
      const available = item.qty - (item._allocated_units || 0);
      if (available <= 0) return;

      const discUnits = Math.min(available, remaining);
      item._allocated_units     = (item._allocated_units || 0) + discUnits;
      item._discounted_subtotal = (item._discounted_subtotal || 0) + discPricePerUnit * discUnits;
      item.unit_price           = parseFloat((item._discounted_subtotal + base * (item.qty - item._allocated_units)).toFixed(2));
      item._special_applied     = special.id;
      remaining                -= discUnits;
    });
  });
}

// ═══════════════════════════════════════════════════════
// INGREDIENT SUBSTITUTION MODAL
// ═══════════════════════════════════════════════════════
let _subsProduct    = null;  // product being customised
let _subsCartKey    = null;  // cart key being edited (null = new entry)
let _subsIngredients = [];   // [{ingredient_id, ingredient_name, qty_base, unit_type, base_unit, replaced_by_id, removed}]
let _subsExtras     = [];    // [{ingredient_id, qty_base, unit_type, base_unit}]
let _subsAlts       = [];    // all possible stock-item alternatives
let _subsHistory    = {};    // {ingredient_id: [ranked product_ids]}

async function openSubsModal(p, cartKey = null) {
  _subsProduct = p;
  _subsCartKey = cartKey;
  document.getElementById('subs-product-name').textContent = p.name;
  _subsExtras = [];

  try {
    const data = await api(`/api/products/${p.id}/substitutions`);
    _subsIngredients = data.default_ingredients.map(i => ({ ...i, replaced_by_id: null, removed: false }));
    _subsAlts        = data.alternatives;
    _subsHistory     = data.history_ranked;
  } catch (e) {
    toast(e.message, 'error'); return;
  }

  // If editing an existing cart entry, pre-fill its subs/extras
  if (cartKey && STATE.cart[cartKey]) {
    const existing = STATE.cart[cartKey];
    const existingSubs = existing.subs || {};
    _subsIngredients = _subsIngredients.map(ing => ({
      ...ing,
      replaced_by_id: existingSubs[ing.ingredient_id] === -1 ? null
                      : existingSubs[ing.ingredient_id] || null,
      removed: existingSubs[ing.ingredient_id] === -1,
    }));
    _subsExtras = (existing.extras || []).map(ex => ({ ...ex }));
  }

  renderSubsTable();
  updateSubsPriceDelta();
  bootstrap.Modal.getOrCreateInstance(document.getElementById('subsModal')).show();
}

function _buildUnitSel(unitType, baseUnit, currentUnit, dataAttr) {
  const opts = UNITS[unitType]?.display || [baseUnit || 'unit'];
  let html = `<select class="form-select form-select-sm" style="width:auto;min-width:60px" ${dataAttr}>`;
  opts.forEach(u => { html += `<option value="${u}" ${u === currentUnit ? 'selected' : ''}>${u}</option>`; });
  html += '</select>';
  return html;
}

function renderSubsTable() {
  const tbody = document.getElementById('subs-body');
  if (!tbody) return;
  tbody.innerHTML = '';

  // ── Default ingredient rows ──
  _subsIngredients.forEach((ing, idx) => {
    const isRemoved  = !!ing.removed;

    // Which product's units govern the qty field?
    // If swapped, use the replacement's unit type; otherwise use the default ingredient's.
    const activeProd = ing.replaced_by_id
      ? _subsAlts.find(a => a.id === ing.replaced_by_id)
      : { unit_type: ing.unit_type, base_unit: ing.base_unit };
    const unitType = activeProd?.unit_type || ing.unit_type || 'count';
    const baseUnit = activeProd?.base_unit || ing.base_unit || 'unit';

    // Initialise display fields on first render
    if (ing.qty_display === undefined) {
      ing.qty_display = ing.qty_base ? +ing.qty_base.toFixed(3) : '';
      ing.unit        = baseUnit;
    }

    const historyIds = _subsHistory[ing.ingredient_id] || [];
    const histAlts   = historyIds.map(id => _subsAlts.find(a => a.id === id)).filter(Boolean);
    const otherAlts  = _subsAlts.filter(a => a.id !== ing.ingredient_id && !historyIds.includes(a.id));

    let selHTML = `<select class="form-select form-select-sm" data-sub-idx="${idx}" data-sub-field="replace" ${isRemoved ? 'disabled' : ''}>`;
    selHTML += `<option value="">- keep default -</option>`;
    if (histAlts.length) {
      selHTML += `<optgroup label="Previously used">`;
      histAlts.forEach(a => { selHTML += `<option value="${a.id}" ${a.id === ing.replaced_by_id ? 'selected' : ''}>${a.name} (${a.unit_type || ''})</option>`; });
      selHTML += `</optgroup>`;
    }
    selHTML += `<optgroup label="All options">`;
    selHTML += `<option value="${ing.ingredient_id}" ${(!ing.replaced_by_id || ing.replaced_by_id === ing.ingredient_id) ? 'selected' : ''}>${ing.ingredient_name} (default)</option>`;
    otherAlts.forEach(a => { selHTML += `<option value="${a.id}" ${a.id === ing.replaced_by_id ? 'selected' : ''}>${a.name} (${a.unit_type || ''})</option>`; });
    selHTML += `</optgroup></select>`;

    const unitSelHTML = isRemoved ? '' : _buildUnitSel(unitType, baseUnit, ing.unit || baseUnit, `data-sub-idx="${idx}" data-sub-field="unit"`);
    const removeBtnLabel = isRemoved ? 'Restore' : 'Remove';
    const removeBtnClass = isRemoved ? 'btn btn-outline-success btn-sm' : 'btn btn-outline-danger btn-sm';

    const tr = document.createElement('tr');
    tr.style.opacity = isRemoved ? '0.45' : '';
    tr.innerHTML = `
      <td class="small fw-semibold">${ing.ingredient_name}${isRemoved ? ' <span class="badge bg-secondary ms-1">removed</span>' : ''}</td>
      <td>${selHTML}</td>
      <td>
        ${isRemoved ? '' : `<div class="d-flex gap-1 align-items-center">
          <input type="number" step="0.01" min="0.01" value="${ing.qty_display}" class="form-control form-control-sm" style="width:75px" data-sub-idx="${idx}" data-sub-field="qty_display">
          ${unitSelHTML}
        </div>`}
      </td>
      <td><button class="${removeBtnClass}" data-sub-remove="${idx}">${removeBtnLabel}</button></td>`;
    tbody.appendChild(tr);

    tr.querySelector(`[data-sub-field="replace"]`)?.addEventListener('change', e => {
      const repId = parseInt(e.target.value) || null;
      _subsIngredients[idx].replaced_by_id = repId;
      // Reset qty unit to match the new ingredient's unit type
      const rep = repId ? _subsAlts.find(a => a.id === repId) : null;
      const newBase = rep?.base_unit || ing.base_unit || 'unit';
      _subsIngredients[idx].unit = newBase;
      renderSubsTable();  // re-render so unit selector updates
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-sub-field="qty_display"]`)?.addEventListener('input', e => {
      _subsIngredients[idx].qty_display = parseFloat(e.target.value) || 0;
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-sub-field="unit"]`)?.addEventListener('change', e => {
      _subsIngredients[idx].unit = e.target.value;
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-sub-remove]`)?.addEventListener('click', () => {
      _subsIngredients[idx].removed = !_subsIngredients[idx].removed;
      if (_subsIngredients[idx].removed) _subsIngredients[idx].replaced_by_id = null;
      renderSubsTable();
      updateSubsPriceDelta();
    });
  });

  // ── Extra ingredient rows ──
  _subsExtras.forEach((ex, idx) => {
    const alt      = _subsAlts.find(a => a.id === ex.ingredient_id);
    const unitType = alt?.unit_type || ex.unit_type || 'count';
    const baseUnit = alt?.base_unit || ex.base_unit || 'unit';
    if (!ex.unit) ex.unit = baseUnit;

    let ingSelHTML = `<select class="form-select form-select-sm" data-extra-idx="${idx}" data-extra-field="ingredient_id">
      <option value="">- select ingredient -</option>`;
    _subsAlts.forEach(a => {
      ingSelHTML += `<option value="${a.id}" ${a.id === ex.ingredient_id ? 'selected' : ''}>${a.name} (${a.unit_type || ''})</option>`;
    });
    ingSelHTML += '</select>';

    const unitSelHTML = _buildUnitSel(unitType, baseUnit, ex.unit, `data-extra-idx="${idx}" data-extra-field="unit"`);

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="small text-success fw-semibold">+ Extra</td>
      <td>${ingSelHTML}</td>
      <td>
        <div class="d-flex gap-1 align-items-center">
          <input type="number" step="0.01" min="0.01" value="${ex.qty_display ?? ex.qty_base ?? ''}" placeholder="qty" class="form-control form-control-sm" style="width:75px" data-extra-idx="${idx}" data-extra-field="qty_display">
          ${unitSelHTML}
        </div>
      </td>
      <td><button class="btn btn-outline-danger btn-sm" data-extra-remove="${idx}"><i class="bi bi-x-lg"></i></button></td>`;
    tbody.appendChild(tr);

    tr.querySelector(`[data-extra-field="ingredient_id"]`).addEventListener('change', e => {
      const id  = parseInt(e.target.value) || null;
      _subsExtras[idx].ingredient_id = id;
      const newAlt = _subsAlts.find(a => a.id === id);
      _subsExtras[idx].unit_type = newAlt?.unit_type || 'count';
      _subsExtras[idx].base_unit = newAlt?.base_unit || 'unit';
      _subsExtras[idx].unit      = newAlt?.base_unit || 'unit';

      // Pre-fill qty: try history first, then recipe default, then base qty
      if (id) {
        const recalled = _recallExtraQty(id);
        if (recalled) {
          _subsExtras[idx].qty_display = recalled.qty;
          _subsExtras[idx].unit        = recalled.unit || newAlt?.base_unit || 'unit';
        } else {
          // Use the recipe's own qty for this ingredient if it appears as a default
          const recipeMatch = _subsIngredients.find(i => i.ingredient_id === id);
          if (recipeMatch) {
            _subsExtras[idx].qty_display = recipeMatch.qty_display ?? recipeMatch.qty_base ?? '';
            _subsExtras[idx].unit        = recipeMatch.unit || newAlt?.base_unit || 'unit';
          }
        }
      }

      renderSubsTable();
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-extra-field="qty_display"]`).addEventListener('input', e => {
      _subsExtras[idx].qty_display = parseFloat(e.target.value) || 0;
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-extra-field="unit"]`).addEventListener('change', e => {
      _subsExtras[idx].unit = e.target.value;
      updateSubsPriceDelta();
    });
    tr.querySelector(`[data-extra-remove]`).addEventListener('click', () => {
      _subsExtras.splice(idx, 1);
      renderSubsTable();
      updateSubsPriceDelta();
    });
  });
}

function _qtyBaseFromDisplay(qtyDisplay, unit, unitType) {
  return toBase(parseFloat(qtyDisplay) || 0, unit, unitType || 'count');
}

function updateSubsPriceDelta() {
  let delta = 0;
  _subsIngredients.forEach(ing => {
    if (ing.removed) return;
    if (ing.replaced_by_id && ing.replaced_by_id !== ing.ingredient_id) {
      // Default ingredient cost at the original qty_base
      const defaultCost = (STATE._stockCostMap?.[ing.ingredient_id] || 0) * (ing.qty_base || 0);
      // Swap ingredient cost - use edited qty if provided, else original qty_base
      const rep         = _subsAlts.find(a => a.id === ing.replaced_by_id);
      const swapQtyBase = (ing.qty_display !== undefined)
        ? _qtyBaseFromDisplay(ing.qty_display, ing.unit || rep?.base_unit, rep?.unit_type)
        : (ing.qty_base || 0);
      const swapCost    = (STATE._stockCostMap?.[ing.replaced_by_id] || 0) * swapQtyBase;
      delta += Math.max(0, swapCost - defaultCost);
    }
  });
  _subsExtras.forEach(ex => {
    if (ex.ingredient_id && (ex.qty_display > 0 || ex.qty_base > 0)) {
      const alt      = _subsAlts.find(a => a.id === ex.ingredient_id);
      const qtyBase  = _qtyBaseFromDisplay(ex.qty_display ?? ex.qty_base ?? 0, ex.unit || alt?.base_unit, alt?.unit_type);
      delta += (STATE._stockCostMap?.[ex.ingredient_id] || 0) * qtyBase;
    }
  });

  const el = document.getElementById('subs-price-delta');
  if (!el) return;
  if (delta === 0) {
    el.textContent = 'No change';
    el.className = 'text-muted';
    el._priceAdj = 0;
  } else {
    const markup   = parseFloat(document.getElementById('calc-markup')?.value || '50') / 100;
    const priceAdj = delta * (1 + (markup > 0 ? markup : 0.5));
    el.textContent = `+R${fmt(priceAdj)}`;
    el.className = 'text-danger fw-bold';
    el._priceAdj = priceAdj;
  }
}

// Remember last-used qty per ingredient for extras (keyed by ingredient_id)
function _recallExtraQty(ingredientId) {
  try {
    const store = JSON.parse(localStorage.getItem('extraQtyHistory') || '{}');
    return store[ingredientId] || null;
  } catch { return null; }
}
function _saveExtraQty(ingredientId, qtyDisplay, unit) {
  try {
    const store = JSON.parse(localStorage.getItem('extraQtyHistory') || '{}');
    store[ingredientId] = { qty: qtyDisplay, unit };
    localStorage.setItem('extraQtyHistory', JSON.stringify(store));
  } catch {}
}

document.getElementById('btn-add-extra')?.addEventListener('click', () => {
  _subsExtras.push({ ingredient_id: null, qty_display: '', qty_base: 0, unit_type: 'count', base_unit: 'unit', unit: 'unit' });
  renderSubsTable();
});

document.getElementById('btn-subs-confirm')?.addEventListener('click', () => {
  if (!_subsProduct) return;
  bootstrap.Modal.getOrCreateInstance(document.getElementById('subsModal')).hide();

  const p        = _subsProduct;
  const priceAdj = Math.max(0, parseFloat(document.getElementById('subs-price-delta')?._priceAdj || 0));
  const basePrice  = parseFloat(p.price || 0);
  const finalPrice = parseFloat((basePrice + priceAdj).toFixed(2));

  // Build subs map: {ingredient_id: replacement_id} or {ingredient_id: -1} for removed
  const subs = {};
  _subsIngredients.forEach(ing => {
    if (ing.removed) {
      subs[ing.ingredient_id] = -1;
    } else if (ing.replaced_by_id && ing.replaced_by_id !== ing.ingredient_id) {
      subs[ing.ingredient_id] = ing.replaced_by_id;
    }
  });

  // Convert display qty + unit → base qty for backend consumption, and save qty history
  const extras = _subsExtras
    .filter(ex => ex.ingredient_id && (ex.qty_display > 0 || ex.qty_base > 0))
    .map(ex => {
      const alt     = _subsAlts.find(a => a.id === ex.ingredient_id);
      const qtyBase = _qtyBaseFromDisplay(ex.qty_display ?? ex.qty_base ?? 0, ex.unit || alt?.base_unit, alt?.unit_type);
      if (ex.ingredient_id && ex.qty_display > 0) _saveExtraQty(ex.ingredient_id, ex.qty_display, ex.unit);
      return { ingredient_id: ex.ingredient_id, qty_base: qtyBase };
    })
    .filter(ex => ex.qty_base > 0);

  const hasCustomisation = Object.keys(subs).length > 0 || extras.length > 0;

  // Build display label
  const swapLabels   = _subsIngredients
    .filter(ing => !ing.removed && ing.replaced_by_id && ing.replaced_by_id !== ing.ingredient_id)
    .map(ing => { const rep = _subsAlts.find(a => a.id === ing.replaced_by_id); return rep ? `${ing.ingredient_name}→${rep.name}` : null; })
    .filter(Boolean);
  const removeLabels = _subsIngredients.filter(ing => ing.removed).map(ing => `no ${ing.ingredient_name}`);
  const extraLabels  = _subsExtras.filter(ex => ex.ingredient_id && (ex.qty_display > 0 || ex.qty_base > 0)).map(ex => {
    const alt = _subsAlts.find(a => a.id === ex.ingredient_id);
    const qtyStr = ex.qty_display ? `${ex.qty_display}${ex.unit || ''}` : '';
    return alt ? `+${alt.name}${qtyStr ? ' ' + qtyStr : ''}` : null;
  }).filter(Boolean);
  const allLabels    = [...swapLabels, ...removeLabels, ...extraLabels];

  if (_subsCartKey && STATE.cart[_subsCartKey]) {
    const entry = STATE.cart[_subsCartKey];
    if (!hasCustomisation) {
      // No actual change - just update name/price in place
      entry.name       = p.name;
      entry.unit_price = finalPrice * entry.qty;
      entry.subs       = undefined;
      entry.extras     = undefined;
    } else if (entry.qty > 1) {
      // Split: subtract 1 from original, create a new customised entry
      entry.qty       -= 1;
      entry.unit_price = parseFloat(p.price || 0) * entry.qty;
      const newKey = `${p.id}__${Date.now()}`;
      STATE.cart[newKey] = {
        _key: newKey, product_id: p.id,
        name: allLabels.length ? `${p.name} (${allLabels.join(', ')})` : p.name,
        unit_price: finalPrice, qty: 1, is_weight: false,
        subs, extras,
      };
    } else {
      // qty === 1 - update in place as before
      entry.name       = allLabels.length ? `${p.name} (${allLabels.join(', ')})` : p.name;
      entry.unit_price = finalPrice;
      entry.subs       = subs;
      entry.extras     = extras;
    }
  } else {
    // New entry - use unique key if customised so multiple versions can coexist
    const cartKey = hasCustomisation ? `${p.id}__${Date.now()}` : String(p.id);
    const existingPlain = !hasCustomisation && STATE.cart[cartKey];
    if (existingPlain) {
      existingPlain.qty += 1;
      existingPlain.unit_price = parseFloat(p.price || 0) * existingPlain.qty;
    } else {
      STATE.cart[cartKey] = {
        _key:       cartKey,
        product_id: p.id,
        name:       allLabels.length ? `${p.name} (${allLabels.join(', ')})` : p.name,
        unit_price: finalPrice,
        qty:        1,
        is_weight:  false,
        ...(hasCustomisation ? { subs, extras } : {}),
      };
    }
    STATE.scanHistory.push(p.id);
  }

  renderCart();
  detectAndOfferSpecials();
  toast(`Added: ${p.name}${allLabels.length ? ` (${allLabels.join(', ')})` : ''}`, 'success', 1500);
});

// ═══════════════════════════════════════════════════════
// CUSTOMER VISIT POLLING - greet returning customers
// ═══════════════════════════════════════════════════════
let _customerVisitPollTimer = null;
const _acknowledgedVisits = new Set();

function startCustomerVisitPoll() {
  if (_customerVisitPollTimer) return;
  _customerVisitPollTimer = setInterval(async () => {
    try {
      const visits = await api('/api/customers/pending_visits');
      for (const v of visits) {
        if (_acknowledgedVisits.has(v.id)) continue;
        _acknowledgedVisits.add(v.id);
        api(`/api/customers/visits/${v.id}/acknowledge`, { method: 'POST' }).catch(() => {});

        const visitNote = v.visit_count === 1 ? ' - first visit!' : '';
        const name = v.customer_name || 'customer';

        // Fetch purchase history hint for returning customers
        let hint = '';
        if (v.visit_count > 1 && v.customer_id) {
          try {
            const profile = await api(`/api/customers/${v.customer_id}/profile`);
            const top = (profile.top_products || []).slice(0, 2).map(p => p.name);
            if (top.length && profile.total_spent > 0) {
              hint = ` - usually buys ${top.join(' & ')}`;
            }
          } catch {}
        }
        toast(`Welcome back, ${name}${visitNote}${hint}`, 'info', 8000);
      }
    } catch {}
  }, 5000);
}

// ═══════════════════════════════════════════════════════
// CUSTOMERS TAB
// ═══════════════════════════════════════════════════════
let _customerSubTab = 'instore';

document.getElementById('customer-subtabs')?.addEventListener('click', e => {
  const btn = e.target.closest('[data-customer-tab]');
  if (!btn) return;
  _customerSubTab = btn.dataset.customerTab;
  document.querySelectorAll('#customer-subtabs .nav-link').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderCustomersList();
});

async function loadCustomers() {
  STATE.customers = await api('/api/customers');
  renderCustomersList();
  // Run merge suggestions on every refresh - auto-merges fire inline, panel updates live
  loadMergeSuggestions().catch(() => {});
}

function renderCustomersList() {
  const container = document.getElementById('customers-list');
  if (!container) return;

  // Preserve checked state across re-renders
  const checkedIds = new Set([...document.querySelectorAll('.merge-check:checked')].map(cb => cb.dataset.id));

  // Preserve any in-progress name inputs so a background refresh doesn't wipe them
  const draftNames = {};
  document.querySelectorAll('[id^="qn-input-"]').forEach(el => {
    if (el.value.trim()) draftNames[el.id] = el.value;
  });

  // Update sub-tab counts
  const onlineOnlyPool = STATE.customers.filter(c => c.is_online_customer && !c.is_pos_customer);
  const instorePool    = STATE.customers.filter(c => !(c.is_online_customer && !c.is_pos_customer));
  const onlineCount  = document.getElementById('cst-count-online');
  const instoreCount = document.getElementById('cst-count-instore');
  if (onlineCount)  onlineCount.textContent  = onlineOnlyPool.length;
  if (instoreCount) instoreCount.textContent = instorePool.length;

  // Apply sub-tab filter
  const tabPool = _customerSubTab === 'online' ? onlineOnlyPool : instorePool;

  if (!tabPool.length) {
    container.innerHTML = _customerSubTab === 'online'
      ? '<div class="text-muted">No online-only customers yet.</div>'
      : '<div class="text-muted">No customers yet.</div>';
    return;
  }

  // Apply search filter
  const q = (document.getElementById('customer-search')?.value || '').trim().toLowerCase();
  const filtered = q
    ? tabPool.filter(c =>
        (c.name || '').toLowerCase().includes(q) ||
        (c.customer_number || '').toLowerCase().includes(q) ||
        (c.phone || '').includes(q) ||
        (c.email || '').toLowerCase().includes(q))
    : tabPool;

  if (!filtered.length) {
    container.innerHTML = q
      ? `<div class="text-muted">No customers match "${q}".</div>`
      : '<div class="text-muted">No customers in this view.</div>';
    return;
  }

  // Apply sort
  const sort = document.getElementById('customer-sort')?.value || 'last_visit';
  let sortedPool = sort === 'unnamed' ? filtered.filter(c => !c.name && c.auto_enrolled) : filtered;

  const sorted = [...sortedPool].sort((a, b) => {
    if (sort === 'last_visit')  return (b.last_visit || '').localeCompare(a.last_visit || '');
    if (sort === 'visit_count') return (b.visit_count || 0) - (a.visit_count || 0);
    if (sort === 'name')        return (a.name || 'zzz').localeCompare(b.name || 'zzz');
    if (sort === 'no_purchase') return (a.visit_count || 0) - (b.visit_count || 0);
    if (sort === 'unnamed')     return (b.visit_count || 0) - (a.visit_count || 0);
    return 0;
  });
  STATE._sortedCustomers = sorted;

  // Merge toolbar - shown when ≥2 checked
  const toolbarHtml = `
    <div id="merge-toolbar" class="d-none mb-2 p-2 bg-warning-subtle border rounded d-flex align-items-center gap-2">
      <span id="merge-selected-count" class="small fw-semibold"></span>
      <span class="text-muted small flex-grow-1">Select the primary customer first (keep their details), then check the duplicates.</span>
      <button class="btn btn-warning btn-sm" onclick="openMergeModal()">Merge Selected</button>
      <button class="btn btn-outline-secondary btn-sm" onclick="clearMergeSelection()">Cancel</button>
    </div>`;

  const _attrChips = attrs => {
    if (!attrs) return '';
    const chips = [];
    const gMap = { male: '<i class="bi bi-gender-male"></i>', female: '<i class="bi bi-gender-female"></i>', 'm': '<i class="bi bi-gender-male"></i>', 'f': '<i class="bi bi-gender-female"></i>' };
    if (attrs.gender)     chips.push(`<span class="badge bg-light text-dark border" title="Gender">${gMap[attrs.gender.toLowerCase()] || attrs.gender}</span>`);
    if (attrs.age_range)  chips.push(`<span class="badge bg-light text-dark border" title="Age">${attrs.age_range}</span>`);
    if (attrs.build)      chips.push(`<span class="badge bg-light text-dark border" title="Build">${attrs.build}</span>`);
    if (attrs.hair_color) chips.push(`<span class="badge bg-light text-dark border" title="Hair">${attrs.hair_color} hair</span>`);
    return chips.join('');
  };

  const cardsHtml = sorted.map(c => `
    <div class="card mb-2 ${c.active ? '' : 'opacity-50'}" data-customer-id="${c.id}">
      <div class="card-body py-2 d-flex align-items-center gap-2">
        <input type="checkbox" class="form-check-input merge-check flex-shrink-0" style="width:1.1rem;height:1.1rem"
          data-id="${c.id}" onchange="updateMergeToolbar()">
        <div class="flex-shrink-0 d-flex gap-1">
          ${(c.has_face || c.has_photo)
            ? `<img src="/api/customers/${c.id}/photo" alt="face"
                style="width:52px;height:52px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6;"
                onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
            : ''}
          <div style="width:52px;height:52px;border-radius:50%;background:#e9ecef;display:${(c.has_face || c.has_photo) ? 'none' : 'flex'};align-items:center;justify-content:center;font-size:1.4rem;flex-shrink:0;"><i class="bi bi-person"></i></div>
          ${c.has_body_photo
            ? `<img src="/api/customers/${c.id}/body_photo" alt="body"
                style="width:44px;height:66px;object-fit:cover;border-radius:4px;border:2px solid #dee2e6;"
                onerror="this.style.display='none'">`
            : ''}
        </div>
        <div class="flex-grow-1 min-width-0">
          <div class="fw-semibold">${c.name || '<span class="text-muted fst-italic">Unnamed</span>'}
            ${c.is_employee ? '<span class="badge bg-warning text-dark ms-1" style="font-size:0.65rem"><i class="bi bi-person-badge me-1"></i>Employee</span>' : ''}
            ${c.auto_enrolled && !c.is_employee ? '<span class="badge bg-info text-dark ms-1" style="font-size:0.65rem">Auto</span>' : ''}
            ${(c.is_online_customer && c.is_pos_customer) ? '<span class="badge bg-purple text-white ms-1" style="font-size:0.65rem;background:#7c3aed"><i class="bi bi-globe me-1"></i><i class="bi bi-shop me-1"></i>Both</span>' : (c.is_online_customer ? '<span class="badge ms-1" style="font-size:0.65rem;background:#0ea5e9;color:#fff"><i class="bi bi-globe me-1"></i>Online</span>' : (c.is_pos_customer ? '<span class="badge bg-success ms-1" style="font-size:0.65rem"><i class="bi bi-shop me-1"></i>In-store</span>' : ''))}
            ${c.customer_number ? `<span class="text-muted small ms-1">${c.customer_number}</span>` : ''}
          </div>
          ${c.phone ? `<div class="small text-muted">${c.phone}</div>` : ''}
          <div class="small mt-1 d-flex flex-wrap gap-1">
            ${c.plates.length ? c.plates.map(p => `<span class="badge bg-light text-dark border">${p}</span>`).join('') : ''}
            ${c.has_face ? '<span class="badge bg-success">Face <i class="bi bi-check-lg"></i></span>' : '<span class="badge bg-secondary">Face -</span>'}
            ${c.has_gait ? '<span class="badge bg-success">Body <i class="bi bi-check-lg"></i></span>' : '<span class="badge bg-secondary">Body -</span>'}
            ${_attrChips(c.physical_attributes)}
          </div>
          <div class="small mt-1 d-flex flex-wrap gap-2 text-muted">
            <span title="Visits">${c.visit_count} visit${c.visit_count !== 1 ? 's' : ''}${c.last_visit ? ` · last ${new Date(c.last_visit).toLocaleDateString()}` : ''}</span>
            ${c.receipt_count > 0 ? `<span class="text-success fw-semibold" title="Total spent">R${c.total_spent != null ? c.total_spent.toFixed(2) : '0.00'}</span>` : '<span title="No purchases yet">Never purchased</span>'}
            ${c.receipt_count > 0 ? `<span title="Receipts">${c.receipt_count} receipt${c.receipt_count !== 1 ? 's' : ''}</span>` : ''}
            ${c.receipt_count > 0 ? `<span title="Avg basket">avg R${c.avg_basket != null ? c.avg_basket.toFixed(2) : '0.00'}</span>` : ''}
          </div>
          ${!c.name ? `
          <div class="d-flex gap-1 mt-2 quick-name-row" id="qn-row-${c.id}">
            <input class="form-control form-control-sm" style="max-width:200px" placeholder="Name this person…"
              id="qn-input-${c.id}" onkeydown="if(event.key==='Enter')quickNameCustomer(${c.id});if(event.key==='Escape')document.getElementById('qn-row-${c.id}').style.display='none'">
            <button class="btn btn-success btn-sm" onclick="quickNameCustomer(${c.id})">Save</button>
          </div>` : ''}
        </div>
        <div class="d-flex flex-column gap-1 flex-shrink-0">
          <button class="btn btn-outline-primary btn-sm" onclick="openCustomerDetail(${c.id})">Details</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="openCustomerEnroll(${c.id})">Edit</button>
        </div>
      </div>
    </div>
  `).join('');

  container.innerHTML = toolbarHtml + cardsHtml;

  // Restore previously checked customers
  if (checkedIds.size) {
    document.querySelectorAll('.merge-check').forEach(cb => {
      if (checkedIds.has(cb.dataset.id)) cb.checked = true;
    });
    updateMergeToolbar();
  }

  // Restore any in-progress name inputs that were wiped by the re-render
  Object.entries(draftNames).forEach(([id, val]) => {
    const el = document.getElementById(id);
    if (el) { el.value = val; el.focus(); }
  });
}

function updateMergeToolbar() {
  const checked = [...document.querySelectorAll('.merge-check:checked')];
  const toolbar = document.getElementById('merge-toolbar');
  const countEl = document.getElementById('merge-selected-count');
  if (checked.length >= 2) {
    toolbar.classList.remove('d-none');
    toolbar.classList.add('d-flex');
    countEl.textContent = `${checked.length} selected`;
  } else {
    toolbar.classList.add('d-none');
    toolbar.classList.remove('d-flex');
  }
}

function clearMergeSelection() {
  document.querySelectorAll('.merge-check').forEach(cb => cb.checked = false);
  updateMergeToolbar();
}

async function openMergeModal() {
  const checked = [...document.querySelectorAll('.merge-check:checked')];
  if (checked.length < 2) return;
  const ids = checked.map(cb => parseInt(cb.dataset.id));
  const customers = ids.map(id => STATE.customers.find(c => c.id === id));

  const body = document.getElementById('customerDetailBody');
  const title = document.getElementById('customerDetailTitle');
  title.textContent = 'Merge Customers';
  body.innerHTML = '<div class="text-center text-muted py-4">Selecting primary...</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('customerDetailModal')).show();

  // Auto-select primary from server
  let suggestResult = null;
  try {
    suggestResult = await api('/api/customers/merge_suggest_primary', {
      method: 'POST', body: JSON.stringify({ ids })
    });
  } catch(e) {
    // Fall back to first selected - don't block the modal on server error
    console.warn('merge_suggest_primary failed:', e.message);
  }
  let selectedPrimaryId = suggestResult?.primary_id ?? ids[0];
  const primaryReason = suggestResult?.reason ?? 'auto-selected';

  // Load radars for comparison
  const [radars] = await Promise.all([
    Promise.all(customers.map(c => api(`/api/customers/${c.id}/radar`).catch(() => null)))
  ]);

  const radarComparison = radars.some(Boolean) ? `
    <div class="d-flex gap-3 justify-content-center mb-3 flex-wrap">
      ${customers.map((c, i) => radars[i] ? `
        <div class="text-center border rounded p-2 ${c.id === selectedPrimaryId ? 'border-warning' : ''}">
          <img src="/api/customers/${c.id}/photo" style="width:52px;height:52px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6;margin-bottom:4px" onerror="this.style.display='none'">
          <div class="small fw-semibold">${c.name || c.customer_number}</div>
          <div class="d-flex gap-1 mt-1">
            <div><div class="text-muted" style="font-size:.6rem">Biometric</div><canvas id="merge-bio-${c.id}" width="160" height="160"></canvas></div>
            <div><div class="text-muted" style="font-size:.6rem">Behavioural</div><canvas id="merge-beh-${c.id}" width="160" height="160"></canvas></div>
          </div>
          <div class="text-muted mt-1" style="font-size:.65rem">${c.visit_count} visits · ${radars[i].details.face_angles} angles · ${radars[i].details.purchase_count} purchases</div>
        </div>` : '').join('')}
    </div>` : '';

  // Override selector (collapsed by default)
  const overrideOpts = customers.map(c => `
    <option value="${c.id}" ${c.id === selectedPrimaryId ? 'selected' : ''}>
      ${c.customer_number} - ${c.name || 'Unnamed'} (${c.visit_count} visits${c.has_face ? ', face <i class="bi bi-check-lg"></i>' : ''})
    </option>`).join('');

  body.innerHTML = `
    <div class="alert alert-warning py-2 mb-3 d-flex align-items-center gap-2">
      <img src="/api/customers/${selectedPrimaryId}/photo"
        style="width:36px;height:36px;object-fit:cover;border-radius:50%"
        onerror="this.style.display='none'">
      <div>
        <div class="fw-semibold small">Primary: ${customers.find(c=>c.id===selectedPrimaryId)?.name || customers.find(c=>c.id===selectedPrimaryId)?.customer_number}</div>
        <div class="text-muted" style="font-size:.7rem">Auto-selected - ${primaryReason}. Their name, number and details are kept.</div>
      </div>
    </div>
    ${radarComparison}
    <details class="mb-3">
      <summary class="text-muted small" style="cursor:pointer">Override primary selection</summary>
      <select class="form-select form-select-sm mt-2" id="merge-primary-override">${overrideOpts}</select>
    </details>
    <p class="text-muted small mb-3">All biometrics, visits and purchases from the other customer(s) will be merged in. This can be undone from the customer's profile.</p>
    <div class="d-flex justify-content-end gap-2">
      <button class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
      <button class="btn btn-warning" id="btn-confirm-merge">Merge</button>
    </div>`;

  const bioColors = ['#2a6f3e', '#198754', '#fd7e14', '#dc3545'];
  const behColors = ['#0d6efd', '#6610f2', '#fd7e14', '#dc3545'];
  customers.forEach((c, i) => {
    if (radars[i]) {
      if (radars[i].biometric)   drawRadarChart(`merge-bio-${c.id}`, radars[i].biometric,   bioColors[i % bioColors.length]);
      if (radars[i].behavioural) drawRadarChart(`merge-beh-${c.id}`, radars[i].behavioural, behColors[i % behColors.length]);
    }
  });

  document.getElementById('btn-confirm-merge').onclick = async () => {
    const primaryId = parseInt(document.getElementById('merge-primary-override')?.value ?? selectedPrimaryId);
    const mergeIds  = ids.filter(id => id !== primaryId);
    try {
      await api('/api/customers/merge', {
        method: 'POST',
        body: JSON.stringify({ primary_id: primaryId, merge_ids: mergeIds, auto_merged: false })
      });
      bootstrap.Modal.getOrCreateInstance(document.getElementById('customerDetailModal')).hide();
      clearMergeSelection();
      toast('Customers merged - undo available from their profile', 'success', 6000);
      await loadCustomers();
    } catch(e) {
      toast(e.message, 'danger');
    }
  };
}

function drawRadarChart(canvasId, scores, color) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W / 2, cy = H / 2;
  // Leave generous margin for labels - 52px on all sides
  const R = Math.min(cx, cy) - 52;
  const labels = Object.keys(scores);
  const values = Object.values(scores);
  const N = labels.length;
  const angleStep = (2 * Math.PI) / N;
  const startAngle = -Math.PI / 2;

  ctx.clearRect(0, 0, W, H);

  // Grid rings
  for (let r = 1; r <= 5; r++) {
    ctx.beginPath();
    for (let i = 0; i < N; i++) {
      const a = startAngle + i * angleStep;
      const x = cx + Math.cos(a) * (R * r / 5);
      const y = cy + Math.sin(a) * (R * r / 5);
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.closePath();
    ctx.strokeStyle = r === 5 ? '#adb5bd' : '#dee2e6';
    ctx.lineWidth = r === 5 ? 1.5 : 0.8;
    ctx.stroke();
  }

  // Spokes
  for (let i = 0; i < N; i++) {
    const a = startAngle + i * angleStep;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
    ctx.strokeStyle = '#dee2e6';
    ctx.lineWidth = 0.8;
    ctx.stroke();
  }

  // Data polygon
  ctx.beginPath();
  for (let i = 0; i < N; i++) {
    const a = startAngle + i * angleStep;
    const v = Math.max(0, Math.min(1, values[i]));
    const x = cx + Math.cos(a) * R * v;
    const y = cy + Math.sin(a) * R * v;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fillStyle = color + '33';
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Data points
  for (let i = 0; i < N; i++) {
    const a = startAngle + i * angleStep;
    const v = Math.max(0, Math.min(1, values[i]));
    const x = cx + Math.cos(a) * R * v;
    const y = cy + Math.sin(a) * R * v;
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, 2 * Math.PI);
    ctx.fillStyle = color;
    ctx.fill();
  }

  // Labels - positioned well outside the radar ring with word-wrap for long names
  const lineH = 13;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  for (let i = 0; i < N; i++) {
    const a = startAngle + i * angleStep;
    // Push labels further out - 26px clearance from ring edge
    const labelR = R + 26;
    const lx = cx + Math.cos(a) * labelR;
    const ly = cy + Math.sin(a) * labelR;
    const pct = Math.round(values[i] * 100);
    const label = labels[i];

    // Split label into two lines if longer than 8 chars
    const words = label.split(' ');
    let line1 = label, line2 = '';
    if (label.length > 8 && words.length > 1) {
      const mid = Math.ceil(words.length / 2);
      line1 = words.slice(0, mid).join(' ');
      line2 = words.slice(mid).join(' ');
    }

    ctx.font = '10px system-ui, sans-serif';
    ctx.fillStyle = '#495057';
    if (line2) {
      ctx.fillText(line1, lx, ly - lineH);
      ctx.fillText(line2, lx, ly);
      ctx.font = 'bold 10px system-ui, sans-serif';
      ctx.fillStyle = pct >= 80 ? '#198754' : pct >= 40 ? '#fd7e14' : '#dc3545';
      ctx.fillText(pct + '%', lx, ly + lineH);
    } else {
      ctx.fillText(line1, lx, ly - 6);
      ctx.font = 'bold 10px system-ui, sans-serif';
      ctx.fillStyle = pct >= 80 ? '#198754' : pct >= 40 ? '#fd7e14' : '#dc3545';
      ctx.fillText(pct + '%', lx, ly + 7);
    }
  }
}

async function openCustomerDetail(customerId) {
  const c = STATE.customers.find(x => x.id === customerId);
  if (!c) return;

  document.getElementById('customerDetailTitle').textContent =
    (c.name || 'Unnamed') + (c.customer_number ? ` - ${c.customer_number}` : '');
  document.getElementById('customerDetailBody').innerHTML =
    '<div class="text-center text-muted py-4">Loading...</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('customerDetailModal')).show();

  const [attrs, visits, profile, radar, mergeHistory] = await Promise.all([
    api(`/api/customers/${c.id}/attributes`).catch(() => null),
    api(`/api/customers/${c.id}/visits`).catch(() => []),
    api(`/api/customers/${c.id}/profile`).catch(() => null),
    api(`/api/customers/${c.id}/radar`).catch(() => null),
    api(`/api/customers/${c.id}/merge_history`).catch(() => null),
  ]);

  // ── Photo + identity signals ──────────────────────────────────
  const t = Date.now();
  const facePhotoHtml = (c.has_face || c.has_photo)
    ? `<img src="/api/customers/${c.id}/photo?t=${t}" alt="face"
         style="width:90px;height:90px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6;"
         onerror="this.style.display='none'">`
    : `<div style="width:90px;height:90px;border-radius:50%;background:#e9ecef;display:flex;align-items:center;justify-content:center;font-size:2rem;"><i class="bi bi-person"></i></div>`;
  const bodyPhotoHtml = c.has_body_photo
    ? `<div class="ms-2">
         <div class="text-muted" style="font-size:.65rem;margin-bottom:2px">Body</div>
         <img src="/api/customers/${c.id}/body_photo?t=${t}" alt="body"
           style="height:140px;max-width:110px;object-fit:cover;border-radius:4px;border:2px solid #dee2e6;"
           onerror="this.style.display='none'">
       </div>`
    : '';
  const photoHtml = `<div class="d-flex align-items-start">${facePhotoHtml}${bodyPhotoHtml}</div>`;

  const originBadge = (c.is_online_customer && c.is_pos_customer)
    ? '<span class="badge" style="background:#7c3aed;color:#fff"><i class="bi bi-globe me-1"></i><i class="bi bi-shop me-1"></i>Online & In-store</span>'
    : c.is_online_customer
      ? '<span class="badge" style="background:#0ea5e9;color:#fff"><i class="bi bi-globe me-1"></i>Online customer</span>'
      : c.is_pos_customer
        ? '<span class="badge bg-success"><i class="bi bi-shop me-1"></i>In-store customer</span>'
        : '';
  const signalBadges = [
    c.has_face ? '<span class="badge bg-success">Face <i class="bi bi-check-lg"></i></span>' : '<span class="badge bg-secondary">Face -</span>',
    c.has_gait ? '<span class="badge bg-success">Body <i class="bi bi-check-lg"></i></span>' : '<span class="badge bg-secondary">Body -</span>',
    ...(c.plates || []).map(p => `<span class="badge bg-light text-dark border">${p}</span>`),
    c.auto_enrolled ? '<span class="badge bg-info text-dark">Auto-enrolled</span>' : '',
    originBadge,
  ].filter(Boolean).join(' ');

  // ── 12 Physical attributes ────────────────────────────────────
  const ATTR_LABELS = [
    ['height_category', 'Height',       v => v],
    ['height_cm',       'Height (cm)',  v => v + ' cm'],
    ['build',           'Build',        v => v],
    ['age_range',       'Age',          v => v],
    ['gender',          'Gender',       v => v],
    ['hair_color',      'Hair',         v => v],
    ['skin_tone',       'Skin',         v => v],
    ['eye_color',       'Eyes',         v => v],
    ['facial_hair',     'Facial hair',  v => v],
    ['wearing_glasses', 'Glasses',      v => v ? 'yes' : 'no'],
    ['camera_source',   'Camera',       v => v],
    ['confidence',      'Conf.',        v => Math.round(v * 100) + '%'],
  ];

  let attrsHtml = '';
  if (attrs) {
    const rows = ATTR_LABELS.map(([key, label, fmt]) => {
      const val = attrs[key];
      if (val === null || val === undefined || val === '') return '';
      if (key === 'facial_hair' && val === 'none') return '';
      if (key === 'wearing_glasses' && val === null) return '';
      return `<tr><td class="text-muted small pe-3" style="white-space:nowrap">${label}</td>
                  <td class="small fw-semibold">${fmt(val)}</td></tr>`;
    }).filter(Boolean);

    attrsHtml = rows.length
      ? `<div class="mb-3">
           <div class="fw-semibold small text-uppercase text-muted mb-1" style="letter-spacing:.05em">Physical Attributes</div>
           <table class="table table-sm table-borderless mb-0" style="width:auto"><tbody>${rows.join('')}</tbody></table>
         </div>`
      : `<div class="text-muted small mb-3">No physical attributes captured yet.</div>`;
  }

  // ── Visit history ─────────────────────────────────────────────
  const SIGNAL_LABELS = {
    face: 'Face', gait: 'Body', plate: 'Plate',
    height_cat: 'Height', auto_enrollment: 'Auto-enroll',
    track_confidence: 'Track match',
    face_similarity: 'Face sim', gait_distance: 'Gait dist',
    session_face_sim: 'Face sim', session_cameras: 'Cameras', session_faces: 'Faces',
  };
  // These are raw counts/values - do not multiply by 100 or append %
  const SIGNAL_RAW_COUNT = new Set(['session_cameras', 'session_faces']);

  let visitsHtml = '';
  if (visits.length) {
    const rows = visits.map(v => {
      // detected_at is stored as UTC without 'Z' - append it so the browser parses as UTC
      // then toLocaleTimeString converts to the user's local timezone (SAST = UTC+2)
      const dtRaw = v.detected_at || '';
      const dt = new Date(dtRaw.endsWith('Z') || dtRaw.includes('+') ? dtRaw : dtRaw + 'Z');
      const dateStr = dt.toLocaleDateString('en-ZA', {day:'2-digit', month:'short', year:'numeric'});
      const timeStr = dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
      const camera = v.camera_source
        ? `<span class="badge bg-light text-dark border" style="font-size:.65rem">${v.camera_source}</span>` : '';

      const scores = v.confidence_scores || {};
      const signalBadges = Object.entries(scores).map(([k, score]) => {
        if (k === 'face_similarity' || k === 'gait_distance' || k === 'session_face_sim') return ''; // shown as sub-detail
        const label = SIGNAL_LABELS[k] || k;
        if (SIGNAL_RAW_COUNT.has(k)) {
          // Raw count - show as plain number, no %
          return `<span class="badge bg-secondary" style="font-size:.7rem">${label}: ${score}</span>`;
        }
        const pct = typeof score === 'number' ? Math.round(score * 100) : null;
        const colour = pct === null ? 'bg-secondary'
          : pct >= 80 ? 'bg-success' : pct >= 50 ? 'bg-warning text-dark' : 'bg-danger';
        return `<span class="badge ${colour}" style="font-size:.7rem">${label}${pct !== null ? ': ' + pct + '%' : ''}</span>`;
      }).filter(Boolean).join(' ');

      // Sub-detail: similarity values - convert gait distance to % match (0=perfect, 0.25=threshold)
      const gaitPct = scores.gait_distance != null
        ? Math.max(0, Math.round((1 - scores.gait_distance / 0.25) * 100))
        : null;
      const faceSim = scores.face_similarity ?? scores.session_face_sim ?? null;
      const details = [
        faceSim != null ? `face sim: ${(faceSim * 100).toFixed(1)}%` : '',
        gaitPct !== null ? `gait match: ${gaitPct}%` : '',
        v.matched_signals && v.matched_signals !== 'track_consensus' ? `method: ${v.matched_signals}` : '',
      ].filter(Boolean).join(' · ');

      return `<tr>
        <td style="white-space:nowrap;vertical-align:top" class="pe-3">
          <div class="small fw-semibold">${timeStr}</div>
          <div class="text-muted" style="font-size:.7rem">${dateStr}</div>
          <div class="mt-1">${camera}</div>
        </td>
        <td style="vertical-align:top">
          <div class="d-flex flex-wrap gap-1 mb-1">${signalBadges || '<span class="text-muted small">-</span>'}</div>
          ${details ? `<div class="text-muted" style="font-size:.7rem">${details}</div>` : ''}
        </td>
      </tr>`;
    }).join('');

    visitsHtml = `<div>
      <div class="fw-semibold small text-uppercase text-muted mb-1" style="letter-spacing:.05em">Visit History (last ${visits.length})</div>
      <div style="max-height:260px;overflow-y:auto">
        <table class="table table-sm table-borderless mb-0">
          <thead><tr>
            <th class="small text-muted fw-normal pe-3">When</th>
            <th class="small text-muted fw-normal">Signals captured</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  } else {
    visitsHtml = '<div class="text-muted small">No visits recorded yet.</div>';
  }

  // ── Business intelligence ─────────────────────────────────────
  let bizHtml = '';
  if (profile) {
    const p = profile;
    const fmtR = n => `R${fmt(n || 0)}`;
    const fmtDwell = s => s > 0 ? (s >= 60 ? `${Math.floor(s/60)}m ${Math.round(s%60)}s` : `${Math.round(s)}s`) : '-';

    // Key stats row
    const firstSeen = p.first_seen ? new Date(p.first_seen).toLocaleDateString('en-ZA') : '-';
    const purchaseVisits = (p.recent_sessions || []).filter(s => s.purchase_made).length;
    const totalVisits = (p.recent_sessions || []).length;
    const buyRate = totalVisits > 0 ? Math.round(purchaseVisits / totalVisits * 100) : null;

    const onlinePct  = p.online_spend_pct !== null && p.online_spend_pct !== undefined ? p.online_spend_pct + '%' : null;
    const lastPurchaseStr = p.days_since_purchase !== null && p.days_since_purchase !== undefined
      ? (p.days_since_purchase === 0 ? 'Today' : p.days_since_purchase + 'd ago') : '-';
    const longestGapStr = p.longest_gap_days !== null && p.longest_gap_days !== undefined ? p.longest_gap_days + 'd' : '-';

    bizHtml = `
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Total Spent</div>
        <div class="fw-bold text-success">${fmtR(p.total_spent)}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Avg Basket</div>
        <div class="fw-bold">${fmtR(p.avg_basket)}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Buy Rate</div>
        <div class="fw-bold ${buyRate >= 50 ? 'text-success' : 'text-warning'}">${buyRate !== null ? buyRate + '%' : '-'}</div>
      </div></div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Visits</div>
        <div class="fw-bold">${p.visit_count || 0}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Avg Dwell</div>
        <div class="fw-bold">${fmtDwell(p.avg_dwell_seconds)}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">First Seen</div>
        <div class="fw-bold" style="font-size:12px">${firstSeen}</div>
      </div></div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Online Receipts</div>
        <div class="fw-bold">${p.online_count || 0}</div>
        ${p.online_spend ? `<div class="text-muted" style="font-size:10px">${fmtR(p.online_spend)}</div>` : ''}
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">In-Store</div>
        <div class="fw-bold">${p.instore_count || 0}</div>
        ${p.instore_spend ? `<div class="text-muted" style="font-size:10px">${fmtR(p.instore_spend)}</div>` : ''}
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Online Split</div>
        <div class="fw-bold">${onlinePct || '-'}</div>
      </div></div>
    </div>
    <div class="row g-2 mb-3">
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Last Purchase</div>
        <div class="fw-bold" style="font-size:12px">${lastPurchaseStr}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Fav. Day</div>
        <div class="fw-bold" style="font-size:12px">${p.fav_day || '-'}</div>
      </div></div>
      <div class="col-4"><div class="border rounded text-center py-2 px-1">
        <div class="text-muted" style="font-size:11px">Fav. Time</div>
        <div class="fw-bold" style="font-size:12px">${p.fav_time || '-'}</div>
      </div></div>
    </div>`;

    // Favourite products
    if (p.top_products && p.top_products.length) {
      bizHtml += `<div class="mb-3">
        <div class="fw-semibold small text-uppercase text-muted mb-1" style="letter-spacing:.05em">Favourite Products</div>
        <table class="table table-sm table-borderless mb-0">
          <tbody>${p.top_products.slice(0,5).map(prod => `
            <tr>
              <td class="small">${prod.name}</td>
              <td class="small text-muted text-end">${prod.count}× bought</td>
              <td class="small text-success text-end fw-semibold">R${fmt(prod.total_spent)}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
    } else {
      bizHtml += `<div class="text-muted small mb-3">No purchase history yet.</div>`;
    }

    // Last 3 receipts
    const recentReceipts = (p.receipts || []).sort((a,b) => b.date_time.localeCompare(a.date_time)).slice(0,3);
    if (recentReceipts.length) {
      bizHtml += `<div class="mb-3">
        <div class="fw-semibold small text-uppercase text-muted mb-1" style="letter-spacing:.05em">Recent Purchases</div>`;
      recentReceipts.forEach(r => {
        const dt = new Date(r.date_time);
        bizHtml += `<div class="border rounded px-2 py-1 mb-1 small">
          <div class="d-flex justify-content-between">
            <span class="text-muted">${dt.toLocaleDateString('en-ZA')} ${dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</span>
            <span class="fw-semibold text-success">R${fmt(r.total)}</span>
          </div>
          <div class="text-muted" style="font-size:11px">${r.items.map(i => `${i.product_name} ×${i.qty % 1 === 0 ? i.qty : i.qty.toFixed(1)}`).join(', ')}</div>
        </div>`;
      });
      bizHtml += `</div>`;
    }
  }

  // ── Merge history ─────────────────────────────────────────────
  let mergeHistoryHtml = '';
  if (mergeHistory) {
    const absorbed  = mergeHistory.absorbed || [];
    const mergedInto = mergeHistory.merged_into;

    if (mergedInto) {
      // This customer was merged into another
      mergeHistoryHtml = `
        <div class="mt-3 pt-2 border-top">
          <div class="fw-semibold small text-uppercase text-muted mb-2" style="letter-spacing:.05em">Merge Status</div>
          <div class="alert alert-secondary py-2 d-flex align-items-center gap-2">
            <div class="flex-grow-1 small">
              Merged into <strong>${mergedInto.primary_name || mergedInto.primary_number}</strong>
              on ${new Date(mergedInto.merged_at).toLocaleDateString('en-ZA')}
            </div>
            <button class="btn btn-outline-secondary btn-sm"
              onclick="unmergeCustomer(${mergedInto.log_id}, '${(mergedInto.primary_name || mergedInto.primary_number || '').replace(/'/g,'')}')">
              Unmerge
            </button>
          </div>
        </div>`;
    }

    if (absorbed.length) {
      const rows = absorbed.map(m => {
        const dateStr = new Date(m.merged_at).toLocaleDateString('en-ZA');
        const simBadge = m.similarity != null
          ? `<span class="badge ${m.similarity >= 0.95 ? 'bg-success' : 'bg-warning text-dark'} ms-1">${Math.round(m.similarity*100)}%</span>` : '';
        const autoBadge = m.auto_merged ? `<span class="badge bg-info text-dark ms-1" style="font-size:.6rem">Auto</span>` : '';
        const unmergedNote = m.unmerged_at
          ? `<div class="text-muted" style="font-size:.65rem">Unmerged ${new Date(m.unmerged_at).toLocaleDateString('en-ZA')}</div>` : '';
        const unmergeBtn = !m.unmerged_at
          ? `<button class="btn btn-outline-secondary btn-sm flex-shrink-0"
               onclick="unmergeCustomer(${m.log_id}, '${(m.source_name || m.source_customer_number || '').replace(/'/g,'')}')">Unmerge</button>` : '';

        return `
          <div class="d-flex align-items-center gap-2 py-2 border-bottom">
            ${m.source_face_photo
              ? `<img src="${m.source_face_photo}" style="width:44px;height:44px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6;flex-shrink:0">`
              : `<div style="width:44px;height:44px;border-radius:50%;background:#e9ecef;display:flex;align-items:center;justify-content:center;flex-shrink:0"><i class="bi bi-person"></i></div>`}
            <div class="flex-grow-1 min-width-0">
              <div class="small fw-semibold">${m.source_name || '<span class="text-muted fst-italic">Unnamed</span>'}
                ${m.source_customer_number ? `<span class="text-muted ms-1">${m.source_customer_number}</span>` : ''}
                ${simBadge}${autoBadge}
              </div>
              <div class="text-muted" style="font-size:.7rem">${m.source_visit_count || 0} visits · merged ${dateStr}</div>
              ${unmergedNote}
            </div>
            ${unmergeBtn}
          </div>`;
      }).join('');

      mergeHistoryHtml += `
        <div class="mt-3 pt-2 border-top">
          <div class="fw-semibold small text-uppercase text-muted mb-2" style="letter-spacing:.05em">Merged Customers (${absorbed.length})</div>
          ${rows}
        </div>`;
    }
  }

  // ── Delete button ─────────────────────────────────────────────
  const deleteBtn = `<div class="mt-3 pt-2 border-top">
    <button class="btn btn-outline-danger btn-sm" onclick="deleteCustomer(${c.id}, '${(c.name || c.customer_number || 'this customer').replace(/'/g, '')}')">
      Delete Customer
    </button>
    <span class="text-muted small ms-2">Removes all biometric data and visit history permanently.</span>
  </div>`;

  // ── Two radar charts: Biometric + Behavioural ────────────────
  const radarHtml = radar ? `
    <div class="mb-3">
      <div class="d-flex gap-2 justify-content-center flex-wrap">
        <div class="text-center">
          <div class="text-muted small fw-semibold mb-1">Biometric</div>
          <canvas id="customer-radar-bio-${c.id}" width="300" height="300"></canvas>
        </div>
        <div class="text-center">
          <div class="text-muted small fw-semibold mb-1">Behavioural</div>
          <canvas id="customer-radar-beh-${c.id}" width="300" height="300"></canvas>
        </div>
      </div>
      <div class="text-muted text-center mt-1" style="font-size:.7rem">
        ${radar.details.face_angles} angle${radar.details.face_angles!==1?'s':''} ·
        ID best ${radar.details.best_face_sim}% avg ${radar.details.avg_face_sim}% ·
        ${radar.details.purchase_count} purchase${radar.details.purchase_count!==1?'s':''} ·
        ${radar.details.distinct_days} day${radar.details.distinct_days!==1?'s':''} ·
        ${radar.details.last_visit_days !== null ? radar.details.last_visit_days + 'd ago' : 'never seen'}
      </div>
    </div>` : '';

  // ── Assemble ──────────────────────────────────────────────────
  document.getElementById('customerDetailBody').innerHTML = `
    <div class="d-flex gap-3 mb-3 align-items-start">
      <div class="flex-shrink-0">${photoHtml}</div>
      <div class="flex-grow-1">
        <div class="fw-semibold fs-5 mb-1">${c.name || '<span class="text-muted fst-italic">Unnamed</span>'}</div>
        <div class="mb-1">${signalBadges}</div>
        <div class="text-muted small">
          ${c.phone ? c.phone + ' · ' : ''}
          ${c.last_visit ? 'Last seen ' + new Date(c.last_visit).toLocaleDateString('en-ZA') : 'Never purchased'}
        </div>
      </div>
    </div>
    <hr class="my-2">
    ${radarHtml}
    ${bizHtml}
    ${attrsHtml ? '<hr class="my-2">' + attrsHtml : ''}
    <hr class="my-2">
    ${visitsHtml}
    ${mergeHistoryHtml}
    ${deleteBtn}`;

  // Draw both radars after DOM is ready
  if (radar) {
    if (radar.biometric)   drawRadarChart(`customer-radar-bio-${c.id}`, radar.biometric,   '#2a6f3e');
    if (radar.behavioural) drawRadarChart(`customer-radar-beh-${c.id}`, radar.behavioural, '#0d6efd');
  }
}

async function unmergeCustomer(logId, sourceName) {
  const label = sourceName || 'this customer';
  if (!confirm(
    `Unmerge "${label}"?\n\n` +
    `• Their profile will be reactivated as a separate customer\n` +
    `• Their biometric data will be restored (if this merge was done with the current system)\n` +
    `• Visit and sales history will remain on the primary profile\n\n` +
    `Continue?`
  )) return;

  try {
    const result = await api(`/api/customers/merge_log/${logId}/unmerge`, { method: 'POST' });
    if (result.soft_unmerge) {
      toast(`${label} reactivated. Biometric data will rebuild automatically on next sighting.`, 'info', 7000);
    } else {
      toast(`${label} unmerged - biometric data restored.`, 'success', 5000);
    }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('customerDetailModal')).hide();
    await loadCustomers();
  } catch(e) {
    toast(e.message, 'danger');
  }
}

async function openCustomerEnroll(customerId) {
  const c = customerId ? STATE.customers.find(x => x.id === customerId) : null;
  document.getElementById('customerEnrollTitle').textContent = c ? 'Edit Customer' : 'Enroll Customer';
  document.getElementById('enroll-customer-id').value = c?.id || '';
  document.getElementById('enroll-name').value        = c?.name  || '';
  document.getElementById('enroll-phone').value       = c?.phone || '';
  document.getElementById('enroll-email').value       = c?.email || '';
  document.getElementById('enroll-notes').value       = c?.notes || '';
  document.getElementById('enroll-is-employee').checked = c?.is_employee || false;

  // Plates
  const platesList = document.getElementById('enroll-plates-list');
  platesList.innerHTML = '';
  (c?.plates || []).forEach(plate => addPlateBadge(plate));

  // Biometric status
  document.getElementById('enroll-face-status').innerHTML  = c?.has_face ? 'Face: enrolled <i class="bi bi-check-lg"></i>' : 'Face: not enrolled';
  document.getElementById('enroll-face-status').className    = `badge ${c?.has_face ? 'bg-success' : 'bg-secondary'}`;
  document.getElementById('enroll-gait-status').innerHTML  = c?.has_gait ? 'Body: enrolled <i class="bi bi-check-lg"></i>' : 'Body: not enrolled';
  document.getElementById('enroll-gait-status').className    = `badge ${c?.has_gait ? 'bg-success' : 'bg-secondary'}`;

  // Face photo
  const photoEl = document.getElementById('enroll-face-photo');
  if (photoEl) {
    if (c?.has_face) {
      photoEl.src = `/api/customers/${c.id}/photo?t=${Date.now()}`;
      photoEl.style.display = 'block';
    } else {
      photoEl.style.display = 'none';
    }
  }

  // Physical attributes
  const attrsEl = document.getElementById('enroll-attributes');
  if (attrsEl) {
    attrsEl.innerHTML = '';
    if (c?.id) {
      try {
        const attrs = await api(`/api/customers/${c.id}/attributes`);
        if (attrs) {
          const items = [
            attrs.height_category && `Height: ${attrs.height_category}`,
            attrs.height_cm       && `${attrs.height_cm}cm`,
            attrs.build           && `Build: ${attrs.build}`,
            attrs.age_range       && `Age: ${attrs.age_range}`,
            attrs.gender          && `Gender: ${attrs.gender}`,
            attrs.hair_color      && `Hair: ${attrs.hair_color}`,
            attrs.skin_tone       && `Skin: ${attrs.skin_tone}`,
            attrs.eye_color       && `Eyes: ${attrs.eye_color}`,
            attrs.facial_hair && attrs.facial_hair !== 'none' && `Facial hair: ${attrs.facial_hair}`,
            attrs.wearing_glasses !== null && attrs.wearing_glasses !== undefined && `Glasses: ${attrs.wearing_glasses ? 'yes' : 'no'}`,
            attrs.camera_source   && `Camera: ${attrs.camera_source}`,
            attrs.confidence      && `Confidence: ${Math.round(attrs.confidence * 100)}%`,
          ].filter(Boolean);
          if (items.length) {
            attrsEl.innerHTML = items.map(i =>
              `<span class="badge bg-light text-dark border me-1 mb-1">${i}</span>`
            ).join('');
          }
        }
      } catch(e) {}
    }
  }

  const deactivateBtn = document.getElementById('btn-deactivate-customer');
  c ? show(deactivateBtn) : hide(deactivateBtn);

  bootstrap.Modal.getOrCreateInstance(document.getElementById('customerEnrollModal')).show();
}

function addPlateBadge(plate) {
  const platesList = document.getElementById('enroll-plates-list');
  const badge = document.createElement('span');
  badge.className = 'badge bg-light text-dark border d-flex align-items-center gap-1';
  badge.innerHTML = `${plate} <button type="button" class="btn-close btn-close-sm" style="font-size:0.6rem" aria-label="Remove"></button>`;
  badge.querySelector('.btn-close').addEventListener('click', () => badge.remove());
  badge.dataset.plate = plate;
  platesList.appendChild(badge);
}

async function deleteCustomer(customerId, name) {
  if (!confirm(`Permanently delete "${name}"?\n\nThis removes all biometric data, visit history and recognition data. This cannot be undone.`)) return;
  try {
    await api(`/api/customers/${customerId}/delete_permanent`, { method: 'POST' });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('customerDetailModal')).hide();
    toast(`${name} deleted`, 'warning');
    await loadCustomers();
  } catch(e) {
    toast(e.message, 'danger');
  }
}

document.getElementById('btn-refresh-customers')?.addEventListener('click', async () => {
  const btn = document.getElementById('btn-refresh-customers');
  btn.disabled = true;
  btn.textContent = '↻ Refreshing...';
  await loadCustomers();
  btn.disabled = false;
  btn.textContent = '↻ Refresh';
});

document.getElementById('btn-add-plate')?.addEventListener('click', () => {
  const input = document.getElementById('enroll-plate-input');
  const plate = input.value.trim().toUpperCase();
  if (!plate) return;
  addPlateBadge(plate);
  input.value = '';
});

document.getElementById('enroll-plate-input')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); document.getElementById('btn-add-plate').click(); }
});

document.getElementById('btn-save-customer')?.addEventListener('click', async () => {
  const cid   = document.getElementById('enroll-customer-id').value;
  const name  = document.getElementById('enroll-name').value.trim();

  const payload = {
    name: name || null,
    phone: document.getElementById('enroll-phone').value.trim() || null,
    email: document.getElementById('enroll-email').value.trim() || null,
    notes: document.getElementById('enroll-notes').value.trim() || null,
    is_employee: document.getElementById('enroll-is-employee').checked,
  };

  try {
    let id = cid ? parseInt(cid) : null;
    if (!id) {
      const r = await api('/api/customers', { method: 'POST', body: JSON.stringify(payload) });
      id = r.id;
    } else {
      await api(`/api/customers/${id}`, { method: 'POST', body: JSON.stringify(payload) });
    }

    // Sync plates
    const existingC = STATE.customers.find(x => x.id === id);
    const existingPlates = existingC?.plates || [];
    const newPlates = [...document.getElementById('enroll-plates-list').querySelectorAll('[data-plate]')]
      .map(b => b.dataset.plate);

    for (const p of newPlates) {
      if (!existingPlates.includes(p)) {
        await api(`/api/customers/${id}/enroll/plate`, { method: 'POST', body: JSON.stringify({ plate_number: p }) });
      }
    }

    bootstrap.Modal.getOrCreateInstance(document.getElementById('customerEnrollModal')).hide();
    toast(cid ? 'Customer updated' : 'Customer enrolled', 'success');
    await loadCustomers();
  } catch (e) {
    toast(e.message, 'danger');
  }
});

document.getElementById('btn-deactivate-customer')?.addEventListener('click', async () => {
  const cid = document.getElementById('enroll-customer-id').value;
  if (!cid || !confirm('Deactivate this customer?')) return;
  try {
    await api(`/api/customers/${cid}`, { method: 'DELETE' });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('customerEnrollModal')).hide();
    toast('Customer deactivated', 'success');
    await loadCustomers();
  } catch (e) {
    toast(e.message, 'danger');
  }
});

async function quickNameCustomer(cid) {
  const input = document.getElementById(`qn-input-${cid}`);
  const name  = (input?.value || '').trim();
  if (!name) return toast('Enter a name', 'warning');
  try {
    await api(`/api/customers/${cid}/name`, { method: 'POST', body: JSON.stringify({ name }) });
    const c = STATE.customers.find(x => x.id === cid);
    if (c) c.name = name;
    toast(`Named: ${name}`, 'success', 2000);
    renderCustomersList();
  } catch(e) { toast(e.message, 'error'); }
}

document.getElementById('btn-cleanup-empty')?.addEventListener('click', async () => {
  try {
    const preview = await api('/api/customers/cleanup_empty', { method: 'POST' });
    if (preview.deleted === 0) {
      toast('No empty records to clean up', 'info');
    } else {
      if (!confirm(`Delete ${preview.deleted} auto-enrolled customer${preview.deleted !== 1 ? 's' : ''} with no face data and no recent visits? This cannot be undone.`)) return;
      toast(`Deleted ${preview.deleted} empty records`, 'warning', 4000);
      await loadCustomers();
    }
  } catch(e) { toast(e.message, 'error'); }
});

// Sort/search change re-renders without reloading
document.getElementById('customer-sort')?.addEventListener('change', renderCustomersList);
document.getElementById('customer-search')?.addEventListener('input', renderCustomersList);

// Auto-refresh customers tab every 5 seconds while visible
// Also run duplicate check automatically on open
let _customerTabRefreshTimer = null;
document.querySelector('[data-bs-target="#customers"]')?.addEventListener('shown.bs.tab', () => {
  loadCustomers();  // includes loadMergeSuggestions() via loadCustomers
  if (_customerTabRefreshTimer) clearInterval(_customerTabRefreshTimer);
  _customerTabRefreshTimer = setInterval(loadCustomers, 5000);
});
document.querySelector('[data-bs-target="#customers"]')?.addEventListener('hidden.bs.tab', () => {
  if (_customerTabRefreshTimer) { clearInterval(_customerTabRefreshTimer); _customerTabRefreshTimer = null; }
});

// ─── Merge Suggestions - runs automatically on tab open ──────
async function loadMergeSuggestions() {
  const panel = document.getElementById('merge-suggestions-panel');
  if (!panel) return;
  try {
    const suggestions = await api('/api/customers/merge_suggestions');
    if (!suggestions.length) {
      panel.innerHTML = '';
      panel.classList.add('hidden');
      return;
    }

    // Auto-merge threshold - read from settings, default 0.95
    let AUTO_MERGE_THRESHOLD = 0.95;
    try { const cfg = await api('/api/settings'); AUTO_MERGE_THRESHOLD = parseFloat(cfg.auto_merge_min_sim ?? 0.95); } catch {}

    const autoMerge = suggestions.filter(s => s.similarity >= AUTO_MERGE_THRESHOLD);
    const manual    = suggestions.filter(s => s.similarity <  AUTO_MERGE_THRESHOLD);

    for (const s of autoMerge) {
      try {
        // Let server auto-select primary by score; pass both ids with no primary_id
        const allIds = [s.customer_a.id, s.customer_b.id];
        await api('/api/customers/merge', {
          method: 'POST',
          body: JSON.stringify({ merge_ids: allIds, auto_merged: true, similarity: s.similarity })
        });
        const nameA = s.customer_a.name || s.customer_a.customer_number;
        const nameB = s.customer_b.name || s.customer_b.customer_number;
        toast(`Auto-merged ${nameA} ↔ ${nameB} (${Math.round(s.similarity * 100)}% match)`, 'success', 5000);
      } catch(e) {
        toast(`Auto-merge failed: ${e.message}`, 'danger');
      }
    }

    if (autoMerge.length) await loadCustomers();

    // Show manual review panel for lower-confidence pairs
    if (!manual.length) {
      panel.innerHTML = '';
      panel.classList.add('hidden');
      return;
    }

    panel.classList.remove('hidden');
    panel.innerHTML = `
      <div class="alert alert-warning py-2 mb-2">
        <strong>${manual.length} possible duplicate${manual.length > 1 ? 's' : ''} - review needed</strong>
        <span class="text-muted small ms-2">Merge or decline each pair.</span>
      </div>
      ${manual.map(s => `
        <div class="border rounded px-3 py-2 mb-2 d-flex align-items-center gap-3 bg-warning-subtle">
          <div class="d-flex gap-2 flex-shrink-0" style="cursor:pointer" onclick="openMergeSuggestion(${s.customer_a.id}, ${s.customer_b.id})">
            <img src="/api/customers/${s.customer_a.id}/photo" style="width:40px;height:40px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6" onerror="this.style.display='none'">
            <img src="/api/customers/${s.customer_b.id}/photo" style="width:40px;height:40px;object-fit:cover;border-radius:50%;border:2px solid #dee2e6" onerror="this.style.display='none'">
          </div>
          <div class="flex-grow-1" style="cursor:pointer" onclick="openMergeSuggestion(${s.customer_a.id}, ${s.customer_b.id})">
            <span class="fw-semibold">${s.customer_a.name || s.customer_a.customer_number}</span>
            <span class="text-muted small ms-1">(${s.customer_a.visit_count} visits)</span>
            <span class="mx-2 text-muted">↔</span>
            <span class="fw-semibold">${s.customer_b.name || s.customer_b.customer_number}</span>
            <span class="text-muted small ms-1">(${s.customer_b.visit_count} visits)</span>
          </div>
          <span class="badge bg-warning text-dark me-1">${Math.round(s.similarity * 100)}% similar</span>
          <button class="btn btn-outline-danger btn-sm" title="Not the same person - never suggest again"
                  onclick="declineMergeSuggestion(${s.customer_a.id}, ${s.customer_b.id}, event)"><i class="bi bi-x-lg me-1"></i>Not same</button>
        </div>`).join('')}`;
  } catch(e) { /* silently fail */ }
}

function openMergeSuggestion(idA, idB) {
  // Tick both checkboxes and open merge modal
  document.querySelectorAll('.merge-check').forEach(cb => {
    cb.checked = cb.dataset.id == idA || cb.dataset.id == idB;
  });
  updateMergeToolbar();
  openMergeModal();
}

async function declineMergeSuggestion(idA, idB, event) {
  event.stopPropagation();
  try {
    await api('/api/customers/exclusions', {
      method: 'POST',
      body: JSON.stringify({ customer_a_id: idA, customer_b_id: idB, reason: 'Declined by user' })
    });
    toast('Pair marked as different people - won\'t be suggested again', 'success');
    await loadMergeSuggestions();
  } catch(e) {
    toast('Could not save decline: ' + e.message, 'error');
  }
}

// ─── Dual-handle merge slider ────────────────────────────────
function initMergeSlider(reviewPct, autoPct) {
  const reviewEl = document.getElementById('merge-review-min');
  const autoEl   = document.getElementById('merge-auto-min');
  const reviewVal = document.getElementById('merge-review-val');
  const autoVal   = document.getElementById('merge-auto-val');
  const track     = document.getElementById('merge-slider-track');
  if (!reviewEl || !autoEl) return;

  reviewEl.value = Math.round(reviewPct * 100);
  autoEl.value   = Math.round(autoPct   * 100);

  function updateTrack() {
    const min = 10, max = 99;
    const r = parseInt(reviewEl.value);
    const a = parseInt(autoEl.value);
    const rPct = (r - min) / (max - min) * 100;
    const aPct = (a - min) / (max - min) * 100;
    if (track) track.style.left  = rPct + '%';
    if (track) track.style.width = Math.max(0, aPct - rPct) + '%';
    if (reviewVal) reviewVal.textContent = r;
    if (autoVal)   autoVal.textContent   = a;
    // Enable pointer events on slider thumbs
    reviewEl.style.pointerEvents = 'none';
    autoEl.style.pointerEvents   = 'none';
    // Whichever thumb is being dragged gets pointer events
  }

  reviewEl.addEventListener('input', () => {
    if (parseInt(reviewEl.value) > parseInt(autoEl.value))
      reviewEl.value = autoEl.value;
    updateTrack();
    _markRecSettingsDirty();
  });
  autoEl.addEventListener('input', () => {
    if (parseInt(autoEl.value) < parseInt(reviewEl.value))
      autoEl.value = reviewEl.value;
    updateTrack();
    _markRecSettingsDirty();
  });

  // Allow both thumbs to be dragged by detecting which is closer to click
  const wrap = document.getElementById('merge-slider-wrap');
  if (wrap) {
    wrap.addEventListener('mousedown', e => {
      reviewEl.style.pointerEvents = 'auto';
      autoEl.style.pointerEvents   = 'auto';
    });
    wrap.addEventListener('touchstart', e => {
      reviewEl.style.pointerEvents = 'auto';
      autoEl.style.pointerEvents   = 'auto';
    }, { passive: true });
  }

  updateTrack();
}

// ─── Configuration Tab ────────────────────────────────

function _flashSaved(id) {
  const el = document.getElementById(id);
  if (!el) return;
  show(el);
  setTimeout(() => hide(el), 3000);
}

function _bindSlider(id, valId, fmt) {
  const el  = document.getElementById(id);
  const vEl = document.getElementById(valId);
  if (!el) return;
  el.oninput = () => {
    if (vEl) vEl.textContent = fmt ? fmt(parseFloat(el.value)) : parseFloat(el.value).toFixed(2);
  };
}

function _setSlider(id, valId, val, fmt) {
  const el  = document.getElementById(id);
  const vEl = document.getElementById(valId);
  if (el)  el.value = val;
  if (vEl) vEl.textContent = fmt ? fmt(val) : parseFloat(val).toFixed(2);
}

document.querySelector('[data-bs-target="#recognition-settings"]')?.addEventListener('shown.bs.tab', async () => {
  try {
    const s = await api('/api/settings');

    // Business
    _setSlider('set-markup-pct', 'set-markup-pct-val', s.markup_percent, v => Math.round(v) + '%');
    _bindSlider('set-markup-pct', 'set-markup-pct-val', v => Math.round(v) + '%');

    // Kiosk connection settings
    const apiKeyEl     = document.getElementById('kiosk-api-key');
    const portEl       = document.getElementById('kiosk-port');
    const inactivityEl = document.getElementById('kiosk-inactivity-mins');
    const syncUrlEl    = document.getElementById('kiosk-sync-url');
    if (apiKeyEl)     apiKeyEl.value     = s.kiosk_api_key            || '';
    if (portEl)       portEl.value       = s.kiosk_port               || 2323;
    if (inactivityEl) inactivityEl.value = s.kiosk_inactivity_minutes || 0;
    if (syncUrlEl)    syncUrlEl.value    = s.kiosk_url                || '';

    // Load kiosk tablet statuses
    loadKioskTablets();

    // Face Recognition
    _setSlider('set-face-threshold',   'set-face-threshold-val',  s.face_threshold);
    _setSlider('set-link-threshold',   'set-link-threshold-val',  s.link_threshold);
    _setSlider('set-face-quality-min', 'set-face-quality-val',    s.face_quality_min);
    _setSlider('set-visit-min-gap',    'set-visit-min-gap-val',   s.visit_min_gap_seconds ?? 180, v => Math.round(v) + 's');
    _bindSlider('set-face-threshold',   'set-face-threshold-val');
    _bindSlider('set-link-threshold',   'set-link-threshold-val');
    _bindSlider('set-face-quality-min', 'set-face-quality-val');
    _bindSlider('set-visit-min-gap',    'set-visit-min-gap-val',  v => Math.round(v) + 's');

    // Merging
    initMergeSlider(s.merge_suggest_min_sim, s.auto_merge_min_sim ?? 0.95);

    // Enrollment
    _setSlider('set-max-face-angles', 'set-max-face-angles-val', s.max_face_angles, v => Math.round(v) + ' angles');
    _setSlider('set-min-angle-dist',  'set-min-angle-dist-val',  s.min_angle_distance, v => parseFloat(v).toFixed(2));
    _bindSlider('set-max-face-angles', 'set-max-face-angles-val', v => Math.round(v) + ' angles');
    _bindSlider('set-min-angle-dist',  'set-min-angle-dist-val',  v => parseFloat(v).toFixed(2));

    // Printer settings
    const rwEl = document.getElementById('setting-receipt-width');
    if (rwEl && s.receipt_width_mm) rwEl.value = s.receipt_width_mm;
    const apEl = document.getElementById('setting-auto-print-receipt');
    if (apEl) apEl.checked = s.auto_print_receipt === 'true';
    // Populate receipt printer select
    const rpSel = document.getElementById('setting-receipt-printer');
    if (rpSel) {
      rpSel.innerHTML = '<option value="">Auto-detect USB (default)</option>';
      (LABELS.printers || []).forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.id; opt.textContent = `${p.name} (${p.connection})`;
        if (String(p.id) === String(s.receipt_printer_id)) opt.selected = true;
        rpSel.appendChild(opt);
      });
    }
    // Populate label printer config list
    _renderPrinterConfigList();

    // Branding (white-label)
    const _bset = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
    _brandFillFontSelects();
    _bset('brand-store-name', s.branding_store_name);
    _bset('brand-primary-hex', s.branding_primary);
    _bset('brand-bg-hex', s.branding_bg);
    _bset('brand-font', s.branding_font || 'Nunito');
    _bset('brand-invoice-legal', s.branding_invoice_legal);
    _bset('brand-invoice-subtitle', s.branding_invoice_subtitle);
    _bset('brand-invoice-footer', s.branding_invoice_footer);
    _bset('web-primary-hex', s.web_branding_primary);
    _bset('web-font', s.web_branding_font || 'Nunito');
    // sync the colour pickers to their hex fields
    for (const [pick, hex] of [['brand-primary-picker','brand-primary-hex'],['brand-bg-picker','brand-bg-hex'],['web-primary-picker','web-primary-hex']]) {
      const hv = document.getElementById(hex)?.value;
      if (hv && /^#[0-9a-fA-F]{6}$/.test(hv)) { const p = document.getElementById(pick); if (p) p.value = hv; }
    }
  } catch(e) { console.error('loadConfigSettings', e); }
});

// Font <select> options rendered IN their own font so the owner sees a live sample.
const _BRAND_FONTS = ['Nunito','Arial','Helvetica','Verdana','Tahoma','Georgia','Times New Roman','Courier New','system-ui'];
function _brandFillFontSelects() {
  for (const id of ['brand-font','web-font']) {
    const sel = document.getElementById(id);
    if (!sel || sel.options.length) continue;   // fill once
    for (const f of _BRAND_FONTS) {
      const o = document.createElement('option');
      o.value = f; o.textContent = f; o.style.fontFamily = "'" + f + "'";
      sel.appendChild(o);
    }
  }
}

// Colour pickers mirror into their hex fields (live)
document.getElementById('brand-primary-picker')?.addEventListener('input', (e) => {
  const hex = document.getElementById('brand-primary-hex'); if (hex) hex.value = e.target.value;
});
document.getElementById('brand-bg-picker')?.addEventListener('input', (e) => {
  const hex = document.getElementById('brand-bg-hex'); if (hex) hex.value = e.target.value;
});
// Clear the background = fall back to an auto light tint of the primary colour
document.getElementById('brand-bg-clear')?.addEventListener('click', () => {
  const hex = document.getElementById('brand-bg-hex'); if (hex) hex.value = '';
});
document.getElementById('web-primary-picker')?.addEventListener('input', (e) => {
  const hex = document.getElementById('web-primary-hex'); if (hex) hex.value = e.target.value;
});

document.getElementById('btn-save-branding')?.addEventListener('click', async () => {
  const _v = id => (document.getElementById(id)?.value || '').trim();
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      branding_store_name:        _v('brand-store-name'),
      branding_primary:           _v('brand-primary-hex'),
      branding_bg:                _v('brand-bg-hex'),
      branding_font:              _v('brand-font'),
      branding_invoice_legal:     _v('brand-invoice-legal'),
      branding_invoice_subtitle:  _v('brand-invoice-subtitle'),
      branding_invoice_footer:    _v('brand-invoice-footer'),
      web_branding_primary:       _v('web-primary-hex'),
      web_branding_font:          _v('web-font'),
    })});
    _flashSaved('branding-saved');
    toast('Branding saved - reload to see it (applies within 30s everywhere)', 'success', 3500);
  } catch(e) { toast(e.message, 'error'); }
});

// ── Printer settings save ─────────────────────────────────────────────────────
document.getElementById('btn-save-printer-settings')?.addEventListener('click', async () => {
  const width     = document.getElementById('setting-receipt-width')?.value || '72';
  const autoprint = document.getElementById('setting-auto-print-receipt')?.checked ? 'true' : 'false';
  const printerId = document.getElementById('setting-receipt-printer')?.value || '';
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      receipt_width_mm:    width,
      auto_print_receipt:  autoprint,
      receipt_printer_id:  printerId,
    })});
    STATE._receiptPrinterId = printerId ? parseInt(printerId) : null;
    _flashSaved('printer-settings-saved');
    toast('Printer settings saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
});

function _renderPrinterConfigList() {
  const wrap = document.getElementById('printer-config-list');
  if (!wrap) return;
  if (!LABELS.printers.length) {
    wrap.innerHTML = '<div class="text-muted small">No printers configured yet.</div>';
    return;
  }
  wrap.innerHTML = LABELS.printers.map(p => `
    <div class="d-flex align-items-center gap-2 py-1 border-bottom">
      <span style="flex:1"><strong>${escapeHtml(p.name)}</strong>
        <span class="text-muted small ms-1">${p.connection}${p.address ? ' · ' + escapeHtml(p.address) : ''}</span>
      </span>
      <button class="btn btn-outline-danger btn-sm py-0 px-2" onclick="deletePrinter(${p.id})"><i class="bi bi-x-lg"></i></button>
    </div>
  `).join('');
}

async function openAddPrinterModal() {
  const name    = prompt('Printer name (e.g. XP-365B Receipt):', 'XP-365B');
  if (!name) return;
  const connOpts = ['usb', 'bluetooth', 'network'];
  const conn    = prompt('Connection type (usb / bluetooth / network):', 'usb');
  if (!connOpts.includes(conn)) { toast('Invalid connection type', 'warning'); return; }
  const address = prompt(
    conn === 'usb'       ? 'USB VID:PID (e.g. 2d84:0200) — blank = auto-detect:' :
    conn === 'bluetooth' ? 'Bluetooth MAC (e.g. XX:XX:XX:XX:XX:XX):' :
                           'IP:port (e.g. 192.168.1.100:9100):',
    ''
  ) || '';
  try {
    await api('/api/label-printers', { method: 'POST', body: JSON.stringify({
      name, model: 'xprinter_xp365b', connection: conn, address,
    })});
    await loadLabelPrinters();
    _renderPrinterConfigList();
    // Refresh receipt printer select
    const rpSel = document.getElementById('setting-receipt-printer');
    if (rpSel) _populatePrinterSelect(rpSel);
    toast(`Printer "${name}" added`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function deletePrinter(id) {
  if (!confirm('Remove this printer?')) return;
  try {
    await api(`/api/label-printers/${id}`, { method: 'DELETE' });
    await loadLabelPrinters();
    _renderPrinterConfigList();
    toast('Printer removed', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

// Logo uploads immediately on file selection
document.getElementById('brand-logo-file')?.addEventListener('change', async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const fd = new FormData(); fd.append('logo', file);
  try {
    // Direct fetch (NOT api()) - api() forces Content-Type: application/json which would
    // strip the multipart boundary and give Flask an empty request.files. Let the browser
    // set the multipart Content-Type itself.
    const res = await fetch('/api/branding/logo', { method: 'POST', body: fd, credentials: 'same-origin' });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.error || 'Upload failed');
    const thumb = document.getElementById('brand-logo-thumb');
    if (thumb && j.logo_url) thumb.src = j.logo_url + '?t=' + Date.now();
    toast('Logo updated - reload to see it in the header', 'success', 3500);
  } catch(err) { toast('Logo upload failed: ' + err.message, 'error'); }
  e.target.value = '';
});

document.getElementById('btn-save-business-settings')?.addEventListener('click', async () => {
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      markup_percent: parseFloat(document.getElementById('set-markup-pct')?.value || 20),
    })});
    _globalMarkupPct = parseFloat(document.getElementById('set-markup-pct')?.value || 20);
    _flashSaved('business-settings-saved');
    toast('Business settings saved', 'success', 2000);
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-save-recognition-settings')?.addEventListener('click', async () => {
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      face_threshold:       parseFloat(document.getElementById('set-face-threshold')?.value),
      link_threshold:       parseFloat(document.getElementById('set-link-threshold')?.value),
      face_quality_min:     parseFloat(document.getElementById('set-face-quality-min')?.value),
      visit_min_gap_seconds: parseInt(document.getElementById('set-visit-min-gap')?.value || 180),
    })});
    _flashSaved('rec-settings-saved');
    toast('Recognition settings saved - takes effect within 60s', 'success', 3000);
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-save-merge-settings')?.addEventListener('click', async () => {
  try {
    const reviewPct = parseInt(document.getElementById('merge-review-min')?.value || 55);
    const autoPct   = parseInt(document.getElementById('merge-auto-min')?.value   || 95);
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      merge_suggest_min_sim: reviewPct / 100,
      auto_merge_min_sim:    autoPct   / 100,
    })});
    _flashSaved('merge-settings-saved');
    toast('Merge settings saved', 'success', 2000);
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-save-enrollment-settings')?.addEventListener('click', async () => {
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      max_face_angles:   parseInt(document.getElementById('set-max-face-angles')?.value  || 24),
      min_angle_distance: parseFloat(document.getElementById('set-min-angle-dist')?.value || 0.25),
    })});
    _flashSaved('enrollment-settings-saved');
    toast('Enrollment settings saved', 'success', 2000);
  } catch(e) { toast(e.message, 'error'); }
});

// ─── Kiosk Tablet Management ────────────────────────────────

let _kioskTablets = [];

function _batteryIcon(level) {
  if (level == null) return '';
  if (level > 60) return '<i class="bi bi-battery-full"></i>';
  if (level > 30) return '<i class="bi bi-battery-half"></i>';
  return '<i class="bi bi-battery"></i>';
}

function _statusBadge(available) {
  return available === false
    ? '<span class="badge bg-secondary">Offline</span>'
    : '<span class="badge bg-success">Online</span>';
}

function _renderKioskTablets(statuses) {
  const list = document.getElementById('kiosk-tablet-list');
  if (!list) return;
  if (!_kioskTablets.length) {
    list.innerHTML = '<div class="text-muted small p-3">No tablets configured. Add one below.</div>';
    return;
  }
  list.innerHTML = _kioskTablets.map((t, i) => {
    const s = statuses?.[i];
    const online = s && !s.error;
    const battery = s?.battery;
    const screen  = s?.screen;
    const wifi    = s?.wifi;
    const mem     = s?.memory;
    return `
      <div class="p-3 border-bottom">
        <div class="d-flex justify-content-between align-items-start mb-2">
          <div>
            <span class="fw-semibold">${t.name}</span>
            <span class="text-muted small ms-2">${t.ip}</span>
          </div>
          <div class="d-flex gap-1 align-items-center">
            ${_statusBadge(online)}
            <button class="btn btn-link btn-sm text-danger p-0 ms-2" onclick="removeKioskTablet(${i})" title="Remove"><i class="bi bi-x-lg"></i></button>
          </div>
        </div>
        ${(t.url || (t.apps && t.apps.length)) ? `
        <div class="d-flex flex-wrap gap-1 mb-2">
          ${t.url ? `<button class="btn btn-outline-primary btn-sm" onclick="kioskAction('${t.ip}','url',{url:${JSON.stringify(t.url)}}).then(()=>kioskAction('${t.ip}','reload'))" title="${t.url}"><i class="bi bi-globe me-1"></i>Open URL</button>` : ''}
          ${(t.apps||[]).map(a => a.package ? `<button class="btn btn-outline-success btn-sm" onclick="kioskAction('${t.ip}','app/launch',{package:${JSON.stringify(a.package)}})">${a.label||a.package}</button>` : '').join('')}
        </div>` : ''}
        ${online ? `
        <!-- Status row -->
        <div class="d-flex flex-wrap gap-3 small text-muted mb-2">
          ${battery ? `<span title="Battery">${_batteryIcon(battery.level)} ${battery.level}%${battery.charging ? ' <i class="bi bi-lightning"></i>' : ''} · ${battery.temperature}°C</span>` : ''}
          ${screen  ? `<span title="Screen"><i class="bi bi-display me-1"></i>${screen.on ? 'On' : 'Off'} · ${screen.brightness}% brightness${screen.screensaverActive ? ' · screensaver' : ''}</span>` : ''}
          ${wifi    ? `<span title="WiFi"><i class="bi bi-wifi me-1"></i>${wifi.ssid || 'WiFi'} · ${wifi.rssi} dBm · ${wifi.ip}</span>` : ''}
          ${mem     ? `<span title="Memory"><i class="bi bi-memory me-1"></i>${mem.availableMB}MB free / ${mem.totalMB}MB${mem.lowMemory ? ' <i class="bi bi-exclamation-triangle text-warning"></i> LOW' : ''}</span>` : ''}
          ${s.audio ? `<span title="Volume"><i class="bi bi-volume-up me-1"></i>${s.audio.volume ?? '?'}%</span>` : ''}
        </div>

        <!-- Screen controls -->
        <div class="mb-1 small text-muted fw-semibold">Screen</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','screen/on')">On</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','screen/off')">Off</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','wake')">Wake</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','lock')">Lock</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','screensaver/on')">Screensaver On</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','screensaver/off')">Screensaver Off</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskSetBrightness('${t.ip}', ${screen?.brightness ?? 80})">Brightness…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAutoBrightness('${t.ip}')">Auto Brightness…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','autoBrightness/disable')">Auto Off</button>
        </div>

        <!-- WebView controls -->
        <div class="mb-1 small text-muted fw-semibold">WebView</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','reload')">Reload</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','clearCache')">Clear Cache</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskNavigate('${t.ip}')">Navigate…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskSwitchMode('${t.ip}')">Switch Mode…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskRunJs('${t.ip}')">Run JS…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskTypeText('${t.ip}')">Type Text…</button>
        </div>

        <!-- Remote / D-pad -->
        <div class="mb-1 small text-muted fw-semibold">Remote Control</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/up')">▲</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/down')">▼</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/left')">◀</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/right')">▶</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/select')">OK</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/back')">Back</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/home')">Home</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/menu')">Menu</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','remote/playpause')">⏯</button>
        </div>

        <!-- Keyboard emulation -->
        <div class="mb-1 small text-muted fw-semibold">Keyboard</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskKeyPress('${t.ip}')">Key Press…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskKeyCombo('${t.ip}')">Key Combo…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskTypeText('${t.ip}')">Type Text…</button>
        </div>

        <!-- Audio / Comms -->
        <div class="mb-1 small text-muted fw-semibold">Audio &amp; Notifications</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskSetVolume('${t.ip}', ${s.audio?.volume})">Volume…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','audio/beep')">Beep</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskPlayAudio('${t.ip}')">Play Audio…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskAction('${t.ip}','audio/stop')">Stop Audio</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskTts('${t.ip}')">Speak…</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskToast('${t.ip}')">Toast…</button>
        </div>

        <!-- Apps -->
        <div class="mb-1 small text-muted fw-semibold">Apps</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskLaunchApp('${t.ip}')">Launch App…</button>
        </div>

        <!-- Info / Diagnostics -->
        <div class="mb-1 small text-muted fw-semibold">Diagnostics</div>
        <div class="d-flex flex-wrap gap-1 mb-2">
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','battery')">Battery</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','screen')">Screen</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','volume')">Volume</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','wifi')">WiFi</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','storage')">Storage</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','memory')">Memory</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','sensors')">Sensors</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','autoBrightness')">Auto-Brightness</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','info')">Device Info</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskQuery('${t.ip}','location')">GPS Location</button>
          <button class="btn btn-outline-secondary btn-sm" onclick="kioskShowScreenshot('${t.ip}')">Screenshot</button>
        </div>

        <!-- System -->
        <div class="mb-1 small text-muted fw-semibold">System</div>
        <div class="d-flex flex-wrap gap-1">
          <button class="btn btn-outline-warning btn-sm" onclick="kioskAction('${t.ip}','restart-ui')">Restart UI</button>
          <button class="btn btn-outline-danger btn-sm" onclick="kioskReboot('${t.ip}')">Reboot</button>
        </div>` : '<div class="text-muted small">Cannot reach tablet - check Tailscale connection.</div>'}
      </div>`;
  }).join('');
}

async function loadKioskTablets() {
  try {
    const data = await api('/api/kiosk/tablets');
    _kioskTablets = data.tablets || [];
    _renderKioskTablets(null);
    // Load statuses in parallel
    const statuses = await Promise.all(
      _kioskTablets.map(t => api(`/api/kiosk/status/${t.ip}`).catch(() => ({ error: true, available: false })))
    );
    _renderKioskTablets(statuses);
  } catch(e) { console.error('loadKioskTablets', e); }
}

async function saveKioskTablets() {
  await api('/api/kiosk/tablets', { method: 'POST', body: JSON.stringify({ tablets: _kioskTablets }) });
}

async function removeKioskTablet(i) {
  _kioskTablets.splice(i, 1);
  await saveKioskTablets();
  await loadKioskTablets();
}

async function kioskAction(ip, action, extra) {
  try {
    await api(`/api/kiosk/control/${ip}`, { method: 'POST', body: JSON.stringify({ action, ...(extra || {}) }) });
    toast(`${action} sent`, 'success', 1500);
  } catch(e) { toast(e.message, 'error'); }
}

async function kioskReboot(ip) {
  if (!confirm('Reboot this tablet?')) return;
  await kioskAction(ip, 'reboot');
}

async function kioskSetBrightness(ip, current) {
  const val = prompt('Brightness (0–100):', current);
  if (val === null) return;
  const n = parseInt(val);
  if (isNaN(n) || n < 0 || n > 100) { toast('Enter 0–100', 'error'); return; }
  await kioskAction(ip, 'brightness', { value: n });
}

async function kioskSetVolume(ip, current) {
  const val = prompt('Volume (0–100):', current ?? 50);
  if (val === null) return;
  const n = parseInt(val);
  if (isNaN(n) || n < 0 || n > 100) { toast('Enter 0–100', 'error'); return; }
  await kioskAction(ip, 'volume', { value: n });
}

async function kioskNavigate(ip) {
  const tablet = _kioskTablets.find(t => t.ip === ip);
  const url = prompt('Navigate to URL:', tablet?.url || '');
  if (!url) return;
  await kioskAction(ip, 'url', { url });
}

async function kioskToast(ip) {
  const text = prompt('Message to show on tablet:');
  if (!text) return;
  await kioskAction(ip, 'toast', { text });
}

async function kioskTts(ip) {
  const text = prompt('Text to speak:');
  if (!text) return;
  await kioskAction(ip, 'tts', { text });
}

async function kioskTypeText(ip) {
  const text = prompt('Text to type into the WebView:');
  if (!text) return;
  await kioskAction(ip, 'remote/text', { text });
}

async function kioskRunJs(ip) {
  const code = prompt('JavaScript to execute in WebView:');
  if (!code) return;
  await kioskAction(ip, 'js', { code });
}

async function kioskPlayAudio(ip) {
  const url = prompt('Audio URL to play:');
  if (!url) return;
  await kioskAction(ip, 'audio/play', { url, loop: false });
}

async function kioskAutoBrightness(ip) {
  const min = prompt('Auto-brightness min (0–100):', 10);
  if (min === null) return;
  const max = prompt('Auto-brightness max (0–100):', 100);
  if (max === null) return;
  await kioskAction(ip, 'autoBrightness/enable', { min: parseInt(min), max: parseInt(max) });
}

async function kioskSwitchMode(ip) {
  const mode = prompt('Mode: "webview" or "external_app"');
  if (!mode) return;
  if (mode === 'webview') {
    const url = prompt('URL to load (leave blank to keep current):');
    await kioskAction(ip, 'mode', url ? { mode, url } : { mode });
  } else if (mode === 'external_app') {
    const pkg = prompt('Package name (e.g. com.example.app):');
    if (!pkg) return;
    await kioskAction(ip, 'mode', { mode, package: pkg });
  } else {
    toast('Unknown mode', 'error');
  }
}

async function kioskKeyPress(ip) {
  const key = prompt('Key to press (e.g. enter, f5, a, escape):');
  if (!key) return;
  await kioskAction(ip, `remote/keyboard/${key.trim()}`);
}

async function kioskKeyCombo(ip) {
  const combo = prompt('Key combo (e.g. ctrl+c, ctrl+shift+a):');
  if (!combo) return;
  await kioskAction(ip, 'remote/keyboard', { map: combo.trim() });
}

async function kioskLaunchApp(ip) {
  const pkg = prompt('Package name to launch (e.g. com.spotify.music):');
  if (!pkg) return;
  await kioskAction(ip, 'app/launch', { package: pkg });
}

async function kioskQuery(ip, endpoint) {
  try {
    const data = await api(`/api/kiosk/query/${ip}`, { method: 'POST', body: JSON.stringify({ endpoint }) });
    toast(`${endpoint}: ${JSON.stringify(data)}`, 'info', 6000);
  } catch(e) { toast(e.message, 'error'); }
}

async function kioskShowScreenshot(ip) {
  const win = window.open('', '_blank');
  win.document.write(`<img src="/api/kiosk/screenshot/${ip}" style="max-width:100%">`);
}

document.getElementById('btn-kiosk-refresh')?.addEventListener('click', loadKioskTablets);

document.getElementById('btn-kiosk-add')?.addEventListener('click', async () => {
  const name      = document.getElementById('kiosk-new-name')?.value.trim();
  const ip        = document.getElementById('kiosk-new-ip')?.value.trim();
  const url       = document.getElementById('kiosk-new-url')?.value.trim();
  const app1label = document.getElementById('kiosk-new-app1-label')?.value.trim();
  const app1pkg   = document.getElementById('kiosk-new-app1-pkg')?.value.trim();
  const app2label = document.getElementById('kiosk-new-app2-label')?.value.trim();
  const app2pkg   = document.getElementById('kiosk-new-app2-pkg')?.value.trim();
  if (!name || !ip) { toast('Enter a name and IP', 'error'); return; }
  const apps = [];
  if (app1pkg) apps.push({ label: app1label || app1pkg, package: app1pkg });
  if (app2pkg) apps.push({ label: app2label || app2pkg, package: app2pkg });
  const tablet = { name, ip };
  if (url)        tablet.url  = url;
  if (apps.length) tablet.apps = apps;
  _kioskTablets.push(tablet);
  await saveKioskTablets();
  ['kiosk-new-name','kiosk-new-ip','kiosk-new-url',
   'kiosk-new-app1-label','kiosk-new-app1-pkg',
   'kiosk-new-app2-label','kiosk-new-app2-pkg'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  await loadKioskTablets();
});

document.getElementById('btn-kiosk-save-settings')?.addEventListener('click', async () => {
  try {
    const apiKey      = document.getElementById('kiosk-api-key')?.value.trim();
    const port        = document.getElementById('kiosk-port')?.value.trim();
    const inactivity  = parseInt(document.getElementById('kiosk-inactivity-mins')?.value || 0);
    const syncUrl     = document.getElementById('kiosk-sync-url')?.value.trim();
    await api('/api/settings', { method: 'POST', body: JSON.stringify({
      kiosk_api_key:            apiKey,
      kiosk_port:               parseInt(port) || 2323,
      kiosk_inactivity_minutes: inactivity,
      kiosk_url:                syncUrl,
    })});
    _startKioskInactivityTimer(inactivity);
    _flashSaved('kiosk-settings-saved');
    toast('Kiosk settings saved', 'success', 2000);
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-kiosk-sync-all')?.addEventListener('click', async () => {
  const url = document.getElementById('kiosk-sync-url')?.value.trim();
  if (!_kioskTablets.length) { toast('No tablets configured', 'error'); return; }
  if (!url) { toast('Enter a URL to sync', 'error'); return; }
  const statusEl = document.getElementById('kiosk-sync-status');
  try {
    // Save URL to settings so it persists
    await api('/api/settings', { method: 'POST', body: JSON.stringify({ kiosk_url: url }) });
    // Push URL + reload to all tablets in parallel
    await Promise.all(_kioskTablets.map(t =>
      api(`/api/kiosk/control/${t.ip}`, { method: 'POST', body: JSON.stringify({ action: 'url', url }) })
        .then(() => api(`/api/kiosk/control/${t.ip}`, { method: 'POST', body: JSON.stringify({ action: 'reload' }) }))
        .catch(() => {}) // offline tablets silently skipped
    ));
    if (statusEl) { show(statusEl); setTimeout(() => hide(statusEl), 3000); }
    toast(`Synced ${_kioskTablets.length} tablet(s)`, 'success', 2500);
  } catch(e) { toast(e.message, 'error'); }
});

// ─── Inactivity reload timer ─────────────────────────────────
let _kioskInactivityTimer = null;
let _kioskInactivityMs    = 0;

function _resetKioskInactivity() {
  if (!_kioskInactivityMs) return;
  clearTimeout(_kioskInactivityTimer);
  _kioskInactivityTimer = setTimeout(() => {
    window.location.reload();
  }, _kioskInactivityMs);
}

function _startKioskInactivityTimer(minutes) {
  clearTimeout(_kioskInactivityTimer);
  _kioskInactivityMs = (minutes > 0) ? minutes * 60 * 1000 : 0;
  if (!_kioskInactivityMs) return;
  ['mousemove', 'mousedown', 'keydown', 'touchstart', 'touchmove', 'scroll'].forEach(evt =>
    document.addEventListener(evt, _resetKioskInactivity, { passive: true })
  );
  _resetKioskInactivity();
}

// Bootstrap inactivity timer on page load from server setting
(async () => {
  try {
    const s = await api('/api/settings');
    _startKioskInactivityTimer(parseInt(s.kiosk_inactivity_minutes) || 0);
  } catch {}
})();


// ═══════════════════════════════════════════════════════
// TILL CUSTOMER DETECTION (Phase 1: Purchase Linking)
// ═══════════════════════════════════════════════════════

async function pollActiveCustomer() {
  try {
    const resp = await api('/api/till/active_customer');

    if (resp.customer_id) {
      // Customer detected with name
      if (!STATE.activeCustomer || STATE.activeCustomer.customer_id !== resp.customer_id) {
        STATE.activeCustomer = resp;
        showCustomerBadge(resp.name, resp.customer_number);
      }
    } else {
      // No customer or customer left
      if (STATE.activeCustomer) {
        clearActiveCustomer();
      }
    }
  } catch (e) {
    console.warn('Customer polling error:', e);
  }
}

function showCustomerBadge(name, customer_number) {
  const container = document.getElementById('customer-badge-container');
  if (!container) return;
  const displayName = name || customer_number || 'Unknown customer';

  container.innerHTML = `
    <div class="alert alert-info d-flex align-items-center gap-2 mb-0 py-2 px-3">
      <span class="fw-semibold">${displayName}</span>
      <button class="btn btn-sm btn-outline-secondary py-0 ms-auto" onclick="clearActiveCustomer()"><i class="bi bi-x-lg"></i></button>
    </div>
  `;
}

function clearActiveCustomer() {
  STATE.activeCustomer = null;
  const container = document.getElementById('customer-badge-container');
  if (container) container.innerHTML = '';
}

function startCustomerPolling() {
  if (STATE.customerPollInterval) return; // Already running
  STATE.customerPollInterval = setInterval(pollActiveCustomer, 5000);
  pollActiveCustomer(); // Poll immediately
}

function stopCustomerPolling() {
  if (STATE.customerPollInterval) {
    clearInterval(STATE.customerPollInterval);
    STATE.customerPollInterval = null;
  }
  clearActiveCustomer();
}

// Start/stop polling when teller tab shown/hidden
document.querySelector('[data-bs-target="#teller"]')?.addEventListener('shown.bs.tab', startCustomerPolling);
document.querySelector('[data-bs-target="#teller"]')?.addEventListener('hidden.bs.tab', stopCustomerPolling);
document.querySelector('[data-bs-target="#teller"]')?.addEventListener('shown.bs.tab', () => setTimeout(_focusTrap, 200));

function _sizeTellerScreen() {
  const el = document.getElementById('teller-screen');
  if (!el) return;
  const top = el.getBoundingClientRect().top;
  if (top === 0 && el.offsetHeight === 0) return; // element not yet in layout
  const h = window.innerHeight - top - 8;
  if (h > 100) el.style.height = h + 'px';
}

// Fires sizing on initial load with multiple retries to catch font/layout settling.
// Laptops with different DPI or slow font loads can have the navbar shift height
// after the first rAF fires, leaving the teller screen too tall until next resize.
function _initTellerSize() {
  requestAnimationFrame(() => requestAnimationFrame(_sizeTellerScreen));   // immediate
  setTimeout(_sizeTellerScreen, 100);   // after fonts likely loaded
  setTimeout(_sizeTellerScreen, 500);   // catch any remaining late paints
  document.fonts?.ready?.then(_sizeTellerScreen);  // definitive: fires when all fonts are loaded
}

window.addEventListener('resize', _sizeTellerScreen);
window.addEventListener('load', _sizeTellerScreen);
document.querySelector('[data-bs-target="#teller"]')?.addEventListener('shown.bs.tab', _sizeTellerScreen);

// Watch navbar height with ResizeObserver — re-sizes teller if toolbar reflows
const _navEl = document.querySelector('.navbar') || document.querySelector('nav');
if (_navEl && typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(_sizeTellerScreen).observe(_navEl);
}

// (System Updates removed - deployment is via Docker rebuild, not Windows updater)

// ═══════════════════════════════════════════════════════
// DEVELOPER MONITOR
// ═══════════════════════════════════════════════════════

let _monitorInterval = null;

function _fmtUptime(s) {
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

async function refreshMonitor() {
  try {
    const d = await api('/api/recognition/status');
    const dot = document.getElementById('monitor-status-dot');
    const txt = document.getElementById('monitor-status-text');
    if (dot) dot.style.background = '#22c55e';
    if (txt) txt.textContent = 'live';

    const set = (id, v) => { const el = document.getElementById(id); if(el) el.textContent = v; };
    set('m-cpu',    d.cpu_pct + '%');
    set('m-mem',    Math.round(d.mem_mb) + ' MB');
    set('m-uptime', _fmtUptime(d.uptime_s));
    const onnxEl = document.getElementById('m-onnx'); if(onnxEl) onnxEl.innerHTML = (d.onnx_providers||[]).find(p=>p.includes('OpenVINO')) ? '<i class="bi bi-circle-fill text-success me-1"></i>OpenVINO GPU' : '<i class="bi bi-circle-fill text-warning me-1"></i>CPU';
    set('m-sessions', d.sessions_total);
    set('m-anon',   d.anon_total);
    set('m-queue',  d.clip_queue_depth);
    set('m-cache',  d.customer_cache_size);

    // Sessions
    const sl = document.getElementById('monitor-sessions-list');
    const se = document.getElementById('monitor-sessions-empty');
    if (sl) {
      sl.innerHTML = '';
      if (!d.sessions || d.sessions.length === 0) {
        if(se) { se.classList.remove('hidden'); }
      } else {
        if(se) se.classList.add('hidden');
        d.sessions.forEach(s => {
          const color = s.status==='resolving' ? 'warning' : s.candidate_cid ? 'success' : 'primary';
          const promoPct = Math.round((s.promo_score || 0) * 100);
          const promoColor = promoPct >= 65 ? '#22c55e' : promoPct >= 40 ? '#f59e0b' : '#94a3b8';
          const resolveIn = s.resolve_in_s ?? '?';
          const resolveUrgency = resolveIn <= 10 ? '#f97316' : resolveIn <= 30 ? '#fbbf24' : '#94a3b8';
          const badge = document.createElement('div');
          badge.className = `card border-${color} p-2`;
          badge.style.minWidth = '180px';
          badge.style.maxWidth = '220px';
          badge.innerHTML = `
            <div class="d-flex justify-content-between align-items-center mb-1">
              <span class="small fw-bold text-${color} font-monospace">${s.id}</span>
              <span style="font-size:10px;color:${resolveUrgency};font-weight:700">⏱ ${resolveIn}s</span>
            </div>
            <div class="small text-muted mb-1">
              ${s.faces} faces · ${s.cameras.join('+')||'?'}
              ${s.gait ? ' · <i class="bi bi-person-walking"></i>gait' : ''}
              ${s.clips_pending > 0 ? ` · <i class="bi bi-paperclip"></i>${s.clips_pending} clip${s.clips_pending>1?'s':''}` : ''}
            </div>
            <div class="small text-muted mb-1">age ${s.age_s}s · sim ${Math.round(s.best_sim*100)}%</div>
            <div style="background:#1e293b;border-radius:3px;height:6px;margin-bottom:2px;overflow:hidden">
              <div style="width:${promoPct}%;height:100%;background:${promoColor};transition:width 0.5s"></div>
            </div>
            <div style="font-size:10px;color:${promoColor};font-weight:600">${promoPct}% promo</div>
            ${s.candidate_cid ? `<div class="small text-success mt-1">→ cid=${s.candidate_cid}</div>` : ''}
          `;
          sl.appendChild(badge);
        });
      }
    }

    // Clip queue
    const ql = document.getElementById('monitor-queue-list');
    if (ql) {
      if (!d.clip_queue_depth) {
        ql.innerHTML = '<span class="text-muted">Empty</span>';
      } else {
        ql.innerHTML = (d.clip_queue_items||[]).map(j =>
          `<div>${j.event_id} → cid=${j.customer_id ?? '?'}</div>`
        ).join('') + (d.clip_queue_depth > 10 ? `<div class="text-muted">… +${d.clip_queue_depth-10} more</div>` : '');
      }
    }

    // Anon identities
    const al = document.getElementById('monitor-anon-list');
    const ae = document.getElementById('monitor-anon-empty');
    if (al) {
      al.innerHTML = '';
      if (!d.anon_identities || d.anon_identities.length === 0) {
        if(ae) ae.classList.remove('hidden');
      } else {
        if(ae) ae.classList.add('hidden');
        d.anon_identities.forEach(a => {
          const ttlMin = Math.round((a.ttl_s || 0) / 60);
          const promoPct = Math.round((a.promo_score || 0) * 100);
          const promoColor = promoPct >= 65 ? '#22c55e' : promoPct >= 40 ? '#f59e0b' : '#94a3b8';
          const card = document.createElement('div');
          card.className = 'card border-warning p-2 text-center';
          card.style.minWidth = '130px';
          card.innerHTML = `
            ${a.photo_b64
              ? `<img src="data:image/jpeg;base64,${a.photo_b64}" style="width:60px;height:60px;object-fit:cover;border-radius:50%;margin:0 auto 4px;display:block;border:2px solid #ffc107">`
              : `<div style="width:60px;height:60px;border-radius:50%;background:#fff3cd;display:flex;align-items:center;justify-content:center;font-size:1.4rem;margin:0 auto 4px"><i class="bi bi-person"></i></div>`}
            <div class="small fw-bold text-warning">${a.id}</div>
            <div class="small text-muted">${a.faces} face${a.faces !== 1 ? 's' : ''} · ${a.cameras.join(',')}</div>
            <div class="small text-muted">TTL ${ttlMin}m · seen ${Math.round(a.last_seen_s)}s ago</div>
            <div style="margin:4px 0 2px">
              <div style="background:#374151;border-radius:3px;height:6px;overflow:hidden">
                <div style="width:${promoPct}%;height:100%;background:${promoColor};transition:width 0.3s"></div>
              </div>
              <div style="font-size:10px;color:${promoColor};font-weight:600">${promoPct}% promo</div>
            </div>
          `;
          al.appendChild(card);
        });
      }
    }
  } catch(e) {
    const dot = document.getElementById('monitor-status-dot');
    const txt = document.getElementById('monitor-status-text');
    if (dot) dot.style.background = '#ef4444';
    if (txt) txt.textContent = 'offline: ' + e.message;
  }
}

// ─── Identity Tracks panel ───────────────────────────────────────────────────
const _STATE_COLORS = {
  detected: 'secondary', tracking: 'info', session_active: 'primary',
  building: 'warning', ready: 'success', promoted: 'success',
  grace: 'warning', closed: 'secondary',
};
const _STATE_ICONS = {
  detected:       '<i class="bi bi-eye"></i>',
  tracking:       '<i class="bi bi-search"></i>',
  session_active: '<i class="bi bi-link-45deg"></i>',
  building:       '<i class="bi bi-box-seam"></i>',
  ready:          '<i class="bi bi-star-fill"></i>',
  promoted:       '<i class="bi bi-check-circle-fill"></i>',
  grace:          '<i class="bi bi-hourglass-split"></i>',
  closed:         '<i class="bi bi-lock-fill"></i>',
};

async function refreshIdentityTracks() {
  try {
    const d = await api('/api/recognition/tracks');
    const tracks = d.tracks || [];
    // Only show active tracks - closed ones are just clutter
    const activeTracks = tracks.filter(t => t.state !== 'closed');
    const countEl = document.getElementById('m-tracks-count');
    if (countEl) countEl.textContent = activeTracks.length + (tracks.length > activeTracks.length ? ` (+${tracks.length - activeTracks.length} closed)` : '');

    const byStateEl = document.getElementById('m-tracks-bystate');
    if (byStateEl && d.by_state) {
      byStateEl.innerHTML = Object.entries(d.by_state)
        .filter(([, n]) => n > 0)
        .map(([s, n]) => `<span class="badge bg-${_STATE_COLORS[s]||'secondary'} me-1">${s}: ${n}</span>`)
        .join('');
    }

    const tbody = document.getElementById('monitor-tracks-body');
    const empty = document.getElementById('monitor-tracks-empty');
    if (!tbody) return;

    if (!activeTracks.length) {
      tbody.innerHTML = '';
      empty?.classList.remove('hidden');
      return;
    }
    empty?.classList.add('hidden');

    tbody.innerHTML = activeTracks.map(t => {
      const stab = t.stability || {};
      const stabStr = stab.summary === '✅' ? '<i class="bi bi-check-circle-fill text-success"></i>'
        : `<i class="bi bi-exclamation-triangle text-warning me-1"></i>s:${stab.session_reassignments} a:${stab.anon_reassignments} f:${stab.identity_flips}`;
      const identityStr = t.customer_id
        ? `<span class="text-success fw-bold">cid=${t.customer_id}</span>`
        : (t.anon_id
            ? `<span class="text-warning">${t.anon_id.slice(0,8)}</span>`
            : '<span class="text-muted">-</span>');
      const promoBar = `<div style="width:50px;height:6px;background:#e9ecef;border-radius:3px;display:inline-block;vertical-align:middle">
        <div style="width:${Math.round((t.promotion_score||0)*100)}%;height:100%;background:${(t.promotion_score||0)>=0.65?'#22c55e':'#f59e0b'};border-radius:3px"></div>
      </div> ${Math.round((t.promotion_score||0)*100)}%`;
      const locked = t.locked ? ' <i class="bi bi-lock ms-1"></i>' : '';
      return `<tr>
        <td><span class="badge bg-${_STATE_COLORS[t.state]||'secondary'}">${_STATE_ICONS[t.state]||''} ${t.state}</span>${locked}</td>
        <td class="font-monospace" style="font-size:11px">${t.stable_id.slice(0,8)}</td>
        <td>${t.current_camera || '-'}</td>
        <td class="font-monospace" style="font-size:11px">${t.session_id ? t.session_id.slice(0,8) : '-'}</td>
        <td>${identityStr}</td>
        <td>${Math.round((t.confidence||0)*100)}%</td>
        <td>${promoBar}</td>
        <td>${t.frames_buffered}</td>
        <td>${t.flush_count}</td>
        <td style="white-space:nowrap">${stabStr}</td>
        <td>${t.last_seen_ago}s</td>
      </tr>`;
    }).join('');
  } catch(e) { /* silently fail - don't break main monitor */ }
}

// ─── Identity Event Log panel ────────────────────────────────────────────────
const _EVENT_COLORS = {
  TRACK_CREATED: '#60a5fa', STATE_TRANSITION: '#a3a3a3',
  SESSION_RESUMED: '#34d399', REENTRY_MATCHED: '#34d399',
  CAMERA_HANDOFF: '#fbbf24', TRACK_REBOUND: '#fbbf24',
  ANON_MERGE: '#f97316', ANON_CREATED: '#a78bfa',
  PROMOTED: '#4ade80', GRACE_STARTED: '#fde68a',
  GRACE_EXPIRED: '#9ca3af', BAD_EMBEDDING: '#f87171',
  EVIDENCE_FLUSH: '#818cf8', TRACK_CAP_REACHED: '#f87171',
};

let _identityLogKnown = new Set();
let _identityLogCutoff = 0; // unix seconds - events at or before this ts are suppressed after a clear

async function refreshIdentityLog() {
  try {
    const d = await api('/api/recognition/identity_events');
    const events = d.events || [];
    const logEl = document.getElementById('monitor-identity-log');
    if (!logEl) return;

    const newEvents = events.filter(ev => {
      if (ev.ts <= _identityLogCutoff) return false;
      const key = `${ev.ts}_${ev.event}_${ev.stable_id}`;
      if (_identityLogKnown.has(key)) return false;
      _identityLogKnown.add(key);
      return true;
    });
    if (!newEvents.length) return;

    // Trim known set if it grows too large
    if (_identityLogKnown.size > 2000) _identityLogKnown = new Set([..._identityLogKnown].slice(-1000));


    newEvents.forEach(ev => {
      const color = _EVENT_COLORS[ev.event] || '#d4d4d4';
      const sid = ev.stable_id ? ev.stable_id.slice(0,8) : '        ';
      const detail = ev.detail || (ev.from_state ? `${ev.from_state}→${ev.to_state||''}` : '');
      const extra = ev.sim ? ` sim=${ev.sim}` : (ev.score ? ` score=${ev.score}` : '');
      const line = document.createElement('div');
      line.innerHTML = `<span style="color:#666">${ev.ts_iso||''}</span> `
        + `<span style="color:${color};font-weight:600">${ev.event.padEnd(22)}</span> `
        + `<span style="color:#60a5fa">${sid}</span>`
        + (detail ? ` <span style="color:#d4d4d4">${detail}</span>` : '')
        + (extra  ? ` <span style="color:#fbbf24">${extra}</span>` : '');
      logEl.appendChild(line);
    });
    logEl.scrollTop = logEl.scrollHeight;
  } catch(e) { /* silently fail */ }
}

document.getElementById('btn-clear-identity-log')?.addEventListener('click', () => {
  const logEl = document.getElementById('monitor-identity-log');
  if (logEl) logEl.innerHTML = '<div class="text-muted">Cleared.</div>';
  _identityLogCutoff = Date.now() / 1000; // service uses unix seconds
  _identityLogKnown = new Set();
});

document.querySelector('[data-bs-target="#dev-monitor"]')?.addEventListener('hidden.bs.tab', () => {
  if (_monitorInterval) { clearInterval(_monitorInterval); _monitorInterval = null; }
});

document.getElementById('btn-monitor-refresh')?.addEventListener('click', () => {
  refreshMonitor(); refreshLogs(); refreshIdentityTracks(); refreshIdentityLog();
});


// ═══════════════════════════════════════════════════════
// CHANGE PASSWORD
// ═══════════════════════════════════════════════════════
document.getElementById('btn-change-password')?.addEventListener('click', () => {
  ['cp-current','cp-new','cp-confirm'].forEach(id => { const el=document.getElementById(id); if(el) el.value=''; });
  const err = document.getElementById('cp-error'); if(err) err.classList.add('hidden');
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('changePasswordModal'));
  modal.show();
});

document.getElementById('btn-cp-save')?.addEventListener('click', async () => {
  const cur  = document.getElementById('cp-current')?.value;
  const nw   = document.getElementById('cp-new')?.value;
  const conf = document.getElementById('cp-confirm')?.value;
  const err  = document.getElementById('cp-error');
  const showErr = (msg) => { if(err) { err.textContent=msg; err.classList.remove('hidden'); } };
  if (!cur) return showErr('Current password required');
  if (!nw) return showErr('New password required');
  if (nw !== conf) return showErr('New passwords do not match');
  try {
    await api('/api/users/change_password', { method:'POST', body: JSON.stringify({ current_password:cur, new_password:nw }) });
    bootstrap.Modal.getInstance(document.getElementById('changePasswordModal'))?.hide();
    toast('Password changed', 'success');
  } catch(e) { showErr(e.message); }
});

// ═══════════════════════════════════════════════════════
// MONITOR: LOGS + CONTROLS
// ═══════════════════════════════════════════════════════

async function refreshLogs() {
  const search = document.getElementById('log-search')?.value || '';
  const level  = document.getElementById('log-level')?.value || '';
  const container = document.getElementById('monitor-log-container');
  if (!container) return;
  try {
    const params = new URLSearchParams({ n: 200 });
    if (level)  params.set('level', level);
    if (search) params.set('q', search);
    const d = await api('/api/recognition/logs?' + params);
    const logColors = { ERROR:'#f87171', WARNING:'#fbbf24', INFO:'#86efac', DEBUG:'#94a3b8' };
    container.innerHTML = (d.logs || []).map(r => {
      const color = logColors[r.lvl] || '#94a3b8';
      const lvlBadge = `<span style="color:${color};min-width:60px;display:inline-block">[${r.lvl}]</span>`;
      const escapedMsg = r.msg.replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `<div style="color:#e2e8f0;line-height:1.4"><span style="color:#64748b">${r.ts}</span> ${lvlBadge} ${escapedMsg}</div>`;
    }).join('') || '<div style="color:#64748b">No logs</div>';
    // Always pin to bottom - newest logs at the bottom, always visible
    container.scrollTop = container.scrollHeight;
  } catch(e) {
    container.innerHTML = `<div style="color:#f87171">Error: ${e.message}</div>`;
  }
}

async function monitorControl(action, payload, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  const msg = document.getElementById('monitor-ctrl-msg');
  try {
    const d = await api(`/api/recognition/control/${action}`, { method:'POST', body: JSON.stringify(payload||{}) });
    const text = d.ok ? `<i class="bi bi-check-lg me-1"></i>${action.replace(/_/g,' ')} done` + (d.cleared !== undefined ? ` (${d.cleared} items)` : '') + (d.flushed !== undefined ? ` (${d.flushed} sessions)` : '') : `<i class="bi bi-x-lg me-1"></i>${d.error}`;
    if(msg) { msg.innerHTML = text; msg.style.color = d.ok ? '#22c55e' : '#ef4444'; setTimeout(()=>{if(msg) msg.textContent='';}, 4000); }
    await refreshMonitor();
  } catch(e) {
    if(msg) { msg.innerHTML = '<i class="bi bi-x-lg me-1"></i>' + e.message; msg.style.color='#ef4444'; }
  }
}

document.getElementById('btn-ctrl-clear-queue')?.addEventListener('click',
  () => monitorControl('clear_queue', {}, 'Clear all pending clip analysis jobs?'));
document.getElementById('btn-ctrl-flush-sessions')?.addEventListener('click',
  () => monitorControl('flush_sessions', {}, 'Expire all active sessions? (they will not create customers)'));
document.getElementById('btn-ctrl-clear-anon')?.addEventListener('click',
  () => monitorControl('clear_anon', {}, 'Delete all anonymous identities?'));
document.getElementById('btn-ctrl-sync-cache')?.addEventListener('click',
  () => monitorControl('sync_cache', {}));

document.getElementById('btn-log-refresh')?.addEventListener('click', refreshLogs);
document.getElementById('log-search')?.addEventListener('input', refreshLogs);
document.getElementById('log-level')?.addEventListener('change', refreshLogs);

document.querySelector('[data-bs-target="#dev-monitor"]')?.addEventListener('shown.bs.tab', () => {
  refreshMonitor();
  refreshLogs();
  refreshIdentityTracks();
  refreshIdentityLog();
  if (!_monitorInterval) {
    _monitorInterval = setInterval(() => {
      refreshMonitor(); refreshLogs();
      refreshIdentityTracks(); refreshIdentityLog();
    }, 2000);
  }
});

// ═══════════════════════════════════════════════════════
// INVOICES
// ═══════════════════════════════════════════════════════
let _invoices = [];
let _invLines = [];
let _invNewCustomerMode = false;  // true = showing new customer form

// ── Populate customer dropdown from STATE.customers ──
function _invPopulateCustomers() {
  const sel = document.getElementById('inv-customer-select');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">- Search or select a customer -</option>';
  [...STATE.customers]
    .sort((a, b) => (a.name || 'zzz').localeCompare(b.name || 'zzz'))
    .forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = (c.name || c.customer_number || `#${c.id}`) + (c.phone ? ` · ${c.phone}` : '');
      sel.appendChild(opt);
    });
  if (prev) sel.value = prev;
}

// ── Product search / typeahead ──
let _invSelectedProduct = null;

function _invPopulateProducts() { /* no-op - replaced by live search */ }

function _invUpdateUnitDropdown(p) {
  const unitSel = document.getElementById('inv-product-unit');
  const qtyEl   = document.getElementById('inv-product-qty');
  if (!unitSel) return;
  unitSel.innerHTML = '';
  if (p && p.sold_by_weight) {
    const unitOpts = buildUnitOptions(p.unit_type || 'weight', p.package_size, p.package_unit);
    unitOpts.forEach(o => {
      const el = document.createElement('option');
      el.value = o.value; el.textContent = o.label; el.dataset.conv = o.conv;
      unitSel.appendChild(el);
    });
    const bigUnit = p.unit_type === 'volume' ? 'L' : 'kg';
    if ([...unitSel.options].some(o => o.value === bigUnit)) unitSel.value = bigUnit;
    if (qtyEl) { qtyEl.placeholder = unitSel.value; qtyEl.step = '0.001'; }
  } else {
    const el = document.createElement('option'); el.value = 'unit'; el.textContent = 'unit'; el.dataset.conv = 1;
    unitSel.appendChild(el);
    if (qtyEl) { qtyEl.placeholder = 'Qty'; qtyEl.step = '1'; }
  }
}

// Wire up product search (runs once at page load, reacts to STATE.products at call time)
document.getElementById('inv-product-search')?.addEventListener('input', function() {
  const q         = this.value.trim().toLowerCase();
  const resultsEl = document.getElementById('inv-product-results');
  _invSelectedProduct = null;
  const hiddenSel = document.getElementById('inv-product-select');
  if (hiddenSel) hiddenSel.value = '';
  if (!resultsEl) return;
  resultsEl.innerHTML = '';
  if (!q) { resultsEl.style.display = 'none'; return; }

  const matches = STATE.products
    .filter(p => p.is_for_sale !== false && !p.is_archived &&
      (p.name.toLowerCase().includes(q) || (p.barcode || '').includes(q)))
    .slice(0, 20);

  if (!matches.length) { resultsEl.style.display = 'none'; return; }

  matches.forEach(p => {
    const isByWeight  = p.sold_by_weight;
    const bigUnit     = p.unit_type === 'volume' ? 'L' : 'kg';
    const pricePerBig = isByWeight ? parseFloat(p.price_per_unit || 0) * 1000 : 0;
    const flatPrice   = isByWeight ? 0 : parseFloat(p.price || 0);
    const priceLabel  = isByWeight ? `R${fmt(pricePerBig)}/${bigUnit}` : `R${fmt(flatPrice)}`;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'list-group-item list-group-item-action py-1 px-2';
    btn.style.fontSize = '14px';
    btn.innerHTML = `<span class="fw-semibold">${p.name}</span> <span class="text-muted small ms-1">${priceLabel}</span>`;
    btn.addEventListener('mousedown', e => {
      e.preventDefault();
      _invSelectedProduct = p;
      if (hiddenSel) hiddenSel.value = p.id;
      document.getElementById('inv-product-search').value = p.name;
      resultsEl.style.display = 'none';
      _invUpdateUnitDropdown(p);
      document.getElementById('inv-product-qty')?.focus();
    });
    resultsEl.appendChild(btn);
  });
  resultsEl.style.display = 'block';
});

document.getElementById('inv-product-search')?.addEventListener('blur', () => {
  setTimeout(() => {
    const resultsEl = document.getElementById('inv-product-results');
    if (resultsEl) resultsEl.style.display = 'none';
  }, 150);
});

function _invSetCustomerMode(newMode) {
  _invNewCustomerMode = newMode;
  const picker  = document.getElementById('inv-customer-picker');
  const form    = document.getElementById('inv-new-customer-form');
  const btn     = document.getElementById('btn-inv-new-customer');
  if (newMode) {
    hide(picker); show(form);
    btn.innerHTML = '<i class="bi bi-arrow-left me-1"></i>Back to customer list';
  } else {
    show(picker); hide(form);
    btn.textContent = '+ New customer';
    // clear new-customer fields
    ['inv-customer-name','inv-customer-phone','inv-customer-email','inv-customer-address']
      .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  }
}

document.getElementById('btn-inv-new-customer')?.addEventListener('click', () => {
  _invSetCustomerMode(!_invNewCustomerMode);
});

// Show selected customer details
document.getElementById('inv-customer-select')?.addEventListener('change', e => {
  const preview = document.getElementById('inv-customer-preview');
  const cid = parseInt(e.target.value);
  const c = STATE.customers.find(x => x.id === cid);
  if (c && preview) {
    const parts = [c.phone, c.email].filter(Boolean);
    preview.textContent = parts.length ? parts.join(' · ') : '';
    show(preview);
  } else if (preview) {
    hide(preview);
  }
});

async function loadInvoices() {
  try {
    _invoices = await api('/api/invoices');
    renderInvoicesList();
  } catch(e) { console.error('loadInvoices', e); }
}

function renderInvoicesList() {
  const host = document.getElementById('invoices-list');
  if (!host) return;
  if (!_invoices.length) {
    host.innerHTML = '<div class="text-muted">No invoices yet. Click "+ New Invoice" to create one.</div>';
    return;
  }

  // Apply filters
  const fCustomer  = (document.getElementById('inv-filter-customer')?.value || '').trim().toLowerCase();
  const fStatus    = document.getElementById('inv-filter-status')?.value || '';
  const fDateFrom  = document.getElementById('inv-filter-date-from')?.value || '';
  const fDateTo    = document.getElementById('inv-filter-date-to')?.value || '';
  const fMin       = parseFloat(document.getElementById('inv-filter-min')?.value || '') || null;
  const fMax       = parseFloat(document.getElementById('inv-filter-max')?.value || '') || null;

  const filtered = _invoices.filter(i => {
    if (fCustomer && !(i.customer_name || '').toLowerCase().includes(fCustomer)) return false;
    if (fStatus   && i.status !== fStatus) return false;
    if (fDateFrom && i.created_at && i.created_at.slice(0,10) < fDateFrom) return false;
    if (fDateTo   && i.created_at && i.created_at.slice(0,10) > fDateTo)   return false;
    if (fMin !== null && i.total < fMin) return false;
    if (fMax !== null && i.total > fMax) return false;
    return true;
  });
  const statusBadge = s => ({
    draft:      '<span class="badge bg-secondary">Draft</span>',
    sent:       '<span class="badge bg-primary">Sent</span>',
    paid:       '<span class="badge bg-success">Paid</span>',
    finalised:  '<span class="badge bg-dark">Finalised <i class="bi bi-check-lg ms-1"></i></span>',
  }[s] || `<span class="badge bg-secondary">${s}</span>`);

  if (!filtered.length) {
    host.innerHTML = '<div class="text-muted small">No invoices match the current filters.</div>';
    return;
  }

  host.innerHTML = `
    <table class="table table-sm table-hover">
      <thead class="table-light">
        <tr><th>#</th><th>Date</th><th>Customer</th><th>Total</th><th>Status</th><th></th><th></th><th></th></tr>
      </thead>
      <tbody>
        ${filtered.map(i => `
          <tr style="cursor:pointer" onclick="openInvoiceEditor(${i.id})">
            <td class="fw-semibold">${i.invoice_number}</td>
            <td class="text-muted small">${i.created_at ? new Date(i.created_at).toLocaleDateString() : ''}</td>
            <td>${i.customer_name || '<span class="text-muted">-</span>'}${i.customer_id ? ' <span class="badge" style="font-size:0.6rem;background:#7c3aed;color:#fff" title="Linked to POS customer"><i class="bi bi-link-45deg"></i></span>' : ''}</td>
            <td class="fw-semibold">R${fmt(i.total)}</td>
            <td>${statusBadge(i.status)}</td>
            <td>
              ${i.status === 'finalised'
                ? `<span class="badge bg-dark">Finalised <i class="bi bi-check-lg ms-1"></i></span>`
                : i.sale_id
                  ? `<span class="text-muted small">Stock deducted</span>`
                  : (i.status !== 'draft'
                    ? `<button class="btn btn-success btn-sm" onclick="event.stopPropagation();_invFinaliseFromList(${i.id})">Finalise</button>`
                    : '<span class="text-muted small">-</span>')}
            </td>
            <td><a href="/invoices/${i.id}/print" target="_blank" class="btn btn-outline-secondary btn-sm" onclick="event.stopPropagation()">Print</a></td>
            <td><button class="btn btn-outline-danger btn-sm" onclick="event.stopPropagation();_invDeleteFromList(${i.id}, '${i.invoice_number}')">Delete</button></td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

async function _invDeleteFromList(invId, invNum) {
  if (!confirm(`Delete invoice ${invNum}? This does not affect stock.`)) return;
  try {
    await api(`/api/invoices/${invId}/delete`, { method: 'POST' });
    toast('Invoice deleted', 'warning');
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
}

async function _invFinaliseFromList(invId) {
  if (!confirm('Finalise this invoice? Stock will be deducted from inventory.')) return;
  try {
    await api(`/api/invoices/${invId}/finalise`, { method: 'POST' });
    toast('Invoice finalised - stock deducted', 'success');
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
}

async function _invUndoFromList(invId) {
  if (!confirm('Undo this sale? Stock will be restored to inventory.')) return;
  try {
    await api(`/api/invoices/${invId}/undo`, { method: 'POST' });
    toast('Invoice undone - stock restored. Invoice is now Draft.', 'warning');
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
}

function _invRecalc() {
  const disc = parseFloat(document.getElementById('inv-discount-pct')?.value || 0) || 0;
  const subtotal = _invLines.reduce((s, l) => s + (parseFloat(l.subtotal) || 0), 0);
  const total = disc > 0 ? subtotal * (1 - disc / 100) : subtotal;
  const subEl = document.getElementById('inv-subtotal-display');
  const totEl = document.getElementById('inv-total-display');
  if (subEl) subEl.textContent = `R${fmt(subtotal)}`;
  if (totEl) totEl.textContent = `R${fmt(total)}`;
}

function _renderInvLines() {
  const body = document.getElementById('inv-lines-body');
  if (!body) return;
  body.innerHTML = '';
  _invLines.forEach((line, i) => {
    // Build unit options for weight lines
    let unitCell = '';
    if (line._is_weight && line._unit_type) {
      const unitOpts = buildUnitOptions(line._unit_type, line._pkg_size, line._pkg_unit);
      const optHtml = unitOpts.map(o =>
        `<option value="${o.value}" data-conv="${o.conv}"${o.value === line.unit ? ' selected' : ''}>${o.label}</option>`
      ).join('');
      unitCell = `<td><select class="form-select form-select-sm" data-inv-unit="${i}">${optHtml}</select></td>`;
    } else {
      unitCell = `<td class="align-middle text-muted small">${line.unit || 'unit'}</td>`;
    }

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input class="form-control form-control-sm" value="${line.name || ''}" data-inv-name="${i}"></td>
      <td><input type="number" step="any" min="0.001" class="form-control form-control-sm" value="${line.qty || 1}" data-inv-qty="${i}"></td>
      ${unitCell}
      <td><div class="input-group input-group-sm"><span class="input-group-text">R</span><input type="number" step="0.0001" min="0" class="form-control" value="${line.unit_price != null ? +parseFloat(line.unit_price).toFixed(4) : ''}" data-inv-price="${i}"></div></td>
      <td class="text-end align-middle fw-semibold" id="inv-line-sub-${i}">R${fmt(line.subtotal || 0)}</td>
      <td><button class="btn btn-outline-danger btn-sm" data-inv-remove="${i}"><i class="bi bi-x-lg"></i></button></td>`;
    body.appendChild(tr);

    tr.querySelector(`[data-inv-name="${i}"]`).addEventListener('input', e => { _invLines[i].name = e.target.value; });

    tr.querySelector(`[data-inv-qty="${i}"]`).addEventListener('input', e => {
      const qty = parseFloat(e.target.value) || 0;
      _invLines[i].qty = qty;
      if (_invLines[i]._is_weight && _invLines[i]._price_per_base != null) {
        const unitEl = tr.querySelector(`[data-inv-unit="${i}"]`);
        const conv   = parseFloat(unitEl?.options[unitEl?.selectedIndex]?.dataset?.conv || 1);
        _invLines[i].unit_price = _invLines[i]._price_per_base * conv;
        _invLines[i].subtotal   = _invLines[i]._price_per_base * qty * conv;
        const priceEl = tr.querySelector(`[data-inv-price="${i}"]`);
        if (priceEl) priceEl.value = +_invLines[i].unit_price.toFixed(4);
      } else {
        _invLines[i].subtotal = qty * (_invLines[i].unit_price || 0);
      }
      const sub = document.getElementById(`inv-line-sub-${i}`); if (sub) sub.textContent = `R${fmt(_invLines[i].subtotal)}`;
      _invRecalc();
    });

    // Unit dropdown change - recalc price per selected unit and subtotal
    const unitSel = tr.querySelector(`[data-inv-unit="${i}"]`);
    if (unitSel) {
      unitSel.addEventListener('change', e => {
        const conv = parseFloat(e.target.options[e.target.selectedIndex]?.dataset?.conv || 1);
        _invLines[i].unit      = e.target.value;
        if (_invLines[i]._price_per_base != null) {
          _invLines[i].unit_price = _invLines[i]._price_per_base * conv;
          _invLines[i].subtotal   = _invLines[i]._price_per_base * (_invLines[i].qty || 1) * conv;
          const priceEl = tr.querySelector(`[data-inv-price="${i}"]`);
          if (priceEl) priceEl.value = +_invLines[i].unit_price.toFixed(4);
          const sub = document.getElementById(`inv-line-sub-${i}`); if (sub) sub.textContent = `R${fmt(_invLines[i].subtotal)}`;
          _invRecalc();
        }
      });
    }

    tr.querySelector(`[data-inv-price="${i}"]`).addEventListener('input', e => {
      _invLines[i].unit_price = parseFloat(e.target.value) || 0;
      _invLines[i].subtotal   = (_invLines[i].qty || 1) * _invLines[i].unit_price;
      const sub = document.getElementById(`inv-line-sub-${i}`); if (sub) sub.textContent = `R${fmt(_invLines[i].subtotal)}`;
      _invRecalc();
    });

    tr.querySelector(`[data-inv-remove="${i}"]`).addEventListener('click', () => {
      _invLines.splice(i, 1); _renderInvLines(); _invRecalc();
    });
  });
  _invRecalc();
}

function openInvoiceEditor(invId) {
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('invoiceEditorModal'));
  document.getElementById('inv-id').value = invId || '';
  document.getElementById('invoiceEditorTitle').textContent = invId ? 'Edit Invoice' : 'New Invoice';
  const printBtn    = document.getElementById('btn-inv-print');
  const delBtn      = document.getElementById('btn-inv-delete');
  const finaliseBtn = document.getElementById('btn-inv-finalise');
  const undoBtn     = document.getElementById('btn-inv-undo');
  [finaliseBtn, undoBtn].forEach(b => b && hide(b));

  // Populate dropdowns fresh
  _invPopulateCustomers();
  _invPopulateProducts();

  if (invId) {
    if (printBtn) { printBtn.disabled = false; printBtn.onclick = () => window.open(`/invoices/${invId}/print`, '_blank'); }
    if (delBtn) show(delBtn);
    api(`/api/invoices/${invId}`).then(inv => {
      document.getElementById('inv-due-date').value        = inv.due_date || '';
      document.getElementById('inv-notes').value           = inv.notes || '';
      document.getElementById('inv-bank-details').value    = inv.bank_details || '';
      document.getElementById('inv-discount-pct').value    = inv.discount_pct || '';
      document.getElementById('inv-status').value          = inv.status || 'draft';
      // Show finalise/undo based on state
      const statusSel  = document.getElementById('inv-status');
      const addLineBtn = document.getElementById('btn-inv-add-line');
      if (inv.sale_id && inv.status === 'finalised') {
        // Fully finalised - lock everything; undo is the only action
        if (statusSel)  statusSel.disabled  = true;
        if (addLineBtn) addLineBtn.disabled = true;
        if (finaliseBtn) hide(finaliseBtn);
        if (undoBtn) show(undoBtn);
      } else if (inv.sale_id) {
        // Stock deducted but status is paid/sent - allow status change, lock line items
        if (statusSel)  statusSel.disabled  = false;
        if (addLineBtn) addLineBtn.disabled = true;
        if (finaliseBtn) hide(finaliseBtn);
        if (undoBtn) show(undoBtn);
      } else {
        // No sale yet - normal editable state
        if (statusSel)  statusSel.disabled  = false;
        if (addLineBtn) addLineBtn.disabled = false;
        if (undoBtn) hide(undoBtn);
      }
      if (!inv.sale_id && inv.status !== 'draft') {
        // Ready to finalise (will deduct stock)
        if (finaliseBtn) { finaliseBtn.disabled = false; finaliseBtn.textContent = 'Finalise Sale'; finaliseBtn.className = 'btn btn-success btn-sm'; show(finaliseBtn); }
      }
      // Try to match customer by name to existing customer
      const matchedCust = STATE.customers.find(c => c.name === inv.customer_name);
      if (matchedCust) {
        _invSetCustomerMode(false);
        const sel = document.getElementById('inv-customer-select');
        if (sel) sel.value = matchedCust.id;
        sel?.dispatchEvent(new Event('change'));
      } else if (inv.customer_name) {
        _invSetCustomerMode(true);
        document.getElementById('inv-customer-name').value    = inv.customer_name || '';
        document.getElementById('inv-customer-phone').value   = inv.customer_phone || '';
        document.getElementById('inv-customer-email').value   = inv.customer_email || '';
        document.getElementById('inv-customer-address').value = inv.customer_address || '';
      } else {
        _invSetCustomerMode(false);
      }
      _invLines = (inv.lines || []).map(l => ({ ...l }));
      _renderInvLines();
    }).catch(e => toast(e.message, 'error'));
  } else {
    _invSetCustomerMode(false);
    const sel = document.getElementById('inv-customer-select'); if (sel) sel.value = '';
    const preview = document.getElementById('inv-customer-preview'); if (preview) hide(preview);
    ['inv-due-date','inv-notes','inv-discount-pct'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    _invLoadBankDetails();
    _invSelectedProduct = null;
    const searchEl = document.getElementById('inv-product-search'); if (searchEl) searchEl.value = '';
    _invUpdateUnitDropdown(null);
    document.getElementById('inv-status').value = 'draft';
    if (printBtn) printBtn.disabled = true;
    if (delBtn) hide(delBtn);
    _invLines = [];
    _renderInvLines();
  }
  modal.show();
}

document.getElementById('btn-new-invoice')?.addEventListener('click', () => openInvoiceEditor(null));

// Unit dropdown is now updated by _invUpdateUnitDropdown() when a product is selected via search

document.getElementById('btn-inv-add-line')?.addEventListener('click', () => {
  _invLines.push({ name: '', qty: 1, unit_price: 0, subtotal: 0 });
  _renderInvLines();
});

document.getElementById('btn-inv-add-product')?.addEventListener('click', () => {
  const p = _invSelectedProduct;
  if (!p) return toast('Search and select a product first', 'warning');

  const unitSel    = document.getElementById('inv-product-unit');
  const qtyDisplay = parseFloat(document.getElementById('inv-product-qty')?.value || 1) || 1;
  const isByWeight = p.sold_by_weight;
  const unitVal    = unitSel?.value || 'unit';
  const conv       = parseFloat(unitSel?.options[unitSel?.selectedIndex]?.dataset?.conv || 1);

  let name, unitPrice, subtotal;
  if (isByWeight) {
    const pricePerBase = parseFloat(p.price_per_unit || 0);
    unitPrice = pricePerBase * conv;
    subtotal  = pricePerBase * qtyDisplay * conv;
    name      = p.name;
  } else {
    unitPrice = parseFloat(p.price || 0);
    subtotal  = qtyDisplay * unitPrice;
    name      = p.name;
  }

  _invLines.push({
    name,
    product_id:      p.id,
    qty:             qtyDisplay,
    unit:            unitVal,
    unit_price:      parseFloat(unitPrice.toFixed(4)),
    subtotal:        parseFloat(subtotal.toFixed(2)),
    _price_per_base: isByWeight ? parseFloat(p.price_per_unit || 0) : null,
    _unit_type:      isByWeight ? (p.unit_type || 'weight') : null,
    _pkg_size:       p.package_size || null,
    _pkg_unit:       p.package_unit || null,
    _is_weight:      isByWeight,
  });
  _renderInvLines();
  // Reset picker
  _invSelectedProduct = null;
  const searchEl = document.getElementById('inv-product-search'); if (searchEl) searchEl.value = '';
  const hiddenSel = document.getElementById('inv-product-select'); if (hiddenSel) hiddenSel.value = '';
  const qtyEl = document.getElementById('inv-product-qty'); if (qtyEl) qtyEl.value = '1';
  _invUpdateUnitDropdown(null);
});

document.getElementById('inv-discount-pct')?.addEventListener('input', _invRecalc);

document.getElementById('btn-inv-save')?.addEventListener('click', async () => {
  const invId = document.getElementById('inv-id').value;

  // Resolve customer details
  let custName = null, custPhone = null, custEmail = null, custAddress = null;
  if (_invNewCustomerMode) {
    custName    = document.getElementById('inv-customer-name')?.value.trim() || null;
    custPhone   = document.getElementById('inv-customer-phone')?.value.trim() || null;
    custEmail   = document.getElementById('inv-customer-email')?.value.trim() || null;
    custAddress = document.getElementById('inv-customer-address')?.value.trim() || null;
    if (!custName) return toast('Enter a customer name', 'warning');
    // Create the customer in the system so they can be merged later
    try {
      const res = await api('/api/customers', { method: 'POST', body: JSON.stringify({
        name: custName, phone: custPhone, email: custEmail, notes: custAddress ? `Address: ${custAddress}` : null
      })});
      // Switch to picker mode pointing at new customer
      if (res?.id) {
        STATE.customers.push({ id: res.id, name: custName, phone: custPhone, email: custEmail, customer_number: res.customer_number, plates: [], has_face: false, has_gait: false, visit_count: 0 });
        _invSetCustomerMode(false);
        _invPopulateCustomers();
        const sel = document.getElementById('inv-customer-select');
        if (sel) sel.value = res.id;
        toast(`Customer "${custName}" added`, 'success', 2000);
      }
    } catch(e) {
      if (!e.message?.includes('exists')) return toast(e.message, 'error');
    }
  } else {
    const sel = document.getElementById('inv-customer-select');
    const cid = parseInt(sel?.value || 0);
    const c = STATE.customers.find(x => x.id === cid);
    if (c) {
      custName    = c.name;
      custPhone   = c.phone;
      custEmail   = c.email;
    }
  }

  if (!_invLines.length) return toast('Add at least one item', 'warning');

  const payload = {
    customer_name:    custName,
    customer_phone:   custPhone,
    customer_email:   custEmail,
    customer_address: custAddress,
    due_date:         document.getElementById('inv-due-date').value || null,
    notes:            document.getElementById('inv-notes').value.trim() || null,
    bank_details:     document.getElementById('inv-bank-details').value.trim() || null,
    discount_pct:     parseFloat(document.getElementById('inv-discount-pct').value || 0) || null,
    status:           document.getElementById('inv-status').value,
    lines: _invLines.map(l => ({
      name: l.name, qty: parseFloat(l.qty) || 1,
      unit_price: parseFloat(l.unit_price) || 0,
      subtotal: parseFloat(l.subtotal) || 0,
    })),
  };

  try {
    let id = invId ? parseInt(invId) : null;
    if (id) {
      await api(`/api/invoices/${id}`, { method: 'POST', body: JSON.stringify(payload) });
      toast('Invoice updated', 'success');
    } else {
      const res = await api('/api/invoices', { method: 'POST', body: JSON.stringify(payload) });
      id = res.id;
      toast(`Invoice ${res.invoice_number} created`, 'success');
      document.getElementById('inv-id').value = id;
      document.getElementById('invoiceEditorTitle').textContent = 'Edit Invoice';
      const printBtn = document.getElementById('btn-inv-print');
      if (printBtn) { printBtn.disabled = false; printBtn.onclick = () => window.open(`/invoices/${id}/print`, '_blank'); }
      show(document.getElementById('btn-inv-delete'));
    }
    bootstrap.Modal.getInstance(document.getElementById('invoiceEditorModal'))?.hide();
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
});

// Prevent changing status away from finalised without undoing the sale
document.getElementById('inv-status')?.addEventListener('change', e => {
  const invId = document.getElementById('inv-id').value;
  if (!invId) return;
  const inv = _invoices.find(i => i.id === parseInt(invId));
  if (inv?.sale_id && e.target.value !== 'finalised') {
    e.target.value = 'finalised';
    toast('Undo the sale first before changing the status', 'warning');
  }
});

document.getElementById('btn-inv-finalise')?.addEventListener('click', async () => {
  const invId = document.getElementById('inv-id').value;
  if (!invId) return;
  if (!confirm('Finalise this invoice? Stock will be deducted from inventory.')) return;
  try {
    await api(`/api/invoices/${invId}/finalise`, { method: 'POST' });
    toast('Invoice finalised - stock deducted', 'success');
    bootstrap.Modal.getInstance(document.getElementById('invoiceEditorModal'))?.hide();
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-inv-undo')?.addEventListener('click', async () => {
  const invId = document.getElementById('inv-id').value;
  if (!invId) return;
  if (!confirm('Undo this invoice?\n\nStock will be restored to inventory.\nThe invoice will return to Draft so it can be edited and re-finalised.')) return;
  try {
    await api(`/api/invoices/${invId}/undo`, { method: 'POST' });
    toast('Invoice undone - stock restored. Invoice is now Draft.', 'warning');
    bootstrap.Modal.getInstance(document.getElementById('invoiceEditorModal'))?.hide();
    await loadInvoices();
  } catch(e) { toast(e.message, 'error'); }
});

document.getElementById('btn-inv-delete')?.addEventListener('click', async () => {
  const invId = document.getElementById('inv-id').value;
  if (!invId || !confirm('Delete this invoice?')) return;
  try {
    await api(`/api/invoices/${invId}/delete`, { method: 'POST' });
    bootstrap.Modal.getInstance(document.getElementById('invoiceEditorModal'))?.hide();
    await loadInvoices();
    toast('Invoice deleted', 'warning');
  } catch(e) { toast(e.message, 'error'); }
});

// ── Bank details: auto-load from settings, auto-save on blur ──
async function _invLoadBankDetails() {
  try {
    const s = await api('/api/settings');
    const val = s.invoice_bank_details || '';
    const el = document.getElementById('inv-bank-details');
    if (el && val) el.value = val;
  } catch {}
}

document.getElementById('inv-bank-details')?.addEventListener('blur', async () => {
  const val = document.getElementById('inv-bank-details')?.value || '';
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify({ invoice_bank_details: val }) });
  } catch {}
});

['inv-filter-customer','inv-filter-status','inv-filter-date-from','inv-filter-date-to','inv-filter-min','inv-filter-max']
  .forEach(id => document.getElementById(id)?.addEventListener('input', renderInvoicesList));
document.getElementById('inv-filter-status')?.addEventListener('change', renderInvoicesList);

document.getElementById('btn-inv-filter-clear')?.addEventListener('click', () => {
  ['inv-filter-customer','inv-filter-date-from','inv-filter-date-to','inv-filter-min','inv-filter-max']
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const statusEl = document.getElementById('inv-filter-status'); if (statusEl) statusEl.value = '';
  renderInvoicesList();
});

document.querySelector('[data-bs-target="#invoices"]')?.addEventListener('shown.bs.tab', async () => {
  if (!STATE.customers.length) await loadCustomers();
  await loadInvoices();
});

// ── Shipping fees editor (online shop delivery pricing) ──
async function loadShippingFees() {
  const rowsEl = document.getElementById('shipping-fees-rows');
  const errEl  = document.getElementById('shipping-fees-error');
  if (!rowsEl) return;
  errEl?.classList.add('d-none');
  rowsEl.innerHTML = '<div class="text-muted small">Loading…</div>';
  try {
    const resp = await api('/api/invoices/shipping-fees');
    const fees = (resp && resp.fees) || [];
    if (!fees.length) throw new Error('No shipping fees returned by the server.');
    rowsEl.innerHTML = '';
    fees.forEach(f => {
      const row = document.createElement('div');
      row.className = 'mb-3';
      row.innerHTML = `
        <label class="form-label mb-1 fw-semibold">${f.label}</label>
        <div class="input-group">
          <span class="input-group-text">R</span>
          <input type="number" inputmode="decimal" min="0" step="0.01"
                 class="form-control shipping-fee-input"
                 data-method="${f.method}" value="${Number(f.fee).toFixed(2)}">
        </div>`;
      rowsEl.appendChild(row);
    });
  } catch (e) {
    rowsEl.innerHTML = '';
    if (errEl) {
      errEl.textContent = 'Could not load shipping fees: ' + (e.message || 'unknown error');
      errEl.classList.remove('d-none');
    }
  }
}

// Populate whenever the modal opens (Bootstrap event - robust on mobile, no click race)
document.getElementById('shippingFeesModal')?.addEventListener('shown.bs.modal', loadShippingFees);

document.getElementById('btn-save-shipping-fees')?.addEventListener('click', async () => {
  const btn   = document.getElementById('btn-save-shipping-fees');
  const errEl = document.getElementById('shipping-fees-error');
  errEl?.classList.add('d-none');
  const fees = {};
  document.querySelectorAll('.shipping-fee-input').forEach(inp => {
    const v = parseFloat(inp.value);
    if (!isNaN(v) && v >= 0) fees[inp.dataset.method] = v;
  });
  btn.disabled = true;
  try {
    await api('/api/invoices/shipping-fees', { method: 'POST', body: JSON.stringify({ fees }) });
    toast('Shipping fees saved', 'success');
    bootstrap.Modal.getInstance(document.getElementById('shippingFeesModal'))?.hide();
  } catch (e) {
    if (errEl) { errEl.textContent = e.message || 'Could not save shipping fees.'; errEl.classList.remove('d-none'); }
  } finally {
    btn.disabled = false;
  }
});

// ═══════════════════════════════════════════════════════
// LABEL PRINTING SUBSYSTEM
// ═══════════════════════════════════════════════════════

const LABELS = {
  templates: [],
  printers:  [],
  _currentProduct: null,
  _currentTemplateId: null,
  _designer: {
    templateId: null,
    elements: [],
    selectedIdx: null,
    widthMm: 40,
    heightMm: 20,
    PX_PER_MM: 4,   // canvas display scale (4px = 1mm)
  },
};

// ── Data loaders ──────────────────────────────────────────────────────────────

async function loadLabelTemplates() {
  try {
    LABELS.templates = await api('/api/label-templates');
  } catch (e) { LABELS.templates = []; }
}

async function loadLabelPrinters() {
  try {
    LABELS.printers = await api('/api/label-printers');
  } catch (e) { LABELS.printers = []; }
}

function _populateTemplateSelect(selectEl, selectedId) {
  if (!selectEl) return;
  selectEl.innerHTML = '';
  if (!LABELS.templates.length) {
    selectEl.innerHTML = '<option value="">No templates — create one</option>';
    return;
  }
  const cats = { small_barcode: 'Small Barcode', shelf: 'Shelf', sticker: 'Sticker', price_tag: 'Price Tag', custom: 'Custom' };
  const grouped = {};
  LABELS.templates.forEach(t => {
    (grouped[t.category] = grouped[t.category] || []).push(t);
  });
  Object.entries(grouped).forEach(([cat, tmpls]) => {
    const grp = document.createElement('optgroup');
    grp.label = cats[cat] || cat;
    tmpls.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id; opt.textContent = `${t.name} (${t.width_mm}×${t.height_mm}mm)`;
      if (String(t.id) === String(selectedId)) opt.selected = true;
      grp.appendChild(opt);
    });
    selectEl.appendChild(grp);
  });
}

function _populatePrinterSelect(selectEl) {
  if (!selectEl) return;
  selectEl.innerHTML = '<option value="">Default / Browser Print</option>';
  LABELS.printers.forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id; opt.textContent = `${p.name} (${p.connection})`;
    selectEl.appendChild(opt);
  });
}

// ── Single-product print modal ────────────────────────────────────────────────

async function openLabelPrintModal(product) {
  LABELS._currentProduct = product;
  await loadLabelTemplates();
  await loadLabelPrinters();
  document.getElementById('lp-product-name').textContent = product.name;
  document.getElementById('lp-qty').value = 1;
  document.getElementById('lp-status').textContent = '';
  _populateTemplateSelect(document.getElementById('lp-template-select'), LABELS._currentTemplateId);
  _populatePrinterSelect(document.getElementById('lp-printer-select'));
  // Auto-preview on template change
  document.getElementById('lp-template-select').onchange = refreshLabelPreview;
  bootstrap.Modal.getOrCreateInstance(document.getElementById('labelPrintModal')).show();
  if (LABELS.templates.length) refreshLabelPreview();
}

async function refreshLabelPreview() {
  const tmplId    = document.getElementById('lp-template-select')?.value;
  const productId = LABELS._currentProduct?.id;
  const img       = document.getElementById('lp-preview-img');
  const ph        = document.getElementById('lp-preview-placeholder');
  if (!tmplId) { if (img) img.style.display = 'none'; if (ph) ph.style.display = ''; return; }
  LABELS._currentTemplateId = tmplId;
  try {
    const res = await fetch('/api/labels/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id: productId, template_id: parseInt(tmplId) }),
    });
    if (!res.ok) throw new Error(await res.text());
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    if (img) {
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      img.src = url; img.style.display = '';
    }
    if (ph)  ph.style.display = 'none';
  } catch (e) {
    if (ph) { ph.textContent = `Preview failed: ${e.message}`; ph.style.display = ''; }
    if (img) img.style.display = 'none';
  }
}

function adjLabelQty(delta) {
  const el = document.getElementById('lp-qty');
  if (!el) return;
  el.value = Math.max(1, Math.min(500, (parseInt(el.value) || 1) + delta));
}
function setLabelQty(n) { const el = document.getElementById('lp-qty'); if (el) el.value = n; }

async function executeLabelPrint() {
  const tmplId    = parseInt(document.getElementById('lp-template-select')?.value);
  const qty       = parseInt(document.getElementById('lp-qty')?.value) || 1;
  const printerId = document.getElementById('lp-printer-select')?.value || null;
  const statusEl  = document.getElementById('lp-status');
  const productId = LABELS._currentProduct?.id;
  if (!tmplId)    { toast('Select a template first', 'warning'); return; }
  if (!productId) { toast('No product selected', 'warning');     return; }
  const btn = document.getElementById('lp-print-btn');
  btn.disabled = true; if (statusEl) statusEl.textContent = 'Sending…';
  try {
    const result = await api('/api/labels/print', {
      method: 'POST', body: JSON.stringify({ product_id: productId, template_id: tmplId, qty, printer_id: printerId }),
    });
    toast(`${qty} label${qty > 1 ? 's' : ''} sent to printer`, 'success');
    if (statusEl) statusEl.innerHTML = `<i class="bi bi-check-lg me-1"></i>Job #${result.job_id} — ${qty} label${qty > 1 ? 's' : ''}`;
    bootstrap.Modal.getInstance(document.getElementById('labelPrintModal'))?.hide();
  } catch (e) {
    toast(`Print failed: ${e.message}`, 'error');
    if (statusEl) statusEl.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ── Bulk print modal ──────────────────────────────────────────────────────────

async function openBulkLabelModal(preselectIds) {
  await loadLabelTemplates();
  await loadLabelPrinters();
  _populateTemplateSelect(document.getElementById('bulk-template-select'), LABELS._currentTemplateId);
  _populatePrinterSelect(document.getElementById('bulk-printer-select'));
  document.getElementById('bulk-status').textContent = '';
  document.getElementById('bulk-filter').value = '';
  const allProducts = STATE.products.filter(p => !p.is_archived);
  // If called from multi-select toolbar, show only those products (all pre-checked)
  const showProducts = preselectIds?.length
    ? allProducts.filter(p => preselectIds.includes(p.id))
    : allProducts;
  renderBulkProductList(showProducts, !!preselectIds?.length);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkLabelModal')).show();
}

function renderBulkProductList(products, allChecked = false) {
  const wrap = document.getElementById('bulk-product-list');
  if (!wrap) return;
  if (!products.length) { wrap.innerHTML = '<div class="text-muted small">No products.</div>'; return; }
  wrap.innerHTML = products.map(p => `
    <div class="d-flex align-items-center gap-2 py-1 border-bottom bulk-product-row" data-product-id="${p.id}">
      <input type="checkbox" class="form-check-input bulk-chk" data-pid="${p.id}" ${allChecked ? 'checked' : ''}>
      <span style="min-width:0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
      <span class="text-muted small me-1">${p.price != null ? 'R' + fmt(p.price) : ''}</span>
      <select class="form-select form-select-sm bulk-row-template" style="width:160px" data-pid="${p.id}"></select>
      <input type="number" class="form-control form-control-sm bulk-row-qty" data-pid="${p.id}" value="1" min="1" max="500" style="width:60px">
    </div>
  `).join('');
  // Populate per-row template selects
  wrap.querySelectorAll('.bulk-row-template').forEach(sel => {
    _populateTemplateSelect(sel, LABELS._currentTemplateId);
  });
  _updateBulkCount();
  wrap.querySelectorAll('.bulk-chk').forEach(chk => chk.addEventListener('change', _updateBulkCount));
}

function filterBulkProducts() {
  const q = (document.getElementById('bulk-filter')?.value || '').toLowerCase();
  const prods = STATE.products.filter(p => !p.is_archived && (!q || p.name.toLowerCase().includes(q)));
  renderBulkProductList(prods);
}

function bulkSelectAll() {
  document.querySelectorAll('.bulk-chk').forEach(c => c.checked = true);
  _updateBulkCount();
}
function bulkSelectNone() {
  document.querySelectorAll('.bulk-chk').forEach(c => c.checked = false);
  _updateBulkCount();
}
function _updateBulkCount() {
  const n = document.querySelectorAll('.bulk-chk:checked').length;
  const el = document.getElementById('bulk-selected-count');
  if (el) el.textContent = `${n} selected`;
}

function applyBulkDefaults() {
  const tmplId = document.getElementById('bulk-template-select')?.value;
  const qty    = document.getElementById('bulk-default-qty')?.value || 1;
  document.querySelectorAll('.bulk-row-template').forEach(sel => { if (tmplId) sel.value = tmplId; });
  document.querySelectorAll('.bulk-row-qty').forEach(inp => inp.value = qty);
}

async function executeBulkPrint() {
  const printerId = document.getElementById('bulk-printer-select')?.value || null;
  const statusEl  = document.getElementById('bulk-status');
  const items = [];
  document.querySelectorAll('.bulk-chk:checked').forEach(chk => {
    const pid    = chk.dataset.pid;
    const row    = chk.closest('.bulk-product-row');
    const tmplId = row?.querySelector('.bulk-row-template')?.value;
    const qty    = parseInt(row?.querySelector('.bulk-row-qty')?.value) || 1;
    if (pid && tmplId) items.push({ product_id: parseInt(pid), template_id: parseInt(tmplId), qty });
  });
  if (!items.length) { toast('Select at least one product with a template', 'warning'); return; }
  if (statusEl) statusEl.textContent = `Rendering ${items.length} product(s)…`;
  try {
    const result = await api('/api/labels/print-bulk', {
      method: 'POST', body: JSON.stringify({ items, printer_id: printerId }),
    });
    toast(`${result.pages} label${result.pages > 1 ? 's' : ''} sent to printer for ${items.length} product(s)`, 'success');
    bootstrap.Modal.getInstance(document.getElementById('bulkLabelModal'))?.hide();
  } catch (e) {
    toast(`Bulk print failed: ${e.message}`, 'error');
    if (statusEl) statusEl.textContent = `Error: ${e.message}`;
  }
}

// ── Browser PDF print (USB / Bluetooth path) ─────────────────────────────────

function _browserPrintPdf(b64) {
  const bytes  = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const blob   = new Blob([bytes], { type: 'application/pdf' });
  const url    = URL.createObjectURL(blob);
  const iframe = document.createElement('iframe');
  iframe.style.cssText = 'position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;border:0';
  document.body.appendChild(iframe);
  iframe.src = url;
  iframe.onload = () => {
    try { iframe.contentWindow.print(); } catch (e) { window.open(url, '_blank'); }
    setTimeout(() => { document.body.removeChild(iframe); URL.revokeObjectURL(url); }, 60000);
  };
}

// ═══════════════════════════════════════════════════════
// LABEL DESIGNER  — WYSIWYG (server-rendered PNG + overlay handles)
// ═══════════════════════════════════════════════════════
//
// Architecture:
//   ld-canvas-img    = the rendered PNG from /api/labels/preview
//   ld-canvas-overlay= transparent div on top; holds drag/resize handles
//
// All positions/sizes are stored in mm (LD.elements[].x/y/w/h).
// LD.scale = CSS pixels per mm at current zoom — derived from canvas size.
// On every edit we call ldRequestRender() which debounces → server PNG → img.src.
// Overlay handles are redrawn after each render.

const LD = LABELS._designer;

// ── Designer scale: CSS px per mm. Computed from canvas container width on open.
LD.scale = 6;   // default fallback; recalculated in ldComputeScale()

// Render is always server-side. This is the debounce timer.
let _ldRenderTimer = null;

// ── Open ─────────────────────────────────────────────────────────────────────

async function openLabelDesigner(existingTemplateId) {
  await loadLabelTemplates();
  _populateDesignerLoadSelect(existingTemplateId ? parseInt(existingTemplateId) : null);

  // Populate product selector
  const prodSel = document.getElementById('ld-preview-product');
  if (prodSel) {
    prodSel.innerHTML = '<option value="">— no product —</option>';
    STATE.products.filter(p => !p.is_archived && p.is_for_sale).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id; opt.textContent = p.name;
      prodSel.appendChild(opt);
    });
    if (LABELS._currentProduct) prodSel.value = LABELS._currentProduct.id;
    prodSel.onchange = ldRequestRender;
  }

  if (existingTemplateId) {
    const tmpl = LABELS.templates.find(t => t.id === parseInt(existingTemplateId));
    if (tmpl) {
      LD.templateId = tmpl.id;
      LD.elements   = JSON.parse(JSON.stringify(tmpl.elements));
      LD.widthMm    = tmpl.width_mm;
      LD.heightMm   = tmpl.height_mm;
      document.getElementById('ld-template-name').value = tmpl.name;
      document.getElementById('ld-category').value      = tmpl.category;
      document.getElementById('ld-width').value         = tmpl.width_mm;
      document.getElementById('ld-height').value        = tmpl.height_mm;
      document.getElementById('ld-border').checked      = tmpl.border;
      document.getElementById('ld-bg-color').value      = tmpl.background_color || '#ffffff';
      document.getElementById('ld-delete-tmpl-btn').style.display = '';
    }
  } else {
    LD.templateId = null; LD.elements = []; LD.selectedIdx = null;
    LD.widthMm = 40; LD.heightMm = 20;
    document.getElementById('ld-template-name').value = 'New Template';
    document.getElementById('ld-category').value      = 'custom';
    document.getElementById('ld-width').value         = 40;
    document.getElementById('ld-height').value        = 20;
    document.getElementById('ld-border').checked      = false;
    document.getElementById('ld-bg-color').value      = '#ffffff';
    document.getElementById('ld-delete-tmpl-btn').style.display = 'none';
  }

  ['ld-width','ld-height'].forEach(id => {
    document.getElementById(id).oninput = () => {
      LD.widthMm  = parseFloat(document.getElementById('ld-width').value)  || LD.widthMm;
      LD.heightMm = parseFloat(document.getElementById('ld-height').value) || LD.heightMm;
      ldRequestRender();
    };
  });
  document.getElementById('ld-bg-color').oninput = ldRequestRender;
  document.getElementById('ld-border').onchange  = ldRequestRender;

  document.getElementById('labelDesignerModal').onkeydown = e => {
    if ((e.key === 'Delete' || e.key === 'Backspace') &&
        (e.target === document.getElementById('labelDesignerModal') ||
         e.target.id === 'ld-canvas-wrap' || e.target.id === 'ld-canvas-overlay')) {
      if (LD.selectedIdx != null) { ldRemoveElement(LD.selectedIdx); e.preventDefault(); }
    }
  };

  // If the print modal is currently open (user clicked Edit Template from within it),
  // hide it first — Bootstrap 5 can't manage two simultaneous modal backdrops cleanly.
  bootstrap.Modal.getInstance(document.getElementById('labelPrintModal'))?.hide();

  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('labelDesignerModal'));
  modal.show();

  document.getElementById('labelDesignerModal').addEventListener('shown.bs.modal', () => {
    ldComputeScale();
    ldRequestRender(true);
  }, { once: true });
}

// Calculate display scale so the label fills the available canvas area
function ldComputeScale() {
  const scroll = document.getElementById('ld-canvas-scroll');
  if (!scroll) return;
  const availW = scroll.clientWidth  - 64;
  const availH = scroll.clientHeight - 64;
  const scaleW = availW / LD.widthMm;
  const scaleH = availH / LD.heightMm;
  LD.scale = Math.min(scaleW, scaleH, 12);   // cap at 12px/mm (very small labels)
}

// ── Add element ───────────────────────────────────────────────────────────────

function ldAddElement(type) {
  const defaults = {
    product_name: { w: Math.min(LD.widthMm - 2, 38), h: 6,  font_size: 9,  bold: true  },
    price:        { w: 20, h: 8,  font_size: 12, bold: true,  align: 'center' },
    barcode:      { w: Math.min(LD.widthMm - 4, 34), h: 12, barcode_format: 'auto' },
    sku:          { w: 20, h: 4,  font_size: 6  },
    store_name:   { w: Math.min(LD.widthMm - 2, 38), h: 5,  font_size: 7, align: 'center' },
    store_logo:   { w: 20, h: 10 },
    weight:       { w: 20, h: 5,  font_size: 8  },
    category:     { w: 25, h: 4,  font_size: 6  },
    custom_text:  { w: 25, h: 5,  font_size: 8, value: 'Custom text' },
  };
  const offset = (LD.elements.length % 5) * 1.5;
  const elW = defaults[type]?.w || 20;
  const elH = defaults[type]?.h || 6;
  const el = {
    type, color: '#000000',
    x: Math.min(Math.max(1, (LD.widthMm  - elW) / 2 + offset), Math.max(1, LD.widthMm  - elW)),
    y: Math.min(Math.max(1, (LD.heightMm - elH) / 2 + offset), Math.max(1, LD.heightMm - elH)),
    ...defaults[type],
  };
  LD.elements.push(el);
  LD.selectedIdx = LD.elements.length - 1;
  ldShowProps(LD.selectedIdx);
  ldRequestRender(true);
}

// ── Render pipeline ───────────────────────────────────────────────────────────

function ldRequestRender(immediate = false) {
  clearTimeout(_ldRenderTimer);
  _ldRenderTimer = setTimeout(ldDoRender, immediate ? 0 : 220);
}

async function ldDoRender() {
  const spinner  = document.getElementById('ld-canvas-spinner');
  const errDiv   = document.getElementById('ld-canvas-error');
  const img      = document.getElementById('ld-canvas-img');
  const status   = document.getElementById('ld-render-status');

  if (spinner) { spinner.style.display = 'flex'; }
  if (errDiv)  { errDiv.style.display  = 'none'; }
  if (status)  { status.textContent    = 'rendering…'; }

  const productId = document.getElementById('ld-preview-product')?.value || null;
  const template  = ldBuildTemplate();

  try {
    const res = await fetch('/api/labels/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        product_id: productId ? parseInt(productId) : null,
        template,
        dpr: Math.min(Math.ceil(window.devicePixelRatio || 1), 3),
      }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `Server error ${res.status}`);
    }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const wPx  = Math.round(LD.widthMm  * LD.scale);
    const hPx  = Math.round(LD.heightMm * LD.scale);
    if (img) {
      img.style.width  = wPx + 'px';
      img.style.height = hPx + 'px';
      if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
      img.src = url;
    }
    if (status) status.textContent = '';
    ldDrawOverlay();
  } catch (e) {
    if (status) status.textContent = '';
    if (errDiv) {
      errDiv.textContent = 'Preview failed: ' + e.message;
      errDiv.style.display = 'flex';
    }
    console.error('ldDoRender:', e);
  } finally {
    if (spinner) spinner.style.display = 'none';
  }
}

function ldBuildTemplate() {
  return {
    width_mm:         parseFloat(document.getElementById('ld-width')?.value)  || LD.widthMm,
    height_mm:        parseFloat(document.getElementById('ld-height')?.value) || LD.heightMm,
    elements:         LD.elements,
    background_color: document.getElementById('ld-bg-color')?.value || '#ffffff',
    border:           document.getElementById('ld-border')?.checked || false,
  };
}

// ── Overlay: selection + drag + resize handles ────────────────────────────────

const _GRIPS = [
  { id:'nw', cx:0,   cy:0,   cursor:'nw-resize',  dx:-1, dy:-1, dw: 1, dh: 1 },
  { id:'n',  cx:0.5, cy:0,   cursor:'n-resize',   dx: 0, dy:-1, dw: 0, dh: 1 },
  { id:'ne', cx:1,   cy:0,   cursor:'ne-resize',  dx: 0, dy:-1, dw: 1, dh: 1 },
  { id:'e',  cx:1,   cy:0.5, cursor:'e-resize',   dx: 0, dy: 0, dw: 1, dh: 0 },
  { id:'se', cx:1,   cy:1,   cursor:'se-resize',  dx: 0, dy: 0, dw: 1, dh: 1 },
  { id:'s',  cx:0.5, cy:1,   cursor:'s-resize',   dx: 0, dy: 0, dw: 0, dh: 1 },
  { id:'sw', cx:0,   cy:1,   cursor:'sw-resize',  dx:-1, dy: 0, dw: 1, dh: 1 },
  { id:'w',  cx:0,   cy:0.5, cursor:'w-resize',   dx:-1, dy: 0, dw: 1, dh: 0 },
];

function ldDrawOverlay() {
  const overlay = document.getElementById('ld-canvas-overlay');
  if (!overlay) return;
  overlay.innerHTML = '';
  overlay.style.pointerEvents = 'none';

  LD.elements.forEach((el, idx) => {
    const s    = LD.scale;
    const x    = el.x * s;
    const y    = el.y * s;
    const w    = el.w * s;
    const h    = el.h * s;
    const sel  = idx === LD.selectedIdx;
    const GRIP = 22;

    const ghost = document.createElement('div');
    ghost.style.cssText = `
      position:absolute; left:${x}px; top:${y}px; width:${w}px; height:${h}px;
      box-sizing:border-box; cursor:move; pointer-events:all; touch-action:none;
      border:${sel ? '2px solid #0d6efd' : '1px dashed rgba(0,100,255,.35)'};
      background:${sel ? 'rgba(13,110,253,.06)' : 'transparent'};
    `;
    ghost.title = el.type;
    ghost.addEventListener('mousedown',  e => { e.stopPropagation(); ldStartMove(e, idx); });
    ghost.addEventListener('touchstart', e => { e.stopPropagation(); e.preventDefault(); ldStartMove(e, idx); }, { passive: false });
    ghost.addEventListener('click',      e => { e.stopPropagation(); ldSelectElement(idx); });
    overlay.appendChild(ghost);

    if (sel) {
      _GRIPS.forEach(g => {
        const grip = document.createElement('div');
        const gx   = x + g.cx * w - GRIP / 2;
        const gy   = y + g.cy * h - GRIP / 2;
        grip.style.cssText = `
          position:absolute; left:${gx}px; top:${gy}px;
          width:${GRIP}px; height:${GRIP}px;
          background:#0d6efd; border:2px solid #fff; border-radius:3px;
          cursor:${g.cursor}; pointer-events:all; box-sizing:border-box; touch-action:none;
        `;
        grip.addEventListener('mousedown',  e => { e.stopPropagation(); ldStartResize(e, idx, g); });
        grip.addEventListener('touchstart', e => { e.stopPropagation(); e.preventDefault(); ldStartResize(e, idx, g); }, { passive: false });
        overlay.appendChild(grip);
      });
    }
  });

  const bg = document.createElement('div');
  bg.style.cssText = 'position:absolute;inset:0;pointer-events:all;cursor:default';
  bg.addEventListener('mousedown',  () => ldSelectElement(null));
  bg.addEventListener('touchstart', () => ldSelectElement(null));
  overlay.insertBefore(bg, overlay.firstChild);
}

function ldSelectElement(idx) {
  LD.selectedIdx = idx;
  ldDrawOverlay();
  ldShowProps(idx);
}

// ── Move (drag entire element) ────────────────────────────────────────────────

function ldStartMove(e, idx) {
  if (e.preventDefault) e.preventDefault();
  ldSelectElement(idx);
  const s      = LD.scale;
  const origX  = LD.elements[idx].x;
  const origY  = LD.elements[idx].y;
  const pt     = ldGetCoords(e);
  const startX = pt.clientX, startY = pt.clientY;

  const onMove = ev => {
    if (ev.preventDefault) ev.preventDefault();
    const p  = ldGetCoords(ev);
    const dx = (p.clientX - startX) / s;
    const dy = (p.clientY - startY) / s;
    LD.elements[idx].x = Math.max(0, parseFloat((origX + dx).toFixed(1)));
    LD.elements[idx].y = Math.max(0, parseFloat((origY + dy).toFixed(1)));
    ldDrawOverlay();
    ldSyncPropsPosition(idx);
    ldRequestRender();
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup',   onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend',  onUp);
    ldRequestRender(true);
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);
  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('touchend',  onUp);
}

// ── Resize (drag a grip) ──────────────────────────────────────────────────────

function ldStartResize(e, idx, grip) {
  if (e.preventDefault) e.preventDefault();
  const s      = LD.scale;
  const el     = LD.elements[idx];
  const origX  = el.x, origY  = el.y;
  const origW  = el.w, origH  = el.h;
  const pt     = ldGetCoords(e);
  const startX = pt.clientX, startY = pt.clientY;
  const MIN    = 2;

  const onMove = ev => {
    if (ev.preventDefault) ev.preventDefault();
    const p  = ldGetCoords(ev);
    const dx = (p.clientX - startX) / s;
    const dy = (p.clientY - startY) / s;
    let nx = origX, ny = origY, nw = origW, nh = origH;
    if (grip.dw) nw = Math.max(MIN, origW + dx * (grip.dx < 0 ? -1 : 1));
    if (grip.dh) nh = Math.max(MIN, origH + dy * (grip.dy < 0 ? -1 : 1));
    if (grip.dx < 0) nx = Math.max(0, Math.min(origX + origW - MIN, origX + dx));
    if (grip.dy < 0) ny = Math.max(0, Math.min(origY + origH - MIN, origY + dy));
    el.x = parseFloat(nx.toFixed(1));
    el.y = parseFloat(ny.toFixed(1));
    el.w = parseFloat(nw.toFixed(1));
    el.h = parseFloat(nh.toFixed(1));
    ldDrawOverlay();
    ldSyncPropsSize(idx);
    ldRequestRender();
  };
  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup',   onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend',  onUp);
    ldRequestRender(true);
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);
  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('touchend',  onUp);
}

// ── Properties panel ─────────────────────────────────────────────────────────

function ldShowProps(idx) {
  const panel = document.getElementById('ld-props-content');
  if (!panel) return;
  if (idx == null || !LD.elements[idx]) {
    panel.innerHTML = '<div class="text-muted small mt-2">Click an element on the label to select it.<br><br>Drag to move &bull; Drag corners/edges to resize.</div>';
    return;
  }
  const el = LD.elements[idx];
  const typeLabel = {
    product_name:'Product Name', price:'Price', barcode:'Barcode', sku:'SKU',
    store_name:'Store Name', store_logo:'Store Logo', weight:'Weight',
    category:'Category', custom_text:'Custom Text',
  }[el.type] || el.type;

  panel.innerHTML = `
    <div class="fw-semibold small mb-3" style="color:#0d6efd">${typeLabel}</div>

    <div class="mb-2">
      <label class="form-label mb-1 small fw-semibold">Position &amp; Size (mm)</label>
      <div class="row g-1">
        <div class="col-6">
          <label class="form-label mb-0" style="font-size:10px">X</label>
          <input id="ld-prop-x" type="number" class="form-control form-control-sm" value="${el.x}" step="0.5" oninput="ldPropChange('x',+this.value)">
        </div>
        <div class="col-6">
          <label class="form-label mb-0" style="font-size:10px">Y</label>
          <input id="ld-prop-y" type="number" class="form-control form-control-sm" value="${el.y}" step="0.5" oninput="ldPropChange('y',+this.value)">
        </div>
        <div class="col-6">
          <label class="form-label mb-0" style="font-size:10px">Width</label>
          <input id="ld-prop-w" type="number" class="form-control form-control-sm" value="${el.w}" step="0.5" min="2" oninput="ldPropChange('w',+this.value)">
        </div>
        <div class="col-6">
          <label class="form-label mb-0" style="font-size:10px">Height</label>
          <input id="ld-prop-h" type="number" class="form-control form-control-sm" value="${el.h}" step="0.5" min="2" oninput="ldPropChange('h',+this.value)">
        </div>
      </div>
    </div>

    ${el.type !== 'barcode' && el.type !== 'store_logo' ? `
    <div class="mb-2">
      <label class="form-label mb-1 small fw-semibold">Text</label>
      <div class="d-flex align-items-center gap-1 mb-1">
        <span style="font-size:10px;min-width:28px">Size</span>
        <input type="number" class="form-control form-control-sm" style="width:60px" value="${el.font_size ?? 9}" min="5" max="72" oninput="{ const v=+this.value; if(this.value!==''&&!isNaN(v)) ldPropChange('font_size', Math.max(5,Math.min(72,v))); }">
        <span style="font-size:10px">pt</span>
        <div class="form-check mb-0 ms-1">
          <input class="form-check-input" type="checkbox" id="ld-prop-bold" ${el.bold ? 'checked' : ''} onchange="ldPropChange('bold',this.checked)">
          <label class="form-check-label small" for="ld-prop-bold">Bold</label>
        </div>
      </div>
      <div class="btn-group btn-group-sm w-100 mb-1">
        <button class="btn ${!el.align||el.align==='left' ?'btn-secondary':'btn-outline-secondary'}" onclick="ldPropChange('align','left')">Left</button>
        <button class="btn ${el.align==='center'?'btn-secondary':'btn-outline-secondary'}"  onclick="ldPropChange('align','center')">Center</button>
        <button class="btn ${el.align==='right' ?'btn-secondary':'btn-outline-secondary'}"  onclick="ldPropChange('align','right')">Right</button>
      </div>
      <label class="form-label mb-1 small fw-semibold">Colour</label>
      <input type="color" class="form-control form-control-color w-100" value="${el.color||'#000000'}" oninput="ldPropChange('color',this.value)">
    </div>
    ` : ''}

    ${el.type === 'barcode' ? `
    <div class="mb-2">
      <label class="form-label mb-1 small fw-semibold">Barcode Format</label>
      <select class="form-select form-select-sm" onchange="ldPropChange('barcode_format',this.value)">
        <option value="auto"    ${el.barcode_format==='auto'   ||!el.barcode_format?'selected':''}>Auto-detect</option>
        <option value="code128" ${el.barcode_format==='code128'?'selected':''}>Code 128</option>
        <option value="ean13"   ${el.barcode_format==='ean13'  ?'selected':''}>EAN-13</option>
        <option value="code39"  ${el.barcode_format==='code39' ?'selected':''}>Code 39</option>
        <option value="qrcode"  ${el.barcode_format==='qrcode' ?'selected':''}>QR Code</option>
      </select>
    </div>
    ` : ''}

    ${el.type === 'custom_text' ? `
    <div class="mb-2">
      <label class="form-label mb-1 small fw-semibold">Text Content</label>
      <input type="text" class="form-control form-control-sm" value="${escapeHtml(el.value||'')}" oninput="ldPropChange('value',this.value)">
    </div>
    ` : ''}

    <hr class="my-2">
    <button class="btn btn-outline-danger btn-sm w-100" onclick="ldRemoveElement(${idx})">Remove element</button>
  `;
}

function ldPropChange(key, value) {
  if (LD.selectedIdx == null || !LD.elements[LD.selectedIdx]) return;
  LD.elements[LD.selectedIdx][key] = value;
  ldDrawOverlay();
  if (key === 'align') ldSyncPropsAlign(LD.selectedIdx);
  ldRequestRender();
}

function ldSyncPropsPosition(idx) {
  const el = LD.elements[idx];
  const xEl = document.getElementById('ld-prop-x'); if (xEl) xEl.value = el.x;
  const yEl = document.getElementById('ld-prop-y'); if (yEl) yEl.value = el.y;
}
function ldSyncPropsSize(idx) {
  const el = LD.elements[idx];
  const wEl = document.getElementById('ld-prop-w'); if (wEl) wEl.value = el.w;
  const hEl = document.getElementById('ld-prop-h'); if (hEl) hEl.value = el.h;
  ldSyncPropsPosition(idx);
}
function ldSyncPropsAlign(idx) {
  const el = LD.elements[idx];
  const aligns = ['left', 'center', 'right'];
  document.querySelectorAll('#ld-props-content .btn-group .btn').forEach((btn, i) => {
    if (i >= aligns.length) return;
    const active = el.align === aligns[i] || (!el.align && aligns[i] === 'left');
    btn.className = `btn ${active ? 'btn-secondary' : 'btn-outline-secondary'}`;
  });
}
function ldGetCoords(e) {
  if (e.touches && e.touches.length)         return e.touches[0];
  if (e.changedTouches && e.changedTouches.length) return e.changedTouches[0];
  return e;
}

function ldRemoveElement(idx) {
  LD.elements.splice(idx, 1);
  LD.selectedIdx = null;
  ldShowProps(null);
  ldRequestRender(true);
}

// ── Save / Delete ─────────────────────────────────────────────────────────────

async function ldSaveTemplate(andPrint) {
  const name = document.getElementById('ld-template-name')?.value.trim();
  if (!name) { toast('Enter a template name first', 'warning'); return; }
  const body = {
    name,
    description: '',
    width_mm:         parseFloat(document.getElementById('ld-width')?.value)  || LD.widthMm,
    height_mm:        parseFloat(document.getElementById('ld-height')?.value) || LD.heightMm,
    category:         document.getElementById('ld-category')?.value || 'custom',
    elements:         LD.elements,
    background_color: document.getElementById('ld-bg-color')?.value || '#ffffff',
    border:           document.getElementById('ld-border')?.checked || false,
  };
  try {
    let saved;
    if (LD.templateId) {
      saved = await api(`/api/label-templates/${LD.templateId}`, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      saved = await api('/api/label-templates', { method: 'POST', body: JSON.stringify(body) });
      LD.templateId = saved.id;
      document.getElementById('ld-delete-tmpl-btn').style.display = '';
    }
    LABELS._currentTemplateId = saved.id;
    await loadLabelTemplates();
    _populateDesignerLoadSelect(saved.id);
    const lpSel = document.getElementById('lp-template-select');
    if (lpSel) {
      _populateTemplateSelect(lpSel, saved.id);
      if (!andPrint) refreshLabelPreview();
    }
    toast(`Template "${name}" saved`, 'success');
    if (andPrint && LABELS._currentProduct) {
      bootstrap.Modal.getInstance(document.getElementById('labelDesignerModal'))?.hide();
      await openLabelPrintModal(LABELS._currentProduct);
    }
  } catch (e) { toast(`Save failed: ${e.message}`, 'error'); }
}

async function ldDeleteTemplate() {
  if (!LD.templateId || !confirm('Delete this template? This cannot be undone.')) return;
  try {
    await api(`/api/label-templates/${LD.templateId}`, { method: 'DELETE' });
    toast('Template deleted', 'success');
    bootstrap.Modal.getInstance(document.getElementById('labelDesignerModal'))?.hide();
    await loadLabelTemplates();
  } catch (e) { toast(e.message, 'error'); }
}

function ldNewTemplate() {
  LD.templateId = null; LD.elements = []; LD.selectedIdx = null;
  LD.widthMm = 40; LD.heightMm = 20;
  document.getElementById('ld-template-name').value = 'New Template';
  document.getElementById('ld-category').value      = 'custom';
  document.getElementById('ld-width').value         = 40;
  document.getElementById('ld-height').value        = 20;
  document.getElementById('ld-border').checked      = false;
  document.getElementById('ld-bg-color').value      = '#ffffff';
  document.getElementById('ld-delete-tmpl-btn').style.display = 'none';
  document.getElementById('ld-load-select').value   = '';
  ldShowProps(null);
  ldRequestRender(true);
}

function _populateDesignerLoadSelect(selectedId) {
  const sel = document.getElementById('ld-load-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">— load existing —</option>';
  LABELS.templates.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t.id;
    opt.textContent = `${t.name} (${t.width_mm}×${t.height_mm}mm)`;
    if (t.id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  });
}

function ldLoadSelectedTemplate(idStr) {
  const id = parseInt(idStr);
  if (!id) return;
  const tmpl = LABELS.templates.find(t => t.id === id);
  if (!tmpl) return;
  LD.templateId = tmpl.id;
  LD.elements   = JSON.parse(JSON.stringify(tmpl.elements));
  LD.widthMm    = tmpl.width_mm;
  LD.heightMm   = tmpl.height_mm;
  LD.selectedIdx = null;
  document.getElementById('ld-template-name').value = tmpl.name;
  document.getElementById('ld-category').value      = tmpl.category;
  document.getElementById('ld-width').value         = tmpl.width_mm;
  document.getElementById('ld-height').value        = tmpl.height_mm;
  document.getElementById('ld-border').checked      = tmpl.border;
  document.getElementById('ld-bg-color').value      = tmpl.background_color || '#ffffff';
  document.getElementById('ld-delete-tmpl-btn').style.display = '';
  ldShowProps(null);
  ldRequestRender(true);
}

// ── Show label-templates / bulk edit buttons only on products tab ────────────
document.addEventListener('shown.bs.tab', e => {
  const tabId = e.target?.getAttribute('data-bs-target') || e.target?.href?.split('#')[1];
  const tmplBtn = document.getElementById('btn-label-templates');
  if (tmplBtn) tmplBtn.classList.toggle('hidden', tabId !== '#products');
  const bulkEditBtn = document.getElementById('btn-bulk-edit');
  if (bulkEditBtn) bulkEditBtn.classList.toggle('hidden', tabId !== '#products');
});

// ══════════════════════════════════════════════════════════════════════════════
// BULK PRODUCT EDITOR
// ══════════════════════════════════════════════════════════════════════════════

let _bulkFields   = null; // loaded once from /api/products/bulk/fields
let _bulkMatched  = 0;    // count from last filter run
let _bulkPreviewData = null; // last preview result
let _bulkFilteredProducts = []; // products returned by last filter run
let _bulkExcludedIds = new Set(); // product IDs unchecked by user

async function _bulkEnsureFields() {
  if (_bulkFields) return _bulkFields;
  _bulkFields = await api('/api/products/bulk/fields');
  return _bulkFields;
}

function openBulkEditor() {
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('bulkEditorModal'));
  modal.show();
  _bulkEnsureFields();
  // Reset to Filter tab
  const filterTab = document.querySelector('#bulk-tabs .nav-link');
  if (filterTab) bootstrap.Tab.getOrCreateInstance(filterTab).show();
  document.getElementById('bulk-filter-result').textContent = '';
  document.getElementById('bulk-action-scope').textContent = '';
  const matchedList = document.getElementById('bulk-filter-matched-list');
  if (matchedList) matchedList.innerHTML = '';
  _bulkFilteredProducts = [];
  _bulkExcludedIds = new Set();
}

// ── Condition builder ────────────────────────────────────────────────────────

const _BULK_OP_LABELS = {
  contains: 'contains', not_contains: "doesn't contain", starts: 'starts with',
  ends: 'ends with', eq: 'equals', ne: 'not equals',
  empty: 'is empty', populated: 'is filled',
  gt: '>', gte: '>=', lt: '<', lte: '<=',
};

function _bulkOpsForType(type) {
  if (type === 'bool')    return ['eq', 'empty', 'populated'];
  if (type === 'float' || type === 'int') return ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'empty', 'populated'];
  return ['contains', 'not_contains', 'starts', 'ends', 'eq', 'ne', 'empty', 'populated'];
}

function bulkAddCondition() {
  const fields = _bulkFields?.filterable || [];
  const row = document.createElement('div');
  row.className = 'd-flex gap-2 align-items-center condition-row';

  const fieldSel = document.createElement('select');
  fieldSel.className = 'form-select form-select-sm';
  fieldSel.style.maxWidth = '180px';
  fields.forEach(f => {
    const o = document.createElement('option');
    o.value = f.key; o.textContent = f.label;
    fieldSel.appendChild(o);
  });

  const opSel = document.createElement('select');
  opSel.className = 'form-select form-select-sm';
  opSel.style.maxWidth = '160px';

  const valInput = document.createElement('input');
  valInput.type = 'text';
  valInput.className = 'form-control form-control-sm';
  valInput.placeholder = 'value';

  const removeBtn = document.createElement('button');
  removeBtn.className = 'btn btn-outline-danger btn-sm';
  removeBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
  removeBtn.addEventListener('click', () => {
    row.remove();
    _bulkUpdateNoConditionsHint();
  });

  function _refreshOps() {
    const fieldKey = fieldSel.value;
    const field = fields.find(f => f.key === fieldKey);
    const ftype = field?.type || 'str';
    opSel.innerHTML = '';
    _bulkOpsForType(ftype).forEach(op => {
      const o = document.createElement('option');
      o.value = op; o.textContent = _BULK_OP_LABELS[op] || op;
      opSel.appendChild(o);
    });
    _refreshValueInput();
  }

  function _refreshValueInput() {
    const op = opSel.value;
    const fieldKey = fieldSel.value;
    const field = fields.find(f => f.key === fieldKey);
    const ftype = field?.type || 'str';
    const noVal = (op === 'empty' || op === 'populated');
    valInput.style.display = noVal ? 'none' : '';
    if (ftype === 'bool') {
      valInput.type = 'text';
      valInput.placeholder = 'true / false';
    } else if (ftype === 'float' || ftype === 'int') {
      valInput.type = 'number';
      valInput.placeholder = 'number';
    } else {
      valInput.type = 'text';
      valInput.placeholder = 'value';
    }
  }

  fieldSel.addEventListener('change', _refreshOps);
  opSel.addEventListener('change', _refreshValueInput);
  _refreshOps();

  row.append(fieldSel, opSel, valInput, removeBtn);
  document.getElementById('bulk-conditions').appendChild(row);
  _bulkUpdateNoConditionsHint();
}

function _bulkUpdateNoConditionsHint() {
  const noEl = document.getElementById('bulk-no-conditions');
  const rows = document.querySelectorAll('.condition-row');
  if (noEl) noEl.style.display = rows.length === 0 ? '' : 'none';
}

function _bulkGetConditions() {
  const rows = document.querySelectorAll('.condition-row');
  return Array.from(rows).map(row => {
    const [fieldSel, opSel, valInput] = row.querySelectorAll('select, select, input');
    const sels = row.querySelectorAll('select');
    const inp  = row.querySelector('input');
    return {
      field:    sels[0]?.value,
      operator: sels[1]?.value,
      value:    inp?.style.display === 'none' ? '' : (inp?.value || ''),
    };
  });
}

async function bulkRunFilter() {
  const conditions = _bulkGetConditions();
  const includeArchived = document.getElementById('bulk-include-archived')?.checked;
  const resultEl = document.getElementById('bulk-filter-result');
  resultEl.textContent = 'Filtering…';
  try {
    const resp = await api('/api/products/bulk/filter', {
      method: 'POST',
      body: JSON.stringify({ conditions, include_archived: includeArchived, per_page: 500 }),
    });
    _bulkMatched = resp.total;
    _bulkFilteredProducts = resp.products || [];
    _bulkExcludedIds = new Set();
    resultEl.textContent = `${resp.total} product${resp.total !== 1 ? 's' : ''} matched.`;
    document.getElementById('bulk-action-scope').textContent = `Applies to ${resp.total} product${resp.total !== 1 ? 's' : ''}`;
    _renderBulkFilteredList();
    // Auto-advance to Action tab
    const actionTabLink = document.getElementById('bulk-tab-action-link');
    if (actionTabLink) bootstrap.Tab.getOrCreateInstance(actionTabLink).show();
  } catch(e) { resultEl.textContent = `Error: ${e.message}`; }
}

function _renderBulkFilteredList() {
  const wrap = document.getElementById('bulk-filter-matched-list');
  if (!wrap) return;
  if (!_bulkFilteredProducts.length) { wrap.innerHTML = ''; return; }
  const typeLabel = { stock_item: 'Stock', simple: 'Simple', recipe: 'Recipe' };
  wrap.innerHTML = `
    <div class="d-flex justify-content-between align-items-center mb-1 mt-3">
      <span class="small fw-semibold text-muted">Matched products — uncheck to exclude from action</span>
      <span class="small text-muted" id="bulk-included-count">${_bulkFilteredProducts.length} included</span>
    </div>
    <div style="max-height:200px;overflow-y:auto;border:1px solid #dee2e6;border-radius:4px;padding:4px 8px">
      ${_bulkFilteredProducts.map(p => `
        <div class="d-flex align-items-center gap-2 py-1 border-bottom" style="font-size:13px">
          <input type="checkbox" class="form-check-input bulk-filter-chk" data-pid="${p.id}" checked>
          <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>
          <span class="badge bg-secondary" style="font-size:10px">${typeLabel[p.product_type] || p.product_type}</span>
          ${p.price != null ? `<span class="text-muted" style="font-size:11px;min-width:50px;text-align:right">R${fmt(p.price)}</span>` : ''}
        </div>
      `).join('')}
    </div>
  `;
  wrap.querySelectorAll('.bulk-filter-chk').forEach(chk => {
    chk.addEventListener('change', () => {
      const pid = parseInt(chk.dataset.pid);
      if (chk.checked) _bulkExcludedIds.delete(pid);
      else             _bulkExcludedIds.add(pid);
      const included = _bulkFilteredProducts.length - _bulkExcludedIds.size;
      const countEl = document.getElementById('bulk-included-count');
      if (countEl) countEl.textContent = `${included} included`;
      document.getElementById('bulk-action-scope').textContent =
        `Applies to ${included} product${included !== 1 ? 's' : ''}`;
    });
  });
}

// ── Action builder ───────────────────────────────────────────────────────────

function bulkAddAction() {
  const fields = _bulkFields?.editable || [];
  const row = document.createElement('div');
  row.className = 'd-flex gap-2 align-items-start flex-wrap action-row border rounded p-2';

  const opSel = document.createElement('select');
  opSel.className = 'form-select form-select-sm';
  opSel.style.maxWidth = '110px';
  ['set', 'replace'].forEach(op => {
    const o = document.createElement('option'); o.value = op; o.textContent = op; opSel.appendChild(o);
  });

  const fieldSel = document.createElement('select');
  fieldSel.className = 'form-select form-select-sm';
  fieldSel.style.maxWidth = '180px';
  fields.forEach(f => {
    const o = document.createElement('option'); o.value = f.key; o.textContent = f.label; fieldSel.appendChild(o);
  });

  const valueWrap = document.createElement('div');
  valueWrap.className = 'd-flex gap-2 align-items-center flex-wrap';

  const valInput = document.createElement('input');
  valInput.className = 'form-control form-control-sm';
  valInput.style.maxWidth = '180px';
  valInput.placeholder = 'value';

  // Replace-specific fields
  const findInput = document.createElement('input');
  findInput.className = 'form-control form-control-sm'; findInput.style.maxWidth = '140px'; findInput.placeholder = 'find';
  const replaceInput = document.createElement('input');
  replaceInput.className = 'form-control form-control-sm'; replaceInput.style.maxWidth = '140px'; replaceInput.placeholder = 'replace with';
  const caseSensCheck = document.createElement('div');
  caseSensCheck.innerHTML = '<div class="form-check mb-0"><input class="form-check-input" type="checkbox" id="bulk-case-'+Math.random().toString(36).slice(2)+'" /><label class="form-check-label small">Case-sensitive</label></div>';

  const removeBtn = document.createElement('button');
  removeBtn.className = 'btn btn-outline-danger btn-sm'; removeBtn.innerHTML = '<i class="bi bi-x-lg"></i>';
  removeBtn.addEventListener('click', () => { row.remove(); _bulkUpdateNoActionsHint(); });

  function _refresh() {
    const op = opSel.value;
    const ftype = (fields.find(f => f.key === fieldSel.value) || {}).type || 'str';
    valueWrap.innerHTML = '';
    if (op === 'set') {
      if (ftype === 'bool') {
        valInput.type = 'text'; valInput.placeholder = 'true / false';
      } else if (ftype === 'float' || ftype === 'int') {
        valInput.type = 'number'; valInput.placeholder = 'number';
      } else {
        valInput.type = 'text'; valInput.placeholder = 'value';
      }
      const toLabel = document.createElement('span');
      toLabel.className = 'small text-muted align-self-center'; toLabel.textContent = 'to';
      valueWrap.append(toLabel, valInput);
    } else {
      // replace - only for string fields; show find/replace
      if (ftype !== 'str') {
        const warn = document.createElement('span');
        warn.className = 'small text-danger align-self-center'; warn.textContent = 'Replace only works on text fields';
        valueWrap.appendChild(warn);
      } else {
        valueWrap.append(findInput, document.createTextNode('→'), replaceInput, caseSensCheck);
      }
    }
  }

  opSel.addEventListener('change', _refresh);
  fieldSel.addEventListener('change', _refresh);
  _refresh();

  row.append(opSel, fieldSel, valueWrap, removeBtn);
  document.getElementById('bulk-actions').appendChild(row);
  _bulkUpdateNoActionsHint();
}

function _bulkUpdateNoActionsHint() {
  const noEl = document.getElementById('bulk-no-actions');
  const rows = document.querySelectorAll('.action-row');
  if (noEl) noEl.style.display = rows.length === 0 ? '' : 'none';
}

function _bulkGetActions() {
  const rows = document.querySelectorAll('.action-row');
  return Array.from(rows).map(row => {
    const sels = row.querySelectorAll('select');
    const op    = sels[0]?.value;
    const field = sels[1]?.value;
    if (op === 'set') {
      const val = row.querySelector('input[type="text"], input[type="number"]')?.value ?? '';
      return { op, field, value: val };
    } else {
      const inputs = row.querySelectorAll('input[type="text"]');
      const caseCheck = row.querySelector('input[type="checkbox"]');
      return { op, field, find: inputs[0]?.value || '', replace: inputs[1]?.value || '', case_sensitive: caseCheck?.checked || false };
    }
  }).filter(a => a.field);
}

async function bulkRunPreview() {
  const conditions = _bulkGetConditions();
  const actions    = _bulkGetActions();
  const includeArchived = document.getElementById('bulk-include-archived')?.checked;
  if (!actions.length) { toast('Add at least one action first', 'warning'); return; }

  const previewTabLink = document.getElementById('bulk-tab-preview-link');
  if (previewTabLink) bootstrap.Tab.getOrCreateInstance(previewTabLink).show();
  document.getElementById('bulk-preview-summary').textContent = 'Loading preview…';
  document.getElementById('bulk-preview-tbody').innerHTML = '';

  try {
    const excludeIds = [..._bulkExcludedIds];
    const resp = await api('/api/products/bulk/preview', {
      method: 'POST',
      body: JSON.stringify({ conditions, actions, include_archived: includeArchived, exclude_ids: excludeIds }),
    });
    _bulkPreviewData = { conditions, actions, includeArchived, excludeIds };

    const summaryEl = document.getElementById('bulk-preview-summary');
    summaryEl.innerHTML = `
      Matched: <strong>${resp.matched_total}</strong> products ·
      Affected: <strong>${resp.affected}</strong> will change
      ${resp.preview_capped ? '<span class="text-warning ms-2">(preview capped at 500)</span>' : ''}
    `;

    const tbody = document.getElementById('bulk-preview-tbody');
    if (!resp.changes.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted text-center py-3">No changes</td></tr>';
      return;
    }
    tbody.innerHTML = resp.changes.map(prod =>
      prod.changes.map((c, ci) => `
        <tr>
          ${ci === 0 ? `<td rowspan="${prod.changes.length}" class="align-middle">${escapeHtml(prod.name)}</td>` : ''}
          <td class="small text-muted">${escapeHtml(c.label)}</td>
          <td class="small text-danger">${escapeHtml(String(c.old ?? ''))}</td>
          <td class="small text-success">${escapeHtml(String(c.new ?? ''))}</td>
        </tr>
      `).join('')
    ).join('');
    document.getElementById('bulk-apply-status').textContent = '';
  } catch(e) {
    document.getElementById('bulk-preview-summary').textContent = `Error: ${e.message}`;
  }
}

async function bulkApply() {
  if (!_bulkPreviewData) { toast('Run Preview first', 'warning'); return; }
  const { conditions, actions, includeArchived, excludeIds } = _bulkPreviewData;
  const description = document.getElementById('bulk-description')?.value?.trim() || '';
  const statusEl = document.getElementById('bulk-apply-status');
  statusEl.textContent = 'Applying…';
  document.getElementById('bulk-apply-btn').disabled = true;
  try {
    const resp = await api('/api/products/bulk/apply', {
      method: 'POST',
      body: JSON.stringify({ conditions, actions, include_archived: includeArchived, description, exclude_ids: excludeIds || [] }),
    });
    statusEl.textContent = `Done — ${resp.affected} product${resp.affected !== 1 ? 's' : ''} updated.`;
    toast(`Bulk edit applied — ${resp.affected} products updated`, 'success');
    await loadProducts();
  } catch(e) {
    statusEl.textContent = `Error: ${e.message}`;
    toast(e.message, 'error');
  } finally {
    document.getElementById('bulk-apply-btn').disabled = false;
  }
}

// ── History & rollback ───────────────────────────────────────────────────────

async function bulkLoadHistory() {
  const tbody = document.getElementById('bulk-history-tbody');
  tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">Loading…</td></tr>';
  try {
    const runs = await api('/api/products/bulk/history');
    if (!runs.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">No history</td></tr>';
      return;
    }
    tbody.innerHTML = runs.map(r => {
      const when = r.created_at ? new Date(r.created_at).toLocaleString() : '-';
      const actions = (() => {
        try {
          return JSON.parse(r.action_json).map(a =>
            a.op === 'replace'
              ? `replace "${a.find}" → "${a.replace}" in ${a.field}`
              : `set ${a.field}=${a.value}`
          ).join('; ');
        } catch { return r.action_json; }
      })();
      const rolled = r.rolled_back_at ? `<span class="badge bg-secondary">rolled back</span>` : '';
      return `<tr>
        <td class="small">${when}</td>
        <td class="small">${escapeHtml(r.created_by)}</td>
        <td class="small">${escapeHtml(r.description || actions)}</td>
        <td class="text-center">${r.product_count}</td>
        <td>${rolled}${!r.rolled_back_at ? `<button class="btn btn-outline-warning btn-sm" onclick="bulkRollback(${r.id})"><i class="bi bi-arrow-counterclockwise me-1"></i>Rollback</button>` : ''}</td>
      </tr>`;
    }).join('');
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-danger">${e.message}</td></tr>`;
  }
}

async function bulkRollback(runId) {
  if (!confirm('Rollback this bulk edit? The affected products will return to their state before the edit.')) return;
  try {
    const resp = await api(`/api/products/bulk/rollback/${runId}`, { method: 'POST' });
    toast(`Rolled back — ${resp.restored} products restored`, 'success');
    await loadProducts();
    await bulkLoadHistory();
  } catch(e) { toast(e.message, 'error'); }
}

// Load history when switching to the history tab
document.getElementById('bulkEditorModal')?.addEventListener('shown.bs.modal', () => {
  bulkLoadHistory();
});
document.querySelector('[href="#bulk-tab-history"]')?.addEventListener('click', bulkLoadHistory);
