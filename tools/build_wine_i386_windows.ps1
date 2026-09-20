param(
  [string]$OutDir = "out/wine-wow64",
  [string]$WineCommit = "7817e220384e895651f868ba4d97affcf21b3816"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = (Resolve-Path ".").Path
if ($env:MADEIRA_CACHE_DIR) {
  $cacheRoot = $env:MADEIRA_CACHE_DIR
} elseif ($env:RUNNER_WORKSPACE) {
  $cacheRoot = Join-Path (Split-Path $env:RUNNER_WORKSPACE -Parent) "_madeira-cache"
} else {
  $cacheRoot = Join-Path $env:LOCALAPPDATA "MadeiraIOSCache"
}

$src = Join-Path $cacheRoot "wine-ios"
$toolchainRoot = Join-Path $cacheRoot "llvm-mingw-20260421-ucrt-x86_64"
$toolchainZip = Join-Path $cacheRoot "llvm-mingw-20260421-ucrt-x86_64.zip"
$build = Join-Path $root "wine-wow64-build"
$out = Join-Path $root $OutDir
$runtimeCache = Join-Path $cacheRoot "wine-wow64"

$llvmUrl = "https://github.com/mstorsjo/llvm-mingw/releases/download/20260421/llvm-mingw-20260421-ucrt-x86_64.zip"
$llvmSha256 = "0c47b9fc1043b68a8d7e8e022b878f11306ed35beacf858864b36b096a0acd95"

$parserRoot = Join-Path $cacheRoot "winflexbison-2.5.25"
$parserZip = Join-Path $cacheRoot "win_flex_bison-2.5.25.zip"
$parserUrl = "https://github.com/lexxmark/winflexbison/releases/download/v2.5.25/win_flex_bison-2.5.25.zip"
$parserSha256 = "8d324b62be33604b2c45ad1dd34ab93d722534448f55a16ca7292de32b6ac135"

$msysVersion = "20260611"
$msysRoot = Join-Path $cacheRoot "msys64"
$msysArchive = Join-Path $cacheRoot "msys2-base-x86_64-$msysVersion.sfx.exe"
$msysUrl = "https://github.com/msys2/msys2-installer/releases/download/2026-06-11/msys2-base-x86_64-$msysVersion.sfx.exe"
$msysSha256 = "c105946e64e08f099ac0e4647461ce762b95333ad211777666476a9a41451d65"

New-Item -ItemType Directory -Force $cacheRoot,$out | Out-Null

function Require-Command([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "Required command not found: $Name" }
  return $cmd
}

function Resolve-MakePath {
  $cmd = Get-Command make.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }

  $candidates = @(
    "C:\Program Files (x86)\GnuWin32\bin\make.exe",
    "C:\Program Files\GnuWin32\bin\make.exe"
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) { return $candidate }
  }

  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
  if (-not $winget) { throw "GNU make is missing and winget is unavailable." }

  Write-Host "Installing GNU make with winget..."
  & $winget.Source install --id GnuWin32.Make -e --accept-package-agreements --accept-source-agreements --silent
  if ($LASTEXITCODE -ne 0) { throw "winget could not install GNU make" }

  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) { return $candidate }
  }
  $cmd = Get-Command make.exe -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "GNU make was installed but could not be located" }
  return $cmd.Source
}

Require-Command "git.exe" | Out-Null
$bash = Require-Command "bash.exe"
$python = Require-Command "python.exe"

if (-not (Test-Path (Join-Path $src ".git"))) {
  Write-Host "Cloning pinned Madeira Wine fork into persistent cache..."
  git clone https://github.com/willfaust/wine.git $src
  if ($LASTEXITCODE -ne 0) { throw "Wine clone failed" }
}
git -C $src fetch origin $WineCommit --depth=1
if ($LASTEXITCODE -ne 0) { throw "Wine fetch failed" }
git -C $src checkout --force $WineCommit
if ($LASTEXITCODE -ne 0) { throw "Wine checkout failed" }
git -C $src clean -ffdx
if ($LASTEXITCODE -ne 0) { throw "Wine source clean failed" }

Write-Host "Applying Madeira WoW64 guest-pointer translation..."
& $python.Source (Join-Path $root "tools/patch_wine_wow64_guest_ptrs.py") $src
if ($LASTEXITCODE -ne 0) { throw "Wine WoW64 guest-pointer patch failed" }

$llvm = Join-Path $toolchainRoot "bin"
if (-not (Test-Path (Join-Path $llvm "i686-w64-mingw32-clang.exe"))) {
  if (-not (Test-Path $toolchainZip)) {
    Write-Host "Downloading pinned llvm-mingw toolchain..."
    Invoke-WebRequest -Uri $llvmUrl -OutFile $toolchainZip
  }
  $actual = (Get-FileHash $toolchainZip -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $llvmSha256) {
    throw "llvm-mingw SHA256 mismatch. Expected $llvmSha256, got $actual"
  }
  Write-Host "Extracting llvm-mingw into persistent runner cache..."
  Expand-Archive -Path $toolchainZip -DestinationPath $cacheRoot -Force
}
foreach ($compiler in @(
  "i686-w64-mingw32-clang.exe",
  "aarch64-w64-mingw32-clang.exe",
  "x86_64-w64-mingw32-clang.exe"
)) {
  if (-not (Test-Path (Join-Path $llvm $compiler))) {
    throw "Pinned llvm-mingw toolchain is missing $compiler"
  }
}
Write-Host "Using llvm-mingw: $llvm"

$wb = Get-ChildItem $parserRoot -Recurse -File -Filter "win_bison.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$wf = Get-ChildItem $parserRoot -Recurse -File -Filter "win_flex.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $wf -or -not $wb) {
  if (-not (Test-Path $parserZip)) {
    Write-Host "Downloading pinned WinFlexBison..."
    Invoke-WebRequest -Uri $parserUrl -OutFile $parserZip
  }
  $actual = (Get-FileHash $parserZip -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $parserSha256) {
    throw "WinFlexBison SHA256 mismatch. Expected $parserSha256, got $actual"
  }
  if (Test-Path $parserRoot) { Remove-Item -Recurse -Force $parserRoot }
  New-Item -ItemType Directory -Force $parserRoot | Out-Null
  Expand-Archive -Path $parserZip -DestinationPath $parserRoot -Force
  $wf = Get-ChildItem $parserRoot -Recurse -File -Filter "win_flex.exe" | Select-Object -First 1
  $wb = Get-ChildItem $parserRoot -Recurse -File -Filter "win_bison.exe" | Select-Object -First 1
}
if (-not $wf -or -not $wb) { throw "WinFlexBison archive is incomplete" }

$parserBin = $wb.Directory.FullName
$flexExe = Join-Path $parserBin "flex.exe"
$bisonExe = Join-Path $parserBin "bison.exe"
Copy-Item $wf.FullName $flexExe -Force
Copy-Item $wb.FullName $bisonExe -Force
& $flexExe --version
if ($LASTEXITCODE -ne 0) { throw "flex bootstrap failed" }
& $bisonExe --version
if ($LASTEXITCODE -ne 0) { throw "bison bootstrap failed" }

$probeDir = Join-Path $cacheRoot "parser-smoke"
New-Item -ItemType Directory -Force $probeDir | Out-Null
@"
%define parse.error verbose
%%
empty: ;
"@ | Set-Content (Join-Path $probeDir "conftest.y") -Encoding ascii
Push-Location $probeDir
try {
  & $bisonExe "conftest.y"
  if ($LASTEXITCODE -ne 0) { throw "bison grammar smoke test failed" }
} finally {
  Pop-Location
}
Write-Host "WinFlexBison grammar smoke test passed"

# Wine's host-side makedep must understand POSIX paths generated by
# configure/config.status. A native llvm-mingw makedep.exe cannot open MSYS
# paths such as /tmp/... or /c/..., so run the host build tools inside a
# pinned MSYS2 runtime instead of mixing Git Bash with native Windows GCC.
$msysBash = Join-Path $msysRoot "usr\bin\bash.exe"
if (-not (Test-Path $msysBash)) {
  if (-not (Test-Path $msysArchive)) {
    Write-Host "Downloading pinned MSYS2 base environment..."
    Invoke-WebRequest -Uri $msysUrl -OutFile $msysArchive
  }
  $actual = (Get-FileHash $msysArchive -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $msysSha256) {
    throw "MSYS2 SHA256 mismatch. Expected $msysSha256, got $actual"
  }
  Write-Host "Extracting pinned MSYS2 into persistent runner cache..."
  & $msysArchive -y "-o$cacheRoot"
  if ($LASTEXITCODE -ne 0) { throw "MSYS2 base extraction failed" }
}
if (-not (Test-Path $msysBash)) { throw "MSYS2 bash missing after extraction: $msysBash" }

& $msysBash -lc "true"
if ($LASTEXITCODE -ne 0) { throw "MSYS2 initialization failed" }
& $msysBash -lc "pacman -Sy --needed --noconfirm gcc make flex bison mingw-w64-i686-binutils mingw-w64-x86_64-binutils"
if ($LASTEXITCODE -ne 0) { throw "MSYS2 host/linker tool installation failed" }
& $msysBash -lc "gcc --version && make --version && flex --version && bison --version && /mingw32/bin/ld.exe --version"
if ($LASTEXITCODE -ne 0) { throw "MSYS2 host/linker tools are not runnable" }

# Git for Windows reports x86_64-pc-msys. Wine has PE-only Windows-host logic
# for Cygwin/MinGW but omits MSYS, so patch our cached integration checkout to
# use the same .exe/tooling behavior. This patch is local only.
foreach ($configureFile in @("configure","configure.ac")) {
  $path = Join-Path $src $configureFile
  $text = [System.IO.File]::ReadAllText($path)
  $text = $text.Replace("cygwin*|mingw32*)", "cygwin*|mingw32*|msys*)")
  $text = $text.Replace("mingw32*|cygwin*)", "mingw32*|cygwin*|msys*)")
  $text = $text.Replace("cygwin*|mingw32*|darwin*|linux-android*)", "cygwin*|mingw32*|msys*|darwin*|linux-android*)")
  [System.IO.File]::WriteAllText($path, $text)
}
if (-not ([System.IO.File]::ReadAllText((Join-Path $src "configure")).Contains("mingw32*|cygwin*|msys*)"))) {
  throw "Wine MSYS host patch did not apply"
}

# Madeira's ARM64EC ntdll supplies xlate_ios_jit in signal_arm64ec.c, while
# this fork's shared loader.c also calls it in the separately linked i386
# ntdll. The 32-bit module cannot link against that ARM64EC object. Its safe
# fallback is "no translated pool alias": return the original address, which
# makes the existing pslot != slot guards no-op.
$loaderPath = Join-Path $src "dlls\ntdll\loader.c"
$loaderText = [System.IO.File]::ReadAllText($loaderPath)
$loaderAnchor = "WINE_DECLARE_DEBUG_CHANNEL(imports);"
$loaderShim = @"
$loaderAnchor

#ifdef __i386__
void *xlate_ios_jit( void *ptr )
{
    return ptr;
}
#endif
"@
if (-not $loaderText.Contains($loaderAnchor)) {
  throw "Wine i386 xlate_ios_jit shim anchor was not found"
}
$loaderText = $loaderText.Replace($loaderAnchor, $loaderShim)
[System.IO.File]::WriteAllText($loaderPath, $loaderText)
if (-not ([System.IO.File]::ReadAllText($loaderPath).Contains("void *xlate_ios_jit( void *ptr )"))) {
  throw "Wine i386 xlate_ios_jit shim did not apply"
}

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force $build | Out-Null

Push-Location $build
try {
  Write-Host "Configuring Wine PE multiarch build..."
  $configureArgs = @(
    "--disable-tests",
    "--disable-win16",
    # This auxiliary Windows build produces PE modules only. Host-side media,
    # font, display, device and security libraries belong to Madeira's iOS
    # Unix-side build and must not gate this PE-only module build.
    "--without-alsa",
    "--without-capi",
    "--without-coreaudio",
    "--without-cups",
    "--without-dbus",
    "--without-ffmpeg",
    "--without-fontconfig",
    "--without-freetype",
    "--without-gettext",
    "--without-gnutls",
    "--without-gphoto",
    "--without-gssapi",
    "--without-gstreamer",
    "--without-hwloc",
    "--without-inotify",
    "--without-krb5",
    "--without-netapi",
    "--without-opencl",
    "--without-opengl",
    "--without-oss",
    "--without-pcap",
    "--without-pcsclite",
    "--without-pthread",
    "--without-pulse",
    "--without-sane",
    "--without-sdl",
    "--without-udev",
    "--without-unwind",
    "--without-usb",
    "--without-v4l2",
    "--without-vulkan",
    "--without-wayland",
    "--without-x",
    "--with-mingw=llvm-mingw",
    "--enable-archs=aarch64,i386"
  )
  # Keep configure, makedep and make in one MSYS2 runtime so every POSIX
  # path has identical semantics. /usr/bin comes first for the host compiler;
  # the pinned llvm-mingw directory remains available for PE cross-compilers.
  $srcPosix = (& $msysBash -lc "cygpath -u '$($src.Replace("'","'\\''"))'").Trim()
  $buildPosix = (& $msysBash -lc "cygpath -u '$($build.Replace("'","'\\''"))'").Trim()
  $llvmPosix = (& $msysBash -lc "cygpath -u '$($llvm.Replace("'","'\\''"))'").Trim()
  foreach ($pair in @(@("source",$srcPosix), @("build",$buildPosix), @("llvm",$llvmPosix))) {
    if (-not $pair[1].StartsWith("/")) { throw "Failed to convert Wine $($pair[0]) path to MSYS2 form: $($pair[1])" }
  }
  Write-Host "Wine source (MSYS2): $srcPosix"
  Write-Host "Wine build  (MSYS2): $buildPosix"
  Write-Host "llvm-mingw  (MSYS2): $llvmPosix"

  # llvm-mingw installs target-prefixed ld launchers as POSIX shell scripts.
  # Native clang.exe uses CreateProcess for the linker and Windows rejects those
  # scripts with ERROR_BAD_EXE_FORMAT (0xC1).  Replace only those launchers with
  # the toolchain's own native ld.lld.exe.  Keep a copy of the original wrapper
  # for provenance; all compiler/runtime files remain from the pinned archive.
  $nativeLld = Join-Path $llvm "ld.lld.exe"
  if (-not (Test-Path $nativeLld)) { throw "llvm-mingw native ld.lld.exe missing: $nativeLld" }
  # Invoke the .exe itself for the version check. Calling an extensionless PE
  # launcher from PowerShell can fall through to the user's file association and
  # open a desktop application instead of running the linker.
  & $nativeLld --version
  if ($LASTEXITCODE -ne 0) { throw "Native linker is not runnable: $nativeLld" }
  foreach ($triple in @("i686-w64-mingw32", "aarch64-w64-mingw32")) {
    $launcher = Join-Path $llvm "$triple-ld"
    $backup = "$launcher.posix-wrapper"
    if ((Test-Path $launcher) -and -not (Test-Path $backup)) {
      Copy-Item $launcher $backup -Force
    }
    Copy-Item $nativeLld $launcher -Force
  }

  # Fail here, before the expensive Wine build, unless both cross compilers can
  # perform an actual PE link through the repaired native linker launchers.
  $linkSmoke = Join-Path $build "linker-smoke.c"
  "void mainCRTStartup(void){}" | Set-Content $linkSmoke -Encoding ascii
  foreach ($triple in @("i686-w64-mingw32", "aarch64-w64-mingw32")) {
    $cc = Join-Path $llvm "$triple-clang.exe"
    $exe = Join-Path $build "$triple-link-smoke.exe"
    & $cc $linkSmoke "-nostdlib" "-Wl,--entry,mainCRTStartup" "-o" $exe
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) {
      throw "PE linker smoke test failed for $triple"
    }
  }
  Write-Host "llvm-mingw native linker smoke tests passed"

  foreach ($value in @($srcPosix, $buildPosix, $llvmPosix)) {
    if ($value -match '\\s') { throw "MSYS2 build paths must not contain whitespace: $value" }
  }

  $configureCommand = 'export PATH=/usr/bin:{0}:$PATH; cd {1}; {2}/configure {3}' -f $llvmPosix, $buildPosix, $srcPosix, ($configureArgs -join ' ')
  & $msysBash -lc $configureCommand
  if ($LASTEXITCODE -ne 0) { throw "Wine multiarch configure failed" }

  $targets = @(
    "dlls/ntdll/i386-windows/ntdll.dll",
    "dlls/kernelbase/i386-windows/kernelbase.dll",
    "dlls/kernel32/i386-windows/kernel32.dll",
    "dlls/wow64/aarch64-windows/wow64.dll",
    "dlls/wow64win/aarch64-windows/wow64win.dll"
  )
  Write-Host "Building required PE32/WoW64 modules:"
  $targets | ForEach-Object { Write-Host "  $_" }

  $makeCommand = 'export PATH=/usr/bin:{0}:$PATH; cd {1}; make -j2 V=1 {2}' -f $llvmPosix, $buildPosix, ($targets -join ' ')
  & $msysBash -lc $makeCommand
  if ($LASTEXITCODE -ne 0) { throw "Wine i386/WoW64 module build failed" }
} finally {
  Pop-Location
}

$expected = @(
  @{ rel = "dlls/ntdll/i386-windows/ntdll.dll"; arch = "i386-windows"; machine = "x86"; name = "ntdll.dll" },
  @{ rel = "dlls/kernelbase/i386-windows/kernelbase.dll"; arch = "i386-windows"; machine = "x86"; name = "kernelbase.dll" },
  @{ rel = "dlls/kernel32/i386-windows/kernel32.dll"; arch = "i386-windows"; machine = "x86"; name = "kernel32.dll" },
  @{ rel = "dlls/wow64/aarch64-windows/wow64.dll"; arch = "aarch64-windows"; machine = "arm64"; name = "wow64.dll" },
  @{ rel = "dlls/wow64win/aarch64-windows/wow64win.dll"; arch = "aarch64-windows"; machine = "arm64"; name = "wow64win.dll" }
)

$manifest = @()
foreach ($item in $expected) {
  $source = Join-Path $build $item.rel
  if (-not (Test-Path $source)) { throw "Expected Wine module missing: $($item.rel)" }

  $destDir = Join-Path $out $item.arch
  New-Item -ItemType Directory -Force $destDir | Out-Null
  $dest = Join-Path $destDir $item.name
  Copy-Item $source $dest -Force

  $jsonPath = "$dest.json"
  & $python.Source (Join-Path $root "tools/pe_characterizer.py") $dest -o $jsonPath
  if ($LASTEXITCODE -ne 0) { throw "PE characterization failed: $dest" }
  $report = Get-Content $jsonPath -Raw | ConvertFrom-Json
  if ($report.pe.machine -ne $item.machine) {
    throw "Wrong machine for $($item.name): expected $($item.machine), got $($report.pe.machine)"
  }

  $manifest += [pscustomobject]@{
    name = $item.name
    architecture = $item.arch
    machine = $report.pe.machine
    source = $item.rel
    imports = @($report.imports | ForEach-Object { $_.dll.ToLowerInvariant() } | Sort-Object -Unique)
    sha256 = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLowerInvariant()
    size = (Get-Item $dest).Length
  }
}

$manifestPath = Join-Path $out "manifest.json"
$manifest | ConvertTo-Json -Depth 4 | Set-Content $manifestPath -Encoding utf8

if (Test-Path $runtimeCache) { Remove-Item -Recurse -Force $runtimeCache }
New-Item -ItemType Directory -Force $runtimeCache | Out-Null
Copy-Item (Join-Path $out "*") $runtimeCache -Recurse -Force

Write-Host ""
Write-Host "SUCCESS - Wine PE32/WoW64 module set built and cached"
$manifest | Format-Table -AutoSize
Write-Host "Cache: $runtimeCache"
