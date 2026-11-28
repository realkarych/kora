#!/usr/bin/env bash
set -euo pipefail

revision=$(cat tests/db/predecessor_revision.txt)
[[ $revision =~ ^[0-9a-f]{40}$ ]]

if ! type=$(git cat-file -t "$revision" 2>/dev/null) || [[ $type != commit ]]; then
  git fetch --no-tags --depth=1 --no-write-fetch-head https://github.com/realkarych/kora.git "$revision"
fi
test "$(git cat-file -t "$revision")" = commit
