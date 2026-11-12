#!/bin/sh
set -e

if [ -z "$LITESTREAM_BUCKET" ]; then
  echo "LITESTREAM_BUCKET not set: running without replication (local testing only)."
  exec python -m app.server
fi

# Pull the latest copy of the database from the bucket. Both flags make this a no-op
# when the file already exists or when the bucket is still empty (first deploy).
litestream restore -if-db-not-exists -if-replica-exists "$DB_PATH"

# Litestream runs the server as a child process and replicates while it runs.
# On shutdown it stops the server, flushes the last changes, then exits.
exec litestream replicate -exec "python -m app.server"
