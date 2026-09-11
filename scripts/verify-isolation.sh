#!/usr/bin/env bash
# Isolation gate. The tunnel does not go public until this passes.
# Untested isolation is not isolation.
set -uo pipefail

fail=0
check() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "FAIL  $desc (succeeded, should have been blocked)"
    fail=1
  else
    echo "ok    $desc (blocked)"
  fi
}
expect() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "ok    $desc"
  else
    echo "FAIL  $desc"
    fail=1
  fi
}

GATEWAY=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')

echo "container cannot reach the LAN"
check "app -> router ($GATEWAY)" docker compose exec -T app curl -s --max-time 3 "http://$GATEWAY"
check "app -> 192.168.1.1" docker compose exec -T app curl -s --max-time 3 http://192.168.1.1
check "app -> 10.0.0.1" docker compose exec -T app curl -s --max-time 3 http://10.0.0.1

echo
echo "llama has no internet at all"
check "llama -> example.com" docker compose exec -T llama curl -s --max-time 3 https://example.com

echo
echo "app reaches only allowlisted domains"
check "app -> evil.example" docker compose exec -T app curl -s --max-time 3 https://evil.example

echo
echo "container privileges"
expect "app runs as non-root" sh -c 'test "$(docker compose exec -T app id -u)" != "0"'
check "app root filesystem is read-only" docker compose exec -T app touch /probe

echo
if [ "$fail" -eq 0 ]; then
  echo "PASS. Isolation verified. Tunnel may go public."
else
  echo "FAILED. Do not expose this. Fix the above first."
  exit 1
fi
