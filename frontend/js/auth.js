/**
 * auth.js — MA Trader 权限认证与登录/修改密码管理器
 */

const AuthManager = (() => {
  const TOKEN_KEY = 'ma_trader_token';
  const USER_KEY = 'ma_trader_username';
  let changePwdModalInstance = null;

  // ─── Token Utilities ────────────────────────────────────────────────
  function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function setSession(token, username) {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    if (username) localStorage.setItem(USER_KEY, username);
    updateUserDisplay(username);
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    updateUserDisplay(null);
  }

  function getUsername() {
    return localStorage.getItem(USER_KEY) || 'admin';
  }

  function updateUserDisplay(username) {
    const el = document.getElementById('navUsername');
    if (el) {
      el.textContent = username || '未登录';
    }
  }

  // ─── Setup Global Fetch Hook for Auto-Authorization ─────────────────
  function setupFetchInterceptor() {
    const originalFetch = window.fetch;
    window.fetch = async function (...args) {
      let [resource, config] = args;
      config = config || {};

      const url = typeof resource === 'string' ? resource : resource.url;

      // Only intercept internal /api/ calls
      if (url && (url.startsWith('/api/') || url.includes('/api/'))) {
        const token = getToken();
        if (token) {
          config.headers = config.headers || {};
          if (config.headers instanceof Headers) {
            if (!config.headers.has('Authorization')) {
              config.headers.set('Authorization', `Bearer ${token}`);
            }
          } else if (Array.isArray(config.headers)) {
            const hasAuth = config.headers.some(([k]) => k.toLowerCase() === 'authorization');
            if (!hasAuth) {
              config.headers.push(['Authorization', `Bearer ${token}`]);
            }
          } else {
            if (!config.headers['Authorization']) {
              config.headers['Authorization'] = `Bearer ${token}`;
            }
          }
        }
      }

      const response = await originalFetch(resource, config);

      // Auto logout on 401 Unauthorized (except for the login endpoint itself)
      if (response.status === 401 && !url.includes('/api/auth/login')) {
        console.warn('[Auth] Received 401 Unauthorized, clearing session...');
        clearSession();
        showLoginOverlay('登录已过期或未授权，请重新登录');
      }

      return response;
    };
  }

  // ─── Init ───────────────────────────────────────────────────────────
  function init() {
    setupFetchInterceptor();

    const changePwdEl = document.getElementById('changePwdModal');
    if (changePwdEl && typeof bootstrap !== 'undefined') {
      changePwdModalInstance = new bootstrap.Modal(changePwdEl);
    }

    // Bind Login Form
    const loginForm = document.getElementById('loginForm');
    if (loginForm) {
      loginForm.addEventListener('submit', handleLogin);
    }

    // Bind Logout Button
    const btnLogout = document.getElementById('btnLogout');
    if (btnLogout) {
      btnLogout.addEventListener('click', (e) => {
        e.preventDefault();
        logout();
      });
    }

    // Bind Open Change Password Modal
    const btnOpenChangePwd = document.getElementById('btnOpenChangePwd');
    if (btnOpenChangePwd) {
      btnOpenChangePwd.addEventListener('click', (e) => {
        e.preventDefault();
        openChangePwdModal();
      });
    }

    // Bind Change Password Form
    const changePwdForm = document.getElementById('changePwdForm');
    if (changePwdForm) {
      changePwdForm.addEventListener('submit', handleChangePassword);
    }

    updateUserDisplay(getUsername());
  }

  // ─── Check Authentication Status on Startup ────────────────────────
  async function checkAuth() {
    const token = getToken();
    if (!token) {
      showLoginOverlay();
      return false;
    }

    try {
      const res = await fetch('/api/auth/me');
      if (res.ok) {
        const data = await res.json();
        setSession(token, data.username);
        hideLoginOverlay();
        return true;
      } else {
        clearSession();
        showLoginOverlay();
        return false;
      }
    } catch (err) {
      console.warn('[Auth] checkAuth network error:', err);
      // If server unreachable, show login with error
      showLoginOverlay();
      return false;
    }
  }

  // ─── Handle Login ───────────────────────────────────────────────────
  async function handleLogin(e) {
    e.preventDefault();
    const userInput = document.getElementById('loginUsername');
    const pwdInput = document.getElementById('loginPassword');
    const btnSubmit = document.getElementById('btnLoginSubmit');
    const errorBox = document.getElementById('loginErrorBox');

    const username = (userInput?.value || '').trim();
    const password = pwdInput?.value || '';

    if (!username || !password) {
      showLoginError('请输入用户名和密码');
      return;
    }

    if (errorBox) errorBox.style.display = 'none';
    if (btnSubmit) {
      btnSubmit.disabled = true;
      btnSubmit.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>登录中…';
    }

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || '用户名或密码错误');
      }

      setSession(data.token, data.username);
      hideLoginOverlay();

      // Trigger app refresh
      if (typeof AppController !== 'undefined' && AppController.init) {
        AppController.init();
      }
    } catch (err) {
      showLoginError(err.message);
    } finally {
      if (btnSubmit) {
        btnSubmit.disabled = false;
        btnSubmit.innerHTML = '<i class="fa-solid fa-right-to-bracket me-1"></i>登录系统';
      }
    }
  }

  function showLoginError(msg) {
    const errorBox = document.getElementById('loginErrorBox');
    if (errorBox) {
      errorBox.textContent = msg;
      errorBox.style.display = 'block';
    }
  }

  // ─── Handle Logout ──────────────────────────────────────────────────
  async function logout() {
    if (!confirm('确定要退出当前管理员登录吗？')) return;
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch {}
    clearSession();
    showLoginOverlay('已安全退出登录');
  }

  // ─── Handle Change Password ─────────────────────────────────────────
  function openChangePwdModal() {
    const oldPwdInput = document.getElementById('pwdOld');
    const newPwdInput = document.getElementById('pwdNew');
    const confirmPwdInput = document.getElementById('pwdConfirm');
    const errorBox = document.getElementById('changePwdError');
    if (oldPwdInput) oldPwdInput.value = '';
    if (newPwdInput) newPwdInput.value = '';
    if (confirmPwdInput) confirmPwdInput.value = '';
    if (errorBox) errorBox.style.display = 'none';

    changePwdModalInstance?.show();
  }

  async function handleChangePassword(e) {
    e.preventDefault();
    const oldPwd = document.getElementById('pwdOld')?.value || '';
    const newPwd = document.getElementById('pwdNew')?.value || '';
    const confirmPwd = document.getElementById('pwdConfirm')?.value || '';
    const errorBox = document.getElementById('changePwdError');
    const btnSubmit = document.getElementById('btnSavePwd');

    if (errorBox) errorBox.style.display = 'none';

    if (!oldPwd) {
      showChangePwdError('请输入当前原密码');
      return;
    }
    if (newPwd.length < 6) {
      showChangePwdError('新密码长度不能少于 6 位');
      return;
    }
    if (newPwd !== confirmPwd) {
      showChangePwdError('两次输入的新密码不一致，请核对');
      return;
    }

    if (btnSubmit) {
      btnSubmit.disabled = true;
      btnSubmit.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>保存中…';
    }

    try {
      const res = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPwd, new_password: newPwd })
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || '密码修改失败');
      }

      if (data.token) {
        setSession(data.token, getUsername());
      }

      changePwdModalInstance?.hide();
      alert('✅ ' + (data.message || '密码修改成功，新密码已生效！'));
    } catch (err) {
      showChangePwdError(err.message);
    } finally {
      if (btnSubmit) {
        btnSubmit.disabled = false;
        btnSubmit.innerHTML = '保存修改';
      }
    }
  }

  function showChangePwdError(msg) {
    const errorBox = document.getElementById('changePwdError');
    if (errorBox) {
      errorBox.textContent = msg;
      errorBox.style.display = 'block';
    }
  }

  // ─── Overlay Visibility ─────────────────────────────────────────────
  function showLoginOverlay(hint = '') {
    const overlay = document.getElementById('loginOverlay');
    const mainLayout = document.getElementById('mainLayout');
    const errorBox = document.getElementById('loginErrorBox');
    if (overlay) overlay.style.display = 'flex';
    if (mainLayout) mainLayout.style.display = 'none';

    if (hint && errorBox) {
      errorBox.textContent = hint;
      errorBox.style.display = 'block';
    }
    const pwdInput = document.getElementById('loginPassword');
    if (pwdInput) {
      pwdInput.value = '';
      setTimeout(() => pwdInput.focus(), 100);
    }
  }

  function hideLoginOverlay() {
    const overlay = document.getElementById('loginOverlay');
    const mainLayout = document.getElementById('mainLayout');
    if (overlay) overlay.style.display = 'none';
    if (mainLayout) mainLayout.style.display = 'flex';
  }

  return {
    init,
    checkAuth,
    getToken,
    getUsername,
    showLoginOverlay,
    hideLoginOverlay,
    logout
  };
})();
