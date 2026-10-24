"""The SMART Sniffer panel's page in a browser (v0.9).

Ported from the HA App 0.3.1 page tests (smart_sniffer_agent/web/
test_page.py) where they still apply: the page is the same, its data now
comes from the integration's websocket commands. The page runs inside
panel.js, as Home Assistant runs it, against tests/panel_page/backend.py:
the integration's real command handlers over a real Home Assistant core,
with the network faked.

Needs Playwright with Chromium in this Python, and a Python with Home
Assistant for the backend:

    SS_HA_PYTHON=/path/to/ha-venv/bin/python python3 -m pytest tests/panel_page

Skipped otherwise. SS_PANEL_SHOTS=<dir> also saves the screenshots.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HA_PYTHON = os.environ.get("SS_HA_PYTHON")
SHOTS = os.environ.get("SS_PANEL_SHOTS")

pytestmark = pytest.mark.skipif(not HA_PYTHON, reason="SS_HA_PYTHON is not set")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def backend():
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen([HA_PYTHON, "-m", "tests.panel_page.backend", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/fake/state", timeout=1)
            break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("backend did not start: " + (proc.stdout.read() if proc.stdout else ""))
    yield base
    proc.kill()
    proc.wait()


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _state(base: str) -> dict:
    with urllib.request.urlopen(base + "/fake/state", timeout=10) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def browser_():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        kwargs = {}
        if os.environ.get("PW_CHROMIUM"):
            kwargs["executable_path"] = os.environ["PW_CHROMIUM"]
        b = p.chromium.launch(**kwargs)
        yield b
        b.close()


class Page:
    """The shell page and the panel's frame inside it."""

    def __init__(self, browser, base, scenario="board", width=1280, shell="", height=900, touch=False):
        _post(base, "/fake/reset", {"scenario": scenario})
        self.base = base
        self.errors: list[str] = []
        # touch: a phone (a touch screen that cannot hover).
        self.page = browser.new_page(viewport={"width": width, "height": height}, has_touch=touch,
                                     is_mobile=touch)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        # The test shell has no favicon; a full Chromium asks for one.
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error"
                     and not str((m.location or {}).get("url", "")).endswith("/favicon.ico") else None)
        self.page.goto(base + "/" + (f"?shell={shell}" if shell else ""))
        self.page.wait_for_selector("smart-sniffer-panel")
        self.frame = self.page.frame_locator("smart-sniffer-panel iframe")
        self.frame.locator("main.page").first.wait_for()

    @property
    def doc(self):
        return next(f for f in self.page.frames if "index.html" in f.url)

    def card(self, text):
        """A card by its text, then by its id, so it stays found while its
        title is a rename field or its Details are open."""
        found = self.frame.locator("article.card", has_text=text).first
        found.wait_for()
        cid = found.get_attribute("data-card")
        return self.frame.locator(f'article.card[data-card="{cid}"]')

    def wait(self, fn, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if fn():
                return True
            time.sleep(0.1)
        return False

    def shot(self, name):
        if not SHOTS:
            return
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        height = self.doc.evaluate("document.documentElement.scrollHeight")
        width = self.page.viewport_size["width"]
        bar = 56 if width <= 870 else 0
        self.page.set_viewport_size({"width": width, "height": height + bar})
        self.page.wait_for_timeout(400)
        file = name if name.startswith("replica-") else f"panel-{name}"
        self.page.screenshot(path=str(Path(SHOTS) / f"{file}.png"))
        self.page.set_viewport_size({"width": width, "height": 900})

    def close(self):
        if not self.page.is_closed():
            self.page.close()


@pytest.fixture
def page(browser_, backend):
    pages = []

    def make(scenario="board", width=1280, **kwargs):
        p = Page(browser_, backend, scenario, width, **kwargs)
        pages.append(p)
        return p

    yield make
    for p in pages:
        p.close()


def _titles(p):
    return p.frame.locator("article.card h3.c-title").all_inner_texts()


# ── The board ────────────────────────────────────────────────────────────


def test_every_machine_in_any_state_shows(page):
    p = page()
    p.frame.locator("article.card").first.wait_for()
    pills = p.frame.locator("article.card .pill").all_inner_texts()
    assert {"HAOS", "NAS-01", "STUDIO-MAC", "BACKUP-PI"} <= set(pills)
    assert "Seagate Exos 10E2400 ST1200MM0129" in " ".join(_titles(p))
    mac = p.card("STUDIO-MAC")
    assert "3 h ago, not answering" in mac.locator(".seen").inner_text()
    pi = p.card("BACKUP-PI")
    assert "This agent wants a password." in pi.inner_text()
    version = json.loads((ROOT / "custom_components/smart_sniffer/manifest.json").read_text())["version"]
    assert p.frame.locator("header .versions").inner_text().strip() == f"integration {version}"
    assert not p.errors, p.errors
    p.shot("board-1280")


def test_board_at_390_has_no_sideways_scroll_and_the_menu_bar(page):
    p = page(width=390)
    p.frame.locator("article.card").first.wait_for()
    scroll = p.doc.evaluate("document.documentElement.scrollWidth")
    assert scroll <= 390
    bar = p.page.locator("smart-sniffer-panel .bar")
    assert bar.is_visible()
    bar.locator("button.menu").click()
    assert p.page.evaluate("window.menuToggles") == 1
    p.shot("board-390")


@pytest.mark.parametrize("shell", ["", "ha"])
@pytest.mark.parametrize(("width", "height"), [(1280, 900), (390, 844)])
def test_the_panel_fills_the_window(page, shell, width, height):
    """VM999 bench on HA 2026.10.2: the page opened about 150 px tall, as
    the panel's parents give it no height there. The panel fills the window
    itself, with Home Assistant's height given (2024.4) or not (2026.10, the
    "ha" shell), wide and narrow, and the window itself never scrolls: only
    the page inside the frame does."""
    p = page(width=width, shell=shell, height=height)
    p.frame.locator("article.card").first.wait_for()
    box = p.page.evaluate("""() => {
        const el = document.querySelector('smart-sniffer-panel');
        const frame = el.shadowRoot.querySelector('iframe');
        const bar = el.shadowRoot.querySelector('.bar');
        const r = frame.getBoundingClientRect();
        return { inner: window.innerHeight, host: el.getBoundingClientRect().height,
                 frameTop: r.top, frameHeight: r.height, bar: bar.hidden ? 0 : bar.offsetHeight,
                 scroll: document.scrollingElement.scrollHeight };
    }""")
    assert abs(box["host"] - box["inner"]) <= 2, box
    assert box["frameHeight"] >= box["inner"] - box["bar"] - 2, box
    assert box["frameHeight"] >= 0.9 * box["inner"] - 56, box
    assert box["scroll"] <= box["inner"] + 1, box  # no second scrollbar
    if width <= 870:
        assert box["bar"] > 0 and abs(box["frameTop"] - box["bar"]) <= 1, box
    # The page inside scrolls on its own.
    assert p.doc.evaluate("document.documentElement.scrollHeight") > 0
    assert not p.errors, p.errors


def test_agent_line_only_with_the_app_on_haos(page):
    p = page()
    line = p.frame.locator("section.agent-line")
    line.wait_for()
    assert "a0d7b954-smart-sniffer-agent:9099" in line.inner_text()
    p.close()
    q = page("container")
    q.frame.locator("article.card").first.wait_for()
    assert q.frame.locator("section.agent-line").count() == 0


def test_haos_cards_are_hidden_never_removed_and_keep_their_label(page):
    p = page()
    haos = p.card("HAOS")
    haos.wait_for()
    assert haos.locator("button.trash").count() == 0
    assert haos.locator(".pill-ed").count() == 0
    where = haos.locator(".c-where").inner_text()
    assert where.startswith("/dev/") and "9099" not in where


def test_exit_icon_opens_the_device_in_home_assistant(page):
    p = page()
    nas = p.card("NAS-01")
    nas.wait_for()
    nas.locator("a.exit").first.click()
    assert p.wait(lambda: any(n.startswith("/config/devices/device/")
                              for n in p.page.evaluate("window.navigated")))


def test_details_and_thresholds_save_into_the_entry(page):
    p = page()
    card = p.card("Reallocated sectors")
    card.locator("button.chev").click()
    card.locator("table.attrs").first.wait_for()
    card.locator("button.tune").click()
    field = card.locator("input.thr-in").first
    field.wait_for()
    card.get_by_role("button", name="Accept current readings").click()
    card.get_by_role("button", name="Save", exact=True).click()
    assert p.wait(lambda: card.locator("button.tune[aria-pressed='false']").count() == 1)
    entries = _state(p.base)["entries"]
    thresholds = entries["01NAS00000000000000000000B"].get("thresholds") or {}
    assert any("Reallocated Sector Count" in v for v in thresholds.values())
    # The drive is judged again at once: no longer at Attention.
    assert p.wait(lambda: "Health OK" in card.inner_text() or "Maybe" in card.inner_text())
    assert not p.errors, p.errors


def test_hide_and_show_again_through_the_eye(page):
    p = page()
    card = p.card("Samsung SSD 870 EVO")
    card.locator("button[aria-label='Hide this card']").click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="Samsung SSD 870 EVO").count() == 0)
    assert "hidden" in json.dumps(_state(p.base)["state"]["hidden"]) or _state(p.base)["state"]["hidden"]
    eye = p.frame.locator("button.eye-btn")
    assert eye.inner_text().strip() == "1"
    eye.click()
    p.card("Samsung SSD 870 EVO").locator("button[aria-label='Hidden. Show this card again']").click()
    assert p.wait(lambda: not any(_state(p.base)["state"]["hidden"].values()))


def test_rename_a_drive_writes_its_device_name(page):
    p = page()
    card = p.card("Samsung SSD 870 EVO")
    card.locator(".t-name").click()
    box = p.frame.locator("input.name-in")
    box.fill("Boot SSD")
    box.press("Enter")
    assert p.wait(lambda: p.frame.locator("article.card h3.c-title", has_text="Boot SSD").count() == 1)
    # Kept by Home Assistant (the device's name_by_user), not by the panel.
    assert not _state(p.base)["state"]["card_names"]
    p.page.reload()
    p.frame = p.page.frame_locator("smart-sniffer-panel iframe")
    p.frame.locator("article.card h3.c-title", has_text="Boot SSD").wait_for()


def test_rename_a_machine_is_a_panel_label(page):
    p = page()
    pill = p.card("NAS-01").locator(".pill-ed")
    pill.click()
    box = p.frame.locator(".pill-edit input.name-in")
    box.fill("Basement NAS")
    box.press("Enter")
    assert p.wait(lambda: p.frame.locator(".pill", has_text="BASEMENT NAS").count() >= 4)
    assert _state(p.base)["state"]["machine_names"] == {"01NAS00000000000000000000B": "Basement NAS"}


def test_rename_a_filesystem_card_is_kept_by_the_panel(page):
    p = page()
    card = p.card("/srv/backup")
    card.locator(".t-name").click()
    box = p.frame.locator("input.name-in")
    box.fill("Backups")
    box.press("Enter")
    assert p.wait(lambda: _state(p.base)["state"]["card_names"] == {
        "01NAS00000000000000000000B|fs:fs-md1": "Backups"})


def test_reorder_by_keyboard_is_kept(page):
    p = page()
    first = _titles(p)[0]
    grip = p.frame.locator("article.card").first.locator("button.grip")
    grip.focus()
    grip.press("ArrowRight")
    assert p.wait(lambda: _titles(p)[1] == first)
    assert p.wait(lambda: _state(p.base)["state"]["order_set"] is True)


def test_trash_removes_a_card_from_the_page_only(page):
    p = page()
    card = p.card("QEMU HARDDISK")
    trash = card.locator("button.trash")
    trash.click()
    # In Home Assistant: two buttons, Remove here and Remove here + disable.
    card.locator("button.rm-ha").wait_for()
    trash.click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="QEMU HARDDISK").count() == 0)
    removed = _state(p.base)["state"]["removed"]
    assert any(k.startswith("01NAS00000000000000000000B|") for k in removed)


def test_trash_last_card_with_entry_deletes_the_machine(page):
    p = page()
    card = p.card("STUDIO-MAC")
    card.locator("button.trash").click()
    ha = card.locator("button.rm-ha")
    assert "delete entities" in ha.inner_text()
    ha.click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="STUDIO-MAC").count() == 0)
    assert "01MAC00000000000000000000C" not in _state(p.base)["entries"]
    assert "Deleted from Home Assistant." in p.frame.locator("p.toast").inner_text()


def test_password_on_the_card_runs_reauth(page):
    p = page()
    card = p.card("BACKUP-PI")
    card.locator("input[type=password]").fill("wrong")
    card.get_by_role("button", name="Authenticate").click()
    assert p.wait(lambda: "That password was refused." in card.inner_text())
    card.locator("input[type=password]").fill("fieldpass")
    card.get_by_role("button", name="Authenticate").click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="BACKUP-PI")
                  .filter(has_text="Health OK").count() == 1)


def test_plus_card_lists_discovered_agents_and_adds_them(page):
    p = page()
    p.frame.locator("button.card.ghost").click()
    row = p.frame.locator(".pick-row", has_text="office-nuc")
    row.wait_for()
    assert row.locator("input.tick").is_checked()
    p.frame.get_by_role("button", name="Add 1").click()
    assert p.wait(lambda: p.frame.locator(".pill", has_text="OFFICE-NUC").count() >= 1)
    assert p.frame.locator("button.card.ghost").count() == 1


def test_add_by_hand_checks_then_adds(page):
    p = page()
    p.frame.locator("button.card.ghost").click()
    p.frame.locator(".pick-row").first.wait_for()
    host = p.frame.locator("input.host")
    host.fill("10.0.10.98")
    p.frame.get_by_role("button", name="Check").click()
    msg = p.frame.locator("p.check-msg")
    assert p.wait(lambda: "is not a SMART Sniffer agent" in msg.inner_text())
    host.fill("nowhere.lan")
    p.frame.get_by_role("button", name="Check").click()
    assert p.wait(lambda: "Nothing answered at nowhere.lan:9099." in msg.inner_text())
    host.fill("10.0.20.14")
    p.frame.get_by_role("button", name="Check").click()
    assert p.wait(lambda: "Added 10.0.20.14 to the list, ticked." in msg.inner_text())
    assert "wants a password" in p.frame.locator(".add-foot").inner_text()
    p.shot("plus-open-1280")
    p.frame.get_by_role("button", name="Add 2").click()
    # The one that wants a password asks for it on its card.
    assert p.wait(lambda: p.frame.locator("article.card", has_text="MEDIA-BOX")
                  .filter(has_text="This agent wants a password.").count() == 1)


def test_agentic_access_in_the_panel(page):
    p = page()
    p.frame.get_by_role("link", name="Agentic access").click()
    pre = p.frame.locator("pre.ax-json")
    assert p.wait(lambda: '"schema": "smart-sniffer-agentic-access"' in pre.inner_text())
    doc = json.loads(pre.inner_text())
    assert {a["name"] for a in doc["agents"]} >= {"HAOS", "nas-01", "studio-mac", "backup-pi"}
    assert "fieldpass" not in pre.inner_text()
    assert "4 agents" in p.frame.locator(".ax-meta").inner_text()
    with p.page.expect_download() as dl:
        p.frame.get_by_role("button", name="Download").click()
    assert dl.value.suggested_filename.startswith("smart-sniffer-")
    p.shot("agentic-1280")
    p.frame.get_by_role("link", name="Control Center").click()
    p.frame.locator("article.card").first.wait_for()


def test_card_download_is_a_replica_file(page):
    p = page()
    card = p.card("Samsung SSD 870 EVO")
    with p.page.expect_download() as dl:
        card.locator("button[aria-label='Download as a file']").click()
    path = dl.value.path()
    doc = json.loads(Path(path).read_text())
    assert doc["format"] == "smart-sniffer-replica"
    assert doc["meta"]["serial"].startswith("REPLICA-")


def test_app_note_on_haos_without_the_app_and_dismissed_for_everyone(page):
    p = page("empty")
    empty = p.frame.locator("p.empty-line")
    empty.wait_for()
    assert empty.inner_text() == "No machines yet. Add one with the + card."
    note = p.frame.locator("p.app-note").first
    assert "Install the SMART Sniffer App." in note.inner_text()
    p.shot("empty-1280")
    p.frame.locator("button.card.ghost").click()
    assert p.frame.locator(".add-note").count() == 1
    p.shot("plus-empty-1280")
    p.frame.locator("p.app-note").first.locator("button.notice-x").click()
    assert p.wait(lambda: p.frame.locator("p.app-note").count() == 0)
    assert _state(p.base)["state"]["notes"] == {"app_hint": True}


def test_live_update_when_a_machine_stops_answering(page):
    p = page()
    card = p.card("NAS-01")
    card.wait_for()
    _post(p.base, "/fake/control", {"action": "down", "entry_id": "01NAS00000000000000000000B"})
    assert p.wait(lambda: "not answering" in p.card("NAS-01").locator(".seen").inner_text())
    _post(p.base, "/fake/control", {"action": "up", "entry_id": "01NAS00000000000000000000B"})
    assert p.wait(lambda: "not answering" not in p.card("NAS-01").locator(".seen").inner_text())


def test_page_xss_rules():
    """Same origin as Home Assistant: no x-html, no innerHTML, no data-built
    links (App rules, kept)."""
    panel = ROOT / "custom_components" / "smart_sniffer" / "panel"
    html = (panel / "index.html").read_text()
    js = (panel / "cc.js").read_text() + (panel / "panel.js").read_text()
    assert "x-html" not in html
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
    assert "eval(" not in js and "new Function" not in js


@pytest.mark.skipif(not SHOTS, reason="SS_PANEL_SHOTS is not set")
@pytest.mark.parametrize("width", [1280, 390])
def test_screenshots(page, width):
    """The set for the owner: the board, Details and thresholds open, the +
    card open, the empty page with the App note, Agentic access."""
    p = page(width=width)
    p.frame.locator("article.card").first.wait_for()
    p.shot(f"board-{width}")
    card = p.card("Reallocated sectors")
    card.locator("button.chev").click()
    card.locator("table.attrs").first.wait_for()
    p.shot(f"details-{width}")
    card.locator("button.tune").click()
    card.locator("input.thr-in").first.wait_for()
    p.shot(f"thresholds-{width}")
    p.page.close()
    p = page(width=width)
    p.frame.locator("button.card.ghost").click()
    p.frame.locator(".pick-row").first.wait_for()
    p.shot(f"plus-open-{width}")
    p.page.close()
    p = page("empty", width=width)
    p.frame.locator("p.empty-line").wait_for()
    p.shot(f"empty-app-note-{width}")
    p.frame.locator("button.card.ghost").click()
    p.frame.locator(".add-note").wait_for()
    p.shot(f"plus-empty-app-note-{width}")
    p.page.close()
    p = page(width=width)
    p.frame.get_by_role("link", name="Agentic access").click()
    p.frame.locator("pre.ax-json", has_text="smart-sniffer-agentic-access").wait_for()
    p.shot(f"agentic-{width}")


# ── The Replica (v0.9) ───────────────────────────────────────────────────


def _open_plus(p):
    """The + card open, the Network row discovery ticked unticked."""
    p.frame.locator("button.card.ghost").click()
    p.frame.locator(".area-replica .replica-pick label").first.wait_for()
    rows = p.frame.locator(".pick-row input:checked")
    p.wait(lambda: rows.count() > 0, timeout=2)
    for i in range(rows.count()):
        rows.nth(0).uncheck()


def _rep_row(p, name):
    return p.frame.locator(".area-replica .replica-pick label", has_text=name).first


def test_plus_replica_picks_files_into_the_replica_machine(page):
    p = page()
    _open_plus(p)
    area = p.frame.locator(".area-replica")
    assert "Test drives for trying alerts and automations." in area.inner_text()
    assert "Shipped with SMART Sniffer" in area.inner_text()
    assert "SATA drive" in _rep_row(p, "SATA HDD, healthy").inner_text()
    assert "ZFS pool" in _rep_row(p, "ZFS pool, degraded").inner_text()
    _rep_row(p, "SATA HDD, reallocated sectors").locator("input").check()
    _rep_row(p, "ZFS pool, degraded").locator("input").check()
    p.frame.get_by_role("button", name="Add 2").click()
    assert p.wait(lambda: p.frame.locator(".pill-ghost", has_text="REPLICA").count() == 2)
    card = p.card("SATA HDD, reallocated sectors")
    assert "Attention" in card.inner_text()
    assert card.locator(".pill-ed").count() == 0  # the machine keeps its label
    pool = p.frame.locator("article.card", has_text="ZFS pool, degraded").first
    assert "DEGRADED" in pool.inner_text()
    entries = _state(p.base)["entries"]
    assert any(e.get("kind") == "replica" and e["host"] == "replica.invalid" for e in entries.values())
    assert not p.errors, p.errors


def _shots(p, width):
    tag = "1280" if width == 1280 else "390"
    p.frame.locator(".pill-ghost").first.wait_for()
    p.shot(f"replica-cards-{tag}")
    _open_plus(p)
    p.frame.locator(".ghost-open").scroll_into_view_if_needed()
    p.shot(f"replica-picker-{tag}")


def test_replica_board_and_picker_1280(page):
    p = page("replica")
    rows = p.frame.locator(".area-replica")
    p.frame.locator(".pill-ghost").first.wait_for()
    # The file gone: its card says so, and can be removed.
    gone = p.card("Old test drive")
    assert "This replica file is gone. Remove this drive and pick another." in gone.inner_text()
    # The replicas-moved note, once.
    assert "Replicas moved into the integration." in p.frame.locator("p.moved-note").inner_text()
    _shots(p, 1280)
    text = rows.inner_text()
    for group in ("Shipped with SMART Sniffer", "Your replicas", "From /share/smart-sniffer/replicas"):
        assert group in text
    assert "Not a replica file" in _rep_row(p, "broken").inner_text()
    assert _rep_row(p, "broken").locator("input").is_disabled()
    assert "Upload a replica" in rows.inner_text()
    assert scroll_ok(p, 1280)
    assert not p.errors, p.errors


def scroll_ok(p, width):
    return p.doc.evaluate("document.documentElement.scrollWidth") <= width


def test_replica_board_and_picker_390(page):
    p = page("replica", width=390)
    _shots(p, 390)
    assert scroll_ok(p, 390)
    assert not p.errors, p.errors


def test_agentic_header_uses_the_documents_counts(page):
    """The header line's numbers are the document's counts: real agents
    apart from the Replica's simulated drives and pools (VM999 bench)."""
    p = page("replica")
    p.frame.locator(".pill-ghost").first.wait_for()
    p.frame.get_by_role("link", name="Agentic access").click()
    pre = p.frame.locator("pre.ax-json")
    assert p.wait(lambda: '"schema": "smart-sniffer-agentic-access"' in pre.inner_text())
    counts = json.loads(pre.inner_text())["counts"]
    meta = p.frame.locator(".ax-meta").inner_text()
    word = lambda n, one, many: f"{n} {one if n == 1 else many}"  # noqa: E731
    assert word(counts["agents"], "agent", "agents") in meta
    assert word(counts["drives"], "drive", "drives") in meta
    assert counts["replica_drives"] > 0
    assert f"Replica: {word(counts['replica_drives'], 'drive', 'drives')}" in meta
    if counts["replica_pools"]:
        assert word(counts["replica_pools"], "pool", "pools") in meta.split("Replica:")[1]
    total = counts["agents"] + 1
    assert f"{total} agents" not in meta  # the Replica is not counted as an agent
    assert not p.errors, p.errors


def test_replica_trash_removes_the_pick_not_the_file(page):
    p = page("replica")
    card = p.card("Old test drive")
    card.locator("button.trash").click()
    assert card.locator("button.rm-ha").count() == 0  # one Remove? only
    assert "Its file stays in your replicas." in card.locator("button.trash").get_attribute("aria-label")
    card.locator("button.trash").click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="Old test drive").count() == 0)
    assert p.frame.locator(".pill-ghost").count() == 2
    p.frame.locator("p.moved-note button.notice-x").click()
    assert p.wait(lambda: p.frame.locator("p.moved-note").count() == 0)
    assert _state(p.base)["state"]["notes"]["replicas_moved"] is True


def test_save_as_replica_lands_in_the_library(page):
    p = page()
    card = p.card("Samsung SSD 870 EVO")
    card.locator("button[aria-label='Save as replica']").click()
    toast = p.frame.locator("p.toast")
    assert p.wait(lambda: "Saved to your replicas." in toast.inner_text())
    assert "Samsung SSD 870 EVO 500GB, saved" in toast.inner_text()
    _open_plus(p)
    row = _rep_row(p, "Samsung SSD 870 EVO 500GB, saved")
    assert "Your replicas" in p.frame.locator(".area-replica").inner_text()
    row.locator("input").check()
    p.frame.get_by_role("button", name="Add 1").click()
    assert p.wait(lambda: p.frame.locator(".pill-ghost", has_text="REPLICA").count() == 1)
    # Never on a Replica card.
    rep = p.frame.locator("article.card", has=p.frame.locator(".pill-ghost")).first
    assert rep.locator("button[aria-label='Save as replica']").count() == 0


def test_upload_a_replica_and_its_errors(page, tmp_path):
    p = page()
    _open_plus(p)
    seed = json.loads((ROOT / "custom_components" / "smart_sniffer" / "replicas" / "nvme-wear.json").read_text())
    good = tmp_path / "mine.json"
    good.write_text(json.dumps({**seed, "name": "My uploaded NVMe"}))
    p.frame.locator("input[type=file]").set_input_files(str(good))
    row = _rep_row(p, "My uploaded NVMe")
    row.wait_for()
    assert row.locator("input").is_checked()
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "nope"}')
    p.frame.locator("input[type=file]").set_input_files(str(bad))
    msg = p.frame.locator(".up-msg")
    assert p.wait(lambda: msg.inner_text() == "That file is not a replica file.")
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (1024 * 1024 + 1))
    p.frame.locator("input[type=file]").set_input_files(str(big))
    assert p.wait(lambda: msg.inner_text() == "That file is over 1 MB.")
    p.frame.get_by_role("button", name="Add 1").click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="My uploaded NVMe").count() == 1)


def test_the_caps_disable_further_ticks(page):
    p = page("replica")
    _post(p.base, "/fake/control", {"action": "fill"})
    p.page.reload()
    p.frame = p.page.frame_locator("smart-sniffer-panel iframe")
    p.frame.locator(".pill-ghost").first.wait_for()
    _open_plus(p)
    area = p.frame.locator(".area-replica")
    assert p.wait(lambda: "The Replica holds up to 12 drives and 4 pools." in area.inner_text())
    assert _rep_row(p, "NVMe, healthy").locator("input").is_disabled()
    assert _rep_row(p, "ZFS pool, healthy").locator("input").is_disabled()


# ── Round 3 (owner's VM999 review) ───────────────────────────────────────


def test_network_lists_seen_added_and_scans_like_the_app(page):
    """The + card's Network: every agent zeroconf sees (the added one shown
    as added, not selectable), the discovery waiting, and the HA App 0.3.1
    block scan around this Home Assistant, run at once."""
    p = page()
    p.frame.locator("button.card.ghost").click()
    area = p.frame.locator("section.area-net")
    nas = p.frame.locator(".pick-row", has_text="nas-01")
    nas.wait_for()
    assert "nas-01, already added" in nas.inner_text()
    assert nas.locator("input.tick").is_disabled()
    assert p.frame.locator(".pick-row", has_text="office-nuc").locator("input.tick").is_checked()
    block = area.locator("input[aria-label='Network block']")
    assert block.input_value() == "10.0.10.0/24"
    # The scan ran: the bar is full, the new and added counts are shown.
    assert p.wait(lambda: "already added" in area.locator(".scan-prog").inner_text())
    assert "Home Assistant has found no other agent" not in area.inner_text()
    assert area.get_by_role("button", name="Rescan").count() == 1
    # Another block: garage-pi, ticked, with its lock.
    block.fill("10.0.30.0/24")
    area.get_by_role("button", name="Scan").click()
    row = p.frame.locator(".pick-row", has_text="garage-pi")
    row.wait_for()
    assert row.locator("input.tick").is_checked() and row.locator("svg.lock").count() == 1
    # Too wide: the block around this Home Assistant is offered.
    block.fill("10.0.0.0/16")
    area.get_by_role("button", name="Scan").click()
    wide = area.locator(".scan-wide")
    assert p.wait(lambda: "That is 65,534 addresses." in wide.inner_text())
    wide.get_by_role("button", name="Use 10.0.10.0/24").click()
    assert p.wait(lambda: block.input_value() == "10.0.10.0/24")
    p.shot("r3-network-1280")
    assert not p.errors, p.errors


def test_network_says_nothing_found_only_when_nothing_is(page):
    p = page("empty")
    p.frame.locator("button.card.ghost").click()
    area = p.frame.locator("section.area-net")
    assert p.wait(lambda: "None answered on 10.0.10.0/24, port 9099." in area.inner_text())
    assert "Home Assistant has found no other agent on the network." in area.inner_text()
    assert not p.errors, p.errors


@pytest.mark.parametrize("width", [1280, 390])
def test_plus_opens_in_view_with_the_keyboard_in_it(page, width):
    p = page("replica", width=width)
    p.frame.locator(".pill-ghost").first.wait_for()
    p.frame.locator("button.card.ghost").scroll_into_view_if_needed()
    p.doc.evaluate("window.scrollTo(0, 0)")  # the ghost box below the window
    p.frame.locator("button.card.ghost").evaluate("el => el.click()")
    p.frame.locator(".ghost-open").wait_for()
    assert p.wait(lambda: p.doc.evaluate("""() => {
        const r = document.querySelector('.ghost-open').getBoundingClientRect();
        return r.top >= -1 && r.top < window.innerHeight - 40;
    }"""))
    assert p.doc.evaluate("document.activeElement.getAttribute('aria-label')") == "Network block"
    assert not p.errors, p.errors


def test_delete_your_own_replica_file_with_the_cards_trash(page):
    p = page("replica")
    _open_plus(p)
    area = p.frame.locator(".area-replica")
    office = area.locator(".rep-row", has_text="Office SSD, saved")
    # Only your own files have a trash: never a shipped or /share one.
    assert area.locator(".rep-row", has_text="SATA HDD, healthy").first.locator("button.rep-del").count() == 0
    assert area.locator(".rep-row", has_text="Lab NVMe, from the share").locator("button.rep-del").count() == 0
    trash = office.locator("button.rep-del")
    trash.click()
    assert "Delete?" in trash.inner_text() and "armed" in trash.get_attribute("class")
    p.shot("r3-replica-delete-armed-1280")
    if "armed" not in trash.get_attribute("class"):  # the shot's resize moved the pointer off it
        trash.click()
    trash.click()
    assert p.wait(lambda: area.locator(".rep-row", has_text="Office SSD, saved").count() == 0)
    # A file a Replica card shows: its drive first.
    _rep_row(p, "Lab NVMe").wait_for()
    broken = area.locator(".rep-row", has_text="broken")
    broken.locator("button.rep-del").click()
    broken.locator("button.rep-del").click()
    assert p.wait(lambda: area.locator(".rep-row", has_text="broken").count() == 0)
    assert not p.errors, p.errors


def test_a_picked_file_is_not_deleted(page, tmp_path):
    p = page()
    _open_plus(p)
    seed = json.loads((ROOT / "custom_components" / "smart_sniffer" / "replicas" / "nvme-wear.json").read_text())
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps({**seed, "name": "Picked NVMe"}))
    p.frame.locator("input[type=file]").set_input_files(str(mine))
    _rep_row(p, "Picked NVMe").wait_for()
    p.frame.get_by_role("button", name="Add 1").click()
    assert p.wait(lambda: p.frame.locator("article.card", has_text="Picked NVMe").count() == 1)
    _open_plus(p)
    row = p.frame.locator(".area-replica .rep-row", has_text="Picked NVMe")
    row.locator("button.rep-del").click()
    row.locator("button.rep-del").click()
    err = p.frame.locator(".area-replica .rep-ask-err")
    assert p.wait(lambda: err.count() == 1 and err.inner_text() == "Remove its replica drive first.")
    assert row.count() == 1
    assert not p.errors, p.errors


def test_big_numbers_read_cleanly_with_the_exact_one_behind(page):
    p = page()
    card = p.card("Reallocated sectors")
    powered = card.locator(".c-stats .st-t", has_text="Powered").locator(".st-v").inner_text()
    assert powered.endswith(" d"), powered  # power-on as days
    card.locator("button.chev").click()
    table = card.locator("table.attrs").first
    table.wait_for()
    lba = table.locator("tr", has_text="Total_LBAs_Written").locator("td.c-raw .bign")
    shown = lba.inner_text()
    assert shown.endswith(("TB", "GB", "MB")) and "LBAs" in lba.get_attribute("title")
    assert table.locator(".raw-sub").count() == 0  # no second line
    poh = table.locator("tr", has_text="Power_On_Hours").locator("td.c-raw .bign")
    assert poh.inner_text().endswith(" d") and "hours" in poh.get_attribute("title")
    lba.click()
    pop = p.frame.locator(".numpop")
    pop.wait_for()
    assert "LBAs" in pop.inner_text() and pop.get_by_role("button", name="Copy").count() == 1
    # The header's copy glyph, not a word.
    assert pop.locator("button.copy svg use").get_attribute("href") == "#copy"
    assert pop.locator("button.copy").inner_text().strip() == ""
    p.shot("r3-lba-popover-1280")
    p.page.keyboard.press("Escape")
    assert p.wait(lambda: pop.count() == 0)
    assert not p.errors, p.errors


def test_replica_cards_dashed_hidden_cards_dotted(page):
    p = page("replica")
    rep = p.frame.locator("article.card", has=p.frame.locator(".pill-ghost")).first
    rep.wait_for()
    style = lambda el: el.evaluate("e => getComputedStyle(e).borderTopStyle")  # noqa: E731
    assert style(rep) == "dashed" and "is-replica" in rep.get_attribute("class")
    nas = p.card("NAS-01")
    assert style(nas) == "solid"
    # The dashes are the plain card border's colour and width, never brighter.
    color = lambda el: el.evaluate(  # noqa: E731
        "e => [getComputedStyle(e).borderTopColor, getComputedStyle(e).borderTopWidth, getComputedStyle(e).opacity]")
    assert color(rep) == color(nas)
    nas.locator("button[aria-label='Hide this card']").click()
    p.frame.locator("header button.eye-btn").click()
    hidden = p.frame.locator("article.card.is-hidden").first
    hidden.wait_for()
    assert style(hidden) == "dotted"
    p.shot("r4-replica-dashed-hidden-dotted-1280")
    assert not p.errors, p.errors


def test_replica_readings_edited_in_the_tune_editor(page):
    p = page("replica")
    card = p.card("SATA HDD, reallocated sectors")
    assert "Attention" in card.inner_text()
    card.locator("button.chev").click()
    card.locator("button.tune").click()
    field = card.locator("input.rep-in[aria-label='Raw value of 5 Reallocated_Sector_Ct']")
    field.wait_for()
    p.shot("r3-replica-edit-1280")
    field.fill("0")
    assert "pending" in field.get_attribute("class")
    card.locator("input.rep-in[aria-label='Raw value of 196 Reallocated_Event_Count']").fill("0")
    card.get_by_role("button", name="Save").click()
    toast = p.frame.locator("p.toast")
    assert p.wait(lambda: "Saved as a copy in your replicas." in toast.inner_text()), \
        card.locator(".thr-err").inner_text()
    edited = p.card("SATA HDD, reallocated sectors, edited")
    assert p.wait(lambda: "Health OK" in edited.inner_text())
    # Only on Replica cards.
    real = p.card("NAS-01")
    real.locator("button.chev").first.click()
    if real.locator("button.tune").count():
        real.locator("button.tune").click()
        real.locator("input.thr-in").first.wait_for()
        assert real.locator("input.rep-in").count() == 0
    assert not p.errors, p.errors


def test_a_machine_added_elsewhere_is_announced_with_a_way_to_it(page):
    p = page()
    p.frame.locator("article.card").first.wait_for()
    _post(p.base, "/fake/ws", {"type": "smart_sniffer/panel/agent/add",
                               "hosts": [{"address": "10.0.10.61", "port": 9099}]})
    toast = p.frame.locator("p.toast")
    assert p.wait(lambda: "Added office-nuc." in toast.inner_text() or "Added 10.0.10.61" in toast.inner_text())
    toast.get_by_role("button", name="Show").click()
    assert p.wait(lambda: p.frame.locator("article.card.flash").count() == 1)
    assert not p.errors, p.errors


# ── The SMART Sniffer App's handover (v0.9) ──────────────────────────────


@pytest.mark.parametrize("width", [1280, 390])
def test_app_handover_report_line(page, width):
    p = page("imported", width=width)
    note = p.frame.locator(".import-note")
    note.wait_for()
    text = note.inner_text()
    assert "Moved 1 machine from the SMART Sniffer App." in text
    assert "With them: 1 already here, 1 name." in text
    assert "1 machine not moved." in text
    # The machine that does not answer is on the page anyway, offline.
    p.frame.locator("article.card", has_text="10.0.10.250").first.wait_for()
    assert p.doc.evaluate("document.documentElement.scrollWidth") <= width
    note.get_by_role("button", name="Show").click()
    assert p.wait(lambda: "printer room:9099: not a valid address" in note.inner_text())
    p.shot(f"import-report-{width}")
    note.get_by_role("button", name="Discard printer room:9099").click()
    assert p.wait(lambda: "not moved" not in note.inner_text())
    note.get_by_role("button", name="Dismiss").click()
    assert p.wait(lambda: p.frame.locator(".import-note").count() == 0)
    state = _state(p.base)
    assert state["state"]["machine_names"].get("01NAS00000000000000000000B") == "Basement NAS"
    assert not p.errors, p.errors


def test_app_handover_report_dismissed_stays_dismissed(page):
    p = page("imported")
    note = p.frame.locator(".import-note")
    note.wait_for()
    note.get_by_role("button", name="Dismiss").click()
    assert p.wait(lambda: p.frame.locator(".import-note").count() == 0)
    p.page.reload()
    p.page.wait_for_selector("smart-sniffer-panel")
    frame = p.page.frame_locator("smart-sniffer-panel iframe")
    frame.locator("article.card").first.wait_for()
    p.page.wait_for_timeout(500)
    assert frame.locator(".import-note").count() == 0
    assert "fieldpass" not in json.dumps(_state(p.base)["state"])


def test_agentic_raw_link_and_token_guide(page):
    p = page()
    p.frame.get_by_role("link", name="Agentic access").click()
    p.frame.locator("pre.ax-json", has_text="smart-sniffer-agentic-access").wait_for()
    guide = p.frame.locator(".ax-token").inner_text()
    assert 'curl -H "Authorization: Bearer <token>" http://<ha>/api/smart_sniffer/agentic.json' in guide
    assert "?fresh=1" in guide and "?download=1" in guide
    raw = p.frame.get_by_role("link", name="Raw JSON")
    raw.wait_for()
    assert raw.get_attribute("href").startswith("/api/smart_sniffer/agentic.json?authSig=")
    with p.page.expect_popup() as popup:
        raw.click()
    tab = popup.value
    tab.wait_for_load_state()
    doc = json.loads(tab.locator("body").inner_text())
    assert doc["schema"] == "smart-sniffer-agentic-access"
    assert "fieldpass" not in tab.content()
    tab.close()
    p.shot("agentic-raw-1280")
    assert not p.errors, p.errors


# ── Round 6: vitals line, Details top, Device Statistics, one number rule ─


def _open(card):
    card.locator("button.chev").first.click()
    card.locator(".details").first.wait_for()


def _stats(card):
    """A card's stat row as (label, value) pairs, labels as written in the
    page (the uppercase is CSS)."""
    return card.locator(".c-stats .st-t").evaluate_all(
        "els => els.map(e => [e.querySelector('dt').textContent, e.querySelector('dd').textContent])")


def test_vitals_stat_row_temperature_days_written_read_worn(page):
    p = page()
    # NVMe (the App's agent): worn from the log's percentage_used.
    nvme = p.card("Samsung SSD 980 PRO 1TB")
    tiles = _stats(nvme)
    assert [k for k, _ in tiles] == ["Temp", "Powered", "Written", "Read", "Worn"], tiles
    values = dict(tiles)
    assert re.fullmatch(r"\d+ C", values["Temp"]) and re.fullmatch(r"[\d,]+ d", values["Powered"]), tiles
    assert values["Written"] == "4.67 TB" and values["Read"] == "9.34 TB" and values["Worn"] == "3%", tiles
    # Shown in capitals, a hairline before every tile (the first one clipped).
    assert nvme.locator(".st-k").first.inner_text() == "TEMP"
    lines = nvme.locator(".st-t").evaluate_all("els => els.map(e => getComputedStyle(e).borderLeftStyle)")
    assert set(lines) == {"solid"}, lines
    clip = nvme.locator(".c-stats").evaluate(
        "e => [e.getBoundingClientRect().left, e.querySelector('.st-t').getBoundingClientRect().left,"
        " getComputedStyle(e).overflow]")
    assert clip[1] < clip[0] and clip[2] == "hidden", clip
    # No status colours, no chips; the units in the lighter grey.
    assert nvme.locator(".c-stats .pill, .c-stats .chip").count() == 0
    colors = nvme.locator(".st-v > span").evaluate_all("els => [...new Set(els.map(e => getComputedStyle(e).color))]")
    assert len(colors) == 1, colors
    # Full units, never a bare T, G or M.
    assert all(re.search(r"\d (TB|GB|MB|kB|B)$", values[k]) for k in ("Written", "Read")), tiles
    # A SATA SSD with no Device Statistics: its wear attribute is never made
    # into "worn". Read only when known (this one has no attribute 242).
    ssd = dict(_stats(p.card("Samsung SSD 870 EVO 500GB")))
    assert "Written" in ssd and "Worn" not in ssd and "Read" not in ssd, ssd
    # A hard disk with Device Statistics but no page 7: no worn either.
    hdd = dict(_stats(p.card("WDC  WUH721414ALE604")))
    assert "Written" in hdd and "Read" in hdd and "Worn" not in hdd, hdd
    # A virtual disk that reports nothing: no row at all, no stray hairline.
    vd = p.card("QEMU HARDDISK")
    assert vd.locator(".c-stats, .st-t, .c-vitals").count() == 0
    assert not p.errors, p.errors


def test_stat_row_leaves_out_what_is_not_reported_and_zero_degrees(page):
    p = page()
    p.card("Samsung SSD 980 PRO 1TB")
    got = p.doc.evaluate("""() => {
      const x = Alpine.$data(document.querySelector('main.page'));
      const card = (stats) => ({ kind: 'drive', it: { drive: { smart_data: {} }, view: { stats } } });
      const keys = (c) => x.stats(c).map((t) => t.k + '=' + t.n + t.u);
      return {
        zero: keys(card({ temp_c: 0, power_on_hours: 12000 })),
        missing: keys(card({ power_on_hours: 30 })),
        none: x.stats(card({ temp_c: 0 })).length,
        warm: keys(card({ temp_c: 41 })),
        pool: x.stats({ kind: 'pool', it: {} }).length,
      };
    }""")
    assert got["zero"] == ["Powered=500 d"], got
    assert got["missing"] == ["Powered=30 h"], got
    assert got["none"] == 0 and got["pool"] == 0, got
    assert got["warm"] == ["Temp=41 C"], got
    assert not p.errors, p.errors


@pytest.mark.parametrize(("width", "card_px"), [(1100, 340), (390, 358)])
def test_stat_row_is_one_row_with_all_five(page, width, card_px):
    p = page(width=width)
    nvme = p.card("Samsung SSD 980 PRO 1TB")
    assert abs(nvme.evaluate("e => e.getBoundingClientRect().width") - card_px) <= 2
    tops = lambda: nvme.locator(".st-t").evaluate_all(  # noqa: E731
        "els => els.map(e => Math.round(e.getBoundingClientRect().top))")
    assert len(tops()) == 5 and len(set(tops())) == 1, tops()
    p.shot(f"r7-stat-row-{width}")
    # Still one row with the widest values a drive gives.
    wide = nvme.evaluate("""card => {
      const row = card.querySelector('.c-stats .st-row');
      const vals = ['-12 C', '12,345 d', '999 TB', '999 TB', '100%'];
      row.querySelectorAll('.st-v').forEach((d, i) => { d.textContent = vals[i]; });
      return [...row.querySelectorAll('.st-t')].map(e => Math.round(e.getBoundingClientRect().top));
    }""")
    assert len(set(wide)) == 1, wide
    assert not p.errors, p.errors


@pytest.mark.parametrize("width", [1280, 390])
def test_details_top_names_the_drive(page, width):
    p = page("replica", width=width)
    card = p.card("SATA HDD, reallocated sectors")
    _open(card)
    ident = card.locator("dl.kv.ident")
    labels = ident.locator("dt").all_inner_texts()
    assert labels == ["Model", "Serial", "Firmware", "Capacity", "Interface"], labels
    values = ident.locator("dd").all_inner_texts()
    assert values[0] == "Seagate Barracuda ST2000DM008"
    assert values[2:] == ["0001", "2.00 TB", "SATA 6.0 Gb/s"], values
    # No copy glyph at rest: model, serial and firmware open the popover,
    # and every value lines up on the right.
    assert ident.locator("button.copy").count() == 0
    assert ident.locator(".bign.txt").all_inner_texts() == [values[0], values[1], values[2]]
    rights = ident.locator("dd").evaluate_all(
        "els => els.map(e => Math.round((e.querySelector('.v') || e).getBoundingClientRect().right))")
    assert len(set(rights)) == 1, rights
    # The old rows are gone.
    details = card.locator(".details").inner_text()
    assert "Data Read" not in details and "not applicable" not in details
    # Device Statistics follow the attribute table, inside the same Details.
    assert card.locator(".details .devstat h4").inner_text().lower() == "device statistics"
    assert card.locator(".details").count() == 1
    assert p.doc.evaluate("document.documentElement.scrollWidth") <= width
    p.shot(f"r6-replica-details-{width}")
    # A field machine: Connect by hand comes after the drive's own lines.
    nas = p.card("WDC  WUH721414ALE604")
    _open(nas)
    order = nas.locator(".details > *").evaluate_all("els => els.map(e => e.className)")
    assert order.index("kv ident") < order.index("conn"), order
    assert not p.errors, p.errors


def test_nvme_details_say_nvme_and_no_device_statistics(page):
    p = page()
    nvme = p.card("Samsung SSD 980 PRO 1TB")
    _open(nvme)
    ident = nvme.locator("dl.kv.ident")
    labels = ident.locator("dt").all_inner_texts()
    # The mock's NVMe drive gives no firmware or capacity: left out.
    assert labels == ["Model", "Serial", "Interface"], labels
    assert ident.locator("dd").all_inner_texts()[-1] == "NVMe"
    assert nvme.locator(".devstat").count() == 0
    assert not p.errors, p.errors


def test_device_statistics_table_takes_the_stand_in_rows(page):
    p = page()
    card = p.card("WDC  WUH721414ALE604")
    _open(card)
    attrs = card.locator("table.attrs").first
    attrs.wait_for()
    # The page 4 count no longer sits in the attribute table...
    assert "from device statistics" not in attrs.inner_text().lower()
    # ...but in Device Statistics, marked, with its note.
    ds = card.locator("table.devstat-t")
    row = ds.locator("tr", has_text="Reported uncorrectable")
    assert "crit" in row.get_attribute("class")
    assert row.locator("td.c-raw").inner_text() == "18"
    assert "expected 0" in row.inner_text()
    names = ds.locator("td.nm .lb").all_inner_texts()
    assert {"Power cycles", "Temperature, lifetime max", "Temperature, lifetime min",
            "Time above the operating maximum", "Reallocated sectors", "Reported uncorrectable"} <= set(names), names
    # What the stat row shows is left out here.
    assert not {"Temperature", "Power-on hours", "Data written", "Data read", "Endurance used"} & set(names), names
    assert ds.locator("td.c-raw").all_inner_texts()[names.index("Temperature, lifetime max")].endswith(" C")
    p.shot("r6-devstat-1280")
    assert not p.errors, p.errors


def test_one_number_rule_everywhere(page):
    p = page("replica")
    card = p.card("SATA HDD, reallocated sectors")
    written = dict(_stats(card))["Written"]
    _open(card)
    attr = card.locator("table.attrs").first.locator("tr", has_text="Total_LBAs_Written").locator(".bign").inner_text()
    assert attr == written, (attr, written)
    assert re.fullmatch(r"\d\.\d\d TB|\d\d\.\d TB|\d{3} [GT]B", attr), attr  # three figures
    # The rule itself.
    fmt = p.doc.evaluate("""() => {
      const x = Alpine.$data(document.querySelector('main.page'));
      return [x.bytes(5104198542848), x.bytes(19834000000000), x.bytes(898000000000), x.bytes(999.6e9),
              x.bytes(512), x.compact(344046933), x.compact(12345), x.bytes(2000398934016)];
    }""")
    assert fmt == ["5.10 TB", "19.8 TB", "898 GB", "1.00 TB", "512 B", "344 M", "12,345", "2.00 TB"], fmt
    assert not p.errors, p.errors


def test_formatted_numbers_underline_only_on_hover(page):
    p = page()
    card = p.card("Samsung SSD 980 PRO 1TB")
    big = card.locator(".c-stats .bign").first
    deco = lambda: big.evaluate("e => getComputedStyle(e).textDecorationLine")  # noqa: E731
    assert deco() == "none"
    assert p.doc.evaluate("matchMedia('(hover: hover)').matches")
    big.hover()
    assert p.wait(lambda: deco() == "underline")
    assert big.evaluate("e => getComputedStyle(e).cursor") == "pointer"
    assert big.get_attribute("title")
    big.click()
    p.frame.locator(".numpop").wait_for()
    assert not p.errors, p.errors


def test_a_phone_gets_no_underline_and_a_tap_opens_the_number(page):
    p = page(width=390, touch=True)
    assert not p.doc.evaluate("matchMedia('(hover: hover)').matches")
    card = p.card("Samsung SSD 980 PRO 1TB")
    big = card.locator(".c-stats .bign").first
    big.tap()
    pop = p.frame.locator(".numpop")
    pop.wait_for()
    assert "hours" in pop.inner_text() and pop.locator("button.copy").count() == 1
    assert big.evaluate("e => getComputedStyle(e).textDecorationLine") == "none"
    assert not p.errors, p.errors


# ── Details header: text values open the big numbers' popover ─────────
# Model, Serial, Firmware, Host and Port show no copy glyph at rest. A
# click, a tap, Enter or Space opens the same popover as a big number, with
# the full value and Copy; Escape closes it. The board's field machines
# report no firmware and a Replica has no Host or Port, so the five are
# covered by a field machine's drive and a Replica drive together.

TEXT_CARDS = {
    "field": ("board", 'article.card[data-card="01NAS00000000000000000000B|MOCK-SATA-0003"]',
              ["Model", "Serial", "Host", "Port"]),
    "replica": ("replica", "SATA HDD, reallocated sectors", ["Model", "Serial", "Firmware"]),
}


def _text_card(page, which, **kwargs):
    scenario, where, labels = TEXT_CARDS[which]
    p = page(scenario, **kwargs)
    card = p.frame.locator(where) if where.startswith("article") else p.card(where)
    _open(card)
    fields = _text_fields(card)
    assert [f[0] for f in fields] == labels, fields
    return p, card, fields


def _text_fields(card):
    """(label, value, trigger) for Model, Serial, Firmware, Host and Port,
    the value as written (a model can hold two spaces)."""
    out = []
    for row in card.locator("dl.kv.ident > div").all():
        label = row.locator("dt").inner_text()
        if label in ("Model", "Serial", "Firmware"):
            out.append((label, row.locator("dd").text_content().strip(), row.locator(".bign.txt")))
    for cp in card.locator(".conn .cp").all():
        out.append((cp.locator(".cp-k").inner_text(), cp.locator(".cp-v").text_content(), cp.locator(".bign.txt")))
    return out


def _clip(p):
    p.page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=p.base)


@pytest.mark.parametrize("which", ["field", "replica"])
@pytest.mark.parametrize("width", [1280, 390])
def test_details_text_values_open_the_popover_and_copy_the_value(page, which, width):
    p, card, fields = _text_card(page, which, width=width)
    _clip(p)
    pop = p.frame.locator(".numpop")
    for label, value, trigger in fields:
        assert value.strip(), label
        trigger.click()
        pop.wait_for()
        # The exact value, whole: letters, dots and spaces kept.
        assert pop.locator(".cp-v").text_content() == value, (label, value)
        assert pop.locator("button.copy svg use").get_attribute("href") == "#copy"
        box = pop.evaluate("e => e.getBoundingClientRect().toJSON()")
        assert box["left"] >= 0 and box["right"] <= width, (label, box)
        # Under the value where it fits, else pulled in from the right edge.
        left = trigger.evaluate("e => e.getBoundingClientRect().left")
        if left + box["width"] <= width - 8:
            assert abs(box["left"] - left) <= 1, (label, left, box)
        else:
            assert abs(box["right"] - (width - 8)) <= 1, (label, box)
        pop.locator("button.copy").click()
        assert p.wait(lambda: pop.locator(".copied").is_visible() and pop.locator(".copied").inner_text() == "copied")
        assert p.page.evaluate("navigator.clipboard.readText()") == value, label
        # A second click on the value closes it.
        trigger.click()
        assert p.wait(lambda: pop.count() == 0), label
    assert p.doc.evaluate("document.documentElement.scrollWidth") <= width
    assert not p.errors, p.errors


def test_a_big_number_still_copies_its_digits(page):
    p = page("replica")
    _clip(p)
    card = p.card("SATA HDD, reallocated sectors")
    _open(card)
    lba = card.locator("table.attrs").first.locator("tr", has_text="Total_LBAs_Written").locator(".bign")
    exact = lba.get_attribute("title")
    lba.click()
    pop = p.frame.locator(".numpop")
    pop.wait_for()
    assert pop.locator(".cp-v").inner_text() == exact
    pop.locator("button.copy").click()
    assert p.wait(lambda: pop.locator(".copied").is_visible())
    assert p.page.evaluate("navigator.clipboard.readText()") == re.sub(r"[^0-9]", "", exact)
    assert not p.errors, p.errors


@pytest.mark.parametrize("which", ["field", "replica"])
@pytest.mark.parametrize("touch", [False, True])
def test_no_copy_glyph_at_rest_in_details(page, which, touch):
    p, card, fields = _text_card(page, which, width=390 if touch else 1280, touch=touch)
    assert card.locator(".details button.copy").count() == 0
    assert card.locator(".details .copied").count() == 0
    assert p.frame.locator(".numpop").count() == 0
    # Each value is the popover's trigger and nothing else sits beside it.
    for label, _value, trigger in fields:
        assert trigger.evaluate("e => e.parentElement.querySelectorAll('button, svg').length") == 0, label
    assert not p.errors, p.errors


@pytest.mark.parametrize("which", ["field", "replica"])
def test_text_values_underline_only_on_hover(page, which):
    p, card, fields = _text_card(page, which)
    assert p.doc.evaluate("matchMedia('(hover: hover)').matches")
    for label, _value, trigger in fields:
        style = lambda t=trigger: t.evaluate(  # noqa: E731
            "e => { const s = getComputedStyle(e); return [s.textDecorationLine, s.textDecorationStyle, s.cursor]; }")
        p.page.mouse.move(0, 0)
        assert p.wait(lambda s=style: s()[0] == "none"), label
        # No tooltip: the value is already the whole text.
        assert trigger.get_attribute("title") is None, label
        trigger.hover()
        assert p.wait(lambda s=style: s()[0] == "underline"), label
        assert style()[1:] == ["dotted", "pointer"], (label, style())
    assert not p.errors, p.errors


@pytest.mark.parametrize("which", ["field", "replica"])
def test_a_phone_gets_no_underline_on_text_values_and_a_tap_opens_them(page, which):
    p, card, fields = _text_card(page, which, width=390, touch=True)
    assert not p.doc.evaluate("matchMedia('(hover: hover)').matches")
    pop = p.frame.locator(".numpop")
    for label, value, trigger in fields:
        trigger.tap()
        pop.wait_for()
        assert pop.locator(".cp-v").text_content() == value, label
        assert pop.locator("button.copy").count() == 1
        assert trigger.evaluate("e => getComputedStyle(e).textDecorationLine") == "none", label
        trigger.tap()
        assert p.wait(lambda: pop.count() == 0), label
    assert not p.errors, p.errors


@pytest.mark.parametrize("which", ["field", "replica"])
def test_text_values_open_and_close_from_the_keyboard(page, which):
    p, card, fields = _text_card(page, which)
    pop = p.frame.locator(".numpop")
    for label, value, trigger in fields:
        assert trigger.get_attribute("role") == "button" and trigger.get_attribute("tabindex") == "0", label
        assert trigger.get_attribute("aria-label") == f"{label} {value}"
        trigger.focus()
        assert trigger.evaluate("e => e === document.activeElement"), label
        for key in ("Enter", " "):
            p.page.keyboard.press(key)
            assert p.wait(lambda: pop.count() == 1), (label, key)
            assert pop.locator(".cp-v").text_content() == value
            p.page.keyboard.press("Escape")
            assert p.wait(lambda: pop.count() == 0), (label, key)
    # Tab goes from one to the next.
    fields[0][2].focus()
    p.page.keyboard.press("Tab")
    assert fields[1][2].evaluate("e => e === document.activeElement")
    # The big numbers take Space the same way.
    big = card.locator(".c-stats .bign").first
    big.focus()
    p.page.keyboard.press(" ")
    assert p.wait(lambda: pop.count() == 1)
    p.page.keyboard.press("Escape")
    assert p.wait(lambda: pop.count() == 0)
    assert not p.errors, p.errors


def test_replica_cards_have_no_scenario_picker_and_edit_device_statistics(page):
    p = page("replica")
    assert p.frame.locator("select.scen, .scen-box").count() == 0
    card = p.card("SATA HDD, reallocated sectors")
    _open(card)
    card.locator("button.tune").click()
    field = card.locator("input.rep-in[aria-label='Raw value of Reported uncorrectable (Device Statistics)']")
    field.wait_for()
    assert card.locator("input.rep-in[aria-label='Raw value of Reallocated sectors (Device Statistics)']").count() == 1
    field.fill("5")
    card.get_by_role("button", name="Save").click()
    edited = p.card("SATA HDD, reallocated sectors, edited")
    row = edited.locator("table.devstat-t tr", has_text="Reported uncorrectable")
    assert p.wait(lambda: row.count() == 1 and row.locator("td.c-raw").inner_text() == "5")
    assert not p.errors, p.errors


# ── Round 7: attribute names wrap only at underscores; trim list ─────────


@pytest.mark.parametrize(("width", "card_px"), [(1100, 340), (390, 358)])
def test_attribute_names_never_break_inside_a_word(page, width, card_px):
    p = page(width=width)
    nvme = p.card("Samsung SSD 980 PRO 1TB")
    assert abs(nvme.evaluate("e => e.getBoundingClientRect().width") - card_px) <= 2
    _open(nvme)
    table = nvme.locator("table.attrs").first
    table.wait_for()
    names = table.locator("td.nm .lb").all_inner_texts()
    assert "available_spare_threshold" in names, names
    # Every piece between underscores sits on one line.
    split = table.locator("td.nm .lb > span > span").evaluate_all(
        "els => els.filter(e => e.getClientRects().length > 1).map(e => e.textContent)")
    assert split == [], split
    # The same with a longer name than any drive gives, and a wide value.
    split = table.evaluate("""t => {
      const row = t.querySelector('tbody tr');
      const parts = 'percentage_used_endurance_indicator_threshold_long_name'.split(/(?<=_)/);
      const lb = row.querySelector('td.nm .lb');
      lb.replaceChildren(...parts.map((w) => { const s = document.createElement('span');
        const t2 = document.createElement('span'); t2.textContent = w; s.append(t2, document.createElement('wbr'));
        return s; }));
      row.querySelector('td.c-raw').textContent = '123,456,789,012';
      return [...lb.querySelectorAll(':scope > span > span')].filter(e => e.getClientRects().length > 1).map(e => e.textContent);
    }""")
    assert split == [], split
    # A very long raw value (an NVMe 128-bit counter) wraps in its own box:
    # the table stays inside the card and the names keep their width.
    fit = table.evaluate("""t => {
      const cell = [...t.querySelectorAll('tbody tr')][1].querySelector('td.c-raw');
      const s = document.createElement('span');
      s.className = 'long';
      s.textContent = '1449400005035951791936293027446784';
      cell.replaceChildren(s);
      const name = t.querySelector('tbody tr td.nm');
      return [t.getBoundingClientRect().width <= t.parentElement.getBoundingClientRect().width + 1,
              name.getBoundingClientRect().width];
    }""")
    assert fit[0] and fit[1] > 150, fit
    assert p.doc.evaluate("document.documentElement.scrollWidth") <= width
    assert not p.errors, p.errors


def test_long_raw_values_get_their_own_box():
    html = (ROOT / "custom_components" / "smart_sniffer" / "panel" / "index.html").read_text()
    assert ':class="{ long: String(a.disp).length > 20 }"' in html


@pytest.mark.parametrize("width", [1270, 1100, 390])
def test_short_raw_values_stay_on_one_line(page, width):
    """A raw value of 20 characters or fewer never wraps, also in a narrow
    card (three columns or a phone); the table stays inside the card."""
    p = page(width=width)
    card = p.card("Samsung SSD 870 EVO 500GB")
    _open(card)
    table = card.locator("table.attrs").first
    table.wait_for()
    got = table.evaluate("""t => {
      const rows = [...t.querySelectorAll('tbody tr')];
      return ['38 (0 18 0 0 0)', '38 (Min/Max 19/48) C'].map((text, i) => {
        const span = rows[i].querySelector('td.c-raw span');
        span.textContent = text;
        const names = [...t.querySelectorAll('td.nm .lb > span > span')]
          .filter(e => e.getClientRects().length > 1).length;
        return [span.getClientRects().length, names,
                t.getBoundingClientRect().width <= t.parentElement.getBoundingClientRect().width + 1];
      });
    }""")
    assert got == [[1, 0, True], [1, 0, True]], got
    assert p.doc.evaluate("document.documentElement.scrollWidth") <= width
    p.shot(f"short-raw-{width}")
    assert not p.errors, p.errors


def test_name_parts_split_after_each_underscore(page):
    p = page()
    p.card("Samsung SSD 980 PRO 1TB")
    got = p.doc.evaluate("""() => {
      const x = Alpine.$data(document.querySelector('main.page'));
      return [x.nameParts('available_spare_threshold'), x.nameParts('Power cycles'), x.nameParts(null)];
    }""")
    assert got == [["available_", "spare_", "threshold"], ["Power cycles"], [""]], got
    assert not p.errors, p.errors


def test_device_statistics_trim_is_a_fixed_page_offset_list():
    js = (ROOT / "custom_components" / "smart_sniffer" / "panel" / "cc.js").read_text()
    m = re.search(r"const DEVSTAT_IN_STATS = new Set\(\[([^\]]*)\]\)", js)
    assert m, "DEVSTAT_IN_STATS not found"
    keys = re.findall(r"'(\d+:\d+)'", m.group(1))
    assert sorted(keys) == sorted(["5:8", "1:16", "1:24", "1:40", "7:8"]), keys


# ── v0.9.1 ──────────────────────────────────────────────────────────────────


def test_details_open_on_a_page_that_just_fits(browser_, backend):
    """Test box, 2026-10-23: on a page short enough to need no scrollbar,
    opening a card's Details froze the tab. The taller page brought a
    scrollbar, the cards narrowed, the row aligner measured again with the
    card closed (no scrollbar, wider), opened it (scrollbar, narrower), and
    went round without end. Every card's Details open and close here, in a
    Chromium with real scrollbars that take width (headless Chromium hides
    them by default, which hid the freeze)."""
    kwargs = {"ignore_default_args": ["--hide-scrollbars"]}
    if os.environ.get("PW_CHROMIUM"):
        kwargs["executable_path"] = os.environ["PW_CHROMIUM"]
    browser = browser_.browser_type.launch(**kwargs)
    p = Page(browser, backend, height=1600)
    try:
        p.frame.locator("article.card").first.wait_for()
        time.sleep(1)
        assert p.doc.evaluate("document.documentElement.scrollHeight") <= 1600
        cards = p.frame.locator("article.card")
        for i in range(cards.count()):
            chev = cards.nth(i).locator("button.chev")
            if not chev.count():
                continue
            chev.click(timeout=4000)
            # A frozen page answers nothing; this times out there.
            assert p.doc.evaluate("() => new Promise((r) => setTimeout(() => r(true), 200))")
            chev.click(timeout=4000)
        assert not p.errors, p.errors
    finally:
        p.close()
        browser.close()


def test_the_menu_bar_makes_room_for_a_top_inset(page):
    """iPhone app, 2026-10-22: the notch's inset was taken out of the bar's
    56 px, so the menu button and the title were cut off. The bar now grows
    by the inset."""
    p = page(width=390)
    p.frame.locator("article.card").first.wait_for()
    p.page.evaluate("document.querySelector('smart-sniffer-panel').style.setProperty("
                    "'--safe-area-inset-top', '47px')")
    bar = p.page.locator("smart-sniffer-panel .bar")
    box = bar.bounding_box()
    assert round(box["height"]) == 56 + 47
    for part in ("button.menu", ".title"):
        r = bar.locator(part).bounding_box()
        assert r["y"] >= box["y"] + 47 - 1
        assert r["y"] + r["height"] <= box["y"] + box["height"] + 1
    frame = p.page.locator("smart-sniffer-panel iframe").bounding_box()
    assert frame["y"] >= box["y"] + box["height"] - 1
