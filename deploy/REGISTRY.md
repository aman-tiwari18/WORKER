# Worker image registry (ghcr) + continuous scaling

The worker image publishes to **GitHub Container Registry (ghcr.io)** so any GPU
host can pull-and-run it. This is what turns "one host" into "add a machine, it
joins the fleet."

```
push uat ─→ CI builds cctv-worker ─→ pushes ghcr.io/<owner>/cctv-worker:uat
                                              │
   new GPU host:  scale-worker.sh  ──pull────┘ ──→ N containers (1/GPU)
                                                   each self-registers with the fleet
```

---

## One-time setup (you, on GitHub — I can't change repo settings)

### 1. Turn on registry push — set two repo **Variables**
Repo → **Settings → Secrets and variables → Actions → Variables**:

| Variable | Value |
|---|---|
| `WORKER_IMAGE` | `ghcr.io/aman-tiwari18/cctv-worker` |
| `WORKER_REGISTRY_PUSH` | `true` |

(`ENABLE_DEPLOY=true` is already set.) That's all CI needs — the build job has
`permissions: packages: write`, so the built-in `GITHUB_TOKEN` pushes to ghcr
with **no extra secret**. The next push to `uat` publishes `:uat` and `:uat-<sha>`.

### 2. Make the package pullable by other hosts
After the first successful push, the package appears at
`github.com/users/aman-tiwari18/packages/container/cctv-worker`. Choose one:

- **Public** (simplest): package → **Package settings → Change visibility → Public**.
  Hosts then pull with no login.
- **Private**: create a **read-only token** (Settings → Developer settings →
  Personal access tokens → classic, scope **`read:packages`**) and give it to each
  host (see `GHCR_TOKEN` below). The image contains detection code, so private +
  a read token is the safer default.

> If you ever need a dedicated **push** token instead of `GITHUB_TOKEN` (e.g. an
> org registry), set the repo **secret** `REGISTRY_TOKEN` — the workflow prefers it.

---

## Bring a worker up on a NEW host (continuous scaling)

On the new GPU machine — needs Docker + nvidia-container-toolkit, network reach to
the backend and ghcr.io, and the `cctvv2_net` docker network (or edit the compose
network for a remote DB path):

```bash
git clone <worker repo> && cd worker        # or copy deploy/ + models/
cp .env.example .env                          # set MAIN_BACKEND_URL, API_KEY,
                                              # DB_ACCESS_MODE=backend, COORDINATION_MODE, …
# private package? export a read token:
export GHCR_USER=aman-tiwari18 GHCR_TOKEN=<read:packages PAT>

./deploy/scale-worker.sh
```

`scale-worker.sh` auto-detects the GPUs, logs in + pulls `ghcr.io/.../cctv-worker:uat`,
generates **one container per GPU** (each with its own `WORKER_INDEX`, `WORKER_ID`
and pinned `CUDA_VISIBLE_DEVICES`), and starts them. Every container registers with
the fleet on boot — confirm with `GET /fleet/summary`.

### Useful flags
| Env | Purpose |
|---|---|
| `GPUS=8` | override GPU count (default: `nvidia-smi -L` count) |
| `INDEX_BASE=12` | `WORKER_INDEX` of this host's first GPU — set per host so indexes are globally unique in **hash** mode. Irrelevant once `COORDINATION_MODE=lease` (workers claim shards, no indexing). |
| `IMAGE_TAG=uat-<sha>` | pin a specific build instead of rolling `:uat` |
| `NO_PULL=1` | run a locally-built image (no registry round-trip) |
| `DRY_RUN=1` | generate + validate the compose, don't start anything |

### Roll out a new build to an existing host
```bash
./deploy/scale-worker.sh          # re-pulls :uat and recreates the containers
```

---

## Why this scales cleanly
- **One image, every host** — differences are env only (`WORKER_INDEX`, GPU, capabilities).
- **Self-registration** — a new host's workers appear in `/fleet/summary` automatically; nothing central to reconfigure.
- **Lease mode** (`COORDINATION_MODE=lease`) makes it fully dynamic: a joining worker claims OPEN camera shards, a leaving one's shards get reclaimed — no `TOTAL_WORKERS`/`INDEX_BASE` bookkeeping.
- **No DB coupling** (`DB_ACCESS_MODE=backend`) — the host needs only backend + ghcr reachability, not a MySQL path.

The same-host CI `deploy` job still works as before (it builds locally and also
pushes to ghcr); other hosts use `scale-worker.sh`. See `CI-CD.md` for the pipeline.
