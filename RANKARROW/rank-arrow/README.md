# Rank Arrow — Instagram Profile Viewer & Downloader

A self-hosted Instagram **profile viewer & downloader** — a working build of an
IgAnony-style product. Type a username to browse a public profile (posts, reels,
stories, highlights) or paste a link, then download in the format you want.

> **Read `ARCHITECTURE.md`** for the full design write-up: the stream-catching
> cascade, the service-account model, rate-limit handling, and exactly how this
> scales to IgAnony's no-login infrastructure.

---

## What it does

- **Username search -> full profile view.** Avatar, bio, follower / following /
  post counts, verified badge, and tabbed grids for **Posts / Reels / Stories /
  Highlights**.
- **Hover any tile** -> it blurs with a **Download** button in the centre.
- **One preview, every format.** The download panel stays open, so after grabbing
  the full reel you can grab the audio or silent version with one more tap — no
  re-fetching.
- **Three video formats:** Full (mp4, video + audio), Audio only (mp3), No audio
  (mp4, sound stripped). Photos and stories save directly.
- **No visitor login.** The Instagram account lives in the backend; visitors only
  ever see search + download.
- **ffmpeg is bundled** (via `imageio-ffmpeg`) — audio/silent work with no manual
  install.
- **Chrome TLS impersonation** (via `curl_cffi`) — defeats Instagram's TLS
  fingerprinting that blocks plain `requests` (the real cause of profile `429`s).

---

## Quick start

```bash
# 1. install dependencies (FastAPI, yt-dlp, bundled ffmpeg, dotenv)
pip install -r requirements.txt
#    if Windows blocks pip.exe:  python -m pip install -r requirements.txt

# 2. connect a throwaway account ONCE  (see "Service account" below)
copy .env.example .env          # Windows   (cp on macOS/Linux)
#    then edit .env

# 3. run the backend  (no --reload — it would watch the venv and restart a lot)
uvicorn backend:app

# 4. open frontend.html in your browser
```

---

## Service account (one-time setup — visitors never log in)

Like IgAnony, the Instagram account is embedded in the **backend**. On startup the
server connects once, caches the session to `.web_session.json`, and reuses it for
everything — and **auto-reconnects on its own** if the session ever drops.

Connect one of two ways (in `.env` or `account.json`):

**Option A — sessionid cookie (most reliable, never hits a security check):**
1. Open instagram.com in a browser, logged in to a throwaway account.
2. `F12` -> **Application** (Chrome/Edge) or **Storage** (Firefox) -> **Cookies** ->
   `https://www.instagram.com` -> copy the **`sessionid`** value.
3. Put it in `.env`:
   ```
   INSTA_SESSIONID=the_long_sessionid_value
   INSTA_USER=label_for_the_account   # optional, just shown in the UI
   ```
   When `INSTA_SESSIONID` is set, nothing else is needed — this is the
   recommended way to connect.

**Option B — username + password:**
```
INSTA_USER=your_throwaway_account
INSTA_PASS=your_password
```
Works on a clean, browser-warmed account. If Instagram answers with a security
checkpoint (common on heavily-tested accounts), the app stops retrying and tells
you to switch to the sessionid method above.

> Credentials live only in `.env` / `account.json` (both gitignored). The cached
> session is `.web_session.json`.

---

## Which input to use

| Input | What you get | Reliability |
|---|---|---|
| `instagram.com/reel/XXXX/` (or `/p/`, `/tv/`) | that one reel/post | most reliable — single lightweight call |
| `@username` / profile link | the full profile view | extra profile lookup (Instagram throttles these per-account) |
| `instagram.com/stories/user/...` | that user's story | same profile-endpoint throttling |

**Rate limits are per-account.** Profile/username lookups hit an endpoint Instagram
throttles hard *per account*. A fresh or normally-used account works fine; an
account that's done many lookups in a short time gets a temporary `429` cooldown.
Reel links are barely affected. To remove the limit at scale, add residential
proxies to `proxies.txt` (see ARCHITECTURE.md section 5).

---

## How "never breaks" works (short version)

Instagram rotates the GraphQL `doc_id` every 2-4 weeks — the single value most
likely to break extraction. `DOC_IDS` in `backend.py` is a **list tried in order**,
with `?__a=1` and yt-dlp as fallbacks, so downloads usually keep working even
before you update it. When all the doc_ids eventually expire, copy the current one
(devtools -> Network -> a `graphql/query` request -> `doc_id`) to the top of the
list. One line. yt-dlp (the last fallback) auto-updates via `update_engines.sh`.

---

## Files

```
backend.py         FastAPI server — service account + stream-catch cascade
                   + profile/stories/highlights + bundled-ffmpeg convert
frontend.html      Single-file UI (no build step) — search, profile view,
                   hover-blur grid, download modal, FAQ
ARCHITECTURE.md    Full design write-up
requirements.txt   Python dependencies (bundled ffmpeg via imageio-ffmpeg)
.env.example       Service-account template (copy to .env)
account.example.json   Alternative to .env (copy to account.json)
proxies.txt        Optional proxy pool for scaling (empty by default)
update_engines.sh  Daily yt-dlp updater (keeps the fallback engine current)
.gitignore         Keeps secrets/session/downloads out of version control
```

---

## Maintenance (what drifts, and how often)

- **Session** — refresh the sessionid (or rely on password auto-relogin) every few
  weeks, or whenever the account is flagged. The app auto-reconnects from `.env`.
- **GraphQL `doc_id`** — one-line update every 2-4 weeks when Instagram rotates it.
- **yt-dlp** — auto-updates via `update_engines.sh` (the safety-net engine).
- Everything else (server, UI, ffmpeg, download logic) needs no routine upkeep.

---

*For personal use. Public content only — this build does not access private
accounts. Respect creators' copyright and Instagram's Terms of Service.*
