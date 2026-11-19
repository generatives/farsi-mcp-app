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
| `start_session` | Starts a session: summary, known grammar and vocabulary, the new items it may introduce, and the first exercise |
| `log_attempt` | Logs one graded exercise and returns the next one |
| `skip_exercise` | Sets the current exercise aside without logging it and returns a different one |
| `get_progress` | Items by level, functions open and done, accuracy |

## How a session runs

There's no plan made in advance. Each time Claude logs an attempt, the server picks the next exercise from what
has happened so far, in this order:

1. **A new item due back.** A new item comes back on a short ladder within its first session: Claude introduces
   it and you repeat it, then 2 exercises later you hear it, 4 later you say it with a cue, 8 later you say it
   without one, and once more later on. A miss sends it back two steps. In the last two minutes, unfinished
   ladders get their next step.
2. **Warm-up.** At the start, up to two of your best-known items.
3. **A mission**, when a function's mission is ready and nothing new is mid-ladder.
4. **Something new**, while fewer than two new items are still on the first three steps of their ladder.
   At most 5 new items a day, and the Stage 0 grammar patterns come one a day.
5. **A due review**, the item you're closest to forgetting first.
6. Otherwise a new item early, or extra practice.

Exercises happen inside a scene, which lasts at least six exercises before it can change.

## Layout

| File | What it does |
| --- | --- |
| `app/server.py` | MCP server, tool definitions, server instructions and the grading rubric |
| `app/practice.py` | Sessions, logging attempts, progress |
| `app/planner.py` | Picks each exercise when it's needed: new items climbing their ladder, warm-up, missions, new items, due reviews |
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

## Deploy

`.github/workflows/deploy.yml` runs the tests, builds the image, pushes it to Artifact Registry, deploys it to
the `farsi-mcp-app` Cloud Run service, and checks that the server lists its four tools.

- **On merge to main:** it runs by itself.
- **Any other branch:** open **Actions → Deploy → Run workflow**, type the branch name in the box and run it. It
  deploys to the same production service, so the next merge to main replaces it.

Deploys run one at a time and are never cancelled halfway, because only one instance may write the database.
GitHub signs in to Google Cloud with Workload Identity Federation through the `PRD` environment, so no Google key
is stored in GitHub. The PRD environment holds the secret `MCP_SECRET` and the variables `GCP_PROJECT`,
`GCP_REGION`, `GCP_DEPLOY_SA`, `GCP_WIF_PROVIDER` and `LITESTREAM_BUCKET`.

Why the deploy uses these Cloud Run flags:

- `--max-instances 1`: only one process ever writes the SQLite file.
- `--no-cpu-throttling`: Litestream replicates in the background between requests, which needs CPU
  when no request is in flight. This switches to instance-based billing. A container that lives about
  30 minutes a day stays well inside the free tier.
- `--cpu-boost`: shortens the cold start on the first tool call of a session.
- The service allows unauthenticated calls, because Claude calls it from the internet; the secret path is the guard.

## Add it to Claude

In claude.ai, open **Settings → Connectors → Add custom connector**, and give it the URL
`https://<service-url>/mcp/<MCP_SECRET>`. Then start a voice conversation and say you'd like to practise Persian.
Claude should call `start_session` and begin with the first exercise. If the connector was already added for the
smoke test, it keeps working with the same URL; Claude picks up the new tools in a new conversation.

## Look at the data

```sh
litestream restore -o farsi.db gs://<LITESTREAM_BUCKET>/farsi.db
datasette farsi.db        # or open it in DB Browser for SQLite
```

`reviews` and `review_items` log every attempt, and `sessions` every session. Nothing else is stored:
levels, schedules and unlocks are rebuilt from these at startup. The smoke test's `words` and `served`
tables are left in place but no longer used.
