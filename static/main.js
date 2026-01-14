
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
const addProductBtn   = document.getElementById('addProductBtn');
const updateProductBtn= document.getElementById('updateProductBtn');
const deleteProductBtn= document.getElementById('deleteProductBtn');

const refreshTxBtn    = document.getElementById('refreshTxBtn');
const transactionsBody= document.getElementById('transactionsBody');

const barcodeInput    = document.getElementById('barcodeInput');
const scanStartBtn    = document.getElementById('scanStartBtn');
const scanStopBtn     = document.getElementById('scanStopBtn');
const cameraArea      = document.getElementById('cameraArea');
const previewVideo    = document.getElementById('preview');

let codeReader;

async function loadProducts() {
  const res = await fetch('/api/products');
  PRODUCTS = await res.json();

  productSelect.innerHTML = '';
  const optPlaceholder = document.createElement('option');
  optPlaceholder.textContent = 'Select Product';
  optPlaceholder.value = '';
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

  if (!info) { // by numeric id
    const asId = parseInt(productName, 10);
    if (!Number.isNaN(asId)) {
      for (const [n, v] of Object.entries(PRODUCTS)) {
        if (v.id === asId) { info = v; productName = n; break; }
      }
    }
  }

  if (!info) { // by barcode
    for (const [n, v] of Object.entries(PRODUCTS)) {
      if (v.barcode && v.barcode === inputValue) { info = v; productName = n; break; }
    }
  }

  if (!info) {
    alert('Product not found: ' + inputValue);
    return;
  }

  const existing = CART.find(x => x.name === productName);
  if (existing) existing.qty += qty;
  else CART.push({ name: productName, qty, price: info.price });

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

cancelBtn.onclick = () => { CART = []; renderCart(); };
addBtn.onclick = () => { addToCart(productSelect.value, qtyInput.value); };
barcodeInput.onchange = () => { addToCart(barcodeInput.value, 1); barcodeInput.value = ''; };

checkoutBtn.onclick = async () => {
  if (CART.length === 0) return alert('Cart is empty');
  const payload = { items: CART.map(x => ({ product_name: x.name, qty: x.qty })) };
  const res = await fetch('/api/transactions', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) { alert(JSON.stringify(data)); return; }
  CART = []; renderCart();
  await loadTransactions();
  alert('Sale completed. Transaction #' + data.tran_id);
};

addProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  const price = parseFloat(prodPrice.value || '0');
  if (!name) return alert('Name required');

  const barcode = prompt('Enter barcode (leave blank to auto-generate EAN-13):', '');

  const res = await fetch('/api/products', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name, price, barcode })
  });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
  prodName.value = ''; prodPrice.value = '';
  if (data.barcode) alert('Product added. Barcode: ' + data.barcode);
};

updateProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  const price = parseFloat(prodPrice.value || '0');
  if (!name) return alert('Select a product first');

  const cur = PRODUCTS[name]?.barcode || '';
  const barcode = prompt('Enter new barcode (leave blank to keep current):', cur) || cur;

  const res = await fetch('/api/products/update', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ old_name: name, new_name: name, price, barcode })
  });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
};

deleteProductBtn.onclick = async () => {
  const name = (prodName.value || '').trim();
  if (!name) return alert('Select a product first');
  if (!confirm('Delete ' + name + '?')) return;
  const res = await fetch('/api/products/' + encodeURIComponent(name), { method: 'DELETE' });
  const data = await res.json();
  if (!res.ok) return alert(JSON.stringify(data));
  await loadProducts();
  prodName.value = ''; prodPrice.value = '';
};

refreshTxBtn.onclick = () => loadTransactions();

async function loadTransactions() {
  const res = await fetch('/api/transactions');
  const tx = await res.json();
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

// --- Camera scanning (mobile-friendly) ---
scanStartBtn.onclick = async () => {
  try {
    cameraArea.style.display = '';
    previewVideo.setAttribute('playsinline', 'true'); // iOS
    previewVideo.muted = true;
    previewVideo.autoplay = true;

    codeReader = new ZXing.BrowserMultiFormatReader();

    // Prefer rear camera
    try {
      await codeReader.decodeFromConstraints(
        { video: { facingMode: { exact: "environment" } } },
        'preview',
        (result, err) => {
          if (result) {
            const text = result.getText();
            addToCart(text, 1);
            previewVideo.style.outline = '3px solid #28a745';
            setTimeout(() => previewVideo.style.outline = '', 300);
          }
        }
      );
    } catch {
      // Fallback if exact not supported
      await codeReader.decodeFromConstraints(
        { video: { facingMode: "environment" } },
        'preview',
        (result, err) => {
          if (result) {
            const text = result.getText();
            addToCart(text, 1);
            previewVideo.style.outline = '3px solid #28a745';
            setTimeout(() => previewVideo.style.outline = '', 300);
          }
        }
      );
    }
  } catch (e2) {
    cameraArea.style.display = 'none';
    const msg = (location.protocol !== 'https:' ? 'This feature requires HTTPS.\n' : '') +
                'Camera error: ' + e2;
    alert(msg);
  }
};

scanStopBtn.onclick = () => {
  try { codeReader?.reset(); } catch {}
  cameraArea.style.display = 'none';
};

// Init
loadProducts();
loadTransactions();
