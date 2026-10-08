#!/usr/bin/env bash
# Instaluje/aktualizuje netbird-tray w systemie. Uruchom jako root (sudo -A ./install.sh).
# Na X13 robi to automatycznie target/apply.sh po każdej zmianie w netbird-tray/ albo tray-common/.
# Działający tray podmienia się dopiero po ponownym zalogowaniu (albo: pkill -x netbird-tray; /usr/local/lib/netbird-tray/netbird-tray &).
# Kod wyjścia 10 = brak NetBird, nic nie zainstalowano.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/netbird-tray
[[ $EUID -eq 0 ]] || { echo "Uruchom przez sudo"; exit 1; }

if ! command -v netbird >/dev/null; then
    echo "netbird-tray: brak netbird - pomijam"
    exit 10
fi

pacman -S --needed --asdeps --noconfirm kdialog wl-clipboard libnotify xdg-utils

# Binarka w Rust (od 2.0; wcześniej netbird_tray.py z GTK/AppIndicator), budowana jako zwykły użytkownik.
BIN=$("$SRC/../tray-common/build.sh" "$SRC")

install -Dm755 "$BIN" "$LIB/netbird-tray"
rm -rf "$LIB/netbird_tray.py" "$LIB/__pycache__"  # wersja w Pythonie
rm -rf "$LIB/icons"  # bez ikon o starych nazwach
install -Dm644 -t "$LIB/icons" "$SRC"/icons/*.svg
install -Dm644 -t /etc/xdg/autostart "$SRC/netbird-tray.desktop"
install -Dm644 "$SRC/netbird-tray-launcher.desktop" /usr/local/share/applications/netbird-tray.desktop
install -Dm644 "$SRC/netbird-tray.svg" /usr/local/share/icons/hicolor/scalable/apps/netbird-tray.svg
gtk-update-icon-cache -qtf /usr/local/share/icons/hicolor 2>/dev/null || true

# Restart/start usługi z traya bez hasła - odpowiednik operatora w Tailscale.
install -Dm644 "$SRC/49-netbird-tray.rules" /etc/polkit-1/rules.d/49-netbird-tray.rules

# CLI netbird domyślnie szuka /var/run/netbird.sock; pakiet netbird-bin uruchamia netbird@<iface>
# z socketem /var/run/netbird/<iface>.sock - zmienna dla terminali w sesji (tray wykrywa socket sam).
unit=$(systemctl list-unit-files --plain --no-legend 'netbird@*.service' | awk '$2=="enabled"{print $1; exit}')
if [[ -n $unit && $unit =~ ^netbird@(.+)\.service$ ]]; then
    install -d /etc/environment.d
    echo "NB_DAEMON_ADDR=unix:///var/run/netbird/${BASH_REMATCH[1]}.sock" > /etc/environment.d/50-netbird.conf
fi
