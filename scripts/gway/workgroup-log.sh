#!/usr/bin/env bash

readonly WORKGROUP_INTERACTION_LOG="${WORKGROUP_INTERACTION_LOG:-/home/arthe/logs/usb-interactions.log}"
readonly WORKGROUP_INTERACTION_LOCK="${WORKGROUP_INTERACTION_LOCK:-/home/arthe/logs/usb-interactions.lock}"

workgroup_log_name() {
    if [[ -n "${WORKGROUP_NAME:-}" ]]; then
        printf '%s\n' "$WORKGROUP_NAME"
        return 0
    fi

    if [[ -n "${USB_WORKGROUP_NAME:-}" ]]; then
        printf '%s\n' "$USB_WORKGROUP_NAME"
        return 0
    fi

    if [[ -n "${CODEX_WORKGROUP_NAME:-}" ]]; then
        printf '%s\n' "$CODEX_WORKGROUP_NAME"
        return 0
    fi

    printf '%s\n' "unknown"
}

workgroup_log_kv() {
    local key="$1"
    local value="${2:-}"
    printf ' %s=%q' "$key" "$value"
}

workgroup_log_event() {
    local tool="$1"
    local phase="$2"
    local action="$3"
    local detail="${4:-}"
    local status="${5:-}"
    local ts user_name

    mkdir -p "$(dirname "$WORKGROUP_INTERACTION_LOG")"
    printf -v ts '%(%Y-%m-%dT%H:%M:%S%z)T' -1
    user_name="${USER:-$(id -un)}"

    if command -v flock >/dev/null 2>&1; then
        {
            flock 9
            {
                printf '%s' "$ts"
                workgroup_log_kv tool "$tool"
                workgroup_log_kv phase "$phase"
                workgroup_log_kv action "$action"
                workgroup_log_kv workgroup "$(workgroup_log_name)"
                workgroup_log_kv user "$user_name"
                workgroup_log_kv pid "$$"
                workgroup_log_kv cwd "$PWD"
                if [[ -n "$detail" ]]; then
                    workgroup_log_kv detail "$detail"
                fi
                if [[ -n "$status" ]]; then
                    workgroup_log_kv status "$status"
                fi
                printf '\n'
            } >>"$WORKGROUP_INTERACTION_LOG"
        } 9>>"$WORKGROUP_INTERACTION_LOCK"
        return 0
    fi

    {
        printf '%s' "$ts"
        workgroup_log_kv tool "$tool"
        workgroup_log_kv phase "$phase"
        workgroup_log_kv action "$action"
        workgroup_log_kv workgroup "$(workgroup_log_name)"
        workgroup_log_kv user "$user_name"
        workgroup_log_kv pid "$$"
        workgroup_log_kv cwd "$PWD"
        if [[ -n "$detail" ]]; then
            workgroup_log_kv detail "$detail"
        fi
        if [[ -n "$status" ]]; then
            workgroup_log_kv status "$status"
        fi
        printf '\n'
    } >>"$WORKGROUP_INTERACTION_LOG"
}
