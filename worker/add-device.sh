#!/bin/bash
# Add a child's computer to your relay and print its agent token.
#   ./add-device.sh <id> "<name>"      e.g.  ./add-device.sh an "An"
# Then on that computer:  Linux  sudo ./install.sh <relay-url> <token> <child-account>
#                         Windows .\install.ps1 -Url <relay-url> -Token <token> -Kid <child-account>
set -euo pipefail
cd "$(dirname "$0")"
ID=${1:-}; NAME=${2:-}
[[ $ID =~ ^[a-z0-9-]{1,20}$ ]] && [ -n "$NAME" ] || { echo "usage: $0 <id: a-z 0-9 -> \"<name>\"" >&2; exit 1; }
DB=$(grep -m1 '^database_name' wrangler.toml | cut -d'"' -f2)
TOKEN=$(node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))")
HASH=$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)
npx wrangler d1 execute "$DB" --remote --command \
  "INSERT INTO devices(id,name,token_hash,created_at) VALUES('$ID','${NAME//\'/\'\'}','$HASH',$(date +%s)000)"
echo
echo "Agent token for $NAME (keep it secret, it is shown only once):"
echo "$TOKEN"
