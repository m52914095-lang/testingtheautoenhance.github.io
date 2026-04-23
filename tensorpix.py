#!/usr/bin/env python3
"""
TensorPix Automation Bot - COMBINED & FIXED VERSION
Creates accounts with Boomlify temp mail (10min), verifies, uploads video, enhances at 4K with Animation model, merges results

Usage:
    python3 tensorpix_bot_combined.py <video_file> <num_accounts> [options]

    BOOMLIFY_API_KEY=key python3 tensorpix_bot_combined.py input.mp4 5
    BOOMLIFY_API_KEY=key1,key2 python3 tensorpix_bot_combined.py input.mp4 5
    python3 tensorpix_bot_combined.py input.mp4 5 --api=key1,key2 --start=100

Each API key is used for up to 50 mailbox creations; the next key is used automatically after that.
Set BOOMLIFY_API_KEY to a comma-separated list, or pass +API= / --api= on the command line.

Mailboxes use Boomlify custom domains zikzak.site and edu.zikzak.site (alternating per account).
Chromium uses a pool built from free_proxies.json (optional) plus live free HTTP proxy lists.
"""

import asyncio
from playwright.async_api import async_playwright
import re
import time
import sys
import os
import subprocess
import json
from datetime import datetime
import random
import urllib.parse
import urllib.request
import urllib.error

# ==================== CONFIGURATION ====================
BOOMLIFY_BASE = "https://v1.boomlify.com"
BOOMLIFY_MAIL_LIFETIME = "10min"
ACCOUNTS_PER_BOOMLIFY_KEY = 50
# Verified custom domains for Boomlify create (?domain=...) 閳ワ拷 must match your Boomlify account.
BOOMLIFY_EMAIL_DOMAINS = ("zikzak.site", "edu.zikzak.site")

# How many different HTTP proxies to try launching Chromium with before falling back to direct.
MAX_PROXY_LAUNCH_ATTEMPTS = int(os.environ.get("TENSORPIX_MAX_PROXY_TRIES", "15"))
# Quick-connect timeout (seconds) per proxy 閳ワ拷 dead proxies get skipped fast.
PROXY_QUICK_TEST_SECONDS = int(os.environ.get("PROXY_QUICK_TEST_S", "10"))

TENSORPIX_PASSWORD = "12345wasdD!"
TENSORPIX_URL = "https://app.tensorpix.ai"
# Playwright defaults are 30s; networkidle often never settles on SPAs (websockets / analytics).
PLAYWRIGHT_NAV_TIMEOUT_MS = int(os.environ.get("PLAYWRIGHT_NAV_TIMEOUT_MS", "120000"))

# Only accept verification emails from @mta.notify.tensorpix.ai
TENSORPIX_EMAIL_DOMAIN = "@mta.notify.tensorpix.ai"

# Proxy cooldown: once a proxy is used, skip it for this many seconds (84000s = ~23.3 hours)
PROXY_COOLDOWN_SECONDS = int(os.environ.get("TENSORPIX_PROXY_COOLDOWN", "84000"))

VERIFY_LINK_PATTERNS = [
    r'href=["\']?(https://[^\s<>"\']*/verify[^\s<>"\']*)["\'\s>]',
    r'href=["\']?(https://app\.tensorpix\.ai[^\s<>"\']*)["\'\s>]',
    r'https://[^\s<>"]+verify[^\s<>"]*',
    r'https://app\.tensorpix\.ai[^\s<>"]*',
    r'https://[^\s<>"]+/verify[^\s<>"]*',
    r'http://[^\s<>"]+verify[^\s<>"]*',
]

# Quality settings - segment duration in seconds based on resolution
# 360P = 2 min, 480P = 1 min, 720P = 45 sec, 1080P = 30 sec, 4K = 20 sec
QUALITY_SETTINGS = {
    "360p": 120,   # 2 minutes
    "480p": 60,    # 1 minute
    "720p": 45,    # 45 seconds
    "1080p": 30,   # 30 seconds
    "2160p": 20,   # 20 seconds (4K)
}

STATE_FILE = "bot_state.json"
PROXY_STATE_FILE = "proxy_cooldown.json"

# Browser fingerprinting (simplified)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
]

# ==================== UTILITIES ====================

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}")

def _normalize_playwright_proxy(entry):
    """Return {'server': 'http://host:port'} or None."""
    if isinstance(entry, dict) and entry.get("server"):
        s = str(entry["server"]).strip()
        if "://" not in s:
            s = f"http://{s}"
        return {"server": s}
    if isinstance(entry, str):
        s = entry.strip()
        if not s or s.startswith("#"):
            return None
        if "://" not in s:
            s = f"http://{s}"
        if re.match(r"^https?://[\w.-]+:\d+$", s):
            return {"server": s}
    return None


def load_proxies_file():
    """Load proxies from free_proxies.json (list of strings or {server: ...} dicts)."""
    out = []
    try:
        path = os.environ.get("FREE_PROXIES_JSON", "free_proxies.json")
        if os.path.exists(path):
            with open(path, "r") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                for item in raw:
                    p = _normalize_playwright_proxy(item)
                    if p:
                        out.append(p)
            log(f"[PROXY] Loaded {len(out)} entries from {path}")
    except Exception as e:
        log(f"[PROXY] Could not load proxy file: {e}", "WARNING")
    return out


def _http_get_text(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TensorPixBot/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(errors="replace")


def fetch_proxies_proxyscrape_http(max_lines=120):
    """Public HTTP proxy list (text, ip:port per line)."""
    url = (
        "https://api.proxyscrape.com/v2/?request=get&protocol=http"
        "&timeout=10000&country=all&ssl=all&anonymity=all"
    )
    try:
        text = _http_get_text(url, timeout=35)
    except Exception as e:
        log(f"[PROXY] proxyscrape.com list failed: {e}", "WARNING")
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[\w.-]+:\d+$", line):
            out.append({"server": f"http://{line}"})
        if len(out) >= max_lines:
            break
    return out


def fetch_proxies_public_http_list(url, max_lines=80):
    """Generic raw text proxy list (ip:port per line)."""
    try:
        text = _http_get_text(url, timeout=35)
    except Exception as e:
        log(f"[PROXY] fetch {url[:60]}... failed: {e}", "WARNING")
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[\w.-]+:\d+$", line):
            out.append({"server": f"http://{line}"})
        if len(out) >= max_lines:
            break
    return out


def _dedupe_proxies(proxy_list):
    seen = set()
    out = []
    for p in proxy_list:
        if not isinstance(p, dict):
            continue
        s = p.get("server")
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append({"server": s})
    return out


def refresh_proxy_pool():
    """Merge file-based proxies with live free HTTP proxy lists."""
    merged = []
    merged.extend(load_proxies_file())
    merged.extend(fetch_proxies_proxyscrape_http(150))
    merged.extend(
        fetch_proxies_public_http_list(
            "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
            100,
        )
    )
    merged.extend(
        fetch_proxies_public_http_list(
            "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
            80,
        )
    )
    return _dedupe_proxies(merged)


# ==================== PROXY COOLDOWN SYSTEM ====================
# Once a proxy is used, it's put on cooldown for PROXY_COOLDOWN_SECONDS.
# On the next get_next_proxy() call, cooled-down proxies are skipped.

PROXIES = []
_proxy_cooldowns = {}  # server_string -> timestamp when cooldown expires


def _load_proxy_cooldowns():
    """Load cooldowns from disk so they persist across bot restarts."""
    global _proxy_cooldowns
    if os.path.exists(PROXY_STATE_FILE):
        try:
            with open(PROXY_STATE_FILE, "r") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                _proxy_cooldowns = saved
                log(f"[PROXY-COOLDOWN] Loaded {len(saved)} cooldown entries from {PROXY_STATE_FILE}")
        except Exception:
            _proxy_cooldowns = {}


def _save_proxy_cooldowns():
    """Persist cooldowns to disk."""
    try:
        with open(PROXY_STATE_FILE, "w") as f:
            json.dump(_proxy_cooldowns, f)
    except Exception as e:
        log(f"[PROXY-COOLDOWN] Could not save cooldowns: {e}", "DEBUG")


def _is_proxy_on_cooldown(proxy_dict):
    """Return True if this proxy is still within its cooldown window."""
    server = proxy_dict.get("server", "") if isinstance(proxy_dict, dict) else str(proxy_dict)
    if not server:
        return False
    expires_at = _proxy_cooldowns.get(server)
    if expires_at is None:
        return False
    if time.time() < expires_at:
        return True
    # Cooldown expired 閳ワ拷 clean it up
    del _proxy_cooldowns[server]
    return False


def _mark_proxy_used(proxy_dict):
    """Put a proxy on cooldown immediately after it's used."""
    server = proxy_dict.get("server", "") if isinstance(proxy_dict, dict) else str(proxy_dict)
    if not server:
        return
    _proxy_cooldowns[server] = time.time() + PROXY_COOLDOWN_SECONDS
    log(f"[PROXY-COOLDOWN] {server} put on cooldown for {PROXY_COOLDOWN_SECONDS}s "
        f"(expires {datetime.fromtimestamp(time.time() + PROXY_COOLDOWN_SECONDS).isoformat()})")
    _save_proxy_cooldowns()


def _prune_expired_cooldowns():
    """Remove expired entries to keep the file small."""
    now = time.time()
    expired = [k for k, v in _proxy_cooldowns.items() if now >= v]
    for k in expired:
        del _proxy_cooldowns[k]
    if expired:
        _save_proxy_cooldowns()


def get_next_proxy():
    """Return the next available (non-cooled-down) proxy from the pool, or None."""
    ensure_proxy_pool()
    _prune_expired_cooldowns()

    if not PROXIES:
        log("[PROXY] No proxies in pool 閳ワ拷 using direct connection", "WARNING")
        return None

    total = len(PROXIES)
    available = []
    for p in PROXIES:
        if not _is_proxy_on_cooldown(p):
            available.append(p)

    if not available:
        log(f"[PROXY] All {total} proxies are on cooldown 閳ワ拷 using direct connection", "WARNING")
        return None

    # Pick a random available proxy (avoids predictable patterns)
    chosen = random.choice(available)
    log(f"[PROXY] Selected: {chosen['server']} ({len(available)}/{total} available, {total - len(available)} on cooldown)")
    return chosen


def ensure_proxy_pool():
    """Lazy-fetch free proxies on first use."""
    global PROXIES
    if not PROXIES:
        _load_proxy_cooldowns()
        log("[PROXY] Fetching free HTTP proxy lists (online + optional free_proxies.json)...")
        PROXIES = refresh_proxy_pool()
        log(f"[PROXY] Ready: {len(PROXIES)} unique http:// proxies in pool")

def random_delay(min_sec=5, max_sec=15):
    delay = random.uniform(min_sec, max_sec)
    log(f"[DELAY] Waiting for {delay:.2f} seconds...")
    time.sleep(delay)

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {"current_num": 1, "processed_segments": 0, "successful": 0}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def get_video_duration(video_path):
    """Get video duration in seconds using ffprobe"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except:
        return None

def get_video_quality(video_path):
    """Detect video quality (height)"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30
        )
        height = int(result.stdout.strip())
        if height >= 2160: return "2160p"
        if height >= 1080: return "1080p"
        if height >= 720: return "720p"
        if height >= 480: return "480p"
        return "360p"
    except Exception:
        return "720p"  # default

def split_video(input_file, segment_dir="segments", quality="2160p"):
    """Split video into segments based on quality"""
    os.makedirs(segment_dir, exist_ok=True)

    duration = get_video_duration(input_file)
    if not duration:
        log(f"[ERROR] Could not get video duration", "ERROR")
        return []

    segment_time = QUALITY_SETTINGS.get(quality, 10)
    log(f"[VIDEO] Duration: {duration}s, Quality: {quality}, Segment: {segment_time}s")

    segments = []
    num_segments = max(1, int(duration / segment_time))
    if duration % segment_time > 0 and duration > segment_time:
        num_segments += 1

    for i in range(num_segments):
        start_time = i * segment_time
        output = os.path.join(segment_dir, f"segment_{i:03d}.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", input_file,
            "-ss", str(start_time), "-t", str(segment_time),
            "-c", "copy", "-avoid_negative_ts", "make_zero", output
        ]
        result = subprocess.run(cmd, capture_output=True)

        if os.path.exists(output):
            size = os.path.getsize(output)
            if size > 100:  # sanity check
                segments.append(output)
                log(f"[VIDEO] Created segment {i}: {output} ({size} bytes)")
            else:
                log(f"[VIDEO] Segment {i} too small: {size} bytes", "WARNING")
        else:
            log(f"[VIDEO] Failed to create segment {i}", "ERROR")

    log(f"[VIDEO] Split into {len(segments)} segments")
    return segments

def downscale_to_720p_if_needed(input_file):
    """If video height > 720p, downscale to 720p using high-quality ffmpeg settings.
    
    Returns (output_path, was_downscaled).
    If already 720p or below, returns original path and False.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height,width", "-of", "csv=s=x:p=0", input_file],
            capture_output=True, text=True, timeout=30
        )
        parts = result.stdout.strip().split('x')
        if len(parts) < 2:
            return input_file, False
        width, height = int(parts[0]), int(parts[1])
    except Exception:
        return input_file, False

    if height <= 720:
        log(f"[DOWNSCALE] Video is {height}p 閳ワ拷 no downscale needed")
        return input_file, False

    # Calculate new width maintaining aspect ratio (scale to height=720)
    new_height = 720
    new_width = int(width * (new_height / height))
    # Make width even (required by many codecs)
    new_width = new_width if new_width % 2 == 0 else new_width + 1

    out_path = input_file.replace(".mp4", "_720p.mp4")
    if os.path.exists(out_path):
        os.remove(out_path)

    log(f"[DOWNSCALE] Downscaling {width}x{height} -> {new_width}x{new_height}...")
    cmd = [
        "ffmpeg", "-y", "-i", input_file,
        "-vf", f"scale={new_width}:{new_height}",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if os.path.exists(out_path) and os.path.getsize(out_path) > 100:
        log(f"[DOWNSCALE] Created 720p version: {out_path} ({os.path.getsize(out_path)} bytes)")
        return out_path, True
    else:
        log(f"[DOWNSCALE] Failed to downscale, using original: {result.stderr[-200:]}", "WARNING")
        return input_file, False


def merge_videos(segments, output_file):
    """Merge enhanced segments back into single video"""
    if not segments:
        log(f"[ERROR] No segments to merge", "ERROR")
        return False

    # Create concat file
    concat_file = "concat_list.txt"
    with open(concat_file, 'w') as f:
        for seg in segments:
            f.write(f"file '{seg}'\n")

    result = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c", "copy", output_file
    ], capture_output=True)

    os.remove(concat_file)

    if os.path.exists(output_file):
        log(f"[VIDEO] Merged into: {output_file}")
        return True
    return False


# ==================== BOOMLIFY API ====================
# Response formats (confirmed via live API testing):
#
# CREATE: POST /api/v1/emails/create?time=10min&domain=zikzak.site
#   {"success": true, "email": {"id": "uuid", "address": "user@domain", ...}, "meta": {...}}
#
# MESSAGES: GET /api/v1/emails/{id}/messages
#   {"success": true, "messages": [...], "email": {...}, "pagination": {...}, "meta": {...}}
#
# Each message object may contain fields like:
#   sender, sender_email, from, from_address, subject,
#   html_body, body_html, html, text_body, body_text, text, content, snippet, preview


def boomlify_domain_for_account(email_num):
    """Alternate zikzak.site / edu.zikzak.site so TensorPix sees both domains over a run."""
    doms = BOOMLIFY_EMAIL_DOMAINS
    return doms[email_num % len(doms)]

def _boomlify_headers(api_key):
    return {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _boomlify_http_json(method, url, api_key, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, method=method, headers=_boomlify_headers(api_key), data=body)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace") if e.fp else ""
        log(f"[BOOMLIFY] HTTP {e.code}: {err_body[:400]}", "ERROR")
        raise


_EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _normalize_id(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coerce_address(value):
    if isinstance(value, str) and "@" in value:
        return value.strip()
    if isinstance(value, dict):
        for k in (
            "email",
            "address",
            "email_address",
            "emailAddress",
            "mail",
            "full_email",
            "fullEmail",
        ):
            v = value.get(k)
            r = _coerce_address(v)
            if r:
                return r
    return None


def _deep_find_inbox_id_and_address(obj, depth=0):
    """Walk JSON for any email string and plausible inbox id (last wins for id, any email)."""
    if depth > 14 or obj is None:
        return None, None
    found_id, found_addr = None, None
    if isinstance(obj, dict):
        for key, val in obj.items():
            kl = str(key).lower().replace("-", "_")
            if kl in ("id", "email_id", "mailbox_id", "inbox_id", "_id", "uuid", "public_id") and not isinstance(val, (dict, list)):
                nid = _normalize_id(val)
                if nid:
                    found_id = nid
            if kl in ("email", "address", "email_address", "emailaddress", "mail", "mailbox"):
                ad = _coerce_address(val)
                if ad:
                    found_addr = ad
        for val in obj.values():
            cid, cad = _deep_find_inbox_id_and_address(val, depth + 1)
            if cid:
                found_id = cid
            if cad:
                found_addr = cad
    elif isinstance(obj, list):
        for item in obj:
            cid, cad = _deep_find_inbox_id_and_address(item, depth + 1)
            if cid:
                found_id = cid
            if cad:
                found_addr = cad
    elif isinstance(obj, str):
        m = _EMAIL_ADDR_RE.search(obj)
        if m:
            found_addr = m.group(0)
    return found_id, found_addr


def _parse_mailbox_from_create_response(payload):
    """Parse inbox ID and email address from the Boomlify create response.

    Confirmed format (2026-04):
      {
        "success": true,
        "email": {"id": "uuid", "address": "user@domain", "domain": "...", ...},
        "meta": {...}
      }

    Also handles legacy/alternative shapes for forward-compat.
    """
    if payload is None:
        return None, None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        return None, None

    # === Priority 1: Current Boomlify v1 format: {"email": {"id", "address"}} ===
    email_obj = payload.get("email")
    if isinstance(email_obj, dict):
        mid = _normalize_id(email_obj.get("id"))
        addr = _coerce_address(email_obj.get("address")) or _coerce_address(email_obj.get("email"))
        if mid and addr:
            return mid, addr
        # If we got id but not address (or vice versa), keep them as partial
        # and fall through to try more fields below

    # === Priority 2: {"data": {...}} wrapper ===
    inner = payload.get("data")
    blob = None
    if isinstance(inner, dict):
        blob = inner
    elif isinstance(inner, list) and inner and isinstance(inner[0], dict):
        blob = inner[0]

    if blob:
        mb = blob.get("mailbox")
        if isinstance(mb, dict):
            mid = mid or mb.get("id") or blob.get("id") or payload.get("id")
            addr = addr or mb.get("email") or mb.get("address") or mb.get("email_address")
        mid = mid or (
            blob.get("id")
            or blob.get("email_id")
            or blob.get("mailbox_id")
            or blob.get("inboxId")
            or blob.get("inbox_id")
        )
        addr = addr or (
            blob.get("email")
            or blob.get("address")
            or blob.get("email_address")
            or blob.get("emailAddress")
            or blob.get("full_email")
            or blob.get("fullEmail")
        )
        if not addr and isinstance(mb, str):
            addr = mb
        addr = addr or blob.get("mailbox") if isinstance(blob.get("mailbox"), str) else addr

    # === Priority 3: Top-level fields ===
    mid = mid or payload.get("id")
    addr = addr or _coerce_address(payload.get("email")) or _coerce_address(payload.get("address"))

    if isinstance(addr, dict):
        addr = _coerce_address(addr)
    mid = _normalize_id(mid)
    if isinstance(addr, str):
        addr = addr.strip()

    if mid and addr:
        return mid, addr

    # === Fallback: deep scan for any uuid and email string ===
    dm_id, dm_addr = _deep_find_inbox_id_and_address(payload)
    mid = mid or dm_id
    addr = addr or dm_addr
    return mid, addr


def boomlify_create_inbox(api_key, domain=None):
    params = {"time": BOOMLIFY_MAIL_LIFETIME}
    if domain:
        params["domain"] = domain.strip().lower()
    q = urllib.parse.urlencode(params)
    url = f"{BOOMLIFY_BASE}/api/v1/emails/create?{q}"
    body = b"{}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "X-API-Key": api_key.strip(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                log("[BOOMLIFY] create inbox: empty response body", "ERROR")
                return None, None
            text = raw.decode(errors="replace").strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                log(f"[BOOMLIFY] create inbox: not JSON (first 200 chars): {text[:200]!r}", "ERROR")
                return None, None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace") if e.fp else ""
        log(f"[BOOMLIFY] create inbox HTTP {e.code}: {err_body[:500]}", "ERROR")
        raise
    if isinstance(data, dict) and data.get("error"):
        msg = data.get("message") or data.get("detail") or ""
        log(f"[BOOMLIFY] create rejected: {data.get('error')!r} {msg}", "ERROR")
        return None, None
    mid, addr = _parse_mailbox_from_create_response(data)
    if not mid or not addr:
        preview = text[:800]
        if len(text) > 800:
            preview += "..."
        log(
            f"[BOOMLIFY] Could not parse inbox id/email from create response. Raw JSON (truncated):\n{preview}",
            "ERROR",
        )
    return mid, addr


def boomlify_list_messages(api_key, inbox_id):
    eid = urllib.parse.quote(str(inbox_id), safe="")
    url = f"{BOOMLIFY_BASE}/api/v1/emails/{eid}/messages"
    data = _boomlify_http_json("GET", url, api_key)
    if not data:
        return []
    msgs = data.get("messages")
    if msgs is None and isinstance(data.get("data"), list):
        msgs = data.get("data")
    if msgs is None:
        msgs = data if isinstance(data, list) else []
    return msgs if isinstance(msgs, list) else []


def boomlify_delete_inbox(api_key, inbox_id):
    eid = urllib.parse.quote(str(inbox_id), safe="")
    url = f"{BOOMLIFY_BASE}/api/v1/emails/{eid}"
    req = urllib.request.Request(url, method="DELETE", headers=_boomlify_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        log(f"[BOOMLIFY] DELETE inbox {eid}: HTTP {e.code}", "DEBUG")


def _message_text_from_boomlify(msg):
    """Extract all text/html content from a Boomlify message object."""
    if not isinstance(msg, dict):
        return ""
    chunks = []

    # Check nested content dict first
    content = msg.get("content")
    if isinstance(content, dict):
        for key in ("html", "text", "plain", "body"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                chunks.append(val)

    # Check all common top-level field names
    for key in (
        "html", "html_body", "body_html",
        "body", "text", "text_body", "body_text",
        "content", "snippet", "preview",
    ):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            chunks.append(val)

    return "\n".join(chunks)


def _message_sender_from_boomlify(msg):
    """Extract the sender address from a message object (for logging)."""
    if not isinstance(msg, dict):
        return ""
    # Checked against REAL Boomlify response: field is "from_email"
    for key in ("from_email", "sender_email", "from", "sender", "from_address", "mail_from", "reply_to"):
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Nested
    sender_obj = msg.get("sender")
    if isinstance(sender_obj, dict):
        return _coerce_address(sender_obj) or sender_obj.get("name", "")
    return ""


def _message_matches_tensorpix(msg):
    """Only return True if the sender is from @mta.notify.tensorpix.ai."""
    if not isinstance(msg, dict):
        return False
    # Checked against REAL Boomlify response: field is "from_email"
    from_hdr = str(
        msg.get("from_email")
        or msg.get("sender_email")
        or msg.get("from")
        or msg.get("sender")
        or msg.get("from_address")
        or msg.get("mail_from")
        or ""
    ).lower().strip()
    return TENSORPIX_EMAIL_DOMAIN in from_hdr


def extract_verification_link_from_body(body):
    if not body:
        return None
    for pat in VERIFY_LINK_PATTERNS:
        match = re.search(pat, body, re.IGNORECASE)
        if match:
            link = match.group(1) if "href" in pat else match.group(0)
            link = link.strip().rstrip('"\')>').rstrip('"').rstrip("'")
            return link
    return None


def get_verification_link_boomlify(api_key, inbox_id, timeout=300, poll_seconds=5):
    log(f"[BOOMLIFY] Waiting for verification (inbox id {inbox_id})...")
    start = time.time()
    poll_count = 0
    while time.time() - start < timeout:
        try:
            messages = boomlify_list_messages(api_key, inbox_id)
        except Exception as e:
            log(f"[BOOMLIFY] messages poll error: {e}", "DEBUG")
            time.sleep(poll_seconds)
            continue

        poll_count += 1
        if messages:
            log(f"[BOOMLIFY] Got {len(messages)} message(s) in inbox")

        for msg in reversed(messages):
            sender = _message_sender_from_boomlify(msg)
            if not _message_matches_tensorpix(msg):
                log(f"[BOOMLIFY] Skipping non-TensorPix message from: {sender}", "DEBUG")
                continue
            body = _message_text_from_boomlify(msg)
            link = extract_verification_link_from_body(body)
            if link:
                log(f"[BOOMLIFY] Verification link found from: {sender}")
                return link
            else:
                log(f"[BOOMLIFY] TensorPix message received but no verify link found in body (first 200 chars): {body[:200]}", "DEBUG")

        elapsed = int(time.time() - start)
        log(f"[BOOMLIFY] No link yet; retry in {poll_seconds}s (elapsed {elapsed}s, poll #{poll_count})")
        time.sleep(poll_seconds)
    log("[BOOMLIFY] Verification email not received in time", "ERROR")
    return None


class BoomlifyKeyManager:
    """Rotates API keys after ACCOUNTS_PER_BOOMLIFY_KEY mailbox creations per key."""

    def __init__(self, keys, state_ref):
        self.keys = [k.strip() for k in keys if k and str(k).strip()]
        if not self.keys:
            raise ValueError("No Boomlify API keys configured")
        self.state = state_ref
        entry = self.state.setdefault("boomlify", {})
        self.key_index = int(entry.get("key_index", 0))
        self.key_index = min(max(0, self.key_index), len(self.keys) - 1)
        usage = entry.get("usage")
        if isinstance(usage, list):
            self.usage = [int(x) for x in usage]
        else:
            self.usage = [0] * len(self.keys)
        while len(self.usage) < len(self.keys):
            self.usage.append(0)
        self.usage = self.usage[: len(self.keys)]

    def _persist_boomlify(self):
        self.state["boomlify"] = {
            "key_index": self.key_index,
            "usage": self.usage,
        }
        save_state(self.state)

    def api_key_for_next_mailbox(self):
        while self.key_index < len(self.keys):
            if self.usage[self.key_index] < ACCOUNTS_PER_BOOMLIFY_KEY:
                k = self.keys[self.key_index]
                log(
                    f"[BOOMLIFY] Using key #{self.key_index + 1}/{len(self.keys)} "
                    f"({self.usage[self.key_index]}/{ACCOUNTS_PER_BOOMLIFY_KEY} mailboxes on this key)",
                    "INFO",
                )
                return k
            if self.key_index >= len(self.keys) - 1:
                break
            log(
                f"[BOOMLIFY] Key #{self.key_index + 1} reached {ACCOUNTS_PER_BOOMLIFY_KEY} mailboxes; "
                f"switching to key #{self.key_index + 2}",
                "INFO",
            )
            self.key_index += 1
            self._persist_boomlify()
        raise RuntimeError(
            "All Boomlify API keys are exhausted (50 mailbox creations each). Add keys via +API= or BOOMLIFY_API_KEY."
        )

    def record_mailbox_created(self):
        if self.key_index < len(self.keys):
            self.usage[self.key_index] += 1
            self._persist_boomlify()


async def pw_goto(page, url):
    """Navigate with load (not networkidle 閳ワ拷 avoids 30s+ hangs on modern apps)."""
    await page.goto(url, wait_until="load", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)


# ==================== BROWSER AUTOMATION ====================


def _test_proxy_alive(proxy_dict, timeout=None):
    """Quick HTTP check: can this proxy reach the internet at all?

    Returns True if the proxy responded within the timeout, False otherwise.
    This avoids launching Chromium through a dead proxy only to get ERR_TIMED_OUT.
    """
    if timeout is None:
        timeout = PROXY_QUICK_TEST_SECONDS
    server = proxy_dict.get("server", "") if isinstance(proxy_dict, dict) else str(proxy_dict)
    if not server:
        return False
    # Strip protocol for the CONNECT target
    proxy_hostport = server.split("://", 1)[-1]
    try:
        import socket
        host, _, port = proxy_hostport.partition(":")
        port = int(port) if port else 80
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


async def _launch_browser_with_proxy(p, browser_args, proxy_dict):
    """Launch Chromium via proxy AND verify it can actually load a page.

    Returns (browser, proxy_dict) or (None, None) on failure.
    Dead proxies are caught fast 閳ワ拷 not after a 120 s navigation timeout.
    """
    # Pre-test: can we even TCP-connect to the proxy?
    if not _test_proxy_alive(proxy_dict):
        log(f"[PROXY] Dead (no TCP connection): {proxy_dict.get('server', '?')}", "WARNING")
        return None, None

    try:
        browser = await p.chromium.launch(
            args=browser_args, proxy=proxy_dict, timeout=30000,
        )
    except Exception as e:
        log(f"[PROXY] Chromium launch failed: {e}", "WARNING")
        return None, None

    # Navigate to TensorPix with a SHORT timeout to catch slow/dead proxies
    page = await browser.new_page()
    try:
        await page.goto(
            f"{TENSORPIX_URL}/register",
            wait_until="load",
            timeout=PROXY_QUICK_TEST_SECONDS * 1000,
        )
        # If we got here the proxy works 閳ワ拷 close this test page, caller will make a real one
        await page.close()
        return browser, proxy_dict
    except Exception as e:
        log(f"[PROXY] Navigation test failed ({e}): {proxy_dict.get('server', '?')}", "WARNING")
        try:
            await browser.close()
        except Exception:
            pass
        return None, None


async def create_account_and_enhance(email_num, video_file, output_prefix, key_manager):
    """Complete flow: Boomlify inbox -> register -> verify -> login -> upload -> enhance -> download"""
    password = TENSORPIX_PASSWORD
    inbox_id = None
    api_key_used = None

    try:
        api_key_used = key_manager.api_key_for_next_mailbox()
        mail_domain = boomlify_domain_for_account(email_num)
        log(f"[BOOMLIFY] Creating inbox with domain={mail_domain}")
        inbox_id, email_addr = await asyncio.to_thread(
            boomlify_create_inbox, api_key_used, mail_domain
        )
    except Exception as e:
        log(f"[BOOMLIFY] Could not create inbox: {e}", "ERROR")
        return False

    if not inbox_id or not email_addr:
        log(
            "[BOOMLIFY] No inbox id/address after create 閳ワ拷 see ERROR lines above (raw JSON or API error)",
            "ERROR",
        )
        return False

    key_manager.record_mailbox_created()

    log(f"\n{'='*60}")
    log(f"ACCOUNT {email_num}: {email_addr} (Boomlify id {inbox_id})")
    log(f"{'='*60}")

    async with async_playwright() as p:
        try:
            browser_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            user_agent = random.choice(USER_AGENTS)
            viewport_width = random.randint(1200, 1920)
            viewport_height = random.randint(800, 1080)

            browser = None
            last_used_proxy = None
            for attempt in range(max(1, MAX_PROXY_LAUNCH_ATTEMPTS)):
                pc = get_next_proxy()
                if not pc:
                    log("[PROXY] Pool empty 閳ワ拷 falling back to direct", "WARNING")
                    break
                log(f"[PROXY] Attempt {attempt + 1}/{MAX_PROXY_LAUNCH_ATTEMPTS}: testing {pc['server']}")
                browser, last_used_proxy = await _launch_browser_with_proxy(p, browser_args, pc)
                if browser:
                    _mark_proxy_used(pc)
                    log(f"[PROXY] Working proxy: {pc['server']}")
                    break

            if not browser:
                log("[PROXY] All proxies failed or pool empty 閳ワ拷 using direct connection", "WARNING")
                browser = await p.chromium.launch(args=browser_args, timeout=60000)

            page = await browser.new_page(user_agent=user_agent, viewport={'width': viewport_width, 'height': viewport_height})
            page.set_default_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(PLAYWRIGHT_NAV_TIMEOUT_MS)
            log(f"[BROWSER] Using User-Agent: {user_agent}")
            log(f"[BROWSER] Using Viewport: {viewport_width}x{viewport_height}")

            try:
                # === STEP 1: REGISTER ===
                log(f"[STEP 1] Registering...")
                await pw_goto(page, f"{TENSORPIX_URL}/register")
                await page.wait_for_selector("input[type='email']", state="visible", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)

                # Fill email
                await page.fill("input[type='email']", email_addr)
                log(f"[REGISTER] Email: {email_addr}")

                # Fill password
                await page.fill("input[type='password']", password)
                log(f"[REGISTER] Password entered")

                # Click register button
                await page.click("button:has-text('Create'), button:has-text('Register'), button[type='submit']")
                await page.wait_for_timeout(4000)

                # === STEP 2: VERIFY EMAIL ===
                log(f"[STEP 2] Waiting for TensorPix verification via Boomlify...")
                verify_link = await asyncio.to_thread(
                    get_verification_link_boomlify, api_key_used, inbox_id
                )

                if not verify_link:
                    log(f"[VERIFY] No verification link - skipping account", "ERROR")
                    await browser.close()
                    return False

                log(f"[VERIFY] Clicking verification link")
                await pw_goto(page, verify_link)
                await page.wait_for_timeout(2000)

                # === STEP 3: LOGIN ===
                log(f"[STEP 3] Logging in...")
                await pw_goto(page, f"{TENSORPIX_URL}/login")
                await page.wait_for_selector("input[type='email']", state="visible", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)

                await page.fill("input[type='email']", email_addr)
                await page.fill("input[type='password']", password)
                await page.click("button:has-text('Sign In'), button:has-text('Login'), button[type='submit']")
                await page.wait_for_timeout(5000)
                log(f"[LOGIN] Logged in")

                # === STEP 4: DOWNSCALE TO 720P IF NEEDED ===
                log(f"[STEP 4] Checking if video needs downscaling to 720p...")
                actual_video_file, was_downscaled = await asyncio.to_thread(
                    downscale_to_720p_if_needed, video_file
                )
                if was_downscaled:
                    log(f"[DOWNSCALE] Using downscaled file: {actual_video_file}")
                else:
                    actual_video_file = video_file

                # === STEP 5: UPLOAD VIDEO ===
                log(f"[STEP 5] Uploading video ({actual_video_file})...")
                await pw_goto(page, f"{TENSORPIX_URL}/videos")
                await page.wait_for_timeout(3000)

                # Try multiple methods to upload file
                upload_success = False

                # Method 1: Direct file input
                file_input = await page.query_selector("input[type='file']")
                if file_input:
                    try:
                        await file_input.set_input_files(actual_video_file)
                        log(f"[UPLOAD] File selected via input: {actual_video_file}")
                        upload_success = True
                    except Exception as e:
                        log(f"[UPLOAD] File input failed: {e}", "DEBUG")

                # Method 2: Try clicking upload button first
                if not upload_success:
                    try:
                        upload_btn = await page.query_selector("button:has-text('Upload'), button:has-text('Select'), button:has-text('Choose')")
                        if upload_btn:
                            await upload_btn.click()
                            await page.wait_for_timeout(500)
                            file_input = await page.query_selector("input[type='file']")
                            if file_input:
                                await file_input.set_input_files(actual_video_file)
                                log(f"[UPLOAD] File selected via button: {actual_video_file}")
                                upload_success = True
                    except Exception as e:
                        log(f"[UPLOAD] Button method failed: {e}", "DEBUG")

                # Method 3: Try drag-drop area
                if not upload_success:
                    try:
                        drop_area = await page.query_selector("[class*='drop'], [class*='upload'], [class*='drag']")
                        if drop_area:
                            file_input = await page.query_selector("input[type='file']")
                            if file_input:
                                await file_input.set_input_files(actual_video_file)
                                log(f"[UPLOAD] File selected via drag-drop area: {actual_video_file}")
                                upload_success = True
                    except Exception as e:
                        log(f"[UPLOAD] Drag-drop method failed: {e}", "DEBUG")

                if not upload_success:
                    log(f"[UPLOAD] Could not find any upload method.", "ERROR")
                    await browser.close()
                    return False

                # Wait for upload to settle (avoid networkidle 閳ワ拷 hangs on background requests)
                await page.wait_for_timeout(5000)

                # Click on uploaded video to open enhance options
                video_name = os.path.basename(actual_video_file)
                try:
                    await page.wait_for_selector(f"text={video_name}", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
                    await page.click(f"text={video_name}")
                except Exception as e:
                    log(f"[UPLOAD] Could not find uploaded video {video_name}: {e}", "DEBUG")
                    # Try clicking the uploaded video card/thumbnail instead
                    try:
                        video_card = await page.query_selector("[class*='video'], [class*='media'], [class*='item']")
                        if video_card:
                            await video_card.click()
                            log(f"[UPLOAD] Clicked video card/thumbnail")
                        else:
                            await page.click("button:has-text('Enhance')")
                    except Exception as e2:
                        log(f"[UPLOAD] No video card found: {e2}", "WARNING")

                await page.wait_for_timeout(3000)

                # === STEP 6: SELECT 2160p (4K) ===
                log(f"[STEP 6] Selecting 2160p (4K)...")
                try:
                    res_clicked = False
                    # Try multiple selectors for resolution
                    for sel in ["text=2160p (4K)", "text=2160p", "text=4K", "label:has-text('2160p')"]:
                        try:
                            await page.click(sel, timeout=3000)
                            log(f"[QUALITY] Clicked: {sel}")
                            res_clicked = True
                            break
                        except Exception:
                            continue
                    if not res_clicked:
                        # Try opening a dropdown first
                        for dd_sel in ["text=Resolution", "[data-testid='resolution']", "button:has-text('Resolution')"]:
                            try:
                                await page.click(dd_sel, timeout=2000)
                                await page.wait_for_timeout(500)
                                for opt in ["text=2160p (4K)", "text=2160p", "text=4K"]:
                                    try:
                                        await page.click(opt, timeout=2000)
                                        log(f"[QUALITY] Selected via dropdown: {opt}")
                                        res_clicked = True
                                        break
                                    except Exception:
                                        continue
                                if res_clicked:
                                    break
                            except Exception:
                                continue
                    if not res_clicked:
                        log(f"[QUALITY] WARNING: Could not select 2160p 閳ワ拷 may already be default", "WARNING")
                except Exception as e:
                    log(f"[QUALITY] Resolution selection error: {e}", "WARNING")

                await page.wait_for_timeout(1000)

                # === STEP 7: SELECT ANIMATION MODEL ===
                log(f"[STEP 7] Selecting Animation model...")
                try:
                    model_clicked = False
                    # Try multiple selectors for model/preset
                    for sel in ["text=Animation", "label:has-text('Animation')", "[data-value='animation']"]:
                        try:
                            await page.click(sel, timeout=3000)
                            log(f"[MODEL] Clicked: {sel}")
                            model_clicked = True
                            break
                        except Exception:
                            continue
                    if not model_clicked:
                        # Try opening a dropdown first
                        for dd_sel in ["text=General", "text=Preset", "text=Model", "[data-testid='model']", "button:has-text('General')"]:
                            try:
                                await page.click(dd_sel, timeout=2000)
                                await page.wait_for_timeout(500)
                                for opt in ["text=Animation", "label:has-text('Animation')"]:
                                    try:
                                        await page.click(opt, timeout=2000)
                                        log(f"[MODEL] Selected via dropdown: {opt}")
                                        model_clicked = True
                                        break
                                    except Exception:
                                        continue
                                if model_clicked:
                                    break
                            except Exception:
                                continue
                    if not model_clicked:
                        log(f"[MODEL] WARNING: Could not select Animation 閳ワ拷 may already be default", "WARNING")
                except Exception as e:
                    log(f"[MODEL] Model selection error: {e}", "WARNING")

                await page.wait_for_timeout(1000)

                # === STEP 8: START ENHANCEMENT ===
                log(f"[STEP 8] Starting enhancement...")

                enhance_btn = await page.query_selector("button:has-text('Enhance')")
                if enhance_btn:
                    await enhance_btn.click()
                    log(f"[ENHANCE] Enhancement started")
                else:
                    log(f"[ENHANCE] Enhance button not found", "ERROR")
                    await browser.close()
                    return False

                # === STEP 9: WAIT FOR COMPLETION ===
                log(f"[STEP 9] Waiting for enhancement (5-10 minutes)...")
                max_wait = 900
                start = time.time()

                # TensorPix uses Quasar/Vue 閳ワ拷 download button is either:
                # - <a class="buttonPrimary">Download</a> on preview page
                # - <button class="buttonSecondary"> (icon-only, mdi:download) on list page
                # - Page may redirect to /videos/enhanced or /videos/:id/preview
                # So we check multiple signals, not just "button:has-text('Download')"
                download_selectors = [
                    "a.buttonPrimary:has-text('Download')",
                    "a:has-text('Download')",
                    "text=Download",
                    "button.buttonSecondary:not([disabled])",
                    "button[class*='buttonSecondary']:not([disabled])",
                    "[name='mdi:download']",
                    "button:has-text('Download')",
                    "a:has-text('download')",
                ]
                # Also check if page URL indicates we're on enhanced/preview page
                done = False
                while time.time() - start < max_wait:
                    # Check 1: URL changed to enhanced/preview page
                    current_url = page.url
                    if "/enhanced" in current_url or "/preview" in current_url:
                        # Give it a few more seconds for the download button to appear
                        await page.wait_for_timeout(3000)
                        for sel in download_selectors:
                            try:
                                el = await page.query_selector(sel)
                                if el:
                                    # Make sure it's visible and not disabled
                                    is_visible = await el.is_visible()
                                    is_disabled = await el.get_attribute("disabled") if await el.evaluate("e => e.tagName === 'BUTTON' || e.tagName === 'A'") else None
                                    if is_visible and is_disabled is None:
                                        log(f"[ENHANCE] Enhancement complete! (URL: {current_url}, selector: {sel})")
                                        done = True
                                        break
                            except Exception:
                                continue
                        if done:
                            break
                        # URL changed but button not ready yet 閳ワ拷 wait a bit more
                        log(f"[WAIT] On enhanced page but download not ready yet... ({int((time.time()-start)/60)}m)")
                    else:
                        # Check 2: download button appeared on current page
                        for sel in download_selectors:
                            try:
                                el = await page.query_selector(sel)
                                if el:
                                    is_visible = await el.is_visible()
                                    if is_visible:
                                        log(f"[ENHANCE] Enhancement complete! (selector: {sel})")
                                        done = True
                                        break
                            except Exception:
                                continue

                    if done:
                        break

                    elapsed = int(time.time() - start)
                    log(f"[WAIT] Still processing... ({elapsed // 60}m {elapsed % 60}s)")
                    await page.wait_for_timeout(15000)
                else:
                    log(f"[ENHANCE] Enhancement did not complete within {max_wait/60} minutes.", "ERROR")
                    # Take screenshot for debugging
                    try:
                        await page.screenshot(path="debug_enhance_timeout.png")
                        log(f"[DEBUG] Screenshot saved: debug_enhance_timeout.png")
                    except Exception:
                        pass
                    await browser.close()
                    return False

                # === STEP 10: DOWNLOAD ===
                log(f"[STEP 10] Downloading enhanced video...")

                output_file = f"{output_prefix}_{email_num}.mp4"

                # TensorPix download uses window.location.href = url (direct navigation),
                # NOT a proper file download event. So we intercept the URL and download it ourselves.
                try:
                    # Try clicking the download element directly first
                    dl_clicked = False
                    for sel in download_selectors:
                        try:
                            el = await page.query_selector(sel)
                            if not el or not await el.is_visible():
                                continue
                            # Check if it's an <a> tag with href
                            tag = await el.evaluate("e => e.tagName.toLowerCase()")
                            href = await el.get_attribute("href")
                            if tag == "a" and href:
                                # Direct download URL 閳ワ拷 download with urllib
                                log(f"[DOWNLOAD] Found <a> with href: {href[:100]}")
                                import urllib.request as ur
                                dl_req = ur.Request(href, headers={"User-Agent": user_agent})
                                with ur.urlopen(dl_req, timeout=120) as resp:
                                    with open(output_file, "wb") as f:
                                        f.write(resp.read())
                                dl_clicked = True
                                break
                            # Not an <a> tag 閳ワ拷 try clicking and handle download event
                            try:
                                async with page.expect_download(timeout=15000) as dl_info:
                                    await el.click()
                                download = await dl_info.value
                                await download.save_as(output_file)
                                dl_clicked = True
                                break
                            except Exception:
                                pass
                            # If no download event, it might navigate
                            try:
                                async with page.expect_navigation(timeout=15000) as nav_info:
                                    await el.click()
                                new_url = await nav_info.value
                                log(f"[DOWNLOAD] Navigation to: {new_url}")
                                import urllib.request as ur
                                dl_req = ur.Request(str(new_url), headers={"User-Agent": user_agent})
                                with ur.urlopen(dl_req, timeout=120) as resp:
                                    with open(output_file, "wb") as f:
                                        f.write(resp.read())
                                dl_clicked = True
                                break
                            except Exception:
                                pass
                        except Exception as e:
                            log(f"[DOWNLOAD] Selector {sel} failed: {e}", "DEBUG")
                            continue

                    if not dl_clicked:
                        log(f"[DOWNLOAD] Could not click any download button, trying fallback...", "WARNING")
                        # Fallback: navigate to enhanced videos page and try there
                        await pw_goto(page, f"{TENSORPIX_URL}/videos/enhanced")
                        await page.wait_for_timeout(3000)
                        for sel in download_selectors:
                            try:
                                el = await page.query_selector(sel)
                                if not el or not await el.is_visible():
                                    continue
                                tag = await el.evaluate("e => e.tagName.toLowerCase()")
                                href = await el.get_attribute("href")
                                if tag == "a" and href:
                                    log(f"[DOWNLOAD] Fallback download via href")
                                    import urllib.request as ur
                                    dl_req = ur.Request(href, headers={"User-Agent": user_agent})
                                    with ur.urlopen(dl_req, timeout=120) as resp:
                                        with open(output_file, "wb") as f:
                                            f.write(resp.read())
                                    dl_clicked = True
                                    break
                            except Exception:
                                continue

                    if dl_clicked and os.path.exists(output_file) and os.path.getsize(output_file) > 100:
                        log(f"[DOWNLOAD] Saved: {output_file} ({os.path.getsize(output_file)} bytes)")
                    else:
                        log(f"[DOWNLOAD] Download may have failed (file missing or empty)", "WARNING")
                        if os.path.exists(output_file):
                            log(f"[DOWNLOAD] File exists but is {os.path.getsize(output_file)} bytes", "WARNING")
                except Exception as e:
                    log(f"[DOWNLOAD] Download error: {e}", "ERROR")
                    try:
                        await page.screenshot(path="debug_download_error.png")
                    except Exception:
                        pass

                await browser.close()
                return output_file

            except Exception as e:
                log(f"[ERROR] An unexpected error occurred: {e}", "ERROR")
                try:
                    await browser.close()
                except:
                    pass
                return False
        finally:
            if inbox_id and api_key_used:
                try:
                    await asyncio.to_thread(boomlify_delete_inbox, api_key_used, inbox_id)
                except Exception as cleanup_err:
                    log(f"[BOOMLIFY] Inbox cleanup: {cleanup_err}", "DEBUG")

async def run_bot(video_file, num_accounts, start_num=1, boomlify_keys=None):
    """Main function to process video with multiple accounts"""
    if boomlify_keys is None:
        boomlify_keys = []

    log(f"TensorPix Bot - COMBINED & FIXED VERSION")
    log(f"Video: {video_file}")
    log(f"Accounts: {num_accounts} (starting from {start_num})")
    log(f"Password: {TENSORPIX_PASSWORD}")
    log(f"Boomlify: {len(boomlify_keys)} key(s), up to {ACCOUNTS_PER_BOOMLIFY_KEY} mailboxes per key ({BOOMLIFY_MAIL_LIFETIME})")
    log(f"Proxy cooldown: {PROXY_COOLDOWN_SECONDS}s ({PROXY_COOLDOWN_SECONDS/3600:.1f}h)")

    # Check video exists
    if not os.path.exists(video_file):
        log(f"[ERROR] Video not found: {video_file}")
        return

    # Detect quality and split
    quality = get_video_quality(video_file)
    base_name = os.path.splitext(video_file)[0]
    segment_dir = f"{base_name}_segments"

    log(f"[VIDEO] Detected quality: {quality}")
    log(f"[VIDEO] Splitting video into segments...")

    segments = split_video(video_file, segment_dir, quality)

    if not segments:
        log(f"[ERROR] Could not create segments", "ERROR")
        return

    # Process each segment with different account
    enhanced_files = []
    state = load_state()
    try:
        key_manager = BoomlifyKeyManager(boomlify_keys, state)
    except ValueError as e:
        log(f"[ERROR] {e}", "ERROR")
        return

    # Only use saved state if start_num is still 1 (default)
    # If user specified a different start_num via CLI, use that instead
    if start_num == 1:
        start_num = state.get("current_num", start_num)
        log(f"[INFO] Resuming from saved state: {start_num}")
    else:
        # User specified a start number, reset state with new starting point
        state["current_num"] = start_num
        log(f"[INFO] CLI override: Starting from {start_num} (ignoring saved state)")
        save_state(state)

    for i, segment in enumerate(segments):
        email_num = start_num + i

        if email_num > (start_num + num_accounts - 1):
            log(f"[INFO] Reached account limit ({num_accounts})")
            break

        log(f"\n{'#'*60}")
        log(f"PROCESSING SEGMENT {i+1}/{len(segments)} with account {email_num}")
        log(f"{'#'*60}")

        output_prefix = f"{base_name}_4k"
        result = await create_account_and_enhance(email_num, segment, output_prefix, key_manager)

        if result:
            enhanced_files.append(result)
            state["successful"] = state.get("successful", 0) + 1
        else:
            log(f"[FAILED] Segment {i+1}")

        state["current_num"] = email_num + 1
        save_state(state)

        # Wait between accounts with random delay
        if i < len(segments) - 1:
            random_delay(min_sec=30, max_sec=60)
            await asyncio.sleep(1)

    # Merge all enhanced segments
    if enhanced_files:
        final_output = f"{base_name}-4k.mkv"
        log(f"\n[MERGE] Merging {len(enhanced_files)} segments...")

        if merge_videos(enhanced_files, final_output):
            log(f"\n[DONE] Final 4K video: {final_output}")
            log(f"[DONE] Total enhanced segments: {len(enhanced_files)}/{len(segments)}")
        else:
            log("[MERGE] Failed to merge segments", "ERROR")
    else:
        log("\n[DONE] No segments were enhanced successfully", "ERROR")


# ==================== CLI ====================

def parse_args():
    args = sys.argv[1:]
    video_file = None
    num_accounts = 1
    start_num = 1
    api_keys = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("+API=") or arg.startswith("--api="):
            raw = arg.split("=", 1)[1]
            api_keys = [k.strip() for k in raw.split(",") if k.strip()]
        elif arg.startswith("--start="):
            start_num = int(arg.split("=", 1)[1])
        elif video_file is None and not arg.startswith("-"):
            video_file = arg
        elif video_file is not None and not arg.startswith("-") and num_accounts == 1:
            num_accounts = int(arg)
        i += 1

    # Fall back to env var if no keys from CLI
    if not api_keys:
        env = os.environ.get("BOOMLIFY_API_KEY", "")
        api_keys = [k.strip() for k in env.split(",") if k.strip()]

    return video_file, num_accounts, start_num, api_keys


if __name__ == "__main__":
    video_file, num_accounts, start_num, api_keys = parse_args()

    if not video_file:
        print("Usage: python3 tensorpix_bot_combined.py <video_file> <num_accounts> [--api=key1,key2] [--start=N]")
        print("       BOOMLIFY_API_KEY=key python3 tensorpix_bot_combined.py input.mp4 5")
        sys.exit(1)

    if not api_keys:
        print("ERROR: No Boomlify API key. Set BOOMLIFY_API_KEY or pass +API=/--api=")
        sys.exit(1)

    asyncio.run(run_bot(video_file, num_accounts, start_num, api_keys))