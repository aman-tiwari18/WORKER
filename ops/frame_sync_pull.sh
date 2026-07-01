#!/usr/bin/env bash
# Pull locally-staged detection/classification frames from a worker VM (w5/w6)
# onto main's shared NFS tree (/data/nfs/cctv_images), then prune old local files.
#
# Runs FROM main as a systemd timer (cctv-frame-sync@N.timer). We pull (not push)
# because w6's outbound :22 is firewalled, while main->VM ssh is always open.
#
# The DB stores canonical /data/nfs/cctv_images/... paths regardless of where the
# worker actually wrote the bytes, so landing files here makes those paths resolve.
#
# Usage: frame_sync_pull.sh <worker_index>   (5 or 6)
set -uo pipefail

N="${1:?usage: frame_sync_pull.sh <worker_index 5|6>}"
PORT=$((2218 + N))                       # w5 -> 2223, w6 -> 2224
SSH_USER="iitkanpur.admin"
SSH_HOST="103.42.51.250"
SSH_KEY="/root/.ssh/id_ed25519"
REMOTE_DIR="/localstorage/cctv_frames/"  # trailing slash: copy contents, not the dir
DEST_DIR="/data/nfs/cctv_images/"
LOCK="/run/cctv-frame-sync-${N}.lock"
RETENTION_MIN="${FRAME_SYNC_RETENTION_MIN:-360}"   # 6h local buffer; pulled long before

SSH_CMD="ssh -i ${SSH_KEY} -p ${PORT} -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes"

# Single in-flight pull per worker.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[frame-sync-${N}] another run in progress, skipping"
  exit 0
fi

echo "[frame-sync-${N}] pull start $(date -Is) port=${PORT}"
# -a archive, --partial(+dir) resumable & hides partials from the frontend.
# NO --delete (shared tree also holds w0-3 and other workers' files).
# NO --remove-source-files (Pass-2 still reads the local frame; janitor handles cleanup).
rsync -a --partial --partial-dir=.rsync-partial --timeout=120 \
  -e "${SSH_CMD}" \
  "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}" "${DEST_DIR}"
rc=$?
echo "[frame-sync-${N}] rsync rc=${rc} $(date -Is)"

# rc 24 = "some source files vanished" (janitor raced) — benign.
if [ "$rc" -eq 0 ] || [ "$rc" -eq 24 ]; then
  ${SSH_CMD} "${SSH_USER}@${SSH_HOST}" \
    "find /localstorage/cctv_frames -type f -mmin +${RETENTION_MIN} -delete 2>/dev/null; \
     find /localstorage/cctv_frames -mindepth 1 -type d -empty -delete 2>/dev/null" || true
  exit 0
fi
exit "$rc"
