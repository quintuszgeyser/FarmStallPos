async function api(path, opts={}) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!res.ok) { let t=''; try{t=await res.text();}catch{}; throw new Error(`HTTP ${res.status}: ${t}`); }
  const ct = res.headers.get('Content-Type')||''; return ct.includes('application/json')? res.json(): res.text();
}

let currentUser = null;
let cart = [];
let codeReader = null;
let lastScanAt = 0;
const SCAN_COOLDOWN_MS = 700;
let mediaStream = null;

function setLoggedInUI(isLoggedIn, role) {
  const loginArea = document.getElementById('loginArea');
  const appContainer = document.getElementById('appContainer');
  const navTabs = document.getElementById('navTabs');
  const tabManage = document.getElementById('tabManageLink');
  const tabUsers = document.getElementById('tabUsersLink');
  const btnLogout = document.getElementById('btnLogout');

  if (isLoggedIn) {
    loginArea.style.display = 'none';
    appContainer.style.display = '';
    navTabs.style.display = '';
    btnLogout.classList.remove('hidden');
    if (role === 'admin') { tabManage.parentElement.style.display = ''; tabUsers.parentElement.style.display = ''; }
    else { tabManage.parentElement.style.display = 'none'; tabUsers.parentElement.style.display = 'none'; }
  } else {
    loginArea.style.display = '';
    appContainer.style.display = 'none';
    navTabs.style.display = 'none';
    btnLogout.classList.add('hidden');
  }
}

async function refreshProductsList() {
  const listDiv = document.getElementById('productsList');
  try {
    const data = await api('/api/products');
    const entries = Object.entries(data).sort((a,b)=>a[0].localeCompare(b[0]));
    let html = '<table class="table table-sm"><thead><tr><th>Name</th><th>Price</th><th>Barcode</th></tr></thead><tbody>';
    for (const [name, obj] of entries) { html += `<tr><td>${name}</td><td>${(+obj.price).toFixed(2)}</td><td>${obj.barcode||''}</td></tr>`; }
    html += '</tbody></table>';
    listDiv.innerHTML = html;
  } catch (e) { listDiv.innerHTML = `<div class="text-danger">Failed to load products: ${e.message}</div>`; }
}

async function refreshUsersList() {
  const list = document.getElementById('usersList');
  try {
    const users = await api('/api/users');
    list.innerHTML='';
    users.forEach(u=>{
      const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between align-items-center';
      li.innerHTML = `<div><strong>${u.username}</strong> <span class=\"badge bg-secondary ms-2\">${u.role}</span> ${u.active? '' : '<span class=\"badge bg-warning text-dark ms-1\">inactive</span>'}</div>`+
                     `<div><button class=\"btn btn-sm btn-outline-primary me-2\" data-act=\"toggle\" data-u=\"${u.username}\" data-active=\"${u.active}\">${u.active? 'Deactivate':'Activate'}</button>`+
                     `<button class=\"btn btn-sm btn-outline-danger\" data-act=\"delete\" data-u=\"${u.username}\">Delete</button></div>`;
      list.appendChild(li);
    });
  } catch(e) { list.innerHTML = `<li class="list-group-item text-danger">Failed to load users: ${e.message}</li>`; }
}

async function refreshTransactions() {
  const container = document.getElementById('txList'); container.innerHTML = '<div class="text-muted">Loading…</div>';
  try {
    const txs = await api('/api/transactions');
    if (!Array.isArray(txs) || txs.length===0) { container.innerHTML = '<div class="text-muted">No transactions today.</div>'; return; }
    let html='';
    for (const t of txs) {
      html += `<div class=\"card mb-2\"><div class=\"card-body\">
        <div class=\"d-flex justify-content-between\"><div><strong>#${t.id}</strong> <span class=\"text-muted\">${t.date_time}</span></div><div><strong>Total: ${(+t.total).toFixed(2)}</strong></div></div>
        <table class=\"table table-sm mt-2\"><thead><tr><th>Item</th><th>Qty</th><th>Unit</th><th>Line</th></tr></thead><tbody>`;
      for (const ln of t.lines) { html += `<tr><td>${ln.product_name}</td><td>${ln.qty}</td><td>${(+ln.unit_price).toFixed(2)}</td><td>${(+ln.line_total).toFixed(2)}</td></tr>`; }
      html += '</tbody></table></div></div>';
    }
    container.innerHTML = html;
  } catch(e) { container.innerHTML = `<div class="text-danger">Failed to load transactions: ${e.message}</div>`; }
}

function redrawCart() {
  const tbody = document.querySelector('#cartTable tbody'); tbody.innerHTML='';
  let total=0; cart.forEach(it=>{ const tr=document.createElement('tr'); const line=it.qty*it.price; total+=line; tr.innerHTML=`<td>${it.name}</td><td>${it.qty}</td><td>${it.price.toFixed(2)}</td><td>${line.toFixed(2)}</td>`; tbody.appendChild(tr); });
  document.getElementById('cartTotal').textContent = `Total: ${total.toFixed(2)}`;
}

async function ensureLoggedIn() {
  try { const me = await api('/api/me'); if (me.logged_in){ currentUser=me.user; setLoggedInUI(true, me.user.role); await refreshProductsList(); await refreshTransactions(); } else setLoggedInUI(false); }
  catch { setLoggedInUI(false); }
}

// --- Scanner logic ---
function beep() {
  try {
    const ctx = new (window.AudioContext||window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination); o.frequency.value=880; g.gain.value=0.05; o.start();
    setTimeout(()=>{o.stop(); ctx.close();}, 140);
  } catch {}
}

async function onScan(text) {
  const now = Date.now(); if (now - lastScanAt < SCAN_COOLDOWN_MS) return; lastScanAt = now;
  const video = document.getElementById('scanVideo'); video.classList.add('scan-ok'); setTimeout(()=>video.classList.remove('scan-ok'), 450);
  beep();
  // Lookup product by barcode or ID or name
  try {
    const data = await api('/api/products');
    let found = null; const num = parseInt(text,10);
    for (const [n, o] of Object.entries(data)) {
      if (o.barcode && o.barcode===text) { found = { name:n, id:o.id, price:+o.price }; break; }
      if (!isNaN(num) && o.id===num) { found = { name:n, id:o.id, price:+o.price }; break; }
      if (n.toLowerCase()===text.toLowerCase()) { found = { name:n, id:o.id, price:+o.price }; break; }
    }
    if (!found) { document.getElementById('scanMsg').textContent = 'Code scanned but no matching product.'; return; }
    const existing = cart.find(it=>it.id===found.id); if (existing) existing.qty += 1; else cart.push({ name: found.name, price: found.price, qty:1, id: found.id });
    redrawCart();
    document.getElementById('scanMsg').textContent = `Added: ${found.name}`;
  } catch(e) {
    document.getElementById('scanMsg').textContent = 'Scan lookup failed: '+e.message;
  }
}

async function startScanner() {
  const video = document.getElementById('scanVideo'); const msg = document.getElementById('scanMsg');
  msg.textContent = 'Starting scanner…';
  try {
    // Prefer environment (rear) camera
    mediaStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false });
    video.srcObject = mediaStream; await video.play();
    if (!window.ZXing || !ZXing.BrowserMultiFormatReader) { msg.textContent = 'ZXing library not loaded'; return; }
    codeReader = new ZXing.BrowserMultiFormatReader();
    const devices = await codeReader.listVideoInputDevices();
    let deviceId = undefined;
    const back = devices.find(d=>d.label && d.label.toLowerCase().includes('back'));
    if (back) deviceId = back.deviceId; else if (devices[0]) deviceId = devices[0].deviceId;
    codeReader.decodeContinuouslyFromVideoDevice(deviceId, video, (result, err) => {
      if (result && result.text) onScan(result.text);
    });
    msg.textContent = 'Scanner running (cooldown '+SCAN_COOLDOWN_MS+'ms).';
  } catch(e) { msg.textContent = 'Camera error: '+e.message; }
}

function stopScanner() {
  const msg = document.getElementById('scanMsg');
  try { if (codeReader) { codeReader.reset(); codeReader = null; } } catch {}
  if (mediaStream) { mediaStream.getTracks().forEach(t=>t.stop()); mediaStream=null; }
  msg.textContent = 'Scanner stopped.';
}

// --- DOM events ---
document.addEventListener('DOMContentLoaded', () => {
  ensureLoggedIn();

  document.getElementById('btnLogin').addEventListener('click', async () => {
    const u = document.getElementById('loginUsername').value.trim();
    const p = document.getElementById('loginPassword').value; const msg = document.getElementById('loginMsg'); msg.textContent='';
    try { const res = await api('/api/login', { method:'POST', body: JSON.stringify({ username:u, password:p }) }); currentUser=res.user; setLoggedInUI(true,res.user.role); await refreshProductsList(); await refreshTransactions(); }
    catch(e){ msg.textContent='Login failed: '+e.message; }
  });

  document.getElementById('btnLogout').addEventListener('click', async () => { try{ await api('/api/logout',{method:'POST'});}catch{} location.reload(); });

  document.getElementById('btnReloadProducts').addEventListener('click', refreshProductsList);

  document.getElementById('btnAddProduct').addEventListener('click', async () => {
    const name = document.getElementById('pName').value.trim(); const price = parseFloat(document.getElementById('pPrice').value); const barcode = document.getElementById('pBarcode').value.trim(); const msg = document.getElementById('addPMsg'); msg.textContent='';
    try { await api('/api/products',{method:'POST',body:JSON.stringify({name,price,barcode:barcode||null})}); msg.className='small text-success'; msg.textContent='Product added.'; await refreshProductsList(); }
    catch(e){ msg.className='small text-danger'; msg.textContent='Add failed: '+e.message; }
  });

  document.getElementById('btnUpdateProduct').addEventListener('click', async () => {
    const old_name=document.getElementById('pOldName').value.trim(); const new_name=document.getElementById('pNewName').value.trim(); const priceVal=document.getElementById('pNewPrice').value; const barcode=document.getElementById('pNewBarcode').value.trim(); const msg=document.getElementById('updPMsg'); msg.textContent='';
    const payload={old_name}; if(new_name) payload.new_name=new_name; if(priceVal!=='') payload.price=parseFloat(priceVal); if(barcode!=='') payload.barcode=barcode;
    try { await api('/api/products/update',{method:'POST',body:JSON.stringify(payload)}); msg.className='small text-success'; msg.textContent='Product updated.'; await refreshProductsList(); }
    catch(e){ msg.className='small text-danger'; msg.textContent='Update failed: '+e.message; }
  });

  document.getElementById('btnDeleteProduct').addEventListener('click', async () => {
    const name=document.getElementById('pDelName').value.trim(); const msg=document.getElementById('delPMsg'); msg.textContent='';
    try { const res = await fetch('/api/products/'+encodeURIComponent(name), { method:'DELETE' }); if(!res.ok) throw new Error('HTTP '+res.status); msg.className='small text-success'; msg.textContent='Product deleted.'; await refreshProductsList(); }
    catch(e){ msg.className='small text-danger'; msg.textContent='Delete failed: '+e.message; }
  });

  document.getElementById('btnAddToCart').addEventListener('click', () => {
    const name=document.getElementById('tellerProduct').value.trim(); const qty=Math.max(1,parseInt(document.getElementById('tellerQty').value||'1',10)); if(!name) return;
    fetch('/api/products').then(r=>r.json()).then(data=>{ let found=null; for(const [n,o] of Object.entries(data)){ if(n.toLowerCase()===name.toLowerCase() || (o.barcode && o.barcode===name)) { found={name:n,price:+o.price,id:o.id}; break; } }
      if(!found){ alert('Product not found'); return; } const existing=cart.find(it=>it.name===found.name); if(existing) existing.qty+=qty; else cart.push({name:found.name, price:found.price, qty, id:found.id}); redrawCart(); });
  });

  document.getElementById('btnCheckout').addEventListener('click', async () => {
    if(cart.length===0) return; const items=cart.map(it=>({product_name:it.name,product_id:it.id,qty:it.qty}));
    try { await api('/api/transactions',{method:'POST',body:JSON.stringify({items})}); cart=[]; redrawCart(); await refreshTransactions(); alert('Sale completed.'); }
    catch(e){ alert('Checkout failed: '+e.message); }
  });

  document.getElementById('btnRefreshTx').addEventListener('click', refreshTransactions);

  // Users
  document.getElementById('btnReloadUsers').addEventListener('click', refreshUsersList);
  document.getElementById('btnCreateUser').addEventListener('click', async () => {
    const username=document.getElementById('uName').value.trim(); const password=document.getElementById('uPass').value; const role=document.getElementById('uRole').value;
    try { await api('/api/users',{method:'POST',body:JSON.stringify({username,password,role,active:true})}); await refreshUsersList(); alert('User created'); }
    catch(e){ alert('Create failed: '+e.message); }
  });
  document.getElementById('usersList').addEventListener('click', async (ev) => {
    const btn=ev.target.closest('button'); if(!btn) return; const act=btn.getAttribute('data-act'); const uname=btn.getAttribute('data-u');
    if(act==='delete'){ if(!confirm('Delete user '+uname+'?')) return; const res=await fetch('/api/users/'+encodeURIComponent(uname),{method:'DELETE'}); if(!res.ok){ alert('Delete failed'); return; } await refreshUsersList(); }
    else if(act==='toggle'){ const active=btn.getAttribute('data-active')==='true'; await api('/api/users/update',{method:'POST',body:JSON.stringify({username:uname,active:!active})}); await refreshUsersList(); }
  });

  // Scanner buttons
  document.getElementById('btnStartScan').addEventListener('click', startScanner);
  document.getElementById('btnStopScan').addEventListener('click', stopScanner);
});
