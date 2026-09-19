# Madeira PE32 iOS Builder

A public GitHub Actions pipeline that builds an **unsigned iPhoneOS IPA** from open-source components:

- [Madeira](https://github.com/willfaust/Madeira), pinned to a known commit
- Madeira's FEX-based ARM64 WoW64 runtime
- the minimal 32-bit and ARM64 Wine modules needed for the WoW64 path
- a tiny, synthetic CRT-free PE32 executable used only to test the pipeline

## What this repository does not contain

This repository contains **no Dishonored files, game binaries, cracks, DRM bypasses, account data, or private compatibility reports**. It is an open build/test harness for a synthetic PE32 program.

## Build

Open **Actions → Build unsigned Madeira PE32 IPA → Run workflow**.

The workflow uses standard GitHub-hosted Linux, Windows, and macOS runners. GitHub documents standard hosted runners as free for public repositories. It uploads an unsigned IPA artifact retained for seven days.

The IPA still needs to be signed with your own Apple identity before installation on an iPhone. A free Apple ID can be used for personal-device development signing, with Apple's normal expiration and capability limits.

## Reproducibility

Third-party source revisions and downloaded toolchains are pinned, and downloaded archives are SHA-256 verified. Upstream projects remain governed by their own licenses. This repository does not redistribute the game or third-party source trees.
