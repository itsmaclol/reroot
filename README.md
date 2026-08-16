# KernelSU Re-Root Tool (Pixel GKI)

Automates re-rooting a Pixel phone after a beta OTA wipes the root. Swaps the stock GKI kernel inside `boot.img` for a KernelSU+SUSFS kernel, then flashes it.

## Files

| File | Purpose |
|------|---------|
| `resolvers.py` | Shared config, helpers, and URL resolvers. Imported by both scripts. |
| `test_links.py` | Phase 1 — resolve and reachability-check all three URLs. Run this first. |
| `reroot.py` | Phase 2 — full pipeline: download, extract, repack, flash. |

## Requirements

- Python 3.9+
- Android Platform Tools (`adb`, `fastboot`) on your PATH
- `magiskboot` is downloaded automatically; or supply `--magiskboot PATH`
- No third-party Python packages needed

## Configuration

All configurable variables are at the top of `resolvers.py`:

```python
DEVICE_CODENAME  = "cheetah"    # Pixel 7 Pro. Change for other devices.
ANDROID_MAJOR    = "17"         # Android major version (not the GKI branch).
BETA_TRACK       = None         # Set to "qpr1", "qpr2", etc., or None to prompt.
WORKDIR          = "./reroot_work"

WILDKERNELS_REPO = "WildKernels/GKI_KernelSU_SUSFS"
MAGISKBOOT_REPO  = "svoboda18/magiskboot"
```

Set `GITHUB_TOKEN` in your environment to avoid GitHub API rate limits:
```
set GITHUB_TOKEN=ghp_yourtoken   # Windows
export GITHUB_TOKEN=ghp_yourtoken  # Linux/macOS
```

## Usage

### Phase 1 — Validate URLs

```
python test_links.py
```

Resolves and reachability-checks:
1. Google factory image ZIP for your device/beta
2. WildKernels AnyKernel3 ZIP (KernelSU+SUSFS)
3. magiskboot ZIP (svoboda18)

Also finds the recommended KernelSU manager from the WildKernels release notes and offers to download it.

Confirm Phase 1 output before proceeding.

### Phase 2 — Full pipeline

```
python reroot.py
```

Or with options:

```
python reroot.py --dry-run                 # everything except flashing
python reroot.py --workdir D:\reroot       # custom working directory
python reroot.py --adb C:\adb\adb.exe     # explicit adb path
python reroot.py --fastboot C:\adb\fastboot.exe
python reroot.py --magiskboot .\magiskboot.exe  # skip auto-download
```

### Pipeline steps

1. **Resolve** — scrapes the Google download page for the factory ZIP URL; queries GitHub API for WildKernels and magiskboot releases
2. **Download** — downloads all three artifacts with progress bar and resume support; skips if a valid cached file already exists
3. **Extract** — unpacks the factory ZIP (zip-in-zip), locates `boot.img` (aborts if not found rather than silently substituting `init_boot.img` or `vendor_boot.img`), extracts the `Image` from AnyKernel3
4. **Repack** — `magiskboot unpack` → swap `kernel` with `Image` → `magiskboot repack` → sanity-check size
5. **Verify** — connects via adb, prints device props, warns on codename or build mismatch
6. **Flash** — reboots to bootloader, checks bootloader is unlocked, flashes `new-boot.img`, optionally reboots

### After flash

Open the KernelSU Manager app — it should show the device as rooted.

## Safety notes

- The tool never flashes without an explicit final confirmation.
- It will not unlock the bootloader for you (unlocking wipes data).
- `boot.img` must match the OS build currently installed on the phone. The tool warns loudly if there is a mismatch.
- All intermediate files are preserved in `WORKDIR` on failure so you can inspect what went wrong.
- `--dry-run` stops before any `fastboot` commands.
