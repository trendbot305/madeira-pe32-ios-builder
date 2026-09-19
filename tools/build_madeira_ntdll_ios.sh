#!/bin/bash
set -euo pipefail

MADEIRA_ROOT="${1:-external/Madeira}"
MADEIRA_ROOT="$(cd "$MADEIRA_ROOT" && pwd)"
WINE_SRC="$MADEIRA_ROOT/wine"
WINE_BUILD="$WINE_SRC/build-macos"
NTDLL_DIR="$MADEIRA_ROOT/build/ntdll-unix"
GNUTLS_BUILD="$MADEIRA_ROOT/build/gnutls-ios"
FREETYPE_SRC="$MADEIRA_ROOT/research/freetype"
LLVM_MINGW_DIR="$MADEIRA_ROOT/toolchains/llvm-mingw-20260421-ucrt-macos-universal"

test -f "$WINE_BUILD/include/config.h"
test -f "$NTDLL_DIR/build.sh"
test -f "$GNUTLS_BUILD/build.sh"
test -x "$LLVM_MINGW_DIR/bin/aarch64-w64-mingw32-clang"

echo "=== Ensuring modern Bison for widl ==="
BISON_MAJOR="$(bison --version 2>/dev/null | head -1 | sed -E 's/.* ([0-9]+)\..*/\1/' || true)"
if [ -z "$BISON_MAJOR" ] || [ "$BISON_MAJOR" -lt 3 ]; then
  if ! brew list bison >/dev/null 2>&1; then
    brew install bison
  fi
  export PATH="$(brew --prefix bison)/bin:$PATH"
fi
bison --version | head -1
BISON_MAJOR="$(bison --version | head -1 | sed -E 's/.* ([0-9]+)\..*/\1/')"
test "$BISON_MAJOR" -ge 3

export PATH="$LLVM_MINGW_DIR/bin:$PATH"

echo "=== Preparing DirectWrite generated headers ==="
(
  cd "$WINE_BUILD"
  make -j2 include/dwrite.h include/dwrite_1.h include/dwrite_2.h include/dwrite_3.h
)
test -f "$WINE_BUILD/include/dwrite.h"
test -f "$WINE_BUILD/include/dwrite_3.h"

# Madeira's ntdll iOS script names build-arm64ec/include because that was
# the original local build tree containing widl-generated DWrite headers.
# For CI the same pinned Wine revision generated them in build-macos.
mkdir -p "$WINE_SRC/build-arm64ec"
rm -rf "$WINE_SRC/build-arm64ec/include"
ln -s ../build-macos/include "$WINE_SRC/build-arm64ec/include"

echo "=== Preparing FreeType headers ==="
if [ ! -d "$FREETYPE_SRC/include/freetype" ]; then
  rm -rf "$FREETYPE_SRC"
  git clone --depth 1 --branch VER-2-13-3 \
    https://github.com/freetype/freetype.git "$FREETYPE_SRC"
fi
test -f "$FREETYPE_SRC/include/ft2build.h"

echo "=== Building pinned GnuTLS static stack for iOS ==="
bash "$GNUTLS_BUILD/build.sh"
test -f "$MADEIRA_ROOT/toolchains/gnutls-ios/lib/libgnutls.a"
test -f "$MADEIRA_ROOT/toolchains/gnutls-ios/include/gnutls/gnutls.h"

echo "=== Building Madeira native ntdll unix archive ==="
bash "$NTDLL_DIR/build.sh"

test -s "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a"
file "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a"
echo "Built libntdll_unix.a: $(wc -c < "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a" | tr -d ' ') bytes"
