#!/bin/sh
set -eu

psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set test_database="$POSTGRES_TEST_DB" \
    --set database_owner="$POSTGRES_USER" <<-'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'test_database', :'database_owner') \gexec
SQL
