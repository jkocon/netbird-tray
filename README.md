# netbird-tray

Prosta ikona NetBird w zasobniku w stylu klienta na Windows – odpowiednik `tailscale-tray` i `twingate-tray`
(oficjalny `netbird-ui` to osobne okno Fyne). Menu otwiera się lewym lub prawym kliknięciem:

- status: klik łączy (`netbird up`) albo rozłącza (`netbird down`); przy „Logged out / Session expired”
  klik otwiera logowanie SSO w przeglądarce (raz – `up --no-browser`, URL otwiera tray),
- profil: lista profili NetBird do przełączania (`netbird profile select`, z ponownym połączeniem),
  termin wygaśnięcia sesji, „Log in again…”, „Add another profile…” (nazwa + adres serwera zarządzającego)
  i „Admin console” (dashboard pod adresem serwera zarządzającego),
- to urządzenie (FQDN, IP NetBird, tryb WireGuard: kernel/userspace – klik kopiuje IP),
- urządzenia w sieci: połączone/bezczynne, P2P albo Relayed, opóźnienie; klik kopiuje IP,
- Networks: zaznaczanie/odznaczanie sieci i zasobów (`netbird networks select -a / deselect`), „Select all”,
- Exit nodes: trasy 0.0.0.0/0 jako lista do wyboru,
- Settings: SSH server na tym urządzeniu (`up --allow-server-ssh`, wymaga ponownego połączenia),
  Start with the system (`systemctl enable` usługi, przez pkexec), Restart NetBird service, Service log,
- About, Exit.

Ikona w zasobniku (sygnet NetBird z github.com/netbirdio/netbird): jasna = połączony, szara = rozłączony,
półprzezroczysta = łączy się albo serwer zarządzający jest nieosiągalny, zielona strzałka = ruch przez exit node,
pomarańczowy „!” = trzeba się zalogować.

Stan czyta z `netbird status --json`, `netbird networks list` i `netbird profile list` (co 3 s, w wątku).
Socket demona ma prawa 0666, więc połącz/rozłącz/sieci nie potrzebują roota. Pakiet AUR `netbird-bin`
uruchamia demona jako `netbird@<iface>.service` z socketem `/var/run/netbird/<iface>.sock` – tray wykrywa
jednostkę i socket sam, a `install.sh` zapisuje `NB_DAEMON_ADDR` w `/etc/environment.d/50-netbird.conf`
dla CLI w terminalu. Reguła polkit `49-netbird-tray.rules` pozwala grupie `wheel` na start/stop/restart
usługi bez hasła; włączanie autostartu nadal pyta o hasło.

Kilka instancji (np. prywatna `netbird@wt0` i firmowa `netbird@wt1`): tray bez argumentu uruchamia
po jednej ikonie na każdą włączoną/działającą instancję; nagłówek menu pokazuje serwer zarządzający
(np. „NetBird – vpn.example.com”). Druga instancja potrzebuje własnego stanu i nie może sprzątać
tras pierwszej – drop-in `/etc/systemd/system/netbird@wt1.service.d/10-instance.conf`:
`StateDirectory=netbird-wt1`, `NB_STATE_DIR`, `NB_DNS_STATE_FILE`, `NB_NFTABLES_TABLE=netbird-wt1`,
`NB_FWMARK_BASE=0x1BE00`, `NB_DISABLE_SSH_CONFIG=true` i `NB_USE_LEGACY_ROUTING=true` (obie instancje
dzieliłyby tablicę routingu 7120 i `down` jednej kasował trasy drugiej). Do tego dla wszystkich instancji
`/etc/systemd/system/netbird@.service.d/10-shared-runtime.conf` z `RuntimeDirectoryPreserve=yes` –
inaczej restart jednej instancji kasuje `/run/netbird` razem z gniazdem drugiej.
Pierwsze połączenie drugiej instancji: `NB_DAEMON_ADDR=unix:///var/run/netbird/wt1.sock netbird up
--management-url https://… --interface-name wt1 --wireguard-port 51821`.

Kod: Rust (`src/`, od wersji 2.0 zamiast `netbird_tray.py` z GTK/AppIndicator). Ikona i menu przez `ksni`
(StatusNotifierItem + DBusMenu, bez GTK; lewy klik otwiera menu), wspólne części w `../tray-common/`.
`install.sh` buduje binarkę jako zwykły użytkownik (`tray-common/build.sh`, cargo z pakietu `rust`).
Podgląd bez ikony: `cargo run -- --dump` wypisuje menu dla bieżącego stanu demona; testy: `cargo test`.

Instalacja: `sudo -A ./install.sh` (do `/usr/local/lib/netbird-tray`, autostart w `/etc/xdg/autostart`).
Na X13 robi to automatycznie `target/apply.sh` (kod 10 = brak `netbird`, pomija).
