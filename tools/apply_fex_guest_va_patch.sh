#!/usr/bin/env bash
set -euo pipefail

ROOT="${1:-external/Madeira/FEX}"
PATCH="${2:-integration/patches/fex-a04b0241-darwin-guest-memory-bias-stage3.patch}"
BASE="a04b0241c2fe3911729842205cd8643981108aad"

git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "FEX checkout missing: $ROOT" >&2; exit 1; }
test -s "$PATCH" || { echo "FEX guest-VA patch missing: $PATCH" >&2; exit 1; }

# The Madeira fork is a direct descendant of the patch's audited base. Fetching
# that one object lets git perform a real three-way merge instead of a fuzzy
# context-only apply against a fast-moving iOS fork.
git -C "$ROOT" fetch --no-tags --depth=1 origin "$BASE"
git -C "$ROOT" apply --3way --whitespace=error-all "$(cd "$(dirname "$PATCH")" && pwd)/$(basename "$PATCH")"

# Fail closed if any of the contract pieces did not land.
grep -q "SetGuestMemoryAddressBias" "$ROOT/FEXCore/include/FEXCore/Core/Context.h"
grep -q "TranslateGuestMemoryAddress" "$ROOT/FEXCore/Source/Interface/Context/Context.cpp"
grep -q "ApplyGuestMemoryAddressBias" "$ROOT/FEXCore/Source/Interface/Core/JIT/MemoryOps.cpp"
grep -q "GUEST_MEMORY_ADDRESS_REGION_CAPACITY" "$ROOT/FEXCore/include/FEXCore/Core/Context.h"

if git -C "$ROOT" diff --check; then
  echo "FEX guest-VA translation patch applied cleanly."
else
  echo "FEX guest-VA translation patch introduced whitespace errors." >&2
  exit 1
fi
