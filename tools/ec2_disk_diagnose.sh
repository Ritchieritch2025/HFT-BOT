#!/usr/bin/env bash
# tools/ec2_disk_diagnose.sh — INCIDENT PRIORITY RESET (operator order 2026-07-14)
# FIXED READ-ONLY storage diagnosis for the 07-13 seal/WAL "No space left" incident.
#
# READ-ONLY GUARANTEE
#   External commands used, complete list:
#     date findmnt df lsblk stat ls du lsof ps pgrep git ulimit quota sleep
#   (sleep is required by the two-sample mandate; it touches nothing.)
#   Everything else is bash builtins (echo/printf/arithmetic/read).
#   NO writes anywhere, NO DuckDB connection, NO sudo mutation, NO kill/restart,
#   NO seal/prune/vacuum, NO EBS/IAM change. Output goes to stdout only.
#   /proc/<pid>/environ is NEVER read (credential zero-echo, DOC-2).
#
# Usage (on the EC2 box, as ubuntu or root; root sees more in lsof):
#   bash tools/ec2_disk_diagnose.sh
# Optional env overrides (read-only path hints, no behavior change):
#   REPO=/home/ubuntu/hft-bot  DB=...  RAW=...  DUCKDB_TEMP_DIR=...
#   SAMPLE_INTERVAL=300   (seconds between the two samples; default 300)
#
# Exit: prints a STORAGE_DIAGNOSIS=<CLASS> line and stops. No remediation.

set -u
LC_ALL=C
export LC_ALL

REPO="${REPO:-$HOME/hft-bot}"
WORK="$REPO/work"
DB="${DB:-$WORK/staging.duckdb}"
WAL="$DB.wal"
RAW="${RAW:-$WORK/raw}"
DTMP="${DUCKDB_TEMP_DIR:-}"   # operator-supplied if known; config parsing (grep)
                              # is outside the allowlist, so candidates are
                              # probed by ls/stat instead.
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-300}"

hr(){ printf '%s\n' "--------------------------------------------------------------"; }
sec(){ hr; printf '== %s\n' "$1"; hr; }

# df helpers (GNU df on Ubuntu). One target per call; parse with read, no awk.
df_line(){ # $1=path -> prints "avail_bytes pcent iavail ipcent fstarget"
  df -B1 --output=avail,pcent,iavail,ipcent,target "$1" 2>/dev/null | {
    read -r _hdr || true
    read -r a p ia ip t || true
    printf '%s %s %s %s %s\n' "${a:-0}" "${p:-?}" "${ia:-0}" "${ip:-?}" "${t:--}"
  }
}
mount_of(){ findmnt -no TARGET -T "$1" 2>/dev/null || printf 'UNKNOWN'; }
size_of(){ stat -c '%s' "$1" 2>/dev/null || printf '0'; }

sec "0. IDENTITY"
date -u
printf 'repo: %s\n' "$REPO"
git -C "$REPO" rev-parse HEAD 2>/dev/null || printf 'git HEAD: UNAVAILABLE\n'
git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true
printf 'ulimit -a:\n'; ulimit -a

sec "1. FILESYSTEM / MOUNT IDENTITY (requirement 1)"
printf 'targets:\n  DB=%s\n  WAL=%s\n  RAW=%s\n  /tmp\n  DUCKDB_TEMP_DIR=%s\n' \
  "$DB" "$WAL" "$RAW" "${DTMP:-UNKNOWN (not supplied; grep not allowlisted)}"
for p in "$DB" "$WAL" "$RAW" /tmp ${DTMP:+"$DTMP"}; do
  printf -- '- %s\n' "$p"
  findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS -T "$p" 2>/dev/null || printf '  (path missing)\n'
done
printf '\nDuckDB temp-dir candidates (probe only):\n'
ls -ld "$WORK/.tmp" "$WORK/tmp" "$WORK"/*.tmp 2>/dev/null || printf '  none of the common candidates exist\n'
printf '\nlsblk:\n'; lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS 2>/dev/null || lsblk

sec "2. BYTE + INODE HEADROOM PER MOUNT (requirement 2)"
df -hT
printf '\n'
df -i
printf '\nper-target (bytes): avail pcent iavail ipcent mount\n'
for p in "$DB" "$RAW" /tmp ${DTMP:+"$DTMP"}; do
  [ -e "$p" ] || { printf '%s: MISSING\n' "$p"; continue; }
  printf '%s: ' "$p"; df_line "$p"
done

sec "3. SIZES: raw by date/channel, DB/WAL/temp (requirement 3)"
printf 'staging.duckdb : %s bytes\n' "$(size_of "$DB")"
printf 'staging.wal    : %s bytes\n' "$(size_of "$WAL")"
ls -l "$DB" "$WAL" 2>/dev/null || true
printf '\nwork/ first level (du -x -d1, bounded, unsorted — sort not allowlisted):\n'
du -x -d1 -B1 "$WORK" 2>/dev/null
printf '\nraw by date (du -s per date dir):\n'
for d in "$RAW"/date=*/; do
  [ -d "$d" ] || continue
  du -sB1 "$d" 2>/dev/null
done
printf '\nraw by date x channel family (du -cs on family globs):\n'
for d in "$RAW"/date=*/; do
  [ -d "$d" ] || continue
  for fam in firehose l2 rfq_receipts rfq; do
    # shellcheck disable=SC2086
    tot=$(du -cB1 "$d"${fam}_* 2>/dev/null | { t=0; while read -r s _; do t=$s; done; printf '%s' "$t"; })
    [ "${tot:-0}" != "0" ] && [ -n "${tot:-}" ] && printf '%s %s: %s bytes\n' "$d" "$fam" "$tot"
  done
done

sec "4. DELETED-BUT-OPEN FILES (requirement 4)"
printf '(run as root for complete visibility; non-root output may be partial)\n'
DELOPEN_BYTES=0
if command -v lsof >/dev/null 2>&1; then
  lsof +L1 2>/dev/null || printf 'lsof +L1: empty or unavailable\n'
  # sum sizes (column 7 = SIZE/OFF) with a pure-bash loop
  while read -r _cmd _pid _user _fd _type _dev sz _rest; do
    case "$sz" in (''|*[!0-9]*) continue;; esac
    DELOPEN_BYTES=$((DELOPEN_BYTES + sz))
  done < <(lsof +L1 2>/dev/null | { read -r _h || true; while IFS= read -r l; do printf '%s\n' "$l"; done; })
else
  printf 'lsof: NOT INSTALLED\n'
fi
printf 'deleted-but-open total: %s bytes\n' "$DELOPEN_BYTES"

sec "5. TWO SAMPLES ACROSS ${SAMPLE_INTERVAL}s (requirement 5)"
read -r A0 _ _ _ M0 < <(df_line "$DB")
W0=$(size_of "$WAL"); D0=$(size_of "$DB")
printf 'T0 %s: avail=%s bytes (mount %s) db=%s wal=%s\n' "$(date -u +%H:%M:%S)" "$A0" "$M0" "$D0" "$W0"
sleep "$SAMPLE_INTERVAL"
read -r A1 _ _ _ _ < <(df_line "$DB")
W1=$(size_of "$WAL"); D1=$(size_of "$DB")
printf 'T1 %s: avail=%s bytes db=%s wal=%s\n' "$(date -u +%H:%M:%S)" "$A1" "$D1" "$W1"
DELTA=$((A1 - A0)); [ "$DELTA" -lt 0 ] && ADELTA=$((-DELTA)) || ADELTA=$DELTA
printf 'delta avail over window: %s bytes (|%s|)\n' "$DELTA" "$ADELTA"

sec "6. INGEST PROCESS IDENTITY (requirement 6)"
FOUND_PID=""
pgrep -af 'ingest' 2>/dev/null || printf 'no ingest process matched\n'
for pid in $(pgrep -f 'tools/ingest\.py' 2>/dev/null); do
  FOUND_PID="$pid"
  ps -o pid,ppid,etime,rss,args -p "$pid" 2>/dev/null
  printf 'cwd: '; ls -l "/proc/$pid/cwd" 2>/dev/null || printf 'UNREADABLE\n'
  printf 'exe: '; ls -l "/proc/$pid/exe" 2>/dev/null || printf 'UNREADABLE\n'
done
printf 'deployed git HEAD (repo %s): %s\n' "$REPO" "$(git -C "$REPO" rev-parse HEAD 2>/dev/null || printf UNAVAILABLE)"

sec "7. QUOTA (if available)"
if command -v quota >/dev/null 2>&1; then quota -u 2>/dev/null || printf 'quota: none/inactive\n'; else printf 'quota: NOT INSTALLED\n'; fi
QUOTA_HIT=0
if command -v quota >/dev/null 2>&1; then
  case "$(quota -u 2>/dev/null)" in (*'*'*) QUOTA_HIT=1;; esac
fi

sec "8. CLASSIFICATION (requirement 7) — thresholds printed, evidence-based"
# gather classification inputs
read -r AV _ IAV IPC MNT < <(df_line "$DB")
MDB=$(mount_of "$DB"); MWAL=$(mount_of "$(printf '%s' "$WAL" | { IFS=; read -r x; printf '%s' "${x%/*}"; })" 2>/dev/null || printf '%s' "$MDB")
MRAW=$(mount_of "$RAW"); MTMP=$(mount_of /tmp)
DBSZ=$(size_of "$DB")
IPC_N=${IPC%\%}; case "$IPC_N" in (''|*[!0-9]*) IPC_N=0;; esac
GIB=$((1024*1024*1024))
CLASS="UNKNOWN"; EVID="no rule matched"
if [ "$QUOTA_HIT" -eq 1 ]; then
  CLASS="QUOTA"; EVID="quota -u shows an exceeded (*) limit"
elif [ "$IPC_N" -ge 99 ]; then
  CLASS="INODE_EXHAUSTION"; EVID="IUse% on $MNT = ${IPC} (>=99%), iavail=$IAV"
elif [ "$DELOPEN_BYTES" -gt $((5*GIB)) ]; then
  CLASS="DELETED_OPEN"; EVID="deleted-but-open = $DELOPEN_BYTES bytes (> 5 GiB threshold)"
elif [ "$MWAL" != "$MDB" ] || [ "$MTMP" != "$MDB" ] && [ "$AV" -gt $((10*GIB)) ]; then
  CLASS="WRONG_MOUNT"; EVID="db/wal/tmp not co-mounted (db=$MDB wal=$MWAL tmp=$MTMP) while db-mount avail=$AV"
elif [ "$AV" -gt $((2*DBSZ)) ] && [ "$AV" -gt $((10*GIB)) ]; then
  CLASS="TRANSIENT_PEAK"; EVID="avail now ($AV bytes) > 2x db size ($DBSZ) and > 10 GiB, yet WAL commit reported ENOSPC earlier; 5-min |delta|=$ADELTA bytes -> shortage was transient (burst commit peak)"
fi
printf 'rules: QUOTA(*) > INODE(IUse>=99%%) > DELETED_OPEN(>5GiB) > WRONG_MOUNT(split mounts) > TRANSIENT_PEAK(avail>max(2xDB,10GiB)) > UNKNOWN\n'
printf 'inputs: avail=%s ipcent=%s delopen=%s dbsize=%s mounts db=%s wal=%s raw=%s tmp=%s\n' \
  "$AV" "$IPC" "$DELOPEN_BYTES" "$DBSZ" "$MDB" "$MWAL" "$MRAW" "$MTMP"
hr
printf 'STORAGE_DIAGNOSIS=%s\n' "$CLASS"
printf 'EVIDENCE: %s\n' "$EVID"
# STOP. No remediation is proposed or executed by this script.
