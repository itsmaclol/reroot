#!/usr/bin/env python3
"""
reroot.py v2 — KernelSU re-root pipeline for Pixel GKI devices.

Steps:
  1. Resolve URLs  (factory image, WildKernels AnyKernel3, magiskboot)
  2. Get boot.img  (HTTP Range partial download — no full factory ZIP needed)
  3. Download      (AnyKernel3 kernel + magiskboot)
  4. Repack        (magiskboot unpack → swap kernel → repack)
  5. Verify device (adb identity + build match)
  6. Flash         (fastboot flash boot)

Run with --dry-run to do everything except the actual flash.
"""

VERSION = "2.0"

import argparse
import ctypes
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import signal
import struct as _struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from typing import Optional

# ═══════════════════════════════════════════════════════════════════════════════
#  DEVICE CONFIG — change these for your phone
#  Common codenames:
#    cheetah  = Pixel 7 Pro      panther  = Pixel 7
#    husky    = Pixel 8 Pro      shiba    = Pixel 8
#    caiman   = Pixel 9 Pro      tokay    = Pixel 9
# ═══════════════════════════════════════════════════════════════════════════════

DEVICE_CODENAME  = "mustang"   # Your device codename (see list above)
ANDROID_MAJOR    = "17"        # Android major version (Settings → About → Android version)

# Prompted at runtime if None. Set here to skip the prompt (e.g. "qpr1", "qpr2").
BETA_TRACK: Optional[str] = None

# Working directory for all downloads and intermediate files.
WORKDIR = "./reroot_work"

# GitHub repos (no reason to change these)
WILDKERNELS_REPO = "WildKernels/GKI_KernelSU_SUSFS"
MAGISKBOOT_REPO  = "svoboda18/magiskboot"

# Optional GitHub token — avoids API rate limits.
# Set via:  GITHUB_TOKEN=ghp_xxx python reroot.py
GITHUB_TOKEN: Optional[str] = os.environ.get("GITHUB_TOKEN")

# Set to True by --yes flag to skip non-dangerous confirmations
AUTO_YES: bool = False

# ─── ANSI colours ────────────────────────────────────────────────────────────

def _enable_ansi_windows():
    if platform.system().lower() != "windows":
        return
    try:
        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

_enable_ansi_windows()

_USE_COLOR = sys.stdout.isatty() or os.environ.get("FORCE_COLOR")

def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t):   return _c("1;32", t)
def red(t):     return _c("1;31", t)
def yellow(t):  return _c("1;33", t)
def cyan(t):    return _c("1;36", t)
def blue(t):    return _c("1;34", t)
def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)
def magenta(t): return _c("1;35", t)
def white(t):   return _c("0;37", t)

OK  = green("✓")
ERR = red("✗")
WRN = yellow("⚠")
INF = cyan("ℹ")
ARR = cyan("→")
BLT = dim("•")

# ─── Step tracker ─────────────────────────────────────────────────────────────

_STEPS = [
    ("Resolve",  "Resolve URLs"),
    ("boot.img", "Get boot.img"),
    ("Download", "Download kernel"),
    ("Repack",   "Repack"),
    ("Verify",   "Verify device"),
    ("Flash",    "Flash"),
]
_current_step   = 0
_step_start_ts  = 0.0
_pipeline_start = 0.0
_step_timings: dict = {}   # step_index → elapsed_seconds


def _step_bar(current: int) -> str:
    parts = []
    for i, (label, _) in enumerate(_STEPS):
        if i < current:
            parts.append(green(f"✓ {label}"))
        elif i == current:
            parts.append(bold(cyan(f"● {label}")))
        else:
            parts.append(dim(f"○ {label}"))
    return "  " + dim("  ╴  ").join(parts)


def step(index: int, title: str):
    global _current_step, _step_start_ts
    now = time.time()
    if _current_step > 0 and _step_start_ts:
        _step_timings[_current_step - 1] = now - _step_start_ts
    _current_step  = index + 1
    _step_start_ts = now

    width = 62
    bar   = "─" * width
    print(f"\n{cyan(bar)}")
    print(_step_bar(index))
    print(cyan(bar))
    print(f"  {bold(cyan(f'[{index+1}/{len(_STEPS)}]'))}  {bold(title)}")
    print(cyan(bar))


# ─── UI helpers ──────────────────────────────────────────────────────────────

def prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{WRN}  Aborted.")
        sys.exit(1)


def confirm(msg: str, *, danger: bool = False) -> bool:
    if AUTO_YES and not danger:
        print(f"  {bold(msg)} {dim('(y/n):')} {green('y')}  {dim('[--yes]')}")
        return True
    ans = prompt(f"  {bold(msg)} {dim('(y/n):')} ").lower()
    return ans.startswith("y")


def banner():
    w   = 62
    bar = "═" * w
    pad = lambda s: s + " " * max(w - 2 - len(s), 0)
    print()
    print(magenta(bar))
    print(f"  {bold(pad(f'KernelSU Re-Root  v{VERSION}'))}")
    print(magenta("─" * w))
    print(f"  {bold(pad(f'Device   : {DEVICE_CODENAME}  (Android {ANDROID_MAJOR})'))}  ")
    print(magenta(bar))


def section_mini(title: str):
    """Lightweight sub-section header (used inside a step)."""
    print(f"\n  {dim('┄' * 54)}")
    print(f"  {bold(white(title))}")


def ok(msg: str):    print(f"  {OK}  {msg}")
def err(msg: str):   print(f"  {ERR}  {red(msg)}")
def warn(msg: str):  print(f"  {WRN}  {yellow(msg)}")
def info(msg: str):  print(f"  {INF}  {dim(msg)}")
def arrow(msg: str): print(f"  {ARR}  {msg}")
def bullet(msg: str): print(f"  {BLT}  {dim(msg)}")


def _fmt_duration(secs: float) -> str:
    if secs < 60:
        return f"{secs:.1f}s"
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s"


def _summary(build_id: str, kver: str, wildkernels_tag: str,
             boot_size_kb: int, new_boot_size_kb: int,
             download_mode: str, serial: str, slot: str):
    """Print a final summary box."""
    now     = time.time()
    total   = now - _pipeline_start
    w       = 62

    def row(label: str, value: str) -> str:
        lbl = f"  {dim(f'{label:<18s}')} {value}"
        return lbl

    print()
    print(magenta("═" * w))
    print(f"  {bold(green('Pipeline complete — summary'))}")
    print(magenta("─" * w))
    print(row("Device",        f"{bold(DEVICE_CODENAME)}  (Android {ANDROID_MAJOR})"))
    print(row("Build",         bold(build_id)))
    print(row("Kernel",        bold(kver) if kver else dim("(unknown)")))
    print(row("Kernel source", dim(wildkernels_tag)))
    print(row("boot.img",      f"{boot_size_kb:,} KB  {dim(f'({download_mode})')}"))
    print(row("new-boot.img",  f"{new_boot_size_kb:,} KB"))
    if serial:
        print(row("Device serial", dim(serial)))
    if slot:
        print(row("Flash slot",    bold(slot)))
    print(magenta("─" * w))
    # Per-step timings
    for i, (label, _) in enumerate(_STEPS):
        t = _step_timings.get(i)
        if t is not None:
            print(f"  {dim(f'  {label:<12s}')} {dim(_fmt_duration(t))}")
    print(magenta("─" * w))
    print(f"  {bold('Total time')}        {bold(green(_fmt_duration(total)))}")
    print(magenta("═" * w))
    print()

# ─── Network helpers ─────────────────────────────────────────────────────────

def _ua_headers() -> dict:
    """Headers for GitHub API calls (includes auth token if set)."""
    hdrs = {"User-Agent": f"pixel-reroot/{VERSION}"}
    if GITHUB_TOKEN:
        hdrs["Authorization"] = f"token {GITHUB_TOKEN}"
    return hdrs


def _dl_headers() -> dict:
    """Headers for plain file downloads — NO auth token.
    Sending an Authorization header to Google/CDN endpoints causes 416 on Range requests."""
    return {"User-Agent": f"pixel-reroot/{VERSION}"}


def fetch_text(url: str, *, headers: Optional[dict] = None) -> str:
    req = urllib.request.Request(url, headers=headers or _ua_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_json(url: str) -> dict:
    return json.loads(fetch_text(url, headers=_ua_headers()))


def head_check(url: str) -> tuple:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": f"pixel-reroot/{VERSION}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return True, r.status
    except urllib.error.HTTPError as e:
        if e.code in (405, 403):
            try:
                req2 = urllib.request.Request(
                    url, headers={"User-Agent": f"pixel-reroot/{VERSION}",
                                  "Range": "bytes=0-0"})
                with urllib.request.urlopen(req2, timeout=15) as r2:
                    return True, r2.status
            except Exception:
                pass
        return False, e.code
    except Exception:
        return False, 0

# ─── Logging ─────────────────────────────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    _COLORS = {
        logging.DEBUG:    dim,
        logging.INFO:     cyan,
        logging.WARNING:  yellow,
        logging.ERROR:    red,
        logging.CRITICAL: red,
    }
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        col = self._COLORS.get(record.levelno, lambda x: x)
        return msg.replace(record.levelname, col(record.levelname), 1)


def setup_logging(workdir: pathlib.Path) -> logging.Logger:
    workdir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("reroot")
    log.setLevel(logging.DEBUG)
    plain = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                               datefmt="%H:%M:%S")
    color = _ColorFormatter("%(asctime)s  %(levelname)-7s  %(message)s",
                             datefmt="%H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(color)
    fh = logging.FileHandler(str(workdir / "run.log"), encoding="utf-8")
    fh.setFormatter(plain)
    log.addHandler(ch)
    log.addHandler(fh)
    return log

# ─── Download helpers ─────────────────────────────────────────────────────────

def download_file(url: str, dest: pathlib.Path, log: logging.Logger) -> pathlib.Path:
    if url.startswith("file://"):
        local = pathlib.Path(url[7:])
        if not local.exists():
            raise FileNotFoundError(f"Local cached file not found: {local}")
        if local.resolve() != dest.resolve():
            log.debug(f"Copying local cache: {local} → {dest}")
            shutil.copy2(local, dest)
            log.info(f"Copied from cache: {dest.name}")
        else:
            log.info(f"Using in-place local cache: {dest.name}")
        return dest

    if dest.exists() and dest.stat().st_size > 0:
        if dest.suffix == ".zip":
            try:
                with zipfile.ZipFile(dest) as z:
                    bad = z.testzip()
                if bad is None:
                    log.info(f"Cache hit — integrity OK: {dest.name}")
                    ok(f"Cached: {bold(dest.name)}  {dim(f'{dest.stat().st_size//1024:,} KB')}")
                    return dest
                log.warning(f"Cached zip corrupt (bad entry: {bad}) — re-downloading")
            except Exception as exc:
                log.warning(f"Cached zip unreadable ({exc}) — re-downloading")
        else:
            log.info(f"Cache hit: {dest.name}")
            ok(f"Cached: {bold(dest.name)}  {dim(f'{dest.stat().st_size//1024:,} KB')}")
            return dest

    existing = dest.stat().st_size if dest.exists() else 0
    headers  = {"User-Agent": f"pixel-reroot/{VERSION}"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
        log.info(f"Resuming {dest.name} at byte {existing:,}")
    else:
        log.info(f"Starting download: {dest.name}")
        log.debug(f"URL: {url}")

    t0    = time.time()
    bar_w = 35
    print(f"\n  {ARR} {bold(dest.name)}")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            total_remote = int(r.headers.get("Content-Length", 0))
            mode = "ab" if existing and r.status == 206 else "wb"
            if mode == "wb":
                existing = 0
            downloaded  = existing
            grand_total = total_remote + existing
            with open(dest, mode) as f:
                while True:
                    buf = r.read(65536)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    elapsed = time.time() - t0
                    speed   = (downloaded - existing) / elapsed if elapsed > 0 else 0
                    if grand_total:
                        pct    = downloaded * 100 // grand_total
                        filled = bar_w * pct // 100
                        bar    = green("█" * filled) + dim("░" * (bar_w - filled))
                        mb     = downloaded / 1_048_576
                        tmb    = grand_total / 1_048_576
                        spd    = f"{speed/1_048_576:.1f} MB/s" if speed > 0 else ""
                        print(f"\r    [{bar}] {bold(f'{pct:3d}%')}  "
                              f"{mb:.1f}/{tmb:.1f} MB  {dim(spd)}",
                              end="", flush=True)
                    else:
                        mb = downloaded / 1_048_576
                        print(f"\r    {cyan('↓')} {mb:.1f} MB  {dim(f'{speed/1_048_576:.1f} MB/s')}",
                              end="", flush=True)
        elapsed = time.time() - t0
        size_kb = dest.stat().st_size // 1024
        avg_spd = (dest.stat().st_size - existing) / elapsed / 1_048_576 if elapsed > 0 else 0
        print(f"\r    {OK} {green('Done')}  "
              f"{dim(f'{size_kb:,} KB')}  {dim(f'in {_fmt_duration(elapsed)}')}  "
              f"{dim(f'avg {avg_spd:.1f} MB/s')}          ")
        log.info(f"Download complete: {dest.name}  "
                 f"({size_kb:,} KB  {_fmt_duration(elapsed)}  {avg_spd:.1f} MB/s avg)")
    except Exception as e:
        print()
        log.error(f"Download failed for {dest.name}: {e}")
        raise

    if dest.suffix == ".zip":
        log.debug(f"Verifying zip integrity: {dest.name}")
        info("Verifying zip integrity ...")
        with zipfile.ZipFile(dest) as z:
            bad = z.testzip()
        if bad:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Downloaded zip is corrupt (first bad file: {bad})")
        log.info(f"Zip integrity OK: {dest.name}")
        ok("Zip integrity OK")

    return dest


def download_file_simple(url: str, dest_dir: str) -> Optional[str]:
    pathlib.Path(dest_dir).mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0] or "manager_download"
    dest = str(pathlib.Path(dest_dir) / filename)
    print(f"  {ARR} {bold(filename)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"pixel-reroot/{VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total      = int(r.headers.get("Content-Length", 0))
            downloaded = 0
            bar_w      = 30
            with open(dest, "wb") as f:
                while True:
                    buf = r.read(65536)
                    if not buf:
                        break
                    f.write(buf)
                    downloaded += len(buf)
                    if total:
                        pct    = downloaded * 100 // total
                        filled = bar_w * pct // 100
                        bar    = green("█" * filled) + dim("░" * (bar_w - filled))
                        print(f"\r    [{bar}] {pct:3d}%  {downloaded//1024:,} KB",
                              end="", flush=True)
                    else:
                        print(f"\r    {downloaded//1024:,} KB", end="", flush=True)
        print(f"\r    {OK} Saved: {dim(dest)}                              ")
        return dest
    except Exception as e:
        print()
        err(f"Download failed: {e}")
        return None

# ─── GitHub helpers ───────────────────────────────────────────────────────────

def _cached_zip(repo_name: str) -> Optional[str]:
    hint = repo_name.split("/")[-1].lower()
    work = pathlib.Path(WORKDIR)
    if not work.exists():
        return None
    # Search root and one level of subdirectories (per-device workdirs)
    candidates = list(work.glob("*.zip")) + list(work.glob("*/*.zip"))
    for p in candidates:
        if hint in p.name.lower() and p.stat().st_size > 0:
            try:
                with zipfile.ZipFile(p) as z:
                    if z.testzip() is None:
                        return str(p.resolve())
            except Exception:
                pass
    return None


def _github_rate_limit_fallback(repo: str, what: str) -> str:
    cached = _cached_zip(repo)
    if cached:
        ok(f"Found cached file: {dim(cached)}")
        if confirm(f"Use cached {bold(pathlib.Path(cached).name)}?"):
            return f"file://{cached}"

    warn("GitHub API rate limit hit.")
    info("Tip: set GITHUB_TOKEN env var for 5,000 req/hr.")
    print(f"  Paste the direct download URL for the {bold(what)} zip:")
    info(f"  Find it at: https://github.com/{repo}/releases/latest")
    url = prompt(f"  {cyan('URL>')} ").strip()
    if not url:
        sys.exit(1)
    return url


def _resolve_apk_from_repo(repo: str) -> Optional[str]:
    info(f"Querying releases: {cyan(repo)} ...")
    try:
        data = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as e:
        warn(f"GitHub API {e.code} for {repo}")
        return None
    except Exception as e:
        warn(f"Failed to query {repo}: {e}")
        return None

    tag  = data.get("tag_name", "?")
    apks = [a for a in data.get("assets", []) if a["name"].endswith(".apk")]
    if not apks:
        warn(f"No .apk assets in {repo}@{tag}")
        return None

    preferred = [a for a in apks if "spoof" not in a["name"].lower()] or apks

    if len(preferred) == 1:
        ok(f"Found: {bold(preferred[0]['name'])}  {dim(f'release {tag}')}")
        return preferred[0]["browser_download_url"]

    if AUTO_YES:
        chosen = preferred[0]
        ok(f"Auto-selected: {bold(chosen['name'])}  {dim(f'release {tag}')}")
        return chosen["browser_download_url"]

    print(f"    {yellow('Multiple APKs')} in {cyan(repo)} {bold(tag)}:")
    for i, a in enumerate(preferred, 1):
        print(f"      {cyan(f'[{i}]')} {a['name']}")
    choice = prompt(f"    {cyan('Pick:')} ")
    try:
        return preferred[int(choice) - 1]["browser_download_url"]
    except (ValueError, IndexError):
        warn("Invalid choice — skipping.")
        return None

# ─── Step 1a: Google factory image ───────────────────────────────────────────

def resolve_factory_image(beta_track: str, log: logging.Logger) -> tuple:
    section_mini("Google Factory Image")

    page_url = (
        f"https://developer.android.com/about/versions/{ANDROID_MAJOR}/"
        + (f"{beta_track}/download" if beta_track else "download")
    )
    log.debug(f"Fetching factory image page: {page_url}")
    info(f"Scraping: {dim(page_url)}")

    html = None
    try:
        html = fetch_text(page_url)
        log.debug(f"Page fetched: {len(html):,} bytes")
    except Exception as e:
        log.warning(f"Could not fetch factory page: {e}")
        warn(f"Could not fetch page: {e}")

    pattern = re.compile(
        r"https://dl\.google\.com/developers/android/[^\s\"'>]+/"
        rf"(?:images/)?factory/{re.escape(DEVICE_CODENAME)}_beta-"
        r"([^\s\"'>]+)-factory-[0-9a-fA-F]+\.zip",
        re.IGNORECASE,
    )

    matches = list(dict.fromkeys(m.group(0) for m in pattern.finditer(html or "")))
    log.debug(f"Factory URL matches found: {len(matches)}")

    if not matches:
        if html:
            warn("No factory ZIP link found — page may be JS-gated.")
        print(f"\n  {yellow('Manual override')} — paste the factory zip URL:")
        override = prompt(f"  {cyan('URL>')} ")
        if not override:
            sys.exit(1)
        matches = [override]

    if len(matches) > 1:
        print(f"\n  {yellow('Multiple matches:')}")
        for i, u in enumerate(matches, 1):
            print(f"    {cyan(f'[{i}]')} {u}")
        choice = prompt(f"  {cyan('Pick:')} ")
        try:
            chosen_url = matches[int(choice) - 1]
        except (ValueError, IndexError):
            err("Invalid choice.")
            sys.exit(1)
    else:
        chosen_url = matches[0]

    def _extract_build(url: str) -> str:
        m = re.search(
            rf"{re.escape(DEVICE_CODENAME)}_beta-([^-]+(?:\.[^-]+)*)-factory-",
            url, re.IGNORECASE)
        return m.group(1) if m else "(unknown)"

    build_id = _extract_build(chosen_url)

    while True:
        print(f"\n  {BLT} URL   : {dim(chosen_url)}")
        print(f"  {BLT} Build : {bold(green(build_id))}")
        if confirm(f"Factory build {green(build_id)} for {cyan(DEVICE_CODENAME)} — correct?"):
            break
        override = prompt(f"  {cyan('URL>')} ")
        if not override:
            sys.exit(1)
        chosen_url = override
        build_id   = _extract_build(chosen_url)

    log.info(f"Factory URL resolved: build={build_id}")
    ok(f"Factory build: {bold(green(build_id))}")
    return chosen_url, build_id

# ─── Manager APK parser ───────────────────────────────────────────────────────

def _is_direct_download(url: str) -> bool:
    u = url.lower()
    return u.endswith(".apk") or "releases/download" in u


def parse_manager_info(body: str, workdir: str, log: logging.Logger) -> None:
    if not body:
        return
    url_pat = re.compile(r"https?://[^\s\)\]>\"']+")
    manager_lines = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"(?i)\bmanager\b|\bksud\b|\bapk\b", line) and line.strip()
    ]
    all_urls     = [re.sub(r"[,\.;>\)]+$", "", u) for u in url_pat.findall(body)]
    direct_urls  = list(dict.fromkeys(u for u in all_urls if _is_direct_download(u)))
    browser_urls = list(dict.fromkeys(
        u for u in all_urls if not _is_direct_download(u) and "actions/runs" in u))

    if not manager_lines and not direct_urls and not browser_urls:
        return

    print(f"\n  {cyan('── Manager recommendation ──')}")
    for line in manager_lines[:5]:
        line_clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", line)
        line_clean = re.sub(r"[#*`>]+", "", line_clean).strip()
        if line_clean:
            print(f"    {dim(line_clean)}")

    if browser_urls:
        log.debug(f"CI/Actions URLs to resolve: {browser_urls}")
        print(f"\n  {INF} Resolving APK from CI/Actions URLs ...")
        for u in browser_urls:
            m = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/actions/runs/", u)
            if not m:
                continue
            apk_url = _resolve_apk_from_repo(m.group(1))
            if apk_url:
                direct_urls.append(apk_url)
            else:
                warn(f"No APK release found — open manually: {u}")

    if direct_urls:
        print(f"\n  {bold('Manager download(s):')}")
        for u in dict.fromkeys(direct_urls):
            reachable, code = head_check(u)
            icon = OK if reachable else ERR
            status = green(f"HTTP {code}") if reachable else red(f"HTTP {code}")
            print(f"    {icon} {status}  {dim(u)}")

        if confirm("Download the manager APK now?"):
            for u in dict.fromkeys(direct_urls):
                log.info(f"Downloading manager APK: {u}")
                download_file_simple(u, workdir)
        else:
            info("Skipped — download manually from the URLs above.")
    print()

# ─── Step 1b: WildKernels AnyKernel3 ─────────────────────────────────────────

_wildkernels_tag = "(unknown)"


def adb_kernel_version() -> Optional[str]:
    try:
        r = subprocess.run(["adb", "shell", "uname -r"],
                           capture_output=True, text=True, timeout=10)
        m = re.match(r"(\d+\.\d+\.\d+-android\d+)", r.stdout.strip())
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _wk_url_cache_path(workdir: pathlib.Path) -> pathlib.Path:
    return workdir / ".wildkernels_url_cache"


def resolve_wildkernels(kernel_version_hint: Optional[str],
                        log: logging.Logger,
                        workdir: Optional[pathlib.Path] = None) -> str:
    global _wildkernels_tag
    section_mini("WildKernels AnyKernel3 (KernelSU + SUSFS)")

    # Check URL cache first — avoids GitHub API on repeat runs
    if workdir:
        cache_file = _wk_url_cache_path(workdir)
        if cache_file.exists():
            cached_url = cache_file.read_text().strip()
            if cached_url:
                log.info(f"WildKernels URL from cache: {cached_url}")
                ok(f"Cached: {dim(cached_url.split('/')[-1])}")
                return cached_url

    api_url = f"https://api.github.com/repos/{WILDKERNELS_REPO}/releases/latest"
    log.debug(f"Querying WildKernels API: {api_url}")
    info(f"GitHub API: {dim(api_url)}")

    try:
        data = fetch_json(api_url)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            url = _github_rate_limit_fallback(WILDKERNELS_REPO, "AnyKernel3")
            if workdir and url and not url.startswith("file://"):
                try:
                    _wk_url_cache_path(workdir).write_text(url)
                except OSError:
                    pass
            return url
        raise

    _wildkernels_tag = data.get("tag_name", "(unknown)")
    assets           = data.get("assets", [])
    log.info(f"WildKernels release: {_wildkernels_tag}  ({len(assets)} assets)")
    ok(f"Release: {bold(_wildkernels_tag)}  {dim(f'{len(assets)} assets')}")

    parse_manager_info(data.get("body", ""), workdir=WORKDIR, log=log)

    kver = kernel_version_hint
    if not kver:
        log.debug("No kernel version hint — trying adb")
        info("Trying adb for kernel version ...")
        kver = adb_kernel_version()
        if kver:
            log.info(f"Kernel version from adb: {kver}")
            ok(f"Kernel version from device: {bold(green(kver))}")
        else:
            log.warning("Could not read kernel version from adb")
            warn("No adb device — enter kernel version manually.")
            kver = prompt(f"  {cyan('Kernel version (e.g. 6.1.162-android14):')} ").strip()
            if not kver:
                sys.exit(1)

    log.info(f"Target kernel version: {kver}")
    info(f"Target kernel: {bold(kver)}")

    candidates = [a for a in assets
                  if "AnyKernel3" in a["name"] and a["name"].startswith(kver)]

    while True:
        if not candidates:
            all_ak3 = [a for a in assets if "AnyKernel3" in a["name"]]
            warn(f"No AnyKernel3 assets match '{kver}'.")
            if all_ak3:
                print(f"  {bold('Available:')}")
                for a in all_ak3[:10]:
                    print(f"    {dim(a['name'])}")
            kver = prompt(f"  {cyan('Kernel version to search:')} ").strip()
            if not kver:
                sys.exit(1)
            candidates = [a for a in assets
                          if "AnyKernel3" in a["name"] and a["name"].startswith(kver)]
            continue

        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            print(f"\n  {yellow('Multiple variants')} for {bold(kver)}:")
            for i, a in enumerate(candidates, 1):
                print(f"    {cyan(f'[{i}]')} {a['name']}")
            choice = prompt(f"  {cyan('Pick:')} ")
            try:
                chosen = candidates[int(choice) - 1]
            except (ValueError, IndexError):
                err("Invalid choice.")
                sys.exit(1)

        print(f"\n  {BLT} Asset : {bold(green(chosen['name']))}")
        print(f"  {BLT} URL   : {dim(chosen['browser_download_url'])}")
        if confirm(f"Use {green(chosen['name'])}?"):
            log.info(f"AnyKernel3 selected: {chosen['name']}")
            ok(f"AnyKernel3: {bold(chosen['name'])}")
            dl_url = chosen["browser_download_url"]
            if workdir:
                try:
                    _wk_url_cache_path(workdir).write_text(dl_url)
                except OSError:
                    pass
            return dl_url

        kver = prompt(f"  {cyan('Kernel version to search:')} ").strip()
        if not kver:
            sys.exit(1)
        candidates = [a for a in assets
                      if "AnyKernel3" in a["name"] and a["name"].startswith(kver)]

# ─── Step 1c: magiskboot ──────────────────────────────────────────────────────

_MAGISKBOOT_URL = (
    "https://github.com/svoboda18/magiskboot/releases/download/1.0-3/magiskboot.zip"
)


def resolve_magiskboot(log: logging.Logger) -> str:
    section_mini("magiskboot binary (svoboda18)")

    cached = _cached_zip(MAGISKBOOT_REPO)
    if cached:
        log.info(f"magiskboot cached: {cached}")
        ok(f"Cached: {dim(cached)}")
        return f"file://{cached}"

    log.info(f"Using static magiskboot URL: {_MAGISKBOOT_URL}")
    ok(f"magiskboot 1.0-3  {dim('(static URL)')}")
    return _MAGISKBOOT_URL


def _resolve_magiskboot_unused(log: logging.Logger) -> str:
    """Kept for reference — dynamic GitHub API lookup (unused)."""
    api_url = f"https://api.github.com/repos/{MAGISKBOOT_REPO}/releases/latest"
    log.debug(f"Querying magiskboot API: {api_url}")
    info(f"GitHub API: {dim(api_url)}")

    try:
        data = fetch_json(api_url)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return _github_rate_limit_fallback(MAGISKBOOT_REPO, "magiskboot")
        raise

    release_tag = data.get("tag_name", "(unknown)")
    assets      = data.get("assets", [])
    log.info(f"magiskboot release: {release_tag}  ({len(assets)} assets)")
    ok(f"Release: {bold(release_tag)}  {dim(f'{len(assets)} assets')}")

    current_os = platform.system().lower()
    log.debug(f"Detected OS: {current_os}")
    info(f"OS detected: {bold(platform.system())}")

    def os_score(name: str) -> int:
        n = name.lower()
        if current_os == "windows":
            if "win" in n or ".exe" in n: return 3
            if "linux" in n or "darwin" in n or "mac" in n: return -1
        elif current_os == "linux":
            if "linux" in n: return 3
            if "win" in n or ".exe" in n or "darwin" in n or "mac" in n: return -1
        elif current_os == "darwin":
            if "darwin" in n or "mac" in n: return 3
            if "win" in n or ".exe" in n or "linux" in n: return -1
        return 1

    print(f"\n  {bold('Assets:')}")
    for a in assets:
        score = os_score(a["name"])
        marker = green("  ← best match") if score == 3 else ""
        print(f"    {dim(a['name'])}{marker}")

    top = [a for a in assets if os_score(a["name"]) == max(os_score(x["name"]) for x in assets)]

    if len(top) == 1:
        chosen = top[0]
        log.info(f"magiskboot auto-selected: {chosen['name']}")
        ok(f"Auto-selected: {bold(chosen['name'])}")
        if confirm(f"Use {green(chosen['name'])}?"):
            return chosen["browser_download_url"]

    print(f"\n  {bold('Select asset:')}")
    for i, a in enumerate(assets, 1):
        print(f"    {cyan(f'[{i}]')} {a['name']}")
    choice = prompt(f"  {cyan('Pick:')} ")
    try:
        chosen = assets[int(choice) - 1]
    except (ValueError, IndexError):
        err("Invalid choice.")
        sys.exit(1)

    if not confirm(f"Use {green(chosen['name'])}?"):
        sys.exit(1)

    log.info(f"magiskboot selected: {chosen['name']}")
    ok(f"magiskboot: {bold(chosen['name'])}")
    return chosen["browser_download_url"]

# ─── Step 2: Partial factory download (HTTP Range + ZIP CD) ───────────────────

def _raw_range_request(url: str, range_header: str,
                        timeout: int = 120) -> tuple:
    """
    HTTP GET with a Range header.

    Tries curl first (ships with Windows 10+, macOS, most Linux distros) because
    urllib consistently returns 416 on Google's cinnamonbun/baklava CDN paths even
    though the server advertises Accept-Ranges: bytes.  curl handles the same
    request correctly and returns 206.

    Returns (body_bytes, content_range_str, final_url).
    Raises urllib.error.HTTPError on 4xx/5xx.
    """
    curl = shutil.which("curl")
    if curl:
        import tempfile
        # Write body to a temp file so binary data never contaminates header
        # parsing.  Using -D - with binary body in stdout causes rfind(\r\n\r\n)
        # to land inside the body data, producing false 416 status reads.
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".bin")
        os.close(tmp_fd)
        try:
            # -sL              : silent + follow redirects
            # -H "Range: ..."  : raw Range header
            # -D -             : dump response headers to stdout (no body here)
            # -o tmp_path      : body goes to temp file (not stdout)
            cmd = [curl, "-sL", "-H", f"Range: {range_header}",
                   "-D", "-", "-o", tmp_path,
                   "--max-time", str(timeout), url]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            except subprocess.TimeoutExpired:
                raise RuntimeError("curl range request timed out")

            hdr_raw = r.stdout.decode("utf-8", errors="replace")
            with open(tmp_path, "rb") as f:
                body = f.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Parse status + Content-Range from the last HTTP response block
        # (there may be multiple blocks when -L follows redirects)
        status = 0
        cr     = ""
        for line in hdr_raw.splitlines():
            m = re.match(r"HTTP/\S+\s+(\d+)", line)
            if m:
                status = int(m.group(1))   # last match wins
                cr     = ""                # reset for this response block
            if line.lower().startswith("content-range:"):
                cr = line.split(":", 1)[1].strip()

        if status == 416:
            raise urllib.error.HTTPError(url, 416, "Range Not Satisfiable", {}, None)
        if status not in (200, 206):
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, None)

        return body, cr, url

    # ── urllib fallback (no curl) ─────────────────────────────────────────────
    import ssl

    class _RangeRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new_req = urllib.request.Request(newurl)
            for k, v in req.header_items():
                new_req.add_header(k, v)
            return new_req

    ctx    = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _RangeRedirectHandler,
        urllib.request.HTTPSHandler(context=ctx),
    )
    req = urllib.request.Request(
        url, headers={**_dl_headers(), "Range": range_header},
    )
    with opener.open(req, timeout=timeout) as r:
        data = r.read()
        cr   = r.headers.get("Content-Range", "")
        return data, cr, r.url


def _http_size(url: str) -> int:
    """Return file size, using a bytes=0-0 probe so we follow the same redirect
    chain that Range requests will use (HEAD can resolve to a different host)."""
    try:
        _, cr, _ = _raw_range_request(url, "bytes=0-0", timeout=30)
        m = re.search(r"/(\d+)$", cr)
        if m:
            return int(m.group(1))
    except urllib.error.HTTPError:
        pass
    # Fallback to HEAD
    req = urllib.request.Request(url, method="HEAD", headers=_dl_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers["Content-Length"])


def _http_range(url: str, start: int, end: int) -> bytes:
    data, _, _ = _raw_range_request(url, f"bytes={start}-{end}")
    return data


def _http_range_suffix(url: str, suffix_bytes: int) -> tuple:
    """Fetch the last `suffix_bytes` bytes.  Returns (data, total_file_size)."""
    data, cr, _ = _raw_range_request(url, f"bytes=-{suffix_bytes}")
    m     = re.search(r"/(\d+)$", cr)
    total = int(m.group(1)) if m else suffix_bytes + len(data)
    return data, total


def _parse_zip_cd(data: bytes) -> list:
    entries = []
    pos = 0
    while pos + 46 <= len(data):
        if data[pos:pos+4] != b"PK\x01\x02":
            break
        (_, _, _, _, _, _, _, comp_size, uncomp_size,
         fname_len, extra_len, comment_len, _, _, _,
         lh_offset) = _struct.unpack_from("<IHHHHIIIIHHHHHII", data[pos:])
        fname = data[pos+46 : pos+46+fname_len].decode("utf-8", errors="replace")
        extra = data[pos+46+fname_len : pos+46+fname_len+extra_len]
        if 0xFFFFFFFF in (comp_size, uncomp_size, lh_offset):
            ei = 0
            while ei + 4 <= len(extra):
                tag, esz = _struct.unpack_from("<HH", extra[ei:])
                ei += 4
                if tag == 0x0001:
                    vals = []
                    j = ei
                    while j + 8 <= ei + esz:
                        vals.append(_struct.unpack_from("<Q", extra[j:])[0])
                        j += 8
                    vi = 0
                    if uncomp_size == 0xFFFFFFFF and vi < len(vals):
                        uncomp_size = vals[vi]; vi += 1
                    if comp_size == 0xFFFFFFFF and vi < len(vals):
                        comp_size = vals[vi]; vi += 1
                    if lh_offset == 0xFFFFFFFF and vi < len(vals):
                        lh_offset = vals[vi]
                    break
                ei += esz
        entries.append({
            "name":        fname,
            "comp_size":   comp_size,
            "uncomp_size": uncomp_size,
            "lh_offset":   lh_offset,
            "method":      _struct.unpack_from("<IHHHHII", data[pos:])[4],
        })
        pos += 46 + fname_len + extra_len + comment_len
    return entries


def _remote_zip_cd(url: str, file_size: int) -> list:
    tail_sz = min(65536 + 22, file_size)

    # Prefer suffix range ("bytes=-N") — supported by more CDNs than absolute ranges.
    # Absolute range falls back if suffix range is rejected.
    tail = None
    try:
        tail, reported_size = _http_range_suffix(url, tail_sz)
        # Trust the Content-Range total over the HEAD value
        if reported_size and reported_size != file_size:
            file_size = reported_size
    except urllib.error.HTTPError as e:
        if e.code not in (416, 405, 403):
            raise
        # Suffix range not supported — try absolute range
        tail_off = file_size - tail_sz
        tail     = _http_range(url, tail_off, file_size - 1)

    pos = tail.rfind(b"PK\x05\x06")
    if pos == -1:
        raise RuntimeError("ZIP EOCD not found — server may not support Range requests")
    eocd = tail[pos:]
    _, _, _, _, _, cd_size, cd_off, _ = _struct.unpack_from("<IHHHHIIH", eocd)
    if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
        loc = pos - 20
        if loc >= 0 and tail[loc:loc+4] == b"PK\x06\x07":
            z64_off = _struct.unpack_from("<IQQI", tail[loc:])[1]
            z64 = _http_range(url, z64_off, z64_off + 55)
            if z64[:4] == b"PK\x06\x06":
                cd_size = _struct.unpack_from("<Q", z64[40:])[0]
                cd_off  = _struct.unpack_from("<Q", z64[48:])[0]
    cd_data = _http_range(url, cd_off, cd_off + cd_size - 1)
    return _parse_zip_cd(cd_data)


def _local_data_offset(url: str, lh_offset: int) -> int:
    hdr = _http_range(url, lh_offset, lh_offset + 29)
    fname_len, extra_len = _struct.unpack_from("<HH", hdr[26:])
    return lh_offset + 30 + fname_len + extra_len


def fetch_boot_img_streaming(factory_url: str, workdir: pathlib.Path,
                              log: logging.Logger) -> Optional[pathlib.Path]:
    """
    Stream the factory ZIP via a plain GET (no Range needed).
    Parses local file headers on the fly, extracts boot.img, then closes.
    Typically downloads ~300-600 MB instead of the full 11+ GB.
    """
    import zlib as _zlib

    dest = workdir / "boot.img"
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"boot.img cached: {dest.stat().st_size//1024:,} KB")
        ok(f"Cached: {bold('boot.img')}  {dim(f'{dest.stat().st_size//1024:,} KB')}")
        return dest

    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    info("CDN does not support Range — using streaming ZIP parser ...")
    info("(Downloads entries in order, stops as soon as boot.img is found)")
    print(f"\n  {ARR} {bold('Streaming factory ZIP')}")

    req = urllib.request.Request(factory_url, headers=_dl_headers())

    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            total_remote = int(resp.headers.get("Content-Length", 0))
            total_mb     = total_remote / 1_048_576

            # ── Buffered stream ──────────────────────────────────────────────
            class _Stream:
                def __init__(self, r):
                    self._r    = r
                    self._buf  = bytearray()
                    self.total = 0          # bytes received from network

                def _fill(self, need: int):
                    while len(self._buf) < need:
                        chunk = self._r.read(max(65536, need - len(self._buf)))
                        if not chunk:
                            break
                        self._buf.extend(chunk)
                        self.total += len(chunk)
                        if total_remote:
                            pct    = self.total * 100 // total_remote
                            bar_w  = 30
                            filled = bar_w * pct // 100
                            bar    = green("█" * filled) + dim("░" * (bar_w - filled))
                            ela    = time.time() - t0
                            spd    = f"  {self.total/ela/1_048_576:.1f} MB/s" if ela > 0 else ""
                            print(
                                f"\r    [{bar}] {pct:3d}%  "
                                f"{self.total/1_048_576:.0f}/{total_mb:.0f} MB{spd}",
                                end="", flush=True)

                def read(self, n: int) -> bytes:
                    self._fill(n)
                    data = bytes(self._buf[:n])
                    del self._buf[:n]
                    return data

                def skip(self, n: int):
                    remaining = n
                    while remaining > 0:
                        chunk = min(262144, remaining)
                        self._fill(chunk)
                        drop = min(len(self._buf), chunk)
                        if not drop:
                            break
                        del self._buf[:drop]
                        remaining -= drop

            # ── Local-header reader ──────────────────────────────────────────
            def _lh(stream) -> Optional[tuple]:
                """Read one ZIP local file header from stream.
                Returns (fname, method, comp_size) or None at end/error."""
                sig = stream.read(4)
                if sig != b"PK\x03\x04":
                    return None
                hdr = stream.read(26)
                if len(hdr) < 26:
                    return None
                flags      = int.from_bytes(hdr[2:4],   "little")
                method     = int.from_bytes(hdr[4:6],   "little")
                comp_size  = int.from_bytes(hdr[14:18], "little")
                fname_len  = int.from_bytes(hdr[22:24], "little")
                extra_len  = int.from_bytes(hdr[24:26], "little")
                fname      = stream.read(fname_len).decode("utf-8", errors="replace")
                stream.skip(extra_len)
                has_dd = bool(flags & 0x0008)
                if has_dd and comp_size == 0:
                    log.warning(f"Stream: {fname!r} has data descriptor, no local size — aborting")
                    return None
                return fname, method, comp_size

            # ── Limited sub-stream (for STORED inner ZIP) ────────────────────
            class _Limited:
                """Proxy that reads at most `limit` bytes from an _Stream."""
                def __init__(self, s: _Stream, limit: int):
                    self._s   = s
                    self._rem = limit

                def read(self, n: int) -> bytes:
                    n    = min(n, self._rem)
                    data = self._s.read(n) if n else b""
                    self._rem -= len(data)
                    return data

                def skip(self, n: int):
                    n = min(n, self._rem)
                    self._s.skip(n)
                    self._rem -= n

            s = _Stream(resp)

            # ── Walk outer ZIP entries ────────────────────────────────────────
            while True:
                entry = _lh(s)
                if entry is None:
                    log.warning("Stream: outer ZIP exhausted without finding image-*.zip")
                    print()
                    return None

                fname, method, comp_size = entry
                short = fname.split("/")[-1]
                log.debug(f"Outer entry: {fname}  method={method}  "
                          f"comp={comp_size//1024//1024} MB")

                is_inner_zip = re.match(r"(?i)image-.+\.zip$", short)

                if is_inner_zip and method == 0:   # STORED — parseable as sub-stream
                    log.info(f"Stream: found {fname}  ({comp_size//1024//1024} MB STORED)")
                    bullet(f"Found inner ZIP: {bold(short)}")
                    lim = _Limited(s, comp_size)

                    # Walk inner ZIP entries
                    while lim._rem > 0:
                        entry2 = _lh(lim)
                        if entry2 is None:
                            log.warning("Stream: inner ZIP exhausted without boot.img")
                            print()
                            return None

                        fname2, method2, comp_size2 = entry2
                        log.debug(f"  Inner entry: {fname2}  method={method2}  "
                                  f"comp={comp_size2//1024} KB")

                        is_boot = (fname2 == "boot.img"
                                   and "vendor" not in fname2.lower()
                                   and "init"   not in fname2.lower())

                        if is_boot and method2 in (0, 8):
                            bullet(f"Found boot.img  {dim(f'{comp_size2//1024:,} KB compressed')}")
                            log.info(f"Stream: reading boot.img  "
                                     f"comp={comp_size2//1024:,} KB  method={method2}")
                            raw = lim.read(comp_size2)
                            if method2 == 8:
                                raw = _zlib.decompress(raw, -15)
                            dest.write_bytes(raw)
                            size_kb  = dest.stat().st_size // 1024
                            elapsed  = time.time() - t0
                            net_dl   = s.total / 1_048_576
                            print(
                                f"\r    {OK} {green('Done')}  "
                                f"{dim(f'{size_kb:,} KB')}  "
                                f"{dim(f'streamed {net_dl:.0f} MB in {_fmt_duration(elapsed)}')}      ")
                            log.info(f"boot.img saved: {size_kb:,} KB  "
                                     f"(streamed {net_dl:.0f} MB  {_fmt_duration(elapsed)})")
                            ok(f"boot.img: {bold(f'{size_kb:,} KB')}  "
                               f"{dim(f'streamed {net_dl:.0f} MB  '
                                      f'({_fmt_duration(elapsed)})')}")
                            return dest
                        else:
                            log.debug(f"  Skipping inner: {fname2}  {comp_size2//1024} KB")
                            lim.skip(comp_size2)

                elif is_inner_zip and method != 0:
                    log.warning(f"Stream: {fname} is compressed (method={method}) "
                                "— cannot parse as sub-stream")
                    s.skip(comp_size)

                else:
                    log.debug(f"Skipping outer: {fname}  {comp_size//1024//1024} MB")
                    s.skip(comp_size)

    except Exception as e:
        log.warning(f"Streaming extraction failed: {type(e).__name__}: {e}")
        print()
        return None


def fetch_boot_img_partial(factory_url: str, workdir: pathlib.Path,
                            log: logging.Logger) -> Optional[pathlib.Path]:
    import zlib as _zlib

    dest = workdir / "boot.img"
    if dest.exists() and dest.stat().st_size > 0:
        log.info(f"boot.img already cached: {dest.stat().st_size//1024:,} KB")
        ok(f"Cached: {bold('boot.img')}  {dim(f'{dest.stat().st_size//1024:,} KB')}")
        return dest

    workdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    try:
        info("Probing Range GET support ...")
        log.debug(f"Range probe (bytes=0-0): {factory_url}")
        try:
            _, cr, _ = _raw_range_request(factory_url, "bytes=0-0", timeout=20)
            m = re.search(r"/(\d+)$", cr)
            file_size = int(m.group(1)) if m else 0
            log.debug(f"Range probe OK  Content-Range: {cr}")
        except urllib.error.HTTPError as e:
            if e.code == 416:
                log.info("Server returned 416 for Range GET — partial download not supported")
                warn("CDN does not support Range GET — partial download unavailable.")
                return None
            raise

        if not file_size:
            file_size = _http_size(factory_url)

        log.info(f"Factory ZIP: {file_size/1e9:.2f} GB  ({factory_url.split('/')[-1]})")
        bullet(f"Factory ZIP:  {bold(f'{file_size/1e9:.2f} GB')}  {dim('(not downloading)')}")

        log.debug("Fetching outer ZIP central directory ...")
        entries = _remote_zip_cd(factory_url, file_size)
        log.debug(f"Outer ZIP entries: {[e['name'] for e in entries]}")

        inner = next(
            (e for e in entries
             if re.match(r"(?i)image-.+\.zip$", pathlib.Path(e["name"]).name)),
            None)
        if not inner:
            log.warning(f"image-*.zip not found. Outer entries: {[e['name'] for e in entries]}")
            return None

        log.info(f"Inner ZIP: {inner['name']}  "
                 f"method={inner['method']}  size={inner['comp_size']//1024//1024} MB")
        _inner_mb = inner['comp_size'] // 1024 // 1024
        bullet(f"Inner ZIP:    {bold(inner['name'].split('/')[-1])}  "
               f"{dim(f'{_inner_mb} MB')}")

        inner_data_off = _local_data_offset(factory_url, inner["lh_offset"])
        METHOD_STORED, METHOD_DEFLATED = 0, 8

        if inner["method"] == METHOD_STORED:
            log.debug("Inner ZIP is STORED — fetching its CD tail ...")
            inner_end = inner_data_off + inner["comp_size"] - 1
            tail_sz   = min(65536 + 22, inner["comp_size"])
            inner_tail = _http_range(factory_url,
                                     inner_end - tail_sz + 1, inner_end)

            epos = inner_tail.rfind(b"PK\x05\x06")
            if epos == -1:
                log.warning("EOCD not in inner ZIP tail")
                return None

            eocd_inner = inner_tail[epos:]
            _, _, _, _, _, cd2_size, cd2_off_rel, _ = \
                _struct.unpack_from("<IHHHHIIH", eocd_inner)

            cd2_abs   = inner_data_off + cd2_off_rel
            cd2_data  = _http_range(factory_url, cd2_abs, cd2_abs + cd2_size - 1)
            inner_entries = _parse_zip_cd(cd2_data)
            log.debug(f"Inner ZIP entries ({len(inner_entries)}): "
                      f"{[e['name'] for e in inner_entries]}")

            boot = next(
                (e for e in inner_entries
                 if e["name"] == "boot.img"
                 and "vendor" not in e["name"].lower()
                 and "init" not in e["name"].lower()),
                None)
            if not boot:
                log.warning(f"boot.img not in inner ZIP. Entries: "
                             f"{[e['name'] for e in inner_entries]}")
                return None

            log.info(f"boot.img entry: method={boot['method']}  "
                     f"compressed={boot['comp_size']//1024} KB  "
                     f"uncompressed={boot['uncomp_size']//1024} KB")
            _boot_kb  = boot['uncomp_size'] // 1024
            _boot_mth = boot['method']
            bullet(f"boot.img:     {dim(f'{_boot_kb:,} KB uncompressed')}  "
                   f"{dim(f'compression method {_boot_mth}')}")

            if boot["method"] not in (METHOD_STORED, METHOD_DEFLATED):
                log.warning(f"Unsupported boot.img compression {boot['method']}")
                return None

            boot_data_off = _local_data_offset(
                factory_url, inner_data_off + boot["lh_offset"])

            log.info(f"Downloading boot.img bytes: offset={boot_data_off}  "
                     f"comp_size={boot['comp_size']//1024} KB")
            info(f"Downloading {bold('boot.img')} ({boot['comp_size']//1024:,} KB compressed) ...")
            print(f"\n  {ARR} {bold('boot.img')}  {dim('(targeted range download)')}")

            dl_t0 = time.time()
            raw   = _http_range(factory_url, boot_data_off,
                                 boot_data_off + boot["comp_size"] - 1)
            dl_elapsed = time.time() - dl_t0

            if boot["method"] == METHOD_DEFLATED:
                log.debug("Decompressing DEFLATED boot.img ...")
                raw = _zlib.decompress(raw, -15)

        elif inner["method"] == METHOD_DEFLATED:
            log.info(f"Inner ZIP is DEFLATED — downloading {inner['comp_size']//1024//1024} MB ...")
            info(f"Inner ZIP DEFLATED — downloading {inner['comp_size']//1024//1024} MB ...")
            print(f"\n  {ARR} {bold(inner['name'].split('/')[-1])}")

            req = urllib.request.Request(
                factory_url,
                headers={**_dl_headers(),
                         "Range": f"bytes={inner_data_off}-"
                                  f"{inner_data_off + inner['comp_size'] - 1}"})
            compressed = bytearray()
            dl_t0 = time.time()
            with urllib.request.urlopen(req, timeout=300) as r:
                total = inner["comp_size"]
                bar_w = 35
                while True:
                    buf = r.read(65536)
                    if not buf:
                        break
                    compressed.extend(buf)
                    pct    = len(compressed) * 100 // total
                    filled = bar_w * pct // 100
                    bar    = green("█" * filled) + dim("░" * (bar_w - filled))
                    print(f"\r    [{bar}] {bold(f'{pct:3d}%')}  "
                          f"{len(compressed)//1024//1024}/{total//1024//1024} MB",
                          end="", flush=True)
            dl_elapsed = time.time() - dl_t0
            print(f"\r    {OK} {green('Done')}                                              ")

            import io as _io
            inner_bytes = _zlib.decompress(bytes(compressed), -15)
            with zipfile.ZipFile(_io.BytesIO(inner_bytes)) as iz:
                candidates = [n for n in iz.namelist()
                              if pathlib.Path(n).name == "boot.img"
                              and "vendor" not in n.lower()
                              and "init" not in n.lower()]
                if not candidates:
                    log.warning("boot.img not in deflated inner ZIP")
                    return None
                with iz.open(candidates[0]) as src:
                    raw = src.read()
        else:
            log.warning(f"Unknown inner ZIP method: {inner['method']}")
            return None

        dest.write_bytes(raw)
        size_kb    = dest.stat().st_size // 1024
        total_time = time.time() - t0
        log.info(f"boot.img saved: {size_kb:,} KB  "
                 f"(total partial-DL time: {_fmt_duration(total_time)})")
        print(f"\r    {OK} {green('Done')}  {dim(f'{size_kb:,} KB')}  "
              f"{dim(f'in {_fmt_duration(total_time)}')}          ")
        ok(f"boot.img: {bold(f'{size_kb:,} KB')}  {dim(f'partial download in {_fmt_duration(total_time)}')}")
        return dest

    except Exception as e:
        log.warning(f"Partial download failed: {type(e).__name__}: {e}")
        warn(f"Partial download failed ({e}) — falling back to full ZIP download.")
        return None

# ─── Step 2 fallback: full factory ZIP extract ────────────────────────────────

def extract_factory(factory_zip: pathlib.Path, workdir: pathlib.Path,
                    log: logging.Logger) -> tuple:
    outer_dir = workdir / "factory"
    inner_dir = workdir / "images"

    if outer_dir.exists() and any(outer_dir.iterdir()):
        log.info(f"Outer ZIP already extracted: {outer_dir}")
        info("Outer factory ZIP already extracted — skipping.")
    else:
        log.info(f"Extracting outer ZIP → {outer_dir}")
        info(f"Extracting {factory_zip.name} ...")
        outer_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(factory_zip) as z:
            z.extractall(outer_dir)
        log.debug(f"Outer ZIP extracted: {list(outer_dir.iterdir())}")

    inner_zips = list(outer_dir.rglob("image-*.zip"))
    if not inner_zips:
        contents = [str(p.relative_to(outer_dir)) for p in outer_dir.rglob("*")]
        raise RuntimeError(
            f"No image-*.zip found.\nContents:\n  " + "\n  ".join(contents))
    inner_zip = inner_zips[0]
    log.info(f"Inner image ZIP: {inner_zip.name}")
    bullet(f"Inner ZIP: {inner_zip.name}")

    if inner_dir.exists() and any(inner_dir.iterdir()):
        log.info(f"Inner ZIP already extracted: {inner_dir}")
        info("Inner image ZIP already extracted — skipping.")
    else:
        log.info(f"Extracting inner ZIP → {inner_dir}")
        info(f"Extracting {inner_zip.name} ...")
        inner_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(inner_zip) as z:
            z.extractall(inner_dir)

    return outer_dir, inner_dir


def find_boot_img(outer_dir: pathlib.Path, inner_dir: pathlib.Path,
                  log: logging.Logger) -> pathlib.Path:
    all_candidates = (list(outer_dir.rglob("boot.img"))
                      + list(inner_dir.rglob("boot.img")))
    strict = [p for p in all_candidates
              if p.name == "boot.img"
              and "vendor" not in [x.lower() for x in p.parts]
              and "init"   not in [x.lower() for x in p.parts]]
    found = strict or all_candidates
    if found:
        p = found[0]
        log.info(f"boot.img located: {p}  ({p.stat().st_size//1024:,} KB)")
        return p

    all_imgs = sorted(
        list(outer_dir.rglob("*.img")) + list(inner_dir.rglob("*.img")),
        key=lambda p: p.name)
    log.error("boot.img not found in factory image")
    print(f"\n  {red('[!!!]')} {bold(red('CRITICAL: boot.img not found.'))}")
    print("        Do NOT substitute init_boot.img or vendor_boot.img.")
    print("        Partition images found:")
    for p in all_imgs:
        print(f"          {p.name}  ({p.stat().st_size//1024:,} KB)")
    choice = prompt("  [1] Abort  [2] Provide path manually — choice: ")
    if choice.strip() == "2":
        path_str = prompt("  Full path to boot.img: ")
        p = pathlib.Path(path_str)
        if not p.exists():
            raise FileNotFoundError(f"Not found: {p}")
        return p
    raise FileNotFoundError("boot.img not found — aborting.")

# ─── Kernel version extraction (LZ4 legacy) ───────────────────────────────────

_LZ4_LEGACY_MAGIC = b"\x02\x21\x4c\x18"
_LZ4_BLOCK_SIZE   = 8 * 1024 * 1024


def _lz4_legacy_decompress(data: bytes) -> bytes:
    import lz4.block
    pos = 4
    out = bytearray()
    while pos + 4 <= len(data):
        bsz = int.from_bytes(data[pos:pos+4], "little")
        pos += 4
        if bsz == 0:
            break
        if pos + bsz > len(data):
            break
        out.extend(lz4.block.decompress(data[pos:pos+bsz],
                                         uncompressed_size=_LZ4_BLOCK_SIZE))
        pos += bsz
    return bytes(out)


def kernel_version_from_boot_img(boot_img: pathlib.Path,
                                  log: logging.Logger) -> Optional[str]:
    _VER = re.compile(rb"Linux version (\d+\.\d+\.\d+-android\d+)")
    try:
        log.debug(f"Reading boot.img: {boot_img}  ({boot_img.stat().st_size//1024:,} KB)")
        with open(boot_img, "rb") as f:
            data = f.read()

        if data[:8] == b"ANDROID!":
            hdr_ver     = int.from_bytes(data[40:44], "little")
            kernel_size = int.from_bytes(data[8:12],  "little")
            page_size   = (4096 if hdr_ver in (3, 4)
                           else int.from_bytes(data[36:40], "little"))
            kernel_blob = data[page_size : page_size + kernel_size]
            log.debug(f"Boot header v{hdr_ver}: page_size={page_size}  "
                      f"kernel_size={kernel_size:,}  blob={len(kernel_blob):,} bytes")
        else:
            kernel_blob = data
            log.debug("No ANDROID! magic — treating entire file as kernel blob")

        if kernel_blob[:4] == _LZ4_LEGACY_MAGIC:
            log.debug("LZ4 legacy magic detected — decompressing kernel blob ...")
            try:
                t0           = time.time()
                decompressed = _lz4_legacy_decompress(kernel_blob)
                log.debug(f"LZ4 decompressed: {len(decompressed):,} bytes  "
                          f"in {time.time()-t0:.2f}s")
                m = _VER.search(decompressed)
                if m:
                    return m.group(1).decode()
            except ImportError:
                raise RuntimeError("lz4 not installed — run:  pip install lz4")
            except Exception as e:
                raise RuntimeError(f"LZ4 decompression failed: {e}") from e

        for chunk in (kernel_blob, data):
            m = _VER.search(chunk)
            if m:
                return m.group(1).decode()

    except RuntimeError:
        raise
    except Exception as e:
        log.debug(f"kernel_version_from_boot_img exception: {e}")
    return None

# ─── Step 3: Extract kernel Image + magiskboot ────────────────────────────────

def extract_anykernel_image(ak3_zip: pathlib.Path, workdir: pathlib.Path,
                             log: logging.Logger) -> pathlib.Path:
    dest = workdir / "Image"
    log.debug(f"Opening AnyKernel3 ZIP: {ak3_zip.name}")
    with zipfile.ZipFile(ak3_zip) as z:
        names = z.namelist()
        log.debug(f"AnyKernel3 contents: {names}")
        if "Image" in names:
            with z.open("Image") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            log.info(f"Extracted Image: {dest.stat().st_size//1024:,} KB")
            return dest
        if "Bypass-Image" in names:
            log.warning("'Image' missing from AnyKernel3 ZIP — 'Bypass-Image' present")
            warn("'Image' missing — 'Bypass-Image' is available.")
            if not confirm("Use Bypass-Image instead?"):
                raise FileNotFoundError("'Image' not found. Aborting.")
            with z.open("Bypass-Image") as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            log.warning(f"Using Bypass-Image: {dest.stat().st_size//1024:,} KB")
            return dest
        raise FileNotFoundError(
            f"Neither 'Image' nor 'Bypass-Image' in {ak3_zip.name}.\n"
            f"Contents: {names}")


def extract_magiskboot_bin(mb_zip: pathlib.Path, workdir: pathlib.Path,
                            log: logging.Logger) -> pathlib.Path:
    is_win = platform.system().lower() == "windows"
    prefer = "magiskboot.exe" if is_win else "magiskboot"
    log.debug(f"Extracting magiskboot from {mb_zip.name}  (prefer: {prefer})")

    with zipfile.ZipFile(mb_zip) as z:
        names = z.namelist()
        log.debug(f"magiskboot ZIP contents: {names}")
        match = next((n for n in names if pathlib.Path(n).name == prefer), None)
        if not match:
            match = next((n for n in names
                          if pathlib.Path(n).name in ("magiskboot", "magiskboot.exe")), None)
        if not match:
            raise FileNotFoundError(
                f"magiskboot binary not found in {mb_zip.name}.\nContents: {names}")
        dest = workdir / pathlib.Path(match).name
        with z.open(match) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)

    if not is_win:
        dest.chmod(dest.stat().st_mode | 0o111)
    log.info(f"magiskboot extracted: {dest}  ({dest.stat().st_size//1024} KB)")
    return dest

# ─── Step 4: Repack with magiskboot ──────────────────────────────────────────

def repack_boot(boot_img: pathlib.Path, kernel_image: pathlib.Path,
                magiskboot_bin: pathlib.Path, workdir: pathlib.Path,
                log: logging.Logger, dry_run: bool) -> pathlib.Path:
    ws = workdir / "kernel_workspace"
    ws.mkdir(exist_ok=True)

    boot_copy = workdir / "boot.img"
    if boot_copy.resolve() != boot_img.resolve():
        log.debug(f"Copying boot.img → {boot_copy}")
        shutil.copy2(boot_img, boot_copy)
        log.info(f"Copied boot.img: {boot_copy.stat().st_size//1024:,} KB")

    def run_mb(args_list: list, desc: str):
        cmd = [str(magiskboot_bin)] + [str(a) for a in args_list]
        log.info(f"Running: {' '.join(cmd)}")
        bullet(f"$ {' '.join(pathlib.Path(c).name if i == 0 else c for i, c in enumerate(cmd))}")
        if dry_run:
            log.info("[dry-run] skipped")
            return
        t0 = time.time()
        r  = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True)
        elapsed = time.time() - t0
        if r.stdout.strip():
            for line in r.stdout.strip().splitlines():
                log.debug(f"  stdout: {line}")
        if r.stderr.strip():
            for line in r.stderr.strip().splitlines():
                log.debug(f"  stderr: {line}")
                bullet(dim(line))
        if r.returncode != 0:
            log.error(f"{desc} failed (exit {r.returncode})\n"
                      f"stdout: {r.stdout}\nstderr: {r.stderr}")
            raise RuntimeError(
                f"{desc} failed (exit {r.returncode})\n"
                f"stderr: {r.stderr.strip()}\n"
                f"Artifacts preserved in: {ws}")
        log.info(f"{desc} OK  ({_fmt_duration(elapsed)})")
        ok(f"{desc}  {dim(f'({_fmt_duration(elapsed)})')}")

    print(f"\n  {BLT} Workspace : {dim(str(ws))}")
    print(f"  {BLT} boot.img  : {dim(str(boot_copy))}  "
          f"({boot_copy.stat().st_size//1024:,} KB)")
    print(f"  {BLT} kernel    : {dim(str(kernel_image))}  "
          f"({kernel_image.stat().st_size//1024:,} KB)")
    print()

    run_mb(["unpack", "../boot.img"], "magiskboot unpack")

    kernel_slot = ws / "kernel"
    if not dry_run and not kernel_slot.exists():
        raise FileNotFoundError(
            f"magiskboot unpack did not produce 'kernel' in {ws}.\n"
            f"Contents: {list(ws.iterdir())}")

    log.info(f"Swapping kernel: {kernel_image.stat().st_size//1024:,} KB → {kernel_slot}")
    info(f"Swapping kernel ({kernel_image.stat().st_size//1024:,} KB) ...")
    if not dry_run:
        shutil.copy2(kernel_image, kernel_slot)
        log.debug("Kernel swapped.")

    run_mb(["repack", "../boot.img"], "magiskboot repack")

    new_boot = ws / "new-boot.img"
    if not dry_run:
        if not new_boot.exists():
            raise FileNotFoundError(f"magiskboot repack did not produce new-boot.img in {ws}")

        orig_sz = boot_copy.stat().st_size
        new_sz  = new_boot.stat().st_size
        ratio   = new_sz / orig_sz if orig_sz else 0
        log.info(f"new-boot.img: {new_sz//1024:,} KB  "
                 f"(original: {orig_sz//1024:,} KB  ratio: {ratio:.2f}x)")
        print()
        print(f"  {BLT} Original  : {orig_sz//1024:,} KB")
        print(f"  {BLT} New boot  : {new_sz//1024:,} KB  {dim(f'({ratio:.2f}x)')}")

        if ratio < 0.5 or ratio > 2.0:
            log.warning(f"Suspicious size ratio: {ratio:.2f}x")
            warn(f"new-boot.img is {ratio:.2f}x the original — unusual.")
            if not confirm("Continue anyway?"):
                raise RuntimeError("Aborted — suspicious size ratio.")
        else:
            ok(f"Size ratio {green(f'{ratio:.2f}x')} — looks good")

    return new_boot

# ─── Step 5: Device verification ─────────────────────────────────────────────

def run_adb(cmd: list, adb_path: str, serial: Optional[str] = None,
            timeout: int = 30) -> subprocess.CompletedProcess:
    full = [adb_path] + (["-s", serial] if serial else []) + cmd
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def wait_for_fastboot(fastboot_path: str, log: logging.Logger,
                      timeout_s: int = 60) -> bool:
    log.info("Waiting for fastboot device ...")
    print("  Waiting for fastboot", end="", flush=True)
    for _ in range(timeout_s // 2):
        r = subprocess.run([fastboot_path, "devices"], capture_output=True, text=True)
        if any(l.strip() for l in r.stdout.strip().splitlines()):
            print(f"  {OK} found.")
            return True
        time.sleep(2)
        print(dim("."), end="", flush=True)
    print(f"  {ERR} timed out.")
    return False


_FASTBOOT_SERIAL = "__fastboot__"   # sentinel: device already in fastboot/recovery


def _adb_devices(adb_path: str) -> list:
    try:
        r = subprocess.run([adb_path, "devices"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return []
    lines = r.stdout.strip().splitlines()[1:]
    return [(l.split()[0], l.split()[1]) for l in lines if len(l.split()) >= 2]


def _fastboot_devices(fastboot_path: str) -> list:
    try:
        r = subprocess.run([fastboot_path, "devices"], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return []
    lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    return lines   # each line is "<serial>  fastboot"


def verify_device(adb_path: str, fastboot_path: str, build_id: str,
                  log: logging.Logger) -> Optional[str]:
    """
    Returns:
      serial string  — device found via adb, proceed normally
      _FASTBOOT_SERIAL — device already in fastboot/recovery, skip adb verify
      None           — user cancelled
    """
    log.debug("Checking for connected adb devices ...")

    # ── Check fastboot first (bootloop / manual fastboot boot) ───────────────
    fb_devs = _fastboot_devices(fastboot_path)
    if fb_devs:
        log.info(f"Device already in fastboot: {fb_devs}")
        print(f"\n  {yellow('[!]')} Device detected in {bold(yellow('fastboot'))} mode:")
        for d in fb_devs:
            print(f"      {dim(d)}")
        warn("Cannot read build props in fastboot — skipping identity check.")
        info("This is expected if the phone is bootlooping or you rebooted manually.")
        if not confirm(f"Flash new-boot.img to this device now?", danger=True):
            return None
        return _FASTBOOT_SERIAL

    # ── Normal adb path ───────────────────────────────────────────────────────
    devices = _adb_devices(adb_path)
    if not devices:
        print(f"\n  {WRN}  No device detected via adb or fastboot.")
        info("Options:")
        print(f"    {dim('a)')} Enable USB debugging and connect the phone normally")
        print(f"    {dim('b)')} If bootlooping: hold Vol-Down + Power to enter fastboot, then reconnect")
        print(f"  {dim('Waiting for adb')} ", end="", flush=True)
        for _ in range(60):
            time.sleep(1)
            print(dim("."), end="", flush=True)
            # Check both adb and fastboot while waiting
            devices = _adb_devices(adb_path)
            if devices:
                break
            fb_devs = _fastboot_devices(fastboot_path)
            if fb_devs:
                print()
                log.info(f"Device appeared in fastboot during wait: {fb_devs}")
                print(f"\n  {yellow('[!]')} Device appeared in {bold(yellow('fastboot'))} mode.")
                warn("Skipping identity check — cannot read props in fastboot.")
                if not confirm("Flash new-boot.img to this device now?", danger=True):
                    return None
                return _FASTBOOT_SERIAL
        print()
        if not devices:
            err("Timed out — no device found.")
            if not confirm("Retry?"):
                return None
            devices = _adb_devices(adb_path)
            if not devices:
                # One last fastboot check
                fb_devs = _fastboot_devices(fastboot_path)
                if fb_devs:
                    warn("Device is in fastboot. Skipping identity check.")
                    if not confirm("Flash now?", danger=True):
                        return None
                    return _FASTBOOT_SERIAL
                return None
    else:
        log.info(f"Device connected via adb: {devices}")
        ok("Device connected via adb.")

    if len(devices) == 1:
        serial = devices[0][0]
        state  = devices[0][1]
        log.info(f"Serial: {serial}  state: {state}")
        bullet(f"Serial: {bold(serial)}  state: {dim(state)}")
        if state == "recovery":
            info("Device is in recovery mode — adb shell props may be limited.")
    else:
        print("  Multiple devices:")
        for i, (s, state) in enumerate(devices, 1):
            print(f"    {cyan(f'[{i}]')} {s}  ({state})")
        choice = prompt("  Pick: ")
        try:
            serial = devices[int(choice) - 1][0]
        except (ValueError, IndexError):
            log.error("Invalid device selection")
            return None

    props = {
        "ro.product.device":            "Codename",
        "ro.build.version.release":     "Android",
        "ro.build.version.incremental": "Build incremental",
        "ro.build.fingerprint":         "Fingerprint",
    }
    print()
    values: dict = {}
    for prop, label in props.items():
        r = run_adb(["shell", f"getprop {prop}"], adb_path, serial)
        values[prop] = r.stdout.strip()
        log.debug(f"  {prop} = {values[prop]}")
        print(f"  {BLT} {label:<22s}: {dim(values[prop])}")

    r = run_adb(["shell", "uname -r"], adb_path, serial)
    kver_device = r.stdout.strip()
    log.info(f"Device kernel: {kver_device}")
    print(f"  {BLT} {'Kernel':<22s}: {dim(kver_device)}")
    print()

    device_codename = values.get("ro.product.device", "")
    if device_codename and device_codename != DEVICE_CODENAME:
        log.warning(f"Codename mismatch: device={device_codename}  expected={DEVICE_CODENAME}")
        warn(f"Codename mismatch: device is '{device_codename}', expected '{DEVICE_CODENAME}'.")
        if not confirm("Continue anyway?"):
            return None

    fingerprint = values.get("ro.build.fingerprint", "")
    if build_id and fingerprint and build_id.lower() not in fingerprint.lower():
        log.warning(f"Build mismatch: factory={build_id}  fingerprint={fingerprint}")
        print(f"\n  {red('[!] BUILD MISMATCH')}")
        print(f"  {BLT} Factory build : {bold(build_id)}")
        print(f"  {BLT} Fingerprint   : {dim(fingerprint)}")
        warn("Mismatched boot.img can bootloop the device.")
        if not confirm("Continue despite build mismatch?"):
            return None
    else:
        log.info(f"Build match OK: '{build_id}' in fingerprint")
        ok(f"Build match {green('OK')} — '{build_id}' found in fingerprint")

    if not confirm(f"Confirmed: {bold(device_codename or DEVICE_CODENAME)} ({serial}) — proceed?"):
        return None

    return serial

# ─── Step 6: Flash ───────────────────────────────────────────────────────────

def flash_boot(new_boot: pathlib.Path, adb_path: str, fastboot_path: str,
               serial: Optional[str], log: logging.Logger,
               dry_run: bool) -> str:
    """Returns the flashed slot string (e.g. 'boot_a') or '' on dry-run."""
    if dry_run:
        log.info("[dry-run] Skipping flash step.")
        info("[dry-run] Flash step skipped.")
        return ""

    if serial == _FASTBOOT_SERIAL:
        log.info("Device already in fastboot — skipping adb reboot.")
        info("Device already in fastboot — skipping reboot step.")
        if not wait_for_fastboot(fastboot_path, log):
            raise RuntimeError("Fastboot device disappeared. Check USB connection.")
    else:
        log.info("Rebooting to bootloader ...")
        info("Rebooting to bootloader ...")
        run_adb(["reboot", "bootloader"], adb_path, serial)
        if not wait_for_fastboot(fastboot_path, log):
            raise RuntimeError("Timed out waiting for fastboot. Check USB connection.")

    r = subprocess.run([fastboot_path, "devices"], capture_output=True, text=True)
    fastboot_devices = r.stdout.strip()
    log.info(f"fastboot devices: {fastboot_devices}")
    bullet(f"Fastboot device: {dim(fastboot_devices)}")

    r = subprocess.run([fastboot_path, "getvar", "unlocked"],
                       capture_output=True, text=True)
    output = (r.stdout + r.stderr).lower()
    log.debug(f"fastboot getvar unlocked: {output.strip()}")
    if "unlocked: yes" not in output and "unlocked: true" not in output:
        log.error(f"Bootloader locked: {output.strip()}")
        print(f"\n  {red('[!!!]')} {bold(red('Bootloader is LOCKED.'))}")
        print(f"       fastboot output: {dim(output.strip())}")
        print("       Unlock the bootloader first — this tool will NOT do it.")
        raise RuntimeError("Bootloader locked — cannot flash.")
    log.info("Bootloader unlocked.")
    ok(f"Bootloader: {bold(green('unlocked'))}")

    # Detect active slot
    slot = ""
    r2 = subprocess.run([fastboot_path, "getvar", "current-slot"],
                        capture_output=True, text=True)
    slot_out = (r2.stdout + r2.stderr).lower()
    log.debug(f"fastboot getvar current-slot: {slot_out.strip()}")
    m = re.search(r"current-slot:\s*([ab])", slot_out)
    if m:
        slot = f"boot_{m.group(1)}"
        log.info(f"Active slot: {slot}")
        bullet(f"Active slot: {bold(slot)}")

    print()
    print(f"  {BLT} File    : {bold(new_boot.name)}  ({new_boot.stat().st_size//1024:,} KB)")
    print(f"  {BLT} Command : {dim(f'fastboot flash boot {new_boot}')}")

    if not confirm(f"\n  {bold(red('FINAL CONFIRMATION'))} — flash now?"):
        print("  Aborted. Phone is in fastboot — run 'fastboot reboot' to recover.")
        return slot

    t0 = time.time()
    log.info(f"Flashing: fastboot flash boot {new_boot}")
    r = subprocess.run([fastboot_path, "flash", "boot", str(new_boot)],
                       capture_output=True, text=True)
    elapsed = time.time() - t0
    log.info(f"stdout: {r.stdout.strip()}")
    log.info(f"stderr: {r.stderr.strip()}")
    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            bullet(dim(line))
    if r.returncode != 0:
        raise RuntimeError(
            f"fastboot flash failed (exit {r.returncode}):\n{r.stderr.strip()}")

    log.info(f"Flash complete in {_fmt_duration(elapsed)}")
    ok(f"Flash complete  {dim(f'({_fmt_duration(elapsed)})')}")

    if confirm("\n  Reboot now?"):
        subprocess.run([fastboot_path, "reboot"])
        log.info("Rebooting device ...")
        info("Rebooting. Open KernelSU Manager after boot — it should show rooted.")
    else:
        info("Phone left in fastboot. Run 'fastboot reboot' when ready.")

    return slot

# ─── Platform Tools installer ─────────────────────────────────────────────────

def _install_platform_tools(log: logging.Logger):
    missing = []
    if not shutil.which("adb"):      missing.append("adb")
    if not shutil.which("fastboot"): missing.append("fastboot")
    if not missing:
        return

    log.warning(f"Missing platform tools: {missing}")
    warn(f"Missing: {', '.join(missing)}")
    print()

    is_win   = platform.system() == "Windows"
    is_mac   = platform.system() == "Darwin"
    is_linux = platform.system() == "Linux"

    if is_win:
        print(f"  {cyan('A')} — auto-install via winget  {dim('(recommended)')}")
        print(f"  {cyan('B')} — manual: https://developer.android.com/tools/releases/platform-tools")
        print()
        if not confirm("Install now via winget?"):
            info("Install manually, add to PATH, then re-run.")
            sys.exit(1)
        log.info("Running: winget install Google.PlatformTools")
        r = subprocess.run(
            ["winget", "install", "--id", "Google.PlatformTools", "-e",
             "--accept-source-agreements", "--accept-package-agreements"],
            text=True)
        if r.returncode != 0:
            err("winget install failed.")
            sys.exit(1)
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as k:
                sys_path, _ = winreg.QueryValueEx(k, "Path")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                try:
                    usr_path, _ = winreg.QueryValueEx(k, "Path")
                except FileNotFoundError:
                    usr_path = ""
            os.environ["PATH"] = (sys_path + os.pathsep + usr_path
                                  + os.pathsep + os.environ.get("PATH", ""))
        except Exception:
            pass
        log.info("Platform Tools installed via winget")
        ok("Platform Tools installed. Reopen terminal if still not found.")

    elif is_mac:
        print(f"  {cyan('A')} — brew install android-platform-tools")
        print(f"  {cyan('B')} — manual: https://developer.android.com/tools/releases/platform-tools")
        print()
        if not shutil.which("brew"):
            err("Homebrew not found. Install from https://brew.sh")
            sys.exit(1)
        if not confirm("Install via brew?"):
            sys.exit(1)
        r = subprocess.run(["brew", "install", "android-platform-tools"], text=True)
        if r.returncode != 0:
            err("brew install failed.")
            sys.exit(1)
        ok("Platform Tools installed.")

    elif is_linux:
        if shutil.which("apt-get") or shutil.which("apt"):
            print(f"  {cyan('A')} — sudo apt-get install android-tools-adb android-tools-fastboot")
            print(f"  {cyan('B')} — manual: https://developer.android.com/tools/releases/platform-tools")
            print()
            if not confirm("Install via apt (requires sudo)?"):
                sys.exit(1)
            r = subprocess.run(
                ["sudo", "apt-get", "install", "-y",
                 "android-tools-adb", "android-tools-fastboot"], text=True)
            if r.returncode != 0:
                err("apt-get install failed.")
                sys.exit(1)
            ok("Platform Tools installed.")
        else:
            err("No package manager found. Install adb/fastboot manually.")
            sys.exit(1)
    else:
        err("Unknown OS — install adb/fastboot manually.")
        sys.exit(1)
    print()

# ─── CLI + main ──────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"KernelSU re-root pipeline v{VERSION} for Pixel GKI devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python reroot.py --yes\n"
               "  python reroot.py --yes --dry-run\n"
               "  python reroot.py --full-download --workdir D:\\reroot")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve, download and repack — but do not flash")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Auto-confirm all prompts (including final flash)")
    p.add_argument("--adb",           default=None, metavar="PATH",
                   help="Path to adb binary (default: auto-detect)")
    p.add_argument("--fastboot",      default=None, metavar="PATH",
                   help="Path to fastboot binary (default: auto-detect)")
    p.add_argument("--magiskboot",    default=None, metavar="PATH",
                   help="Path to magiskboot binary (default: auto-download)")
    p.add_argument("--workdir",       default=WORKDIR, metavar="DIR",
                   help=f"Working directory (default: {WORKDIR})")
    p.add_argument("--full-download", action="store_true",
                   help="Download full factory ZIP instead of partial boot.img")
    return p.parse_args()


def main():
    global AUTO_YES, BETA_TRACK, _pipeline_start

    args    = parse_args()
    workdir = pathlib.Path(args.workdir) / DEVICE_CODENAME
    AUTO_YES = args.yes

    _pipeline_start = time.time()

    log = setup_logging(workdir)
    log.info(f"reroot v{VERSION} starting  |  pid={os.getpid()}  |  "
             f"python={sys.version.split()[0]}  |  os={platform.system()}")

    if sys.version_info < (3, 9):
        sys.exit(red("ERROR: Python 3.9+ required."))

    adb_path      = args.adb      or shutil.which("adb")
    fastboot_path = args.fastboot or shutil.which("fastboot")

    if not adb_path or not fastboot_path:
        _install_platform_tools(log)
        adb_path      = args.adb      or shutil.which("adb")
        fastboot_path = args.fastboot or shutil.which("fastboot")
    if not adb_path:
        sys.exit(red("ERROR: 'adb' not found."))
    if not fastboot_path:
        sys.exit(red("ERROR: 'fastboot' not found."))

    log.info(f"adb:      {adb_path}")
    log.info(f"fastboot: {fastboot_path}")
    log.info(f"workdir:  {workdir.resolve()}")
    log.info(f"flags:    dry_run={args.dry_run}  yes={args.yes}  "
             f"full_download={args.full_download}")

    def _sigint(sig, frame):
        print(f"\n\n{WRN}  Interrupted — artifacts in: {dim(str(workdir.resolve()))}")
        sys.exit(130)
    signal.signal(signal.SIGINT, _sigint)

    # ── Banner ────────────────────────────────────────────────────────────────
    banner()
    tags = []
    if args.dry_run:      tags.append(yellow("DRY-RUN"))
    if args.yes:          tags.append(cyan("--yes"))
    if args.full_download: tags.append(dim("full-download"))

    print(f"  {BLT} Workdir  : {dim(str(workdir.resolve()))}")
    print(f"  {BLT} adb      : {dim(adb_path)}")
    print(f"  {BLT} fastboot : {dim(fastboot_path)}")
    if GITHUB_TOKEN:
        print(f"  {BLT} GitHub   : {dim('token set ✓')}")
    if tags:
        print(f"  {BLT} Flags    : {' · '.join(tags)}")

    if BETA_TRACK is None:
        print()
        BETA_TRACK = prompt(
            f"  {cyan('Beta track')} (e.g. qpr1, qpr2 — or Enter for base): ")

    log.info(f"device={DEVICE_CODENAME}  android={ANDROID_MAJOR}  track='{BETA_TRACK}'")

    # ── Step 1: Resolve URLs ──────────────────────────────────────────────────
    step(0, "Resolve URLs")

    factory_url, build_id = resolve_factory_image(BETA_TRACK, log)
    magiskboot_url        = resolve_magiskboot(log)

    # ── Step 2: Get boot.img ──────────────────────────────────────────────────
    step(1, "Get boot.img from factory image")

    boot_img      = None
    download_mode = "partial"

    if not args.full_download:
        # Try HTTP Range partial download (~40 MB). Falls back if CDN rejects Range.
        boot_img = fetch_boot_img_partial(factory_url, workdir, log)

    if boot_img is None:
        download_mode = "full ZIP"
        if not args.full_download:
            info("Falling back to full factory ZIP download ...")
        factory_zip = workdir / factory_url.split("/")[-1]
        download_file(factory_url, factory_zip, log)
        outer_dir, inner_dir = extract_factory(factory_zip, workdir, log)
        boot_img = find_boot_img(outer_dir, inner_dir, log)

    log.info(f"boot.img ready: {boot_img}  ({boot_img.stat().st_size//1024:,} KB)  "
             f"mode={download_mode}")
    ok(f"boot.img ready  {dim(f'{boot_img.stat().st_size//1024:,} KB')}  "
       f"{dim(f'({download_mode})')}")

    try:
        kver = kernel_version_from_boot_img(boot_img, log)
    except RuntimeError as e:
        err(str(e))
        sys.exit(1)

    if kver:
        log.info(f"Kernel version from boot.img: {kver}")
        ok(f"Kernel version: {bold(green(kver))}")
    else:
        log.warning("Could not read kernel version from boot.img")
        warn("Could not read kernel version — will try adb or prompt.")

    # ── Step 3: Download kernel + magiskboot ──────────────────────────────────
    step(2, "Download AnyKernel3 kernel + magiskboot")

    wildkernels_url = resolve_wildkernels(kver, log, workdir=workdir)

    ak3_zip = workdir / wildkernels_url.split("/")[-1]
    download_file(wildkernels_url, ak3_zip, log)

    mb_zip = workdir / magiskboot_url.split("/")[-1]
    download_file(magiskboot_url, mb_zip, log)

    log.info("Extracting kernel Image from AnyKernel3 ZIP ...")
    kernel_image = extract_anykernel_image(ak3_zip, workdir, log)
    ok(f"Kernel Image: {bold(f'{kernel_image.stat().st_size//1024:,} KB')}")

    if args.magiskboot and pathlib.Path(args.magiskboot).exists():
        magiskboot_bin = pathlib.Path(args.magiskboot)
        log.info(f"Using user-supplied magiskboot: {magiskboot_bin}")
        bullet(f"magiskboot: {dim(str(magiskboot_bin))}  (user-supplied)")
    else:
        log.info("Extracting magiskboot binary ...")
        magiskboot_bin = extract_magiskboot_bin(mb_zip, workdir, log)
    ok(f"magiskboot: {dim(str(magiskboot_bin))}")

    # ── Step 4: Repack ────────────────────────────────────────────────────────
    step(3, "Repack boot image")

    new_boot = repack_boot(boot_img, kernel_image, magiskboot_bin,
                            workdir, log, args.dry_run)
    new_boot_kb = new_boot.stat().st_size // 1024 if not args.dry_run else 0
    if not args.dry_run:
        ok(f"new-boot.img: {bold(f'{new_boot_kb:,} KB')}  {dim(str(new_boot))}")
    else:
        info(f"[dry-run] new-boot.img would be at: {new_boot}")

    # ── Step 5: Verify device ─────────────────────────────────────────────────
    step(4, "Verify device identity")

    serial = verify_device(adb_path, fastboot_path, build_id, log)
    if serial is None:
        warn(f"Verification cancelled. Artifacts preserved in: {workdir}")
        sys.exit(0)

    # Install manager APK if one was downloaded
    apks = list(workdir.glob("*.apk"))
    if apks:
        print()
        for apk in apks:
            arrow(f"Manager APK: {bold(apk.name)}")
        if confirm("Install manager APK on device now?"):
            for apk in apks:
                log.info(f"Installing APK: {apk.name}")
                arrow(f"Installing {bold(apk.name)} ...")
                r = subprocess.run(
                    [adb_path, "-s", serial, "install", "-r", str(apk)],
                    capture_output=True, text=True)
                if r.returncode == 0:
                    log.info(f"APK installed: {apk.name}")
                    ok(f"Installed: {green(apk.name)}")
                else:
                    log.warning(f"APK install failed: {r.stdout.strip()} {r.stderr.strip()}")
                    warn(f"Install failed: {r.stdout.strip() or r.stderr.strip()}")

    # ── Step 6: Flash ─────────────────────────────────────────────────────────
    step(5, "Flash new-boot.img")

    slot = flash_boot(new_boot, adb_path, fastboot_path, serial, log, args.dry_run)

    # Record final step timing
    _step_timings[5] = time.time() - _step_start_ts

    log.info(f"Pipeline complete. Total: {_fmt_duration(time.time() - _pipeline_start)}")

    _summary(
        build_id       = build_id,
        kver           = kver or "(unknown)",
        wildkernels_tag = _wildkernels_tag,
        boot_size_kb   = boot_img.stat().st_size // 1024,
        new_boot_size_kb = new_boot_kb,
        download_mode  = download_mode,
        serial         = serial or "",
        slot           = slot,
    )

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if not args.dry_run:
        _cleanup(workdir, factory_url, log)


def _cleanup(workdir: pathlib.Path, factory_url: str, log: logging.Logger):
    """Remove intermediate files. Keeps the factory ZIP (large re-download) and run.log."""
    factory_zip_name = factory_url.split("/")[-1]

    # Directories to remove entirely
    dirs_to_remove = [
        workdir / "kernel_workspace",
        workdir / "factory",
        workdir / "images",
    ]
    # Individual files to remove
    files_to_remove = [
        workdir / "boot.img",
        workdir / "Image",
        workdir / "magiskboot",
        workdir / "magiskboot.exe",
    ]
    # ZIPs that are NOT the factory zip (AnyKernel3, magiskboot)
    for p in workdir.glob("*.zip"):
        if p.name != factory_zip_name:
            files_to_remove.append(p)

    section_mini("Cleanup")
    freed = 0

    for d in dirs_to_remove:
        if d.exists():
            try:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d)
                freed += size
                log.info(f"Removed dir: {d.name}  ({size//1024:,} KB)")
                bullet(f"Removed {dim(d.name + '/')}  {dim(f'{size//1024:,} KB')}")
            except Exception as e:
                log.warning(f"Could not remove {d}: {e}")

    for f in files_to_remove:
        if f.exists():
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
                log.info(f"Removed file: {f.name}  ({size//1024:,} KB)")
                bullet(f"Removed {dim(f.name)}  {dim(f'{size//1024:,} KB')}")
            except Exception as e:
                log.warning(f"Could not remove {f}: {e}")

    if freed:
        ok(f"Freed {bold(f'{freed//1024//1024:,} MB')}  {dim('(factory ZIP and run.log kept)')}")
    else:
        info("Nothing to clean up.")


if __name__ == "__main__":
    main()
