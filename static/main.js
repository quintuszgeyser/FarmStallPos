// Farm Stall POS main.js — v1.5.0

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
  productsSubTab:   'active',  // 'active' | 'ingredients' | 'archived'
};

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
function fmt(n)   { return (Math.round(n * 100) / 100).toFixed(2); }
function fmtQty(n) { return n % 1 === 0 ? String(n) : n.toFixed(3); }

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
  for (let a = 0; a < 20; a++) {
    const rnd  = String(Math.floor(Math.random() * 100000)).padStart(5, '0');
    const core = `200${String(id).padStart(5,'0')}${rnd}`.slice(0, 12);
    const cand = core + ean13Check(core);
    if (!STATE.products.some(p => (p.barcode + '') === cand)) return cand;
  }
  return String(Date.now()).slice(-13);
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
  if (au) au.textContent = `${STATE.user.username} (${STATE.user.role})`;
  show(tabs); show(contents);
  document.querySelectorAll('.admin-only').forEach(el =>
    STATE.user.role === 'admin' ? show(el) : hide(el));
  document.querySelectorAll('.teller-only').forEach(el =>
    STATE.user.role !== 'admin' ? show(el) : hide(el));
}

async function refreshMe() {
  const me = await api('/api/me');
  if (me.logged_in) {
    STATE.user = { username: me.username, role: me.role };
    hide(document.getElementById('btn-login'));
    show(document.getElementById('btn-logout'));
    const s = document.getElementById('login-status'); if (s) s.textContent = '';
  } else {
    STATE.user = null;
    show(document.getElementById('btn-login'));
    hide(document.getElementById('btn-logout'));
  }
  updateVisibility();
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
    await loadProducts();
    await loadTransactions();
    await loadSpecials();
    startKitchenBadgePoll();  // badge visible to all users
    if (STATE.user?.role === 'admin') {
      await loadSettings();
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
document.getElementById('btn-logout')?.addEventListener('click', doLogout);
document.getElementById('btn-logout-top')?.addEventListener('click', doLogout);

// ═══════════════════════════════════════════════════════
// PRODUCTS
// ═══════════════════════════════════════════════════════
async function loadProducts() {
  if (!STATE.user) return;
  try {
    STATE.products = await api('/api/products?full=1');
    renderProductsCards();
    renderProductDropdown();
  } catch (e) { console.error('loadProducts', e); }
}

function renderProductDropdown() {
  const sel = document.getElementById('product-select');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Select product…</option>';
  STATE.products
    .filter(p => p.is_for_sale !== false)
    .forEach(p => {
      const opt  = document.createElement('option');
      opt.value  = String(p.id);
      const label = p.sold_by_weight
        ? `${p.name} (by ${p.base_unit || 'weight'})`
        : `${p.name} — R${fmt(p.price || 0)}`;
      opt.textContent = label;
      sel.appendChild(opt);
    });
  if (prev) sel.value = prev;
  if (!sel._boundChange) {
    sel.addEventListener('change', () => {
      const pid  = parseInt(sel.value || '0', 10);
      const prod = STATE.products.find(x => x.id === pid);
      if (prod) addToCart(prod);
      sel.value = '';
    });
    sel._boundChange = true;
  }
}

function renderProductsCards() {
  const wrap = document.getElementById('products-card-list');
  if (!wrap) return;
  const q = (document.getElementById('products-filter')?.value || '').trim().toLowerCase();

  const tab = STATE.productsSubTab;

  let items = STATE.products.filter(p => {
    const matchesSearch = !q ||
      p.name.toLowerCase().includes(q) ||
      String(p.id) === q ||
      (p.barcode?.toLowerCase().includes(q));
    if (!matchesSearch) return false;
    if (tab === 'archived')     return p.is_archived === true;
    if (tab === 'ingredients')  return p.is_archived !== true && p.is_for_sale === false;
    if (tab === 'recipes')      return p.is_archived !== true && p.product_type === 'recipe' && p.is_for_sale !== false;
    // 'active' (Single Items) = for sale, not archived, not a recipe
    return p.is_archived !== true && p.is_for_sale !== false && p.product_type !== 'recipe';
  });

  // Update count badges
  const ingCount      = STATE.products.filter(p => !p.is_archived && p.is_for_sale === false).length;
  const archivedCount = STATE.products.filter(p => p.is_archived).length;
  const recipeCount   = STATE.products.filter(p => !p.is_archived && p.product_type === 'recipe').length;
  const ingBadge  = document.getElementById('ingredients-count-badge');
  const arcBadge  = document.getElementById('archived-count-badge');
  const recBadge  = document.getElementById('recipes-count-badge');
  if (ingBadge)  { ingBadge.textContent  = ingCount;      ingBadge.style.display  = ingCount > 0      ? '' : 'none'; }
  if (arcBadge)  { arcBadge.textContent  = archivedCount; arcBadge.style.display  = archivedCount > 0 ? '' : 'none'; }
  if (recBadge)  { recBadge.textContent  = recipeCount;   recBadge.style.display  = recipeCount > 0   ? '' : 'none'; }

  wrap.innerHTML = '';
  if (items.length === 0) {
    const msg = q ? 'No products match.' : 'No products yet.';
    wrap.innerHTML = `<div class="text-muted">${msg}</div>`;
    return;
  }

  items.forEach(p => {
    const isStockItem = p.product_type === 'stock_item';
    const isSimple    = p.product_type === 'simple';
    const hasStock    = isStockItem || isSimple;

    // Use expandable stock-card layout for stock items; thin-card for everything else
    const card = document.createElement('div');
    card.className = hasStock ? 'stock-card' : 'product-thin-card';

    const typeLabel   = { simple: '', stock_item: '📦', recipe: '🍳' }[p.product_type] || '';
    const marginLabel = p.margin_pct != null ? ` • ${p.margin_pct}% margin` : '';

    let stockBadge = '';
    if (isStockItem) {
      const level   = displayQty(p.stock_level || 0, p.unit_type);
      const lowBadge = p.low_stock ? ' <span class="badge bg-warning text-dark">⚠ LOW</span>' : '';
      stockBadge = `<span class="badge bg-light text-dark ms-2">${level}</span>${lowBadge}`;
    } else if (isSimple) {
      stockBadge = `<span class="badge bg-light text-dark ms-2">Stock: ${p.stock_qty ?? 0}</span>`;
    }

    let priceDisplay = '';
    if (p.sold_by_weight && p.price_per_unit != null) {
      const bigUnit  = p.unit_type === 'volume' ? 'L' : 'kg';
      const conv     = UNITS[p.unit_type]?.toBase[bigUnit] || 1;
      const priceBig = parseFloat(p.price_per_unit) * conv;
      priceDisplay = `R${fmt(priceBig)}/${bigUnit}`;
    } else if (p.price != null) {
      priceDisplay = `R${fmt(p.price)}`;
    }

    const barcodeId = `bc-${p.id}`;
    const barcodeHtml = p.barcode ? `<svg id="${barcodeId}" class="product-barcode"></svg>` : '';

    if (hasStock) {
      // ── Expandable unified card ──
      const header = document.createElement('div');
      header.className = 'stock-card-header';
      header.innerHTML = `
        <div style="min-width:0;flex:1">
          <span class="fw-semibold">${p.name}</span>
          <span class="text-muted ms-1" style="font-size:11px">${typeLabel}</span>
          ${stockBadge}
          ${priceDisplay ? `<span class="text-success ms-2 fw-semibold">${priceDisplay}</span>` : ''}
          ${marginLabel ? `<span class="text-muted ms-2" style="font-size:11px">${marginLabel}</span>` : ''}
          ${barcodeHtml}
        </div>
        <div class="d-flex gap-1 align-items-center flex-wrap">
          ${isStockItem ? `
            <button class="btn btn-success btn-sm"         data-receive-id="${p.id}"   data-receive-name="${p.name}">Receive</button>
            <button class="btn btn-outline-warning btn-sm" data-stocktake-id="${p.id}" data-stocktake-name="${p.name}">Stocktake</button>
            <button class="btn btn-outline-danger btn-sm"  data-writeoff-id="${p.id}"  data-writeoff-name="${p.name}">Write Off</button>
          ` : ''}
          <button class="btn btn-outline-primary btn-sm"   data-edit-product>Edit</button>
          ${p.is_archived
            ? `<button class="btn btn-outline-success btn-sm" data-restore-product>Restore</button>`
            : `<button class="btn btn-outline-secondary btn-sm" data-archive-product>Archive</button>`}
          <span class="text-muted small">▾</span>
        </div>
      `;

      const body = document.createElement('div');
      body.className = 'stock-card-body';

      if (isStockItem) {
        const stockData = STATE._stockItems?.[p.id];
        if (stockData) {
          body.appendChild(_buildStockBody(stockData, p));
        } else {
          body.innerHTML = '<div class="small text-muted">Loading stock data…</div>';
        }
      }

      // Toggle body open/close on header click (not on button clicks)
      header.addEventListener('click', e => {
        if (e.target.closest('button')) return;
        body.classList.toggle('open');
      });

      // Wire stock action buttons
      const stockItem = STATE._stockItems?.[p.id] || { id: p.id, name: p.name, unit_type: p.unit_type, base_unit: p.base_unit, package_size: p.package_size, package_unit: p.package_unit, sell_packages: [], batches: [], stock_level: p.stock_level || 0 };
      header.querySelector('[data-receive-id]')?.addEventListener('click', e => { e.stopPropagation(); openReceiveStockModal(stockItem); });
      header.querySelector('[data-stocktake-id]')?.addEventListener('click', e => { e.stopPropagation(); openStocktakeModal(stockItem); });
      header.querySelector('[data-writeoff-id]')?.addEventListener('click', e => { e.stopPropagation(); openWriteoffModal(stockItem); });
      header.querySelector('[data-edit-product]')?.addEventListener('click', e => { e.stopPropagation(); openProductEditor(p); });
      header.querySelector('[data-archive-product]')?.addEventListener('click', e => { e.stopPropagation(); openArchiveModal(p); });
      header.querySelector('[data-restore-product]')?.addEventListener('click', e => { e.stopPropagation(); openRestoreModal(p); });

      card.appendChild(header);
      card.appendChild(body);

    } else {
      // ── Standard thin card for recipes, ingredients without stock ──
      const main = document.createElement('div');
      main.className = 'product-thin-main';
      main.innerHTML = `
        <div class="product-title">${p.name}
          <span class="badge bg-light text-dark ms-1" style="font-size:10px">${typeLabel}</span>
        </div>
        <div class="d-flex gap-3 align-items-center mt-1" style="flex-wrap:wrap">
          ${priceDisplay ? `<span class="fw-semibold text-success">${priceDisplay}</span>` : '<span class="text-muted small">no price</span>'}
          ${barcodeHtml}
          ${marginLabel ? `<span class="text-muted" style="font-size:12px">${marginLabel.replace(' • ','')}</span>` : ''}
        </div>
      `;

      const actions = document.createElement('div');
      actions.className = 'product-actions d-flex gap-1';
      const btnEdit = document.createElement('button');
      btnEdit.className = 'btn btn-outline-primary btn-sm'; btnEdit.textContent = 'Edit';
      btnEdit.onclick = () => openProductEditor(p);
      actions.appendChild(btnEdit);
      if (p.is_archived) {
        const btn = document.createElement('button');
        btn.className = 'btn btn-outline-success btn-sm'; btn.textContent = 'Restore';
        btn.onclick = () => openRestoreModal(p);
        actions.appendChild(btn);
      } else {
        const btn = document.createElement('button');
        btn.className = 'btn btn-outline-danger btn-sm'; btn.textContent = 'Archive';
        btn.onclick = () => openArchiveModal(p);
        actions.appendChild(btn);
      }
      card.appendChild(main); card.appendChild(actions);
    }

    wrap.appendChild(card);
  });

  // Store items for barcode rendering — will render when tab is visible
  wrap._pendingBarcodeItems = items;

  // Render immediately if tab is visible, otherwise defer to tab show event
  const productsPane = document.getElementById('products');
  if (productsPane && productsPane.classList.contains('active')) {
    _renderBarcodes(items);
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

    // Stock decision for stock_item products with remaining stock
    // Use live stock_level from the preview response — always authoritative
    const stockLevel  = data.stock_level || 0;
    const stockAction = p.product_type === 'stock_item' && stockLevel > 0
      ? `<div class="alert alert-info py-2 mb-3">
          <strong>📦 ${displayQty(stockLevel, p.unit_type)} remaining in stock.</strong> What should happen to it?
          <div class="mt-2">
            <div class="form-check">
              <input class="form-check-input" type="radio" name="archive-stock-action" id="stock-action-keep" value="keep" checked>
              <label class="form-check-label" for="stock-action-keep">Keep stock — still visible in Archived tab</label>
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
          ⚠ Used in ${affected.length} active recipe${affected.length>1?'s':''}. Choose what to do with each one.
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
                  <option value="">— select —</option>
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

  // Preview only — find which cascade-archived recipes could be restored alongside this product
  try {
    const restorable = _getRestorableRecipes(p);

    if (restorable.length === 0) {
      body.innerHTML = `<p>Restore <strong>${p.name}</strong> to the active catalogue?</p>
        <p class="text-muted small">No recipes need attention.</p>`;
    } else {
      let html = `<p>Restore <strong>${p.name}</strong>?</p>
        <p class="text-muted small">The following recipes were archived when this ingredient was archived. Tick the ones you want to restore too — only recipes where all other ingredients are active can be restored.</p>`;
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
        width:        1.4,
        height:       36,
        displayValue: true,
        fontSize:     10,
        margin:       2,
        lineColor:    '#222',
        background:   'transparent',
        textMargin:   2,
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

  const isSpecials = STATE.productsSubTab === 'specials';

  // Toggle specials vs product list
  const specialsList = document.getElementById('specials-list');
  const cardList     = document.getElementById('products-card-list');
  const newProduct   = document.getElementById('btn-new-product');
  const newSpecial   = document.getElementById('btn-new-special');
  const filterInput  = document.getElementById('products-filter');
  if (specialsList) specialsList.classList.toggle('hidden', !isSpecials);
  if (cardList)     cardList.style.display = isSpecials ? 'none' : '';
  if (newProduct)   newProduct.classList.toggle('hidden', isSpecials);
  if (newSpecial)   newSpecial.classList.toggle('hidden', !isSpecials);
  if (filterInput)  filterInput.classList.toggle('hidden', isSpecials);

  if (!isSpecials) {
    // Hide +New Product button on archived tab — you can't create archived products
    if (newProduct) newProduct.classList.toggle('hidden', STATE.productsSubTab === 'archived');
    renderProductsCards();
    // Reload ingredients data when switching to recipes sub-tab so costs are current
    if (STATE.productsSubTab === 'recipes') loadIngredients();
    setTimeout(() => {
      const wrap = document.getElementById('products-card-list');
      if (wrap?._pendingBarcodeItems) _renderBarcodes(wrap._pendingBarcodeItems);
    }, 50);
  } else {
    renderSpecialsList();
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
  document.getElementById('p-barcode').value   = p?.barcode ?? '';
  document.getElementById('p-stock').value     = p?.stock_qty ?? '';
  document.getElementById('p-type').value      = p?.product_type ?? 'stock_item';
  document.getElementById('p-unit-type').value = p?.unit_type ?? 'weight';
  document.getElementById('p-low-stock').value = p?.low_stock_threshold ?? '';
  document.getElementById('p-is-for-sale').checked   = p?.is_for_sale !== false;
  document.getElementById('p-is-prepared').checked  = !!p?.is_prepared;
  const purPid = document.getElementById('pur-product-id'); if (purPid) purPid.value = p?.id ?? '';

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

  document.getElementById('productEditorTitle').textContent = p ? `Edit — ${p.name}` : 'New Product';
  // Reset calculator
  hide(document.getElementById('calc-result'));
  initCalcMarkup(p);
  updateProductTypeSections(p?.product_type ?? 'stock_item');

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
  const isEdit = !!p?.id;
  document.getElementById('btn-add-product')   ?.classList.toggle('hidden', isEdit);
  document.getElementById('btn-update-product') ?.classList.toggle('hidden', !isEdit);
  document.getElementById('btn-delete-product') ?.classList.toggle('hidden', !isEdit);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).show();
}

document.getElementById('btn-new-product')?.addEventListener('click', () => {
  const nextId = nextLocalId();
  openProductEditor(null);
  document.getElementById('p-id').value      = String(nextId);
  document.getElementById('p-barcode').value = genBarcode(nextId);
});

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
    toast('This product will be moved to the Ingredients tab — it stays available for recipes but won\'t appear at the till.', 'info', 4000);
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
  const isSimple    = type === 'simple';

  const el = id => document.getElementById(id);

  // Show unit-type selector early (before price) only for stock items
  isStockItem ? show(el('section-unit-type-early')) : hide(el('section-unit-type-early'));

  isStockItem ? show(el('section-stock-item')) : hide(el('section-stock-item'));
  isRecipe    ? show(el('section-recipe'))     : hide(el('section-recipe'));
  isSimple    ? show(el('row-stock-qty'))      : hide(el('row-stock-qty'));
  isSimple    ? show(el('section-purchase'))   : hide(el('section-purchase'));
  isStockItem ? show(el('is-for-sale-row'))    : hide(el('is-for-sale-row'));

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
    // Sum cost of each recipe line × qty (qty = number of portions for a recipe ingredient)
    const lines = p.recipe_lines || [];
    return lines.reduce((sum, rl) => {
      return sum + getIngredientCost(rl.ingredient_id, rl.qty_base * qty, _depth + 1);
    }, 0);
  }
  return 0;
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

    // Calculate line cost — works for both stock_item and recipe ingredients
    const qty      = parseFloat(line.qty_base_display || line.qty_base || 0);
    const unit     = line.unit || baseUnit;
    const qtyBase  = ingr?.product_type === 'recipe' ? qty : toBase(qty, unit, unitType);
    const lineCost = getIngredientCost(ingr?.id, qtyBase);
    totalCost += lineCost;

    const tr = document.createElement('tr');
    tr.className = 'recipe-line-row';

    // Ingredient selector — stock items AND other recipes (for bundles/specials)
    const currentProductId = parseInt(document.getElementById('p-id')?.value || '0');
    let ingSelHTML = `<select class="form-select form-select-sm" data-rl-idx="${idx}" data-rl-field="ingredient_id">`;
    ingSelHTML += '<option value="">— select —</option>';
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

    // Unit selector for this ingredient (hidden for recipe ingredients — qty means "portions")
    const isRecipeIngredient = ingr?.product_type === 'recipe';
    let unitSelHTML = `<select class="form-select form-select-sm" data-rl-idx="${idx}" data-rl-field="unit" style="width:auto" ${isRecipeIngredient ? 'disabled' : ''}>`;
    unitOpts.forEach(u => { unitSelHTML += `<option value="${u}" ${u === (line.unit || baseUnit) ? 'selected' : ''}>${u}</option>`; });
    unitSelHTML += '</select>';

    tr.innerHTML = `
      <td>${ingSelHTML}</td>
      <td><input type="number" step="0.01" min="0.01" value="${line.qty_base_display || line.qty_base || ''}" class="form-control form-control-sm" data-rl-idx="${idx}" data-rl-field="qty_display" style="width:80px"></td>
      <td>${unitSelHTML}</td>
      <td class="small text-muted">${lineCost > 0 ? `R${lineCost.toFixed(4)}` : '—'}</td>
      <td><button class="btn btn-outline-danger btn-sm" data-rl-remove="${idx}">✕</button></td>
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
        // Update only the cost cell — avoid full re-render which destroys focus
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
          if (costCell) costCell.textContent = cost > 0 ? `R${cost.toFixed(4)}` : '—';
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
        // Recipe ingredient: qty is "portions" — no unit conversion needed
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
    if (totalCost === 0) return toast('No stock prices found — receive stock for ingredients first', 'warning');
    const suggestedPrice = totalCost / (1 - markup / 100);
    show(resultEl);
    avgCostEl.textContent   = `R${totalCost.toFixed(4)}`;
    suggestedEl.textContent = `→ R${fmt(suggestedPrice)} at ${markup}% margin`;
    document.getElementById('btn-calc-apply').dataset.price = suggestedPrice.toFixed(2);
    breakdownEl.innerHTML = '';
    breakdown.forEach(l => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="text-muted small">${l.label}</td><td class="text-end small">R${l.line_cost.toFixed(4)}</td>`;
      breakdownEl.appendChild(tr);
    });
    const sEl = document.getElementById('calc-suggestions-row');
    if (sEl) sEl.innerHTML = [20,30,40,50,60,70].map(pct => {
      const p = totalCost / (1 - pct/100);
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
    suggestedEl.textContent = `→ R${fmt(j.suggested_price)} at ${markup}% margin`;
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
      <td><button class="btn btn-outline-danger btn-sm" data-pkg-remove="${idx}">✕</button></td>
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

// ── Save / Update / Delete ──
document.getElementById('btn-add-product')?.addEventListener('click', async () => {
  const payload = buildProductPayload();
  if (!payload) return;
  try {
    const result = await api('/api/products', { method: 'POST', body: JSON.stringify(payload) });
    await loadProducts();
    toast('Product added');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
    // If opened from a purchase run line, auto-select the new product in that line
    if (_pendingPurchaseLine && result?.id) {
      const supplierProductIds = new Set((_currentSupplierProducts || []).map(p => p.id));
      const sel = _pendingPurchaseLine.querySelector('[data-product-select]');
      if (sel) {
        sel.innerHTML = _buildProductOptions(supplierProductIds);
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
  if (!payload) return;
  payload.id = id;

  // Detect is_for_sale change for a meaningful toast
  const prev = STATE.products.find(p => p.id === id);
  const wasForSale = prev?.is_for_sale !== false;
  const nowForSale = payload.is_for_sale !== false;

  try {
    await api('/api/products/update', { method: 'POST', body: JSON.stringify(payload) });
    await loadProducts();
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
    toast('Product deleted');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('productEditorModal')).hide();
  } catch (e) {
    // Deletion blocked — offer to hide instead
    if (e.message.includes('historical references')) {
      if (confirm(`"${name}" has historical records and cannot be deleted.\n\nHide it instead? (It will no longer appear at the till or in active lists, but history is preserved.)`)) {
        try {
          await api('/api/products/update', { method: 'POST', body: JSON.stringify({ id, is_for_sale: false }) });
          await loadProducts();
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
  const barcode      = document.getElementById('p-barcode').value.trim() || null;
  const priceVal     = document.getElementById('p-price').value;
  const price        = priceVal !== '' ? parseFloat(priceVal) : null;
  const stock_qty    = parseInt(document.getElementById('p-stock').value || '0', 10);
  const unitType     = document.getElementById('p-unit-type').value;
  const lowStock     = document.getElementById('p-low-stock').value || null;
  const pkgSize      = document.getElementById('p-pkg-size').value || null;
  const pkgSizeUnit  = document.getElementById('p-pkg-size-unit')?.value || null;
  const pkgUnit      = document.getElementById('p-pkg-unit').value?.trim() || null;
  const isForSale    = document.getElementById('p-is-for-sale').checked;
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

  return {
    name, barcode,
    price:       finalPrice,
    // Only send stock_qty for simple products — other types track stock differently
    ...(type === 'simple' ? { stock_qty } : {}),
    product_type: type,
    unit_type:    type !== 'simple' ? unitType : null,
    is_for_sale:  isForSale,
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
    recipe_lines:  type === 'recipe'     ? getRecipeLinesForSubmit()  : [],
    sell_packages: type === 'stock_item' ? getSellPackagesForSubmit() : [],
  };
}

// ── Legacy purchase (simple products) ──
document.getElementById('btn-add-purchase')?.addEventListener('click', async () => {
  const pid   = parseInt(document.getElementById('p-id').value || '0', 10);
  const qty   = parseInt(document.getElementById('pur-qty').value || '0', 10);
  const price = parseFloat(document.getElementById('pur-price').value || '0');
  try {
    await api('/api/purchases', { method: 'POST', body: JSON.stringify({ product_id: pid, qty_added: qty, purchase_price: price }) });
    await loadProducts();
    toast('Purchase recorded, stock updated');
  } catch (e) { toast(e.message, 'error'); }
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
  if (STATE.user?.role !== 'admin') return;
  try {
    const data = await api('/api/stock/ingredients');
    STATE._stockCostMap = {};
    STATE._stockItems   = {};
    data.forEach(item => {
      STATE._stockItems[item.id] = item;
      const oldestWithStock = item.batches
        .slice()
        .sort((a, b) => new Date(a.purchased_at) - new Date(b.purchased_at))
        .find(b => b.qty_remaining_base > 0);
      if (oldestWithStock) STATE._stockCostMap[item.id] = oldestWithStock.cost_per_base_unit;
    });
    // Refresh any already-rendered product cards so stock levels update
    renderProductsCards();
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
    table.innerHTML = `
      <thead class="table-light">
        <tr>
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

    [...item.batches].reverse().forEach(b => {
      const remaining  = displayQty(b.qty_remaining_base, item.unit_type);
      const purchased  = displayQty(b.qty_purchased_base, item.unit_type);
      const date       = new Date(b.purchased_at).toLocaleDateString('en-ZA');
      const supplier   = b.supplier_name || '—';
      const stockValue = (b.cost_per_base_unit * b.qty_remaining_base);
      const totalCost  = (b.cost_per_base_unit * b.qty_purchased_base).toFixed(2);
      const { cost: costPerDisplay, unit: displayUnit } = displayCost(b.cost_per_base_unit, b.qty_remaining_base, item.unit_type);
      const cogsStr    = `R${costPerDisplay < 0.01 ? costPerDisplay.toFixed(4) : costPerDisplay.toFixed(2)}/${displayUnit}`;

      // Sale value per display unit
      let saleStr = '—';
      if (prod) {
        if (prod.sold_by_weight && prod.price_per_unit != null) {
          const { cost: salePer, unit: saleUnit } = displayCost(parseFloat(prod.price_per_unit), b.qty_remaining_base, item.unit_type);
          saleStr = `R${fmt(salePer)}/${saleUnit}`;
        } else if (prod.price != null) {
          saleStr = `R${fmt(prod.price)}`;
        }
      }

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${date}</td>
        <td>${b.supplier_name ? `<span class="badge bg-info text-dark">${supplier}</span>` : `<span class="text-muted">${supplier}</span>`}</td>
        <td class="text-end">${purchased}</td>
        <td class="text-end"><strong>${remaining}</strong></td>
        <td class="text-end text-muted">R${fmt(stockValue)}</td>
        <td class="text-end text-muted">${cogsStr}</td>
        <td class="text-end text-success">${saleStr}</td>
        <td class="text-end">
          <button class="btn btn-outline-secondary btn-sm py-0 px-1"
            data-edit-batch-id="${b.id}"
            data-edit-batch-date="${b.purchased_at.slice(0,10)}"
            data-edit-batch-supplier="${b.supplier_id || ''}"
            data-edit-batch-total="${totalCost}"
            data-edit-batch-qty="${purchased}">✏️</button>
        </td>`;
      tbody.appendChild(tr);
    });

    wrap.appendChild(table);
  } else {
    wrap.innerHTML = '<div class="small text-muted pb-2">No stock received yet.</div>';
  }

  if (item.sell_packages?.length > 0) {
    const pkgDiv = document.createElement('div');
    pkgDiv.innerHTML = `<div class="small fw-bold mb-1 text-muted">Packages:</div>`;
    item.sell_packages.forEach(pkg => {
      pkgDiv.innerHTML += `<div class="small">• ${pkg.name} — ${displayQty(pkg.qty_base, item.unit_type)} @ R${fmt(pkg.price || 0)}</div>`;
    });
    wrap.appendChild(pkgDiv);
  }
  return wrap;
}

function renderStockList(items) {
  // kept for backward compat — no longer used for display, data goes via STATE._stockItems
}

// ── Edit Batch (delegated off products-card-list since batch rows are dynamic) ──
document.getElementById('products-card-list')?.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-edit-batch-id]');
  if (!btn) return;
  e.stopPropagation();
  const batchId      = btn.dataset.editBatchId;
  const date         = btn.dataset.editBatchDate;
  const supplierId   = btn.dataset.editBatchSupplier;
  const total        = btn.dataset.editBatchTotal;
  const qtyLabel     = btn.dataset.editBatchQty;

  document.getElementById('edit-batch-id').value          = batchId;
  document.getElementById('edit-batch-date').value        = date;
  document.getElementById('edit-batch-total-price').value = total;
  document.getElementById('edit-batch-qty-label').textContent = qtyLabel ? `for ${qtyLabel}` : '';

  const sel = document.getElementById('edit-batch-supplier');
  sel.innerHTML = '<option value="">— No supplier —</option>';
  (_suppliers || []).forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.id; opt.textContent = s.name;
    if (String(s.id) === String(supplierId)) opt.selected = true;
    sel.appendChild(opt);
  });

  bootstrap.Modal.getOrCreateInstance(document.getElementById('editBatchModal')).show();
});

document.getElementById('btn-edit-batch-confirm')?.addEventListener('click', async () => {
  const batchId    = document.getElementById('edit-batch-id').value;
  const supplierId = document.getElementById('edit-batch-supplier').value || null;
  const date       = document.getElementById('edit-batch-date').value;
  const totalPrice = parseFloat(document.getElementById('edit-batch-total-price').value || '0');

  if (!totalPrice || totalPrice <= 0) return toast('Enter a valid total price', 'warning');

  try {
    await api(`/api/stock/batches/${batchId}`, {
      method: 'PATCH',
      body: JSON.stringify({
        supplier_id:  supplierId ? parseInt(supplierId) : null,
        purchased_at: date,
        total_price:  totalPrice,
      }),
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editBatchModal')).hide();
    toast('Batch updated', 'success', 2000);
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
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
    const cpu = totalPrice / qty_base;
    const { cost: cpuDisplay, unit: cpuUnit } = displayCost(cpu, qty_base, unitType);
    show(preview);
    preview.textContent = `Cost: R${cpuDisplay < 0.01 ? cpuDisplay.toFixed(4) : cpuDisplay.toFixed(2)}/${cpuUnit}`;
  } else {
    hide(preview);
  }
}

document.getElementById('receive-qty')?.addEventListener('input', updateReceivePreview);
document.getElementById('receive-total-price')?.addEventListener('input', updateReceivePreview);
document.getElementById('receive-unit')?.addEventListener('change', updateReceivePreview);

document.getElementById('btn-receive-confirm')?.addEventListener('click', async () => {
  const pid        = parseInt(document.getElementById('receive-product-id').value || '0', 10);
  const qty        = parseFloat(document.getElementById('receive-qty').value || '0');
  const totalPrice = parseFloat(document.getElementById('receive-total-price').value || '0');
  const unitSel    = document.getElementById('receive-unit');
  const unit       = unitSel.value;

  if (!pid || qty <= 0)         return toast('Enter a valid quantity', 'warning');
  if (totalPrice <= 0)          return toast('Enter the total amount paid', 'warning');

  const supplier_id = parseInt(document.getElementById('receive-supplier')?.value || '0') || null;

  try {
    const j = await api('/api/stock/receive', {
      method: 'POST',
      body: JSON.stringify({ product_id: pid, qty, unit, total_price: totalPrice, supplier_id })
    });
    toast(`Stock received — R${j.cost_per_base_unit}/unit (${j.qty_base.toFixed(2)} ${j.base_unit})`, 'success', 4000);
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
  removeBtn.textContent = '✕';
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

  // Show running total when multiple rows
  const rowCount = document.querySelectorAll('.stocktake-row').length;
  if (rowCount > 1 && actual > 0) {
    show(totEl);
    totEl.textContent = `Total counted: ${displayQty(actual, _stocktakeItem.unit_type)}`;
  } else {
    hide(totEl);
  }

  if (actual === 0) { hide(preview); return; }

  show(preview);
  if (Math.abs(diff) < 0.001) {
    preview.className = 'alert alert-success py-2 small';
    preview.textContent = '✓ Matches system — no adjustment needed';
  } else if (diff < 0) {
    preview.className = 'alert alert-warning py-2 small';
    preview.textContent = `⚠ System will deduct ${displayQty(Math.abs(diff), _stocktakeItem.unit_type)} (unexplained loss)`;
  } else {
    preview.className = 'alert alert-info py-2 small';
    preview.textContent = `ℹ System will add ${displayQty(diff, _stocktakeItem.unit_type)} (found more than expected)`;
  }
}

function openStocktakeModal(item) {
  _stocktakeItem = item;
  document.getElementById('stocktake-product-id').value = item.id;
  document.getElementById('stocktake-product-name').textContent = item.name;
  document.getElementById('stocktake-reason').value = '';
  hide(document.getElementById('stocktake-diff-preview'));
  hide(document.getElementById('stocktake-total-preview'));

  document.getElementById('stocktake-system-qty').textContent = displayQty(item.stock_level || 0, item.unit_type);

  // Reset rows — start with one blank row using base unit
  document.getElementById('stocktake-rows').innerHTML = '';
  _addStocktakeRow(UNITS[item.unit_type]?.base || 'unit');

  bootstrap.Modal.getOrCreateInstance(document.getElementById('stocktakeModal')).show();
}

document.getElementById('btn-stocktake-add-row')?.addEventListener('click', () => {
  if (!_stocktakeItem) return;
  // Default new rows to package unit if available, else base unit
  const defaultUnit = _stocktakeItem.package_unit
    ? _stocktakeItem.package_unit
    : (UNITS[_stocktakeItem.unit_type]?.base || 'unit');
  _addStocktakeRow(defaultUnit);
});

document.getElementById('btn-stocktake-confirm')?.addEventListener('click', async () => {
  const pid    = parseInt(document.getElementById('stocktake-product-id').value || '0', 10);
  const reason = document.getElementById('stocktake-reason').value.trim();
  const total  = _stocktakeTotalBase();

  if (total < 0)  return toast('Enter the actual quantity counted', 'warning');
  if (!reason)    return toast('Please enter a reason or note', 'warning');

  // Send total in base units directly
  const baseUnit = _stocktakeItem?.base_unit || UNITS[_stocktakeItem?.unit_type]?.base || 'unit';
  try {
    const j = await api('/api/stock/adjust', {
      method: 'POST',
      body: JSON.stringify({ product_id: pid, actual_qty: total, unit: baseUnit, reason })
    });
    const diffDisplay = displayQty(Math.abs(j.difference), _stocktakeItem?.unit_type);
    const msg = j.difference === 0
      ? 'No change — stock levels match'
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
  document.getElementById('writeoff-product-id').value   = item.id;
  document.getElementById('writeoff-product-name').textContent = item.name;
  document.getElementById('writeoff-qty').value          = '';
  document.getElementById('writeoff-reason').value       = '';
  hide(document.getElementById('writeoff-cost-preview'));
  document.getElementById('writeoff-available').textContent = displayQty(item.stock_level || 0, item.unit_type);

  // Unit dropdown
  const unitSel = document.getElementById('writeoff-unit');
  unitSel.innerHTML = '';
  buildUnitOptions(item.unit_type, item.package_size, item.package_unit).forEach(o => {
    const opt = document.createElement('option');
    opt.value = o.value; opt.textContent = o.label; opt.dataset.conv = o.conv;
    unitSel.appendChild(opt);
  });

  bootstrap.Modal.getOrCreateInstance(document.getElementById('writeoffModal')).show();
  setTimeout(() => document.getElementById('writeoff-qty').focus(), 300);
}

function updateWriteoffPreview() {
  if (!_writeoffItem) return;
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
    toast(
      `Written off: ${displayQty(j.qty_written_off, _writeoffItem?.unit_type)} — Cost: R${j.cost_written_off.toFixed(4)}`,
      'warning', 5000
    );
    bootstrap.Modal.getOrCreateInstance(document.getElementById('writeoffModal')).hide();
    await loadIngredients();
    await loadProducts();
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// SUPPLIERS
// ═══════════════════════════════════════════════════════
let _suppliers = [];
let _editingSupplierId = null;
let _currentSupplier = null;
let _purchaseRunLines = [];
let _currentSupplierProducts = [];

async function loadSuppliers() {
  if (STATE.user?.role !== 'admin') return;
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
      s.phone   ? `📞 ${s.phone}`   : '',
      s.email   ? `✉ ${s.email}`   : '',
      s.website ? `🌐 ${s.website}` : '',
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

  // Detail replaces the form — form only shows when adding/editing
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
}

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
            <td class="small text-muted">${p.last_received || '—'}</td>
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

function populateSupplierDropdowns() {
  // Receive stock modal dropdown
  const sel = document.getElementById('receive-supplier');
  if (sel) {
    const prev = sel.value;
    sel.innerHTML = '<option value="">— No supplier —</option>';
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
  document.getElementById('supplier-form-title').textContent = `Edit — ${_currentSupplier.name}`;
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
});

document.getElementById('btn-cancel-purchase-run')?.addEventListener('click', () => {
  hide(document.getElementById('purchase-run-panel'));
  _purchaseRunLines = [];
});

document.getElementById('btn-add-purchase-line')?.addEventListener('click', addPurchaseLine);

// Track which purchase line is waiting for a new product to be created
let _pendingPurchaseLine = null;

function _buildProductOptions(supplierProductIds) {
  // Supplier's own products first (sorted by name), then the rest
  const active = STATE.products.filter(p => !p.is_archived);
  const own    = active.filter(p => supplierProductIds.has(p.id));
  const rest   = active.filter(p => !supplierProductIds.has(p.id));
  const sep    = own.length ? `<option disabled>── Other products ──</option>` : '';
  const opts   = (arr) => arr.map(p => `<option value="${p.id}">${p.name} (${p.product_type})</option>`).join('');
  return `<option value="">— Select product —</option>${opts(own)}${sep}${opts(rest)}`;
}

function addPurchaseLine() {
  const container = document.getElementById('purchase-run-lines');
  if (!container) return;

  const supplierProductIds = new Set(
    (_currentSupplierProducts || []).map(p => p.id)
  );

  const line = document.createElement('div');
  line.className = 'border rounded p-2 mb-2';
  line.dataset.lineId = Date.now() + Math.random();

  line.innerHTML = `
    <div class="d-flex gap-2 align-items-center mb-2">
      <span class="small fw-semibold text-muted">Item</span>
      <button type="button" class="btn btn-outline-secondary btn-sm ms-auto" data-create-product-btn>+ Create New Product</button>
      <button class="btn btn-sm btn-outline-danger" data-remove-line>✕</button>
    </div>
    <div class="mb-2">
      <select class="form-select form-select-sm" data-product-select>
        ${_buildProductOptions(supplierProductIds)}
      </select>
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
  `;

  container.appendChild(line);

  const unitSel = line.querySelector('[data-unit]');
  const UNIT_OPTIONS = {
    weight: [['g','g'],['kg','kg']],
    volume: [['ml','ml'],['L','L']],
    count:  [['unit','unit']],
    simple: [['unit','unit']],
  };

  function updateUnitsForProduct(pid) {
    const p = STATE.products.find(pr => pr.id === parseInt(pid));
    const type = p ? (p.product_type === 'simple' ? 'simple' : p.unit_type || 'count') : null;
    const opts = UNIT_OPTIONS[type] || [['unit','unit'],['g','g'],['kg','kg'],['ml','ml'],['L','L']];
    unitSel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  }

  line.querySelector('[data-product-select]')?.addEventListener('change', e => {
    updateUnitsForProduct(e.target.value);
  });

  // "Create New Product" — open the full product editor modal and come back
  line.querySelector('[data-create-product-btn]')?.addEventListener('click', () => {
    _pendingPurchaseLine = line;
    openProductEditor(null);
  });

  line.querySelector('[data-remove-line]')?.addEventListener('click', () => line.remove());
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

  const dateVal = document.getElementById('purchase-run-date')?.value;
  const body = { lines, date: dateVal || todayISO() };

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
      const typeLabel  = isWriteoff ? '🗑 Write-off' : '📋 Stocktake';
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
          ${isWriteoff ? `<button class="btn btn-outline-secondary btn-sm mt-1 py-0 px-2" data-editwo-id="${r.id}" data-editwo-product="${r.product_name}" data-editwo-qty="${Math.abs(r.qty_change_base)}" data-editwo-unit="${r.base_unit}" data-editwo-reason="${r.reason}" data-editwo-unit-type="">✏️ Edit</button>` : ''}
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
    preview.textContent = '✓ Same quantity — no change to stock';
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

// ═══════════════════════════════════════════════════════
// CART
// ═══════════════════════════════════════════════════════
function renderCart() {
  const host = document.getElementById('cart'); if (!host) return;
  host.innerHTML = '';
  let total = 0;
  Object.values(STATE.cart).forEach(item => {
    const row = document.createElement('div');
    row.className = 'list-group-item d-flex justify-content-between align-items-center';

    const label = item.is_weight
      ? `${item.name}`
      : `${item.name} × ${fmtQty(item.qty)}`;

    const left  = document.createElement('span'); left.textContent = label;
    const displayPrice = item.is_weight ? item._display_total : item.unit_price;
    const mid   = document.createElement('span'); mid.textContent  = `R${fmt(displayPrice)}`;
    const btns  = document.createElement('div');

    if (!item.is_weight) {
      const p = STATE.products.find(pr => pr.id === item.product_id);
      // For customised items use per-unit price snapshot; for plain items scale with base price
      const pricePerUnit = (item.subs || item.extras?.length)
        ? item.unit_price  // already the total for qty=1; keep it fixed
        : parseFloat(p?.price || 0);

      const plus  = document.createElement('button'); plus.textContent = '+'; plus.className = 'btn btn-sm btn-outline-primary';
      plus.onclick = () => {
        item.qty += 1;
        if (!(item.subs || item.extras?.length)) item.unit_price = pricePerUnit * item.qty;
        item._special_applied = null;  // reset so special can re-evaluate
        STATE.scanHistory.push(item.product_id);
        reapplySpecials();
      };
      const minus = document.createElement('button'); minus.textContent = '−'; minus.className = 'btn btn-sm btn-outline-secondary ms-1';
      minus.onclick = () => {
        item.qty = Math.max(1, item.qty - 1);
        if (!(item.subs || item.extras?.length)) item.unit_price = pricePerUnit * item.qty;
        item._special_applied = null;
        reapplySpecials();
      };
      btns.appendChild(plus); btns.appendChild(minus);

      // Customise button — only for recipe products
      if (p?.product_type === 'recipe') {
        const cust = document.createElement('button');
        cust.textContent = 'Customise';
        cust.className = 'btn btn-sm btn-outline-info ms-1';
        cust.onclick = () => openSubsModal(p, item._key);
        btns.appendChild(cust);
      }
    }

    const del = document.createElement('button'); del.textContent = 'Remove'; del.className = 'btn btn-sm btn-outline-danger ms-1';
    del.onclick = () => { delete STATE.cart[item._key]; renderCart(); };
    btns.appendChild(del);

    row.appendChild(left); row.appendChild(mid); row.appendChild(btns);
    host.appendChild(row);
    total += item.is_weight ? parseFloat(item._display_total || 0) : parseFloat(item.unit_price);
  });
  const t = document.getElementById('cart-total');
  if (t) t.textContent = fmt(total);
}

function addToCart(p) {
  if (p.sold_by_weight) {
    openWeightModal(p);
    return;
  }
  // Fixed price product (recipes included — customise button in cart)
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
    // Customised entry already exists — add a fresh uncustomised one with unique key
    const newKey = `${p.id}__${Date.now()}`;
    STATE.cart[newKey] = {
      _key: newKey, product_id: p.id, name: p.name,
      unit_price: parseFloat(p.price || 0), qty: 1,
      is_weight: false
    };
    toast(`Added: ${p.name}`, 'success', 1200);
  }
  STATE.scanHistory.push(p.id);
  renderCart();
  detectAndOfferSpecials();
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

// ── Checkout ──
document.getElementById('btn-checkout')?.addEventListener('click', async () => {
  const cart = Object.values(STATE.cart);
  if (cart.length === 0) return toast('Cart is empty', 'warning');
  const payload = cart.map(item => ({
    product_id: item.product_id,
    qty:        item.qty,
    unit_price: item.unit_price,
    ...(item.subs   ? { subs:   item.subs   } : {}),
    ...(item.extras ? { extras: item.extras } : {}),
  }));
  try {
    const j = await api('/api/transactions', { method: 'POST', body: JSON.stringify({ cart: payload }) });
    STATE.cart = {}; STATE.scanHistory = []; renderCart();
    await loadTransactions();
    await loadProducts();
    const kitchenMsg = j.kitchen_orders > 0 ? ` — ${j.kitchen_orders} kitchen order${j.kitchen_orders > 1 ? 's' : ''} queued` : '';
    toast(`Sale complete — #${String(j.transaction_id).slice(0,8)}${kitchenMsg}`, 'success', 4000);
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

// ═══════════════════════════════════════════════════════
// WEIGHT ENTRY MODAL
// ═══════════════════════════════════════════════════════
let _weightProduct = null;

function openWeightModal(p) {
  _weightProduct = p;
  document.getElementById('weight-modal-title').textContent = p.name;
  document.getElementById('weight-qty').value                = '';
  document.getElementById('weight-price-preview').textContent = '';

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
    unit_price: pricePerUnit,   // price per base unit — backend multiplies by qty
    qty:        qty_base,
    is_weight:  true,
    _display_total: total,      // for cart display only
  };
  STATE.scanHistory.push(_weightProduct.id);
  renderCart();
  detectAndOfferSpecials();
  toast(`Added: ${label} — R${fmt(total)}`, 'success', 1500);
  bootstrap.Modal.getOrCreateInstance(document.getElementById('weightModal')).hide();
});

// ═══════════════════════════════════════════════════════
// TELLER SEARCH
// ═══════════════════════════════════════════════════════
document.getElementById('search')?.addEventListener('input', function() {
  const q    = this.value.trim().toLowerCase();
  const host = document.getElementById('product-search-results'); if (!host) return;
  host.innerHTML = '';
  if (!q) return;
  STATE.products.filter(p =>
    p.is_for_sale !== false && !p.is_archived && (
      p.name.toLowerCase().includes(q) || String(p.id) === q || (p.barcode === q)
    )
  ).forEach(p => {
    const a = document.createElement('a'); a.className = 'list-group-item list-group-item-action';
    const stockInfo = p.product_type === 'stock_item'
      ? ` (${displayQty(p.stock_level || 0, p.unit_type)})`
      : p.product_type === 'simple' ? ` (stock ${p.stock_qty})` : '';
    a.textContent = `#${p.id} ${p.name}${p.price != null ? ` — R${fmt(p.price)}` : ''}${stockInfo}`;
    a.onclick = () => {
      addToCart(p);
      this.value = '';
      host.innerHTML = '';
    };
    host.appendChild(a);
  });
});

// ═══════════════════════════════════════════════════════
// USB / BLUETOOTH BARCODE SCANNER (keyboard wedge)
// ═══════════════════════════════════════════════════════
let _scanBuffer = '', _scanBufferTimer = null;

document.addEventListener('keydown', (e) => {
  const active = document.querySelector('.tab-pane.active');
  if (!active || active.id !== 'teller') return;
  if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName)) return;
  if (!STATE.user) return;

  if (e.key === 'Enter') {
    const code = _scanBuffer.trim();
    _scanBuffer = ''; clearTimeout(_scanBufferTimer);
    if (code.length < 3) return;
    const p = STATE.products.find(x => x.barcode === code)
           || STATE.products.find(x => String(x.id) === code)
           || STATE.products.find(x => x.name.toLowerCase() === code.toLowerCase());
    if (p) { beep(80, 880); flashOK(); addToCart(p); }
    else toast(`Barcode not found: ${code}`, 'warning');
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
      const p = STATE.products.find(x => x.barcode === code)
             || STATE.products.find(x => String(x.id) === code)
             || STATE.products.find(x => x.name.toLowerCase() === code.toLowerCase());
      if (p) addToCart(p);
      else toast(`Barcode not found: ${code}`, 'warning');
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
// TRANSACTIONS
// ═══════════════════════════════════════════════════════
function initTxDatePickers() {
  const t = todayISO();
  const s = document.getElementById('tx-start'); if (s && !s.value) s.value = t;
  const e = document.getElementById('tx-end');   if (e && !e.value) e.value = t;
}

async function loadTransactions(start, end) {
  if (!STATE.user) return;
  try {
    let url = '/api/transactions';
    if (STATE.user.role === 'admin' && (start || end)) {
      const p = new URLSearchParams();
      if (start) p.set('start', start);
      if (end)   p.set('end', end);
      url += '?' + p.toString();
    }
    const trs = await api(url);
    renderTransactions(trs);
  } catch (e) { console.error('loadTransactions', e); }
}

function renderTransactions(trs) {
  const host = document.getElementById('transactions-list'); if (!host) return;
  host.innerHTML = '';
  if (trs.length === 0) {
    host.innerHTML = '<div class="text-muted">No transactions found.</div>';
    return;
  }
  trs.forEach(t => {
    const card = document.createElement('div');
    card.className = 'card mb-2';
    const body = document.createElement('div');
    body.className = 'card-body py-2';

    const header = document.createElement('div');
    header.className = 'd-flex justify-content-between align-items-start flex-wrap gap-1';

    const left = document.createElement('div');
    left.innerHTML = `
      <strong>#${String(t.id).slice(0,8)}</strong>
      <span class="text-muted small ms-1">${new Date(t.date_time).toLocaleString('en-ZA')}</span>
      ${t.teller ? `<span class="badge bg-secondary" style="font-size:11px">${t.teller}</span>` : ''}
    `;

    const right = document.createElement('div');
    right.className = 'd-flex align-items-center gap-2 flex-wrap';
    let summaryHTML = `<strong>R${fmt(t.total)}</strong>`;
    if (STATE.user?.role === 'admin') {
      if (t.cogs != null)       summaryHTML += ` <span class="small text-success">COGS R${fmt(t.cogs)}</span>`;
      if (t.margin_pct != null) summaryHTML += ` <span class="small text-success">${t.margin_pct}% margin</span>`;
    }
    if (t.flagged && !t.flag_resolved) summaryHTML += ` <span class="badge bg-warning text-dark">⚑ Flagged</span>`;
    if (t.flagged && t.flag_resolved)  summaryHTML += ` <span class="badge bg-secondary">✓ Reviewed</span>`;
    right.innerHTML = summaryHTML;

    // Flag button — all users
    const btnFlag = document.createElement('button');
    btnFlag.className = t.flagged && !t.flag_resolved
      ? 'btn btn-warning btn-sm'
      : 'btn btn-outline-warning btn-sm';
    btnFlag.textContent = t.flagged && !t.flag_resolved ? '⚑ Flagged' : '⚑ Flag';
    btnFlag.onclick = () => openFlagModal(t);
    right.appendChild(btnFlag);

    if (STATE.user?.role === 'admin') {
      const btnMgr = document.createElement('button');
      btnMgr.className = 'btn btn-outline-secondary btn-sm';
      btnMgr.textContent = 'Edit / Void';
      btnMgr.onclick = () => openTxModal(t);
      right.appendChild(btnMgr);
      // Resolve flag button
      if (t.flagged && !t.flag_resolved) {
        const btnResolve = document.createElement('button');
        btnResolve.className = 'btn btn-outline-success btn-sm';
        btnResolve.textContent = '✓ Resolve';
        btnResolve.onclick = () => resolveFlag(t.id);
        right.appendChild(btnResolve);
      }
    }

    header.appendChild(left); header.appendChild(right);
    body.appendChild(header);

    const ul = document.createElement('ul'); ul.className = 'mt-1 mb-0 small';
    t.lines.forEach(ln => {
      const li = document.createElement('li');
      li.textContent = `${ln.name} × ${fmtQty(ln.qty)} @ R${fmt(ln.unit_price)} = R${fmt(ln.subtotal)}`;
      ul.appendChild(li);
    });
    body.appendChild(ul);

    // Show flag note if flagged
    if (t.flagged && t.flag_note) {
      const flagDiv = document.createElement('div');
      flagDiv.className = `mt-1 small px-2 py-1 rounded ${t.flag_resolved ? 'bg-light text-muted' : 'bg-warning bg-opacity-25'}`;
      flagDiv.innerHTML = `<strong>${t.flag_resolved ? '✓ Reviewed' : '⚑ Note'}:</strong> ${t.flag_note}`;
      body.appendChild(flagDiv);
    }

    card.appendChild(body);
    host.appendChild(card);
  });
}

document.getElementById('btn-refresh-trans')?.addEventListener('click', () => {
  if (STATE.user?.role === 'admin') {
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
  } else {
    loadTransactions();
  }
});
document.getElementById('btn-tx-filter')?.addEventListener('click', () => {
  loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
});
document.getElementById('btn-tx-today')?.addEventListener('click', () => {
  const t = todayISO();
  const s = document.getElementById('tx-start'); if (s) s.value = t;
  const e = document.getElementById('tx-end');   if (e) e.value = t;
  loadTransactions(t, t);
});

// ── Transaction Void/Edit Modal ──
let _txModalLines = [];

function openTxModal(t) {
  STATE.currentTx = t;
  _txModalLines   = t.lines.map(l => ({ ...l }));
  document.getElementById('txModalTitle').textContent = `Transaction #${String(t.id).slice(0,8)}`;
  const meta = document.getElementById('tx-modal-meta');
  if (meta) meta.textContent = `${new Date(t.date_time).toLocaleString('en-ZA')} — R${fmt(t.total)}${t.teller ? ' — ' + t.teller : ''}`;
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
      <td><button class="btn btn-outline-danger btn-sm" data-remove="${idx}">✕</button></td>
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
    toast('Transaction voided — stock restored', 'warning');
    bootstrap.Modal.getOrCreateInstance(document.getElementById('txModal')).hide();
    loadTransactions(document.getElementById('tx-start')?.value, document.getElementById('tx-end')?.value);
    await loadProducts();
    await loadIngredients();
  } catch (e) { toast(e.message, 'error'); }
});

// ═══════════════════════════════════════════════════════
// ═══════════════════════════════════════════════════════
// FLAG TRANSACTION
// ═══════════════════════════════════════════════════════
function openFlagModal(t) {
  document.getElementById('flag-sale-id').value = t.id;
  document.getElementById('flag-note').value    = t.flag_note || '';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('flagModal')).show();
  setTimeout(() => document.getElementById('flag-note').focus(), 300);
}

document.getElementById('btn-flag-submit')?.addEventListener('click', async () => {
  const saleId = document.getElementById('flag-sale-id').value;
  const note   = document.getElementById('flag-note').value.trim();
  if (!note) return toast('Please describe what needs review', 'warning');
  try {
    await api(`/api/transactions/${saleId}/flag`, {
      method: 'POST',
      body: JSON.stringify({ note })
    });
    toast('Transaction flagged for admin review', 'warning', 3000);
    bootstrap.Modal.getOrCreateInstance(document.getElementById('flagModal')).hide();
    // Refresh transactions
    if (STATE.user?.role === 'admin') {
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
    left.innerHTML = `<strong>${u.username}</strong> <span class="user-meta">• ${u.role} • ${u.active ? 'active' : 'disabled'}</span>`;
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
  document.getElementById('u-role').value = u.role;
  const act = document.getElementById('u-active'); if (act) act.checked = !!u.active;
}

async function loadUsers() {
  if (STATE.user?.role !== 'admin') return;
  try { STATE.users = await api('/api/users') || []; renderUsersList(); }
  catch (e) { console.error('loadUsers', e); }
}

document.getElementById('users-filter')?.addEventListener('input', renderUsersList);
document.getElementById('btn-refresh-users')?.addEventListener('click', loadUsers);

document.getElementById('btn-add-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  const password = document.getElementById('u-password').value;
  const role     = document.getElementById('u-role').value;
  const active   = document.getElementById('u-active').checked;
  if (!username || !password) return toast('Username and password required', 'warning');
  try {
    await api('/api/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
    if (!active) await api('/api/users/update', { method: 'POST', body: JSON.stringify({ username, active }) });
    await loadUsers(); toast('User added');
  } catch (e) { toast(e.message, 'error'); }
});

document.getElementById('btn-update-user')?.addEventListener('click', async () => {
  const username = document.getElementById('u-username').value.trim();
  if (!username) return toast('Select a user first', 'warning');
  const password = document.getElementById('u-password').value;
  const role     = document.getElementById('u-role').value;
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
    ['u-username','u-password'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    document.getElementById('u-role').value = 'teller';
    document.getElementById('u-active').checked = true;
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
        const d = new Date(now); d.setDate(d.getDate() - d.getDay());
        start = iso(d);
      } else if (preset === 'month') {
        start = `${now.getFullYear()}-${pad(now.getMonth()+1)}-01`;
      } else if (preset === 'year') {
        start = `${now.getFullYear()}-01-01`;
      }
      _statsSetDates(start, end);
      loadStats();
    });
  });
}

// drilldown state — which canvas+slice is active for click hits
let _chartClickHandlers = {};

function drawBarChart(canvas, labels, values, opts = {}) {
  if (!canvas) return;
  const parent = canvas.parentElement;
  canvas.width  = parent?.clientWidth  || 800;
  canvas.height = parent?.clientHeight || 320;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const W = canvas.width, H = canvas.height;
  const padL = 60, padR = 20, padT = 30, padB = 50;
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
    const lbl = (labels[i] || '').slice(-10);
    ctx.save(); ctx.translate(x + bw / 2, H - padB + 6);
    if (n > 8) { ctx.rotate(-Math.PI / 4); ctx.textAlign = 'right'; }
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
      <div class="fw-bold fs-6">${s.peak_hour != null ? `${s.peak_hour}:00` : '—'}</div>
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
        ${s.top_products.length ? `<li>Best-seller: <strong>${s.top_products[0].product}</strong> — R${fmt(s.top_products[0].revenue)}</li>` : ''}
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

async function openDrilldown(title, type, value, opts = {}) {
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  document.getElementById('drilldown-title').textContent = title;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-center text-muted p-3">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ start, end });
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

async function openSupplierDrilldown(supplierName) {
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  document.getElementById('drilldown-title').textContent = `Stock purchases — ${supplierName}`;
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ supplier: supplierName, start, end });
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
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  document.getElementById('drilldown-title').textContent = 'Kitchen Orders';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ start, end });
    const orders = await api(`/api/stats/drilldown/kitchen?${params}`);
    if (!orders.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No kitchen orders found.</div>';
      return;
    }
    const fmtWait = s => s != null ? (s >= 60 ? `${Math.floor(s/60)}m ${s%60}s` : `${s}s`) : '—';
    const completed = orders.filter(o => o.status === 'completed').length;
    const avgWait = orders.filter(o => o.wait_seconds != null).reduce((s,o,_,a) => s + o.wait_seconds/a.length, 0);
    let html = `<div class="small text-muted mb-2">${orders.length} order${orders.length!==1?'s':''} · ${completed} completed · Avg wait ${fmtWait(Math.round(avgWait))}</div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Time</th><th>Product</th><th>Qty</th><th>Teller</th><th>Status</th><th>Wait</th><th>Notes</th></tr></thead><tbody>`;
    orders.forEach(o => {
      const statusColor = o.status === 'completed' ? 'text-success' : o.status === 'cancelled' ? 'text-danger' : 'text-warning';
      html += `<tr>
        <td style="font-size:11px">${o.queued_at?.replace('T',' ').slice(0,16) || '—'}</td>
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
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  document.getElementById('drilldown-title').textContent = 'Stock Write-offs';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ start, end });
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
        <td style="font-size:11px">${w.date?.replace('T',' ').slice(0,16) || '—'}</td>
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
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  document.getElementById('drilldown-title').textContent = 'Profit Breakdown by Product';
  document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">Loading…</div>';
  bootstrap.Modal.getOrCreateInstance(document.getElementById('statsDrilldownModal')).show();
  try {
    const params = new URLSearchParams({ start, end });
    const items = await api(`/api/stats/drilldown/profit?${params}`);
    if (!items.length) {
      document.getElementById('drilldown-body').innerHTML = '<div class="text-muted p-2">No data found.</div>';
      return;
    }
    const isAdmin     = STATE.user?.role === 'admin';
    const totalRev    = items.reduce((s, i) => s + i.revenue, 0);
    const totalProfit = items.reduce((s, i) => s + i.profit, 0);
    const overallMargin = totalRev > 0 ? (totalProfit / totalRev * 100).toFixed(1) : '—';
    let html = `<div class="row g-2 mb-3">
      <div class="${isAdmin ? 'col-4' : 'col-12'}"><div class="card border-success text-center py-2"><div class="small text-muted">Revenue</div><div class="fw-bold text-success">R${fmt(totalRev)}</div></div></div>
      ${isAdmin ? `
      <div class="col-4"><div class="card border-success text-center py-2"><div class="small text-muted">Gross Profit</div><div class="fw-bold text-success">R${fmt(totalProfit)}</div></div></div>
      <div class="col-4"><div class="card border-warning text-center py-2"><div class="small text-muted">Margin</div><div class="fw-bold text-warning">${overallMargin}%</div></div></div>
      ` : ''}
    </div>`;
    html += `<table class="table table-sm table-hover">
      <thead class="table-light"><tr><th>Product</th><th class="text-end">Qty</th><th class="text-end">Revenue</th>${isAdmin ? '<th class="text-end">COGS</th><th class="text-end text-success">Profit</th><th class="text-end text-warning">Margin</th>' : ''}</tr></thead><tbody>`;
    items.forEach(i => {
      const profitColor = i.profit >= 0 ? 'text-success' : 'text-danger';
      html += `<tr>
        <td>${i.product}</td>
        <td class="text-end">${i.qty_sold}</td>
        <td class="text-end">R${fmt(i.revenue)}</td>
        ${isAdmin ? `
        <td class="text-end text-muted">R${fmt(i.cogs)}</td>
        <td class="text-end fw-semibold ${profitColor}">R${fmt(i.profit)}</td>
        <td class="text-end text-warning">${i.margin != null ? i.margin + '%' : '—'}</td>
        ` : ''}
      </tr>`;
    });
    html += '</tbody></table>';
    document.getElementById('drilldown-body').innerHTML = html;
  } catch(e) {
    document.getElementById('drilldown-body').innerHTML = `<div class="text-danger p-2">${e.message}</div>`;
  }
}

function _showChartTab(tab) {
  _statsChartTab = tab;
  ['daily','hourly','minute','top','top-rev','suppliers'].forEach(id => {
    const c = document.getElementById(`chart-${id}`);
    if (c) c.style.display = 'none';
  });
  document.querySelectorAll('[data-chart-tab]').forEach(b => {
    b.className = b.dataset.chartTab === tab
      ? 'btn btn-sm btn-primary'
      : 'btn btn-sm btn-outline-secondary';
  });
  const hint = document.getElementById('chart-click-hint');
  const drillableTabs = ['daily','hourly','minute','top-qty','top-rev'];
  if (hint) hint.style.display = drillableTabs.includes(tab) ? '' : 'none';

  if (!_statsData) return;
  const j = _statsData;

  if (tab === 'daily') {
    const c = document.getElementById('chart-daily');
    c.style.display = '';
    const dayData = j.revenue_per_day;
    drawBarChart(c, dayData.map(d => d.date.slice(5)), dayData.map(d => d.revenue), {
      color: '#2a6f3e', color2: '#4caf7d', valuePrefix: 'R',
      onBarClick: (lbl, val, i) => openDrilldown(`Sales on ${dayData[i].date}`, 'day', dayData[i].date),
    });
  } else if (tab === 'hourly') {
    const c = document.getElementById('chart-hourly');
    c.style.display = '';
    const hours = Array.from({length: 24}, (_, i) => i);
    const hourMap = Object.fromEntries((j.revenue_per_hour || []).map(x => [x.hour, x.revenue]));
    drawBarChart(c, hours.map(h => `${h}:00`), hours.map(h => hourMap[h] || 0), {
      color: '#1976d2', valuePrefix: 'R',
      onBarClick: (lbl, val, i) => { if (val > 0) openDrilldown(`Sales at ${i}:00`, 'hour', i); },
    });
  } else if (tab === 'minute') {
    const c = document.getElementById('chart-minute');
    c.style.display = '';
    const mins = j.revenue_per_minute || [];
    drawBarChart(c, mins.map(x => x.minute), mins.map(x => x.revenue), {
      color: '#00838f', valuePrefix: 'R',
      onBarClick: (lbl) => { if (lbl) openDrilldown(`Sales at ${lbl}`, 'minute', lbl); },
    });
  } else if (tab === 'top-qty') {
    const c = document.getElementById('chart-top');
    c.style.display = '';
    const products = j.top_products || [];
    drawBarChart(c, products.map(x => x.name), products.map(x => x.qty_sold), {
      color: '#e65100', valueSuffix: ' units',
      onBarClick: (lbl, val, i) => openDrilldown(`Sales of ${products[i]?.name}`, 'product', products[i]?.product_id),
    });
  } else if (tab === 'top-rev') {
    const c = document.getElementById('chart-top-rev');
    c.style.display = '';
    const products = j.top_by_revenue || [];
    drawBarChart(c, products.map(x => x.name), products.map(x => x.revenue), {
      color: '#7b1fa2', valuePrefix: 'R',
      onBarClick: (lbl, val, i) => openDrilldown(`Sales of ${products[i]?.name}`, 'product', products[i]?.product_id),
    });
  } else if (tab === 'suppliers') {
    const c = document.getElementById('chart-suppliers');
    c.style.display = '';
    const sups = j.supplier_breakdown || [];
    drawBarChart(c, sups.map(x => x.supplier), sups.map(x => x.total_cost), {
      color: '#5d4037', valuePrefix: 'R',
      onBarClick: (lbl) => openSupplierDrilldown(lbl),
    });
  }
}

async function loadStats() {
  const start = document.getElementById('stats-start')?.value || todayISO();
  const end   = document.getElementById('stats-end')?.value   || todayISO();
  const label = document.getElementById('stats-period-label');
  if (label) label.textContent = start === end ? start : `${start} → ${end}`;
  // Sync export pickers
  const es = document.getElementById('export-start'); if (es) es.value = start;
  const ee = document.getElementById('export-end');   if (ee) ee.value = end;

  try {
    const j = await api(`/api/stats?start=${start}&end=${end}`);
    _statsData = j;
    const el = id => document.getElementById(id);

    const cardClick = (cardEl, fn) => {
      if (!cardEl) return;
      cardEl.closest('.card').style.cursor = 'pointer';
      cardEl.closest('.card').onclick = fn;
    };

    el('stat-total')  && (el('stat-total').textContent  = `R${fmt(j.total_sales_value)}`);
    cardClick(el('stat-total'), () => openDrilldown('All transactions', 'range', null));

    el('stat-profit') && (el('stat-profit').textContent = `R${fmt(j.gross_profit)}`);
    el('stat-margin-sub') && (el('stat-margin-sub').textContent = j.gross_margin != null ? `${j.gross_margin}% margin` : '');
    cardClick(el('stat-profit'), () => openProfitDrilldown());

    el('stat-cogs')   && (el('stat-cogs').textContent   = j.total_cogs > 0 ? `R${fmt(j.total_cogs)}` : '—');
    cardClick(el('stat-cogs'), () => openProfitDrilldown());

    el('stat-margin') && (el('stat-margin').textContent = j.gross_margin != null ? `${j.gross_margin}%` : '—');
    cardClick(el('stat-margin'), () => openProfitDrilldown());

    el('stat-tx')     && (el('stat-tx').textContent     = j.transactions_count);
    cardClick(el('stat-tx'), () => openDrilldown('All transactions', 'range', null));

    el('stat-avg')    && (el('stat-avg').textContent    = `R${fmt(j.avg_basket_value)}`);
    cardClick(el('stat-avg'), () => openDrilldown('All transactions', 'range', null));

    el('stat-items')  && (el('stat-items').textContent  = j.total_items_sold);
    cardClick(el('stat-items'), () => openDrilldown('All transactions', 'range', null));

    if (el('stat-writeoff-cost')) {
      el('stat-writeoff-cost').textContent = j.total_writeoff_cost > 0 ? `R${fmt(j.total_writeoff_cost)}` : '—';
      cardClick(el('stat-writeoff-cost'), () => openWriteoffDrilldown());
    }
    if (el('stat-writeoff-count-sub')) {
      el('stat-writeoff-count-sub').textContent = j.writeoff_count > 0 ? `${j.writeoff_count} write-offs` : '';
    }
    if (el('stat-kitchen-count')) {
      el('stat-kitchen-count').textContent = j.kitchen_orders_today > 0 ? j.kitchen_orders_today : '—';
      cardClick(el('stat-kitchen-count'), () => openKitchenDrilldown());
    }
    if (el('stat-avg-wait')) {
      const waitSecs = j.avg_completed_wait ?? j.avg_wait_seconds;
      if (waitSecs != null) {
        const m = Math.floor(waitSecs / 60), s = Math.round(waitSecs % 60);
        el('stat-avg-wait').textContent = m > 0 ? `${m}m ${s}s` : `${s}s`;
      } else {
        el('stat-avg-wait').textContent = '—';
      }
      cardClick(el('stat-avg-wait'), () => openKitchenDrilldown());
    }

    if (el('stat-best-day') && j.best_day) {
      el('stat-best-day').textContent     = j.best_day.date;
      el('stat-best-day-val').textContent = `R${fmt(j.best_day.revenue)} · ${j.best_day.tx_count} sales`;
      cardClick(el('stat-best-day'), () => openDrilldown(`Best day — ${j.best_day.date}`, 'day', j.best_day.date, { context: 'best' }));
    }
    const worstCard = el('stat-worst-day')?.closest('.card');
    if (j.worst_day) {
      if (worstCard) worstCard.style.display = '';
      if (el('stat-worst-day')) {
        el('stat-worst-day').textContent     = j.worst_day.date;
        el('stat-worst-day-val').textContent = `R${fmt(j.worst_day.revenue)} · ${j.worst_day.tx_count} sales`;
        cardClick(el('stat-worst-day'), () => openDrilldown(`Worst day — ${j.worst_day.date}`, 'day', j.worst_day.date, { context: 'worst' }));
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
        const fmtTime = iso => iso ? iso.replace('T',' ').slice(0,16) : '—';
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
              <td class="text-end">${e.session_count || '—'}</td>
              <td class="text-end">${e.session_minutes ? fmtMins(e.session_minutes) : '—'}</td>
              <td class="text-end">${e.revenue_per_hour != null ? `R${fmt(e.revenue_per_hour)}` : '—'}</td>
              <td class="text-end">${e.tx_per_hour != null ? e.tx_per_hour.toFixed(1) : '—'}</td>
              <td class="text-end" style="font-size:11px">${e.first_sale ? e.first_sale.replace('T',' ').slice(0,16) : '—'}</td>
              <td class="text-end" style="font-size:11px">${e.last_sale  ? e.last_sale.replace('T',' ').slice(0,16)  : '—'}</td>
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
                        <td>${s.logout ? fmtTime(s.logout) : '—'}</td>
                        <td>${s.last_active ? fmtTime(s.last_active) : '—'}</td>
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
            const detail = empWrap.querySelector(`[data-detail-for="${row.dataset.empId}"]`);
            const toggle = row.querySelector('.emp-toggle');
            const open   = detail.classList.toggle('d-none');
            toggle.textContent = open ? '▶' : '▼';
          });
        });
      }
    }
  } catch (e) { console.error('loadStats', e); toast('Could not load stats', 'error'); }
}

document.querySelectorAll('[data-chart-tab]').forEach(btn => {
  btn.addEventListener('click', () => _showChartTab(btn.dataset.chartTab));
});
document.getElementById('btn-refresh-stats')?.addEventListener('click', loadStats);
_initStatsPresets();

// CSV export
(function initExportDates() {
  const t = todayISO();
  const s = document.getElementById('export-start'); if (s && !s.value) s.value = t;
  const e = document.getElementById('export-end');   if (e && !e.value) e.value = t;
})();
document.getElementById('btn-export-csv')?.addEventListener('click', () => {
  const s = document.getElementById('export-start')?.value;
  const e = document.getElementById('export-end')?.value;
  const p = new URLSearchParams();
  if (s) p.set('start', s); if (e) p.set('end', e);
  window.open(`/admin/export/transactions?${p.toString()}`, '_blank', 'noopener');
});

// ═══════════════════════════════════════════════════════
// SETTINGS
// ═══════════════════════════════════════════════════════
let _globalMarkupPct = 40;  // default; overwritten by loadSettings

async function loadSettings() {
  try {
    const j = await api('/api/settings');
    _globalMarkupPct = parseFloat(j.markup_percent) || 40;
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
      if (i.removed)     return `<li style="background:#f8d7da;border-radius:4px;padding:2px 6px;font-weight:700;color:#842029;text-decoration:line-through">✕ NO ${i.name}</li>`;
      if (i.extra)       return `<li style="background:#d1e7dd;border-radius:4px;padding:2px 6px;font-weight:700;color:#0a3622">+ EXTRA: ${i.name} — <strong>${qtyDisplay}</strong></li>`;
      if (i.substituted) {
        const origNote = i.original_name ? ` <span style="text-decoration:line-through;opacity:.6">${i.original_name}</span>` : '';
        return `<li style="background:#fff3cd;border-radius:4px;padding:2px 6px;font-weight:700;color:#856404">⚑ SWAP: ${i.name}${origNote} — <strong>${qtyDisplay}</strong></li>`;
      }
      return `<li>• ${i.name} — <strong>${qtyDisplay}</strong></li>`;
    }).join('') + `</ul>`;
}

function renderKitchenQueue(orders) {
  const host = document.getElementById('kitchen-queue-list');
  if (!host) return;

  if (orders.length === 0) {
    host.innerHTML = `
      <div class="text-center py-5 text-muted">
        <div style="font-size:3rem">✅</div>
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
        ${o.notes ? `<div class="small text-info mt-1">📝 ${o.notes}</div>` : ''}
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
              ${teller ? `<span class="badge bg-light text-dark border">🧑 ${teller}</span>` : ''}
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
        <button class="btn btn-success btn-lg-touch flex-fill" data-ko-done-sale="${saleId}">✓ Done — whole order</button>
        <button class="btn btn-outline-danger" data-ko-cancel-sale="${saleId}">✕ Cancel</button>
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
    const wait = o.wait_seconds != null ? fmtWait(o.wait_seconds) : '—';
    const item = document.createElement('div');
    item.className = `d-flex justify-content-between align-items-center py-2 border-bottom small ${o.status === 'cancelled' ? 'text-muted' : ''}`;
    item.innerHTML = `
      <span>${o.status === 'cancelled' ? '✕' : '✓'} <strong>${o.product_name}</strong> ×${o.qty % 1 === 0 ? o.qty : o.qty.toFixed(1)}</span>
      <span>${new Date(o.queued_at).toLocaleTimeString('en-ZA', {hour:'2-digit',minute:'2-digit'})}</span>
      <span class="text-muted">⏱ ${wait}</span>
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

// Background badge poll — runs every 30s regardless of which tab is active
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
    if (STATE.user.role === 'admin') {
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
    // Only auto-load if no data has been loaded yet — preserve the user's selected range
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
    if (STATE.user.role === 'admin') {
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
    if (STATE.user?.role === 'admin') renderSpecialsList();
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
    card.innerHTML = `
      <div class="product-thin-main">
        <div class="product-title">${s.name} ${s.active ? '' : '<span class="badge bg-secondary">Inactive</span>'}</div>
        <div class="product-sub">R${fmt(s.special_price)} — ${lineNames || 'No products set'}</div>
      </div>
      <div class="product-actions">
        <button class="btn btn-outline-primary btn-sm">Edit</button>
      </div>`;
    card.querySelector('button').onclick = () => openSpecialEditor(s);
    host.appendChild(card);
  });
}

document.getElementById('btn-new-special')?.addEventListener('click', () => openSpecialEditor(null));

let _specialLines = [];

function openSpecialEditor(s) {
  _specialLines = (s?.lines || []).map(l => ({ ...l }));
  document.getElementById('special-id').value    = s?.id ?? '';
  document.getElementById('special-name').value  = s?.name ?? '';
  document.getElementById('special-price').value = s?.special_price ?? '';
  document.getElementById('special-active').checked = s?.active !== false;
  document.getElementById('special-editor-title').textContent = s ? `Edit — ${s.name}` : 'New Special';
  const delBtn = document.getElementById('btn-delete-special');
  s ? show(delBtn) : hide(delBtn);
  renderSpecialLines();
  bootstrap.Modal.getOrCreateInstance(document.getElementById('specialEditorModal')).show();
}

function renderSpecialLines() {
  const tbody = document.getElementById('special-lines-body');
  if (!tbody) return;
  tbody.innerHTML = '';
  const forSaleProducts = STATE.products.filter(p => p.is_for_sale && !p.is_archived);
  _specialLines.forEach((line, idx) => {
    const tr = document.createElement('tr');
    let selHTML = `<select class="form-select form-select-sm" data-sl-idx="${idx}" data-sl-field="product_id">
      <option value="">— select —</option>`;
    forSaleProducts.forEach(p => {
      selHTML += `<option value="${p.id}" ${p.id === line.product_id ? 'selected' : ''}>${p.name}</option>`;
    });
    selHTML += '</select>';
    tr.innerHTML = `
      <td>${selHTML}</td>
      <td><input type="number" min="1" value="${line.qty || 1}" class="form-control form-control-sm" style="width:70px" data-sl-idx="${idx}" data-sl-field="qty"></td>
      <td><button class="btn btn-outline-danger btn-sm" data-sl-remove="${idx}">✕</button></td>`;
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
  const payload = { name, special_price: price, active, lines };
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
// COMBO / SPECIAL DETECTION — auto-applies, no prompt
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
  const active = (STATE.specials || []).filter(s => s.active && s.lines.length > 0);
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
    if (sav * times > 0.005) toast(`"${special.name}" ×${times} — saving R${fmt(sav * times)}`, 'success', 3000);
  });

  renderCart();
}

function applySpecial(special, times) {
  // Discount exactly (req.qty × times) units per required product.
  // Uses _allocated_units / _discounted_subtotal so that a product appearing in two
  // different specials (e.g. Coke in both "2×Coke" and "Coke+Cream Soda") is split
  // correctly — each unit is counted towards at most one special.
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
    selHTML += `<option value="">— keep default —</option>`;
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
      <option value="">— select ingredient —</option>`;
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
      <td><button class="btn btn-outline-danger btn-sm" data-extra-remove="${idx}">✕</button></td>`;
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
      // Swap ingredient cost — use edited qty if provided, else original qty_base
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
    const margin   = parseFloat(document.getElementById('calc-markup')?.value || '50') / 100;
    const safeMargin = Math.min(margin, 0.99);
    const priceAdj = delta / (1 - (safeMargin > 0 ? safeMargin : 0.5));
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
    // Editing an existing cart entry — update it in place
    const entry = STATE.cart[_subsCartKey];
    entry.name       = allLabels.length ? `${p.name} (${allLabels.join(', ')})` : p.name;
    entry.unit_price = finalPrice;
    entry.subs       = hasCustomisation ? subs   : undefined;
    entry.extras     = hasCustomisation ? extras : undefined;
  } else {
    // New entry — use unique key if customised so multiple versions can coexist
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
