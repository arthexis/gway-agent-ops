#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
revision=${ROOT##*/}
case "$revision" in *[!0-9a-f]*) exit 2;; esac
[ "${#revision}" -eq 40 ] || exit 2
[ "$(git -C "$ROOT" rev-parse --verify HEAD)" = "$revision" ] || exit 2
[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=all)" ] || exit 2
[ "$(id -u)" -ne 0 ] || exit 2
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
exec /bin/sh "$ROOT/deploy.sh" --expected-sha "$revision"
