# CI/CD — worker (GitHub Actions → Docker on GPU)

Workflow: `.github/workflows/uat.yml` · **Trigger: push to `uat`** (or manual).
Status: ✅ verified green end-to-end (build ~35 s with warm Docker layer cache).

## Flow
```
push uat
  └─ ci     (GitHub-hosted ubuntu)   py_compile: agent + detection + vLLM patches
      └─ build  (self-hosted GPU, labels: cctv-uat + gpu)
            docker build -t $IMAGE:uat-<sha> -t $IMAGE:uat .
            (docker push only if WORKER_REGISTRY_PUSH=true)
          └─ deploy (same runner)
                rsync code → WORKER_DEPLOY_DIR (excludes .env, models, hf_cache, logs, results)
                docker compose --project-directory <dir> -f deploy/docker-compose.uat.yml up -d
```
`build` + `deploy` are gated by `ENABLE_DEPLOY=true`; the `ci` job always runs so the
pipeline stays green without the GPU runner.

The image **must** build on-prem (CUDA + torch + vLLM, ~17 GB).

## Setup already done on this host
- Self-hosted runner `cctv-uat-worker` → systemd, `active` + `enabled` (`/data/new_dev/ci-runners/worker`).
- Repo variable `ENABLE_DEPLOY=true`.
- Docker **data-root on `/data/docker`** (root disk can't hold a 17 GB image).
- **nvidia-container-toolkit** installed + docker `nvidia` runtime configured.
- **Docker Compose v2** plugin installed (v1 cannot redeploy — see gotchas).
- Worker `.env` + YOLO models in `models/` + shared frames dir.

## Repo variables
| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_DEPLOY` | *(unset)* | must be `true` to build/deploy |
| `WORKER_IMAGE` | `cctv-worker` | local tag. Set to `<registry>/cctv-worker` only for multi-host |
| `WORKER_REGISTRY_PUSH` | *(unset)* | `true` to push to a registry (needed only for other hosts) |
| `WORKER_DEPLOY_DIR` | `/data/new_dev/worker` | rsync + compose target |

> Build and deploy run on the **same host**, so the image is already local — pushing 17 GB
> to a registry is pointless here and is therefore opt-in.

## Runtime config (`.env`, git-ignored)
| Key | Value / meaning |
|---|---|
| `WORKER_MODE` | `agent` — **headless**: poller only, no HTTP API, no socket.io |
| `WORKER_INDEX` / `TOTAL_WORKERS` | shard identity (per-GPU) |
| `CUDA_VISIBLE_DEVICES` | `0` |
| `DB_*` | `cctv_v2` via `cctvv2_mysql:3306` on the `cctvv2_net` network |
| `MAIN_BACKEND_URL` | `http://host.docker.internal:4000/current_dev_mode/api` |
| `CAMVIEW_TOKEN_SERVER` | prod's token server — same DVR creds, keeps the one-session-per-DVR login serialized |
| `ENABLE_VLLM` | `1` — first classification run downloads ~62 GB of Qwen3-VL weights |
| `FRAMES_HOST_PATH` | `/data/new_dev/frames` — **must equal** the backend's `FRAMES_DIR` |

## Scaling to more GPUs / hosts
`deploy/docker-compose.uat.yml` defines GPU 0. Duplicate the `worker-gpu0` service per GPU,
bumping `WORKER_INDEX` and `device_ids`. **The image is identical everywhere** — including
the 8-GPU vLLM hosts — because the vLLM stability patches (`patch_vllm.py`,
`patch_vllm_deep_gemm.py`) and a pinned `vllm==0.20.2` are baked in.

## Manual deploy
```bash
cd /data/new_dev/worker
docker build -t cctv-worker:uat .
IMAGE=cctv-worker IMAGE_TAG=uat \
  docker compose --project-directory /data/new_dev/worker -f deploy/docker-compose.uat.yml up -d
docker logs -f cctv_uat_worker_gpu0
```

## Gotchas
- **Use `docker compose` (v2).** v1 (`1.29.2`) throws `KeyError: 'ContainerConfig'` when
  *recreating* a container against modern Docker Engine — it can create but not redeploy,
  and leaves a renamed orphan `<id>_cctv_uat_worker_gpu0` you must `docker rm -f`.
- **`--project-directory` is required** (compose resolves `env_file: .env` and `./models`
  from the project dir).
- **`results/` must be a real directory**, not prod's symlink to `/data/nfs/cctv_images`,
  or the bind-mount fails with *"not a directory"*. A Dockerfile guard enforces this.
- **GPU needs `nvidia-container-toolkit`** — the host driver alone is not enough.

Related: `../backend/CI-CD.md`, `/data/new_dev/GOTCHAS.md`, `/data/new_dev/ROADMAP.md`.
