#!/bin/bash
set -euo pipefail

MADEIRA_ROOT="${1:-external/Madeira}"
MADEIRA_ROOT="$(cd "$MADEIRA_ROOT" && pwd)"
WINE_SRC="$MADEIRA_ROOT/wine"
WINE_BUILD="$WINE_SRC/build-macos"
BUILD_DIR="$MADEIRA_ROOT/build/wineserver"
OBJ_DIR="$BUILD_DIR/obj"
SDK="$(xcrun --sdk iphoneos --show-sdk-path)"

test -d "$WINE_SRC"
test -f "$WINE_SRC/configure"
test -f "$BUILD_DIR/build.sh"

echo "=== Ensuring modern build tools ==="
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

echo "=== Ensuring pinned llvm-mingw PE toolchain ==="
TOOLCHAINS_DIR="$MADEIRA_ROOT/toolchains"
LLVM_MINGW_DIR="$TOOLCHAINS_DIR/llvm-mingw-20260421-ucrt-macos-universal"
if [ ! -x "$LLVM_MINGW_DIR/bin/aarch64-w64-mingw32-clang" ]; then
  mkdir -p "$TOOLCHAINS_DIR"
  ARCHIVE="/tmp/llvm-mingw-20260421-ucrt-macos-universal.tar.xz"
  curl -L --fail --retry 3 \
    "https://github.com/mstorsjo/llvm-mingw/releases/download/20260421/llvm-mingw-20260421-ucrt-macos-universal.tar.xz" \
    -o "$ARCHIVE"
  echo "bd85a3975723815cef28dbbd2ca2cb0c926f6b348a12a0453f39f7af273cb3f7  $ARCHIVE" | shasum -a 256 -c -
  tar -xJf "$ARCHIVE" -C "$TOOLCHAINS_DIR"
fi
test -x "$LLVM_MINGW_DIR/bin/aarch64-w64-mingw32-clang"
export PATH="$LLVM_MINGW_DIR/bin:$PATH"
aarch64-w64-mingw32-clang --version | head -1
llvm-dlltool --version | head -1 || true
ld.lld --version | head -1 || true

echo "=== Preparing Wine generated headers ==="
mkdir -p "$WINE_BUILD"
if [ ! -f "$WINE_BUILD/include/config.h" ]; then
  (
    cd "$WINE_BUILD"
    ../configure
  )
fi
test -f "$WINE_BUILD/include/config.h"

echo "=== Building fresh iOS wineserver base archive ==="
rm -rf "$OBJ_DIR"
mkdir -p "$OBJ_DIR"

CC_FLAGS=(
  -arch arm64
  -isysroot "$SDK"
  -miphoneos-version-min=17.0
  -O2
  -I"$WINE_SRC/include"
  -I"$WINE_SRC/include/wine"
  -I"$WINE_BUILD/include"
  -I"$BUILD_DIR"
  -I"$WINE_SRC/server"
  -I"$MADEIRA_ROOT/build/ntdll-unix/shims"
  -include "$BUILD_DIR/config_ios.h"
  -include stdarg.h
  -include "$BUILD_DIR/unicode_fix.h"
  -include "$BUILD_DIR/wineserver_ios_kill.h"
  -DBINDIR="/usr/local/bin"
  -DDATADIR="/usr/local/share"
  -D__WINESRC__
  -DWINE_IOS=1
  -Dmain=wineserver_main
  -Wno-implicit-function-declaration
)

# This list is pinned to Wine's server/Makefile.in at the Madeira submodule
# revision used by the workflow. Sources with Madeira iOS replacements are
# selected below; the rest compile directly from the pinned Wine fork.
SOURCES=(
  async.c atom.c change.c class.c clipboard.c completion.c console.c d3dkmt.c
  debugger.c device.c directory.c event.c fd.c file.c handle.c hook.c
  inproc_sync.c mach.c mailslot.c main.c mapping.c mutex.c named_pipe.c object.c
  process.c procfs.c ptrace.c queue.c region.c registry.c request.c semaphore.c
  serial.c signal.c sock.c symlink.c thread.c timer.c token.c trace.c unicode.c
  user.c window.c winstation.c
)

source_for() {
  case "$1" in
    request.c) echo "$BUILD_DIR/request_ios.c" ;;
    main.c) echo "$BUILD_DIR/main_ios.c" ;;
    mach.c) echo "$BUILD_DIR/mach_ios.c" ;;
    unicode.c) echo "$BUILD_DIR/unicode_ios.c" ;;
    fd.c) echo "$BUILD_DIR/fd_ios.c" ;;
    window.c) echo "$BUILD_DIR/window_ios.c" ;;
    mapping.c) echo "$BUILD_DIR/mapping_ios.c" ;;
    queue.c) echo "$BUILD_DIR/queue_ios.c" ;;
    *) echo "$WINE_SRC/server/$1" ;;
  esac
}

for src_name in "${SOURCES[@]}"; do
  src="$(source_for "$src_name")"
  obj="$OBJ_DIR/${src_name%.c}.o"
  err="$OBJ_DIR/${src_name%.c}.err"
  printf "  %-24s " "$src_name"
  if xcrun -sdk iphoneos clang "${CC_FLAGS[@]}" -c "$src" -o "$obj" 2>"$err"; then
    echo "OK"
  else
    echo "FAILED"
    cat "$err"
    exit 1
  fi
done

# Extra Madeira logging object is not part of upstream Wine's source list.
printf "  %-24s " "wine_log_ios.c"
if xcrun -sdk iphoneos clang "${CC_FLAGS[@]}" -c "$BUILD_DIR/wine_log_ios.c" \
    -o "$OBJ_DIR/wine_log_ios.o" 2>"$OBJ_DIR/wine_log_ios.err"; then
  echo "OK"
else
  echo "FAILED"
  cat "$OBJ_DIR/wine_log_ios.err"
  exit 1
fi

xcrun -sdk iphoneos ar rcs "$OBJ_DIR/libwineserver.a" "$OBJ_DIR"/*.o
test -s "$OBJ_DIR/libwineserver.a"

echo "=== Applying Madeira wineserver overrides + collision renames ==="
(
  cd "$BUILD_DIR"
  ./build.sh all
)

test -s "$MADEIRA_ROOT/app/Madeira/libwineserver.a"
file "$MADEIRA_ROOT/app/Madeira/libwineserver.a"
echo "Built libwineserver.a: $(wc -c < "$MADEIRA_ROOT/app/Madeira/libwineserver.a" | tr -d ' ') bytes"
