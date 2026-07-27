# Garmin connector

Syncs your Garmin Connect activities into Dreeve's watch folder: it periodically lists new
activities, downloads the original FIT files and delivers them atomically, keeping a persistent
ledger so nothing is ever fetched twice.

```
Garmin Connect ──▶ connector ──▶ Dreeve's watch/ ──▶ Dreeve imports it
```

Dreeve already imports anything dropped into its `watch/` folder. Getting files *into* that folder
is the part this closes: no manual exports, no Strava round-trip.

> **Heads up:** this uses Garmin's unofficial API. Please read
> [Living with an unofficial API](#living-with-an-unofficial-api) before relying on it.

## First run

The login step is where people get stuck, so do it first and do it once.

### 1. A place to put it

```yaml
# docker-compose.yml
services:
  connector:
    image: ghcr.io/dreeveapp/dreeve-garmin-connector:latest
    container_name: dreeve-garmin-connector
    restart: unless-stopped
    env_file:
      - path: ./.env
        required: false
    volumes:
      # Dreeve's watch folder. Read-write: Dreeve deletes files once it has imported them.
      - /path/to/dreeve/watch:/watch
      # The ledger. Losing this means downloading everything again.
      - ./state:/state
      # The Garmin session. Keep this and you should never have to log in twice.
      - ./tokens:/tokens
    ports:
      - '8080:8080'
```

### 2. Tell it who you are

```dotenv
# .env
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-garmin-password
SINCE=-30d
TZ=Europe/Brussels
```

`SINCE` decides how far back to reach on the very first run, and only the first run — after that
it is remembered. `-30d` is a sensible start; see [Why the first day is slow](#why-the-first-day-is-slow)
before setting it to `2015-01-01`.

### 3. Log in, once, by hand

```bash
docker compose run --rm connector login
```

This is the only command that ever uses your password. If your account has multi-factor
authentication you will be asked for the code here — `docker compose run` attaches a terminal, which
is exactly why the login is a separate command instead of something the daemon does at startup.

On success the session is written to `./tokens` (mode `0600`) and refreshes itself from then on.
**You can remove `GARMIN_PASSWORD` from `.env` now** if you like; nothing else needs it.

### 4. Start it

```bash
docker compose up -d
docker compose logs -f
```

You should see a cycle run immediately, then one every hour. Files appear in the watch folder as
`<activityId>.fit`, and Dreeve picks them up within five minutes.

### 5. Check on it

```bash
curl localhost:8080/status
```

The most important field is `authentication`. If it says anything other than `ok`, that is what is
wrong — everything else is downstream of it.

## Why the first day is slow

A first run against five years of history is hundreds of downloads. Asking for all of them at once
is the single most reliable way to get an account rate-limited, so a cycle downloads at most
`MAX_DOWNLOADS_PER_CYCLE` activities (default 25) and picks up where it left off next time.

With the default hourly interval that is **600 activities a day**. A long backfill therefore takes
days, on purpose. It is not stuck: `/status` reports `backlog`, the number still waiting, and it
should fall every cycle.

In a hurry? Raise `MAX_DOWNLOADS_PER_CYCLE` and lower `POLL_INTERVAL` for the initial import, then
put them back. You are trading safety margin for speed, and Garmin decides how that trade goes.

## Configuration

Everything is an environment variable. Everything except `GARMIN_EMAIL` and `SINCE` has a
sensible default. See [`.env.example`](https://github.com/dreeveapp/dreeve-garmin-connector/blob/master/.env.example) for the same table with commentary.

| Variable | Default | What it does |
|---|---|---|
| `GARMIN_EMAIL` | — | **Required.** Also accepts `GARMIN_EMAIL_FILE` for Docker secrets. |
| `GARMIN_PASSWORD` | — | Only needed for `login`. Also accepts `GARMIN_PASSWORD_FILE`. |
| `GARMIN_IS_CN` | `false` | Use Garmin China. |
| `GARMINTOKENS` | `/tokens` | Where the session lives. Mount it as a volume. |
| `WATCH_DIR` | `/watch` | Dreeve's watch folder. |
| `STATE_DIR` | `/state` | Where the ledger lives. Mount it as a volume. |
| `SINCE` | — | **Required on the first run.** A date (`2026-01-01`), an ISO instant, a relative offset (`-30d`, `720h`) or `now`. Resolved once, then remembered. |
| `POLL_INTERVAL` | `3600` | Seconds between cycles. |
| `POLL_JITTER_PCT` | `10` | Randomises the interval by ±this much, so every deployment of this image does not hit Garmin on the same second. |
| `LOOKBACK_DAYS` | `7` | Re-lists the last few days each cycle, catching watches that synced late and activities edited afterwards. |
| `MAX_DOWNLOADS_PER_CYCLE` | `25` | The per-cycle cap. See above. |
| `DOWNLOAD_DELAY_SECONDS` | `2` | Pause between downloads. |
| `ACTIVITY_TYPES` | — | Comma separated, e.g. `cycling,running`. Empty means everything. Widening it later picks up activities that were previously ignored. |
| `FALLBACK_FORMAT` | `tcx` | `tcx`, `gpx` or `none`, for activities that have no FIT file. |
| `ON_CONFLICT` | `skip` | `skip` or `overwrite`, when the file is already in the watch folder. |
| `MAX_ATTEMPTS` | `5` | How often an activity may fail before it is left alone. |
| `MAX_BACKOFF_SECONDS` | `21600` | Cap on the backoff after a rate limit (6 hours). |
| `ALLOW_PASSWORD_LOGIN` | `false` | Permits **one** password login per container start. See below. |
| `HTTP_ADDR` | `0.0.0.0:8080` | `off` disables `/healthz` and `/status`. |
| `MAX_CYCLES` | `0` | `0` runs forever; anything else runs that many cycles and exits. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`, `critical`. |
| `LOG_FORMAT` | `text` | `text` or `json`. |
| `DRY_RUN` | `false` | List what would be downloaded, download nothing, leave the ledger untouched. |
| `PUID` / `PGID` | — | Own the delivered files as this user. Set them to whatever uid Dreeve runs as. |
| `UMASK` / `TZ` | — | As usual. |

> `env_file` is read when a container is **created**, not when it starts. After changing `.env`,
> run `docker compose up -d --force-recreate` — a plain `restart` keeps the old values.

## Commands

```bash
docker compose run --rm connector login              # once, interactively
docker compose run --rm connector sync-once          # a single cycle, then exit
docker compose run --rm connector sync-once --dry-run # list what it would fetch, touch nothing
docker compose exec connector dreeve-garmin-connector status
```

## Health and status

`/healthz` returns 200 while healthy and 503 otherwise, and is what the image's `HEALTHCHECK` uses,
so `docker ps` and `docker compose ps` tell you the truth without extra tooling.

Unhealthy means one of two things: the Garmin session is broken, or no cycle has finished in three
intervals. `/status` says which:

```json
{
  "healthy": true,
  "startedAt": "2026-07-24T22:12:00+00:00",
  "cycles": 12,
  "lastSuccessfulSync": "2026-07-25T10:12:00+00:00",
  "nextRunAt": "2026-07-25T11:09:43+00:00",
  "backoffSeconds": 0.0,
  "authentication": "ok",
  "lastError": null,
  "lastCycle": {
    "listed": 4,
    "delivered": 2,
    "failed": 0,
    "skipped": 0,
    "withoutFile": 1,
    "backlog": 7
  },
  "backlog": 7,
  "activities": {
    "pending": 7,
    "delivered": 312,
    "skipped": 0,
    "failed": 0,
    "no-file": 12
  }
}
```

With `HTTP_ADDR=off` there is no endpoint, and the healthcheck falls back to the mtime of
`$STATE_DIR/heartbeat`.

## Running Dreeve on another host

This connector writes to a **local** folder and nowhere else — no `scp`, no SSH, deliberately.
Transport is a separate problem with its own failure modes, and duplicating it here would mean
maintaining it twice.

If Dreeve lives on another machine, chain the sibling project:

```
connector ──▶ local folder ──▶ dreeve-activity-file-watcher ──▶ remote Dreeve over scp
```

Point `WATCH_DIR` at an ordinary local directory, and let the watcher ship it onward. *(The watcher
is a planned sibling project and is not published yet; until then, a `docker compose` volume over
NFS/SMB, or a cron'd `rsync`, does the same job.)*

## How it decides what to fetch

Worth knowing when something looks wrong.

- **The ledger is the memory.** `$STATE_DIR/ledger.json` records every activity it has seen, keyed
  by Garmin activity id. Dreeve *deletes* files from the watch folder once imported, so the folder
  can never answer "did we already fetch this?" — only the ledger can. It is plain JSON: `cat` it,
  or delete one entry to force a re-download.
- **Statuses:** `delivered`, `no-file` (a manually entered activity with nothing to download),
  `skipped` (older than `SINCE`), `failed` (with the error and an attempt count), `pending`.
  The first three are final and never reconsidered.
- **Windows, not full history.** Each cycle lists from `last sync - LOOKBACK_DAYS` up to today. A
  first run walks from `SINCE` in 30-day pages, pausing between them.
- **Ordering.** Oldest first, so a backlog drains chronologically.

Common questions:

| Symptom | Reason |
|---|---|
| An activity was never imported | No FIT and no fallback (`no-file`), or excluded by `ACTIVITY_TYPES`, or older than `SINCE`. Check `ledger.json`. |
| I deleted a file from `watch/` and it did not come back | Working as intended — it is `delivered`. Delete its ledger entry to force it. |
| Nothing happens, logs are quiet | Check `/status`. Almost always `authentication`. |
| `backlog` is large | The per-cycle cap. It drains. |

## Living with an unofficial API

This is built on [`cyberjunky/python-garminconnect`](https://github.com/cyberjunky/python-garminconnect),
which talks to the same endpoints Garmin's own app does. Garmin does not document or support this,
and has changed the authentication flow more than once. Expect occasional breakage, and be aware
that using it is on you.

Three things follow from that, and they shape the whole design:

**Logging in is the rate-limited operation, not reading data.** So the daemon *never* logs in. It
resumes the session you created with `login`, and if there is none it says so, reports unhealthy and
waits. It will not guess, and it will not retry a login Garmin has rejected — that is precisely the
retry storm that gets accounts blocked. `ALLOW_PASSWORD_LOGIN=true` permits exactly one attempt per
container start, for accounts without MFA that want unattended setup.

**Rate limits are survivable if you back off.** A 429 aborts the whole cycle and doubles the
interval, up to `MAX_BACKOFF_SECONDS`. Together with the per-cycle cap and the delay between
downloads, the defaults are deliberately gentle. If you are seeing 429s, make them gentler still
rather than restarting the container in a loop.

**When Garmin changes something,** the symptom is usually a burst of authentication errors or
`UnexpectedResponse` in the logs. Check for a newer release of this image first — the library is
pinned, and a bump is usually all that is needed. Everything that talks to Garmin lives in one file
(`src/dreeve_garmin_connector/garmin.py`), so the blast radius is small by construction.

Your credentials and session tokens are redacted from every log line, including tracebacks.
