/**
 * Voxtera i18n — lightweight client-side translation runtime
 * Supports: EN, FR, ES, IT, AR, TR
 * Features: DOM translation via data-i18n, query param ?lang=, localStorage, RTL
 */
(function () {
  'use strict';

  const SUPPORTED = ['en', 'fr', 'es', 'it', 'ar', 'tr'];
  const DEFAULT_LANG = 'en';
  const STORAGE_KEY = 'voxtera_lang';
  const RTL_LANGS = ['ar'];

  let currentLang = DEFAULT_LANG;
  let translations = {};
  let basePath = '';

  // Detect base path for i18n folder relative to current page
  function detectBasePath() {
    const scripts = document.querySelectorAll('script[src*="i18n.js"]');
    if (scripts.length > 0) {
      const src = scripts[0].getAttribute('src');
      return src.replace('i18n.js', '');
    }
    return 'i18n/';
  }

  // Resolve language: ?lang= > localStorage > navigator
  function resolveLang() {
    const params = new URLSearchParams(window.location.search);
    const paramLang = params.get('lang');
    if (paramLang && SUPPORTED.includes(paramLang)) return paramLang;

    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && SUPPORTED.includes(stored)) return stored;

    // Browser language (first 2 chars)
    const nav = (navigator.language || '').slice(0, 2).toLowerCase();
    if (SUPPORTED.includes(nav)) return nav;

    return DEFAULT_LANG;
  }

  // Load a language JSON file
  async function loadLang(lang) {
    try {
      const resp = await fetch(`${basePath}${lang}.json`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.json();
    } catch (e) {
      console.warn(`[i18n] Failed to load ${lang}.json:`, e.message);
      return null;
    }
  }

  // Get nested value from object by dot-separated key
  function getNestedValue(obj, key) {
    return key.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : null), obj);
  }

  // Apply translations to DOM
  function applyTranslations() {
    // data-i18n → textContent
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      const val = getNestedValue(translations, key);
      if (val) el.textContent = val;
    });

    // data-i18n-html → innerHTML (for rich content with spans/br)
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      const key = el.getAttribute('data-i18n-html');
      const val = getNestedValue(translations, key);
      if (val) el.innerHTML = val;
    });

    // data-i18n-placeholder → placeholder attribute
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      const key = el.getAttribute('data-i18n-placeholder');
      const val = getNestedValue(translations, key);
      if (val) el.placeholder = val;
    });

    // data-i18n-aria → aria-label attribute
    document.querySelectorAll('[data-i18n-aria]').forEach(el => {
      const key = el.getAttribute('data-i18n-aria');
      const val = getNestedValue(translations, key);
      if (val) el.setAttribute('aria-label', val);
    });

    // data-i18n-title → title attribute
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      const key = el.getAttribute('data-i18n-title');
      const val = getNestedValue(translations, key);
      if (val) el.title = val;
    });

    // Update <html> lang attribute
    document.documentElement.lang = currentLang;

    // RTL support
    if (RTL_LANGS.includes(currentLang)) {
      document.documentElement.dir = 'rtl';
      document.body.classList.add('rtl');
    } else {
      document.documentElement.dir = 'ltr';
      document.body.classList.remove('rtl');
    }

    // Update any language picker display
    document.querySelectorAll('[data-i18n-current]').forEach(el => {
      el.textContent = currentLang.toUpperCase();
    });
    document.querySelectorAll('.i18n-picker-option').forEach(el => {
      el.classList.toggle('active', el.dataset.lang === currentLang);
    });

    // Update URL without reload
    const url = new URL(window.location);
    if (currentLang === DEFAULT_LANG) {
      url.searchParams.delete('lang');
    } else {
      url.searchParams.set('lang', currentLang);
    }
    window.history.replaceState({}, '', url);
  }

  // Switch language
  async function setLang(lang) {
    if (!SUPPORTED.includes(lang)) return;
    if (lang === currentLang && Object.keys(translations).length > 0) return;

    currentLang = lang;
    localStorage.setItem(STORAGE_KEY, lang);

    const data = await loadLang(lang);
    if (data) {
      translations = data;
      applyTranslations();
    }
  }

  // Get a translation string programmatically (for JS-rendered content)
  function t(key, fallback) {
    const val = getNestedValue(translations, key);
    return val || fallback || key;
  }

  // Inject language picker HTML
  function createPicker(containerId) {
    const container = document.getElementById(containerId) || document.querySelector('.i18n-picker');
    if (!container) return;

    const labels = {
      en: { flag: '🇬🇧', name: 'English' },
      fr: { flag: '🇫🇷', name: 'Français' },
      es: { flag: '🇪🇸', name: 'Español' },
      it: { flag: '🇮🇹', name: 'Italiano' },
      ar: { flag: '🇸🇦', name: 'العربية' },
      tr: { flag: '🇹🇷', name: 'Türkçe' },
    };

    container.innerHTML = `
      <button class="i18n-picker-btn" aria-label="Change language" aria-expanded="false">
        <span class="i18n-picker-flag">${labels[currentLang].flag}</span>
        <span class="i18n-picker-code" data-i18n-current>${currentLang.toUpperCase()}</span>
        <svg width="10" height="6" viewBox="0 0 10 6" fill="none" aria-hidden="true"><path d="M1 1l4 4 4-4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <div class="i18n-picker-menu" role="menu" aria-hidden="true">
        ${SUPPORTED.map(code => `
          <button class="i18n-picker-option${code === currentLang ? ' active' : ''}" data-lang="${code}" role="menuitem">
            <span class="i18n-picker-option-flag">${labels[code].flag}</span>
            <span class="i18n-picker-option-name">${labels[code].name}</span>
          </button>
        `).join('')}
      </div>
    `;

    const btn = container.querySelector('.i18n-picker-btn');
    const menu = container.querySelector('.i18n-picker-menu');

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = menu.getAttribute('aria-hidden') === 'false';
      menu.setAttribute('aria-hidden', open ? 'true' : 'false');
      btn.setAttribute('aria-expanded', !open);
    });

    menu.addEventListener('click', (e) => {
      const option = e.target.closest('[data-lang]');
      if (!option) return;
      const lang = option.dataset.lang;
      menu.setAttribute('aria-hidden', 'true');
      btn.setAttribute('aria-expanded', 'false');
      // Update flag + code immediately
      btn.querySelector('.i18n-picker-flag').textContent = labels[lang].flag;
      setLang(lang);
    });

    // Close on outside click
    document.addEventListener('click', () => {
      menu.setAttribute('aria-hidden', 'true');
      btn.setAttribute('aria-expanded', 'false');
    });
  }

  // Initialize
  async function init() {
    basePath = detectBasePath();
    currentLang = resolveLang();
    const data = await loadLang(currentLang);
    if (data) {
      translations = data;
      applyTranslations();
    }
    // Init any picker present in DOM
    document.querySelectorAll('.i18n-picker').forEach(el => createPicker(el.id));
  }

  // Expose API
  window.VoxteraI18n = {
    init,
    setLang,
    t,
    createPicker,
    get currentLang() { return currentLang; },
    get supported() { return [...SUPPORTED]; },
  };

  // Auto-init on DOMContentLoaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
