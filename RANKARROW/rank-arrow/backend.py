"""
InstaFetch Pro — Backend

An IgAnony-style Instagram profile viewer & downloader. The Instagram account
lives here in the backend (configured via .env); visitors never log in.

Core pieces:
  • Service account: connect via INSTA_USER/INSTA_PASS (auto-login + auto-relogin)
    or INSTA_SESSIONID (sessionid cookie — never hits a checkpoint). Cached to
    .web_session.json and reused across restarts. Checkpoint-aware: won't hammer
    a locked account.
  • Stream-catching cascade (catch_stream): GraphQL doc_id → ?__a=1 → yt-dlp,
    first success wins, with one auto-relogin+retry if the session dropped.
  • Inputs (classify): reel/post/tv link · @username (full profile) · story link.
  • Full profile (profile_full): header + posts/reels grid + stories + highlights,
    with an HTML-scrape fallback when web_profile_info is rate-limited (anchored on
    the target user so it never returns the operator's profile).
  • Downloads (run_job): full (mp4) · audio (mp3) · silent (mp4) · photo (jpg),
    from a shortcode (via cascade) or a direct CDN media_url (stories/highlights).
    ffmpeg is BUNDLED via imageio-ffmpeg — no manual install.
  • Scale hooks: per-account 429s mitigated by a 10-min lookup cache and an
    optional proxy-rotation pool (proxies.txt + _next_proxy) — IgAnony's mechanism.

THE HARD PART = stream catching. Instagram rotates the GraphQL doc_id every
2-4 weeks; that's the main thing that breaks. DOC_IDS is a list tried in order,
with ?__a=1 and yt-dlp as fallbacks → one-line fix when a doc_id expires.
See ARCHITECTURE.md for the full write-up.
"""

import os
import http.cookiejar
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import requests as rq

try:
    from curl_cffi import requests as cc
    _IMPERSONATE = "chrome"
    _HAS_CC = True
except Exception:
    cc = None
    _HAS_CC = False
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("instafetch")

app = FastAPI(title="InstaFetch Pro", version="6.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = Path(__file__).parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass
DOWNLOAD_DIR = BASE_DIR / "downloads"; DOWNLOAD_DIR.mkdir(exist_ok=True)
SESSION_FILE = BASE_DIR / ".web_session.json"
COOKIES_FILE = BASE_DIR / "cookies.txt"
PROXIES_FILE = BASE_DIR / "proxies.txt"

import itertools
_proxy_cycle = None
def _load_proxies():
    global _proxy_cycle
    if PROXIES_FILE.exists():
        proxies = [ln.strip() for ln in PROXIES_FILE.read_text().splitlines()
                   if ln.strip() and not ln.startswith("#")]
        if proxies:
            _proxy_cycle = itertools.cycle(proxies)
            return len(proxies)
    return 0

def _next_proxy() -> Optional[dict]:
    if _proxy_cycle is None:
        return None
    p = next(_proxy_cycle)
    return {"http": p, "https": p}

IG_APP_ID = "936619743392459"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

DOC_IDS = [
    "8845758582119845", "10015901848480474", "24368985919464652",
    "25981206651899035", "9510064595728286",
]

def _resolve_ffmpeg() -> Optional[str]:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and Path(p).exists():
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")

FFMPEG = _resolve_ffmpeg()
if FFMPEG:
    log.info(f"ffmpeg: {FFMPEG}")
else:
    log.warning("ffmpeg not found — audio/silent will fall back to muxed file")

_session = {"sessionid": None, "csrftoken": None, "ds_user_id": None, "username": None}
_state = {"checkpoint_blocked": False}

def _load_session():
    if SESSION_FILE.exists():
        try:
            d = json.loads(SESSION_FILE.read_text())
            if d.get("sessionid"):
                _session.update(d); log.info(f"Resumed session @{d.get('username')}")
        except Exception:
            pass
_load_session()
_PROXY_COUNT = _load_proxies()
if _PROXY_COUNT:
    log.info(f'proxy pool: {_PROXY_COUNT} proxies loaded')


ACCOUNT_FILE = BASE_DIR / "account.json"

def _account_cfg() -> dict:
    """Merge credentials from env vars and account.json (env wins)."""
    cfg = {}
    if ACCOUNT_FILE.exists():
        try:
            cfg = json.loads(ACCOUNT_FILE.read_text())
        except Exception as e:
            log.warning(f"account.json unreadable: {e}")
    return {
        "username": os.getenv("INSTA_USER") or cfg.get("username"),
        "password": os.getenv("INSTA_PASS") or cfg.get("password"),
        "sessionid": os.getenv("INSTA_SESSIONID") or cfg.get("sessionid"),
        "csrftoken": os.getenv("INSTA_CSRFTOKEN") or cfg.get("csrftoken"),
        "ds_user_id": os.getenv("INSTA_DS_USER_ID") or cfg.get("ds_user_id"),
    }

def _bootstrap_account(force: bool = False):
    if _session.get("sessionid") and not force:
        return
    cfg = _account_cfg()

    user, pw = cfg.get("username"), cfg.get("password")
    if user and pw and not _state.get("checkpoint_blocked"):
        try:
            c = web_login(user, pw)
            _session.update({"sessionid": c["sessionid"], "csrftoken": c["csrftoken"],
                             "ds_user_id": c.get("ds_user_id"), "username": user.lstrip("@")})
            SESSION_FILE.write_text(json.dumps(_session))
            log.info(f"Service account connected: @{_session['username']} ✓")
            _state["checkpoint_blocked"] = False
            return True
        except Exception as e:
            msg = str(e).lower()
            if "checkpoint" in msg or "challenge" in msg:
                _state["checkpoint_blocked"] = True
                log.warning("ACCOUNT LOCKED BY INSTAGRAM (checkpoint_required). "
                            "Password login can't continue. FIX: open instagram.com in a "
                            "browser, clear the security check, then copy the 'sessionid' "
                            "cookie into INSTA_SESSIONID in .env. Not retrying password until restart.")
            else:
                log.warning(f"Password login failed ({e}).")

    sid = (cfg.get("sessionid") or "").strip()
    if sid:
        _session.update({
            "sessionid": sid,
            "csrftoken": (cfg.get("csrftoken") or "").strip() or "missing",
            "ds_user_id": (cfg.get("ds_user_id") or "").strip() or None,
            "username": (cfg.get("username") or "session").lstrip("@"),
        })
        SESSION_FILE.write_text(json.dumps(_session))
        _state["checkpoint_blocked"] = False
        log.info("Service account connected via sessionid cookie ✓")
        return True

    if not (user and pw):
        log.info("No embedded account configured. Set INSTA_USER / INSTA_PASS in .env.")
    return False


def ensure_session() -> bool:
    """Auto-relogin: if the session has dropped, transparently reconnect from .env.
    Skips the password retry if the account is checkpoint-locked (sessionid needed)."""
    if _session.get("sessionid"):
        return True
    if _state.get("checkpoint_blocked"):
        cfg = _account_cfg()
        if (cfg.get("sessionid") or "").strip():
            return bool(_bootstrap_account(force=True))
        return False
    log.info("Session missing/expired — attempting auto-relogin from .env…")
    return bool(_bootstrap_account(force=True))


def relogin_and_retry():
    """Force a fresh login (called when a request gets 401/403 mid-use)."""
    _session.update({"sessionid": None})
    return _bootstrap_account(force=True)

jobs: dict[str, dict] = {}

_lookup_cache: dict[str, tuple[float, object]] = {}
_CACHE_TTL = 600

def _cache_get(key: str):
    hit = _lookup_cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None

def _cache_put(key: str, value):
    _lookup_cache[key] = (time.time(), value)


class LoginRequest(BaseModel):
    username: str
    password: str
    two_factor_code: Optional[str] = None

class InfoRequest(BaseModel):
    input: str

class DownloadRequest(BaseModel):
    input: Optional[str] = None
    media_url: Optional[str] = None
    is_video: bool = True
    format: Literal["full", "audio", "silent"] = "full"

class CookiesRequest(BaseModel):
    cookies: str

class SessionRequest(BaseModel):
    sessionid: str
    csrftoken: Optional[str] = None
    username: Optional[str] = None


SC_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", re.I)
STORY_RE = re.compile(r"instagram\.com/stories/([A-Za-z0-9._]+)/?([0-9]+)?", re.I)

def classify(raw: str) -> tuple[str, str]:
    """Return (kind, value): ('reel', shortcode) | ('user', handle) | ('story', handle).
    Handles links, share URLs (.../username/profilecard/), bare usernames, and
    pasted text like 'Tuna (@tuna_the_kat)'."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Please paste a reel/post link or a username.")

    url_match = re.search(r"(?:https?://)?(?:www\.)?instagram\.com/\S+", s, re.I)
    if url_match:
        s = url_match.group(0)
    s = s.split("?")[0].split("#")[0].strip()

    at = re.search(r"@([A-Za-z0-9._]{1,30})", s)
    if at and "instagram.com" not in s.lower():
        return ("user", at.group(1))

    if "instagram.com" not in s.lower():
        token = s.lstrip("@").strip("/")
        if re.fullmatch(r"[A-Za-z0-9_-]{6,30}", token) and "-" in token:
            return ("reel", token)
        if re.fullmatch(r"[A-Za-z0-9._]{1,30}", token):
            if len(token) in (10, 11) and "." not in token and any(c.isupper() for c in token) and any(c.isdigit() for c in token):
                return ("reel", token)
            return ("user", token)
        raise ValueError("Couldn't read that. Paste a reel link or a username.")

    path = re.sub(r"^.*?instagram\.com/", "", s, flags=re.I).strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError("That's the Instagram homepage — paste a profile or reel link.")

    first = parts[0].lower()
    if first == "stories" and len(parts) >= 2:
        return ("story", parts[1])
    if first in ("reel", "reels", "p", "tv") and len(parts) >= 2:
        return ("reel", parts[1])
    if first not in ("explore", "accounts", "directory", "about", "developer", "legal"):
        if re.fullmatch(r"[A-Za-z0-9._]{1,30}", parts[0]):
            return ("user", parts[0])

    raise ValueError("Couldn't read that. Use a reel link, a username, or a story link.")


def humanize(err: str) -> str:
    e = (err or "").lower()
    if _state.get("checkpoint_blocked") or "checkpoint" in e or "challenge" in e:
        return ("The service account is locked by Instagram (security check). To fix: open "
                "instagram.com in a browser, log in and clear the check, then put that browser's "
                "'sessionid' cookie into INSTA_SESSIONID in .env and restart.")
    if "429" in e or "rate-limit" in e or "rate limit" in e or "too many" in e or "please wait" in e:
        return ("Instagram rate-limited profile lookups on your account. Wait ~5–10 minutes, "
                "or just paste a direct reel link (instagram.com/reel/…) — those aren't limited the same way.")
    if "not logged in" in e or "login_required" in e:
        return "Not connected to Instagram. The service account needs a valid session (see Operator setup)."
    if "403" in e or "401" in e or "empty media" in e:
        return "Instagram blocked this request — the service account session isn't valid. Reconnect via Operator setup."
    if "404" in e or "not found" in e:
        return "Not found. Check the link/username — the post may be deleted."
    if "could not find a reel" in e:
        return "Couldn't find a reel for that username. Try a direct reel link, or the profile may have no videos."
    if "private" in e:
        return "That account is private — connect an account that follows it."
    if "no posts" in e or "no reels" in e or "no video" in e:
        return "No downloadable video found there."
    if "no stories" in e or "no active" in e:
        return "That user has no active stories right now."
    if "two_factor" in e or "2fa" in e:
        return "Two-factor required — enter your 6-digit code."
    if "bad password" in e or "incorrect" in e or "invalid" in e:
        return "Wrong username or password."
    if "timeout" in e or "connection" in e:
        return "Network error reaching Instagram. Try again."
    return "Couldn't fetch that. Reconnect your account and try a public reel link."


def http_session():
    if _HAS_CC:
        s = cc.Session(impersonate=_IMPERSONATE)
    else:
        s = rq.Session()
    proxy = _next_proxy()
    if proxy:
        s.proxies.update(proxy)
    s.headers.update({
        "User-Agent": WEB_UA, "X-IG-App-ID": IG_APP_ID, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.instagram.com/", "Origin": "https://www.instagram.com",
    })
    if _session["sessionid"]:
        s.cookies.set("sessionid", _session["sessionid"], domain=".instagram.com")
        s.cookies.set("csrftoken", _session["csrftoken"] or "x", domain=".instagram.com")
        if _session.get("ds_user_id"):
            s.cookies.set("ds_user_id", _session["ds_user_id"], domain=".instagram.com")
        s.headers["X-CSRFToken"] = _session["csrftoken"] or "x"
    elif COOKIES_FILE.exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
            for ck in jar:
                try:
                    s.cookies.set(ck.name, ck.value, domain=ck.domain or ".instagram.com")
                except Exception:
                    pass
            csrf = next((c.value for c in jar if c.name == "csrftoken"), None)
            if csrf: s.headers["X-CSRFToken"] = csrf
        except Exception as ex:
            log.warning(f"cookies.txt: {ex}")
    return s

def _cookiefile_for_ytdlp() -> Optional[str]:
    if COOKIES_FILE.exists():
        return str(COOKIES_FILE)
    if _session["sessionid"]:
        tmp = BASE_DIR / ".tmp_cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\t{_session['sessionid']}")
        lines.append(f".instagram.com\tTRUE\t/\tFALSE\t0\tcsrftoken\t{_session['csrftoken'] or 'x'}")
        if _session.get("ds_user_id"):
            lines.append(f".instagram.com\tTRUE\t/\tFALSE\t0\tds_user_id\t{_session['ds_user_id']}")
        tmp.write_text("\n".join(lines) + "\n")
        return str(tmp)
    return None


def web_login(username: str, password: str, code: str = "") -> dict:
    s = cc.Session(impersonate=_IMPERSONATE) if _HAS_CC else rq.Session()
    proxy = _next_proxy()
    if proxy:
        s.proxies.update(proxy)
    s.headers.update({"User-Agent": WEB_UA, "Accept-Language": "en-US,en;q=0.9"})
    s.get("https://www.instagram.com/accounts/login/", timeout=15)
    csrf = s.cookies.get("csrftoken", "missing")
    enc = f"#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{password}"
    r = s.post("https://www.instagram.com/api/v1/web/accounts/login/ajax/",
               data={"username": username.lstrip("@"), "enc_password": enc,
                     "queryParams": "{}", "optIntoOneTap": "false"},
               headers={"X-CSRFToken": csrf, "X-IG-App-ID": IG_APP_ID,
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": "https://www.instagram.com/accounts/login/"},
               timeout=20)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"login not JSON ({r.status_code})")
    if data.get("two_factor_required"):
        if not code:
            raise RuntimeError("two_factor_required")
        ident = data.get("two_factor_info", {}).get("two_factor_identifier", "")
        r = s.post("https://www.instagram.com/api/v1/web/accounts/login/ajax/two_factor/",
                   data={"username": username.lstrip("@"), "verificationCode": code.strip(),
                         "identifier": ident},
                   headers={"X-CSRFToken": csrf, "X-IG-App-ID": IG_APP_ID,
                            "X-Requested-With": "XMLHttpRequest",
                            "Referer": "https://www.instagram.com/accounts/login/"},
                   timeout=20)
        data = r.json()
    if not data.get("authenticated"):
        raise RuntimeError(data.get("message") or data.get("error_type") or "bad password")
    return {"sessionid": r.cookies.get("sessionid") or s.cookies.get("sessionid"),
            "csrftoken": r.cookies.get("csrftoken") or s.cookies.get("csrftoken") or csrf,
            "ds_user_id": r.cookies.get("ds_user_id") or s.cookies.get("ds_user_id")}


_SEC_HEADERS = {
    "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
    "Sec-Ch-Ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0", "Sec-Ch-Ua-Platform": '"Windows"',
}

def _api_headers(referer="https://www.instagram.com/"):
    """Browser-accurate headers for Instagram private-API GET calls (avoids 429)."""
    return {"X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": "129477", "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest", "Referer": referer,
            "Accept": "*/*", **_SEC_HEADERS}

def _profile_info(handle: str) -> dict:
    """Fetch a public profile. Mimics a browser: warm-up page visit (to collect
    the www-claim token + an HTML fallback), then the JSON API with full headers."""
    handle = handle.lstrip("@").strip("/")
    s = http_session()
    prof_url = f"https://www.instagram.com/{handle}/"
    html, claim = None, "0"

    try:
        warm = s.get(prof_url, timeout=20, headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none", "Upgrade-Insecure-Requests": "1",
            "Sec-Ch-Ua": _SEC_HEADERS["Sec-Ch-Ua"], "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })
        claim = warm.headers.get("x-ig-set-www-claim") or warm.headers.get("X-IG-Set-WWW-Claim") or "0"
        if warm.status_code == 200 and len(warm.text) > 2000:
            html = warm.text
    except Exception as e:
        log.warning(f"[profile] warm-up failed: {e}")

    try:
        r = s.get(f"https://www.instagram.com/api/v1/users/web_profile_info/?username={handle}",
                  headers={"X-IG-App-ID": IG_APP_ID, "X-ASBD-ID": "129477",
                           "X-IG-WWW-Claim": claim, "X-Requested-With": "XMLHttpRequest",
                           "Referer": prof_url, "Accept": "*/*", **_SEC_HEADERS}, timeout=20)
        if r.status_code == 200:
            user = (r.json().get("data") or {}).get("user") or {}
            if user:
                return user
        log.warning(f"[profile] web_profile_info HTTP {r.status_code} — using HTML fallback")
    except Exception as e:
        log.warning(f"[profile] web_profile_info error: {str(e)[:120]}")

    if not html:
        try:
            r2 = s.get(prof_url, timeout=20, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://www.instagram.com/", "Upgrade-Insecure-Requests": "1"})
            if r2.status_code == 200:
                html = r2.text
        except Exception:
            pass
    if not html:
        raise RuntimeError("web_profile_info HTTP 429 (and profile page unavailable)")
    return _parse_profile_html(handle, html)


def _parse_profile_html(handle: str, full: str) -> dict:
    """Parse profile data from already-fetched HTML, scoped to the TARGET user
    (never the logged-in operator), failing cleanly if the target isn't present."""
    anchor = re.search(r'"username"\s*:\s*"' + re.escape(handle) + r'"', full, re.I)
    if not anchor:
        raise RuntimeError("web_profile_info HTTP 429 (target not embedded in HTML)")
    start = max(0, anchor.start() - 1500)
    end = min(len(full), anchor.start() + 60000)
    html = full[start:end]

    def g(pat, default=None, cast=str):
        m = re.search(pat, html)
        return cast(m.group(1)) if m else default
    def clean(x):
        if not x: return x
        try: x = x.encode().decode("unicode_escape")
        except Exception: pass
        return x.replace("\\u0026", "&").replace("\\/", "/").replace('\\"', '"')

    user = {
        "username": handle,
        "full_name": clean(g(r'"full_name":"((?:[^"\\]|\\.)*)"')),
        "biography": clean(g(r'"biography":"((?:[^"\\]|\\.)*)"')),
        "profile_pic_url_hd": clean(g(r'"profile_pic_url_hd":"([^"]+)"') or g(r'"profile_pic_url":"([^"]+)"')),
        "is_private": g(r'"is_private":(true|false)') == "true",
        "is_verified": g(r'"is_verified":(true|false)') == "true",
        "edge_followed_by": {"count": g(r'"edge_followed_by":\{"count":(\d+)', None, int)},
        "edge_follow": {"count": g(r'"edge_follow":\{"count":(\d+)', None, int)},
        "id": g(r'"id":"(\d+)"'),
    }
    edges, seen = [], set()
    for m in re.finditer(r'"shortcode":"([A-Za-z0-9_-]{6,20})"', html):
        sc = m.group(1)
        if sc in seen: continue
        seen.add(sc)
        window = html[m.start():m.start()+600]
        is_vid = '"is_video":true' in window
        thumb = re.search(r'"(?:display_url|thumbnail_src)":"([^"]+)"', window)
        edges.append({"node": {"shortcode": sc, "is_video": is_vid,
                               "display_url": clean(thumb.group(1)) if thumb else None,
                               "thumbnail_src": clean(thumb.group(1)) if thumb else None,
                               "__typename": "GraphVideo" if is_vid else "GraphImage"}})
        if len(edges) >= 12: break
    user["edge_owner_to_timeline_media"] = {
        "count": g(r'"edge_owner_to_timeline_media":\{"count":(\d+)', None, int), "edges": edges}

    if not (user["full_name"] or user["profile_pic_url_hd"] or edges or
            user["edge_followed_by"]["count"] is not None):
        raise RuntimeError("web_profile_info HTTP 429 (HTML had no profile data)")
    log.info(f"[profile] HTML fallback OK for @{handle} ({len(edges)} media)")
    return user


def _profile_info_html(handle: str) -> dict:
    """Standalone HTML fetch+parse (used by other callers)."""
    s = http_session()
    r = s.get(f"https://www.instagram.com/{handle}/", timeout=20, headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.instagram.com/", "Upgrade-Insecure-Requests": "1"})
    if r.status_code != 200:
        raise RuntimeError(f"web_profile_info HTTP 429 / html {r.status_code}")
    return _parse_profile_html(handle, r.text)

def _profile_html_shortcode(handle: str) -> str:
    """Scrape the profile HTML page for an embedded reel/post shortcode.
    Often less throttled than the web_profile_info JSON API."""
    s = http_session()
    r = s.get(f"https://www.instagram.com/{handle}/", timeout=20,
              headers={"Accept": "text/html,application/xhtml+xml"})
    if r.status_code == 429:
        raise RuntimeError("429 profile html")
    if r.status_code != 200:
        raise RuntimeError(f"profile html HTTP {r.status_code}")
    html = r.text
    codes = re.findall(r'"(?:shortcode|code)":"([A-Za-z0-9_-]{6,30})"', html)
    for c in codes:
        if 6 <= len(c) <= 20:
            return c
    m = re.search(r'/(?:reel|p|tv)/([A-Za-z0-9_-]{6,30})/', html)
    if m:
        return m.group(1)
    raise RuntimeError("no shortcode in profile html")


def username_to_shortcode(handle: str) -> str:
    handle = handle.lstrip("@").strip("/")
    cached = _cache_get("user:" + handle)
    if cached:
        return cached
    sc = _username_to_shortcode_uncached(handle)
    _cache_put("user:" + handle, sc)
    return sc


def _username_to_shortcode_uncached(handle: str) -> str:
    saw_429 = False
    try:
        return _profile_html_shortcode(handle)
    except Exception as e:
        if "429" in str(e):
            saw_429 = True
        log.warning(f"[user] html scrape failed: {str(e)[:120]}")
    try:
        user = _profile_info(handle)
        edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
        for e in edges:
            if e["node"].get("is_video"):
                return e["node"]["shortcode"]
        if edges:
            return edges[0]["node"]["shortcode"]
    except Exception as e:
        if "429" in str(e):
            saw_429 = True
        log.warning(f"[user] web_profile_info failed: {str(e)[:120]}")
    try:
        url = f"https://www.instagram.com/{handle}/"
        opts = {"quiet": True, "no_warnings": True, "socket_timeout": 25,
                "extract_flat": True, "playlistend": 10,
                "extractor_args": {"instagram": {"app_id": [IG_APP_ID]}},
                "http_headers": {"User-Agent": WEB_UA}}
        cf = _cookiefile_for_ytdlp()
        if cf: opts["cookiefile"] = cf
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
        for e in [x for x in (d.get("entries") or []) if x]:
            sc = e.get("id")
            if isinstance(sc, str) and re.fullmatch(r"[A-Za-z0-9_-]{6,30}", sc):
                return sc
            m = SC_RE.search(e.get("url", "") or e.get("webpage_url", "") or "")
            if m:
                return m.group(1)
    except Exception as e:
        if "429" in str(e):
            saw_429 = True
        log.warning(f"[user] yt-dlp profile failed: {str(e)[:120]}")
    if saw_429:
        raise RuntimeError("rate-limited 429 on profile lookup")
    raise RuntimeError("could not find a reel for that username")

def story_info(handle: str) -> dict:
    user = _profile_info(handle)
    uid = user.get("id")
    if not uid:
        raise RuntimeError("could not resolve user id")
    s = http_session()
    r = s.get(f"https://www.instagram.com/api/v1/feed/user/{uid}/story/",
              headers=_api_headers(), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"story HTTP {r.status_code}")
    reel = (r.json() or {}).get("reel")
    items = (reel or {}).get("items") or []
    if not items:
        raise RuntimeError("no active stories")
    vids = [it for it in items if it.get("video_versions")]
    item = vids[0] if vids else items[0]
    vv = item.get("video_versions") or []
    img = (((item.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url")
    return {"video_url": vv[0]["url"] if vv else None, "is_video": bool(vv),
            "uploader": handle, "description": "Story",
            "duration": item.get("video_duration"), "thumbnail": img,
            "view_count": None, "like_count": None, "comment_count": None,
            "image_url": None if vv else img}


def _best_url(versions):
    return versions[0]["url"] if versions else None

def _story_items(uid: str) -> list:
    s = http_session()
    r = s.get(f"https://www.instagram.com/api/v1/feed/user/{uid}/story/",
              headers=_api_headers(), timeout=20)
    if r.status_code != 200:
        return []
    items = ((r.json() or {}).get("reel") or {}).get("items") or []
    out = []
    for it in items:
        vv = it.get("video_versions") or []
        img = (((it.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url")
        out.append({"id": str(it.get("pk")), "is_video": bool(vv),
                    "thumbnail": img, "media_url": _best_url(vv) if vv else img,
                    "duration": it.get("video_duration")})
    return out

def _highlights_tray(uid: str) -> list:
    s = http_session()
    r = s.get(f"https://www.instagram.com/api/v1/highlights/{uid}/highlights_tray/",
              headers=_api_headers(), timeout=20)
    if r.status_code != 200:
        return []
    tray = (r.json() or {}).get("tray") or []
    out = []
    for h in tray:
        cover = (((h.get("cover_media") or {}).get("cropped_image_version") or {}).get("url"))
        out.append({"id": str(h.get("id")), "title": h.get("title") or "Highlight", "cover": cover})
    return out

def highlight_items(highlight_id: str) -> list:
    hid = highlight_id if str(highlight_id).startswith("highlight:") else f"highlight:{highlight_id}"
    s = http_session()
    r = s.get("https://www.instagram.com/api/v1/feed/reels_media/",
              params={"reel_ids": hid},
              headers=_api_headers(), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"highlight HTTP {r.status_code}")
    reels = (r.json() or {}).get("reels") or {}
    items = (reels.get(hid) or {}).get("items") or []
    out = []
    for it in items:
        vv = it.get("video_versions") or []
        img = (((it.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url")
        out.append({"id": str(it.get("pk")), "is_video": bool(vv),
                    "thumbnail": img, "media_url": _best_url(vv) if vv else img,
                    "duration": it.get("video_duration")})
    return out

def _parse_feed_item(it: dict) -> dict:
    mt = it.get("media_type")
    node = it
    if mt == 8 and it.get("carousel_media"):
        node = it["carousel_media"][0]
    is_video = (node.get("media_type") == 2) or bool(node.get("video_versions"))
    thumb = (((node.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url")
    is_reel = it.get("product_type") == "clips"
    return {
        "shortcode": it.get("code"),
        "thumbnail": thumb,
        "is_video": is_video,
        "type": "reel" if is_reel else ("video" if is_video else "photo"),
        "view_count": it.get("play_count") or it.get("view_count"),
        "like_count": it.get("like_count"),
        "comment_count": it.get("comment_count"),
    }

def _user_media(uid: str, max_items: int = 120) -> list:
    """Fetch a user's posts/reels from the feed endpoint, paginating with the
    max_id cursor until we have everything (up to max_items)."""
    s = http_session()
    host = "https://www.instagram.com"
    base = None
    for h in ("https://www.instagram.com", "https://i.instagram.com"):
        try:
            probe = s.get(f"{h}/api/v1/feed/user/{uid}/?count=12",
                          headers=_api_headers(), timeout=20)
            if probe.status_code == 200:
                base = h
                first = probe.json() or {}
                break
        except Exception as e:
            log.warning(f"[user_media] probe {h}: {str(e)[:80]}")
    if not base:
        return []

    out, data, pages = [], first, 0
    while True:
        items = (data or {}).get("items") or []
        for it in items:
            out.append(_parse_feed_item(it))
        pages += 1
        cursor = (data or {}).get("next_max_id")
        more = (data or {}).get("more_available")
        if not cursor or not more or len(out) >= max_items or pages >= 12:
            break
        try:
            r = s.get(f"{base}/api/v1/feed/user/{uid}/?count=12&max_id={cursor}",
                      headers=_api_headers(), timeout=20)
            if r.status_code != 200:
                break
            data = r.json() or {}
        except Exception as e:
            log.warning(f"[user_media] page {pages}: {str(e)[:80]}")
            break
    log.info(f"[profile] user-feed returned {len(out)} media across {pages} page(s)")
    return out[:max_items]


def profile_full(handle: str) -> dict:
    handle = handle.lstrip("@").strip("/")
    user = _profile_info(handle)
    if not user:
        raise RuntimeError("profile not found")
    uid = user.get("id")
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
    media = []
    for e in edges:
        n = e.get("node") or {}
        is_reel = (n.get("product_type") == "clips") or (n.get("is_video") and n.get("__typename") == "GraphVideo")
        media.append({
            "shortcode": n.get("shortcode"),
            "thumbnail": n.get("thumbnail_src") or n.get("display_url"),
            "is_video": bool(n.get("is_video")),
            "type": "reel" if is_reel else ("video" if n.get("is_video") else "photo"),
            "view_count": n.get("video_view_count"),
            "like_count": (n.get("edge_liked_by") or n.get("edge_media_preview_like") or {}).get("count"),
            "comment_count": (n.get("edge_media_to_comment") or {}).get("count"),
        })
    is_private = user.get("is_private")
    if not media and uid and not is_private:
        try:
            media = _user_media(uid)
        except Exception as e:
            log.warning(f"[profile] user-feed media failed: {e}")
    stories, highlights = [], []
    if not is_private and uid:
        try: stories = _story_items(uid)
        except Exception as e: log.warning(f"[stories] {e}")
        try: highlights = _highlights_tray(uid)
        except Exception as e: log.warning(f"[highlights] {e}")
    return {
        "username": user.get("username"), "full_name": user.get("full_name"),
        "biography": user.get("biography"), "profile_pic": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "is_verified": user.get("is_verified"), "is_private": is_private,
        "followers": (user.get("edge_followed_by") or {}).get("count"),
        "following": (user.get("edge_follow") or {}).get("count"),
        "posts_count": (user.get("edge_owner_to_timeline_media") or {}).get("count"),
        "media": media, "stories": stories, "highlights": highlights,
        "user_id": uid,
    }


def _cap(m):
    try: return m["edge_media_to_caption"]["edges"][0]["node"]["text"]
    except Exception: return ""

def _graphql(shortcode: str) -> dict:
    s = http_session(); last = "no doc_id worked"
    for doc in DOC_IDS:
        try:
            r = s.post("https://www.instagram.com/graphql/query",
                       data={"variables": json.dumps({"shortcode": shortcode}), "doc_id": doc},
                       timeout=20)
            if r.status_code == 403:
                raise RuntimeError("403 not logged in")
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"; continue
            m = ((r.json() or {}).get("data") or {})
            m = m.get("xdt_shortcode_media") or m.get("shortcode_media")
            if not m: last = "no media"; continue
            if not m.get("is_video") and m.get("edge_sidecar_to_children"):
                for e in m["edge_sidecar_to_children"].get("edges", []):
                    if e["node"].get("is_video"):
                        m = {**m, **e["node"]}; break
            if not m.get("video_url"):
                img = m.get("display_url") or (((m.get("display_resources") or [{}])[-1]).get("src"))
                if not img:
                    last = "photo post (no image)"; continue
                log.info(f"[graphql] ✓ {doc} (photo)")
                return {"video_url": None, "image_url": img, "is_video": False,
                        "uploader": (m.get("owner") or {}).get("username") or "instagram",
                        "description": _cap(m), "thumbnail": img,
                        "like_count": (m.get("edge_media_preview_like") or {}).get("count"),
                        "comment_count": (m.get("edge_media_to_comment") or {}).get("count")}
            log.info(f"[graphql] ✓ {doc}")
            return {"video_url": m["video_url"], "is_video": True,
                    "uploader": (m.get("owner") or {}).get("username") or "instagram",
                    "description": _cap(m), "duration": m.get("video_duration"),
                    "view_count": m.get("video_view_count"),
                    "like_count": (m.get("edge_media_preview_like") or {}).get("count"),
                    "comment_count": (m.get("edge_media_to_comment") or {}).get("count"),
                    "thumbnail": m.get("display_url")}
        except RuntimeError:
            raise
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"graphql: {last}")

def _a1(shortcode: str) -> dict:
    s = http_session()
    for ep in (f"https://www.instagram.com/reel/{shortcode}/?__a=1&__d=dis",
               f"https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis"):
        try:
            r = s.get(ep, timeout=20)
            if r.status_code != 200: continue
            items = (r.json() or {}).get("items") or []
            if not items: continue
            it = items[0]
            vv = it.get("video_versions") or []
            if not vv and it.get("carousel_media"):
                for c in it["carousel_media"]:
                    if c.get("video_versions"): vv = c["video_versions"]; break
            if not vv: continue
            cap = (it.get("caption") or {}).get("text", "") if it.get("caption") else ""
            thumb = (((it.get("image_versions2") or {}).get("candidates") or [{}])[0]).get("url")
            log.info("[a1] ✓")
            return {"video_url": vv[0]["url"], "is_video": True,
                    "uploader": (it.get("user") or {}).get("username") or "instagram",
                    "description": cap, "duration": it.get("video_duration"),
                    "view_count": it.get("play_count"), "like_count": it.get("like_count"),
                    "comment_count": it.get("comment_count"), "thumbnail": thumb}
        except Exception:
            continue
    raise RuntimeError("a1: no video")

def _ytdlp(shortcode: str) -> dict:
    url = f"https://www.instagram.com/reel/{shortcode}/"
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 25,
            "extractor_args": {"instagram": {"app_id": [IG_APP_ID]}},
            "http_headers": {"User-Agent": WEB_UA}}
    cf = _cookiefile_for_ytdlp()
    if cf: opts["cookiefile"] = cf
    with yt_dlp.YoutubeDL(opts) as ydl:
        d = ydl.extract_info(url, download=False)
    if d.get("_type") == "playlist" and d.get("entries"):
        d = next(e for e in d["entries"] if e)
    vurl = d.get("url")
    if not vurl:
        for f in reversed(d.get("formats") or []):
            if f.get("vcodec") not in (None, "none"): vurl = f["url"]; break
    if not vurl:
        raise RuntimeError("yt-dlp no url")
    log.info("[yt-dlp] ✓")
    return {"video_url": vurl, "is_video": True,
            "uploader": d.get("uploader") or "instagram",
            "description": d.get("description") or "", "duration": d.get("duration"),
            "view_count": d.get("view_count"), "like_count": d.get("like_count"),
            "comment_count": d.get("comment_count"), "thumbnail": d.get("thumbnail")}

def catch_stream(shortcode: str) -> tuple[dict, str]:
    errs = []
    for name, fn in (("graphql", _graphql), ("a1", _a1), ("yt-dlp", _ytdlp)):
        try:
            return fn(shortcode), name
        except Exception as e:
            log.warning(f"[{name}] {str(e)[:140]}"); errs.append(f"{name}: {e}")
    joined = " | ".join(errs)
    if any(k in joined for k in ("403", "401", "login_required", "not logged", "empty media")):
        if relogin_and_retry():
            log.info("Re-logged in; retrying stream catch…")
            for name, fn in (("graphql", _graphql), ("a1", _a1), ("yt-dlp", _ytdlp)):
                try:
                    return fn(shortcode), name
                except Exception as e:
                    log.warning(f"[{name}/retry] {str(e)[:120]}")
    raise RuntimeError(joined)


def resolve(kind: str, value: str) -> tuple[dict, str]:
    ensure_session()
    if kind == "reel":
        return catch_stream(value)
    if kind == "user":
        return catch_stream(username_to_shortcode(value))
    if kind == "story":
        return story_info(value), "story"
    raise RuntimeError("unknown input")


def fetch_to_file(video_url: str, out: Path, is_video: bool = True) -> Path:
    ext = ".mp4" if is_video else ".jpg"
    fn = out / f"ig_{uuid.uuid4().hex[:10]}{ext}"
    s = http_session()
    r = s.get(video_url, stream=True, timeout=120)
    try:
        r.raise_for_status()
        with open(fn, "wb") as f:
            for chunk in r.iter_content(1 << 15):
                if chunk: f.write(chunk)
    finally:
        try: r.close()
        except Exception: pass
    if fn.stat().st_size < 1000:
        fn.unlink(missing_ok=True)
        raise RuntimeError("download too small (stream URL expired) — retry")
    return fn

def to_mp3(src: Path) -> Path:
    if not FFMPEG:
        raise RuntimeError("ffmpeg unavailable")
    dst = src.with_suffix(".mp3")
    p = subprocess.run([FFMPEG, "-i", str(src), "-vn", "-acodec", "libmp3lame",
                        "-q:a", "0", str(dst), "-y"], capture_output=True)
    if p.returncode != 0 or not dst.exists():
        raise RuntimeError("audio conversion failed: " + p.stderr.decode(errors="ignore")[-200:])
    src.unlink(missing_ok=True); return dst

def to_silent(src: Path) -> Path:
    if not FFMPEG:
        raise RuntimeError("ffmpeg unavailable")
    dst = src.with_name(src.stem + "_silent.mp4")
    p = subprocess.run([FFMPEG, "-i", str(src), "-an", "-c:v", "copy", str(dst), "-y"],
                       capture_output=True)
    if p.returncode != 0 or not dst.exists():
        raise RuntimeError("silent conversion failed: " + p.stderr.decode(errors="ignore")[-200:])
    src.unlink(missing_ok=True); return dst

def run_job(job_id: str, kind: str, value: str, fmt: str,
            media_url: Optional[str] = None, is_video: bool = True):
    jobs[job_id]["status"] = "processing"
    try:
        if media_url:
            src = fetch_to_file(media_url, DOWNLOAD_DIR, is_video=is_video)
            if not is_video:
                out, engine = src, "direct"
            else:
                out = to_mp3(src) if fmt == "audio" else to_silent(src) if fmt == "silent" else src
                engine = "direct"
        else:
            info, engine = resolve(kind, value)
            if info.get("image_url") and not info.get("video_url"):
                out = fetch_to_file(info["image_url"], DOWNLOAD_DIR, is_video=False)
            else:
                if not info.get("video_url"):
                    raise RuntimeError("no video (photo-only post/story)")
                src = fetch_to_file(info["video_url"], DOWNLOAD_DIR)
                out = to_mp3(src) if fmt == "audio" else to_silent(src) if fmt == "silent" else src
        jobs[job_id].update({"status": "done", "file": str(out), "engine": engine,
                             "completed_at": datetime.utcnow().isoformat()})
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": humanize(str(e))})


@app.get("/api/auth/status")
def auth_status():
    return {"logged_in": bool(_session["sessionid"] or COOKIES_FILE.exists()),
            "username": _session.get("username"), "ffmpeg": bool(FFMPEG),
            "checkpoint_blocked": bool(_state.get("checkpoint_blocked"))}

@app.post("/api/auth/login")
def login(b: LoginRequest):
    global _session
    if not b.username or not b.password:
        raise HTTPException(400, "Username and password are required.")
    try:
        c = web_login(b.username, b.password, b.two_factor_code or "")
    except RuntimeError as e:
        if "two_factor_required" in str(e):
            raise HTTPException(401, "Two-factor is on — enter your 6-digit code and try again.")
        raise HTTPException(401, humanize(str(e)))
    _session.update({"sessionid": c["sessionid"], "csrftoken": c["csrftoken"],
                     "ds_user_id": c.get("ds_user_id"), "username": b.username.lstrip("@")})
    SESSION_FILE.write_text(json.dumps(_session))
    log.info(f"Logged in @{_session['username']}")
    return {"ok": True, "username": _session["username"], "ffmpeg": bool(FFMPEG)}

@app.post("/api/auth/logout")
def logout():
    global _session
    _session = {"sessionid": None, "csrftoken": None, "ds_user_id": None, "username": None}
    SESSION_FILE.unlink(missing_ok=True)
    return {"ok": True}

@app.post("/api/auth/session")
def connect_session(b: SessionRequest):
    """Connect by pasting a browser sessionid cookie — the reliable, no-checkpoint path."""
    global _session
    sid = (b.sessionid or "").strip().strip('"')
    if not sid or len(sid) < 10:
        raise HTTPException(400, "Paste the full 'sessionid' cookie value from your browser.")
    _session.update({"sessionid": sid,
                     "csrftoken": (b.csrftoken or "").strip() or "missing",
                     "ds_user_id": sid.split("%")[0] if "%" in sid else None,
                     "username": (b.username or "session").lstrip("@")})
    try:
        s = http_session()
        r = s.get("https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram",
                  headers={"X-IG-App-ID": IG_APP_ID}, timeout=15)
        if r.status_code in (401, 403):
            _session.update({"sessionid": None, "username": None})
            raise HTTPException(401, "That sessionid was rejected by Instagram. Copy a fresh one from a logged-in browser.")
    except HTTPException:
        raise
    except Exception:
        pass
    _state["checkpoint_blocked"] = False
    SESSION_FILE.write_text(json.dumps(_session))
    log.info(f"Connected via sessionid (@{_session['username']}) ✓")
    return {"ok": True, "username": _session["username"], "ffmpeg": bool(FFMPEG)}

@app.post("/api/auth/cookies")
def save_cookies(b: CookiesRequest):
    if "instagram" not in (b.cookies or "").lower():
        raise HTTPException(400, "That doesn't look like an Instagram cookies.txt.")
    COOKIES_FILE.write_text(b.cookies, encoding="utf-8")
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "ok", "yt_dlp": yt_dlp.version.__version__,
            "session": bool(_session["sessionid"]), "ffmpeg": bool(FFMPEG),
            "proxies": _PROXY_COUNT, "tls_impersonation": _HAS_CC}

@app.get("/api/img")
def proxy_image(u: str):
    """Proxy an Instagram CDN image through the backend so the browser can show
    it (Instagram blocks direct hotlinking of avatars/thumbnails)."""
    from urllib.parse import unquote
    url = unquote(u or "")
    if "cdninstagram" not in url and "fbcdn" not in url and "instagram" not in url:
        raise HTTPException(400, "Only Instagram CDN images may be proxied.")
    try:
        s = http_session()
        r = s.get(url, timeout=20)
        if r.status_code != 200:
            raise HTTPException(404, "image unavailable")
        from fastapi.responses import Response
        return Response(content=r.content,
                        media_type=r.headers.get("Content-Type", "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=3600"})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502, "image fetch failed")

@app.post("/api/info")
def get_info(b: InfoRequest):
    try:
        kind, value = classify(b.input)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        info, engine = resolve(kind, value)
    except Exception as e:
        raise HTTPException(422, humanize(str(e)))
    return {"success": True, "engine": engine, "kind": kind,
            "uploader": info.get("uploader"), "description": (info.get("description") or "")[:300],
            "duration": info.get("duration"), "view_count": info.get("view_count"),
            "like_count": info.get("like_count"), "comment_count": info.get("comment_count"),
            "thumbnail": info.get("thumbnail"), "has_video": bool(info.get("video_url")),
            "ffmpeg": bool(FFMPEG)}

@app.post("/api/profile")
def get_profile(b: InfoRequest):
    """Full profile view: header + posts/reels grid + stories + highlights."""
    try:
        kind, value = classify(b.input)
    except ValueError as e:
        raise HTTPException(400, str(e))
    handle = value if kind in ("user", "story") else None
    if not handle:
        raise HTTPException(400, "Enter a username to see a full profile (or paste a reel link to grab one reel).")
    ensure_session()
    try:
        return {"success": True, **profile_full(handle)}
    except Exception as e:
        raise HTTPException(422, humanize(str(e)))

@app.get("/api/highlight/{highlight_id}")
def get_highlight(highlight_id: str):
    try:
        return {"success": True, "items": highlight_items(highlight_id)}
    except Exception as e:
        raise HTTPException(422, humanize(str(e)))

@app.post("/api/download")
def download_media(b: DownloadRequest, bg: BackgroundTasks):
    if b.format in ("audio", "silent") and not FFMPEG:
        raise HTTPException(422, "ffmpeg missing. Run: pip install imageio-ffmpeg")
    job_id = uuid.uuid4().hex
    if b.media_url:
        jobs[job_id] = {"id": job_id, "kind": "direct", "value": "", "format": b.format,
                        "status": "queued", "created_at": datetime.utcnow().isoformat()}
        bg.add_task(run_job, job_id, "direct", "", b.format, b.media_url, b.is_video)
        return {"job_id": job_id}
    try:
        kind, value = classify(b.input or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    jobs[job_id] = {"id": job_id, "kind": kind, "value": value, "format": b.format,
                    "status": "queued", "created_at": datetime.utcnow().isoformat()}
    bg.add_task(run_job, job_id, kind, value, b.format)
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
def job_status(job_id: str):
    if job_id not in jobs: raise HTTPException(404, "Job not found")
    return jobs[job_id]

@app.get("/api/download/{job_id}/file")
def serve_file(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done": raise HTTPException(404, "File not ready")
    path = Path(job["file"])
    if not path.exists(): raise HTTPException(404, "File missing")
    if path.suffix == ".mp3": media = "audio/mpeg"
    elif path.suffix == ".jpg": media = "image/jpeg"
    else: media = "video/mp4"
    return FileResponse(path, media_type=media, filename=path.name)

@app.delete("/api/download/{job_id}/file")
def cleanup(job_id: str):
    job = jobs.pop(job_id, None)
    if job and job.get("file"): Path(job["file"]).unlink(missing_ok=True)
    return {"deleted": True}

try:
    _bootstrap_account()
except Exception as _e:
    log.warning(f'bootstrap: {_e}')
