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

Instalacja: `sudo -A ./install.sh` (do `/usr/local/lib/netbird-tray`, autostart w `/etc/xdg/autostart`).
Na X13 robi to automatycznie `target/apply.sh` (kod 10 = brak `netbird`, pomija).
