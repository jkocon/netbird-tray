# netbird-tray

A **NetBird** icon for the Linux system tray, modelled on the Windows client. The official
`netbird-ui` on Linux is a separate Fyne window; this is just the tray menu: connect, switch
profiles, see peers, select networks and exit nodes. Works with self-hosted NetBird and with
several daemons side by side.

> **Polish documentation:** [README.pl.md](README.pl.md)

Sibling projects with the same look and behaviour:
[tailscale-tray](https://github.com/jkocon/tailscale-tray) and
[twingate-tray](https://github.com/jkocon/twingate-tray).

---

## Features

The menu opens on a left **or** right click:

| Menu entry | What it does |
|---|---|
| **Header** | *NetBird – &lt;management server&gt;*, so several icons can be told apart. |
| **Status** | *Connected* / *Disconnected – click to connect* (`netbird up` / `down`). *Logged out* / *Session expired – click to log in…* starts SSO login and opens the browser once. Warns when the management or signal server is unreachable. Starts the service if it is not running. |
| **Profile** (submenu) | All NetBird profiles – click one to switch (disconnect, `netbird profile select`, reconnect; logs in if the profile needs it). Session expiry, *Log in again (extend session)…* (`netbird login --extend`, keeps the tunnel up), *Add another profile…* (name + management URL), *Admin console* (dashboard at the management server). |
| **This device** | Name, NetBird IP and WireGuard mode (kernel/userspace) – click to copy the IP. |
| **Network devices** | Every peer: connected/idle, P2P or relayed, latency – click to copy its IP. |
| **Networks** | Select/deselect networks and resources (`netbird networks select -a` / `deselect`), *Select all (incl. future ones)*. |
| **Exit nodes** | Routes for `0.0.0.0/0` / `::/0` as a list to choose from, or *None*. |
| **Settings** | *Allow SSH server on this device* (`up --allow-server-ssh`, reconnects), *Start with the system* (enable the service), *Restart NetBird service*, *Service log*. |
| **About**, **Exit** | Version, management URL, profile and service; quit the tray. |

### Tray icon

| Icon | State |
|---|---|
| bright logo | connected |
| grey logo | disconnected |
| translucent logo | connecting, or connected but the management server is unreachable |
| green arrow | connected, traffic goes through an exit node |
| orange "!" | login required (logged out or session expired) |

---

## Requirements

- Linux desktop with a **StatusNotifierItem** tray: KDE Plasma works out of the box; GNOME needs the
  *AppIndicator and KStatusNotifierItem Support* extension.
- **NetBird** client with its daemon running as `netbird.service` (from `netbird service install`) or
  `netbird@<interface>.service` (AUR package `netbird-bin`).
- polkit, and membership in the `wheel` group to start/restart the service without a password.
- Runtime helpers: `kdialog`, `notify-send` (libnotify), `xdg-open` (xdg-utils), `wl-copy`
  (wl-clipboard) – or `xclip`/`xsel` on X11 – and a terminal (`konsole`, `alacritty`, `kitty` or `xterm`).
- To build: Rust 1.85+ (`cargo`).

`install.sh` targets Arch-based systems (CachyOS, Arch, EndeavourOS): it installs the runtime
helpers with `pacman`. On other distributions build with `cargo build --release` and copy the
files listed below by hand.

---

## Installation

```bash
git clone https://github.com/jkocon/netbird-tray.git
cd netbird-tray
sudo ./install.sh
```

`install.sh` (run as root; exit code 10 = NetBird is not installed, nothing done):

1. installs the runtime helpers with `pacman`,
2. builds the binary **as your normal user** with `build.sh` (cargo never runs as root),
3. installs `/usr/local/lib/netbird-tray/netbird-tray` and its state icons,
4. adds autostart (`/etc/xdg/autostart/netbird-tray.desktop`), a menu entry and the app icon,
5. installs the polkit rule `/etc/polkit-1/rules.d/49-netbird-tray.rules`: users in `wheel`, in an
   active local session, may start/stop/restart the NetBird service without a password. Enabling
   autostart still asks for a password; connect/disconnect need no rule at all (the daemon socket is
   world-writable),
6. for `netbird@<iface>` units writes `NB_DAEMON_ADDR` to `/etc/environment.d/50-netbird.conf`, so the
   `netbird` CLI in a terminal finds the daemon socket too (the tray finds it by itself).

The tray starts at the next login, or run `/usr/local/lib/netbird-tray/netbird-tray` now.

### Command line

```
netbird-tray                          one icon per enabled or running NetBird daemon
netbird-tray netbird@wt1.service      only this daemon
netbird-tray --dump [unit]            query the daemon once and print the menu as text (no icon)
netbird-tray --version
```

---

## How it works

- State comes from the `netbird` CLI – `status --json`, `networks list` and `profile list` – every
  3 seconds in a background thread; actions also use the CLI (`up`, `down`, `networks`, `profile`,
  `login`). The daemon socket is mode 0666, so none of this needs root.
- Service actions (start, restart, enable/disable autostart) go through `pkexec systemctl`.
- **Unit and socket detection:** `netbird.service` listens on `/var/run/netbird.sock`; each
  `netbird@<iface>.service` instance has its own `/var/run/netbird/<iface>.sock`. The tray picks the
  socket that belongs to its unit and passes it to the CLI as `NB_DAEMON_ADDR`.
- **Several daemons:** without an argument the tray starts one process – one icon, one
  single-instance lock – per enabled or running instance (for example a private `netbird@wt0` and
  a company `netbird@wt1`).
- SSO login runs `netbird up --no-browser` and opens the login URL from its output **once**.
- Icon and menu use **ksni** (StatusNotifierItem + DBusMenu over D-Bus, no GTK).

### Running two NetBird daemons

A second instance needs its own state and must not clean up the routes of the first one. Drop-in
`/etc/systemd/system/netbird@wt1.service.d/10-instance.conf`:

```ini
[Service]
StateDirectory=netbird-wt1
Environment=NB_NFTABLES_TABLE=netbird-wt1
Environment=NB_FWMARK_BASE=0x1BE00
Environment=NB_DISABLE_SSH_CONFIG=true
Environment=NB_USE_LEGACY_ROUTING=true
```

plus `NB_STATE_DIR` and `NB_DNS_STATE_FILE` pointing into that instance's own state directory
(`/var/lib/netbird-wt1`) instead of the default one.

`NB_USE_LEGACY_ROUTING` matters: otherwise both instances share routing table 7120 and `down` on one
removes the routes of the other. For **all** instances add
`/etc/systemd/system/netbird@.service.d/10-shared-runtime.conf` with `RuntimeDirectoryPreserve=yes` –
otherwise restarting one instance deletes `/run/netbird` together with the other one's socket.

First connection of the second instance:

```bash
NB_DAEMON_ADDR=unix:///var/run/netbird/wt1.sock netbird up \
    --management-url https://vpn.example.com --interface-name wt1 --wireguard-port 51821
```

For switching between networks one at a time, NetBird **profiles** (one daemon, *Profile* submenu)
are simpler than a second daemon.

---

## Building and testing

```bash
cargo build --release
cargo test
cargo run -- --dump        # menu for the current NetBird state, without a tray icon
```

### Layout

```
src/main.rs        tray state, menu, actions, polling, unit detection
src/parse.rs       parsing of CLI output, URLs, session expiry
src/common/        shared with tailscale-tray and twingate-tray: commands with timeouts, kdialog,
                   notifications, clipboard, icons, single-instance lock, poller, ksni menu helpers
icons/             state icons (on, disconnected, connecting, exit, login)
build.sh           builds as a regular user, used by install.sh
install.sh         system installation (Arch-based)
49-netbird-tray.rules   polkit rule
```

`src/common/` is the same copy in all three tray repositories – apply a fix there to all of them.

---

## License

MIT – see [LICENSE](LICENSE).
