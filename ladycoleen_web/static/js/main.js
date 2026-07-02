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

  // Cart count badge in header
  const badge = document.getElementById('nav-cart-count');
  if (badge) {
    const updateBadge = () => {
      const cart = JSON.parse(localStorage.getItem('lc_cart') || '[]');
      const count = cart.reduce((sum, i) => sum + (i.sold_by_weight ? 1 : i.qty), 0);
      if (count > 0) {
        badge.textContent = count > 99 ? '99+' : count;
        badge.style.display = '';
      } else {
        badge.style.display = 'none';
      }
    };
    updateBadge();
    // Update when localStorage changes (e.g. item added on another tab or same page)
    window.addEventListener('storage', updateBadge);
    // Also expose so pages can call it after adding to cart
    window._updateCartBadge = updateBadge;
  }
});
