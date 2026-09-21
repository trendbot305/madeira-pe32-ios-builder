#!/usr/bin/env python3
"""Prepare a pinned Madeira checkout for the synthetic PE32/WoW64 probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

PINNED_MADEIRA = "97e2ce26e6dc9e4a38976f3b5deb9272d64558eb"

WINE_MODULES = {
    ("aarch64-windows", "wow64.dll"): "arm64",
    ("aarch64-windows", "wow64win.dll"): "arm64",
    ("i386-windows", "ntdll.dll"): "x86",
    ("i386-windows", "kernelbase.dll"): "x86",
    ("i386-windows", "kernel32.dll"): "x86",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pe_machine(path: Path) -> str:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise SystemExit(f"Not a PE file: {path}")
    pe_off = int.from_bytes(data[0x3C:0x40], "little")
    if pe_off + 6 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise SystemExit(f"Invalid PE signature: {path}")
    machine = int.from_bytes(data[pe_off + 4:pe_off + 6], "little")
    names = {0x014C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}
    return names.get(machine, f"unknown-0x{machine:04x}")


def git(root: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def patch_launcher(content_view: Path) -> None:
    text = content_view.read_text(encoding="utf-8")
    marker = 'Button("PE32 WoW64 Test")'
    if marker in text:
        required = (
            r'C:\\windows\\syswow64\\madeira-pe32-probe.exe',
            'MADEIRA_FORCE_AARCH64',
            'unsetenv("MADEIRA_USE_ARM64EC")',
        )
        missing = [item for item in required if item not in text]
        if missing:
            raise SystemExit(f"Existing PE32 launcher is stale/incomplete: {missing}")
        return

    anchor = '                Button("Steam Testing") {'
    if anchor not in text:
        raise SystemExit("Could not find Madeira launcher insertion point")

    block = r'''                Button("PE32 WoW64 Test") {
                    logStore.log("Launching 32-bit PE32 WoW64/FEX probe…")
                    setenv("MADEIRA_EXE", "C:\\windows\\syswow64\\madeira-pe32-probe.exe", 1)
                    setenv("MADEIRA_FORCE_AARCH64", "1", 1)
                    unsetenv("MADEIRA_USE_ARM64EC")
                    unsetenv("MADEIRA_ARGS")
                    unsetenv("MADEIRA_DESKTOP")
                    runWineFullSequence()
                }
                .buttonStyle(.borderedProminent)
                .tint(.orange)

'''
    content_view.write_text(text.replace(anchor, block + anchor, 1), encoding="utf-8")


def patch_wine_process_bridge(bridge: Path) -> None:
    text = bridge.read_text(encoding="utf-8")

    old_select = '''        const char *force_ec = getenv("MADEIRA_USE_ARM64EC");
        BOOL use_arm64ec = (force_ec && *force_ec == '1') ||
                           (strstr(madeira_exe, "x64") != NULL) ||
                           (strchr(madeira_exe, '\\\\') != NULL);'''
    new_select = '''        const char *force_ec = getenv("MADEIRA_USE_ARM64EC");
        const char *force_a64 = getenv("MADEIRA_FORCE_AARCH64");
        BOOL use_arm64ec = !(force_a64 && *force_a64 == '1') &&
                           ((force_ec && *force_ec == '1') ||
                            (strstr(madeira_exe, "x64") != NULL) ||
                            (strchr(madeira_exe, '\\\\') != NULL));
        /* One-shot override: do not poison later ARM64EC/x64 launches. */
        if (force_a64 && *force_a64 == '1') unsetenv("MADEIRA_FORCE_AARCH64");'''

    if "MADEIRA_FORCE_AARCH64" not in text:
        if old_select not in text:
            raise SystemExit("Could not patch Madeira architecture selection")
        text = text.replace(old_select, new_select, 1)
    if "const char *force_a64 = getenv(\"MADEIRA_FORCE_AARCH64\");" not in text:
        raise SystemExit("Madeira ARM64 force-mode patch is incomplete")
    if 'unsetenv("MADEIRA_FORCE_AARCH64")' not in text:
        raise SystemExit("Madeira ARM64 force-mode override is not one-shot")

    old_farms = '''                struct { const char *farm; const char *arch; } farms[] = {
                    { "sysx64",  "arm64ec-windows" },
                    { "sysaa64", "aarch64-windows" },
                };'''
    new_farms = '''                struct { const char *farm; const char *arch; } farms[] = {
                    { "sysx64",   "arm64ec-windows" },
                    { "sysaa64",  "aarch64-windows" },
                    { "syswow64", "i386-windows" },
                };'''
    if '{ "syswow64", "i386-windows" }' not in text:
        if old_farms not in text:
            raise SystemExit("Could not patch Madeira SysWOW64 farm")
        text = text.replace(old_farms, new_farms, 1)
    if '{ "syswow64", "i386-windows" }' not in text:
        raise SystemExit("Madeira SysWOW64 farm patch is incomplete")

    # Include every farm entry; upstream only iterates its two original farms.
    text = text.replace("for (int i = 0; i < 2; i++) {", "for (size_t i = 0; i < sizeof(farms) / sizeof(farms[0]); i++) {", 1)
    if "sizeof(farms) / sizeof(farms[0])" not in text:
        raise SystemExit("Could not expand Madeira farm loop for SysWOW64")

    # Verify the PE32 probe is physically reachable through the SysWOW64 farm
    # before Wine enters __wine_main. This catches stale/missing bundle links.
    old_log = '''                    dprintf(STDERR_FILENO, "[WineProc] Farm %s: %d links -> %s\\n",
                            farms[i].farm, farmLinked, farms[i].arch);'''
    new_log = '''                    dprintf(STDERR_FILENO, "[WineProc] Farm %s: %d links -> %s\\n",
                            farms[i].farm, farmLinked, farms[i].arch);
                    if (!strcmp(farms[i].farm, "syswow64")) {
                        NSString *probe = [farmDir stringByAppendingPathComponent:@"madeira-pe32-probe.exe"];
                        BOOL isDir = NO;
                        BOOL exists = [fm fileExistsAtPath:probe isDirectory:&isDir] && !isDir;
                        dprintf(STDERR_FILENO, "[WineProc] SysWOW64 probe path: %s exists=%d\\n",
                                probe.UTF8String, exists ? 1 : 0);
                    }'''
    if "SysWOW64 probe path:" not in text:
        if old_log not in text:
            raise SystemExit("Could not add SysWOW64 probe verification")
        text = text.replace(old_log, new_log, 1)

    bridge.write_text(text, encoding="utf-8")

    # v66: WoW64 process parameters must live below the 2GB ceiling. Madeira's
    # iOS allocator can inherit a >4GB scan base, producing an inverted window
    # and STATUS_NO_MEMORY before FEX starts. Seed this specific allocation low.
    env_ios = bridge.parents[2] / "build" / "ntdll-unix" / "env_ios.c"
    env_text = env_ios.read_text(encoding="utf-8")
    old_alloc = """    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );"""
    new_alloc = """    /* iOS/WoW64: explicitly seed the search below 2GB. */
    wow64_params = (void *)0x10000;
    status = NtAllocateVirtualMemory( NtCurrentProcess(), (void **)&wow64_params, limit_2g - 1, &size,
                                      MEM_COMMIT, PAGE_READWRITE );"""
    if "iOS/WoW64: explicitly seed the search below 2GB" not in env_text:
        if old_alloc not in env_text:
            raise SystemExit("Could not patch WoW64 low-VA process-parameter allocation")
        env_text = env_text.replace(old_alloc, new_alloc, 1)
        env_ios.write_text(env_text, encoding="utf-8")



def patch_xcode_resources(project: Path) -> None:
    text = project.read_text(encoding="utf-8")
    required_markers = (
        "A10000A0 /* i386-windows in Resources */",
        "A20000A0 /* i386-windows */",
    )
    if all(marker in text for marker in required_markers):
        if text.count("i386-windows in Resources") < 2:
            raise SystemExit("Existing i386 Xcode resource patch is incomplete")
        return
    if any(marker in text for marker in required_markers):
        raise SystemExit("Existing i386 Xcode resource patch is partial")

    replacements = (
        (
            '		A1000080 /* arm64ec-windows in Resources */ = {isa = PBXBuildFile; fileRef = A2000080 /* arm64ec-windows */; };',
            '		A1000080 /* arm64ec-windows in Resources */ = {isa = PBXBuildFile; fileRef = A2000080 /* arm64ec-windows */; };\n'
            '		A10000A0 /* i386-windows in Resources */ = {isa = PBXBuildFile; fileRef = A20000A0 /* i386-windows */; };',
        ),
        (
            '		A2000080 /* arm64ec-windows */ = {isa = PBXFileReference; lastKnownFileType = folder; path = "arm64ec-windows"; sourceTree = "<group>"; };',
            '		A2000080 /* arm64ec-windows */ = {isa = PBXFileReference; lastKnownFileType = folder; path = "arm64ec-windows"; sourceTree = "<group>"; };\n'
            '		A20000A0 /* i386-windows */ = {isa = PBXFileReference; lastKnownFileType = folder; path = "i386-windows"; sourceTree = "<group>"; };',
        ),
        (
            '				A2000080 /* arm64ec-windows */,',
            '				A2000080 /* arm64ec-windows */,\n'
            '				A20000A0 /* i386-windows */,',
        ),
        (
            '				A1000080 /* arm64ec-windows in Resources */,',
            '				A1000080 /* arm64ec-windows in Resources */,\n'
            '				A10000A0 /* i386-windows in Resources */,',
        ),
    )

    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"Could not add i386-windows Xcode resource; missing anchor: {old.strip()}")
        text = text.replace(old, new, 1)

    project.write_text(text, encoding="utf-8")


def copy_wine_modules(source_root: Path, madeira_app: Path) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    for (arch_dir, filename), expected_machine in WINE_MODULES.items():
        src = source_root / arch_dir / filename
        if not src.exists():
            raise SystemExit(f"Required Wine WoW64 module missing: {src}")
        dest_dir = madeira_app / arch_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        shutil.copy2(src, dest)
        actual_machine = pe_machine(dest)
        if actual_machine != expected_machine:
            raise SystemExit(
                f"Wrong PE machine for {arch_dir}/{filename}: "
                f"expected {expected_machine}, got {actual_machine}"
            )
        copied.append(
            {
                "filename": filename,
                "architecture_dir": arch_dir,
                "expected_machine": expected_machine,
                "actual_machine": actual_machine,
                "sha256": sha256(dest),
                "bundle_path": str(dest),
            }
        )
    return copied


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--madeira-root", type=Path, required=True)
    ap.add_argument("--probe-exe", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--wow64fex-dll", type=Path)
    ap.add_argument("--wine-wow64-root", type=Path)
    args = ap.parse_args()

    root = args.madeira_root.resolve()
    exe = args.probe_exe.resolve()
    app = root / "app" / "Madeira"

    content_view = app / "ContentView.swift"
    bridge = app / "WineProcessBridge.m"
    project = root / "app" / "Madeira.xcodeproj" / "project.pbxproj"

    for required in (content_view, bridge, project):
        if not required.exists():
            raise SystemExit(f"Not a complete Madeira checkout; missing {required}")
    if not exe.exists():
        raise SystemExit(f"Probe EXE missing: {exe}")
    probe_machine = pe_machine(exe)
    if probe_machine != "x86":
        raise SystemExit(f"PE32 probe must be x86, got {probe_machine}: {exe}")

    actual = git(root, "rev-parse", "HEAD")
    if actual != PINNED_MADEIRA:
        raise SystemExit(f"Expected Madeira {PINNED_MADEIRA}, got {actual or 'unknown'}")

    if bool(args.wow64fex_dll) != bool(args.wine_wow64_root):
        raise SystemExit(
            "Functional PE32 integration requires both --wow64fex-dll and "
            "--wine-wow64-root; omit both only for patch-layout auditing."
        )

    # These native ARM64 companions are supplied by the pinned Madeira bundle.
    # Newly built wow64/wow64win modules resolve against them at runtime.
    native_companions = (
        app / "aarch64-windows" / "ntdll.dll",
        app / "aarch64-windows" / "win32u.dll",
    )
    missing_companions = [str(p.relative_to(root)) for p in native_companions if not p.exists()]
    if missing_companions:
        raise SystemExit(f"Pinned Madeira native WoW64 companions missing: {missing_companions}")

    preexisting_fex = [str(p.relative_to(root)) for p in app.rglob("libwow64fex.dll")]

    # The PE32 executable belongs to the i386 Windows side. The iOS host and
    # Wine Unix side remain 64-bit ARM64.
    i386_dir = app / "i386-windows"
    i386_dir.mkdir(parents=True, exist_ok=True)
    dest = i386_dir / "madeira-pe32-probe.exe"
    shutil.copy2(exe, dest)

    # Wine's ARM64 WoW64 layer loads xtajit.dll as a native 64-bit module.
    # The FEX WoW64 DLL is ARM64, so keep it with the aarch64 Wine modules,
    # not in the ARM64EC bundle used for x86-64 guests.
    bundled_runtime: list[Path] = []
    if args.wow64fex_dll:
        runtime = args.wow64fex_dll.resolve()
        if not runtime.exists():
            raise SystemExit(f"WoW64 FEX DLL missing: {runtime}")
        runtime_machine = pe_machine(runtime)
        if runtime_machine != "arm64":
            raise SystemExit(
                f"WoW64 FEX runtime must be ARM64 PE, got {runtime_machine}: {runtime}"
            )
        runtime_dir = app / "aarch64-windows"
        for name in ("libwow64fex.dll", "xtajit.dll"):
            runtime_dest = runtime_dir / name
            shutil.copy2(runtime, runtime_dest)
            bundled_runtime.append(runtime_dest)

    wine_modules: list[dict[str, object]] = []
    if args.wine_wow64_root:
        wine_root = args.wine_wow64_root.resolve()
        if not wine_root.exists():
            raise SystemExit(f"Wine WoW64 module root missing: {wine_root}")
        wine_modules = copy_wine_modules(wine_root, app)
        for item in wine_modules:
            item["bundle_path"] = str(Path(str(item["bundle_path"])).relative_to(root))

    patch_launcher(content_view)
    patch_wine_process_bridge(bridge)
    patch_xcode_resources(project)

    wow64_sources = [
        root / "FEX" / "Source" / "Windows" / "WOW64" / "Module.cpp",
        root / "FEX" / "Source" / "Windows" / "WOW64" / "libwow64fex.def",
        root / "wine" / "dlls" / "wow64" / "syscall.c",
        root / "wine" / "dlls" / "wow64win" / "syscall.c",
    ]

    manifest = {
        "schema_version": 2,
        "madeira_commit": actual,
        "probe": {
            "filename": dest.name,
            "sha256": sha256(dest),
            "machine": probe_machine,
            "bundle_path": str(dest.relative_to(root)),
            "expected_success_marker": "MADEIRA_PE32_OK",
            "expected_exit_code": 42,
        },
        "wow64": {
            "source_ready": all(p.exists() for p in wow64_sources),
            "libwow64fex_bundled_before_prepare": bool(preexisting_fex),
            "preexisting_libwow64fex_paths": preexisting_fex,
            "wine_modules": wine_modules,
            "syswow64_bundle_dir": "app/Madeira/i386-windows",
        },
        "runtime": {
            "input": str(args.wow64fex_dll.resolve()) if args.wow64fex_dll else None,
            "sha256": sha256(args.wow64fex_dll.resolve()) if args.wow64fex_dll else None,
            "bundle_paths": [str(p.relative_to(root)) for p in bundled_runtime],
            "wine_cpu_dll_alias": "xtajit.dll" if args.wow64fex_dll else None,
            "runtime_architecture_dir": "aarch64-windows" if args.wow64fex_dll else None,
            "machine": pe_machine(args.wow64fex_dll.resolve()) if args.wow64fex_dll else None,
        },
        "launcher_button": "PE32 WoW64 Test",
        "launch_environment": {
            "MADEIRA_EXE": r"C:\windows\syswow64\madeira-pe32-probe.exe",
            "MADEIRA_FORCE_AARCH64": "1",
            "MADEIRA_USE_ARM64EC": None,
        },
        "patched_files": [
            "app/Madeira/ContentView.swift",
            "app/Madeira/WineProcessBridge.m",
            "app/Madeira.xcodeproj/project.pbxproj",
        ],
    }

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
