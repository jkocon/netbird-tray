#!/usr/bin/env python3
"""Prosta ikona NetBird w zasobniku w stylu tailscale-tray (oficjalny klient UI NetBird na Linuksie
to osobna aplikacja Fyne z własnym oknem - tu wystarczy menu).

Menu (lewy lub prawy klik): status (klik = połącz/rozłącz), profil (przełączanie, dodawanie, panel
admina, ważność sesji), to urządzenie, urządzenia w sieci, sieci (Networks / trasy), exit node,
ustawienia, About, Exit.

Rozmawia z demonem przez CLI `netbird` (status --json, networks, profile, up/down) - socket demona
ma prawa 0666, więc nie trzeba roota. Systemctl (autostart, restart usługi) idzie przez pkexec;
install.sh dodaje regułę polkit, żeby start/stop/restart nie pytały o hasło.
AppIndicator zamiast QSystemTrayIcon, bo w Plasmie tylko wtedy lewy klik otwiera menu.
"""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP_NAME = "NetBird Tray"
APP_VERSION = "1.0"
POLL_SECONDS = 3
ICON_DIR = Path(__file__).resolve().with_name("icons")
EXIT_PREFIXES = ("0.0.0.0/0", "::/0")


def icon_path(name: str) -> str:
    """Pełna ścieżka do kopii ikony z hashem zawartości w nazwie.

    Pełna ścieżka, nie nazwa: Plasma ignoruje IconThemePath z płaskim katalogiem i obcina nieznaną
    nazwę do "netbird-tray" (ikona aplikacji). Hash w nazwie: Plasma trzyma w cache ikonę spod
    tej samej ścieżki, więc po zmianie pliku pokazywała starą wersję."""
    src = ICON_DIR / f"{name}.svg"
    try:
        data = src.read_bytes()
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/netbird-tray-{os.getuid()}") / "netbird-tray"
        runtime.mkdir(parents=True, exist_ok=True)
        dst = runtime / f"{name}-{hashlib.sha1(data).hexdigest()[:10]}.svg"
        if not dst.exists():
            dst.write_bytes(data)
        return str(dst)
    except OSError:
        return str(src)


ICON_ON, ICON_OFF, ICON_EXIT, ICON_AUTH, ICON_RELAY = (
    icon_path(name)
    for name in ("netbird-tray-on", "netbird-tray-disconnected", "netbird-tray-exit", "netbird-tray-auth",
                 "netbird-tray-connecting")
)


# ---------- usługa i socket demona ----------

def service_unit() -> str:
    """Jednostka systemd demona. Pakiet AUR netbird-bin ma szablon netbird@.service (instancja = nazwa
    interfejsu, np. netbird@wt0), a `netbird service install` tworzy zwykłe netbird.service."""
    res = run("systemctl", "list-units", "--all", "--plain", "--no-legend", "netbird.service", "netbird@*.service")
    units = [line.split()[0] for line in res.stdout.splitlines() if line.strip()]
    for unit in units:  # najpierw działająca
        if run("systemctl", "is-active", unit).stdout.strip() == "active":
            return unit
    res = run("systemctl", "list-unit-files", "--plain", "--no-legend", "netbird.service", "netbird@*.service")
    files = [line.split() for line in res.stdout.splitlines() if line.strip()]
    for name, state, *_ in files:
        if state == "enabled" and not name.endswith("@.service"):
            return name
    return units[0] if units else "netbird@wt0.service"


def daemon_addr(unit: str) -> str:
    # Instancja szablonu ma własny socket - NB_DAEMON_ADDR z sesji wskazuje tylko jedną z nich.
    m = re.fullmatch(r"netbird@(.+)\.service", unit)
    if m:
        return f"unix:///var/run/netbird/{m.group(1)}.sock"
    env = os.environ.get("NB_DAEMON_ADDR")
    if env:
        return env
    socks = glob.glob("/var/run/netbird.sock") + sorted(glob.glob("/var/run/netbird/*.sock"))
    return f"unix://{socks[0]}" if socks else "unix:///var/run/netbird.sock"


def all_units() -> list[str]:
    """Wszystkie włączone albo działające instancje (np. prywatna netbird@wt0 i firmowa netbird@wt1)."""
    res = run("systemctl", "list-units", "--all", "--plain", "--no-legend", "netbird.service", "netbird@*.service")
    units = {line.split()[0] for line in res.stdout.splitlines()
             if line.strip() and line.split()[2] == "active"}
    res = run("systemctl", "list-unit-files", "--plain", "--no-legend", "netbird.service", "netbird@*.service")
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "enabled" and not parts[0].endswith("@.service"):
            units.add(parts[0])
    return sorted(units)


def instance_name(unit: str) -> str:
    m = re.fullmatch(r"netbird@(.+)\.service", unit)
    return m.group(1) if m else "default"


# ---------- pomocnicze ----------

def idle_once(func, *args) -> None:
    """Wywołaj func raz w wątku GTK. Samo GLib.idle_add powtarza wywołanie, dopóki funkcja zwraca
    True - z webbrowser.open (zwraca True) otwierało to przeglądarkę w pętli i położyło sesję."""
    def _call() -> bool:
        func(*args)
        return False
    GLib.idle_add(_call)


def run(*cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Polecenie z limitem czasu, które przy przekroczeniu ubija całą grupę procesów."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        err = (err or "") + f"\n(timed out after {timeout}s)"
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# CLI netbird loguje ostrzeżenia gRPC na stderr przy każdym wywołaniu - do komunikatów tylko reszta.
NOISE = re.compile(r"^\S+ (INFO|WARN|DEBG) .*$|^.*caller_not_available.*$", re.MULTILINE)


def output(res: subprocess.CompletedProcess) -> str:
    return NOISE.sub("", (res.stderr or "") + (res.stdout or "")).strip()


def notify(title: str, body: str = "") -> None:
    subprocess.Popen(["notify-send", "-a", APP_NAME, "-i", "netbird-tray", title, body])


def error(msg: str) -> None:
    subprocess.Popen(["kdialog", "--title", APP_NAME, "--error", msg])


def copy_to_clipboard(text: str) -> None:
    # Na Waylandzie proces bez okna nie ustawi schowka przez GTK - wl-copy tak.
    if shutil.which("wl-copy") and os.environ.get("WAYLAND_DISPLAY"):
        subprocess.run(["wl-copy", text], timeout=5)
    else:
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(text, -1)
        cb.store()
    notify("Copied to clipboard", text)


def in_terminal(*cmd: str) -> None:
    script = '"$@"; echo; printf "Press Enter to close… "; read _'
    for term in ("konsole", "alacritty", "kitty", "xterm"):
        if shutil.which(term):
            subprocess.Popen([term, "-e", "sh", "-c", script, "sh", *cmd])
            return
    error("No terminal emulator found to run:\n" + " ".join(cmd))


def short_name(fqdn: str) -> str:
    return (fqdn or "?").split(".")[0]


def strip_prefix(ip: str) -> str:
    return (ip or "-").split("/")[0]


def until_text(iso: str) -> str:
    """'2026-10-08T17:06:52.05Z' -> 'in 23h 59m' / 'expired'."""
    try:
        when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d*", r"\1", iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = int((when - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "expired"
    days, rest = divmod(secs, 86400)
    hours, mins = divmod(rest // 60, 60)
    return f"in {days}d {hours}h" if days else f"in {hours}h {mins}m"


def parse_networks(text: str) -> list[dict]:
    """Wyjście `netbird networks list` (brak trybu JSON) -> [{id, network, domains, selected}]."""
    nets, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- ID:"):
            cur = {"id": line[5:].strip(), "network": "", "domains": "", "selected": False}
            nets.append(cur)
        elif cur is not None and ":" in line:
            key, val = (s.strip() for s in line.split(":", 1))
            if key == "Network":
                cur["network"] = val
            elif key == "Domains":
                cur["domains"] = val
            elif key == "Status":
                cur["selected"] = val.lower() == "selected"
    return nets


def parse_profiles(text: str) -> tuple[list[str], str]:
    names, active = [], ""
    for line in text.splitlines()[1:]:  # pierwsza linia to nagłówek NAME ACTIVE
        parts = line.split()
        if not parts:
            continue
        names.append(parts[0])
        if len(parts) > 1 and parts[-1] == "✓":
            active = parts[0]
    return names, active


class NetBirdTray:
    def __init__(self, unit: str = "") -> None:
        self.unit = unit or service_unit()
        os.environ["NB_DAEMON_ADDR"] = daemon_addr(self.unit)
        self.status: dict | None = None
        self.networks: list[dict] = []
        self.profiles: list[str] = []
        self.profile = ""
        self.error = ""
        self.autostart = self.unit_enabled()
        self.signature = None
        self.updating = False  # blokuje sygnały 'toggled' przy programowym ustawianiu pozycji
        self.busy = ""
        self.polling = False
        self.login_proc: subprocess.Popen | None = None

        self.indicator = AppIndicator3.Indicator.new(f"netbird-tray-{instance_name(self.unit)}", ICON_OFF,
                                                     AppIndicator3.IndicatorCategory.SYSTEM_SERVICES)
        self.indicator.set_title(f"NetBird ({instance_name(self.unit)})")
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self.build_menu())
        self.refresh()
        GLib.timeout_add_seconds(POLL_SECONDS, self.refresh)

    # ---------- stan ----------

    def unit_enabled(self) -> bool:
        return run("systemctl", "is-enabled", self.unit).stdout.strip() == "enabled"

    def refresh(self) -> bool:
        """Odpytanie CLI w wątku (każde wywołanie to osobny proces), wynik do GTK przez idle_once."""
        if not self.polling:
            self.polling = True
            threading.Thread(target=self._poll, daemon=True).start()
        return True

    def _poll(self) -> None:
        status, err, networks, profiles, profile = None, "", [], [], ""
        res = run("netbird", "status", "--json", timeout=8)
        try:
            status = json.loads(res.stdout)
        except ValueError:
            err = output(res) or "NetBird daemon is not responding"
            if run("systemctl", "is-active", self.unit).stdout.strip() != "active":
                err = "service-down"
        if status:
            if status.get("daemonStatus") == "Connected":
                networks = parse_networks(run("netbird", "networks", "list", timeout=8).stdout)
            profiles, profile = parse_profiles(run("netbird", "profile", "list", timeout=8).stdout)
        idle_once(self._apply, status, err, networks, profiles, profile)

    def _apply(self, status, err, networks, profiles, profile) -> None:
        self.polling = False
        self.status, self.error, self.networks = status, err, networks
        self.profiles, self.profile = profiles, profile or (status or {}).get("profileName", "")
        sig = self.make_signature()
        if sig != self.signature:
            self.signature = sig
            self.update_icon()
            self.indicator.set_menu(self.build_menu())

    def force_refresh(self) -> None:
        self.signature = None
        self.indicator.set_menu(self.build_menu())
        self.refresh()

    def make_signature(self):
        s = self.status or {}
        peers = tuple(sorted(
            (p.get("fqdn"), p.get("netbirdIp"), p.get("status"), p.get("connectionType"), p.get("latency", 0) // 10**7)
            for p in (s.get("peers") or {}).get("details") or []
        ))
        return (
            self.error, self.busy, s.get("daemonStatus"), s.get("fqdn"), s.get("netbirdIp"),
            (s.get("management") or {}).get("connected"), (s.get("signal") or {}).get("connected"),
            (s.get("relays") or {}).get("available"), s.get("usesKernelInterface"),
            (s.get("sshServer") or {}).get("enabled"), until_text(s.get("sessionExpiresAt") or "")[:6],
            peers, tuple(tuple(sorted(n.items())) for n in self.networks),
            tuple(self.profiles), self.profile, self.autostart,
        )

    @property
    def state(self) -> str:
        return (self.status or {}).get("daemonStatus", "")

    @property
    def connected(self) -> bool:
        return self.state == "Connected"

    def exit_node(self) -> dict | None:
        for n in self.networks:
            if n["network"] in EXIT_PREFIXES and n["selected"]:
                return n
        return None

    def peers(self) -> list[dict]:
        return list(((self.status or {}).get("peers") or {}).get("details") or [])

    def admin_url(self) -> str:
        url = ((self.status or {}).get("management") or {}).get("url", "")
        if not url:
            return ""
        u = urlparse(url)
        port = "" if u.port in (None, 443) else f":{u.port}"
        return f"https://{u.hostname}{port}"

    def update_icon(self) -> None:
        mgmt_ok = ((self.status or {}).get("management") or {}).get("connected")
        if self.state in ("NeedsLogin", "SessionExpired", "LoginFailed"):
            icon, desc = ICON_AUTH, "NetBird: login required"
        elif self.connected and not mgmt_ok:
            icon, desc = ICON_RELAY, "NetBird: connected, management server unreachable"
        elif self.connected and self.exit_node():
            icon, desc = ICON_EXIT, "NetBird: connected (exit node)"
        elif self.connected:
            icon, desc = ICON_ON, "NetBird: connected"
        elif self.state == "Connecting" or self.busy:
            icon, desc = ICON_RELAY, "NetBird: connecting"
        else:
            icon, desc = ICON_OFF, "NetBird: disconnected"
        self.indicator.set_icon_full(icon, desc)

    # ---------- menu ----------

    def item(self, label: str, callback=None, sensitive: bool = True) -> Gtk.MenuItem:
        it = Gtk.MenuItem(label=label)
        it.set_use_underline(False)
        if callback:
            it.connect("activate", lambda _w: callback())
        it.set_sensitive(sensitive and (callback is not None))
        return it

    def check(self, label: str, active: bool, callback, sensitive: bool = True) -> Gtk.CheckMenuItem:
        it = Gtk.CheckMenuItem(label=label)
        it.set_use_underline(False)
        it.set_active(active)
        it.set_sensitive(sensitive)
        it.connect("toggled", lambda w: None if self.updating else callback(w.get_active()))
        return it

    def build_menu(self) -> Gtk.Menu:
        self.updating = True
        m = Gtk.Menu()
        add = m.append
        state = self.state

        # --- nagłówek: która sieć (przy kilku instancjach ikony wyglądają tak samo) ---
        host = urlparse(self.admin_url()).hostname or instance_name(self.unit)
        add(self.item(f"NetBird – {host}"))
        add(Gtk.SeparatorMenuItem())

        # --- status ---
        if self.busy:
            add(self.item(self.busy))
        elif self.error == "service-down":
            add(self.item("NetBird service is not running - click to start", self.start_service))
        elif self.error:
            add(self.item(f"NetBird: {self.error[:80]}"))
        elif state == "Connected":
            add(self.check("Connected", True, lambda _a: self.disconnect()))
        elif state in ("NeedsLogin", "SessionExpired", "LoginFailed"):
            text = "Session expired" if state == "SessionExpired" else "Logged out"
            add(self.item(f"{text} - click to log in…", self.login))
        elif state == "Connecting":
            add(self.item("Connecting…"))
        else:
            add(self.check("Disconnected - click to connect", False, lambda _a: self.connect()))
        if self.connected:
            s = self.status or {}
            problems = [name for name in ("management", "signal") if not (s.get(name) or {}).get("connected")]
            if problems:
                add(self.item("⚠ " + " and ".join(problems).capitalize() + " server unreachable"))
        add(Gtk.SeparatorMenuItem())

        # --- profil / konto ---
        add(self.profiles_menu())
        add(Gtk.SeparatorMenuItem())

        # --- to urządzenie + urządzenia w sieci ---
        s = self.status or {}
        if s.get("netbirdIp"):
            ip = strip_prefix(s["netbirdIp"])
            mode = "kernel" if s.get("usesKernelInterface") else "userspace"
            add(self.item(f"This device: {short_name(s.get('fqdn', ''))} ({ip})  [{mode} WireGuard]",
                          lambda ip=ip: copy_to_clipboard(ip)))
        add(self.devices_menu())
        add(Gtk.SeparatorMenuItem())

        # --- sieci i exit node ---
        add(self.networks_menu())
        add(self.exit_nodes_menu())
        add(Gtk.SeparatorMenuItem())

        # --- ustawienia, about ---
        add(self.settings_menu())
        add(self.item("About", self.about))
        add(Gtk.SeparatorMenuItem())
        add(self.item("Exit", Gtk.main_quit))

        m.show_all()
        self.updating = False
        return m

    def profiles_menu(self) -> Gtk.MenuItem:
        root = Gtk.MenuItem(label=f"Profile: {self.profile}" if self.profile else "Profiles")
        root.set_use_underline(False)
        sub = Gtk.Menu()
        group: list[Gtk.RadioMenuItem] = []
        for name in sorted(self.profiles, key=str.lower):
            it = Gtk.RadioMenuItem.new_with_label(group[0].get_group() if group else None, name)
            it.set_use_underline(False)
            group.append(it)
            it.set_active(name == self.profile)
            it.set_sensitive(not self.busy)
            it.connect("toggled", lambda w, n=name: None if self.updating or not w.get_active() else self.switch_profile(n))
            sub.append(it)
        if self.profiles:
            sub.append(Gtk.SeparatorMenuItem())
        expires = (self.status or {}).get("sessionExpiresAt") or ""
        if expires and self.connected:
            sub.append(self.item(f"Session expires {until_text(expires)}"))
        sub.append(self.item("Log in again (extend session)…", self.extend_session if self.connected else self.login,
                             bool(self.status) and not self.busy))
        sub.append(self.item("Add another profile…", self.add_profile, bool(self.status) and not self.busy))
        admin = self.admin_url()
        sub.append(self.item("Admin console", lambda: webbrowser.open(admin), bool(admin)))
        root.set_submenu(sub)
        return root

    def devices_menu(self) -> Gtk.MenuItem:
        peers = self.peers()
        online = sum(1 for p in peers if p.get("status") == "Connected")
        root = Gtk.MenuItem(label=f"Network devices ({online}/{len(peers)} connected)")
        sub = Gtk.Menu()
        if not peers:
            sub.append(self.item("No devices"))
        for p in sorted(peers, key=lambda n: (n.get("status") != "Connected", short_name(n.get("fqdn", "")).lower())):
            ip = strip_prefix(p.get("netbirdIp", ""))
            up = p.get("status") == "Connected"
            how = ""
            if up:
                how = f"  [{p.get('connectionType') or '?'}"
                latency_ms = (p.get("latency") or 0) / 1e6
                how += f", {latency_ms:.0f} ms]" if latency_ms >= 1 else "]"
            elif p.get("status") == "Idle":
                how = "  [idle]"
            mark = "●" if up else "○"
            sub.append(self.item(f"{mark} {short_name(p.get('fqdn', ''))}  {ip}{how}", lambda ip=ip: copy_to_clipboard(ip)))
        sub.append(Gtk.SeparatorMenuItem())
        sub.append(self.item("Click a device to copy its IP"))
        root.set_submenu(sub)
        root.set_sensitive(bool(self.status))
        return root

    def networks_menu(self) -> Gtk.MenuItem:
        nets = [n for n in self.networks if n["network"] not in EXIT_PREFIXES]
        on = sum(1 for n in nets if n["selected"])
        root = Gtk.MenuItem(label=f"Networks ({on}/{len(nets)})" if nets else "Networks")
        sub = Gtk.Menu()
        if not nets:
            sub.append(self.item("No networks available" if self.connected else "Connect to see networks"))
        for n in sorted(nets, key=lambda x: x["id"].lower()):
            target = n["network"] or n["domains"]
            sub.append(self.check(f"{n['id']}  ({target})", n["selected"],
                                  lambda v, nid=n["id"]: self.set_network(nid, v), not self.busy))
        if nets:
            sub.append(Gtk.SeparatorMenuItem())
            sub.append(self.item("Select all (incl. future ones)", lambda: self.netbird_bg(
                "Changing networks…", "Could not select networks", "networks", "select", "all"), not self.busy))
        root.set_submenu(sub)
        root.set_sensitive(self.connected)
        return root

    def exit_nodes_menu(self) -> Gtk.MenuItem:
        exits = [n for n in self.networks if n["network"] in EXIT_PREFIXES]
        current = self.exit_node()
        root = Gtk.MenuItem(label=f"Exit node: {current['id']}" if current else "Exit nodes")
        root.set_use_underline(False)
        sub = Gtk.Menu()
        group: list[Gtk.RadioMenuItem] = []

        def radio(text: str, active: bool, nid: str) -> Gtk.RadioMenuItem:
            it = Gtk.RadioMenuItem.new_with_label(group[0].get_group() if group else None, text)
            it.set_use_underline(False)
            group.append(it)
            it.set_active(active)
            it.set_sensitive(not self.busy)
            it.connect("toggled", lambda w: None if self.updating or not w.get_active() else self.set_exit_node(nid))
            return it

        sub.append(radio("None", current is None, ""))
        for n in sorted(exits, key=lambda x: x["id"].lower()):
            sub.append(radio(n["id"], current is not None and n["id"] == current["id"], n["id"]))
        if not exits:
            sub.append(self.item("No exit nodes in this network"))
        root.set_submenu(sub)
        root.set_sensitive(self.connected)
        return root

    def settings_menu(self) -> Gtk.MenuItem:
        s = self.status or {}
        root = Gtk.MenuItem(label="Settings")
        sub = Gtk.Menu()
        sub.append(self.check("Allow SSH server on this device", bool((s.get("sshServer") or {}).get("enabled")),
                              self.set_ssh, self.connected and not self.busy))
        sub.append(self.check("Start with the system", self.autostart, self.set_autostart))
        sub.append(Gtk.SeparatorMenuItem())
        sub.append(self.item("Restart NetBird service", self.restart_service, not self.busy))
        sub.append(self.item("Service log", lambda: in_terminal("journalctl", "-u", self.unit, "-n", "200", "-f")))
        root.set_submenu(sub)
        return root

    # ---------- akcje ----------

    def in_background(self, busy: str, work, done=None) -> None:
        """Długie polecenie (CLI netbird, systemctl) w wątku, żeby menu nie zamarzało;
        w tym czasie status w menu pokazuje, co się dzieje."""
        if self.busy:
            return
        self.busy = busy
        self.force_refresh()

        def _thread() -> None:
            result = work()
            idle_once(_finish, result)

        def _finish(result) -> None:
            self.busy = ""
            if done:
                done(result)
            self.force_refresh()

        threading.Thread(target=_thread, daemon=True).start()

    def report(self, res: subprocess.CompletedProcess, what: str) -> None:
        if res.returncode not in (0, 126):  # 126 = anulowane okno pkexec
            error(f"{what}:\n{output(res)}")

    def netbird_bg(self, busy: str, what: str, *args: str, timeout: int = 60) -> None:
        self.in_background(busy, lambda: run("netbird", *args, timeout=timeout), lambda res: self.report(res, what))

    def connect(self) -> None:
        # Bez zapamiętanego logowania `up` poprosi o SSO - wtedy przejmuje to login().
        if self.state in ("NeedsLogin", "SessionExpired", "LoginFailed"):
            self.login()
            return
        self.netbird_bg("Connecting…", "Could not connect", "up", "--no-browser", timeout=90)

    def disconnect(self) -> None:
        self.netbird_bg("Disconnecting…", "Could not disconnect", "down")

    def set_network(self, nid: str, selected: bool) -> None:
        args = ("networks", "select", "-a", nid) if selected else ("networks", "deselect", nid)
        self.netbird_bg("Changing networks…", "Could not change the network selection", *args)

    def set_exit_node(self, nid: str) -> None:
        current = self.exit_node()

        def work():
            res = None
            if current and current["id"] != nid:
                res = run("netbird", "networks", "deselect", current["id"])
            if nid:
                res = run("netbird", "networks", "select", "-a", nid)
            return res or subprocess.CompletedProcess((), 0, "", "")

        self.in_background("Changing exit node…", work, lambda res: self.report(res, "Could not change the exit node"))

    def set_ssh(self, enabled: bool) -> None:
        # Ustawienia `up` działają dopiero po ponownym połączeniu ("Already connected" bez down).
        def work():
            run("netbird", "down")
            return run("netbird", "up", "--no-browser", f"--allow-server-ssh={str(enabled).lower()}", timeout=90)
        self.in_background("Reconnecting…", work, lambda res: self.report(res, "Could not change the SSH setting"))

    def systemctl(self, verb: str) -> subprocess.CompletedProcess:
        return run("pkexec", "systemctl", verb, self.unit, timeout=120)

    def start_service(self) -> None:
        self.in_background("Starting NetBird service…", lambda: self.systemctl("start"),
                           lambda res: self.report(res, "Could not start the NetBird service"))

    def restart_service(self) -> None:
        self.in_background("Restarting NetBird service…", lambda: self.systemctl("restart"),
                           lambda res: self.report(res, "Could not restart the NetBird service"))

    def set_autostart(self, enabled: bool) -> None:
        # "Start with the system" = usługa włączona w systemd; z autoconnect demon sam się połączy.
        res = self.systemctl("enable" if enabled else "disable")
        self.report(res, f"systemctl {'enable' if enabled else 'disable'} {self.unit} failed")
        self.autostart = self.unit_enabled()
        self.force_refresh()

    def switch_profile(self, name: str) -> None:
        was_connected = self.connected

        def work():
            if was_connected:
                run("netbird", "down")
            res = run("netbird", "profile", "select", name)
            if res.returncode == 0 and was_connected:
                up = run("netbird", "up", "--no-browser", timeout=90)
                if up.returncode != 0 and "login" in output(up).lower():
                    return up  # profil wymaga logowania - done() uruchomi login()
            return res

        def done(res: subprocess.CompletedProcess) -> None:
            text = output(res).lower()
            if res.returncode != 0 and ("login" in text or "sso" in text):
                self.login()
            else:
                self.report(res, f"Could not switch to profile {name}")

        self.in_background(f"Switching to {name}…", work, done)

    def add_profile(self) -> None:
        name = run("kdialog", "--title", APP_NAME, "--inputbox",
                   "Name of the new NetBird profile (e.g. home, work):", timeout=600)
        name = name.stdout.strip()
        if name and not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            error("Use only letters, digits, '.', '_' and '-' in the profile name.")
            return
        if not name:
            return
        current = ((self.status or {}).get("management") or {}).get("url", "") or "https://"
        url = run("kdialog", "--title", APP_NAME, "--inputbox",
                  "Management server URL for this profile:", current.replace(":443", ""), timeout=600).stdout.strip()
        if not url:
            return
        res = run("netbird", "profile", "add", name)
        if res.returncode != 0:
            error(f"Could not add profile {name}:\n{output(res)}")
            return
        if self.connected:
            run("netbird", "down")
        run("netbird", "profile", "select", name)
        self.login("--management-url", url, "--admin-url", url)

    def extend_session(self) -> None:
        # `up` przy aktywnym połączeniu kończy się "Already connected" bez logowania; `login --extend`
        # przedłuża sesję SSO bez zrywania tunelu (nowy termin wg ustawienia w panelu).
        self.login(cmd=("netbird", "login", "--extend", "--no-browser"))

    def login(self, *extra: str, cmd: tuple[str, ...] = ("netbird", "up", "--no-browser")) -> None:
        if self.login_proc and self.login_proc.poll() is None:
            return
        # --no-browser: CLI sam też próbuje otworzyć przeglądarkę - otwieramy ją raz, sami.
        self.login_proc = subprocess.Popen(
            [*cmd, *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        threading.Thread(target=self._watch_login, args=(self.login_proc,), daemon=True).start()
        self.busy = "Waiting for login in the browser…"
        self.force_refresh()

    def _watch_login(self, proc: subprocess.Popen) -> None:
        out = []
        opened = False
        for line in proc.stdout:
            out.append(line)
            m = re.search(r"https://\S+/oauth2/auth\S*|https://\S+(device|login|authorize)\S*", line)
            if m and not opened:
                opened = True  # przeglądarka tylko raz na logowanie
                idle_once(webbrowser.open, m.group(0))
        proc.wait()

        def finish() -> None:
            self.busy = ""
            if proc.returncode != 0:
                error("Login failed:\n" + NOISE.sub("", "".join(out[-15:])).strip())
            else:
                try:
                    status = json.loads(run("netbird", "status", "--json").stdout)
                except ValueError:
                    status = {}
                expires = until_text(status.get("sessionExpiresAt") or "")
                notify("NetBird: logged in", f"Session expires {expires}." if expires else "")
            self.force_refresh()

        idle_once(finish)

    def about(self) -> None:
        ver = run("netbird", "version").stdout.strip()
        s = self.status or {}
        text = (
            f"{APP_NAME} {APP_VERSION}\n"
            "A small NetBird tray client in the style of the Windows app.\n\n"
            f"NetBird: {ver or '?'}\n"
            f"Management: {(s.get('management') or {}).get('url', '-')}\n"
            f"Profile: {self.profile or '-'}\n"
            f"Service: {self.unit}\n"
            f"State: {self.state or self.error or '-'}"
        )
        subprocess.Popen(["kdialog", "--title", f"About {APP_NAME}", "--icon", "netbird-tray", "--msgbox", text])


def single_instance_lock(unit: str):
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/netbird-tray-{os.getuid()}"
    os.makedirs(runtime, exist_ok=True)
    lock = open(os.path.join(runtime, f"netbird-tray-{instance_name(unit)}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def main() -> None:
    # Bez argumentu: po jednej ikonie na każdą instancję demona (osobne procesy - każdy z własnym
    # AppIndicatorem i blokadą); z argumentem netbird@<iface>.service: tylko ta instancja.
    if len(sys.argv) > 1:
        unit = sys.argv[1]
    else:
        units = all_units() or [service_unit()]
        for extra in units[1:]:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), extra], start_new_session=True)
        unit = units[0]
    lock = single_instance_lock(unit)
    if lock is None:
        if len(sys.argv) <= 1:
            notify("NetBird Tray is already running", "The icon is in the system tray.")
        return
    GLib.set_prgname("netbird-tray")
    NetBirdTray(unit)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()


if __name__ == "__main__":
    main()
