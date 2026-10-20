/* SMART Sniffer Control Center: the page's Alpine component, its data layer
   and its formatting. Served as a static file by the integration; no build
   step.

   This is the HA App 0.3.1 Control Center, moved into the SMART Sniffer
   integration (v0.9). The page and its cards are as they were; only where
   the data comes from changed. It came from the App's proxy over REST; it
   now comes from the integration over the user's own Home Assistant
   websocket, through the bridge panel.js hands the page (callWS,
   subscribe, navigate). request() below maps each of the page's old routes
   to its smart_sniffer/panel/* command, so the rest of the page is the
   App's code.

   One grid of cards: one per drive, per ZFS pool and per filesystem that
   belongs to no drive, from every machine (each a config entry of the
   integration, in any state), then the + card. The integration does the
   judging: every drive arrives with its verdict, a "view" that names its
   rows and their marks, its filesystems and a card id that stays the same
   across reads, so nothing here matches rules. The card order, hidden
   cards, machine labels and filesystem names are kept by the integration
   (the panel Store); a drive's or pool's name is its device's name in Home
   Assistant.

   The page never reads the network itself: the integration's poll does,
   and pushes each machine's new read to the page. */

// Row keys of the drive view, and where each one sits in the raw table.
const ATA_IDS = { reallocated: 5, pending: 197, offline_unc: 198, reported_unc: 187, cmd_timeout: 188,
  realloc_events: 196, spin_retries: 10, wear: 177 };
// Device Statistics rows: page, offset, and the watched row they stand in for.
const DEVSTAT_ROWS = { reported_unc_devstat: [4, 8, 'reported_unc'], reallocated_devstat: [3, 32, 'reallocated'],
  wear_devstat: [7, 8, 'wear'] };
// Device Statistics entries by page:offset, as the Details table names them,
// with how the value reads: count, hours, sectors (a size), temp (degrees C),
// pct, minutes. null leaves an entry out; one not listed shows smartctl's
// own name as a count.
const DEVSTAT_NAMES = {
  '1:8': ['Power cycles', 'count'], '1:16': ['Power-on hours', 'hours'], '1:24': ['Data written', 'sectors'],
  '1:32': ['Write commands', 'count'], '1:40': ['Data read', 'sectors'], '1:48': ['Read commands', 'count'],
  '1:56': null, '1:64': ['Pending errors', 'count'],
  '2:8': ['Free-fall events', 'count'], '2:16': ['Overlimit shock events', 'count'],
  '3:8': ['Spindle motor hours', 'hours'], '3:16': ['Head flying hours', 'hours'], '3:24': ['Head load events', 'count'],
  '3:32': ['Reallocated sectors', 'count'], '3:40': ['Read recovery attempts', 'count'],
  '3:48': ['Mechanical start failures', 'count'], '3:56': ['Reallocation candidates', 'count'],
  '3:64': ['High priority unloads', 'count'],
  '4:8': ['Reported uncorrectable', 'count'], '4:16': ['Command resets', 'count'],
  '4:24': ['Physical element status changes', 'count'],
  '5:8': ['Temperature', 'temp'], '5:16': ['Temperature, short-term average', 'temp'],
  '5:24': ['Temperature, long-term average', 'temp'], '5:32': ['Temperature, lifetime max', 'temp'],
  '5:40': ['Temperature, lifetime min', 'temp'], '5:48': ['Highest short-term average', 'temp'],
  '5:56': ['Lowest short-term average', 'temp'], '5:64': ['Highest long-term average', 'temp'],
  '5:72': ['Lowest long-term average', 'temp'], '5:80': ['Time above the operating maximum', 'minutes'],
  '5:88': ['Operating maximum', 'temp'], '5:96': ['Time below the operating minimum', 'minutes'],
  '5:104': ['Operating minimum', 'temp'],
  '6:8': ['Hardware resets', 'count'], '6:16': ['ASR events', 'count'], '6:24': ['Interface CRC errors', 'count'],
  '7:8': ['Endurance used', 'pct'],
};
// Device Statistics entries the card's stat row already shows (current
// temperature, power-on hours, data written, data read, endurance used):
// left out of the Device Statistics table in Details.
const DEVSTAT_IN_STATS = new Set(['5:8', '1:16', '1:24', '1:40', '7:8']);
// The wear attribute names the rules read, as the proxy has them.
const WEAR_NAMES = ['Wear_Leveling_Count', 'Media_Wearout_Indicator', 'SSD_Life_Left', 'Remaining_Lifetime_Perc',
  'Percent_Lifetime_Remain', 'Perc_Rated_Life_Remain', 'Percent_Life_Remaining'];
const NVME_FIELDS = { critical_warning: 'critical_warning', media_errors: 'media_errors',
  available_spare: 'available_spare', wear: 'percentage_used' };
const NONE = { kind: 'none', href: null };
const COMPARE_WORDS = { gt: 'alert above', ge: 'alert at', lt: 'alert below' };
const LONG_PRESS_MS = 450;
const ARM_MS = 4000; // how long the trash's Remove? waits for its second click
const TOAST_MS = 4000; // how long a removal's Home Assistant message shows
const HA_NAME_FAILED = 'Saved in this app; Home Assistant did not take the name.';
const enc = encodeURIComponent;
const dec = decodeURIComponent;
// The bridge panel.js gives the page: the user's Home Assistant websocket.
const BRIDGE = window.ssBridge || (window.frameElement && window.frameElement.ssBridge) || null;
const WS = 'smart_sniffer/panel/';
// Websocket error codes as the HTTP statuses the page's messages expect.
const WS_STATUS = { not_found: 404, not_an_agent: 404, unauthorized: 403, invalid_format: 400, bad_request: 400,
  panel_state_newer: 409, fixed_name: 409, cannot_remove: 409, nothing_flagged: 409, home_assistant_error: 502,
  not_answering: 504, password_refused: 422, replica_cap: 409, invalid_replica: 422, too_large: 413,
  replica_folder_unavailable: 503, replica_folder_full: 507, replica_file_in_use: 409, not_deletable: 403 };
// The Replica's messages, by the integration's error codes.
const REPLICA_MSG = {
  too_large: 'That file is over 1 MB.',
  invalid_replica: 'That file is not a replica file.',
  replica_folder_unavailable: 'Your replica folder is not available.',
  replica_folder_full: 'Your replica folder is full.',
  replica_cap: 'The Replica holds up to 12 drives and 4 pools.',
  not_found: 'That replica file is gone.',
  unsupported_drive: 'This drive has no SMART data to copy.',
  unsupported_pool: 'This pool could not be saved.',
  replica_file_in_use: 'Remove its replica drive first.',
  not_deletable: 'Only your own replica files can be deleted.',
};
// The NVMe fields a Replica card's editor changes: name, highest value.
const NVME_EDITS = { temperature: ['Temperature', 150], available_spare: ['Available spare', 100],
  percentage_used: ['Percentage used', 255], media_errors: ['Media errors', 2 ** 48 - 1] };
// The Device Statistics entries a Replica card's editor changes (the ones the
// integration reads in place of a missing attribute): page:offset, highest.
const DEVSTAT_EDITS = { '4:8': 2 ** 32 - 1, '3:32': 2 ** 32 - 1, '7:8': 255 };
const REPLICA_GROUPS = [['seed', 'Shipped with SMART Sniffer'], ['user', 'Your replicas'],
  ['share', 'From /share/smart-sniffer/replicas']];
const MISSING_FILE = 'This replica file is gone. Remove this drive and pick another.';
// The SMART Sniffer App's repository, added in the App store of this Home
// Assistant (My Home Assistant redirect: local first, the public one when the
// local route is not there).
const APP_REPO = 'https://github.com/DAB-LABS/smart-sniffer-app';
const APP_STORE_LOCAL = `/_my_redirect/supervisor_add_addon_repository?repository_url=${encodeURIComponent(APP_REPO)}`;
const APP_STORE_PUBLIC = `https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=${encodeURIComponent(APP_REPO)}`;
// Where the integration comes from: its HACS page (through My Home
// Assistant, which opens the user's own instance), or HACS itself first.
const HACS_REPO_URL = 'https://my.home-assistant.io/redirect/hacs_repository/?owner=DAB-LABS&repository=smart-sniffer&category=integration';
const HACS_DOCS_URL = 'https://hacs.xyz/docs/use/';

function parseBlock(text) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d{1,2}))?$/.exec(String(text).trim());
  if (!m) return null;
  const octets = m.slice(1, 5).map(Number);
  const prefix = m[5] === undefined ? 32 : Number(m[5]);
  if (octets.some((o) => o > 255) || prefix > 32) return null;
  return { octets, prefix };
}

function block24(octets) {
  return `${octets[0]}.${octets[1]}.${octets[2]}.0/24`;
}

function cc() {
  // Row heights while a card is open, kept off the reactive state: each
  // open card's closed height at the card width it was measured at, and
  // each closed card's content height when its row was last pinned.
  const rows = { closedH: {}, sig: {}, busy: false, queued: false, full: false, check: new Set() };
  // Thresholds reads in flight, by card id, and a count per card that a
  // save moves on, so a read begun before the save is not kept.
  const thrReads = new Map();
  const thrSeq = new Map();
  return {
    info: null,
    loaded: false,
    view: 'cards',
    ax: { text: '', meta: '', file: 'smart-sniffer.json', status: '' },
    notes: {},
    importClosed: false,
    importShow: false,
    self: null,
    selfFailed: false,
    selfLoaded: false,
    selfBusy: false,
    selfNext: 0,
    selfLastGood: null,
    selfLastAnswer: null,
    replicas: null,
    repBusy: false,
    repFailed: false,
    replicasNext: 0,
    replicaFiles: null,
    hosts: [],
    hostsNext: 0,
    hostData: {},
    optimistic: {},
    ha: null,
    noticeGone: false,
    haTimers: [],
    toastTimer: null,
    scanTimer: null,
    scanPolling: false,
    now: Date.now(),
    // Cards: the stored order and hidden ids, cards removed while their
    // source reads again, and the drag in progress.
    order: [],
    hidden: [],
    // Names given here: card id -> name, field agent id -> name.
    names: {},
    hostNames: {},
    gone: {},
    showHidden: false,
    pageErr: '',
    hostsErr: '',
    drag: null,
    dragOrder: null,
    popOpenedAt: 0,
    ui: { open: {}, cols: 1, confirm: null, arm: null, rename: null, pw: {}, pwState: {}, busy: {}, note: {}, thr: null, copied: '', copiedText: '', toast: '', toastSub: '', toastAct: null, pop: null },
    // Each drive's thresholds as last read, by card id: { data, err }.
    thrCache: {},
    add: {
      open: false, block: '', port: '9099', scan: null, scanId: null, last: null, wide: null, error: '',
      ticks: {}, manualHost: '', manualPort: '9099', manualRows: [], checkMsg: null, checking: false,
      repTicks: {}, upMsg: '', uploading: false, busy: false, err: '', found: [], listed: false,
      network: null, repDel: null,
    },

    // ── Start and timers ────────────────────────────────────────────
    init() {
      this.selfLoaded = true;
      if (!BRIDGE) {
        this.pageErr = 'Open this page from SMART Sniffer in the Home Assistant sidebar.';
        return;
      }
      this.loadAll();
      this.subscribe();
      setInterval(() => this.tick(), 1000);
      this.watchCols();
      this.watchRows();
      // Links into Home Assistant open in its own window, without a reload.
      document.addEventListener('click', (e) => {
        const a = e.target.closest && e.target.closest('a[target="_top"]');
        const href = a && a.getAttribute('href');
        if (!href || !href.startsWith('/') || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
        e.preventDefault();
        BRIDGE.navigate(href);
      });
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) return;
        this.tick();
        this.resumeScan();
      });
      // The exact-number popover closes on any click outside it, a scroll
      // or Escape.
      document.addEventListener('click', (e) => {
        if (!this.ui.pop) return;
        if (e.target.closest && (e.target.closest('.numpop') || e.target.closest('.bign'))) return;
        this.ui.pop = null;
      }, true);
      // A scroll that lands just after the popover opened is the one that
      // brought the value into view (a tap at the edge, the keyboard's focus
      // scroll): it keeps the popover open.
      window.addEventListener('scroll', () => {
        if (this.ui.pop && Date.now() - this.popOpenedAt > 300) this.ui.pop = null;
      }, { passive: true });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.ui.pop) this.ui.pop = null;
        if (e.key !== 'Escape') return;
        if (this.ui.confirm) this.closeConfirm();
        if (this.ui.arm) this.disarm();
      });
    },

    // Every timer waits while the page is hidden.
    tick() {
      this.now = Date.now();
    },

    // One of the page's App routes, as its smart_sniffer/panel/* command.
    // The answer has the App's shape: { ok, status, data }; a refused
    // command gives its error code (and message) in data.
    wsFor(url, opts) {
      const method = opts.method || 'GET';
      const body = opts.body || {};
      const [path, query] = String(url).split('?');
      const p = path.split('/').map(dec);
      const q = new URLSearchParams(query || '');
      if (path === 'cards/order' && method === 'PUT') return { type: 'cards/order', order: body.order };
      if (p[0] === 'cards' && p.length === 3 && p[2] === 'name') return { type: 'card/name', card_id: p[1], name: body.name };
      if (p[0] === 'cards' && p.length === 3 && p[2] === 'thresholds' && method === 'GET') return { type: 'thresholds/get', card_id: p[1] };
      if (p[0] === 'cards' && p.length === 3 && p[2] === 'thresholds' && method === 'PUT') return { type: 'thresholds/set', card_id: p[1], values: body.values };
      if (p[0] === 'cards' && p.length === 4 && p[2] === 'thresholds' && p[3] === 'accept') return { type: 'thresholds/accept', card_id: p[1] };
      if (p[0] === 'cards' && p.length === 2 && method === 'PATCH') return { type: 'card/hidden', card_id: p[1], hidden: body.hidden };
      if (p[0] === 'cards' && p.length === 2 && method === 'DELETE') {
        const msg = { type: 'card/remove', card_id: p[1] };
        if (q.get('ha')) msg.ha = q.get('ha');
        return msg;
      }
      if (p[0] === 'hosts' && p.length === 3 && p[2] === 'name') return { type: 'machine/name', entry_id: p[1], name: body.name };
      if (p[0] === 'fleet' && p[1] === 'hosts' && p.length === 3 && method === 'PATCH') return { type: 'machine/password', entry_id: p[2], password: body.password };
      if (path === 'fleet/probe') return { type: 'net/check', host: body.host, port: body.port };
      if (path === 'fleet/scan' && method === 'POST') return { type: 'net/scan', block: body.block, port: body.port };
      if (p[0] === 'fleet' && p[1] === 'scan' && p.length === 3) {
        return { type: method === 'DELETE' ? 'net/scan/cancel' : 'net/scan/get', scan_id: p[2] };
      }
      if (path === 'fleet/hosts' && method === 'POST') return { type: 'agent/add', hosts: body.hosts };
      return null;
    },

    async request(url, opts = {}) {
      const msg = this.wsFor(url, opts);
      if (!msg) return { ok: false, status: 404, data: { error: 'not_found' } };
      return this.ws(msg);
    },

    async ws(msg) {
      try {
        const data = await BRIDGE.callWS({ ...msg, type: WS + msg.type });
        return { ok: true, status: 200, data };
      } catch (e) {
        const code = (e && e.code) || 'error';
        return { ok: false, status: WS_STATUS[code] || 400, data: { error: code, message: (e && e.message) || '' } };
      }
    },

    // ── Loads ───────────────────────────────────────────────────────
    // Everything the page draws, in one answer; then each machine's new
    // read as the integration pushes it.
    async loadAll() {
      const r = await this.ws({ type: 'cards' });
      if (!r.ok || !r.data) {
        this.hostsErr = 'SMART Sniffer did not answer. Refresh the page.';
        return;
      }
      this.hostsErr = '';
      this.applyAll(r.data);
    },

    applyAll(data) {
      this.applyInfo(data.info);
      this.applyState(data.state);
      const ha = { installed: true, reachable: true, lookup: 'registry', targets: {}, ha_removal: {} };
      const hosts = [];
      const hostData = {};
      (data.machines || []).forEach((m) => {
        hosts.push(m.host);
        hostData[m.host.id] = m.data;
        ha.targets[m.host.id] = m.ha.target;
        Object.assign(ha.ha_removal, m.ha.removal || {});
      });
      this.ha = ha;
      this.hostData = hostData;
      this.hosts = hosts;
      this.loaded = true;
    },

    applyInfo(info) {
      if (!info) return;
      this.info = info;
      const v = String(window.SS_PAGE_VERSION || '').split('+')[0];  // version+digest
      if (v && info.version && v !== info.version) this.pageErr = 'Restart Home Assistant to finish the update.';
    },

    applyState(state) {
      if (!state) return;
      this.order = state.order || [];
      this.hidden = state.hidden || [];
      this.notes = state.notes || {};
      if (!this.ui.rename) {
        this.names = state.names || {};
        this.hostNames = state.host_names || {};
      }
      if (!this.hidden.length) this.showHidden = false;
    },

    // One machine's new read, or the machine gone.
    applyMachine(m) {
      const id = m.host.id;
      const at = this.hosts.findIndex((h) => h.id === id);
      if (at < 0) this.hosts = this.hosts.concat([m.host]);
      else Object.assign(this.hosts[at], m.host);
      // A machine added while the page is open (Home Assistant's Discovered
      // card, another admin, + here) lands at the end of the grid: say so,
      // with a way to it.
      if (at < 0 && this.loaded) this.newMachine(m.host);
      this.hostData[id] = m.data;
      if (!this.ha) this.ha = { installed: true, reachable: true, lookup: 'registry', targets: {}, ha_removal: {} };
      this.ha.targets[id] = m.ha.target;
      const removal = {};
      Object.entries(this.ha.ha_removal).forEach(([cid, mode]) => { if (!cid.startsWith(`${id}|`)) removal[cid] = mode; });
      this.ha.ha_removal = Object.assign(removal, m.ha.removal || {});
    },

    dropMachine(id) {
      this.hosts = this.hosts.filter((h) => h.id !== id);
      delete this.hostData[id];
      if (this.ha && this.ha.targets) delete this.ha.targets[id];
    },

    subscribe() {
      Promise.resolve(BRIDGE.subscribe({ type: `${WS}subscribe` }, (ev) => {
        if (!ev) return;
        if (ev.type === 'machine' && ev.machine) this.applyMachine(ev.machine);
        else if (ev.type === 'machine_removed') this.dropMachine(ev.entry_id);
        else if (ev.type === 'state') this.applyState(ev.state);
        else if (ev.type === 'info') this.applyInfo(ev.info);
      })).catch(() => { this.hostsErr = 'SMART Sniffer stopped sending updates. Refresh the page.'; });
    },

    async loadHost(id) {
      const r = await this.ws({ type: 'machine', entry_id: id });
      if (!r.ok || !r.data) return;
      if (r.data.machine) this.applyMachine(r.data.machine);
      else this.dropMachine(id);
    },

    async loadHosts() {
      await this.loadAll();
    },

    // Kept for the App's callers: Home Assistant's devices come with each
    // machine.
    loadHa() {},
    haSoon() {},
    // The replica library for the + card's Replica area: every file by
    // source, the user folder's state, the caps and how many are used.
    async loadReplicas() {
      if (!(this.info && this.info.replica)) return;
      const r = await this.ws({ type: 'replica/library' });
      if (r.ok && r.data) this.replicaFiles = r.data;
    },

    async refresh() {
      const r = await this.ws({ type: 'refresh' });
      if (r.ok && r.data) this.applyAll(r.data);
    },

    // ── Home Assistant pages ────────────────────────────────────────
    get integrationInstalled() {
      return true;
    },
    get integrationUrl() {
      return (this.info && this.info.integration_url) || '/config/integrations/integration/smart_sniffer';
    },
    // No machine on the page (every machine deleted this run, or every card
    // of every machine removed): the empty line above the + card.
    get noMachines() {
      return this.loaded && !this.allCards.length;
    },
    // The "Install the SMART Sniffer App" note: HAOS or Supervised, no App.
    get appNote() {
      return !!(this.info && this.info.app_note) && !(this.notes && this.notes.app_hint);
    },
    get appNoteHref() {
      return APP_STORE_PUBLIC;
    },
    // The App store opens in Home Assistant itself (its own My redirect).
    openAppStore(e) {
      if (!BRIDGE) return;
      e.preventDefault();
      BRIDGE.navigate(APP_STORE_LOCAL);
    },
    async dismissAppNote() {
      this.notes = { ...this.notes, app_hint: true };
      await this.ws({ type: 'note/dismiss', note: 'app_hint' });
    },
    // Once, after the integration removed the SMART Sniffer App's old
    // Replica machine.
    get movedNote() {
      return !!(this.notes && this.notes.replicas_moved === false);
    },
    async dismissMovedNote() {
      this.notes = { ...this.notes, replicas_moved: true };
      await this.ws({ type: 'note/dismiss', note: 'replicas_moved' });
    },
    // Once, after the SMART Sniffer App handed its field machines over
    // (the integration's report; dismissed for every admin).
    get importNote() {
      const r = this.info && this.info.app_import;
      return r && !this.importClosed ? r : null;
    },
    importText(r) {
      const n = (v, one, many) => `${v} ${v === 1 ? one : many}`;
      const head = r.machines
        ? `Moved ${n(r.machines, 'machine', 'machines')} from the SMART Sniffer App.`
        : 'Moved settings from the SMART Sniffer App.';
      const parts = [];
      if (r.already_here) parts.push(`${r.already_here} already here`);
      if (r.names) parts.push(n(r.names, 'name', 'names'));
      if (r.hidden) parts.push(n(r.hidden, 'hidden card', 'hidden cards'));
      if (r.thresholds) parts.push(n(r.thresholds, 'threshold', 'thresholds'));
      const skipped = (r.skipped || []).length;
      let text = parts.length ? `${head} With them: ${parts.join(', ')}.` : head;
      if (skipped) text += ` ${n(skipped, 'machine', 'machines')} not moved.`;
      return text;
    },
    importReason(s) {
      const why = {
        bad_address: 'not a valid address',
        not_added: 'could not be added',
        error: 'could not be added; the App tries again later',
        store_unreadable: 'the handover record could not be read',
      };
      return `${s.address}:${s.port}: ${why[s.reason] || s.reason}`;
    },
    async dismissImportNote() {
      this.importClosed = true;
      await this.ws({ type: 'app_import/dismiss' });
    },
    async discardImport(s) {
      const r = await this.ws({ type: 'app_import/discard', item_id: s.item_id });
      if (r.ok && this.info && this.info.app_import) {
        const left = (this.info.app_import.skipped || []).filter((x) => x.item_id !== s.item_id);
        this.info = { ...this.info, app_import: { ...this.info.app_import, skipped: left } };
      }
    },

    // ── Formatting ──────────────────────────────────────────────────
    num(n) {
      return typeof n === 'number' ? n.toLocaleString('en-US') : String(n);
    },
    // The one rule for a shortened number, everywhere on the page (the
    // vitals line, the attribute table, Device Statistics, disk space):
    // the largest unit that leaves at least 1, then three significant
    // figures ("5.10 TB", "19.8 TB", "898 GB", "344 M"). Below the smallest
    // unit the number as it is. Only what is shown changes.
    scaled(n, units, below) {
      if (typeof n !== 'number' || !Number.isFinite(n)) return '';
      for (let i = 0; i < units.length; i += 1) {
        const [size, unit] = units[i];
        if (n < size) continue;
        const v = Number((n / size).toPrecision(3));
        // 999.6 GB rounds to 1000: say 1.00 TB instead.
        if (v >= 1000 && i > 0) return `${(n / units[i - 1][0]).toPrecision(3)} ${units[i - 1][1]}`;
        return `${(n / size).toPrecision(3)} ${unit}`;
      }
      return below(n);
    },
    // Sizes: decimal units, as df -H, the drive makers and the agent's totals.
    bytes(n) {
      return this.scaled(n, [[1e15, 'PB'], [1e12, 'TB'], [1e9, 'GB'], [1e6, 'MB'], [1e3, 'kB']], (x) => `${x} B`);
    },
    ago(t) {
      const ms = typeof t === 'number' ? t : Date.parse(t);
      if (!ms) return '';
      const s = Math.max(0, Math.round((this.now - ms) / 1000));
      if (s < 60) return `${s} s ago`;
      if (s < 3600) return `${Math.floor(s / 60)} min ago`;
      if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
      const d = Math.floor(s / 86400);
      return d === 1 ? '1 day ago' : `${d} days ago`;
    },
    plural(n, one, many) {
      return `${n} ${n === 1 ? one : many}`;
    },

    // ── Header ──────────────────────────────────────────────────────
    get rulesVersion() {
      return (this.info && this.info.rules_version) || '';
    },
    get selfEmpty() {
      return false;
    },

    // ── The cards ───────────────────────────────────────────────────
    // A card: { id, kind, src, it, h, fs }. kind is drive, pool,
    // filesystem or machine (a machine with nothing to show: waiting for
    // its password, never read, not answering). src is field for every
    // machine (each a config entry); h.kind says whether it is the SMART
    // Sniffer App's agent (haos), a replica or a field agent.
    cardsFrom(src, data, h) {
      const out = [];
      ((data && data.items) || []).forEach((it) => {
        out.push({ id: it.card_id || `${src === 'field' ? h.id : src}|${it.id}`, kind: it.type, src, it, h, fs: null });
      });
      ((data && data.filesystem_cards) || []).forEach((f) => {
        out.push({ id: f.card_id, kind: 'filesystem', src, it: f, h, fs: f.filesystem });
      });
      return out;
    },
    get allCards() {
      const out = [];
      this.hosts.forEach((h) => {
        const d = this.hostData[h.id];
        const cards = this.pending(h) ? [] : this.cardsFrom('field', d, h);
        if (cards.length) out.push(...cards);
        else if (!(d && d.status === 'ok')) out.push({ id: h.id, kind: 'machine', src: 'field', it: null, h, fs: null });
      });
      return out.filter((c) => !this.gone[c.id]);
    },
    // The stored order first; cards it does not know keep their place among
    // themselves, after it and before the + card.
    get orderedCards() {
      const cards = this.allCards;
      const order = this.dragOrder || this.order;
      const at = new Map(order.map((id, i) => [id, i]));
      return cards.map((c, i) => ({ c, i, o: at.has(c.id) ? at.get(c.id) : Infinity }))
        .sort((a, b) => (a.o - b.o) || (a.i - b.i)).map((x) => x.c);
    },
    get shownCards() {
      return this.orderedCards.filter((c) => this.showHidden || !this.isHidden(c));
    },
    isHidden(c) {
      return this.hidden.includes(c.id);
    },
    get hiddenCount() {
      const ids = new Set(this.allCards.map((c) => c.id));
      return this.hidden.filter((id) => ids.has(id)).length;
    },
    toggleShowHidden() {
      if (!this.hiddenCount && !this.showHidden) return;
      this.showHidden = !this.showHidden;
    },
    cardClass(c) {
      return {
        machine: ['machine', 'down', 'loading'].includes(c.kind),
        stale: this.unanswered(c),
        'is-hidden': this.isHidden(c),
        'is-replica': this.isReplica(c),
        'drop-gap': !!(this.drag && this.drag.id === c.id),
        open: this.isOpen(c),
      };
    },
    // The grid's column count, kept up to date, so the open + card can
    // tell how wide to be.
    watchCols() {
      const grid = document.querySelector('.grid');
      if (!grid) return;
      const read = () => {
        const n = getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length || 1;
        if (n !== this.ui.cols) this.ui.cols = n;
      };
      read();
      if (window.ResizeObserver) new ResizeObserver(read).observe(grid);
    },
    // A card with Details open grows down; every other card in its grid
    // row keeps the height that row had while it was closed, so nothing
    // beside it shrinks or grows. Rows with no card open stretch as usual.
    // Rows are read from the page (cards that share a top), so this holds
    // at any column count, after a drag and after a resize.
    // Every row is pinned again only when the cards themselves change (added,
    // removed, moved, opened, closed) or the grid's width does. A change
    // inside an open card leaves its row's pins as they are; one inside a
    // closed card pins its row again, and only when the card's size changed.
    watchRows() {
      const grid = document.querySelector('.grid');
      if (!grid) return;
      const later = (full, cards) => {
        if (full) rows.full = true;
        (cards || []).forEach((e) => rows.check.add(e));
        if (rows.queued) return;
        rows.queued = true;
        requestAnimationFrame(() => {
          const all = rows.full;
          const check = [...rows.check];
          rows.queued = false;
          rows.full = false;
          rows.check.clear();
          if (all) this.pinRows();
          else this.checkRows(check);
        });
      };
      let width = null;
      if (window.ResizeObserver) {
        new ResizeObserver(() => {
          const w = this.cardW(grid);
          if (w !== width) { width = w; later(true); } else later(false, this.gridItems());
        }).observe(grid);
      }
      const cardOf = (n) => {
        const e = n.nodeType === 1 ? n : n.parentElement;
        return e && e.closest ? e.closest('.grid > [data-card]') : null;
      };
      new MutationObserver((ms) => {
        const cards = new Set();
        for (const m of ms) {
          if (m.target === grid || (m.type === 'attributes' && m.target.parentElement === grid)) { later(true); return; }
          const card = cardOf(m.target);
          if (card && !card.classList.contains('open')) cards.add(card);
        }
        if (cards.size) later(false, [...cards]);
      }).observe(grid, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['class'] });
    },
    gridItems() {
      const grid = document.querySelector('.grid');
      if (!grid) return [];
      return [...grid.children].filter((e) => e.tagName !== 'TEMPLATE' && e.getClientRects().length);
    },
    // A card's height at its own content, as a closed card in a closed row.
    naturalH(e) {
      e.style.height = '';
      e.style.alignSelf = 'start';
      return e.getBoundingClientRect().height;
    },
    cardW(e) {
      return Math.round(e.getBoundingClientRect().width);
    },
    pinRows() {
      if (rows.busy) return;
      const items = this.gridItems();
      const isOpen = (e) => e.classList.contains('open');
      if (!items.some(isOpen)) {
        items.forEach((e) => { e.style.height = ''; e.style.alignSelf = ''; });
        return;
      }
      // An open card's closed height is known only at the width it was
      // measured at; after a resize, measure it again.
      if (items.some((e) => isOpen(e) && !this.knownClosed(e))) {
        this.remeasure();
        return;
      }
      this.pinGroup(items);
    },
    // The rows of these cards, pinned: natural heights first (all writes,
    // then all reads), then each row.
    pinGroup(items) {
      const isOpen = (e) => e.classList.contains('open');
      items.forEach((e) => { e.style.height = ''; e.style.alignSelf = 'start'; });
      const got = items.map((e) => ({
        e, open: isOpen(e), top: Math.round(e.offsetTop),
        h: isOpen(e) ? rows.closedH[e.dataset.card].h : e.getBoundingClientRect().height,
      }));
      const byRow = new Map();
      got.forEach((g) => { if (!byRow.has(g.top)) byRow.set(g.top, []); byRow.get(g.top).push(g); });
      byRow.forEach((row) => {
        if (!row.some((g) => g.open)) {
          row.forEach((g) => { g.e.style.alignSelf = ''; });
          return;
        }
        const h = Math.max(...row.map((g) => g.h));
        row.forEach((g) => {
          if (g.open) { g.e.style.alignSelf = ''; return; }
          g.e.style.height = h + 'px';
        });
      });
      got.forEach((g) => { if (!g.open) rows.sig[g.e.dataset.card] = this.contentH(g.e); });
    },
    // A card's content height, read without touching its style: what its
    // children take, the stretching spacer left out. It changes only when
    // the card's own size would.
    contentH(e) {
      let h = 0;
      for (const k of e.children) {
        if (k.tagName === 'TEMPLATE' || k.classList.contains('c-spacer')) continue;
        h += k.getBoundingClientRect().height;
      }
      return Math.round(h * 2) / 2;
    },
    // Closed cards whose content changed: their rows are pinned again, only
    // where one of them changed size and the row has an open card.
    checkRows(cards) {
      if (rows.busy) return;
      const items = this.gridItems();
      const isOpen = (e) => e.classList.contains('open');
      if (!items.some(isOpen)) return;
      if (items.some((e) => isOpen(e) && !this.knownClosed(e))) { this.remeasure(); return; }
      const tops = new Set();
      cards.forEach((e) => {
        if (!e.isConnected || isOpen(e) || !e.getClientRects().length) return;
        const h = this.contentH(e);
        if (rows.sig[e.dataset.card] === h) return;
        rows.sig[e.dataset.card] = h;
        tops.add(Math.round(e.offsetTop));
      });
      tops.forEach((top) => {
        const row = items.filter((e) => Math.round(e.offsetTop) === top);
        if (row.some(isOpen)) this.pinGroup(row);
      });
    },
    knownClosed(e) {
      const m = rows.closedH[e.dataset.card];
      return !!(m && m.w === this.cardW(e));
    },
    // Close the open cards for one microtask, read their closed heights at
    // the new width, open them again. No frame is painted in between.
    async remeasure() {
      rows.busy = true;
      const flush = () => new Promise((r) => queueMicrotask(r));
      const ids = Object.keys(this.ui.open).filter((k) => this.ui.open[k]);
      const act = document.activeElement;
      const actCard = act && act.closest && act.closest('[data-card]');
      const actKey = actCard && [actCard.dataset.card, act.getAttribute('aria-label'), act.className];
      try {
        ids.forEach((k) => { this.ui.open[k] = false; });
        await flush(); await flush();
        const grid = document.querySelector('.grid');
        ids.forEach((k) => {
          const e = grid && grid.querySelector(`[data-card="${CSS.escape(k)}"]`);
          if (e) rows.closedH[k] = { w: this.cardW(e), h: this.naturalH(e) };
        });
      } finally {
        ids.forEach((k) => { this.ui.open[k] = true; });
        await flush(); await flush();
        rows.busy = false;
      }
      if (actKey) {
        const card = document.querySelector(`.grid [data-card="${CSS.escape(actKey[0])}"]`);
        const back = card && [...card.querySelectorAll('button, input, a, [tabindex]')]
          .find((x) => x.getAttribute('aria-label') === actKey[1] && x.className === actKey[2]);
        if (back) back.focus({ preventScroll: true });
      }
      this.pinRows();
    },

    // Where a card comes from.
    machine(c) {
      return this.hostName(c.h).toUpperCase();
    },
    // The SMART Sniffer App's agent (HAOS) and replica machines keep their
    // fixed labels and show their cards as the App showed its own.
    isHaos(c) {
      return !!(c.h && c.h.kind === 'haos');
    },
    isReplica(c) {
      return !!(c.h && c.h.kind === 'replica');
    },
    // Every card's pill names its machine; a pool or filesystem card says
    // what it is on the line under its title (kindWord).
    pill(c) {
      return this.machine(c);
    },
    kindWord(c) {
      if (c.kind === 'pool') return 'ZFS pool';
      if (c.kind === 'filesystem') return 'Filesystem';
      return null;
    },
    // The name the card had before anyone renamed it: the model, the pool's
    // name, the mount point.
    baseTitle(c) {
      if (c.kind === 'down') return "This app's agent is not answering";
      if (c.kind === 'loading') return "This machine's drives";
      if (c.kind === 'machine') return this.hostName(c.h);
      if (c.kind === 'filesystem') return c.fs.mountpoint;
      if (c.kind === 'pool') return c.it.pool.name;
      return (c.it.drive && c.it.drive.model) || (c.it.lab && c.it.lab.name) || c.it.id;
    },
    // Shown: the Home Assistant device's own name when the user gave it one,
    // else the name given here, else the original.
    title(c) {
      if (['down', 'loading', 'machine'].includes(c.kind)) return this.baseTitle(c);
      const ha = this.haNames(c);
      return (ha && ha.name_by_user) || this.names[c.id] || this.baseTitle(c);
    },
    renamed(c) {
      return ['drive', 'pool'].includes(c.kind) && this.title(c) !== this.baseTitle(c);
    },
    modelLine(c) {
      return `${c.kind === 'pool' ? 'Pool' : 'Model'}: ${this.baseTitle(c)}`;
    },

    // ── Rename in place ─────────────────────────────────────────────
    // A card's title (drive, pool, filesystem), and a field agent's machine:
    // its pill, or the title of its card while it has nothing else to show.
    canRename(c) {
      return ['drive', 'pool', 'filesystem', 'machine'].includes(c.kind);
    },
    canRenameHost(c) {
      return c.src === 'field' && !!c.h && !c.h.fixed && ['drive', 'machine', 'pool', 'filesystem'].includes(c.kind);
    },
    renaming(c, what) {
      const r = this.ui.rename;
      return !!(r && r.id === c.id && r.what === what);
    },
    // A click or tap on the title or an editable pill opens the field in
    // the element's own box, so nothing around it moves: the title's field
    // keeps the title's height, the pill's keeps the pill's width.
    startRename(c, what, ev) {
      if (what === 'title' && !this.canRename(c)) return;
      if (what === 'pill' && !this.canRenameHost(c)) return;
      if (this.ui.rename) this.finishRename(true);
      const el = ev && ev.currentTarget;
      const box = el ? (what === 'title' ? el.closest('.c-title') : el).getBoundingClientRect() : null;
      const host = what === 'pill' || c.kind === 'machine';
      const value = host ? this.hostName(c.h) : this.title(c);
      const kb = !!(ev && ev.type === 'keydown');
      this.ui.rename = { id: c.id, what, host, value, was: value, kb, t0: Date.now(),
                         h: box ? box.height : 0, w: box ? box.width : 0 };
      this.$nextTick(() => {
        const f = document.querySelector(`article.card[data-card="${CSS.escape(c.id)}"] .name-in`);
        if (f) { f.focus(); f.select(); }
      });
    },
    // Enter or F2 on the focused title or pill starts editing.
    nameKey(e, c, what) {
      if (e.key !== 'Enter' && e.key !== 'F2') return;
      e.preventDefault();
      this.startRename(c, what, e);
    },
    // The second click of a double-click that opened the field keeps the
    // whole name selected instead of picking one word.
    renameDown(e) {
      const r = this.ui.rename;
      if (r && e.detail > 1 && Date.now() - r.t0 < 700) { e.preventDefault(); e.target.select(); }
    },
    renameKey(e) {
      if (e.key === 'Enter') { e.preventDefault(); this.finishRename(true, true); }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.finishRename(false, true); }
    },
    finishRename(save, byKey) {
      const r = this.ui.rename;
      if (!r) return;
      this.ui.rename = null;
      // Ended from the keyboard: focus goes back to what was renamed.
      if (byKey && r.kb) {
        this.$nextTick(() => {
          const card = document.querySelector(`article.card[data-card="${CSS.escape(r.id)}"]`);
          const back = card && card.querySelector(r.what === 'pill' ? '.pill-ed' : '.t-name');
          if (back) back.focus({ preventScroll: true });
        });
      }
      const name = String(r.value || '').replace(/\s+/g, ' ').trim();
      if (!save || name === r.was) return;
      const c = this.allCards.find((x) => x.id === r.id);
      if (!c) return;
      if (r.host) this.saveHostName(c.h, name);
      else this.saveCardName(c, name);
    },
    async saveHostName(h, name) {
      const was = this.hostNames[h.id];
      if (name) this.hostNames[h.id] = name; else delete this.hostNames[h.id];
      this.pageErr = '';
      const r = await this.request(`hosts/${enc(h.id)}/name`, { method: 'PUT', body: { name } });
      if (!r.ok) {
        if (was) this.hostNames[h.id] = was; else delete this.hostNames[h.id];
        this.pageErr = 'That name was not saved. Try again.';
      }
    },
    async saveCardName(c, name) {
      const was = this.names[c.id];
      const ha = this.haNames(c);
      const haWas = ha ? ha.name_by_user : null;
      if (name) this.names[c.id] = name; else delete this.names[c.id];
      if (ha) ha.name_by_user = name || null;
      this.ui.note[`nm:${c.id}`] = '';
      this.pageErr = '';
      const r = await this.request(`cards/${enc(c.id)}/name`, { method: 'PUT', body: { name } });
      if (!r.ok) {
        if (was) this.names[c.id] = was; else delete this.names[c.id];
        if (ha) ha.name_by_user = haWas;
        this.pageErr = 'That name was not saved. Try again.';
        return;
      }
      if (ha && r.data && r.data.home_assistant === 'failed') {
        ha.name_by_user = haWas;
        this.ui.note[`nm:${c.id}`] = HA_NAME_FAILED;
      }
    },
    // The names Home Assistant has for a card's device, when it has one.
    haNames(c) {
      if (!['drive', 'pool'].includes(c.kind) || this.glyph(c).kind !== 'blue') return null;
      const t = this.ha && this.ha.targets && this.ha.targets[this.target(c)];
      const key = this.haKey(c);
      return (t && t.names && key && t.devices && t.devices[key] && t.names[key]) || null;
    },
    haKey(c) {
      const it = c.it;
      return c.kind === 'pool' ? `pool:${it.pool.name}` : it.drive && (it.drive.serial || `id:${it.id}`);
    },
    footNote(c) {
      return this.ui.note[`dl:${c.id}`] || this.ui.note[`rm:${c.id}`] || this.ui.note[`nm:${c.id}`] || '';
    },
    // The where line, as parts shown with a gap between them.
    where(c) {
      const it = c.it || {};
      const addr = c.h ? `${c.h.address}:${c.h.port}` : '';
      // A Replica card says which library file it shows; it has no address.
      if (this.isReplica(c)) {
        if (c.kind === 'machine' || this.replicaMissing(c)) return [];
        if (c.kind === 'filesystem') return [this.kindWord(c), c.fs.device, c.fs.fstype].filter(Boolean);
        const file = (it.lab && it.lab.name) || '';
        if (c.kind === 'pool') return [this.kindWord(c), file].filter(Boolean);
        return [file];
      }
      if (c.kind === 'machine') return [addr];
      // The App's agent: its cards as the App showed this machine's.
      if (this.isHaos(c)) {
        if (c.kind === 'filesystem') return [this.kindWord(c), c.fs.device, c.fs.fstype].filter(Boolean);
        if (c.kind === 'pool') return [this.kindWord(c), this.poolLayout(it)].filter(Boolean);
        const d = it.drive || {};
        return [d.device_path, this.proto(d)].filter(Boolean);
      }
      // A filesystem or pool card starts with what it is; its pill names
      // the machine, as a drive card's does.
      if (c.kind === 'filesystem') {
        return [this.kindWord(c), c.src === 'field' ? addr : null, c.fs.device, c.fs.fstype].filter(Boolean);
      }
      if (c.kind === 'pool') {
        const layout = this.poolLayout(it);
        if (c.src === 'self') return [this.kindWord(c), layout].filter(Boolean);
        return [this.kindWord(c), addr, layout].filter(Boolean);
      }
      const drive = it.drive || {};
      if (c.src === 'field') return [addr, drive.device_path].filter(Boolean);
      return [drive.device_path, this.proto(drive)].filter(Boolean);
    },
    proto(drive) {
      const dev = (drive.smart_data && drive.smart_data.device) || {};
      if (drive.protocol === 'ATA' && dev.type === 'sat') return 'SATA';
      return drive.protocol || '';
    },
    vitals(c) {
      const it = c.it || {};
      if (c.kind === 'pool') {
        const p = it.pool || {};
        const out = [this.poolState(it)];
        if (p.last_scrub_end) out.push(`scrub ${this.ago(p.last_scrub_end)}`);
        return out.filter(Boolean);
      }
      return [];
    },
    // A drive's stat row: Temp, Powered, Written, Read, Worn, each a tiny
    // label over its value (number, then its unit). A value the drive does
    // not report is left out, tile and all; none at all, no row. A
    // temperature of 0 C is taken as not reported (virtual and unknown
    // disks send 0 for "no sensor"). Written and read keep the exact number
    // behind them (x). Plain: no colours.
    stats(c) {
      if (c.kind !== 'drive') return [];
      const it = c.it || {};
      const s = (it.view && it.view.stats) || {};
      const out = [];
      const tile = (k, text, x) => {
        const m = /^(-?[\d.,]+)(\s*)(.*)$/.exec(text);
        out.push({ k, n: m ? m[1] : text, u: m ? `${m[2]}${m[3]}` : '', x: x || '' });
      };
      if (typeof s.temp_c === 'number' && Number.isFinite(s.temp_c) && s.temp_c !== 0) tile('Temp', `${s.temp_c} C`);
      if (typeof s.power_on_hours === 'number') {
        tile('Powered', this.hoursText(s.power_on_hours), `${this.num(s.power_on_hours)} hours powered on`);
      }
      ['written', 'read'].forEach((which) => {
        const b = this.volBytes(it, which);
        if (b !== null) tile(which === 'written' ? 'Written' : 'Read', this.bytes(b), `${this.num(b)} bytes ${which}`);
      });
      const worn = this.worn(it);
      if (worn !== null) tile('Worn', `${worn}%`);
      return out;
    },
    // Bytes written or read: the agent's volume (Device Statistics, the
    // vendor attribute or the NVMe log), else the NVMe log's data units or
    // the drive's Total LBAs attribute (241, 242) times its sector size.
    volBytes(it, which) {
      const v = this.vol(it, which);
      if (v) return v.bytes;
      const sd = this.smartOf(it);
      const log = sd.nvme_smart_health_information_log;
      const units = log && log[`data_units_${which}`];
      if (typeof units === 'number' && units >= 0) return units * 512000;
      const id = which === 'written' ? 241 : 242;
      const a = ((sd.ata_smart_attributes || {}).table || []).find((x) => x.id === id && /LBA/i.test(String(x.name)));
      const raw = a && a.raw && a.raw.value;
      if (typeof raw === 'number' && raw >= 0) return raw * ((Number(sd.logical_block_size)) || 512);
      return null;
    },
    // Percent of rated endurance used: the NVMe log's percentage_used or the
    // ATA Device Statistics Percentage Used Endurance Indicator (page 7).
    // Never worked out from a vendor's wear attribute.
    worn(it) {
      const log = this.smartOf(it).nvme_smart_health_information_log;
      if (log && typeof log.percentage_used === 'number') return log.percentage_used;
      const e = this.devEntry(it, 7, 8);
      return e && typeof e.value === 'number' ? e.value : null;
    },
    smartOf(it) {
      return (it && it.drive && it.drive.smart_data) || {};
    },
    // Device Statistics as the agent sent them, when it read them.
    devPages(it) {
      const ds = (it && it.drive && it.drive.device_statistics) || {};
      return ds.status === 'present' && Array.isArray(ds.pages) ? ds.pages.filter((p) => p && Array.isArray(p.table)) : [];
    },
    devEntry(it, page, offset) {
      const p = this.devPages(it).find((x) => x.number === page);
      const e = p && p.table.find((x) => x.offset === offset);
      return e && e.flags && e.flags.valid === true ? e : null;
    },

    // Health: attention, maybe, ok, unknown; null for a card with no health line.
    health(c) {
      if (['filesystem', 'machine', 'loading'].includes(c.kind)) return null;
      if (c.kind === 'down') return 'unknown';
      if (this.replicaMissing(c)) return 'unknown';
      const it = c.it;
      if (c.kind === 'pool') {
        if (this.vanished(it)) return 'unknown';
        const s = it.verdict && it.verdict.state;
        return s === 'YES' ? 'attention' : s === 'NO' ? 'ok' : 'unknown';
      }
      return (it.view && it.view.header) || 'unknown';
    },
    failed(c) {
      return c.kind === 'drive' && !!(c.it.view && c.it.view.health_failed);
    },
    vanished(it) {
      return !!((it.view && it.view.vanished) || (it.lab && it.lab.vanished));
    },
    hasDetails(c) {
      return (c.kind === 'drive' || c.kind === 'pool') && !this.replicaMissing(c);
    },
    // A Replica card whose library file is gone.
    replicaMissing(c) {
      return !!(c.it && c.it.error === 'replica_missing');
    },
    get missingText() {
      return MISSING_FILE;
    },
    isOpen(c) {
      return !!this.ui.open[c.id];
    },
    toggleOpen(c) {
      // Before a card opens, note its closed height for its row.
      if (!this.ui.open[c.id]) {
        const e = document.querySelector(`.grid [data-card="${CSS.escape(c.id)}"]`);
        if (e) {
          const keep = [e.style.height, e.style.alignSelf];
          rows.closedH[c.id] = { w: this.cardW(e), h: this.naturalH(e) };
          [e.style.height, e.style.alignSelf] = keep;
        }
      }
      this.ui.open[c.id] = !this.ui.open[c.id];
      if (!this.ui.open[c.id] && this.thrEditing(c)) this.ui.thr = null;
      // Read its thresholds now, so edit mode opens with them at hand.
      if (this.ui.open[c.id] && this.canTune(c)) this.thrRead(c);
    },

    // The problem window: every flagged row, no paging.
    problems(c) {
      if (c.kind === 'drive') {
        return this.rows(c.it).filter((r) => r.mark === 'crit' || r.mark === 'warn')
          .map((r) => ({ key: r.key, label: r.name, note: this.rowNote(r), value: r.value, mark: r.mark }));
      }
      if (c.kind === 'pool') {
        if (!this.poolBad(c.it)) return [];
        const out = [];
        const state = this.poolState(c.it);
        if (state && state !== 'ONLINE') {
          out.push({ key: 'state', label: 'Pool state', note: 'expected ONLINE', value: state, mark: 'crit' });
        }
        this.poolDisks(c.it).filter((d) => d.bad).forEach((d) => {
          const errs = [['read', d.read], ['write', d.write], ['cksum', d.cksum]].filter(([, n]) => n)
            .map(([k, n]) => `${this.num(n)} ${k}`);
          out.push({ key: `disk:${d.name}`, label: d.name, note: 'expected ONLINE, no errors',
            value: [d.state, ...errs].join(', '), mark: 'crit' });
        });
        return out;
      }
      return [];
    },
    problemTone(c) {
      return this.problems(c).some((p) => p.mark === 'crit') ? 'crit' : 'warn';
    },

    rows(it) {
      return (it.view && it.view.rows) || [];
    },
    // What sits in brackets after a row's label: "accepted" for a reading
    // a threshold keeps from being flagged; the threshold when it is not the
    // default, else the expectation when the row is flagged; then where the
    // reading came from. Never by the value.
    rowNote(r) {
      const flagged = r.mark === 'crit' || r.mark === 'warn';
      const limit = r.limit ? r.limit.text : (flagged ? r.expect : null);
      return [r.accepted ? 'accepted' : null, limit, r.note].filter(Boolean).join(', ');
    },
    vol(it, which) {
      const v = it.view && it.view.volumes && it.view.volumes[which];
      return v && typeof v.bytes === 'number' ? v : null;
    },
    // The top of a drive's Details: what it is and how it is attached. A
    // field the drive's data does not have is left out. Model, serial and
    // firmware (t) open the exact-number popover with the full text and Copy.
    ident(c) {
      const it = c.it || {};
      const d = it.drive || {};
      const sd = this.smartOf(it);
      const out = [];
      const model = d.model || sd.model_name;
      if (model) out.push({ k: 'Model', v: String(model), t: true });
      const serial = d.serial || sd.serial_number;
      if (serial) out.push({ k: 'Serial', v: String(serial), t: true });
      if (sd.firmware_version) out.push({ k: 'Firmware', v: String(sd.firmware_version), t: true });
      const cap = (sd.user_capacity && sd.user_capacity.bytes) || sd.nvme_total_capacity;
      if (typeof cap === 'number' && cap > 0) out.push({ k: 'Capacity', v: this.bytes(cap), x: `${this.num(cap)} bytes` });
      const link = this.link(d, sd);
      if (link) out.push({ k: 'Interface', v: link });
      return out;
    },
    // NVMe; SATA with the link speed smartctl reports (and the drive's top
    // speed when the link runs slower); else the protocol the agent names.
    link(d, sd) {
      const dev = sd.device || {};
      const proto = String(d.protocol || dev.protocol || '');
      if (sd.nvme_smart_health_information_log || /nvme/i.test(proto) || dev.type === 'nvme') return 'NVMe';
      if (/^ata$/i.test(proto) || dev.type === 'sat' || dev.type === 'ata') {
        const speed = sd.interface_speed || {};
        const cur = speed.current && speed.current.string;
        const max = speed.max && speed.max.string;
        if (cur) return max && max !== cur ? `SATA ${cur} (max ${max})` : `SATA ${cur}`;
        return max ? `SATA, max ${max}` : 'SATA';
      }
      return proto;
    },
    unsupportedNote(c) {
      return c.kind === 'drive' && this.health(c) === 'unknown' && !!c.it.verdict;
    },
    reportKind(it) {
      const sd = (it.drive && it.drive.smart_data) || {};
      if (sd.nvme_smart_health_information_log) return 'nvme';
      if (((sd.ata_smart_attributes || {}).table || []).length) return 'ata';
      return 'none';
    },
    // Details holds one table. An ATA drive: its attribute table, with each
    // watched row's mark and note on the attribute it reads, and a watched
    // attribute the drive does not report as a row of its own. A Device
    // Statistics reading that stands in for one is marked in the Device
    // Statistics table below (devstatTable); only when the pages are not at
    // hand does it sit here, after the row it stands in for. An NVMe drive:
    // its health log, the same way. Anything else: the watched rows.
    detailTable(it) {
      const kind = this.reportKind(it);
      const sd = (it.drive && it.drive.smart_data) || {};
      const entries = [];
      const extras = [];
      const place = (r, entry) => {
        if (r.mark === 'crit' || r.mark === 'warn') entry.mark = r.mark;
        const note = this.rowNote(r);
        if (note) entry.note = note;
      };
      const extra = (r, sortId) => {
        const missing = r.value === 'not known';
        extras.push({ key: `r:${r.key}:${r.name}`, id: (r.key !== 'wear' && ATA_IDS[r.key]) || '', name: r.name, value: missing ? '' : r.value,
          worst: '', thresh: '', raw: missing ? '' : r.value, missing, gap: 'not reported', note: this.rowNote(r),
          mark: r.mark === 'crit' || r.mark === 'warn' ? r.mark : null, sortId });
      };
      if (kind === 'ata') {
        const pad = (n) => (typeof n === 'number' ? String(n).padStart(3, '0') : '');
        ((sd.ata_smart_attributes || {}).table || []).forEach((a) => entries.push({
          key: `a:${a.id}`, id: a.id, name: a.name, value: pad(a.value), worst: pad(a.worst), thresh: pad(a.thresh),
          raw: (a.raw && (a.raw.string ?? String(a.raw.value))) || '', rawValue: a.raw ? a.raw.value : '',
          mark: null, note: '', field: a.name,
        }));
        this.rows(it).forEach((r) => {
          // In the Device Statistics table, unless that entry is one the
          // stat row shows (endurance used): then it stays a row here.
          const at = DEVSTAT_ROWS[r.key];
          if (this.devRowEntry(it, r) && !DEVSTAT_IN_STATS.has(`${at[0]}:${at[1]}`)) return;
          const id = r.key === 'wear' ? null : ATA_IDS[r.key];
          const hit = r.value === 'not known' ? null : r.key === 'wear'
            ? entries.find((e) => WEAR_NAMES.includes(e.name)) : entries.find((e) => e.id === id);
          if (hit) { place(r, hit); return; }
          const base = DEVSTAT_ROWS[r.key] ? DEVSTAT_ROWS[r.key][2] : r.key;
          extra(r, ATA_IDS[base] && base !== 'wear' ? ATA_IDS[base] : 1000);
        });
        // Each extra row by the attribute number it reads, else at the end.
        extras.forEach((x) => {
          x.value = '';
          const at = entries.findIndex((e) => typeof e.id === 'number' && e.sortId === undefined && e.id > x.sortId);
          if (at < 0) entries.push(x); else entries.splice(at, 0, x);
        });
        return { kind, rows: this.cleanRaw(kind, entries, sd) };
      }
      if (kind === 'nvme') {
        const log = sd.nvme_smart_health_information_log || {};
        Object.entries(log).forEach(([k, v]) => entries.push({
          key: `n:${k}`, name: k, value: Array.isArray(v) ? v.join(' ') : String(v), mark: null, note: '',
          field: k, rawValue: v,
        }));
        this.rows(it).forEach((r) => {
          const hit = r.value === 'not known' ? null : entries.find((e) => e.field === NVME_FIELDS[r.key]);
          if (hit) place(r, hit); else extra(r, 1000);
        });
        extras.forEach((x) => entries.push(x));
        return { kind, rows: this.cleanRaw(kind, entries, sd) };
      }
      // An unsupported drive: its watched rows are not known, not missing.
      this.rows(it).forEach((r) => extra(r, 1000));
      extras.forEach((x) => { x.gap = 'not known'; });
      return { kind, rows: this.cleanRaw(kind, extras, sd) };
    },
    // What the Raw (or Value) cell shows, the exact number in its tooltip
    // and in a small popover on click or tap (with Copy). Counts of LBAs
    // (Total LBAs Written and Read, 241 and 242 on most drives) as a size:
    // LBAs times the drive's logical sector size (512 when it does not say);
    // NVMe data units the same way (one unit is 512,000 bytes); power-on
    // hours as days; other large counts with separators, or M, B, T past a
    // million. Only what is shown changes: the data, sensors and Agentic
    // access keep the numbers as the drive gave them.
    cleanRaw(kind, rows, sd) {
      const sector = (sd && Number(sd.logical_block_size)) || 512;
      rows.forEach((e) => {
        const text = kind === 'ata' ? e.raw : e.value;
        e.disp = text;
        e.exact = '';
        if (e.missing || !e.field) return;
        const n = typeof e.rawValue === 'number' ? e.rawValue : NaN;
        if (!Number.isFinite(n) || n < 0 || !/^\d+$/.test(String(text).trim())) return;
        const field = String(e.field);
        if (kind === 'ata' && /LBA/i.test(field)) {
          e.disp = this.bytes(n * sector);
          e.exact = `${this.num(n)} LBAs`;
        } else if (kind === 'nvme' && /^data_units_(read|written)$/.test(field)) {
          e.disp = this.bytes(n * 512000);
          e.exact = `${this.num(n)} data units`;
        } else if ((kind === 'ata' && (e.id === 9 || /^Power_On_Hours/i.test(field))) || (kind === 'nvme' && field === 'power_on_hours')) {
          e.disp = this.hoursText(n);
          e.exact = `${this.num(n)} hours`;
        } else if (n >= 10000) {
          e.disp = this.compact(n);
          e.exact = this.num(n);
        }
      });
      return rows;
    },
    // A table name in pieces, each ending at an underscore, so it can wrap
    // there (a <wbr> after each piece) and never inside a word.
    nameParts(name) {
      return String(name ?? '').split(/(?<=_)/);
    },
    // Hours as days ("887 d"), under 48 hours as hours.
    hoursText(h) {
      return h < 48 ? `${this.num(h)} h` : `${this.num(Math.round(h / 24))} d`;
    },
    // A large count: separators up to a million, then M, B or T, by the
    // same rule as a size.
    compact(n) {
      return this.scaled(n, [[1e12, 'T'], [1e9, 'B'], [1e6, 'M']], (x) => this.num(x));
    },
    // The exact number behind a shown one, in a small popover by it. Copy
    // takes its digits; a text value (model, serial, firmware, host, port)
    // passes its own copy text, the whole value.
    showExact(ev, text, copyText) {
      const el = ev.currentTarget;
      const r = el.getBoundingClientRect();
      if (this.ui.pop && this.ui.pop.el === el) { this.ui.pop = null; return; }
      const left = Math.max(8, Math.min(r.left, window.innerWidth - 240));
      const copy = copyText === undefined ? String(text).replace(/[^0-9]/g, '') : String(copyText);
      this.popOpenedAt = Date.now();
      this.ui.pop = { el, text, copy, x: left, y: r.bottom + 4 };
      // Then by its real width: under the value where it fits, else as far
      // right as the window allows (a long value never runs off the edge).
      this.$nextTick(() => {
        const box = document.querySelector('.numpop');
        if (!box || !this.ui.pop || this.ui.pop.el !== el) return;
        const x = Math.max(8, Math.min(r.left, window.innerWidth - box.getBoundingClientRect().width - 8));
        if (x !== this.ui.pop.x) this.ui.pop.x = x;
      });
    },
    closeExact() {
      this.ui.pop = null;
    },
    // The Device Statistics entry a watched row reads (the integration's
    // stand-in for a missing attribute), when the pages have it.
    devRowEntry(it, r) {
      const at = DEVSTAT_ROWS[r.key];
      return at ? this.devEntry(it, at[0], at[1]) : null;
    },
    // The Device Statistics table: every valid entry of the pages the agent
    // read, by a plain name, its value shown by the same rules as the
    // attribute table (sizes, days, large counts, each with the exact number
    // behind it). A watched row that reads an entry marks it here.
    devstatTable(it) {
      const pages = this.devPages(it);
      if (!pages.length) return [];
      const ds = it.drive.device_statistics || {};
      const sector = Number(ds.logical_block_size) || Number(this.smartOf(it).logical_block_size) || 512;
      const marks = {};
      this.rows(it).forEach((r) => {
        const at = DEVSTAT_ROWS[r.key];
        if (at) marks[`${at[0]}:${at[1]}`] = r;
      });
      const out = [];
      pages.forEach((p) => p.table.forEach((e) => {
        if (!e || !e.flags || e.flags.valid !== true || typeof e.value !== 'number') return;
        const key = `${p.number}:${e.offset}`;
        const known = DEVSTAT_NAMES[key];
        if (known === null) return; // not a reading (the drive's clock)
        if (DEVSTAT_IN_STATS.has(key)) return; // in the card's stat row
        const [name, unit] = known || [String(e.name || key), 'count'];
        const row = { key: `d:${key}`, name, title: `${e.name || name} (page ${p.number})`, disp: '', exact: '', mark: null, note: '' };
        const n = e.value;
        if (unit === 'sectors') { row.disp = this.bytes(n * sector); row.exact = `${this.num(n)} sectors`; }
        else if (unit === 'hours') { row.disp = this.hoursText(n); row.exact = `${this.num(n)} hours`; }
        else if (unit === 'temp') row.disp = `${n} C`;
        else if (unit === 'pct') row.disp = `${n}%`;
        else if (unit === 'minutes') row.disp = `${this.num(n)} min`;
        else if (n >= 10000) { row.disp = this.compact(n); row.exact = this.num(n); }
        else row.disp = this.num(n);
        const r = marks[key];
        if (r) {
          if (r.mark === 'crit' || r.mark === 'warn') row.mark = r.mark;
          row.note = this.rowNote(r).replace(/(, )?from device statistics/, '');
        }
        out.push(row);
      }));
      return out;
    },

    // ── Thresholds ──────────────────────────────────────────────────
    // A drive's thresholds, edited in its open Details. The proxy keeps
    // them where they live: in Home Assistant for a drive in it, else in
    // the app's options.
    canTune(c) {
      return c.kind === 'drive' && !!(c.it.verdict && (c.it.verdict.limits || []).length) && !c.it.stale;
    },
    // The edit state, never null, for the template's bindings.
    get thrS() {
      return this.ui.thr || { values: {}, data: null, busy: false, err: '', loading: false };
    },
    thrSet(label, value) {
      if (this.ui.thr) this.ui.thr.values[label] = value;
    },
    thrEditing(c) {
      return !!(this.ui.thr && this.ui.thr.id === c.id);
    },
    // Edit mode shows in place of the card's table once the thresholds are
    // at hand; while they are still being read the table stays and only the
    // tune button shows it is waiting.
    thrShown(c) {
      return this.thrEditing(c) && !this.ui.thr.loading;
    },
    thrWaiting(c) {
      return this.thrEditing(c) && this.ui.thr.loading;
    },
    // Read a drive's thresholds into its own cache entry; one read at a time
    // per card. Nothing else on the page changes.
    thrRead(c) {
      if (thrReads.has(c.id)) return thrReads.get(c.id);
      const id = c.id;
      const seq = thrSeq.get(id) || 0;
      const read = this.request(`cards/${enc(id)}/thresholds`).then((r) => {
        if (thrReads.get(id) === read) thrReads.delete(id);
        const entry = this.thrCache[id] || (this.thrCache[id] = { data: null, err: '' });
        if ((thrSeq.get(id) || 0) !== seq) return entry;
        if (r.ok && r.data) { entry.data = r.data; entry.err = ''; } else entry.err = 'The thresholds could not be read. Try again.';
        const t = this.ui.thr;
        if (t && t.id === id && t.loading) {
          t.loading = false;
          if (entry.err) t.err = entry.err;
          else this.thrLoad(t, entry.data);
        }
        return entry;
      });
      thrReads.set(id, read);
      return read;
    },
    toggleThr(c) {
      if (this.thrEditing(c)) { this.ui.thr = null; return; }
      const entry = this.thrCache[c.id];
      const ready = !!(entry && entry.data);
      this.ui.thr = { id: c.id, loading: !ready, data: null, values: {}, busy: false, err: '' };
      if (ready) this.thrLoad(this.ui.thr, entry.data);
      else this.thrRead(c);
    },
    thrLoad(t, data) {
      t.data = data;
      t.values = {};
      (data.labels || []).forEach((l) => { t.values[l.label] = String(l.value); });
    },
    // The rows edit mode shows: the labels the drive reports (and any with
    // an override), in the order of the drive's watched rows.
    thrRows(c) {
      const t = this.ui.thr;
      if (!t || !t.data) return [];
      const keys = this.rows(c.it).map((r) => r.key);
      const at = (l) => { const i = keys.indexOf(l.row); return i < 0 ? 1000 : i; };
      return (t.data.labels || []).filter((l) => l.reading !== null && l.reading !== undefined || l.overridden)
        .map((l, i) => ({ l, i })).sort((a, b) => (at(a.l) - at(b.l)) || (a.i - b.i)).map((x) => x.l);
    },
    thrMark(c, l) {
      if (!l.flagged) return '';
      const row = this.rows(c.it).find((r) => r.key === l.row);
      return row && row.mark === 'warn' ? 'warn' : (row && row.mark === 'crit' ? 'crit' : 'warn');
    },
    thrReading(l) {
      if (l.reading === null || l.reading === undefined) return 'not reported';
      return `${this.num(l.reading)}${l.unit || ''}`;
    },
    thrWord(l) {
      return COMPARE_WORDS[l.compare] || 'alert above';
    },
    thrWhere() {
      const t = this.ui.thr;
      if (!t || !t.data) return '';
      return t.data.source === 'integration' ? 'Saved in Home Assistant' : 'Saved in this app';
    },
    thrFlagged(c) {
      return this.thrRows(c).filter((l) => l.flagged);
    },
    // A field whose text is not the value saved: changed here, not yet sent.
    thrPending(l) {
      const t = this.ui.thr;
      return !!(t && t.data && String(t.values[l.label] ?? '').trim() !== String(l.value));
    },
    thrAnyPending(c) {
      return this.thrRows(c).some((l) => this.thrPending(l)) || this.repRawRows(c).some((r) => this.repRawPending(r));
    },
    // A Replica drive's readings, editable in the same editor: each SMART
    // attribute's raw value, or the NVMe log's temperature, available
    // spare, percentage used and media errors, and the Device Statistics
    // entries the integration reads for a missing attribute (reported
    // uncorrectable, reallocated, endurance used). Saved into the user's own
    // copy of its file (replica/edit).
    repRawRows(c) {
      if (!this.isReplica(c) || c.kind !== 'drive' || !this.thrEditing(c)) return [];
      const sd = (c.it.drive && c.it.drive.smart_data) || {};
      const out = [];
      ((sd.ata_smart_attributes || {}).table || []).forEach((a) => {
        const v = a.raw && a.raw.value;
        if (typeof v !== 'number') return;
        const shown = [190, 194].includes(a.id) ? (v & 0xff) : v;
        out.push({ key: `ata:${a.id}`, kind: 'ata', field: String(a.id), name: `${a.id} ${a.name}`, now: shown,
          max: [190, 194].includes(a.id) ? 150 : 2 ** 48 - 1 });
      });
      const log = sd.nvme_smart_health_information_log || {};
      Object.entries(NVME_EDITS).forEach(([field, [name, max]]) => {
        if (typeof log[field] === 'number') out.push({ key: `nvme:${field}`, kind: 'nvme', field, name, now: log[field], max });
      });
      Object.entries(DEVSTAT_EDITS).forEach(([field, max]) => {
        const [page, offset] = field.split(':').map(Number);
        const e = this.devEntry(c.it, page, offset);
        if (!e || typeof e.value !== 'number') return;
        out.push({ key: `devstat:${field}`, kind: 'devstat', field, name: `${DEVSTAT_NAMES[field][0]} (Device Statistics)`, now: e.value, max });
      });
      return out;
    },
    repRawText(r) {
      const t = this.ui.thr;
      return t && t.raw && r.key in t.raw ? t.raw[r.key] : String(r.now);
    },
    repRawSet(r, value) {
      const t = this.ui.thr;
      if (!t) return;
      t.raw = { ...(t.raw || {}), [r.key]: value };
    },
    repRawPending(r) {
      const t = this.ui.thr;
      return !!(t && t.raw && r.key in t.raw && String(t.raw[r.key]).trim() !== String(r.now));
    },
    // The changed readings sent; false when one is not a number in range.
    async repRawSave(c) {
      const t = this.ui.thr;
      const rows = this.repRawRows(c).filter((r) => this.repRawPending(r));
      if (!rows.length) return true;
      const body = { type: 'replica/edit', card_id: c.id, ata: {}, nvme: {}, devstat: {} };
      for (const r of rows) {
        const text = String(t.raw[r.key]).trim();
        const n = Number(text);
        if (!/^\d+$/.test(text) || n > r.max) { t.err = `${r.name}: a whole number from 0 to ${this.num(r.max)}.`; return false; }
        body[r.kind][r.field] = n;
      }
      t.busy = true;
      const res = await this.ws(body);
      t.busy = false;
      if (!res.ok) {
        t.err = `Not saved: ${(res.data && res.data.message) || 'try again'}.`;
        return false;
      }
      t.raw = {};
      if (res.data && res.data.switched) this.showToast('Saved as a copy in your replicas.', res.data.file && res.data.file.name);
      return true;
    },
    async thrSend(c, method, path, body) {
      const t = this.ui.thr;
      if (!t || t.busy) return;
      t.busy = true;
      t.err = '';
      const r = await this.request(`cards/${enc(c.id)}/thresholds${path}`, body === undefined ? { method } : { method, body });
      t.busy = false;
      if (!r.ok || !r.data) {
        const msg = r.data && r.data.message;
        t.err = r.status === 502 ? `Home Assistant did not save them${msg ? `: ${msg}` : ''}.`
          : r.status === 400 && msg ? `Not saved: ${msg}.` : 'They were not saved. Try again.';
        return;
      }
      this.ui.thr = null;
      thrSeq.set(c.id, (thrSeq.get(c.id) || 0) + 1);
      thrReads.delete(c.id);
      const entry = this.thrCache[c.id] || (this.thrCache[c.id] = { data: null, err: '' });
      entry.data = r.data;
      entry.err = '';
      await this.loadHost(c.h.id);
      // The drive judged again: its readings in the thresholds too.
      if (this.isOpen(c)) this.thrRead(c);
    },
    thrSave(c) {
      const t = this.ui.thr;
      if (!t || !t.data) return;
      const values = {};
      for (const l of this.thrRows(c)) {
        const text = String(t.values[l.label] ?? '').trim();
        if (!/^\d+$/.test(text)) { t.err = `${l.name}: a whole number from 0 up.`; return; }
        values[l.label] = Number(text);
      }
      t.err = '';
      this.repRawSave(c).then((ok) => { if (ok) this.thrSend(c, 'PUT', '', { values }); });
    },
    // Accept and Reset only fill the fields; Save is what writes. Accept
    // gives each flagged reading's field the value the proxy's accept
    // would store: the reading, or one more for an "alert at" threshold.
    thrAccept(c) {
      const t = this.ui.thr;
      if (!t || !t.data || t.busy) return;
      t.err = '';
      this.thrFlagged(c).forEach((l) => {
        const reading = Number(l.reading);
        t.values[l.label] = String(l.compare === 'ge' ? reading + 1 : reading);
      });
    },
    thrReset() {
      const t = this.ui.thr;
      if (!t || !t.data || t.busy) return;
      t.err = '';
      (t.data.labels || []).forEach((l) => { t.values[l.label] = String(l.default); });
    },

    // ── Connect by hand ─────────────────────────────────────────────
    // What to type into the integration to add a machine by hand: this
    // app's agent as Home Assistant reaches it, or a field agent's address.
    connect(c) {
      if (!['drive', 'pool'].includes(c.kind) || this.isReplica(c)) return null;
      if (!c.h) return null;
      return { fields: [{ k: 'Host', v: c.h.address }, { k: 'Port', v: String(c.h.port) }], password: !!c.h.has_password };
    },
    // The SMART Sniffer App's agent, for the line at the top of the page:
    // only when the App is on this Home Assistant and its agent is a machine.
    get topAgent() {
      const line = this.info && this.info.agent_line;
      if (!line || !line.addr) return null;
      return { addr: line.addr, mdns: line.mdns || '' };
    },
    // Copy to the clipboard; where the browser refuses (an insecure page, a
    // frame without the permission) select the text so it can be copied by
    // hand.
    async copy(ev, text, key) {
      const btn = ev.currentTarget;
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          ok = true;
        }
      } catch (e) { ok = false; }
      if (!ok) {
        const el = btn && btn.parentElement && btn.parentElement.querySelector('.cp-v');
        if (el) {
          const range = document.createRange();
          range.selectNodeContents(el);
          const sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        }
      }
      // Copied: nothing stays selected, so a phone leaves no selection
      // handles over the value. Not copied: the value stays selected, to
      // copy by hand.
      if (ok) {
        try { window.getSelection().removeAllRanges(); } catch (e) { /* nothing to clear */ }
      }
      this.ui.copied = key;
      this.ui.copiedText = ok ? 'copied' : 'selected';
      clearTimeout(this.copyTimer);
      this.copyTimer = setTimeout(() => { if (this.ui.copied === key) this.ui.copied = ''; }, 1600);
    },

    // Pools
    poolBad(it) {
      return !!(it.verdict && it.verdict.state === 'YES');
    },
    poolLayout(it) {
      const v = it.view || {};
      if (v.layout) return v.layout;
      return null;
    },
    poolState(it) {
      return (it.view && it.view.state) || (it.pool && it.pool.state) || '';
    },
    poolDisks(it) {
      const v = it.view || {};
      const disks = v.disks || (it.lab && it.lab.devices);
      const bad = (d) => d.state !== 'ONLINE' || !!(d.read || d.write || d.cksum);
      if (disks) return disks.map((d) => ({ ...d, bad: d.mark === 'crit' || bad(d) }));
      // A real agent lists only its problem vdevs.
      return ((it.pool && it.pool.problem_vdevs) || []).filter((p) => !/^(mirror|raidz|draid|spare|log|cache)/.test(String(p.type || '')))
        .map((p) => {
          const d = { name: p.name, state: p.state, read: p.read_errors || 0, write: p.write_errors || 0,
            cksum: p.checksum_errors || 0 };
          return { ...d, bad: bad(d) };
        });
    },
    poolFoot(it) {
      const p = it.pool || {};
      if (!p.last_scrub_end) return '';
      let s = `Last scrub ${this.ago(p.last_scrub_end)}`;
      if (p.last_scrub_repaired) s += `, ${this.num(p.last_scrub_repaired)} repaired`;
      else if (p.last_scrub_repaired === 0) s += ', nothing repaired';
      return `${s}.`;
    },

    // ── Disk space ──────────────────────────────────────────────────
    spaces(c) {
      if (c.kind === 'filesystem') return [c.fs];
      return (c.it && c.it.filesystems) || [];
    },
    spaceShown(c) {
      return this.spaces(c).slice(0, 3);
    },
    spaceMore(c) {
      return Math.max(0, this.spaces(c).length - 3);
    },
    pct(f) {
      return Math.round(Number(f.use_percent) || 0);
    },
    // The bar's step: lo below 80%; s80, s90, s95 as the fill brightens and
    // the number weighs more; na for a filesystem that is not mounted.
    spaceStep(f) {
      if (f.status === 'unavailable') return 'na';
      const p = this.pct(f);
      if (p >= 95) return 's95';
      if (p >= 90) return 's90';
      if (p >= 80) return 's80';
      return 'lo';
    },
    spaceInside(f) {
      const step = this.spaceStep(f);
      return step === 'lo' || step === 'na' ? '' : `${this.pct(f)}%`;
    },
    spaceText(f) {
      const step = this.spaceStep(f);
      if (step === 'na') return 'not mounted';
      if (step === 'lo') return `${this.pct(f)}% of ${this.bytes(f.total_bytes)}`;
      return `${this.bytes(f.available_bytes)} free`;
    },
    spaceUsed(f) {
      if (f.status === 'unavailable') return 'not mounted';
      return `${this.bytes(f.used_bytes)} used of ${this.bytes(f.total_bytes)} total`;
    },
    spaceFill(f) {
      return f.status === 'unavailable' ? 0 : Math.min(100, Math.max(0, Number(f.use_percent) || 0));
    },

    // ── Footer and corner icons ─────────────────────────────────────
    unanswered(c) {
      const d = c.h && this.hostData[c.h.id];
      return !!(d && ['unreachable', 'disabled'].includes(d.status));
    },
    lastSeen(c) {
      let at = null;
      {
        const d = this.hostData[c.h.id];
        if (!d) {
          const added = Date.parse(c.h.added_at);
          return !added || this.now - added < 60000 ? 'Added just now' : `Added ${this.ago(added)}`;
        }
        at = d.status === 'ok' ? d.read_at : d.last_answer;
      }
      const when = at ? this.ago(at) : 'not yet';
      return `Last seen: ${when}${this.unanswered(c) ? ', not answering' : ''}`;
    },
    canDownload(c) {
      return (c.kind === 'drive' || c.kind === 'pool') && !this.replicaMissing(c);
    },
    // Save as replica: every real drive and pool card (not a Replica's,
    // whose file is in the library already).
    canSave(c) {
      if (!(this.info && this.info.replica) || !this.canDownload(c) || this.isReplica(c)) return false;
      if (c.kind === 'pool') return true;
      const v = c.it.verdict;
      return !!(c.it.drive && c.it.drive.smart_data && v && v.state !== 'UNSUPPORTED');
    },
    canHide(c) {
      return c.kind !== 'down' && c.kind !== 'loading';
    },
    // The App's agent (HAOS) cards are hidden, never removed, as before.
    canRemove(c) {
      return !(c.h && c.h.removable === false);
    },
    removeText(c) {
      if (this.isReplica(c) && c.kind === 'pool') return 'Remove this replica pool? Its file stays in your replicas.';
      if (this.isReplica(c) && c.kind === 'drive') return 'Remove this replica drive? Its file stays in your replicas.';
      if (c.kind === 'pool') return 'Remove this pool?';
      if (c.kind === 'filesystem') return 'Remove this filesystem?';
      if (c.kind === 'machine') return `Remove ${this.hostName(c.h)}?`;
      return 'Remove this drive?';
    },

    async setHidden(c, hidden) {
      const was = this.hidden.slice();
      this.hidden = hidden ? was.concat([c.id]) : was.filter((id) => id !== c.id);
      if (!this.hiddenCount) this.showHidden = false;
      this.pageErr = '';
      const r = await this.request(`cards/${enc(c.id)}`, { method: 'PATCH', body: { hidden } });
      if (!r.ok) {
        this.hidden = was;
        this.pageErr = 'That change was not saved. Try again.';
      }
    },

    // The trash: the first click turns it into Remove? in the same place,
    // the second removes the card. A card in Home Assistant (blue exit
    // icon) arms two buttons instead, growing left: Remove here, and the
    // same with what Home Assistant can do (ha_removal from /ha/devices:
    // delete its device or the machine's entry, or disable its device).
    // A replica in Home Assistant keeps the single Remove?, and its second
    // click takes the replica's device and entities out of Home Assistant
    // too (ha_removal "replica").
    // Leaving them, Esc, focus going elsewhere or ARM_MS put the trash back.
    armed(c) {
      return !!(this.ui.arm && this.ui.arm.id === c.id);
    },
    haRemoval(c) {
      // A Replica card's trash removes its pick, and its device with it.
      if (this.isReplica(c)) return null;
      if (this.glyph(c).kind !== 'blue') return null;
      const mode = this.ha && this.ha.ha_removal && this.ha.ha_removal[c.id];
      return ['delete', 'disable', 'entry', 'replica'].includes(mode) ? mode : null;
    },
    armedHa(c) {
      return this.armed(c) && !!this.ui.arm.ha && this.ui.arm.ha !== 'replica';
    },
    armedLabel(c) {
      const also = this.ui.arm && this.ui.arm.ha === 'replica' ? ' It leaves Home Assistant too.' : '';
      return `${this.removeText(c)} Press again to remove.${also}`;
    },
    haRemoveText() {
      const a = this.ui.arm;
      return a && a.ha === 'disable' ? 'Remove here + disable entities' : 'Remove here + delete entities';
    },
    trashClick(c) {
      if (this.armed(c)) { this.confirmRemove(c, this.ui.arm.ha === 'replica' ? 'replica' : null); return; }
      this.disarm();
      this.ui.confirm = null;
      const arm = { id: c.id, busy: false, timer: null, ha: this.haRemoval(c) };
      arm.timer = setTimeout(() => { if (this.ui.arm && this.ui.arm.id === c.id && !this.ui.arm.busy) this.disarm(); }, ARM_MS);
      this.ui.arm = arm;
      this.ui.note[`rm:${c.id}`] = '';
    },
    disarm(c) {
      const a = this.ui.arm;
      if (!a || a.busy || (c && a.id !== c.id)) return;
      clearTimeout(a.timer);
      this.ui.arm = null;
    },
    // Focus moving between the two armed buttons keeps them armed.
    rmFocusOut(c, e) {
      if (e.currentTarget.contains(e.relatedTarget)) return;
      this.disarm(c);
    },
    showToast(text, sub, act) {
      clearTimeout(this.toastTimer);
      this.ui.toast = text;
      this.ui.toastSub = sub || '';
      this.ui.toastAct = act || null;
      this.toastTimer = setTimeout(() => { this.ui.toast = ''; this.ui.toastSub = ''; this.ui.toastAct = null; },
        act ? 3 * TOAST_MS : TOAST_MS);
    },
    newMachine(h) {
      this.showToast(`Added ${this.hostName(h)}.`, 'Its cards are at the end of the page.', { label: 'Show', host: h.id });
    },
    toastGo() {
      const act = this.ui.toastAct;
      this.ui.toast = '';
      this.ui.toastAct = null;
      if (!act || !act.host) return;
      const card = this.shownCards.find((c) => c.h && c.h.id === act.host);
      if (!card) return;
      this.$nextTick(() => {
        const el = document.querySelector(`.grid [data-card="${CSS.escape(card.id)}"]`);
        if (!el) return;
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.remove('flash');
        void el.offsetWidth;
        el.classList.add('flash');
        el.addEventListener('animationend', () => el.classList.remove('flash'), { once: true });
      });
    },
    haRemovedText(c, data) {
      if (data && data.home_assistant === 'disabled') {
        return `Disabled in Home Assistant. It cannot be deleted while the agent still sees the ${c.kind === 'pool' ? 'pool' : 'drive'}.`;
      }
      if (data && data.home_assistant === 'deleted') return 'Deleted from Home Assistant.';
      const reason = String((data && data.ha_error) || 'no answer').replace(/[.\s]+$/, '');
      return `Removed here. Home Assistant did not take the change: ${reason}.`;
    },
    async confirmRemove(c, ha) {
      const a = this.ui.arm;
      if (!a || a.busy) return;
      clearTimeout(a.timer);
      a.busy = true;
      const path = `cards/${enc(c.id)}` + (ha ? `?ha=${enc(ha)}` : '');
      const r = await this.request(path, { method: 'DELETE' });
      a.busy = false;
      this.ui.arm = null;
      if (!r.ok) {
        const key = `rm:${c.id}`;
        this.ui.note[key] = 'It was not removed. Try again.';
        setTimeout(() => { this.ui.note[key] = ''; }, 5000);
        return;
      }
      this.gone[c.id] = true;
      this.order = this.order.filter((id) => id !== c.id);
      this.hidden = this.hidden.filter((id) => id !== c.id);
      if (ha) this.showToast(this.haRemovedText(c, r.data));
      if (r.data && r.data.host_removed) this.dropMachine(c.h.id);
      else await this.loadHost(c.h.id);
      delete this.gone[c.id];
    },

    // Save as replica: the card into the user's replica library, at once.
    async saveReplica(c) {
      const key = `sv:${c.id}`;
      if (this.ui.busy[key]) return;
      this.ui.busy[key] = true;
      const r = await this.ws({ type: 'replica/save', card_id: c.id });
      this.ui.busy[key] = false;
      if (r.ok && r.data && r.data.saved) {
        this.showToast('Saved to your replicas.', r.data.saved.name || '');
        this.replicaFiles = null;
        return;
      }
      const code = r.data && r.data.error;
      const note = `dl:${c.id}`;
      this.ui.note[note] = REPLICA_MSG[code] || 'It was not saved. Try again.';
      setTimeout(() => { this.ui.note[note] = ''; }, 5000);
    },

    async download(c) {
      const key = `dl:${c.id}`;
      this.ui.note[key] = '';
      try {
        const r = await this.ws({ type: 'card/file', card_id: c.id });
        if (!r.ok || !r.data) {
          const code = r.data && r.data.error;
          this.ui.note[key] = {
            replica_folder_unavailable: 'Your replica folder is not available.',
            unsupported_drive: 'This drive has no SMART data to copy.',
            not_answering: 'The agent did not answer.',
          }[code] || 'It was not saved. Try again.';
          setTimeout(() => { this.ui.note[key] = ''; }, 5000);
          return;
        }
        const blob = new Blob([r.data.text], { type: 'application/json' });
        const name = r.data.name || `${String(c.id).replace(/[^a-z0-9]+/gi, '-')}.json`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 2000);
      } catch (e) { /* the next read shows the state */ }
    },

    // ── Exit icon and its confirm ───────────────────────────────────
    target(c) {
      return c.h ? c.h.id : '';
    },
    glyph(c) {
      if (!['drive', 'pool'].includes(c.kind)) return NONE;
      const ha = this.ha;
      if (!ha || ha.reachable === false) return NONE;
      // Not installed: the grey icon still shows, and opens the install guide.
      if (ha.installed === false) return { kind: 'grey', href: null };
      if (ha.installed !== true) return NONE;
      const t = ha.targets && ha.targets[this.target(c)];
      if (!t) return { kind: 'grey', href: null };
      const key = this.haKey(c);
      const dev = key && t.devices && t.devices[key];
      if (dev) return { kind: 'blue', href: `/config/devices/device/${enc(dev)}` };
      // Every machine here is in Home Assistant; a card whose device is not
      // made yet (its first poll is still on the way) has no icon.
      return NONE;
    },
    // The confirm under a card's title: what Create makes, in the user's
    // words. The page lives inside Home Assistant, so it never says "add to
    // Home Assistant".
    askText(c) {
      if (c.src === 'replicas') {
        const others = ((this.replicas && this.replicas.items) || []).length - 1;
        const entry = this.ha && this.ha.targets && this.ha.targets.replicas;
        const more = !entry && others > 0 ? `The other ${this.plural(others, 'replica comes', 'replicas come')} too. ` : '';
        return { title: 'Create test sensors for the replicas?',
                 line: `${more}Delete them from the integration when you're done testing.` };
      }
      const data = this.data(c.h);
      const n = ((data && data.items) || []).filter((i) => i.type === 'drive').length;
      const what = 'health sensors for dashboards and automations, and an alert when it needs attention.';
      let line = `Each of its ${n} drives gets ${what}`;
      if (n === 1) line = `Its drive gets ${what}`;
      if (n === 0) line = `It gets ${what}`;
      return { title: `Create sensors and alerts for ${this.machine(c)}?`, line };
    },
    // Without the integration the same icon opens a short install guide.
    // Home Assistant cannot tell an integration downloaded but not yet
    // restarted from one never installed, so the guide always starts at
    // installing it (or HACS first, when HACS is not set up either).
    guideSteps() {
      const noHacs = !!(this.ha && this.ha.hacs === false);
      return {
        first: noHacs ? 'Install HACS, then the SMART Sniffer integration.' : 'Install it from HACS.',
        href: noHacs ? HACS_DOCS_URL : HACS_REPO_URL,
        link: noHacs ? 'How to install HACS' : 'Open in HACS',
      };
    },
    get noticeShown() {
      return false;
    },
    askAdd(c) {
      this.disarm();
      if (this.ui.confirm && this.ui.confirm.id === c.id) { this.closeConfirm(); return; }
      if (this.ha && this.ha.installed === false) {
        this.ui.confirm = { id: c.id, guide: true, busy: false, err: '' };
        return;
      }
      const ask = this.askText(c);
      this.ui.confirm = { id: c.id, target: this.target(c), text: ask.title, line: ask.line, busy: false, err: '' };
    },
    asking(c) {
      return !!(this.ui.confirm && this.ui.confirm.id === c.id);
    },
    closeConfirm() {
      this.ui.confirm = null;
    },
    async confirmAdd() {
      const c = this.ui.confirm;
      if (!c || c.busy) return;
      c.busy = true;
      c.err = '';
      const r = await this.request('ha/add', { method: 'POST', body: { target: c.target } });
      c.busy = false;
      const result = r.data && r.data.result;
      if (r.ok && ['created', 'already_added', 'reloaded'].includes(result)) {
        this.ui.confirm = null;
        this.loadHa();
        this.haSoon();
        return;
      }
      const why = r.data && (r.data.message || r.data.error);
      c.err = {
        cannot_connect: 'Home Assistant could not reach this agent.',
        integration_not_loaded: 'The integration is not installed. Install it from HACS and restart Home Assistant.',
        invalid_auth: 'Home Assistant refused the password.',
        unreachable: 'Home Assistant is not answering.',
      }[why] || 'Home Assistant did not create them. The App log may say why.';
    },

    // ── Field agents ────────────────────────────────────────────────
    data(h) {
      return this.hostData[h.id] || null;
    },
    status(h) {
      const d = this.data(h);
      return d ? d.status : 'reading';
    },
    pending(h) {
      const s = this.status(h);
      return s === 'password_needed' || s === 'password_refused';
    },
    // A machine's label: the one given here, else the integration's (the
    // host name the agent reports, the name discovery found, or the
    // address). HAOS and REPLICA keep theirs.
    hostName(h) {
      return (!h.fixed && this.hostNames[h.id]) || h.label || h.address;
    },
    machineText(c) {
      const h = c.h;
      const s = this.status(h);
      if (this.pending(h)) return '';
      if (s === 'disabled') return 'Disabled in Home Assistant.';
      if (s === 'unreachable') return 'Not answering, and not seen yet.';
      if (s === 'reading') return 'Reading its drives.';
      return 'It reports no drives.';
    },
    pwRefused(h) {
      return this.ui.pwState[h.id] === 'refused' || (this.status(h) === 'password_refused' && !this.ui.pwState[h.id]);
    },
    pwMessage(h) {
      const s = this.ui.pwState[h.id];
      if (s === 'noanswer') return 'That agent is not answering. Try again in a moment.';
      if (s === 'error') return 'The password was not saved. Try again.';
      return '';
    },
    async authenticate(h) {
      const pw = this.ui.pw[h.id] || '';
      if (!pw) return;
      this.ui.busy[`pw:${h.id}`] = true;
      const r = await this.request(`fleet/hosts/${enc(h.id)}`, { method: 'PATCH', body: { password: pw } });
      this.ui.busy[`pw:${h.id}`] = false;
      if (r.ok) {
        this.ui.pw[h.id] = '';
        this.ui.pwState[h.id] = null;
        if (r.data && r.data.host) Object.assign(h, r.data.host);
        await this.loadHost(h.id);
        return;
      }
      const code = r.data && r.data.error;
      this.ui.pwState[h.id] = code === 'password_refused' ? 'refused' : code === 'not_answering' ? 'noanswer' : 'error';
    },

    // ── Drag to reorder ─────────────────────────────────────────────
    // Pointer events, not HTML5 drag and drop, so it works on touch in the
    // Home Assistant app. A mouse drags from the grip at once; a finger
    // must hold the grip first, so a scroll never moves a card.
    gripDown(e, c) {
      if (e.button !== undefined && e.button !== 0) return;
      const el = e.currentTarget.closest('article');
      if (e.pointerType !== 'touch') {
        e.preventDefault();
        this.startDrag(c, el, e.clientX, e.clientY);
        return;
      }
      const x0 = e.clientX;
      const y0 = e.clientY;
      let timer = null;
      const stop = () => {
        clearTimeout(timer);
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', stop);
        window.removeEventListener('pointercancel', stop);
      };
      const move = (ev) => { if (Math.hypot(ev.clientX - x0, ev.clientY - y0) > 8) stop(); };
      timer = setTimeout(() => { stop(); this.startDrag(c, el, x0, y0); }, LONG_PRESS_MS);
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', stop);
      window.addEventListener('pointercancel', stop);
    },
    startDrag(c, el, x, y) {
      if (this.drag || !el) return;
      const r = el.getBoundingClientRect();
      const lifted = el.cloneNode(true);
      lifted.classList.add('lifted');
      lifted.removeAttribute('data-card');
      Object.assign(lifted.style, { width: `${r.width}px`, height: `${r.height}px`, left: `${r.left}px`, top: `${r.top}px` });
      document.getElementById('drag-layer').appendChild(lifted);
      const start = this.orderedCards.map((x2) => x2.id);
      this.dragOrder = start.slice();
      const onMove = (ev) => this.dragMove(ev);
      const onUp = () => this.dragEnd();
      this.drag = { id: c.id, offX: x - r.left, offY: y - r.top, lifted, start, onMove, onUp };
      window.addEventListener('pointermove', onMove);
      window.addEventListener('pointerup', onUp);
      window.addEventListener('pointercancel', onUp);
    },
    dragMove(ev) {
      const d = this.drag;
      if (!d) return;
      ev.preventDefault();
      d.lifted.style.left = `${ev.clientX - d.offX}px`;
      d.lifted.style.top = `${ev.clientY - d.offY}px`;
      const edge = 60;
      if (ev.clientY < edge) window.scrollBy(0, -12);
      else if (ev.clientY > window.innerHeight - edge) window.scrollBy(0, 12);
      for (const el of document.querySelectorAll('.grid > article[data-card]')) {
        const id = el.dataset.card;
        if (id === d.id) continue;
        const r = el.getBoundingClientRect();
        if (ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom) continue;
        const before = ev.clientX < r.left + r.width / 2;
        const order = this.dragOrder.filter((x) => x !== d.id);
        const at = order.indexOf(id) + (before ? 0 : 1);
        order.splice(at, 0, d.id);
        if (order.join('\n') !== this.dragOrder.join('\n')) this.dragOrder = order;
        break;
      }
    },
    dragEnd() {
      const d = this.drag;
      if (!d) return;
      window.removeEventListener('pointermove', d.onMove);
      window.removeEventListener('pointerup', d.onUp);
      window.removeEventListener('pointercancel', d.onUp);
      d.lifted.remove();
      const order = this.dragOrder;
      this.drag = null;
      this.dragOrder = null;
      if (order.join('\n') !== d.start.join('\n')) this.saveOrder(order);
    },
    // The grip by keyboard: arrows move the card one place.
    gripKey(e, c) {
      const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const shown = this.shownCards.map((x) => x.id);
      const i = shown.indexOf(c.id);
      const j = i + step;
      if (i < 0 || j < 0 || j >= shown.length) return;
      const order = this.orderedCards.map((x) => x.id).filter((x) => x !== c.id);
      const at = order.indexOf(shown[j]) + (step > 0 ? 1 : 0);
      order.splice(at, 0, c.id);
      this.saveOrder(order);
      this.$nextTick(() => {
        const el = document.querySelector(`.grid > article[data-card="${CSS.escape(c.id)}"] .grip`);
        if (el) el.focus();
      });
    },
    async saveOrder(order) {
      const was = this.order;
      this.order = order;
      this.pageErr = '';
      const r = await this.request('cards/order', { method: 'PUT', body: { order } });
      if (!r.ok) {
        this.order = was;
        this.pageErr = 'The new order was not saved. Try again.';
      }
    },

    // ── The + card ──────────────────────────────────────────────────
    defaultBlock() {
      const b = this.add.network && this.add.network.block;
      if (b) return b;
      const p = parseBlock(window.location.hostname);
      return p ? block24(p.octets) : '';
    },
    // The + card opens on every agent Home Assistant's zeroconf sees (the
    // ones already added shown as added) and the discoveries waiting, the
    // new ones ticked; then the block around Home Assistant is scanned, as
    // the HA App 0.3.1 did. The Add area is scrolled into view and the
    // keyboard is in its first field.
    async openAdd() {
      this.add.open = true;
      this.add.listed = false;
      this.showAdd();
      this.loadReplicas();
      const r = await this.ws({ type: 'net/list' });
      if (!this.add.open) return;
      this.add.found = (r.ok && r.data && r.data.found) || [];
      this.add.network = (r.ok && r.data && r.data.network) || null;
      this.add.found.forEach((row) => {
        if (!row.added && !(row.id in this.add.ticks)) this.add.ticks[row.id] = true;
      });
      this.add.listed = true;
      if (!this.add.block) this.add.block = this.defaultBlock();
      if (this.add.block && !this.add.scan) this.scan();
    },
    showAdd() {
      this.$nextTick(() => {
        const area = document.querySelector('.ghost-open');
        if (!area) return;
        const still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        area.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'start' });
        const first = area.querySelector('.add-body input, .add-body button, .add-body select');
        if (first) first.focus({ preventScroll: true });
      });
    },
    closeAdd() {
      this.stopScan();
      Object.assign(this.add, {
        open: false, scan: null, last: null, wide: null, error: '', ticks: {}, manualRows: [], checkMsg: null,
        manualHost: '', repTicks: {}, upMsg: '', err: '', found: [], listed: false, repDel: null,
      });
    },
    get scanLabel() {
      const l = this.add.last;
      return l && l.block === this.add.block.trim() && l.port === String(this.add.port).trim() ? 'Rescan' : 'Scan';
    },
    fieldsChanged() {
      if (this.scanLabel === 'Rescan') return;
      this.stopScan();
      this.add.scan = null;
      this.add.wide = null;
      this.add.error = '';
    },
    suggestBlock(parsed) {
      const own = this.add.network && this.add.network.address;
      const p = own ? parseBlock(own) : null;
      return block24(p ? p.octets : parsed.octets);
    },
    stopScan() {
      const id = this.add.scanId;
      if (id && this.add.scan && this.add.scan.state === 'running') {
        this.request(`fleet/scan/${enc(id)}`, { method: 'DELETE' });
      }
      this.add.scanId = null;
    },
    async scan() {
      const block = this.add.block.trim();
      const portText = String(this.add.port).trim();
      const port = Number(portText);
      this.stopScan();
      this.add.error = '';
      this.add.wide = null;
      this.add.scan = null;
      const parsed = parseBlock(block);
      if (!parsed) { this.add.error = 'That is not a network block. Write it as 10.0.10.0/24.'; return; }
      if (!Number.isInteger(port) || port < 1 || port > 65535) { this.add.error = 'The port is a number from 1 to 65535.'; return; }
      if (parsed.prefix < 22) {
        this.add.wide = { addresses: 2 ** (32 - parsed.prefix) - 2, suggest: this.suggestBlock(parsed) };
        return;
      }
      this.add.last = { block, port: portText };
      this.add.scan = { state: 'running', probed: 0, total: 0, found: [], block, port };
      const r = await this.request('fleet/scan', { method: 'POST', body: { block, port } });
      if (r.data && r.data.error === 'too_wide') {
        this.add.scan = null;
        this.add.last = null;
        this.add.wide = { addresses: r.data.addresses, suggest: r.data.suggest };
        return;
      }
      if (!r.ok || !r.data) {
        this.add.scan = null;
        this.add.error = r.data && r.data.error === 'bad_block' ? 'That is not a network block. Write it as 10.0.10.0/24.'
          : 'The scan did not start. Try again.';
        return;
      }
      this.add.scanId = r.data.scan_id;
      this.add.scan.total = r.data.total;
      this.pollScan(r.data.scan_id);
    },
    // One poll every 400 ms while the page is visible. A hidden page waits;
    // when it shows again resumeScan() polls at once so the bar catches up.
    pollScan(id, delay = 400) {
      clearTimeout(this.scanTimer);
      this.scanTimer = setTimeout(async () => {
        this.scanTimer = null;
        if (this.add.scanId !== id) return;
        if (document.hidden) { this.pollScan(id); return; }
        this.scanPolling = true;
        const r = await this.request(`fleet/scan/${enc(id)}`);
        this.scanPolling = false;
        if (this.add.scanId !== id) return;
        if (!r.ok || !r.data) {
          if (this.add.scan) this.add.scan.state = 'done';
          this.add.scanId = null;
          return;
        }
        (r.data.found || []).forEach((row) => {
          if (!row.added && !(row.id in this.add.ticks)) this.add.ticks[row.id] = true;
        });
        this.add.scan = r.data;
        if (r.data.state === 'running') this.pollScan(id);
        else this.add.scanId = null;
      }, delay);
    },
    resumeScan() {
      // A request already on its way schedules the next poll itself.
      if (this.add.scanId && !this.scanPolling) this.pollScan(this.add.scanId, 0);
    },
    useSuggest() {
      this.add.block = this.add.wide.suggest;
      this.scan();
    },
    get scanPct() {
      const s = this.add.scan;
      if (!s) return 0;
      if (s.state !== 'running') return 100;
      return s.total ? Math.min(100, Math.round((100 * s.probed) / s.total)) : 0;
    },
    get foundCount() {
      return ((this.add.scan && this.add.scan.found) || []).filter((r) => !r.added).length;
    },
    // Rows the scan listed that are on the board already, named after the
    // new count so a scan of known machines does not read as nothing found.
    get addedCount() {
      return ((this.add.scan && this.add.scan.found) || []).filter((r) => r.added).length;
    },
    get scanNone() {
      const s = this.add.scan;
      return !!s && s.state !== 'running' && !(s.found || []).length;
    },
    // Nothing at all: zeroconf sees no agent, no discovery waits, the scan
    // (if any) is over and found none, and nothing was checked by hand.
    get netNone() {
      const s = this.add.scan;
      return this.add.listed && !this.pickRows.length && !(s && s.state === 'running');
    },
    get pickRows() {
      const seen = new Set();
      const out = [];
      const at = new Map();
      [...(this.add.found || []), ...((this.add.scan && this.add.scan.found) || []), ...this.add.manualRows].forEach((r) => {
        if (seen.has(r.id)) {
          // Added, as any source says: never offered twice.
          if (r.added && !out[at.get(r.id)].added) out[at.get(r.id)] = { ...out[at.get(r.id)], added: true };
          return;
        }
        seen.add(r.id);
        at.set(r.id, out.length);
        out.push(r);
      });
      return out;
    },
    driveCount(n) {
      return typeof n === 'number' ? this.plural(n, 'drive', 'drives') : '';
    },
    // Check one machine by hand. It always answers in a line under the
    // fields: a machine already in the list is ticked, scrolled to and
    // flashed there; a new one joins the list, ticked; a stored machine with
    // cards is already on the page; else nothing answered, or what answered
    // is not an agent. The list drops a second row for a machine it has, so
    // without this line a Check of a listed machine showed nothing at all.
    async check() {
      const host = this.add.manualHost.trim();
      const port = Number(String(this.add.manualPort).trim()) || 9099;
      if (!host || this.add.checking) return;
      this.add.checkMsg = null;
      this.add.checking = true;
      const r = await this.request('fleet/probe', { method: 'POST', body: { host, port } });
      this.add.checking = false;
      if (!(r.ok && r.data && r.data.id)) {
        const notAgent = r.status === 404 && r.data && r.data.error === 'not_an_agent';
        this.add.checkMsg = notAgent
          ? { text: `${host}:${port} answered, but it is not a SMART Sniffer agent.`, muted: false }
          : { text: `Nothing answered at ${host}:${port}.`, muted: true };
        return;
      }
      const row = r.data;
      this.add.manualHost = '';
      if (row.added) {
        this.add.checkMsg = { text: `${host} is already on the page.`, muted: false };
        return;
      }
      const same = (x) => x.id === row.id || (Number(x.port) === Number(row.port)
        && [row.address, row.resolved].filter(Boolean).map((a) => String(a).toLowerCase())
          .includes(String(x.address).toLowerCase()));
      const listed = this.pickRows.find((x) => !x.added && same(x));
      if (listed) {
        this.add.ticks[listed.id] = true;
        this.add.checkMsg = { text: `${host} is in the list above, ticked.`, muted: false };
        this.flashRow(listed.id);
        return;
      }
      this.add.manualRows = this.add.manualRows.concat([row]);
      this.add.ticks[row.id] = true;
      this.add.checkMsg = { text: `Added ${host} to the list, ticked.`, muted: false };
    },
    // Scroll a Network row into view and pulse its outline once.
    flashRow(id) {
      this.$nextTick(() => {
        const el = [...document.querySelectorAll('.pick-row')].find((e) => e.dataset.id === id);
        if (!el) return;
        el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        el.classList.remove('flash');
        void el.offsetWidth;  // restart the animation on a second Check
        el.classList.add('flash');
        el.addEventListener('animationend', () => el.classList.remove('flash'), { once: true });
      });
    },
    // ── + > Replica ─────────────────────────────────────────────────
    get folderState() {
      return (this.replicaFiles && this.replicaFiles.folders && this.replicaFiles.folders.user) || 'missing';
    },
    get folderOk() {
      return this.folderState === 'ok';
    },
    get folderText() {
      if (this.folderOk) return 'Upload a replica';
      return this.folderState === 'full' ? 'Your replica folder is full.' : 'Your replica folder is not available.';
    },
    get pickFiles() {
      return ((this.replicaFiles && this.replicaFiles.files) || []).filter((f) => f.valid !== false && f.name);
    },
    // The library in its groups, each only when it has files.
    get repGroups() {
      const files = (this.replicaFiles && this.replicaFiles.files) || [];
      return REPLICA_GROUPS.map(([key, title]) => ({ key, title, files: files.filter((f) => f.source === key) }))
        .filter((g) => g.files.length);
    },
    repKind(f) {
      if (!f.valid) return f.reason === 'too_large' ? 'Too large (over 1 MB)' : 'Not a replica file';
      if (f.kind === 'zfs_pool') return 'ZFS pool';
      if (f.kind === 'nvme') return 'NVMe drive';
      if (f.kind === 'scsi') return 'SAS drive';
      if (f.kind === 'unsupported') return 'Drive, no SMART';
      return 'SATA drive';
    },
    // How many drives and pools the Replica would hold with the ticks.
    repLoad(extra) {
      const used = (this.replicaFiles && this.replicaFiles.used) || { drives: 0, pools: 0 };
      const ticked = this.pickFiles.filter((f) => this.add.repTicks[f.id] || (extra && extra.id === f.id));
      return { drives: used.drives + ticked.filter((f) => !f.pool).length, pools: used.pools + ticked.filter((f) => f.pool).length };
    },
    get repCaps() {
      return (this.info && this.info.replica_caps) || { drives: 12, pools: 4 };
    },
    repTickable(f) {
      if (!f.valid) return false;
      if (this.add.repTicks[f.id]) return true;
      const load = this.repLoad(f);
      return f.pool ? load.pools <= this.repCaps.pools : load.drives <= this.repCaps.drives;
    },
    get repAtCap() {
      const load = this.repLoad();
      return load.drives >= this.repCaps.drives || load.pools >= this.repCaps.pools;
    },
    get tickedHosts() {
      return this.pickRows.filter((r) => !r.added && this.add.ticks[r.id]);
    },
    get tickedFiles() {
      return this.pickFiles.filter((f) => this.add.repTicks[f.id]).map((f) => f.id);
    },
    get tickCount() {
      return this.tickedHosts.length + this.tickedFiles.length;
    },
    get tickedLock() {
      return this.tickedHosts.some((r) => r.auth === 'password');
    },
    // Your own files (saved or uploaded) can be deleted from the library,
    // after a second click; a file a Replica card shows cannot.
    canDeleteFile(f) {
      return f.source === 'user';
    },
    // The card trash's way: a first click arms it (Delete?), a second one
    // deletes; it disarms by itself, when the pointer leaves or on Escape.
    repArmed(f) {
      return !!(this.add.repDel && this.add.repDel.id === f.id && !this.add.repDel.err);
    },
    repTrash(f) {
      if (this.repArmed(f)) { this.deleteFile(); return; }
      if (this.add.repDel) clearTimeout(this.add.repDel.timer);
      const d = { id: f.id, name: f.name, busy: false, err: '', timer: null };
      d.timer = setTimeout(() => {
        const now = this.add.repDel;
        if (now && now.id === f.id && !now.busy && !now.err) this.add.repDel = null;
      }, ARM_MS);
      this.add.repDel = d;
    },
    repDisarm(f) {
      const d = this.add.repDel;
      if (!d || d.busy || d.id !== f.id || d.err) return;
      clearTimeout(d.timer);
      this.add.repDel = null;
    },
    async deleteFile() {
      const d = this.add.repDel;
      if (!d || d.busy) return;
      clearTimeout(d.timer);
      d.busy = true;
      d.err = '';
      const r = await this.ws({ type: 'replica/delete', file_id: d.id });
      d.busy = false;
      if (!r.ok) {
        d.err = REPLICA_MSG[r.data && r.data.error] || 'That file was not deleted. Try again.';
        return;
      }
      delete this.add.repTicks[d.id];
      this.add.repDel = null;
      await this.loadReplicas();
    },
    upload(ev) {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      this.add.upMsg = '';
      if (file.size > 1024 * 1024) { this.add.upMsg = 'That file is over 1 MB.'; return; }
      if (!BRIDGE.uploadReplica) { this.add.upMsg = 'Your replica folder is not available.'; return; }
      // The file goes as it is, to the integration's upload (an HTTP call,
      // so a large file never holds the websocket); it is checked there.
      const reader = new FileReader();
      reader.onload = async () => {
        this.add.uploading = true;
        let r;
        try { r = await BRIDGE.uploadReplica(reader.result); } catch (e) { r = { ok: false, data: null }; }
        this.add.uploading = false;
        const saved = r && r.ok && r.data && r.data.saved;
        if (saved) {
          await this.loadReplicas();
          if (this.repTickable(saved)) this.add.repTicks[saved.id] = true;
          return;
        }
        const code = r && r.data && r.data.error;
        this.add.upMsg = REPLICA_MSG[code] || 'That file is not a replica file.';
      };
      reader.readAsArrayBuffer(file);
    },
    // Add: each ticked machine becomes an entry of the integration, through
    // its own setup steps. A machine that wants a password asks for it on
    // its card after it is added.
    async addTicked() {
      const rows = this.tickedHosts;
      const files = this.tickedFiles;
      if (!rows.length && !files.length) return;
      this.add.busy = true;
      this.add.err = '';
      if (files.length) {
        const rr = await this.ws({ type: 'replica/add', file_ids: files });
        if (!rr.ok) {
          this.add.busy = false;
          this.add.err = REPLICA_MSG[rr.data && rr.data.error] || 'The replicas were not added. Try again.';
          return;
        }
        this.add.repTicks = {};
        this.replicaFiles = null;
        if (!rows.length) {
          this.add.busy = false;
          this.closeAdd();
          this.loadAll();
          return;
        }
      }
      const r = await this.request('fleet/hosts', { method: 'POST',
        body: { hosts: rows.map((x) => ({ address: x.address, port: x.port, flow_id: x.flow_id || null,
          drives: Number.isInteger(x.drives) ? x.drives : null })) } });
      this.add.busy = false;
      if (!r.ok || !r.data) {
        this.add.err = 'The machines were not added. Try again.';
        return;
      }
      const failed = (r.data.results || []).filter((x) => !['created', 'restored', 'already_added'].includes(x.result));
      if (failed.length) {
        const why = {
          cannot_connect: 'Home Assistant could not reach this agent.',
          password_needed: 'Home Assistant refused the password.',
        };
        failed.forEach((x) => { this.add.ticks[x.id] = true; });
        (r.data.results || []).filter((x) => !failed.includes(x)).forEach((x) => { this.add.ticks[x.id] = false; });
        this.add.err = failed.map((x) => `${x.address}:${x.port}: ${why[x.result] || 'Home Assistant did not add it.'}`).join(' ');
        this.loadAll();
        return;
      }
      this.closeAdd();
      this.loadAll();
    },

    // ── Agentic access ──────────────────────────────────────────────
    // Everything the Control Center knows, as JSON, built by the
    // integration; Copy and Download use exactly the text shown.
    async openAgentic() {
      this.view = 'agentic';
      this.ax = { text: '', meta: '', file: 'smart-sniffer.json', status: 'Reading' };
      window.scrollTo(0, 0);
      const r = await this.ws({ type: 'agentic' });
      if (!r.ok || !r.data) {
        this.ax.status = 'Not read. Try again.';
        return;
      }
      const d = r.data;
      const size = new Blob([d.text]).size;
      const at = String(d.generated_at || '');
      // The same numbers as the document's counts: real agents, then the
      // Replica's simulated drives and pools apart.
      const c = d.counts || { agents: d.agents, drives: d.drives, pools: d.pools || 0,
        unreachable_agents: d.down || 0 };
      const n = (v, one, many) => `${v} ${v === 1 ? one : many}`;
      const meta = [`generated ${at.slice(0, 10)} ${at.slice(11, 16)} UTC`,
        n(c.agents, 'agent', 'agents'), n(c.drives, 'drive', 'drives')];
      if (c.pools) meta.push(n(c.pools, 'pool', 'pools'));
      if (c.attention) meta.push(`${c.attention} need${c.attention === 1 ? 's' : ''} attention`);
      if (c.unreachable_agents) meta.push(`${c.unreachable_agents} not answering`);
      if (c.replica_drives || c.replica_pools) {
        const parts = [];
        if (c.replica_drives) parts.push(n(c.replica_drives, 'drive', 'drives'));
        if (c.replica_pools) parts.push(n(c.replica_pools, 'pool', 'pools'));
        if (c.replica_attention) parts.push(`${c.replica_attention} attention`);
        meta.push(`Replica: ${parts.join(', ')}`);
      }
      meta.push(size >= 1024 ? `${Math.round(size / 1024)} KB` : `${size} B`);
      this.ax = { text: d.text, meta: meta.join('  \u00b7  '), file: d.file || 'smart-sniffer.json', status: '' };

      this.axSignRaw();
    },
    // The raw JSON as scripts read it, in a tab of its own: a link Home
    // Assistant signs for this user (it lasts five minutes; a click after
    // that signs a new one).
    async axSignRaw() {
      if (!BRIDGE || typeof BRIDGE.agenticUrl !== 'function') return;
      try {
        const url = await BRIDGE.agenticUrl();
        if (url) this.ax = { ...this.ax, raw: url, rawAt: Date.now() };
      } catch (e) { /* no link then */ }
    },
    async axRaw(e) {
      if (this.ax.raw && Date.now() - (this.ax.rawAt || 0) < 240000) return;
      e.preventDefault();
      await this.axSignRaw();
      if (this.ax.raw) window.open(this.ax.raw, '_blank', 'noopener');
    },
    closeAgentic() {
      this.view = 'cards';
    },
    axSay(text) {
      this.ax.status = text;
      clearTimeout(this.axTimer);
      this.axTimer = setTimeout(() => { this.ax.status = ''; }, 1600);
    },
    // The clipboard needs a secure context; over plain http the text is
    // selected instead, ready for Ctrl+C.
    async axCopy() {
      const text = this.ax.text;
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          ok = true;
        }
      } catch (e) { ok = false; }
      if (!ok) {
        const pre = this.$refs.axjson;
        const range = document.createRange();
        range.selectNodeContents(pre);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        if (!ok) { this.axSay('Selected, press Ctrl+C'); return; }
      }
      this.axSay('Copied');
    },
    axDownload() {
      const blob = new Blob([this.ax.text], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = this.ax.file;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    },
  };
}
