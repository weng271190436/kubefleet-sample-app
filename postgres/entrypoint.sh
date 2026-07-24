#!/bin/bash
set -e

# KubeFleet PostgreSQL Replication Entrypoint
# Configures PG as primary or replica based on POSTGRES_ROLE env var.
#
# Required env vars:
#   POSTGRES_ROLE       - "primary" or "replica"
#   POSTGRES_PASSWORD   - superuser password
#   REPLICATION_USER    - replication user name (default: replicator)
#   REPLICATION_PASSWORD- replication password
#   PRIMARY_HOST        - (replica only) IP/hostname of the primary
#   PRIMARY_PORT        - (replica only) port of the primary (default: 5432)

PGDATA="/var/lib/postgresql/data/pgdata"
ROLE="${POSTGRES_ROLE:-primary}"
REPL_USER="${REPLICATION_USER:-replicator}"
REPL_PASS="${REPLICATION_PASSWORD:-replpass}"
PRI_HOST="${PRIMARY_HOST:-}"
PRI_PORT="${PRIMARY_PORT:-5432}"

echo "=== KubeFleet PG entrypoint: ROLE=$ROLE ==="

if [ "$ROLE" = "primary" ]; then
    echo "Starting as PRIMARY"

    # If data directory is empty, initialize
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        echo "Initializing primary database..."
        initdb -D "$PGDATA" --auth-host=md5 --auth-local=trust

        # Configure for replication
        cat >> "$PGDATA/postgresql.conf" <<EOF
listen_addresses = '*'
wal_level = replica
max_wal_senders = 10
wal_keep_size = '256MB'
hot_standby = on
archive_mode = on
archive_command = '/bin/true'
EOF

        # Allow replication connections
        cat >> "$PGDATA/pg_hba.conf" <<EOF
# Replication connections
host replication $REPL_USER 0.0.0.0/0 md5
host all all 0.0.0.0/0 md5
EOF

        # Start temporarily to create replication user and set superuser password
        pg_ctl -D "$PGDATA" -o "-c listen_addresses=''" -w start
        psql -d postgres -c "ALTER ROLE postgres WITH PASSWORD '$POSTGRES_PASSWORD';"
        psql -d postgres -c "CREATE ROLE $REPL_USER WITH REPLICATION LOGIN PASSWORD '$REPL_PASS';"
        psql -d postgres -c "CREATE DATABASE appdb;"
        # Create the configs table for the sample app
        psql -d appdb -c "
            CREATE TABLE IF NOT EXISTS configs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                category TEXT DEFAULT 'general'
            );
            INSERT INTO configs (key, value, category) VALUES
                ('transaction.daily_limit', '50000', 'limits'),
                ('transaction.single_transfer_max', '10000', 'limits'),
                ('transaction.international_fee_pct', '1.5', 'fees'),
                ('auth.session_timeout_min', '15', 'security'),
                ('auth.mfa_required', 'true', 'security'),
                ('interest.savings_apy', '4.25', 'rates'),
                ('interest.mortgage_30yr_fixed', '6.875', 'rates'),
                ('compliance.kyc_verification', 'enhanced', 'compliance'),
                ('notification.fraud_detection', 'realtime', 'alerts');
        "
        pg_ctl -D "$PGDATA" -m fast -w stop
    fi

    exec postgres -D "$PGDATA"

elif [ "$ROLE" = "replica" ]; then
    echo "Starting as REPLICA (primary=$PRI_HOST:$PRI_PORT)"

    if [ -z "$PRI_HOST" ]; then
        echo "ERROR: PRIMARY_HOST is required for replica mode"
        exit 1
    fi

    # Wait for primary to be reachable
    echo "Waiting for primary at $PRI_HOST:$PRI_PORT..."
    for i in $(seq 1 30); do
        if pg_isready -h "$PRI_HOST" -p "$PRI_PORT" -U "$REPL_USER" 2>/dev/null; then
            echo "Primary is ready!"
            break
        fi
        echo "  attempt $i/30..."
        sleep 2
    done

    # If data directory is empty or stale, do a base backup
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        echo "Running pg_basebackup from primary..."
        rm -rf "$PGDATA"
        PGPASSWORD="$REPL_PASS" pg_basebackup \
            -h "$PRI_HOST" -p "$PRI_PORT" -U "$REPL_USER" \
            -D "$PGDATA" -Fp -Xs -P -R

        echo "Base backup complete."
    fi

    # Ensure standby.signal exists and primary_conninfo is set
    touch "$PGDATA/standby.signal"
    # Override primary_conninfo in case the primary host changed
    sed -i '/^primary_conninfo/d' "$PGDATA/postgresql.auto.conf" 2>/dev/null || true
    echo "primary_conninfo = 'host=$PRI_HOST port=$PRI_PORT user=$REPL_USER password=$REPL_PASS'" >> "$PGDATA/postgresql.auto.conf"

    # Ensure hot_standby is on
    sed -i "s/^#*hot_standby.*/hot_standby = on/" "$PGDATA/postgresql.conf" 2>/dev/null || true

    exec postgres -D "$PGDATA"
else
    echo "ERROR: Unknown POSTGRES_ROLE='$ROLE'. Must be 'primary' or 'replica'."
    exit 1
fi
