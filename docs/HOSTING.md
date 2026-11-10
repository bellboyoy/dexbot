# Hosting the launch collector

Paper only. The hosted process records new pools. It does not sign, approve, or submit a transaction, and it does not open paper positions. Accounts and secrets are **not** created by this repository. Nothing is deployed until you run the steps below.

## Why this shape

The snapshot slots are 1, 5, 15, 30, 60 and 240 minutes after launch, then hourly through 24 hours. A slot stays observable for 45 seconds (the 1-minute slot), 90 seconds (the 5-minute slot), or 180 seconds (everything later). A later look is stored as **missed**, with a null price. The window is not widened to flatter a slow scheduler, because a late price is not the price at that age.

| Option | Why it does not fit |
|---|---|
| GitHub Actions cron | The shortest schedule is every 5 minutes, and GitHub says runs are delayed under load and can be dropped. Five minutes is already longer than every slot window (45s, 90s, 180s), so those runs would be recorded as missed. Looping a job all month also spends the free Actions minutes. |
| Vercel Cron on the Hobby plan | A job may run once per day, sometime inside that hour. Pro allows a per-minute cron, which is a paid plan, and a function is still not a 20-second poll loop. |
| Neon free Postgres | Scale-to-zero cannot be turned off. The free project includes 100 CU-hours a month, about 400 hours at the 0.25 CU size. A database that is queried every 20 seconds stays awake (~720 hours, ~180 CU-hours) and is suspended before the month ends. That breaks a 14-day collection. |

**Choice:** one always-on [Fly.io](https://fly.io) machine running `python -m app.collector`, writing to a [Supabase](https://supabase.com) free Postgres. The process polls every 20 seconds, which fits inside the 45-second window with about 25 seconds to spare. Supabase's free project stays up while the database is actually being used (a few queries a day is their usual bar; this process is far above that) and includes 500 MB of disk. Fly's personal-organization bill under $5 is waived as of their pricing page checked on 2026-11-10. One shared-cpu-1x machine with 256 MB is listed at $2.19 per 30 days in `iad`, plus a few cents of egress. There is no separate free tier: if the bill reaches $5, Fly charges the whole amount, so do not add a dedicated IPv4 ($2), a second machine, or Fly Managed Postgres.

If that waiver has changed when you sign up, the same machine is still about $2–4 a month. A 512 MB machine is $3.69 if 256 MB runs out of memory.

## What you create

1. A Supabase account and one free project (the database).
2. A Fly.io account and one app (the collector). Use a personal organization if you want the under-$5 waiver.
3. One secret, `DEXBOT_DATABASE_URL`, set with `fly secrets set`. It is not committed.

You do not need a GitHub Actions secret, a Vercel project, a wallet, or an RPC provider for this process.

## 1. Supabase database

1. Sign up at <https://supabase.com/dashboard>.
2. New project. Name it `dexbot-launches`. Region: **East US (North Virginia)** so it sits next to the Fly region `iad`.
3. Set the database password and store it in a password manager. Supabase shows it once.
4. Wait until the project status is healthy.
5. Open **Connect** (or Project Settings → Database) and copy the **Session pooler** URI. The host contains `pooler.supabase.com` and the port is **5432**. The user looks like `postgres.<project-ref>`.
6. Do not use the transaction pooler. That one is port **6543** and rejects the prepared statements this app uses.
7. Edit the URI:
   - Change the scheme from `postgresql://` to `postgresql+asyncpg://`.
   - If `sslmode` is missing, append `?sslmode=require`.
   - If the password contains `@`, `:`, `/`, `#`, or `%`, URL-encode it (`@` becomes `%40`, `%` becomes `%25`).
8. The result looks like this, with your own host and password:

```text
postgresql+asyncpg://postgres.abcdefghijklmnopqrst:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require
```

Keep that string. It is the only credential.

Check it once from `backend/`, then leave your normal `.env` pointed at the local database:

```bash
cd backend
DEXBOT_DATABASE_URL='postgresql+asyncpg://postgres.abcdefghijklmnopqrst:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require' \
  .venv/bin/alembic upgrade head
DEXBOT_DATABASE_URL='postgresql+asyncpg://postgres.abcdefghijklmnopqrst:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require' \
  .venv/bin/alembic current
```

`current` should print the head revision (the `pool ids and heartbeats` migration, or a later one). This is the same migration chain the container runs on every start. Do not put this URL in `DEXBOT_DATABASE_URL` inside `.env`. Put it in `DEXBOT_HOSTED_DATABASE_URL` there. `.env` is gitignored.

## 2. Fly.io collector

Install `flyctl`: <https://fly.io/docs/flyctl/install/>. Then, from `backend/`:

```bash
cd backend
fly auth login
fly apps create dexbot-launch-collector
```

If the name is taken, pick another, and change the `app` line in `backend/fly.toml` to match. Then:

```bash
fly secrets set DEXBOT_DATABASE_URL='postgresql+asyncpg://postgres.abcdefghijklmnopqrst:[PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require'
fly deploy
fly scale count collector=1
fly status
fly logs
```

`fly deploy` builds the `collector` target of `backend/Dockerfile`. The container runs `bin/run-collector`, which applies migrations and then starts `python -m app.collector`. There is no public HTTP service and no dedicated IPv4.

You should see `DexBot launch collector. Paper-only.` and, after a cycle, a `collector_cycle` log line. A missing or local database URL makes the script exit 1 without printing the URL. Fly restarts the machine after a non-zero exit, including the collector's own exit 1 when a cycle deadline is missed twice.

Stop the collector on the dev box once these logs look healthy. Two collectors double the DexScreener and GeckoTerminal calls. The paper runner on the dev box can keep running: it uses a different IP, so it does not share the hosted machine's GeckoTerminal budget.

## 3. Pull the data back for research

On the dev box, from `backend/`, with `.env` containing the local `DEXBOT_DATABASE_URL` and `DEXBOT_HOSTED_DATABASE_URL`:

```bash
cd backend
.venv/bin/python -m app.research.sync --dry-run
.venv/bin/python -m app.research.sync
.venv/bin/python -m app.research.pit
```

Sync copies `launch_watches`, `launch_snapshots`, and the collector heartbeat. It does not copy paper positions. Running it twice does not duplicate rows. For a pool that exists on the host, the host's slots replace the local ones, including deleting a local price for a slot the host recorded as missed. `pit` still refuses to change `strategy_validation.json` until 14 days and 30 pools have enough observed coverage. That gate is unchanged.

The dry run prints `hosted collector last success`. That timestamp is the heartbeat. It should move forward every cycle (the poll is 20 seconds; a cycle can take longer when providers are slow, and it gives up at 120 seconds).

## Rate limits and disk

On the hosted machine the collector is the only client. GeckoTerminal is one call every 180 seconds, under the keyless limit of about 5 calls per minute per IP. DexScreener is at most 4 pool reads per 20-second cycle (12 per minute), under the 200 per minute client cap, plus discovery well under the profile cap. Do not also run `python -m app.research.collect` there, and do not scale the app past one machine.

The hosted cap is 40 new pools a day and 80 watched at once (`DEXBOT_COLLECTOR_MAX_NEW_PER_DAY` and `DEXBOT_COLLECTOR_MAX_ACTIVE` in `fly.toml`). That is about 2.5 MB a day including indexes, so the 500 MB free disk lasts on the order of six months. The code default of 150 a day is unchanged for local runs and would fill 500 MB in about seven weeks. Supabase then rejects writes that grow the database. Check storage in the Supabase dashboard before raising the cap.

## What a gap looks like

A restart shorter than the slot window can still observe the slot. The 1-minute window is 45 seconds and the loop wakes every 20 seconds. A gap longer than the window (a failed deploy, a Supabase pause, a machine stopped for five minutes) stores the overdue slots as missed and does not backfill them. `python -m app.research.pit` counts only slots that were actually observed. Missed slots lower coverage. They do not count as the 8 real observations a pool needs.

Supabase emails you before it pauses a free project for inactivity. This collector's writes are enough activity while it is running. If you `fly scale count collector=0` for a week, the project can pause. Resume it in the Supabase dashboard, then start the machine again.

## What not to put on Fly

Do not deploy the API image, the paper runner, or a second collector. Do not set `DEXBOT_TRADING_MODE`. The process ignores that variable and stays in paper mode. No wallet secret belongs in `fly secrets`.
