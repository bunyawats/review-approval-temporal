#!/bin/bash
# Applies db/schema.sql (mounted into the container at /schema.sql) to the
# review_approval database created by 01-create-app-database.sh. Must run
# after that script -- filenames are executed in lexical order.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname review_approval -f /schema.sql
