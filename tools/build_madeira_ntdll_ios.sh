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

test -d "$WINE_SRC"
test -f "$WINE_SRC/configure"
test -f "$NTDLL_DIR/build.sh"
test -f "$GNUTLS_BUILD/build.sh"

NEED_WINE_GEN=false
for hdr in config.h wtypesbase.h wtypes.h unknwn.h objidlbase.h objidl.h oaidl.h propidl.h dcommon.h d2d1.h d2d1_1.h d2d1_2.h d2d1_3.h dwrite.h dwrite_1.h dwrite_2.h dwrite_3.h; do
  test -f "$WINE_BUILD/include/$hdr" || NEED_WINE_GEN=true
done

if [ "$NEED_WINE_GEN" = true ] && [ ! -x "$WINE_BUILD/tools/widl/widl" ]; then
  echo "=== Ensuring modern Bison for Wine/widl bootstrap ==="
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
elif [ "$NEED_WINE_GEN" = true ]; then
  echo "=== Reusing cached Wine widl generator ==="
fi

echo "=== Ensuring pinned llvm-mingw PE toolchain ==="
if [ ! -x "$LLVM_MINGW_DIR/bin/aarch64-w64-mingw32-clang" ]; then
  mkdir -p "$MADEIRA_ROOT/toolchains"
  ARCHIVE="/tmp/llvm-mingw-20260421-ucrt-macos-universal.tar.xz"
  curl -L --fail --retry 3 \
    "https://github.com/mstorsjo/llvm-mingw/releases/download/20260421/llvm-mingw-20260421-ucrt-macos-universal.tar.xz" \
    -o "$ARCHIVE"
  echo "bd85a3975723815cef28dbbd2ca2cb0c926f6b348a12a0453f39f7af273cb3f7  $ARCHIVE" | shasum -a 256 -c -
  tar -xJf "$ARCHIVE" -C "$MADEIRA_ROOT/toolchains"
fi
test -x "$LLVM_MINGW_DIR/bin/aarch64-w64-mingw32-clang"
export PATH="$LLVM_MINGW_DIR/bin:$PATH"

if [ "$NEED_WINE_GEN" = true ]; then
  echo "=== Preparing Wine generated host tree ==="
  mkdir -p "$WINE_BUILD"
  if [ ! -f "$WINE_BUILD/include/config.h" ]; then
    (
      cd "$WINE_BUILD"
      ../configure
    )
  fi
  test -f "$WINE_BUILD/include/config.h"

  echo "=== Preparing COM + Direct2D + DirectWrite generated headers ==="
  (
    cd "$WINE_BUILD"
    make -j2 \
      include/wtypesbase.h \
      include/wtypes.h \
      include/unknwn.h \
      include/objidlbase.h \
      include/objidl.h \
      include/oaidl.h \
      include/propidl.h \
      include/dcommon.h \
      include/d2d1.h \
      include/d2d1_1.h \
      include/d2d1_2.h \
      include/d2d1_3.h \
      include/dwrite.h \
      include/dwrite_1.h \
      include/dwrite_2.h \
      include/dwrite_3.h
  )
else
  echo "=== Reusing cached Wine generated headers ==="
fi

for hdr in wtypesbase.h wtypes.h unknwn.h objidlbase.h objidl.h oaidl.h propidl.h dcommon.h d2d1.h d2d1_1.h d2d1_2.h d2d1_3.h dwrite.h dwrite_1.h dwrite_2.h dwrite_3.h; do
  test -f "$WINE_BUILD/include/$hdr"
done

# Madeira's ntdll iOS script expects these generated headers at
# wine/build-arm64ec/include. Reuse the same pinned Wine-generated headers.
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

echo "=== Ensuring pinned GnuTLS static stack for iOS ==="
if [ ! -s "$MADEIRA_ROOT/toolchains/gnutls-ios/lib/libgnutls.a" ]; then
  bash "$GNUTLS_BUILD/build.sh"
fi
test -f "$MADEIRA_ROOT/toolchains/gnutls-ios/lib/libgnutls.a"
test -f "$MADEIRA_ROOT/toolchains/gnutls-ios/include/gnutls/gnutls.h"

echo "=== Building Madeira native ntdll unix archive ==="
set +e
bash "$NTDLL_DIR/build.sh"
NTDLL_STATUS=$?
set -e
if [ "$NTDLL_STATUS" -ne 0 ]; then
  echo ""
  echo "=== ntdll compile diagnostics ==="
  found=0
  for err in "$NTDLL_DIR"/obj/*.err; do
    [ -f "$err" ] || continue
    if [ -s "$err" ]; then
      found=1
      echo "----- $(basename "$err") -----"
      cat "$err"
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "No non-empty per-object .err files were produced."
  fi
  exit "$NTDLL_STATUS"
fi

test -s "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a"
file "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a"
echo "Built libntdll_unix.a: $(wc -c < "$MADEIRA_ROOT/app/Madeira/libntdll_unix.a" | tr -d ' ') bytes"
