# farsi-mcp-app

A remote MCP server for practising spoken colloquial Persian with Claude, by voice. The
server decides what to practise and when (spaced repetition with FSRS, levels, unlock
rules); Claude turns each planned exercise into a natural spoken exchange and grades it.

- Python, using the official MCP SDK (`mcp` 2.x), served over Streamable HTTP
- Curriculum in reviewable YAML files under `curriculum/`, loaded and validated at startup
- SQLite holds only a log of sessions and attempts; all learner state is rebuilt from it in memory
- SQLite is replicated to a Cloud Storage bucket with [Litestream](https://litestream.io)
- Runs on Google Cloud Run, max one instance
- Auth: the endpoint lives at a secret path, `/mcp/<MCP_SECRET>`. Anyone with the full URL can use it, so keep it private.

## Tools

| Tool | What it does |
| --- | --- |
| `start_session` | Starts a session: summary, today's plan (warm-up, scenes, something new), and the first three exercises |
| `log_attempt` | Logs one graded exercise and returns the next one |
| `next_items` | More exercises without logging, for a skipped exercise or extra practice |
| `get_progress` | Items by level, functions open and done, accuracy |

## Layout

| File | What it does |
| --- | --- |
| `app/server.py` | MCP server, tool definitions, server instructions and the grading rubric |
| `app/practice.py` | Sessions, logging attempts, progress |
| `app/planner.py` | Plans a session: warm-up, scenes grouped by function, first encounters, one hard thing per sentence |
| `app/engine.py` | Rebuilds learner state by replaying the log: FSRS cards, levels, unlocks, stages, functions, gates |
| `app/curriculum.py` | Loads and validates `curriculum/` |
| `app/db.py` | The SQLite log: `sessions`, `reviews`, `review_items` |
| `app/settings.py` | Budget, accuracy floor, retention and the fixed rules |
| `curriculum/` | The curriculum (see its README) |
| `entrypoint.sh` | Restores the DB from the bucket, then runs the server under `litestream replicate -exec` |
| `litestream.yml` | Points Litestream at `gs://$LITESTREAM_BUCKET/farsi.db` |

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

## Run locally

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export MCP_SECRET=$(openssl rand -hex 24) DB_PATH=./farsi.db PORT=8080
.venv/bin/python -m app.server
# MCP endpoint: http://localhost:8080/mcp/$MCP_SECRET
npx @modelcontextprotocol/inspector   # connect with transport "Streamable HTTP" to that URL
```

## Deploy to Cloud Run by hand

This is how the service was first created. After that, use the automatic deploys below.

One-time setup. Pick a US region (`us-central1`, `us-east1` or `us-west1`) so the bucket sits in Cloud Storage's free tier.

```sh
PROJECT=your-gcp-project
REGION=us-central1
BUCKET=$PROJECT-farsi-mcp
gcloud config set project $PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Bucket for the Litestream replica
gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access

# Service account the container runs as, allowed to read and write that bucket only
gcloud iam service-accounts create farsi-mcp
SA=farsi-mcp@$PROJECT.iam.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA --role=roles/storage.objectAdmin

# The secret path. Save it; it's part of the connector URL.
MCP_SECRET=$(openssl rand -hex 24)
echo $MCP_SECRET
```

Deploy (re-run this after every code change):

```sh
gcloud run deploy farsi-mcp \
  --source . \
  --region $REGION \
  --service-account $SA \
  --allow-unauthenticated \
  --max-instances 1 \
  --no-cpu-throttling \
  --cpu-boost \
  --memory 512Mi \
  --set-env-vars LITESTREAM_BUCKET=$BUCKET,MCP_SECRET=$MCP_SECRET
```

Why these flags:

- `--max-instances 1`: only one process ever writes the SQLite file.
- `--no-cpu-throttling`: Litestream replicates in the background between requests, which needs CPU
  when no request is in flight. This switches to instance-based billing. A container that lives about
  30 minutes a day stays well inside the free tier.
- `--cpu-boost`: shortens the cold start on the first tool call of a session.
- `--allow-unauthenticated`: Claude calls the server from the internet; the secret path is the guard.

The deploy prints a service URL like `https://farsi-mcp-abc123-uc.a.run.app`.

## Automatic deploys

`.github/workflows/deploy.yml` runs the tests, builds the image, pushes it to Artifact Registry, deploys it to
the `farsi-mcp` Cloud Run service with the same flags as above, and checks that the server lists its four tools.

- **On merge to main:** it runs by itself.
- **Any other branch:** open **Actions → Deploy → Run workflow**, type the branch name in the box and run it. It
  deploys to the same production service, so the next merge to main replaces it.

Deploys run one at a time and are never cancelled halfway, because only one instance may write the database.

GitHub signs in to Google Cloud with Workload Identity Federation, so no Google key is stored in GitHub. Only jobs
that use the `PRD` environment of this repository can sign in, and the deploy account can only push images and
deploy revisions.

### One-time setup

Run this in PowerShell, after the hand setup above (it reuses the `farsi-mcp` service account and the bucket):

```powershell
$PROJECT = "your-gcp-project"
$REGION = "us-central1"
$GH_REPO = "generatives/farsi-mcp-app"
gcloud config set project $PROJECT
$NUMBER = gcloud projects describe $PROJECT --format="value(projectNumber)"
gcloud services enable iamcredentials.googleapis.com artifactregistry.googleapis.com

# Where the images go
gcloud artifacts repositories create farsi-mcp --repository-format=docker --location=$REGION

# The account GitHub deploys as: push images, deploy revisions, run them as farsi-mcp
gcloud iam service-accounts create github-deploy
$DEPLOY_SA = "github-deploy@$PROJECT.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$DEPLOY_SA" --role="roles/run.developer"
gcloud artifacts repositories add-iam-policy-binding farsi-mcp --location=$REGION `
  --member="serviceAccount:$DEPLOY_SA" --role="roles/artifactregistry.writer"
gcloud iam service-accounts add-iam-policy-binding "farsi-mcp@$PROJECT.iam.gserviceaccount.com" `
  --member="serviceAccount:$DEPLOY_SA" --role="roles/iam.serviceAccountUser"

# Let this repo's PRD environment sign in as that account
gcloud iam workload-identity-pools create github --location=global
gcloud iam workload-identity-pools providers create-oidc farsi-mcp-app --location=global `
  --workload-identity-pool=github --issuer-uri="https://token.actions.githubusercontent.com" `
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" `
  --attribute-condition="assertion.sub == 'repo:${GH_REPO}:environment:PRD'"
gcloud iam service-accounts add-iam-policy-binding $DEPLOY_SA --role="roles/iam.workloadIdentityUser" `
  --member="principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$GH_REPO"

# Values for GitHub
"GCP_PROJECT       = $PROJECT"
"GCP_REGION        = $REGION"
"GCP_DEPLOY_SA     = $DEPLOY_SA"
"GCP_WIF_PROVIDER  = projects/$NUMBER/locations/global/workloadIdentityPools/github/providers/farsi-mcp-app"
"LITESTREAM_BUCKET = $PROJECT-farsi-mcp   (or whatever you named the bucket)"
```

Then in GitHub, open **Settings → Environments → PRD**:

- **Environment secrets:** `MCP_SECRET`, the same value the service already uses, so the connector URL doesn't change.
- **Environment variables:** the five values printed at the end of the script.

If the first run fails at the start, its error lists any of these that are missing.

## Add it to Claude

In claude.ai, open **Settings → Connectors → Add custom connector**, and give it the URL
`https://<service-url>/mcp/<MCP_SECRET>`. Then start a voice conversation and say you'd like to practise Persian.
Claude should call `start_session` and begin with the first exercise. If the connector was already added for the
smoke test, it keeps working with the same URL; Claude picks up the new tools in a new conversation.

## Look at the data

```sh
litestream restore -o farsi.db gs://$BUCKET/farsi.db
datasette farsi.db        # or open it in DB Browser for SQLite
```

`reviews` and `review_items` log every attempt, and `sessions` every session. Nothing else is stored:
levels, schedules and unlocks are rebuilt from these at startup. The smoke test's `words` and `served`
tables are left in place but no longer used.
