
let PRODUCTS = {}; // name -> {id, price}
let CART = [];     // [{name, qty, price}]

const productSelect = document.getElementById('productSelect');
const qtyInput = document.getElementById('qtyInput');
const addBtn = document.getElementById('addBtn');
const cartList = document.getElementById('cartList');
const cartTotalEl = document.getElementById('cartTotal');
const cancelBtn = document.getElementById('cancelBtn');
const checkoutBtn = document.getElementById('checkoutBtn');
const productsList = document.getElementById('productsList');
const prodName = document.getElementById('prodName');
const prodPrice = document.getElementById('prodPrice');
const addProductBtn = document.getElementById('addProductBtn');
const updateProductBtn = document.getElementById('updateProductBtn');
const deleteProductBtn = document.getElementById('deleteProductBtn');
const refreshTxBtn = document.getElementById('refreshTxBtn');
const transactionsBody = document.getElementById('transactionsBody');
const barcodeInput = document.getElementById('barcodeInput');
const scanStartBtn = document.getElementById('scanStartBtn');
const scanStopBtn = document.getElementById('scanStopBtn');
const cameraArea = document.getElementById('cameraArea');
const previewVideo = document.getElementById('preview');
let codeReader;

async function loadProducts() {
  const res = await fetch('/api/products');
  PRODUCTS = await res.json();
  // fill select
  productSelect.innerHTML = '';
  const optPlaceholder = document.createElement('option');
  optPlaceholder.textContent = 'Select Product';
  optPlaceholder.value = '';
  productSelect.appendChild(optPlaceholder);
  Object.entries(PRODUCTS).forEach(([name, info]) => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = `${name} — ${info.id} — ${info.price.toFixed ? info.price.toFixed(2) : info.price}`;
    productSelect.appendChild(opt);
  });
  renderProductsList();
}

function renderProductsList() {
  productsList.innerHTML = '';
  Object.entries(PRODUCTS).forEach(([name, info]) => {
    const li = document.createElement('li');
    li.className = 'list-group-item d-flex justify-content-between align-items-center';
    li.textContent = `${name} — ${info.id} — ${info.price}`;
    li.onclick = () => { prodName.value = name; prodPrice.value = info.price; };
    productsList.appendChild(li);
  });
}

function addToCart(productName, qty=1) {
  if (!productName) return;
  qty = parseInt(qty || '1', 10);
  // allow using ID number as barcode: find by id
  let info = PRODUCTS[productName];
  if (!info) {
    // try by id
    const asId = parseInt(productName, 10);
    for (const [n, v] of Object.entries(PRODUCTS)) {
      if (v.id === asId) { info = v; productName = n; break; }
    }
  }
  if (!info) {
    alert('Product not found: ' + productName);
    return;
  }
  const existing = CART.find(x => x.name === productName);
  if (existing) existing.qty += qty; else CART.push({name: productName, qty, price: info.price});
  renderCart();
}

function renderCart() {
  cartList.innerHTML = '';
  let total = 0;
  CART.forEach((item, idx) => {
    const li = document.createElement('div');
    li.className = 'list-group-item d-flex justify-content-between align-items-center';
    const amount = item.price * item.qty;
    total += amount;
    li.innerHTML = `<div><strong>${item.name}</strong> — ${item.qty} items</div><div>${amount.toFixed(2)}</div>`;
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm btn-outline-danger';
    btn.textContent = '✖';
    btn.onclick = () => { removeFromCart(idx); };
    li.appendChild(btn);
    cartList.appendChild(li);
  });
  cartTotalEl.textContent = total.toFixed(2);
}

function removeFromCart(index) {
  const item = CART[index];
  if (!item) return;
  if (item.qty > 1) item.qty -= 1; else CART.splice(index, 1);
  renderCart();
}

cancelBtn.onclick = () => { CART = []; renderCart(); }
addBtn.onclick = () => { addToCart(productSelect.value, qtyInput.value); }
barcodeInput.onchange = () => { addToCart(barcodeInput.value, 1); barcodeInput.value = ''; }

checkoutBtn.onclick = async () => {
  if (CART.length === 0) return alert('Cart is empty');
  const payload = { items: CART.map(x => ({product_name: x.name, qty: x.qty})) };
  const res = await fetch('/api/transactions', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
  const data = await res.json();
  if (!res.ok) { alert(JSON.stringify(data)); return; }
  CART = []; renderCart();
  await loadTransactions();
  alert('Sale completed. Transaction #' + data.tran_id);
}

addProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  const price = parseFloat(prodPrice.value || '0');
  if (!name) return alert('Name required');
  const res = await fetch('/api/products', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, price}) });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
  prodName.value = ''; prodPrice.value = '';
}

updateProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  const price = parseFloat(prodPrice.value || '0');
  if (!name) return alert('Select a product first');
  const res = await fetch('/api/products/update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({old_name: name, new_name: name, price}) });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
}

deleteProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  if (!name) return alert('Select a product first');
  if (!confirm('Delete ' + name + '?')) return;
  const res = await fetch('/api/products/' + encodeURIComponent(name), { method: 'DELETE' });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
  prodName.value = ''; prodPrice.value = '';
}

refreshTxBtn.onclick = () => loadTransactions();

async function loadTransactions() {
  const res = await fetch('/api/transactions');
  const tx = await res.json();
  // Group by tran_id
  const byTran = new Map();
  tx.forEach(line => {
    const key = line.tran_id;
    if (!byTran.has(key)) byTran.set(key, []);
    byTran.get(key).push(line);
  });
  transactionsBody.innerHTML = '';
  [...byTran.entries()].sort((a,b)=>a[0]-b[0]).forEach(([id, lines]) => {
    const total = lines.reduce((sum, l) => sum + l.amount, 0);
    const card = document.createElement('div');
    card.className = 'card mb-2';
    const body = document.createElement('div');
    body.className = 'card-body';
    const title = document.createElement('h6');
    title.textContent = `#${id} — ${lines[0].date_time} — Total: ${total.toFixed(2)}`;
    body.appendChild(title);
    const list = document.createElement('ul');
    list.className = 'list-group list-group-flush';
    lines.forEach(l => {
      const li = document.createElement('li');
      li.className = 'list-group-item d-flex justify-content-between';
      li.innerHTML = `<span>${l.product_id} — ${l.no_of_items} items</span><span>${l.amount.toFixed(2)}</span>`;
      list.appendChild(li);
    });
    body.appendChild(list);
    card.appendChild(body);
    transactionsBody.appendChild(card);
  });
}

// --- Camera scanning ---
scanStartBtn.onclick = async () => {
  try {
    cameraArea.style.display = '';
    codeReader = new ZXing.BrowserMultiFormatReader();
    const devices = await ZXing.BrowserMultiFormatReader.listVideoInputDevices();
    const deviceId = devices?.[0]?.deviceId;
    await codeReader.decodeFromVideoDevice(deviceId, 'preview', (result, err) => {
      if (result) {
        addToCart(result.getText(), 1);
        // brief flash
        previewVideo.style.outline = '3px solid #28a745';
        setTimeout(() => previewVideo.style.outline = '', 300);
      }
    });
  } catch (e) {
    alert('Camera error: ' + e);
    cameraArea.style.display = 'none';
  }
};

scanStopBtn.onclick = () => {
  try { codeReader?.reset(); } catch (e) {}
  cameraArea.style.display = 'none';
};

// Init
loadProducts();
loadTransactions();
