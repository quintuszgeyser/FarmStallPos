// Lady Coleen Admin JS

// ── Toast notification ────────────────────────────────────────────────────────
let _toastEl = null;

function showToast(msg, type = 'success') {
  if (!_toastEl) {
    const container = document.createElement('div');
    container.style.cssText = 'position:fixed;top:20px;right:20px;z-index:9999;min-width:280px';
    container.id = 'toast-container';
    document.body.appendChild(container);

    _toastEl = document.createElement('div');
    _toastEl.className = 'toast align-items-center border-0';
    _toastEl.setAttribute('role', 'alert');
    _toastEl.setAttribute('aria-live', 'assertive');
    _toastEl.innerHTML = `
      <div class="d-flex">
        <div class="toast-body fw-semibold" id="toast-msg"></div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
      </div>`;
    container.appendChild(_toastEl);
  }

  _toastEl.className = `toast align-items-center border-0 text-white bg-${type === 'success' ? 'success' : type === 'error' ? 'danger' : 'warning'}`;
  document.getElementById('toast-msg').textContent = msg;

  const t = bootstrap.Toast.getOrCreateInstance(_toastEl, { delay: 3500 });
  t.show();
}

// ── Loading button helper ────────────────────────────────────────────────────
function setLoading(btn, loading, label = null) {
  if (loading) {
    btn._origText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>' + (label || 'Working…');
  } else {
    btn.disabled = false;
    btn.textContent = label || btn._origText || 'Submit';
  }
}

// ── Auto-dismiss success alerts ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.alert-success, .alert-info').forEach(el => {
    setTimeout(() => {
      el.style.transition = 'opacity .4s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 400);
    }, 4000);
  });
});
