# Rank Arrow — Architecture

A self-hosted Instagram profile viewer and downloader, built to demonstrate the
core of an IgAnony-style product: **catching the media stream reliably**, viewing
public profiles, and serving media in multiple formats — with the account running
**server-side** so visitors never log in.

---

## 1. The problem the CEO framed

> "Everything else is simple — the only hard part is catching the stream, and
> keeping it caught, because Instagram changes its streams regularly. You need a
> fake account and stream catching."

That is exactly right. Three facts drive the design:

1. **Instagram locks media behind a session.** Anonymous requests to its public
   endpoints return `403 / 404 / "empty media response"`. Every request needs a
   valid `sessionid` cookie attached.
2. **The account lives in the backend, not the UI.** IgAnony-style tools run a
   server-side account so the visitor just types a username. We do the same.
3. **Instagram rotates its extraction surface.** The GraphQL `doc_id` used to read
   a post changes every ~2-4 weeks. The defense is *redundancy*, not one clever
   call.

---

## 2. Stream-catching cascade  (`catch_stream`)

Every shortcode runs through three independent engines, in order; the first that
returns a usable video URL wins:

```
shortcode ─► 1. _graphql()  POST /graphql/query  + X-IG-App-ID + doc_id (DOC_IDS list)
             │     └─ 403 / doc_id rotated ─┐
             ▼                               │
          2. _a1()  GET /reel/<sc>/?__a=1&__d=dis   (different endpoint shape)
             │     └─ fails ─────────────────┤
             ▼                                │
          3. _ytdlp()  community extractor, app_id arg, session cookies, auto-updated
                                              │
                       first success ◄────────┘  ─►  direct CDN MP4 URL
```

If **all three** fail with an auth-type error (`403 / login_required / empty
media`), `catch_stream` calls `relogin_and_retry()` once and runs the cascade
again — so a silently-expired session repairs itself mid-request instead of
failing the user.

**Why this "never breaks":**

| Failure mode | What absorbs it |
|---|---|
| `doc_id` rotated | `DOC_IDS` is a list tried in order; add the new one on top (1-line fix) |
| GraphQL surface changes | `?__a=1` fallback uses a different endpoint shape |
| Both IG endpoints change | yt-dlp's Instagram extractor (large community, patched fast) |
| yt-dlp goes stale | `update_engines.sh` cron upgrades it daily |
| Session expired mid-use | one automatic relogin + cascade retry |

Refreshing the `doc_id`: devtools -> Network -> filter `graphql` -> copy the
`doc_id` field -> paste at the top of `DOC_IDS` in `backend.py`.

---

## 3. Service-account model & auth  (`_bootstrap_account`, `ensure_session`)

The account is configured once via `.env` / `account.json` and connected on
startup. The visitor-facing UI has no login.

Connection order (in `_bootstrap_account`):

1. **Username + password** (`INSTA_USER` / `INSTA_PASS`) via Instagram's web
   browser login endpoint (`/api/v1/web/accounts/login/ajax/`) — the same request
   the real site makes. Auto-login on startup; **auto-relogin** later if the
   session drops.
2. **sessionid cookie** (`INSTA_SESSIONID`) — used directly, skipping the login
   flow entirely, so it never triggers a checkpoint. This is the fallback when a
   password login is challenged, and the most reliable method overall.

**Checkpoint handling.** If the password login returns `checkpoint_required`, the
app sets a `checkpoint_blocked` flag and **stops retrying that password** (so it
doesn't hammer a locked account), surfacing a clear "use a sessionid" message.
Connecting a valid sessionid clears the flag.

The session is cached to `.web_session.json` and reused across restarts.
`ensure_session()` runs before every fetch and transparently reconnects from
`.env` if the session is gone.

---

## 4. Inputs & resolution  (`classify`, `resolve`)

`classify()` maps any input to `(kind, value)`:

| Input | kind | Resolution path |
|---|---|---|
| reel / post / tv link or bare shortcode | `reel` | `catch_stream(shortcode)` |
| `@username` / profile link | `user` | `username_to_shortcode()` -> `catch_stream()` |
| story link / username | `story` | `story_info()` -> session story feed |

`username_to_shortcode()` (cached 10 min) tries, in order: profile-HTML scrape ->
`web_profile_info` JSON API -> yt-dlp profile listing. It distinguishes a real
`429` rate-limit from "not found" so the error message is accurate.

---

## 5. Full profile view  (`profile_full`)

Typing a username returns a complete profile, reusing the session plus a couple of
extra endpoints:

```
@username ─► web_profile_info  ──► header (avatar, bio, followers, verified, private)
                                └─► edge_owner_to_timeline_media  ──► Posts + Reels grid
          ─► feed/user/<id>/story/            ──► Stories grid       (_story_items)
          ─► highlights/<id>/highlights_tray/ ──► Highlights tray    (_highlights_tray)
                                              └─ feed/reels_media/?reel_ids=highlight:<id>
                                                 ──► highlight items (highlight_items)
```

**Media grid (`_user_media`).** `web_profile_info` returns the post *count* but
often trims the actual media edges, so the grid is populated from the dedicated
feed endpoint `feed/user/<id>/`, **paginated** via the `next_max_id` cursor to
pull the full post/reel history (capped at ~120 items / 12 pages to stay polite).
It handles photos, videos, and carousels.

**Image proxy (`/api/img`).** Browsers can't hotlink Instagram CDN images
(avatars, thumbnails) — the requests are blocked. Every image is therefore routed
through a backend proxy that fetches it server-side and streams it to the browser.

**Rate-limit fallback (`_parse_profile_html`).** `web_profile_info` is the
endpoint Instagram throttles hardest per account. When it returns `429`,
`_profile_info` falls back to scraping the public profile **HTML page** (throttled
far less). That scrape is **anchored on the target username** and reads only the
data block around it — so it returns the requested profile, never the logged-in
operator's, and fails cleanly if the target isn't present rather than showing
wrong data.

Each grid item carries either a **shortcode** (posts/reels -> stream-catching
cascade) or a **direct CDN media_url** (stories/highlights -> downloaded directly).

---

## 6. Download & convert  (`run_job`, `fetch_to_file`, `to_mp3`, `to_silent`)

Downloads are async jobs (`/api/download` queues, `/api/status/{id}` polls,
`/api/download/{id}/file` serves):

```
job ─► (shortcode) resolve() -> catch_stream() -> video_url
       (or direct media_url for stories/highlights/grid items)
   ─► fetch_to_file() streams the CDN file
   ─► format:
        full   -> the mp4 as-is
        audio  -> to_mp3()    (ffmpeg -vn -> mp3)
        silent -> to_silent() (ffmpeg -an -c:v copy -> mp4)
        photo  -> saved as .jpg
```

**ffmpeg is bundled** through `imageio-ffmpeg` (`_resolve_ffmpeg()`), so audio and
silent conversions work with no manual install. The download panel in the UI keeps
the resolved media in state, so a visitor can grab full, then audio, then silent
from one fetch without re-resolving.

---

## 7. Rate limits — and how IgAnony actually scales

The one architectural gap between this single-account local build and IgAnony:

- **The extraction logic is identical.** IgAnony, AnonyIG, and this tool call the
  same public Instagram endpoints with the same `X-IG-App-ID`. Public profiles only.
- **The difference is infrastructure.** IgAnony runs a **pool of rotating
  residential proxies + many accounts**, so traffic is spread across hundreds of
  IPs and no single one trips a `429`. A single account on one IP eventually gets
  throttled on profile lookups (reel links are far lighter and rarely affected).

**Mitigations already in this build:**

1. **Lookup cache** (`_lookup_cache`, 10-min TTL) — repeating a username search
   doesn't re-hit Instagram, so `429`s don't accumulate.
2. **Proxy-rotation layer** (`proxies.txt` + `_next_proxy()`) — every request
   cycles through the listed proxies. Empty file = direct connection. This is the
   exact mechanism IgAnony uses, wired in and ready.

So the path from this build to IgAnony's scale is **not a rewrite** — it's
populating `proxies.txt` and rotating a few accounts behind the same cascade.

---

## 8. Request flow (end to end)

```
Browser (frontend.html)
   │  POST /api/profile {input:"@user"}   or   POST /api/info {input:"reel link"}
   ▼
FastAPI (backend.py)
   │  ensure_session()  -> classify()  -> resolve()/profile_full()
   │  cascade / profile endpoints -> metadata + media
   ▼
   │  POST /api/download {input|media_url, format}   -> queues a job
   │  background: fetch CDN file -> ffmpeg (full|audio|silent)
   │  GET /api/status/{id}        -> poll until done
   │  GET /api/download/{id}/file -> stream file to browser
```

State is in-memory (`jobs`, `_session`, `_state`, `_lookup_cache`) — right for a
single-operator desktop tool. For multi-user production: move sessions/jobs to
Redis, files to object storage, and add the proxy + account pool from section 7.

---

## 9. API surface

| Method | Path | Purpose |
|---|---|---|
| GET  | `/api/auth/status`        | connected? username? ffmpeg? checkpoint? |
| POST | `/api/auth/session`       | connect by pasting a sessionid (operator) |
| POST | `/api/auth/login`         | connect by username/password (operator) |
| POST | `/api/auth/logout`        | clear the session |
| POST | `/api/info`               | metadata for a reel/username/story |
| POST | `/api/profile`            | full profile (header + grids + stories + highlights) |
| GET  | `/api/img?u=<url>`        | proxy an Instagram CDN image so the browser can show it |
| GET  | `/api/highlight/{id}`     | items inside a highlight |
| POST | `/api/download`           | queue a download (input or direct media_url) |
| GET  | `/api/status/{id}`        | poll a job |
| GET  | `/api/download/{id}/file` | fetch the finished file |
| GET  | `/health`                 | yt-dlp version, ffmpeg, session, proxies, tls_impersonation |

---

## 10. Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + Uvicorn | async, tiny, fast to stand up |
| Extraction | requests + yt-dlp | direct endpoint control + a maintained fallback |
| Media | ffmpeg via `imageio-ffmpeg` | bundled binary — audio/silent work with no install |
| Frontend | single `frontend.html` | zero build step; vanilla JS, Tabler icons |

---

## 11. Honest limitations

- Public content only (same as every tool in this category, IgAnony included).
- Username/story/profile lookups are rate-limited *per account* without a proxy
  pool; the HTML fallback helps but modern profile pages are largely JS-rendered,
  so the JSON API on a non-throttled account is the reliable source.
- CDN URLs are time-signed and expire in a few hours, so the app downloads
  immediately rather than storing links.
- Single-process in-memory state; not horizontally scalable as-is (see section 8).

---

## 12. The real blocker: TLS fingerprinting (added)

After header tuning still left `web_profile_info` returning `429` on fresh
accounts, the actual cause turned out to be **TLS fingerprinting**. Instagram
inspects the TLS/JA3 handshake; Python's `requests`/`httpx` have a signature that
is flagged as automated, so the most-defended endpoints (profile lookups) return
`429` instantly — regardless of headers or account age. Reel/GraphQL calls are
less aggressively filtered, which is why they kept working while username search
failed (the exact symptom we saw).

**Fix:** all Instagram HTTP now goes through **`curl_cffi` with Chrome
impersonation** (`impersonate="chrome"`), which reproduces a real Chrome TLS
handshake. This is the production-standard fix cited by ScrapFly and SocialCrawl
("any Python Instagram scraper should start from curl_cffi, never requests").
`http_session()` returns a curl_cffi session when the package is present and
falls back to `requests` otherwise. `/health` reports `tls_impersonation`.

This is the lightweight equivalent of the CEO's "use a headless browser"
suggestion: a headless browser works because it has a real browser's TLS
fingerprint and JS engine; `curl_cffi` gives us the same TLS legitimacy without
the weight of driving Chrome. The remaining defense — per-IP request caps
(~200/hr on one residential IP) — is what the proxy pool in section 5 addresses.
