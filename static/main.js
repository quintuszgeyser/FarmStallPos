/* ======================= Auth & Role UI ======================= */
const loginArea  = document.getElementById('loginArea');
const loginUser  = document.getElementById('loginUser');
const loginPass  = document.getElementById('loginPass');
const loginBtn   = document.getElementById('loginBtn');
const loginError = document.getElementById('loginError');
const userChip   = document.getElementById('userChip');
const logoutBtn  = document.getElementById('logoutBtn');
const appContainer = document.getElementById('appContainer');

// tabs
const tellerTabBtn       = document.getElementById('teller-tab');
const transactionsTabBtn = document.getElementById('transactions-tab');
const manageTabBtn       = document.getElementById('manage-tab');

// users admin area
const usersAdminArea   = document.getElementById('usersAdminArea');
const usersList        = document.getElementById('usersList');
const userUsername     = document.getElementById('userUsername');
const userPassword     = document.getElementById('userPassword');
const userRole         = document.getElementById('userRole');
const userActive       = document.getElementById('userActive');
const createUserBtn    = document.getElementById('createUserBtn');
const updateUserBtn    = document.getElementById('updateUserBtn');
const deleteUserBtn    = document.getElementById('deleteUserBtn');

async function refreshAuthUI() {
  const res = await fetch('/api/me');
  const me  = await res.json();

  if (!me.logged_in) {
    loginArea.style.display = '';
    appContainer.style.display = 'none';
    userChip.style.display  = 'none';
    logoutBtn.style.display = 'none';
    return;
  }

  // logged in
  loginArea.style.display = 'none';
  appContainer.style.display = '';
  userChip.textContent = `${me.user.username} (${me.user.role})`;
  userChip.style.display = '';
  logoutBtn.style.display = '';

  if (me.user.role === 'teller') {
    // show only Transactions
    tellerTabBtn.parentElement.style.display       = '';
    manageTabBtn.parentElement.style.display       = 'none';
    transactionsTabBtn.parentElement.style.display = '';

    // activate Transactions tab
    new bootstrap.Tab(transactionsTabBtn).show();
    usersAdminArea.style.display = 'none';
    await loadTransactions();
  } else {
    // admin: all tabs visible
    tellerTabBtn.parentElement.style.display       = '';
    transactionsTabBtn.parentElement.style.display = '';
    manageTabBtn.parentElement.style.display       = '';
    usersAdminArea.style.display = '';
    await Promise.allSettled([ loadProducts(), loadTransactions(), loadUsers() ]);
  }
}

loginBtn?.addEventListener('click', async () => {
  loginError.style.display = 'none';
  const payload = { username: loginUser.value.trim(), password: loginPass.value };
  const res = await fetch('/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) { loginError.textContent = data.error || 'Login failed'; loginError.style.display = ''; return; }
  await refreshAuthUI();
});

logoutBtn?.addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' });
  CART = []; renderCart();
  await refreshAuthUI();
});

/* ======================= Users Admin ======================= */
async function loadUsers() {
  try {
    const res = await fetch('/api/users');
    if (!res.ok) return; // teller will get 403
    const rows = await res.json();
    usersList.innerHTML = '';
    rows.forEach(u => {
      const li = document.createElement('li');
      li.className = 'list-group-item d-flex justify-content-between align-items-center';
      li.textContent = `${u.username} — ${u.role} — ${u.active ? 'active' : 'inactive'}`;
      li.onclick = () => {
        userUsername.value = u.username;
        userRole.value     = u.role;
        userActive.checked = !!u.active;
        userPassword.value = '';
      };
      usersList.appendChild(li);
    });
  } catch (e) {}
}

createUserBtn?.addEventListener('click', async () => {
  const payload = {
    username: userUsername.value.trim(),
    password: userPassword.value,
    role:     userRole.value,
    active:   userActive.checked
  };
  const res = await fetch('/api/users', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  userPassword.value = '';
  await loadUsers();
  alert('User created');
});

updateUserBtn?.addEventListener('click', async () => {
  const payload = {
    username: userUsername.value.trim(),
    role:     userRole.value,
    active:   userActive.checked,
    password: userPassword.value || undefined
  };
  const res = await fetch('/api/users/update', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  userPassword.value = '';
  await loadUsers();
  alert('User updated');
});

deleteUserBtn?.addEventListener('click', async () => {
  const username = userUsername.value.trim();
  if (!username) return alert('Select a user first');
  if (!confirm(`Delete ${username}?`)) return;
  const res = await fetch('/api/users/' + encodeURIComponent(username), { method: 'DELETE' });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  userUsername.value = ''; userPassword.value = ''; userActive.checked = true; userRole.value = 'teller';
  await loadUsers();
  alert('User deleted');
});

/* ======================= POS Logic ======================= */
let PRODUCTS = {}; // name -> {id, price, barcode}
let CART = [];     // [{name, qty, price}]

const productSelect   = document.getElementById('productSelect');
const qtyInput        = document.getElementById('qtyInput');
const addBtn          = document.getElementById('addBtn');
const cartList        = document.getElementById('cartList');
const cartTotalEl     = document.getElementById('cartTotal');
const cancelBtn       = document.getElementById('cancelBtn');
const checkoutBtn     = document.getElementById('checkoutBtn');

const productsList    = document.getElementById('productsList');
const prodName        = document.getElementById('prodName');
const prodPrice       = document.getElementById('prodPrice');

const refreshTxBtn    = document.getElementById('refreshTxBtn');
const transactionsBody= document.getElementById('transactionsBody');

const barcodeInput    = document.getElementById('barcodeInput');
const scanStartBtn    = document.getElementById('scanStartBtn');
const scanStopBtn     = document.getElementById('scanStopBtn');
const cameraArea      = document.getElementById('cameraArea');
const previewVideo    = document.getElementById('preview');
let codeReader;

/* --- Scan feedback: beep + cooldown --- */
let audioCtx;
let scanningCooldownUntil = 0;
const SCAN_COOLDOWN_MS = 700;
function playBeep(duration = 120, freq = 1100, volume = 0.25) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine'; osc.frequency.value = freq; gain.gain.value = volume;
    osc.connect(gain); gain.connect(audioCtx.destination);
    osc.start(); setTimeout(() => { osc.stop(); osc.disconnect(); gain.disconnect(); }, duration);
  } catch (e) {}
}
function acceptScanNow() {
  const now = Date.now();
  if (now < scanningCooldownUntil) return false;
  scanningCooldownUntil = now + SCAN_COOLDOWN_MS;
  return true;
}

/* --- Products & Transactions --- */
async function loadProducts() {
  try {
    const res = await fetch('/api/products');
    if (!res.ok) { productsList.innerHTML=''; productSelect.innerHTML=''; return; }
    PRODUCTS = await res.json();
  } catch { PRODUCTS = {}; }

  productSelect.innerHTML = '';
  const optPlaceholder = document.createElement('option');
  optPlaceholder.textContent = 'Select Product'; optPlaceholder.value = '';
  productSelect.appendChild(optPlaceholder);

  Object.entries(PRODUCTS).forEach(([name, info]) => {
    const opt = document.createElement('option');
    opt.value = name;
    const priceText = (info.price?.toFixed ? info.price.toFixed(2) : info.price);
    opt.textContent = `${name} — ${info.id} — ${priceText}${info.barcode ? ' — ' + info.barcode : ''}`;
    productSelect.appendChild(opt);
  });

  renderProductsList();
}

function renderProductsList() {
  productsList.innerHTML = '';
  Object.entries(PRODUCTS).forEach(([name, info]) => {
    const li = document.createElement('li');
    li.className = 'list-group-item d-flex justify-content-between align-items-center';
    li.textContent = `${name} — ${info.id} — ${info.price} — ${info.barcode || ''}`;
    li.onclick = () => { prodName.value = name; prodPrice.value = info.price; };
    productsList.appendChild(li);
  });
}

function addToCart(inputValue, qty = 1) {
  let productName = inputValue;
  if (!productName) return;
  qty = parseInt(qty || '1', 10);

  let info = PRODUCTS[productName]; // by name
  if (!info) { // by id
    const asId = parseInt(productName, 10);
    if (!Number.isNaN(asId)) for (const [n, v] of Object.entries(PRODUCTS)) { if (v.id === asId) { info = v; productName = n; break; } }
  }
  if (!info) { // by barcode
    for (const [n, v] of Object.entries(PRODUCTS)) { if (v.barcode && v.barcode === inputValue) { info = v; productName = n; break; } }
  }
  if (!info) { alert('Product not found: ' + inputValue); return; }

  const existing = CART.find(x => x.name === productName);
  if (existing) existing.qty += qty; else CART.push({ name: productName, qty, price: info.price });
  renderCart();
}

function renderCart() {
  cartList.innerHTML = ''; let total = 0;
  CART.forEach((item, idx) => {
    const amount = item.price * item.qty; total += amount;
    const li = document.createElement('div');
    li.className = 'list-group-item d-flex justify-content-between align-items-center';
    li.innerHTML = `<div><strong>${item.name}</strong> — ${item.qty} items</div><div>${amount.toFixed(2)}</div>`;
    const btn = document.createElement('button'); btn.className='btn btn-sm btn-outline-danger'; btn.textContent='✖';
    btn.onclick = () => { if (item.qty > 1) item.qty -= 1; else CART.splice(idx, 1); renderCart(); };
    li.appendChild(btn); cartList.appendChild(li);
  });
  cartTotalEl.textContent = total.toFixed(2);
}

cancelBtn.onclick = () => { CART = []; renderCart(); };
addBtn.onclick    = () => { addToCart(productSelect.value, qtyInput.value); };
barcodeInput.onchange = () => { addToCart(barcodeInput.value, 1); barcodeInput.value = ''; };

checkoutBtn.onclick = async () => {
  if (CART.length === 0) return alert('Cart is empty');
  const payload = { items: CART.map(x => ({ product_name: x.name, qty: x.qty })) };
  const res = await fetch('/api/transactions', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  const data = await res.json();
  if (!res.ok) { alert(JSON.stringify(data)); return; }
  CART = []; renderCart(); await loadTransactions(); alert('Sale completed. Transaction #' + data.tran_id);
};

refreshTxBtn.onclick = () => loadTransactions();

async function loadTransactions() {
  const res = await fetch('/api/transactions'); const tx = await res.json();
  const byTran = new Map(); tx.forEach(line => { const key = line.tran_id; if (!byTran.has(key)) byTran.set(key, []); byTran.get(key).push(line); });
  transactionsBody.innerHTML = '';
  [...byTran.entries()].sort((a,b)=>a[0]-b[0]).forEach(([id, lines]) => {
    const total = lines.reduce((sum, l) => sum + l.amount, 0);
    const card = document.createElement('div'); card.className='card mb-2';
    const body = document.createElement('div'); body.className='card-body';
    const title = document.createElement('h6'); title.textContent = `#${id} — ${lines[0].date_time} — Total: ${total.toFixed(2)}`;
    body.appendChild(title);
    const list = document.createElement('ul'); list.className='list-group list-group-flush';
    lines.forEach(l => { const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between'; li.innerHTML = `<span>${l.product_id} — ${l.no_of_items} items</span><span>${l.amount.toFixed(2)}</span>`; list.appendChild(li); });
    body.appendChild(list); card.appendChild(body); transactionsBody.appendChild(card);
  });
}

/* ======================= Camera Scanning ======================= */
scanStartBtn.onclick = async () => {
  try {
    cameraArea.style.display = '';
    previewVideo.setAttribute('playsinline', 'true'); previewVideo.muted = true; previewVideo.autoplay = true;
    const ReaderCtor = ZXing?.BrowserMultiFormatReader;
    if (!ReaderCtor) throw new Error('ZXing not available');
    codeReader = new ReaderCtor();
    try {
      await codeReader.decodeFromConstraints(
        { video: { facingMode: { exact: 'environment' } } },
        'preview',
        (result, err) => {
          if (result) {
            if (!acceptScanNow()) return;
            addToCart(result.getText(), 1);
            previewVideo.style.outline = '3px solid #28a745'; setTimeout(()=>previewVideo.style.outline='',300);
            playBeep(120, 1100, 0.25);
          }
        }
      );
    } catch {
      await codeReader.decodeFromConstraints(
        { video: { facingMode: 'environment' } },
        'preview',
        (result, err) => {
          if (result) {
            if (!acceptScanNow()) return;
            addToCart(result.getText(), 1);
            previewVideo.style.outline = '3px solid #28a745'; setTimeout(()=>previewVideo.style.outline='',300);
            playBeep(120, 1100, 0.25);
          }
        }
      );
    }
  } catch (e2) {
    cameraArea.style.display = 'none';
    const httpsHint = location.protocol !== 'https:' ? 'This feature requires HTTPS.\n' : '';
    alert(httpsHint + 'Camera error: ' + e2);
  }
};
scanStopBtn.onclick = () => { try { codeReader?.reset(); } catch {} cameraArea.style.display = 'none'; };

/* ======================= Boot ======================= */
refreshAuthUI();
