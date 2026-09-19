#!/bin/bash
set -euo pipefail

MADEIRA_ROOT="${1:-external/Madeira}"
MADEIRA_ROOT="$(cd "$MADEIRA_ROOT" && pwd)"
OUT_ROOT="$MADEIRA_ROOT/toolchains/llvm-ios-build"
OUT_LIB="$OUT_ROOT/lib"
LLVM_VERSION="15.0.7"
LLVM_TAG="llvmorg-15.0.7"
SRC_ARCHIVE="/tmp/llvm-project-${LLVM_VERSION}.src.tar.xz"
SRC_ROOT="/tmp/llvm-project-${LLVM_VERSION}.src"
BUILD_ROOT="/tmp/llvm-ios-build"

mkdir -p "$MADEIRA_ROOT/toolchains"

if [ -s "$OUT_LIB/libLLVMCore.a" ]; then
  echo "Reusing cached LLVM ${LLVM_VERSION} iOS static libraries."
  file "$OUT_LIB/libLLVMCore.a"
  exit 0
fi

echo "=== Install host build tools ==="
brew list cmake >/dev/null 2>&1 || brew install cmake
brew list ninja >/dev/null 2>&1 || brew install ninja
brew list llvm@15 >/dev/null 2>&1 || brew install llvm@15

HOST_LLVM="$(brew --prefix llvm@15)"
HOST_TBLGEN="$HOST_LLVM/bin/llvm-tblgen"
test -x "$HOST_TBLGEN"
"$HOST_TBLGEN" --version | head -2

echo "=== Fetch LLVM ${LLVM_VERSION} source ==="
curl --fail --location --retry 3   "https://github.com/llvm/llvm-project/releases/download/${LLVM_TAG}/llvm-project-${LLVM_VERSION}.src.tar.xz"   -o "$SRC_ARCHIVE"

rm -rf "$SRC_ROOT" "$BUILD_ROOT"
tar -xJf "$SRC_ARCHIVE" -C /tmp
test -f "$SRC_ROOT/llvm/CMakeLists.txt"

echo "=== Patch LLVM Apple linker platform detection for iOS ==="
python3 - "$SRC_ROOT/llvm/cmake/modules/AddLLVM.cmake" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
old = 'MATCHES "Darwin"'
new = 'MATCHES "Darwin|iOS"'
if old in s:
    s = s.replace(old, new)
elif new not in s:
    raise SystemExit("Could not find expected Darwin linker check in AddLLVM.cmake")
p.write_text(s)
print("Patched AddLLVM.cmake for iOS dead_strip handling")
PY

SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
CLANG="$(xcrun --sdk iphoneos --find clang)"
CLANGXX="$(xcrun --sdk iphoneos --find clang++)"

echo "=== Configure LLVM ${LLVM_VERSION} static libraries for iOS arm64 ==="
cmake -S "$SRC_ROOT/llvm" -B "$BUILD_ROOT" -G Ninja   -DCMAKE_SYSTEM_NAME=iOS   -DCMAKE_OSX_SYSROOT="$SDK"   -DCMAKE_OSX_ARCHITECTURES=arm64   -DCMAKE_OSX_DEPLOYMENT_TARGET=18.0   -DCMAKE_BUILD_TYPE=Release   -DCMAKE_C_COMPILER="$CLANG"   -DCMAKE_CXX_COMPILER="$CLANGXX"   -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY   -DLLVM_TABLEGEN="$HOST_TBLGEN"   -DLLVM_TARGETS_TO_BUILD=AArch64   -DLLVM_BUILD_UTILS=OFF   -DLLVM_INCLUDE_UTILS=OFF   -DLLVM_BUILD_TOOLS=OFF   -DLLVM_INCLUDE_TOOLS=OFF   -DLLVM_INCLUDE_TESTS=OFF   -DLLVM_INCLUDE_EXAMPLES=OFF   -DLLVM_INCLUDE_BENCHMARKS=OFF   -DLLVM_ENABLE_BINDINGS=OFF   -DLLVM_ENABLE_TERMINFO=OFF   -DLLVM_ENABLE_LIBXML2=OFF   -DLLVM_ENABLE_ZLIB=OFF   -DLLVM_ENABLE_ZSTD=OFF   -DLLVM_ENABLE_LIBEDIT=OFF   -DLLVM_ENABLE_BACKTRACES=OFF   -DLLVM_ENABLE_PIC=ON   -DBUILD_SHARED_LIBS=OFF

echo "=== Build LLVM iOS static libraries ==="
cmake --build "$BUILD_ROOT" --parallel 3

echo "=== Collect LLVM iOS archives ==="
rm -rf "$OUT_ROOT"
mkdir -p "$OUT_LIB"
find "$BUILD_ROOT/lib" -maxdepth 1 -type f -name 'libLLVM*.a' -exec cp {} "$OUT_LIB/" \;

count="$(find "$OUT_LIB" -maxdepth 1 -type f -name 'libLLVM*.a' | wc -l | tr -d ' ')"
echo "Collected LLVM static archives: $count"
test "$count" -gt 10
test -s "$OUT_LIB/libLLVMCore.a"
test -s "$OUT_LIB/libLLVMSupport.a"

echo "=== Verify an LLVM archive member is iOS arm64 ==="
VERIFY_DIR="/tmp/llvm-ios-verify"
rm -rf "$VERIFY_DIR"
mkdir -p "$VERIFY_DIR"
member="$(xcrun ar -t "$OUT_LIB/libLLVMCore.a" | head -1)"
(
  cd "$VERIFY_DIR"
  xcrun ar -x "$OUT_LIB/libLLVMCore.a" "$member"
  file "$member"
  xcrun vtool -show-build "$member" || true
)

echo "LLVM iOS libraries ready at: $OUT_LIB"
