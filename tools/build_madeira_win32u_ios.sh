#!/bin/bash
set -euo pipefail

MADEIRA_ROOT="${1:-external/Madeira}"
MADEIRA_ROOT="$(cd "$MADEIRA_ROOT" && pwd)"
WINE_SRC="$MADEIRA_ROOT/wine"
WINE_BUILD="$WINE_SRC/build-macos"
WIN32U_DIR="$MADEIRA_ROOT/build/win32u-unix"
FREETYPE_DIR="$MADEIRA_ROOT/build/freetype-ios"
FREETYPE_SRC="$MADEIRA_ROOT/research/freetype"
OUT="$MADEIRA_ROOT/app/Madeira/libwin32u_unix.a"

test -d "$WINE_SRC"
test -f "$WINE_BUILD/include/config.h"
test -f "$WIN32U_DIR/build.sh"
test -f "$FREETYPE_DIR/build.sh"
test -f "$WIN32U_DIR/config_ios.h"
test -f "$MADEIRA_ROOT/build/ntdll-unix/shims/wine_ios_exit.h"

echo "=== Verify Wine generated-header prerequisites ==="
required_headers=(
  config.h winuser.h wingdi.h winternl.h ntstatus.h
  wtypesbase.h wtypes.h unknwn.h objidlbase.h objidl.h
)
for hdr in "${required_headers[@]}"; do
  if [ ! -f "$WINE_BUILD/include/$hdr" ] && [ ! -f "$WINE_SRC/include/$hdr" ]; then
    echo "ERROR: missing Wine header: $hdr"
    exit 1
  fi
done

echo "=== Ensure win32u Wine generated-header closure ==="
WIDL="$WINE_BUILD/tools/widl/widl"
if [ ! -x "$WIDL" ]; then
  echo "ERROR: cached Wine widl generator is missing: $WIDL"
  exit 1
fi

WIDL_HEADERS=(
  servprov urlmon ocidl docobj
  shtypes structuredquerycondition comcat propsys objectarray shobjidl_core shobjidl
  dxgi d3dcommon d3d10 d3d11 d3d12
  d3d11sdklayers d3d12sdklayers d3d11on12 d3d12shader d3d12video
  exdisp shldisp
)
for hdr in "${WIDL_HEADERS[@]}"; do
  src="$WINE_SRC/include/$hdr.idl"
  out="$WINE_BUILD/include/$hdr.h"
  if [ -f "$src" ] && [ ! -f "$out" ]; then
    echo "widl: $hdr.idl -> $hdr.h"
    "$WIDL" -h -o "$out" \
      -I"$WINE_SRC/include" -I"$WINE_BUILD/include" \
      "$src"
  fi
  test -f "$out"
done

echo "=== Ensure FreeType 2.13.3 source ==="
if [ ! -f "$FREETYPE_SRC/include/ft2build.h" ]; then
  rm -rf "$FREETYPE_SRC"
  git clone --depth 1 --branch VER-2-13-3 \
    https://github.com/freetype/freetype.git "$FREETYPE_SRC"
fi
test -f "$FREETYPE_SRC/include/ft2build.h"

echo "=== Build/reuse FreeType for iOS arm64 ==="
if [ ! -s "$FREETYPE_DIR/build/libfreetype.a" ]; then
  bash "$FREETYPE_DIR/build.sh"
else
  echo "Reusing existing libfreetype.a"
fi
test -s "$FREETYPE_DIR/build/libfreetype.a"
file "$FREETYPE_DIR/build/libfreetype.a"
xcrun lipo -info "$FREETYPE_DIR/build/libfreetype.a" || true

echo "=== Build Madeira win32u unix library ==="
rm -rf "$WIN32U_DIR/obj"
mkdir -p "$WIN32U_DIR/obj"

set +e
bash "$WIN32U_DIR/build.sh"
status=$?
set -e

if [ "$status" -ne 0 ]; then
  echo ""
  echo "=== win32u compile diagnostics ==="
  found=0
  for err in "$WIN32U_DIR"/obj/*.err; do
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
  exit "$status"
fi

test -s "$OUT"
echo "=== Verify libwin32u_unix.a ==="
file "$OUT"
xcrun lipo -info "$OUT"
members="$(xcrun ar -t "$OUT" | wc -l | tr -d ' ')"
echo "archive members: $members"
test "$members" -gt 10

echo "=== Verify expected win32u entry points ==="
xcrun nm -g "$OUT" > /tmp/win32u-nm.txt
grep -q '_win32u_unix_lib_init' /tmp/win32u-nm.txt
grep -q '_winios_drv_post_mouse' /tmp/win32u-nm.txt
grep -q '_winios_drv_post_key' /tmp/win32u-nm.txt

echo "Built libwin32u_unix.a: $(wc -c < "$OUT" | tr -d ' ') bytes"
