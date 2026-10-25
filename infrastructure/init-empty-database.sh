#!/bin/sh
# Explicit recovery after entrypoint failed before schema.sql (not run on startup).
set -eu
: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"

# Refuse partial schemas and populated databases. Never drop/truncate existing data.
tables=$(psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
  "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema') AND schemaname NOT LIKE 'pg_toast%'")
if [ "$tables" != "0" ]; then
  echo 'Refusing recovery: database already has tables. Inspect schema and data manually; nothing was changed.' >&2
  exit 1
fi

for file in /docker-entrypoint-initdb.d/01_schema.sql \
            /docker-entrypoint-initdb.d/02_import.sql \
            /docker-entrypoint-initdb.d/03_application.sql; do
  echo "Applying $file"
  psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$file"
done
echo 'Schema and CSV import completed. Database volume was not deleted.'
