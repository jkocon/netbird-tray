//! Prosta ikona NetBird w zasobniku w stylu tailscale-tray (oficjalny klient UI NetBird na Linuksie
//! to osobna aplikacja Fyne z własnym oknem - tu wystarczy menu).
//!
//! Menu (lewy lub prawy klik): status (klik = połącz/rozłącz), profil (przełączanie, dodawanie, panel
//! admina, ważność sesji), to urządzenie, urządzenia w sieci, sieci (Networks / trasy), exit node,
//! ustawienia, About, Exit.
//!
//! Rozmawia z demonem przez CLI `netbird` (status --json, networks, profile, up/down) - socket demona
//! ma prawa 0666, więc nie trzeba roota. Systemctl (autostart, restart usługi) idzie przez pkexec;
//! install.sh dodaje regułę polkit, żeby start/stop/restart nie pytały o hasło.

mod common;
mod parse;

use std::sync::OnceLock;
use std::time::Duration;

use ksni::blocking::Handle;
use regex::Regex;
use serde_json::Value;
use crate::common::cmd::stream_lines;
use crate::common::json::{arr, b, get, n, s};
use crate::common::menu::{button, check, radio, sep, submenu, text, Menu};
use crate::common::{bg, icon_path, open_url, refresh_now, run, unit_active, unit_enabled, App, Out, Poller, POLLER};

use parse::{
    admin_url, clean, daemon_addr, host_of, instance_name, parse_networks, parse_profiles, short_name, strip_prefix,
    until_text, Net, EXIT_PREFIXES,
};

const APP: App = App { name: "NetBird Tray", id: "netbird-tray" };
const POLL: Duration = Duration::from_secs(3);
const NEEDS_LOGIN: [&str; 3] = ["NeedsLogin", "SessionExpired", "LoginFailed"];

static HANDLE: OnceLock<Handle<Tray>> = OnceLock::new();

fn update(f: impl FnOnce(&mut Tray)) {
    if let Some(h) = HANDLE.get() {
        h.update(f);
    }
}

fn netbird(args: &[&str], timeout: u64) -> Out {
    let mut cmd = vec!["netbird"];
    cmd.extend_from_slice(args);
    run(&cmd, timeout)
}

/// Komunikat CLI bez szumu z logów gRPC.
fn output(out: &Out) -> String {
    clean(&format!("{}{}", out.stderr, out.stdout))
}

fn report(out: &Out, what: &str) {
    if !out.ok_or_cancelled() {
        APP.error(&format!("{what}:\n{}", output(out)));
    }
}

// ---------- usługa ----------

fn list_units() -> Vec<(String, String)> {
    let out = run(
        &["systemctl", "list-units", "--all", "--plain", "--no-legend", "netbird.service", "netbird@*.service"],
        30,
    );
    out.stdout
        .lines()
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            Some((f.first()?.to_string(), f.get(2).unwrap_or(&"").to_string()))
        })
        .collect()
}

fn list_unit_files() -> Vec<(String, String)> {
    let out = run(&["systemctl", "list-unit-files", "--plain", "--no-legend", "netbird.service", "netbird@*.service"], 30);
    out.stdout
        .lines()
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            Some((f.first()?.to_string(), f.get(1)?.to_string()))
        })
        .collect()
}

/// Jednostka systemd demona. Pakiet AUR netbird-bin ma szablon netbird@.service (instancja = nazwa
/// interfejsu, np. netbird@wt0), a `netbird service install` tworzy zwykłe netbird.service.
fn service_unit() -> String {
    let units = list_units();
    if let Some((unit, _)) = units.iter().find(|(u, _)| unit_active(u)) {
        return unit.clone(); // najpierw działająca
    }
    if let Some((name, _)) = list_unit_files().into_iter().find(|(n, st)| st == "enabled" && !n.ends_with("@.service")) {
        return name;
    }
    units.first().map(|(u, _)| u.clone()).unwrap_or_else(|| "netbird@wt0.service".into())
}

/// Wszystkie włączone albo działające instancje (np. prywatna netbird@wt0 i firmowa netbird@wt1).
fn all_units() -> Vec<String> {
    let mut units: Vec<String> = list_units().into_iter().filter(|(_, active)| active == "active").map(|(u, _)| u).collect();
    for (name, state) in list_unit_files() {
        if state == "enabled" && !name.ends_with("@.service") {
            units.push(name);
        }
    }
    units.sort();
    units.dedup();
    units
}

struct Icons {
    on: String,
    off: String,
    exit: String,
    auth: String,
    relay: String,
}

struct Tray {
    unit: String,
    status: Option<Value>,
    networks: Vec<Net>,
    profiles: Vec<String>,
    profile: String,
    error: String,
    autostart: bool,
    busy: String,
    login_running: bool,
    icons: Icons,
}

impl Tray {
    fn st(&self) -> &Value {
        static NULL: Value = Value::Null;
        self.status.as_ref().unwrap_or(&NULL)
    }

    fn state(&self) -> &str {
        s(self.st(), "daemonStatus")
    }

    fn connected(&self) -> bool {
        self.state() == "Connected"
    }

    fn needs_login(&self) -> bool {
        NEEDS_LOGIN.contains(&self.state())
    }

    fn exit_node(&self) -> Option<&Net> {
        self.networks.iter().find(|n| EXIT_PREFIXES.contains(&n.network.as_str()) && n.selected)
    }

    fn peers(&self) -> &[Value] {
        arr(get(self.st(), "peers"), "details")
    }

    fn admin_url(&self) -> String {
        admin_url(s(get(self.st(), "management"), "url"))
    }

    fn icon(&self) -> (&str, &str) {
        let mgmt_ok = b(get(self.st(), "management"), "connected");
        if self.needs_login() {
            (&self.icons.auth, "NetBird: login required")
        } else if self.connected() && !mgmt_ok {
            (&self.icons.relay, "NetBird: connected, management server unreachable")
        } else if self.connected() && self.exit_node().is_some() {
            (&self.icons.exit, "NetBird: connected (exit node)")
        } else if self.connected() {
            (&self.icons.on, "NetBird: connected")
        } else if self.state() == "Connecting" || !self.busy.is_empty() {
            (&self.icons.relay, "NetBird: connecting")
        } else {
            (&self.icons.off, "NetBird: disconnected")
        }
    }

    // ---------- menu ----------

    fn status_items(&self) -> Menu<Self> {
        let mut m = Vec::new();
        let state = self.state();
        m.push(if !self.busy.is_empty() {
            text(&self.busy)
        } else if self.error == "service-down" {
            button("NetBird service is not running - click to start", true, Tray::start_service)
        } else if !self.error.is_empty() {
            text(&format!("NetBird: {}", self.error.chars().take(80).collect::<String>()))
        } else if state == "Connected" {
            check("Connected", true, true, |t: &mut Tray, _| t.disconnect())
        } else if self.needs_login() {
            let what = if state == "SessionExpired" { "Session expired" } else { "Logged out" };
            button(&format!("{what} - click to log in…"), true, |t: &mut Tray| t.login(up_cmd(&[])))
        } else if state == "Connecting" {
            text("Connecting…")
        } else {
            check("Disconnected - click to connect", false, true, |t: &mut Tray, _| t.connect())
        });
        if self.connected() {
            let problems: Vec<&str> =
                ["management", "signal"].into_iter().filter(|name| !b(get(self.st(), name), "connected")).collect();
            if !problems.is_empty() {
                let mut what = problems.join(" and ");
                what[..1].make_ascii_uppercase();
                m.push(text(&format!("⚠ {what} server unreachable")));
            }
        }
        m
    }

    fn profiles_menu(&self) -> ksni::MenuItem<Self> {
        let mut names = self.profiles.clone();
        names.sort_by_key(|n| n.to_lowercase());
        let idle = self.busy.is_empty();
        let mut sub: Menu<Self> = Vec::new();
        if !names.is_empty() {
            let selected = names.iter().position(|n| *n == self.profile).unwrap_or(usize::MAX);
            let options = names.iter().map(|n| (n.clone(), idle)).collect();
            sub.push(radio(options, selected, move |t: &mut Tray, i| t.switch_profile(names[i].clone())));
            sub.push(sep());
        }
        let expires = s(self.st(), "sessionExpiresAt");
        if !expires.is_empty() && self.connected() {
            sub.push(text(&format!("Session expires {}", until_text(expires))));
        }
        let ready = self.status.is_some() && idle;
        sub.push(button("Log in again (extend session)…", ready, |t: &mut Tray| {
            // `up` przy aktywnym połączeniu kończy się "Already connected" bez logowania; `login --extend`
            // przedłuża sesję SSO bez zrywania tunelu (nowy termin wg ustawienia w panelu).
            let cmd = if t.connected() { cmd(&["netbird", "login", "--extend", "--no-browser"]) } else { up_cmd(&[]) };
            t.login(cmd);
        }));
        sub.push(button("Add another profile…", ready, |t: &mut Tray| {
            let (mgmt, connected) = (s(get(t.st(), "management"), "url").to_string(), t.connected());
            bg(move || add_profile(&mgmt, connected));
        }));
        let admin = self.admin_url();
        sub.push(button("Admin console", !admin.is_empty(), move |_| open_url(&admin)));
        let label = if self.profile.is_empty() { "Profiles".into() } else { format!("Profile: {}", self.profile) };
        submenu(&label, true, sub)
    }

    fn devices_menu(&self) -> ksni::MenuItem<Self> {
        let mut peers: Vec<&Value> = self.peers().iter().collect();
        let up = |p: &Value| s(p, "status") == "Connected";
        let online = peers.iter().filter(|p| up(p)).count();
        peers.sort_by_key(|p| (!up(p), short_name(s(p, "fqdn")).to_lowercase()));
        let mut sub: Menu<Self> = Vec::new();
        if peers.is_empty() {
            sub.push(text("No devices"));
        }
        for p in &peers {
            let ip = strip_prefix(s(p, "netbirdIp"));
            let how = if up(p) {
                let kind = s(p, "connectionType");
                let latency_ms = n(p, "latency") / 1e6;
                let lat = if latency_ms >= 1.0 { format!(", {latency_ms:.0} ms") } else { String::new() };
                format!("  [{}{lat}]", if kind.is_empty() { "?" } else { kind })
            } else if s(p, "status") == "Idle" {
                "  [idle]".into()
            } else {
                String::new()
            };
            let mark = if up(p) { "●" } else { "○" };
            let label = format!("{mark} {}  {ip}{how}", short_name(s(p, "fqdn")));
            sub.push(button(&label, true, move |_| copy(&ip)));
        }
        sub.push(sep());
        sub.push(text("Click a device to copy its IP"));
        submenu(&format!("Network devices ({online}/{} connected)", peers.len()), self.status.is_some(), sub)
    }

    fn networks_menu(&self) -> ksni::MenuItem<Self> {
        let mut nets: Vec<&Net> = self.networks.iter().filter(|n| !EXIT_PREFIXES.contains(&n.network.as_str())).collect();
        nets.sort_by_key(|n| n.id.to_lowercase());
        let on = nets.iter().filter(|n| n.selected).count();
        let idle = self.busy.is_empty();
        let mut sub: Menu<Self> = Vec::new();
        if nets.is_empty() {
            sub.push(text(if self.connected() { "No networks available" } else { "Connect to see networks" }));
        }
        for net in &nets {
            let target = if net.network.is_empty() { &net.domains } else { &net.network };
            let id = net.id.clone();
            sub.push(check(&format!("{}  ({target})", net.id), net.selected, idle, move |t: &mut Tray, v| {
                t.set_network(id.clone(), v)
            }));
        }
        if !nets.is_empty() {
            sub.push(sep());
            sub.push(button("Select all (incl. future ones)", idle, |t: &mut Tray| {
                t.netbird_bg("Changing networks…", "Could not select networks", &["networks", "select", "all"], 60)
            }));
        }
        let label = if nets.is_empty() { "Networks".into() } else { format!("Networks ({on}/{})", nets.len()) };
        submenu(&label, self.connected(), sub)
    }

    fn exit_nodes_menu(&self) -> ksni::MenuItem<Self> {
        let mut exits: Vec<&Net> = self.networks.iter().filter(|n| EXIT_PREFIXES.contains(&n.network.as_str())).collect();
        exits.sort_by_key(|n| n.id.to_lowercase());
        let current = self.exit_node().map(|n| n.id.clone());
        let idle = self.busy.is_empty();
        let mut options = vec![("None".to_string(), idle)];
        let mut ids = vec![String::new()];
        let mut selected = if current.is_none() { 0 } else { usize::MAX };
        for net in &exits {
            if current.as_ref() == Some(&net.id) {
                selected = ids.len();
            }
            options.push((net.id.clone(), idle));
            ids.push(net.id.clone());
        }
        let mut sub: Menu<Self> = vec![radio(options, selected, move |t: &mut Tray, i| t.set_exit_node(ids[i].clone()))];
        if exits.is_empty() {
            sub.push(text("No exit nodes in this network"));
        }
        let label = current.map(|c| format!("Exit node: {c}")).unwrap_or_else(|| "Exit nodes".into());
        submenu(&label, self.connected(), sub)
    }

    fn settings_menu(&self) -> ksni::MenuItem<Self> {
        let ssh = b(get(self.st(), "sshServer"), "enabled");
        let idle = self.busy.is_empty();
        let unit = self.unit.clone();
        let sub = vec![
            check("Allow SSH server on this device", ssh, self.connected() && idle, |t: &mut Tray, v| t.set_ssh(v)),
            check("Start with the system", self.autostart, true, |t: &mut Tray, v| {
                let unit = t.unit.clone();
                bg(move || set_autostart(&unit, v));
            }),
            sep(),
            button("Restart NetBird service", idle, Tray::restart_service),
            button("Service log", true, move |_| APP.in_terminal(&["journalctl", "-u", &unit, "-n", "200", "-f"])),
        ];
        submenu("Settings", true, sub)
    }

    // ---------- akcje ----------

    /// Długie polecenie (CLI netbird, systemctl) w wątku, żeby menu nie zamarzało;
    /// w tym czasie status w menu pokazuje, co się dzieje.
    fn in_background(
        &mut self,
        busy: &str,
        work: impl FnOnce() -> Out + Send + 'static,
        done: impl FnOnce(Out) + Send + 'static,
    ) {
        if !self.busy.is_empty() {
            return;
        }
        self.busy = busy.into();
        bg(move || {
            let out = work();
            update(|t| t.busy.clear());
            done(out);
            refresh_now();
        });
    }

    fn netbird_bg(&mut self, busy: &str, what: &str, args: &[&str], timeout: u64) {
        let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
        let what = what.to_string();
        self.in_background(
            busy,
            move || netbird(&args.iter().map(String::as_str).collect::<Vec<_>>(), timeout),
            move |out| report(&out, &what),
        );
    }

    fn connect(&mut self) {
        // Bez zapamiętanego logowania `up` poprosi o SSO - wtedy przejmuje to login().
        if self.needs_login() {
            self.login(up_cmd(&[]));
            return;
        }
        self.netbird_bg("Connecting…", "Could not connect", &["up", "--no-browser"], 90);
    }

    fn disconnect(&mut self) {
        self.netbird_bg("Disconnecting…", "Could not disconnect", &["down"], 60);
    }

    fn set_network(&mut self, id: String, selected: bool) {
        let args: Vec<&str> =
            if selected { vec!["networks", "select", "-a", &id] } else { vec!["networks", "deselect", &id] };
        self.netbird_bg("Changing networks…", "Could not change the network selection", &args, 60);
    }

    fn set_exit_node(&mut self, id: String) {
        let current = self.exit_node().map(|n| n.id.clone());
        let work = move || {
            let mut out = Out::default();
            if let Some(cur) = current.filter(|c| *c != id) {
                out = netbird(&["networks", "deselect", &cur], 30);
            }
            if !id.is_empty() {
                out = netbird(&["networks", "select", "-a", &id], 30);
            }
            out
        };
        self.in_background("Changing exit node…", work, |out| report(&out, "Could not change the exit node"));
    }

    fn set_ssh(&mut self, enabled: bool) {
        // Ustawienia `up` działają dopiero po ponownym połączeniu ("Already connected" bez down).
        let work = move || {
            netbird(&["down"], 30);
            netbird(&["up", "--no-browser", &format!("--allow-server-ssh={enabled}")], 90)
        };
        self.in_background("Reconnecting…", work, |out| report(&out, "Could not change the SSH setting"));
    }

    fn start_service(&mut self) {
        let unit = self.unit.clone();
        self.in_background(
            "Starting NetBird service…",
            move || systemctl("start", &unit),
            |out| report(&out, "Could not start the NetBird service"),
        );
    }

    fn restart_service(&mut self) {
        let unit = self.unit.clone();
        self.in_background(
            "Restarting NetBird service…",
            move || systemctl("restart", &unit),
            |out| report(&out, "Could not restart the NetBird service"),
        );
    }

    fn switch_profile(&mut self, name: String) {
        let was_connected = self.connected();
        let busy = format!("Switching to {name}…");
        let n = name.clone();
        let work = move || {
            if was_connected {
                netbird(&["down"], 30);
            }
            let out = netbird(&["profile", "select", &n], 30);
            if out.ok() && was_connected {
                let up = netbird(&["up", "--no-browser"], 90);
                if !up.ok() && output(&up).to_lowercase().contains("login") {
                    return up; // profil wymaga logowania - done() uruchomi login()
                }
            }
            out
        };
        let done = move |out: Out| {
            let text = output(&out).to_lowercase();
            if !out.ok() && (text.contains("login") || text.contains("sso")) {
                update(|t| t.login(up_cmd(&[])));
            } else {
                report(&out, &format!("Could not switch to profile {name}"));
            }
        };
        self.in_background(&busy, work, done);
    }

    /// Logowanie SSO: --no-browser, bo CLI sam też próbuje otworzyć przeglądarkę - otwieramy ją raz, sami.
    fn login(&mut self, cmd: Vec<String>) {
        if self.login_running {
            return;
        }
        self.login_running = true;
        self.busy = "Waiting for login in the browser…".into();
        bg(move || login(cmd));
    }

    fn about(&mut self) {
        let mgmt = s(get(self.st(), "management"), "url").to_string();
        let state = [self.state(), self.error.as_str(), "-"].into_iter().find(|t| !t.is_empty()).unwrap().to_string();
        let (profile, unit) = (self.profile.clone(), self.unit.clone());
        bg(move || {
            let ver = netbird(&["version"], 30).stdout.trim().to_string();
            let text = format!(
                "{} {}\nA small NetBird tray client in the style of the Windows app.\n\n\
                 NetBird: {}\nManagement: {}\nProfile: {}\nService: {unit}\nState: {state}",
                APP.name,
                env!("CARGO_PKG_VERSION"),
                if ver.is_empty() { "?" } else { &ver },
                if mgmt.is_empty() { "-" } else { &mgmt },
                if profile.is_empty() { "-" } else { &profile },
            );
            APP.about(&text);
        });
    }
}

impl ksni::Tray for Tray {
    const MENU_ON_ACTIVATE: bool = true;

    fn id(&self) -> String {
        format!("netbird-tray-{}", instance_name(&self.unit))
    }

    fn title(&self) -> String {
        format!("NetBird ({})", instance_name(&self.unit))
    }

    fn category(&self) -> ksni::Category {
        ksni::Category::SystemServices
    }

    fn icon_name(&self) -> String {
        self.icon().0.into()
    }

    fn tool_tip(&self) -> ksni::ToolTip {
        ksni::ToolTip { title: self.icon().1.into(), ..Default::default() }
    }

    fn menu(&self) -> Vec<ksni::MenuItem<Self>> {
        // Nagłówek: która sieć (przy kilku instancjach ikony wyglądają tak samo).
        let admin = self.admin_url();
        let host = host_of(&admin).unwrap_or_else(|| instance_name(&self.unit));
        let mut m = vec![text(&format!("NetBird – {host}")), sep()];
        m.extend(self.status_items());
        m.extend([sep(), self.profiles_menu(), sep()]);
        let st = self.st();
        if !s(st, "netbirdIp").is_empty() {
            let ip = strip_prefix(s(st, "netbirdIp"));
            let mode = if b(st, "usesKernelInterface") { "kernel" } else { "userspace" };
            let label = format!("This device: {} ({ip})  [{mode} WireGuard]", short_name(s(st, "fqdn")));
            m.push(button(&label, true, move |_| copy(&ip)));
        }
        m.extend([
            self.devices_menu(),
            sep(),
            self.networks_menu(),
            self.exit_nodes_menu(),
            sep(),
            self.settings_menu(),
            button("About", true, Tray::about),
            sep(),
            button("Exit", true, |_| std::process::exit(0)),
        ]);
        m
    }
}

// ---------- akcje w tle ----------

fn cmd(args: &[&str]) -> Vec<String> {
    args.iter().map(|a| a.to_string()).collect()
}

fn up_cmd(extra: &[&str]) -> Vec<String> {
    let mut c = cmd(&["netbird", "up", "--no-browser"]);
    c.extend(extra.iter().map(|a| a.to_string()));
    c
}

fn copy(text: &str) {
    let text = text.to_string();
    bg(move || APP.copy_to_clipboard(&text));
}

fn systemctl(verb: &str, unit: &str) -> Out {
    run(&["pkexec", "systemctl", verb, unit], 120)
}

/// "Start with the system" = usługa włączona w systemd; z autoconnect demon sam się połączy.
fn set_autostart(unit: &str, enabled: bool) {
    let verb = if enabled { "enable" } else { "disable" };
    report(&systemctl(verb, unit), &format!("systemctl {verb} {unit} failed"));
    let now = unit_enabled(unit);
    update(|t| t.autostart = now);
    refresh_now();
}

fn add_profile(mgmt_url: &str, connected: bool) {
    let Some(name) = APP.inputbox("Name of the new NetBird profile (e.g. home, work):", "") else { return };
    if name.is_empty() {
        return;
    }
    if !Regex::new(r"^[A-Za-z0-9_.-]+$").unwrap().is_match(&name) {
        APP.error("Use only letters, digits, '.', '_' and '-' in the profile name.");
        return;
    }
    let current = if mgmt_url.is_empty() { "https://".to_string() } else { mgmt_url.replace(":443", "") };
    let Some(url) = APP.inputbox("Management server URL for this profile:", &current) else { return };
    if url.is_empty() {
        return;
    }
    let out = netbird(&["profile", "add", &name], 30);
    if !out.ok() {
        APP.error(&format!("Could not add profile {name}:\n{}", output(&out)));
        return;
    }
    if connected {
        netbird(&["down"], 30);
    }
    netbird(&["profile", "select", &name], 30);
    update(|t| t.login(up_cmd(&["--management-url", &url, "--admin-url", &url])));
}

fn login(cmd: Vec<String>) {
    let url = Regex::new(r"https://\S+/oauth2/auth\S*|https://\S+(device|login|authorize)\S*").unwrap();
    let mut opened = false;
    let argv: Vec<&str> = cmd.iter().map(String::as_str).collect();
    let (code, lines) = stream_lines(&argv, |line| {
        if let (false, Some(m)) = (opened, url.find(line)) {
            opened = true; // przeglądarka tylko raz na logowanie
            open_url(m.as_str());
        }
    });
    update(|t| {
        t.busy.clear();
        t.login_running = false;
    });
    if code != 0 {
        let tail = lines[lines.len().saturating_sub(15)..].join("\n");
        APP.error(&format!("Login failed:\n{}", clean(&tail)));
    } else {
        let status: Value = serde_json::from_str(&netbird(&["status", "--json"], 30).stdout).unwrap_or(Value::Null);
        let expires = until_text(s(&status, "sessionExpiresAt"));
        let body = if expires.is_empty() { String::new() } else { format!("Session expires {expires}.") };
        APP.notify("NetBird: logged in", &body);
    }
    refresh_now();
}

struct Gathered {
    status: Option<Value>,
    error: String,
    networks: Vec<Net>,
    profiles: Vec<String>,
    profile: String,
}

/// Odpytanie CLI (każde wywołanie to osobny proces).
fn gather(unit: &str) -> Gathered {
    let mut g = Gathered { status: None, error: String::new(), networks: Vec::new(), profiles: Vec::new(), profile: String::new() };
    let out = netbird(&["status", "--json"], 8);
    match serde_json::from_str::<Value>(&out.stdout) {
        Ok(v) if v.is_object() => g.status = Some(v),
        _ => {
            g.error = output(&out);
            if g.error.is_empty() {
                g.error = "NetBird daemon is not responding".into();
            }
            if !unit_active(unit) {
                g.error = "service-down".into();
            }
        }
    }
    if let Some(st) = &g.status {
        if s(st, "daemonStatus") == "Connected" {
            g.networks = parse_networks(&netbird(&["networks", "list"], 8).stdout);
        }
        (g.profiles, g.profile) = parse_profiles(&netbird(&["profile", "list"], 8).stdout);
        if g.profile.is_empty() {
            g.profile = s(st, "profileName").into();
        }
    }
    g
}

fn apply(t: &mut Tray, g: Gathered) {
    (t.status, t.error, t.networks, t.profiles, t.profile) = (g.status, g.error, g.networks, g.profiles, g.profile);
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--version") {
        println!("netbird-tray {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let dump = args.iter().any(|a| a == "--dump");
    let explicit = args.iter().find(|a| !a.starts_with("--")).cloned();
    // Bez argumentu: po jednej ikonie na każdą instancję demona (osobne procesy - każdy z własnym
    // StatusNotifierItem i blokadą); z argumentem netbird@<iface>.service: tylko ta instancja.
    let unit = match &explicit {
        Some(unit) => unit.clone(),
        None => {
            let mut units = all_units();
            if units.is_empty() {
                units.push(service_unit());
            }
            if !dump {
                if let Ok(exe) = std::env::current_exe() {
                    for extra in &units[1..] {
                        let _ = std::process::Command::new(&exe).arg(extra).spawn();
                    }
                }
            }
            units.remove(0)
        }
    };
    // Instancja szablonu ma własny socket - NB_DAEMON_ADDR z sesji wskazuje tylko jedną z nich.
    std::env::set_var("NB_DAEMON_ADDR", daemon_addr(&unit));

    let lock_name = format!("netbird-tray-{}.lock", instance_name(&unit));
    let _lock = match crate::common::single_instance_lock(APP.id, &lock_name) {
        _ if dump => None,
        Ok(Some(file)) => Some(file),
        Ok(None) => {
            if explicit.is_none() {
                APP.notify("NetBird Tray is already running", "The icon is in the system tray.");
            }
            return;
        }
        Err(e) => {
            eprintln!("Cannot create the single-instance lock: {e}");
            std::process::exit(1);
        }
    };
    let icon = |name: &str| icon_path(APP.id, name);
    let mut tray = Tray {
        autostart: unit_enabled(&unit),
        unit: unit.clone(),
        status: None,
        networks: Vec::new(),
        profiles: Vec::new(),
        profile: String::new(),
        error: String::new(),
        busy: String::new(),
        login_running: false,
        icons: Icons {
            on: icon("netbird-tray-on"),
            off: icon("netbird-tray-disconnected"),
            exit: icon("netbird-tray-exit"),
            auth: icon("netbird-tray-auth"),
            relay: icon("netbird-tray-connecting"),
        },
    };
    if dump {
        apply(&mut tray, gather(&unit));
        println!("icon: {}\n{}", ksni::Tray::tool_tip(&tray).title, crate::common::menu::dump(&ksni::Tray::menu(&tray)));
        return;
    }
    match crate::common::spawn_tray(tray) {
        Ok(handle) => {
            let _ = HANDLE.set(handle);
        }
        Err(e) => {
            eprintln!("Cannot create the tray icon: {e}");
            std::process::exit(1);
        }
    }
    let _ = POLLER.set(Poller::start(POLL, move || {
        let g = gather(&unit);
        update(move |t| apply(t, g));
    }));
    crate::common::park_forever();
}
