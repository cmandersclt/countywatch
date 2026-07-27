# CountyWatch on Railway — deploy guide

This moves the daily brief off your laptop onto Railway, so it fires at 6 AM ET
every day whether the laptop is open, closed, asleep, or in a drawer. Same
GitHub-to-Railway flow you use for Bearing.

## What changed for the cloud
- `run.py` is the new entrypoint. Railway cron fires it at 10:00 and 11:00 UTC;
  it runs the pipeline only in the 6 AM ET window, so DST never shifts your time.
- `railway.json` holds the schedule and start command.
- State and digests write to a persistent volume (`DATA_DIR`) so NEW/ONGOING
  tracking survives deploys. Locally, nothing changes: unset `DATA_DIR` and it
  behaves exactly as before.
- Secrets move from the `.env` file to Railway variables. `.env` is gitignored
  and must NOT be committed.

## Step 1 — make the repo
In the countywatch folder:

    git init
    git add .
    git commit -m "CountyWatch: initial Railway deploy"

Confirm `.env` and `state.db` are NOT staged (they're in .gitignore). Run
`git status` and check neither appears.

Create a new GitHub repo (e.g. countywatch), then:

    git remote add origin https://github.com/cmandersclt/countywatch.git
    git branch -M main
    git push -u origin main

## Step 2 — create the Railway service
1. Railway dashboard -> New Project -> Deploy from GitHub repo -> pick countywatch.
2. It will build from `railway.json` automatically (Nixpacks + requirements.txt).

## Step 3 — add the persistent volume (do this before the first real run)
1. In the service, Settings -> Volumes -> New Volume.
2. Mount path: `/data`
3. Add a variable so the code uses it: `DATA_DIR = /data`

## Step 4 — set the secrets as variables
Service -> Variables -> add these three (values from your local `.env`):

    ANTHROPIC_API_KEY   = <your key>
    GMAIL_ADDRESS       = cole@cloutadvocacy.com
    GMAIL_APP_PASSWORD  = <your 16-char app password>

Optional:

    DATA_DIR            = /data          (from step 3)
    SEND_HOUR_ET        = 6              (change if you ever want a different hour)

## Step 5 — confirm the cron
Service -> Settings -> Cron Schedule should read `0 10,11 * * *` (from
railway.json). If Railway asks you to confirm or set it, use that value.

## Step 6 — test it now, without waiting for 6 AM
Add a temporary variable `FORCE_RUN = 1`, then trigger a deploy or run. The gate
is bypassed and the pipeline runs immediately, so you get a brief in your inbox.
Check it arrived, then DELETE the `FORCE_RUN` variable so normal scheduling
resumes. (Leaving it on would make every 10:00 and 11:00 UTC run send.)

## First-run notes
- Google may challenge the first SMTP login from a new server location. If the
  first send bounces, approve the sign-in in your Google security prompts and
  re-run. It clears after that.
- The first cloud run rebuilds `state.db` from empty, so everything shows as NEW
  once, then settles.
- Malheur is still JavaScript-gated. It stays a coverage gap here too; the cloud
  move does not change that.

## Turning off the laptop task
Once a real 6 AM ET cloud run has landed cleanly, disable the Windows task so you
don't get two briefs:

    # elevated PowerShell
    Disable-ScheduledTask -TaskName "CountyWatch Daily Brief"
