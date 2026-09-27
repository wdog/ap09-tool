#!/usr/bin/env bash
# Install ap09 (CLI) + ap09-gui (GTK app) for the current user.
#
#   ./install.sh               install / upgrade
#   ./install.sh --no-udev     skip the udev rule (you will need sudo to reach the pedal)
#   ./install.sh --uninstall   remove everything this script installed
#
# Copyright (c) 2026 wdog <wdog666@gmail.com> — MIT
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.ap09.Looper"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP="$DATA/applications/$APP_ID.desktop"
ICON="$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
RULE="/etc/udev/rules.d/60-ap09.rules"
APT_PKGS="python3-usb pipx ffmpeg python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gstreamer-1.0 gstreamer1.0-plugins-good python3-numpy"

udev=1
action=install
for a in "$@"; do
    case "$a" in
        --no-udev) udev=0 ;;
        --uninstall) action=uninstall ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $a (try --help)" >&2; exit 2 ;;
    esac
done

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m !\033[0m %s\n' "$*" >&2; }
ask()  { local r; read -r -p "$1 [Y/n] " r </dev/tty || r=n; [[ -z "$r" || "$r" =~ ^[Yy] ]]; }

refresh_caches() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" || true
}

reload_udev() {
    sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usb || true
}

if [[ $action == uninstall ]]; then
    say "removing ap09"
    if command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^ap09-tool '; then
        pipx uninstall ap09-tool
    fi
    rm -f "$DESKTOP" "$ICON"
    refresh_caches
    if [[ -e $RULE ]] && ask "remove udev rule $RULE (needs sudo)?"; then
        sudo rm -f "$RULE" && reload_udev
    fi
    ok "uninstalled (audio cache left in ${XDG_CACHE_HOME:-$HOME/.cache}/ap09)"
    exit 0
fi

# 1. system packages -----------------------------------------------------------
missing=()
python3 -c 'import usb.core' 2>/dev/null || missing+=("python3-usb (pyusb)")
command -v pipx >/dev/null || missing+=("pipx")
command -v ffmpeg >/dev/null || missing+=("ffmpeg (only to upload mp3/flac/…)")
python3 - 2>/dev/null <<'EOF' || missing+=("GTK 4 / libadwaita / GStreamer / numpy (only for the GUI)")
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gtk, Gst  # noqa
import numpy  # noqa
EOF

if ((${#missing[@]})); then
    say "missing: ${missing[*]}"
    if command -v apt-get >/dev/null; then
        if ask "install them with: sudo apt install $APT_PKGS ?"; then
            # shellcheck disable=SC2086
            sudo apt-get install -y $APT_PKGS
        fi
    else
        warn "not a Debian/Ubuntu system: install the equivalents of these packages yourself:"
        warn "  $APT_PKGS"
    fi
fi

command -v pipx >/dev/null || { warn "pipx is required (apt install pipx / dnf install pipx / pacman -S python-pipx)"; exit 1; }

# 2. the program ----------------------------------------------------------------
# --system-site-packages: GTK bindings (PyGObject), numpy and pyusb come from the distro
say "installing ap09 + ap09-gui with pipx"
pipx install --force --system-site-packages "$SRC"
ok "commands: $(command -v ap09 || echo "$HOME/.local/bin/ap09")  ap09-gui"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "$HOME/.local/bin is not in PATH: run 'pipx ensurepath' and open a new terminal" ;;
esac

# 3. menu entry + icon ------------------------------------------------------------
install -Dm644 "$SRC/data/$APP_ID.desktop" "$DESKTOP"
install -Dm644 "$SRC/data/icons/$APP_ID.svg" "$ICON"
refresh_caches
ok "app menu entry: AP-09 Looper"

# 4. udev rule (use the pedal without sudo) -----------------------------------------
if ((udev)); then
    if [[ -e $RULE ]] && cmp -s "$SRC/data/60-ap09.rules" "$RULE"; then
        ok "udev rule already installed"
    elif ask "install udev rule $RULE so the pedal works without sudo (needs sudo)?"; then
        sudo install -Dm644 "$SRC/data/60-ap09.rules" "$RULE"
        reload_udev
        ok "udev rule installed: unplug and replug the pedal"
    else
        warn "no udev rule: run ap09 with sudo"
    fi
fi

echo
ok "done. Plug in the pedal, then run:  ap09 info   or open 'AP-09 Looper' from the app menu"
