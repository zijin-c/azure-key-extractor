// Bind every request to the account that rendered this page, including stale tabs.
(() => {
  const userId = document.currentScript.dataset.userId;
  const originalFetch = window.fetch.bind(window);
  let invalidated = false;
  function invalidate() {
    if (invalidated) return;
    invalidated = true;
    document.body?.replaceChildren(document.createTextNode('登录账号已变化，正在重新加载…'));
    window.dispatchEvent(new Event('session-invalidated'));
    location.replace('/login');
  }
  // Legacy caches have no trustworthy owner. Never migrate their credentials.
  for (const key of ['azkey_accounts', 'azkey_proxy', 'azkey_headless', 'azkey_history',
                     'azkey_current', 'azkey_account_map', 'azure_concurrency']) {
    try { localStorage.removeItem(key); } catch (_) {}
  }
  window.fetch = async (input, options = {}) => {
    if (invalidated) throw new Error('登录账号已变化');
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
    if (url.origin === location.origin) headers.set('X-User-ID', userId);
    const response = await originalFetch(input, {...options, headers});
    if (response.status === 401 || response.status === 409) {
      const result = await response.clone().json().catch(() => null);
      if (result?.need_login || result?.need_reload) {
        invalidate();
        throw new Error('登录状态已变化');
      }
    }
    if (invalidated) throw new Error('登录账号已变化');
    return response;
  };
  window.addEventListener('storage', event => {
    if (event.key === 'azkey_auth_change') invalidate();
  });
  window.addEventListener('pageshow', event => { if (event.persisted) location.reload(); });
})();
