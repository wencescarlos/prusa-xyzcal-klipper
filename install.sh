#!/usr/bin/env bash
# Instala (o desinstala) prusa_xyzcal en Klipper mediante un enlace simbólico.
#
#   ./install.sh                 instala (por defecto Klipper en ~/klipper)
#   ./install.sh -k /ruta/klipper
#   ./install.sh -u              desinstala
#   ./install.sh -r              instala y reinicia el servicio klipper
#
# Moonraker ejecuta este script al actualizar, sin terminal: por eso el
# reinicio de Klipper lo hace Moonraker (managed_services: klipper) y no el
# script, salvo que lo pidas con -r.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${KLIPPER_DIR:-$HOME/klipper}"
UNINSTALL=0
RESTART=0
MODULE="prusa_xyzcal.py"

usage() {
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while getopts "k:urh" opt; do
    case "$opt" in
        k) KLIPPER_DIR="$OPTARG" ;;
        u) UNINSTALL=1 ;;
        r) RESTART=1 ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

EXTRAS="$KLIPPER_DIR/klippy/extras"
TARGET="$EXTRAS/$MODULE"
EXCLUDE="$KLIPPER_DIR/.git/info/exclude"
EXCLUDE_LINE="klippy/extras/$MODULE"

if [ ! -d "$EXTRAS" ]; then
    echo "No encuentro $EXTRAS. Indica la ruta de Klipper con -k /ruta/klipper" >&2
    exit 1
fi

if [ "$UNINSTALL" -eq 1 ]; then
    rm -f "$TARGET"
    if [ -f "$EXCLUDE" ]; then
        grep -vxF "$EXCLUDE_LINE" "$EXCLUDE" > "$EXCLUDE.tmp" || true
        mv "$EXCLUDE.tmp" "$EXCLUDE"
    fi
    echo "Desinstalado. Reinicia Klipper (FIRMWARE_RESTART)."
    exit 0
fi

if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    mv "$TARGET" "$TARGET.bak"
    echo "Había un $MODULE copiado a mano; guardado como $MODULE.bak"
fi
ln -sfn "$REPO_DIR/$MODULE" "$TARGET"
echo "Enlace creado: $TARGET -> $REPO_DIR/$MODULE"

# Evita que Moonraker marque el repositorio de Klipper como "sucio".
if [ -d "$KLIPPER_DIR/.git" ]; then
    mkdir -p "$(dirname "$EXCLUDE")"
    touch "$EXCLUDE"
    grep -qxF "$EXCLUDE_LINE" "$EXCLUDE" || echo "$EXCLUDE_LINE" >> "$EXCLUDE"
fi

if [ "$RESTART" -eq 1 ]; then
    sudo systemctl restart klipper
    echo "Klipper reiniciado."
else
    echo "Listo. Reinicia Klipper (FIRMWARE_RESTART) para cargar el módulo."
fi
