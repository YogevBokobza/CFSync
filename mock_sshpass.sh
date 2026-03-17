#!/usr/bin/env bash
# mock_sshpass.sh — fake sshpass that intercepts CFSync's SSH calls and fetches
# material_box_info.json from the mock server's HTTP endpoint instead.
#
# Usage: place this on PATH *before* the real sshpass when running CFSync:
#
#   PATH="$PWD:$PATH" uvicorn main:app --reload --host 0.0.0.0 --port 8000
#
# CFSync calls:
#   sshpass -p PASSWORD ssh -o ... root@HOST "cat FILE1 || cat FILE2"
#
# We extract HOST from the "root@HOST" argument and fetch:
#   http://HOST:7125/mock/material_box_info
#
# The script must be named "sshpass" (copy or symlink it):
#   cp mock_sshpass.sh sshpass && chmod +x sshpass

# Parse args: skip -p PASSWORD, find "root@HOST" pattern, ignore the rest.
HOST=""
skip_next=false

for arg in "$@"; do
    if $skip_next; then
        skip_next=false
        continue
    fi
    # -p PASSWORD: skip -p and the next arg (password)
    if [[ "$arg" == "-p" ]]; then
        skip_next=true
        continue
    fi
    # root@HOST
    if [[ "$arg" =~ ^root@(.+)$ ]]; then
        HOST="${BASH_REMATCH[1]}"
        break
    fi
done

if [[ -z "$HOST" ]]; then
    echo "mock_sshpass: could not determine printer host from arguments: $*" >&2
    exit 1
fi

URL="http://${HOST}:7125/mock/material_box_info"
response=$(curl -sf --max-time 5 "$URL" 2>/dev/null)
exit_code=$?

if [[ $exit_code -ne 0 || -z "$response" ]]; then
    echo "mock_sshpass: failed to fetch $URL (curl exit $exit_code)" >&2
    exit 1
fi

echo "$response"
