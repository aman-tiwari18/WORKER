# CI/CD — worker (GitHub Actions, Docker)

Pipeline: `.github/workflows/uat.yml`. **Trigger: push to `uat`** (or manual).

Flow: `push uat` → **build** (self-hosted GPU runner: `docker build` → tag
`:uat-<sha>` + `:uat` → push to registry) → **deploy** (same runner: rsync code to
deploy dir, `docker compose -f deploy/docker-compose.uat.yml up -d` with the new tag).

The image is GPU/CUDA/vLLM → it **must** build on-prem (self-hosted). The vLLM
patches are baked in, so the **same image runs on w0-3 and the big w5/6 hosts**.

## One-time GitHub setup
1. **Create the GitHub repo**, push, create the `uat` branch.
2. **Self-hosted runner** on a fleet GPU host, labels: `cctv-uat`, `gpu`.
3. Runner host prerequisites:
   - Docker + NVIDIA Container Toolkit (nvidia runtime).
   - ⚠️ **Docker data-root on `/data`** — the image is ~15-25 GB and root is space-
     constrained. `/etc/docker/daemon.json` → `{"data-root":"/data/docker"}` then
     `systemctl restart docker`.
   - A real `worker/.env` in the deploy dir (DB creds, `MAIN_BACKEND_URL`,
     `WORKER_INDEX`, `TOTAL_WORKERS`, `ENABLE_VLLM`, `CUDA_VISIBLE_DEVICES`…).
   - Registry access (ghcr.io login via `GITHUB_TOKEN`, or a local registry).
4. **Repo variables** (optional; defaults shown):
   - `WORKER_IMAGE` = `ghcr.io/<owner>/cctv-worker` (or `host:5000/cctv-worker`)
   - `WORKER_DEPLOY_DIR` = `/data/new_dev/worker`
5. **Secrets** (optional): `REGISTRY_TOKEN` (only if not using ghcr + `GITHUB_TOKEN`).

## Per-host scale-out
`deploy/docker-compose.uat.yml` shows GPU 0. On the big hosts, duplicate the
`worker-gpu0` service per GPU, bumping `WORKER_INDEX` + `device_ids`. Everything
is ENV-driven — the image is identical on every host.

## Manual deploy
```bash
docker build -t cctv-worker:uat .
IMAGE=cctv-worker IMAGE_TAG=uat docker compose -f deploy/docker-compose.uat.yml up -d
```
