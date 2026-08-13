#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
server_dir="$project_dir/tools/bgutil-ytdlp-pot-provider/server"

if [[ ! -f "$server_dir/build/main.js" ]]; then
  echo "PO-token provider is not built at: $server_dir" >&2
  echo "Follow the PO-token setup in README.md, then try again." >&2
  exit 1
fi

exec node "$server_dir/build/main.js"
