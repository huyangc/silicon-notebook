#!/usr/bin/env bash
set -euo pipefail

docker exec "$POSTGRES_CONTAINER" sh -c '
  df -h "$PGDATA"
  if [ -r /sys/fs/cgroup/memory.peak ]; then
    printf "PostgreSQL container peak memory (bytes): "
    cat /sys/fs/cgroup/memory.peak
  fi
  if [ -r /sys/fs/cgroup/cpu.stat ]; then
    printf "PostgreSQL container CPU counters:\n"
    cat /sys/fs/cgroup/cpu.stat
  fi
'
docker exec "$POSTGRES_CONTAINER" psql --no-password -U postgres -d postgres -P pager=off -c '
  SELECT checkpoints_timed, checkpoints_req, checkpoint_write_time,
         checkpoint_sync_time, buffers_checkpoint, buffers_backend,
         buffers_backend_fsync
  FROM pg_stat_bgwriter;
  SELECT pg_size_bytes(current_setting($$shared_buffers$$)) AS shared_buffers_bytes,
         pg_size_bytes(current_setting($$max_wal_size$$)) AS max_wal_size_bytes;
  SELECT wal_records, wal_fpi, wal_bytes, wal_buffers_full,
         wal_write, wal_sync, wal_write_time, wal_sync_time
  FROM pg_stat_wal;
' 2>/dev/null || printf 'PostgreSQL checkpoint counters unavailable\n'
