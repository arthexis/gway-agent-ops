#!/usr/bin/env bash

readonly OPERATOR_INTERACTION_LOG="${OPERATOR_INTERACTION_LOG:-/home/arthe/logs/usb-interactions.log}"
readonly OPERATOR_INTERACTION_LOCK="${OPERATOR_INTERACTION_LOCK:-/home/arthe/logs/usb-interactions.lock}"

operator_log_name() {
    if [[ -n "${OPERATOR_NAME:-}" ]]; then
        printf '%s\n' "$OPERATOR_NAME"
        return 0
    fi

    if [[ -n "${USB_OPERATOR_NAME:-}" ]]; then
        printf '%s\n' "$USB_OPERATOR_NAME"
        return 0
    fi

    if [[ -n "${CODEX_OPERATOR_NAME:-}" ]]; then
        printf '%s\n' "$CODEX_OPERATOR_NAME"
        return 0
    fi

    printf '%s\n' "unknown"
}

operator_log_kv() {
    local key="$1"
    local value="${2:-}"
    printf ' %s=%q' "$key" "$value"
}

operator_log_event() {
    local tool="$1"
    local phase="$2"
    local action="$3"
    local detail="${4:-}"
    local status="${5:-}"
    local ts user_name

    mkdir -p "$(dirname "$OPERATOR_INTERACTION_LOG")"
    printf -v ts '%(%Y-%m-%dT%H:%M:%S%z)T' -1
    user_name="${USER:-$(id -un)}"

    if command -v flock >/dev/null 2>&1; then
        {
            flock 9
            {
                printf '%s' "$ts"
                operator_log_kv tool "$tool"
                operator_log_kv phase "$phase"
                operator_log_kv action "$action"
                operator_log_kv operator "$(operator_log_name)"
                operator_log_kv user "$user_name"
                operator_log_kv pid "$$"
                operator_log_kv cwd "$PWD"
                if [[ -n "$detail" ]]; then
                    operator_log_kv detail "$detail"
                fi
                if [[ -n "$status" ]]; then
                    operator_log_kv status "$status"
                fi
                printf '\n'
            } >>"$OPERATOR_INTERACTION_LOG"
        } 9>>"$OPERATOR_INTERACTION_LOCK"
        return 0
    fi

    {
        printf '%s' "$ts"
        operator_log_kv tool "$tool"
        operator_log_kv phase "$phase"
        operator_log_kv action "$action"
        operator_log_kv operator "$(operator_log_name)"
        operator_log_kv user "$user_name"
        operator_log_kv pid "$$"
        operator_log_kv cwd "$PWD"
        if [[ -n "$detail" ]]; then
            operator_log_kv detail "$detail"
        fi
        if [[ -n "$status" ]]; then
            operator_log_kv status "$status"
        fi
        printf '\n'
    } >>"$OPERATOR_INTERACTION_LOG"
}
