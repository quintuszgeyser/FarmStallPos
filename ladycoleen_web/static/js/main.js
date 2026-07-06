// Lady Coleen Web - Public JS
// JWT stored in localStorage, attached to fetch calls automatically

const LC = {
  token: localStorage.getItem('lc_token'),

  async api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
    const res = await fetch(path, { ...opts, headers });
    return res;
  },

  setToken(token) {
    this.token = token;
    if (token) localStorage.setItem('lc_token', token);
    else localStorage.removeItem('lc_token');
  },

  isLoggedIn() { return !!this.token; }
};

// Update nav based on login state + cart count
document.addEventListener('DOMContentLoaded', () => {
  const navLink = document.getElementById('nav-account-link');
  if (navLink) {
    if (LC.isLoggedIn()) {
      navLink.textContent = 'My Orders';
      navLink.href = '/account';
    } else {
      navLink.textContent = 'Sign In';
      navLink.href = '/login';
    }
  }

  // Cart count badges in header (desktop nav + mobile persistent button)
  const _updateCartBadge = () => {
    const cart = JSON.parse(localStorage.getItem('lc_cart') || '[]');
    const count = cart.reduce((sum, i) => sum + (i.sold_by_weight ? 1 : i.qty), 0);
    const label = count > 99 ? '99+' : count;
    for (const id of ['nav-cart-count', 'nav-cart-count-mobile']) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.textContent = label;
      el.style.display = count > 0 ? '' : 'none';
    }
  };
  _updateCartBadge();
  window.addEventListener('storage', _updateCartBadge);
  window._updateCartBadge = _updateCartBadge;
});
