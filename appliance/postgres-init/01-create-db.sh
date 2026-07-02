#!/bin/bash
# Runs ONCE, only on an empty Postgres data dir (docker-entrypoint-initdb.d contract).
# On a restore (existing data dir) it never runs, so it can't fight a restore.
#
# POSTGRES_DB=farmpos already causes the entrypoint to create the DB, so this is a
# belt-and-braces idempotent guard (and the documented home for any future
# provisioning DDL). It replaces the old no-op POSTGRES_DB_QA compose variable.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    SELECT 'CREATE DATABASE farmpos'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'farmpos')\gexec
SQL

echo "[init] farmpos database ensured. App strong_migrate() will build the schema on first boot."
