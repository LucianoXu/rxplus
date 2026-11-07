#!/usr/bin/env bash
# Re-vendor the reacting/1 reference checker and corpus from the Reacting repository.
#   tests/conformance/update.sh <path-to-Reacting-checkout> <commit>
# Copies conformance/{check.py,README.md,valid,invalid} at <commit> and rewrites PIN.
set -euo pipefail
repo=$1
commit=$(git -C "$repo" rev-parse "$2")
here=$(cd "$(dirname "$0")" && pwd)
rm -rf "$here/valid" "$here/invalid" "$here/check.py" "$here/README.md"
git -C "$repo" archive "$commit" conformance/check.py conformance/README.md \
    conformance/valid conformance/invalid | tar -x -C "$here" --strip-components=1
{
    echo "commit $commit"
    (cd "$here" && find check.py README.md valid invalid -type f | LC_ALL=C sort \
        | xargs sha256sum)
} > "$here/PIN"
echo "vendored $commit"
