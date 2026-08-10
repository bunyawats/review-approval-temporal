#!/bin/bash
# Runs automatically on first container start (docker-entrypoint-initdb.d).
# The postgres container's default POSTGRES_DB is used by Temporal itself
# (auto-setup creates its own "temporal" + "temporal_visibility" databases
# there); this script creates a SEPARATE database for our app so the two
# don't share a schema namespace.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<-EOSQL
    CREATE DATABASE review_approval;
EOSQL
