/* SMART Sniffer panel: the "SMART Sniffer" page in Home Assistant's sidebar.

   A web component Home Assistant creates for the panel (admins only). It
   frames the Control Center page (index.html, same origin) full size, and
   gives the page a small bridge to the user's own Home Assistant
   connection, as frame.ssBridge, before the page loads:

     callWS(msg)            one websocket command, a promise of its result
     subscribe(msg, cb)     a websocket subscription, a promise of its unsubscribe
     navigate(path)         open a page of Home Assistant in its own window
     toggleMenu()           Home Assistant's sidebar, on a narrow screen
     user()                 { name, is_admin } of the user
     uploadReplica(body)    a replica file to the integration's upload
     agenticUrl()           the raw Agentic access JSON's address, signed by
                            Home Assistant for this user (five minutes)

   The bridge never hands the page the hass object itself. When Home
   Assistant sets hass (on every state change) only the reference the bridge
   reads is swapped; nothing is drawn again.

   On a narrow screen the panel shows Home Assistant's own top bar with the
   menu button, as an app's page in the sidebar has it. */

(function () {
  const VERSION_FALLBACK = '';

  class SmartSnifferPanel extends HTMLElement {
    constructor() {
      super();
      this._hass = null;
      this._narrow = false;
      this._panel = null;
      this._frame = null;
      this._bar = null;
    }

    set hass(hass) {
      this._hass = hass;
    }

    get hass() {
      return this._hass;
    }

    set narrow(narrow) {
      this._narrow = !!narrow;
      if (this._bar) this._bar.hidden = !this._narrow;
    }

    set panel(panel) {
      this._panel = panel;
      this._build();
    }

    set route(_route) {}

    connectedCallback() {
      this._build();
    }

    _config() {
      return (this._panel && this._panel.config) || {};
    }

    _bridge() {
      const el = this;
      return Object.freeze({
        callWS(msg) {
          if (!el._hass) return Promise.reject({ code: 'not_ready', message: 'Home Assistant is not connected' });
          return el._hass.callWS(msg);
        },
        subscribe(msg, callback) {
          if (!el._hass) return Promise.reject({ code: 'not_ready', message: 'Home Assistant is not connected' });
          return el._hass.connection.subscribeMessage(callback, msg);
        },
        navigate(path) {
          if (typeof path !== 'string' || !path.startsWith('/')) return;
          window.history.pushState(null, '', path);
          window.dispatchEvent(new CustomEvent('location-changed', { detail: { replace: false } }));
        },
        toggleMenu() {
          el.dispatchEvent(new Event('hass-toggle-menu', { bubbles: true, composed: true }));
        },
        user() {
          const u = el._hass && el._hass.user;
          return u ? { name: u.name, is_admin: !!u.is_admin } : null;
        },
        // The raw Agentic access JSON (/api/smart_sniffer/agentic.json),
        // signed for this user by Home Assistant (auth/sign_path), so it
        // opens in a tab of its own. The path is fixed here.
        async agenticUrl() {
          if (!el._hass) return null;
          const r = await el._hass.callWS({ type: 'auth/sign_path',
            path: '/api/smart_sniffer/agentic.json', expires: 300 });
          return r && typeof r.path === 'string' && r.path.startsWith('/api/smart_sniffer/agentic.json?')
            ? r.path : null;
        },
        // A replica file into the user's replica library: the file's bytes
        // to the integration's own upload, with the user's login. The path
        // is fixed here; the page cannot choose it.
        async uploadReplica(body) {
          if (!el._hass || !el._hass.fetchWithAuth) return { ok: false, data: { error: 'not_ready' } };
          const resp = await el._hass.fetchWithAuth('/api/smart_sniffer/replica/upload', {
            method: 'POST', body, headers: { 'Content-Type': 'application/json' } });
          let data = null;
          try { data = await resp.json(); } catch (e) { data = null; }
          return { ok: resp.ok, status: resp.status, data };
        },
      });
    }

    _build() {
      if (this._frame || !this.isConnected) return;
      const config = this._config();
      const version = config.version || VERSION_FALLBACK;
      const base = config.static || '/smart_sniffer_static';
      const root = this.attachShadow ? this.attachShadow({ mode: 'open' }) : this;
      const style = document.createElement('style');
      style.textContent = `
        /* The panel fills the window itself: Home Assistant 2026.x gives its
           parents no height (the page opened 150 px tall there), 2024.4
           does. dvh follows a phone's browser bars where it is known. Only
           the page inside the frame scrolls, so there is one scrollbar. */
        :host { display: flex; flex-direction: column; box-sizing: border-box;
          height: 100vh; height: 100dvh; min-height: 0; overflow: hidden;
          background: #000; }
        .bar { display: flex; align-items: center; height: var(--header-height, 56px); flex: none;
          padding: 0 4px; box-sizing: border-box; gap: 8px;
          background: var(--app-header-background-color, var(--primary-color, #03a9f4));
          color: var(--app-header-text-color, #fff);
          border-bottom: var(--app-header-border-bottom, none);
          padding-top: env(safe-area-inset-top, 0); }
        .bar[hidden] { display: none; }
        .menu { width: 48px; height: 48px; border: 0; background: transparent; color: inherit;
          cursor: pointer; display: grid; place-items: center; border-radius: 50%; }
        .menu svg { width: 24px; height: 24px; fill: currentColor; }
        .title { font-size: 20px; font-weight: 400; margin-left: 4px; }
        iframe { flex: 1 1 auto; min-height: 0; width: 100%; height: 100%; border: 0;
          display: block; background: #000; }
      `;
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.hidden = !this._narrow;
      const menu = document.createElement('button');
      menu.className = 'menu';
      menu.type = 'button';
      menu.setAttribute('aria-label', 'Sidebar');
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z');
      svg.appendChild(path);
      menu.appendChild(svg);
      menu.addEventListener('click', () => this._bridge().toggleMenu());
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = 'SMART Sniffer';
      bar.append(menu, title);
      const frame = document.createElement('iframe');
      frame.title = 'SMART Sniffer Control Center';
      frame.ssBridge = this._bridge();
      frame.src = `${base}/index.html${version ? `?v=${encodeURIComponent(version)}` : ''}`;
      root.append(style, bar, frame);
      this._bar = bar;
      this._frame = frame;
    }
  }

  if (!customElements.get('smart-sniffer-panel')) {
    customElements.define('smart-sniffer-panel', SmartSnifferPanel);
  }
}());
