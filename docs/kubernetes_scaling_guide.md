# Kubernetes for the CCTV worker fleet

What Kubernetes is, in plain terms, and exactly what we've built with it here.
Status as of 2026-09-22: **one node (worker-1) running one worker pod, as a proof of concept.**
Workers 2–3 and the main VM are still plain Docker Compose.

---

## 1. What Kubernetes actually is

Forget the buzzwords for a second. Today, scaling a worker means: SSH into a box, run
`docker compose up -d`, and hope it stays up. If the box reboots, you SSH in again. If you
want a 4th worker, you provision a box and repeat the whole dance by hand.

Kubernetes replaces "SSH in and run docker compose" with "tell a control system what you
want, and it makes it true — continuously, on whichever machines are available."

You write down a **desired state** ("3 copies of the worker container, each with 1 GPU,
using this image"), hand it to the cluster, and a piece of software (the **scheduler** +
**controllers**) works out which machines have room, starts the containers there, and keeps
checking — forever — that reality matches what you asked for. If a pod dies, it's restarted.
If a node dies, its pods are rescheduled elsewhere. If you change the image tag, it rolls
out the new version one pod at a time.

This is the same idea as `systemd` (restart my service if it dies) or Docker's
`restart: unless-stopped`, just extended across a whole fleet of machines instead of one.

### The vocabulary, mapped to things you already know

| Kubernetes term | What it is | Closest thing you already know |
|---|---|---|
| **Cluster** | A group of machines working as one pool | Your 3 GPU boxes (worker-1/2/3), treated as one |
| **Node** | One machine in the cluster | `192.168.19.43` (worker-1) itself |
| **Pod** | One running instance of a container (usually 1:1 with a container) | `docker run` — one running container |
| **Image** | The packaged application | `ghcr.io/aman-tiwari18/cctv-worker:uat-<sha>` — same image you already build |
| **StatefulSet** | "Run N copies of this pod, each with a stable name/identity" | `docker-compose.yml`'s `worker-gpu0` service, but templated ×N with numbered identities (`cctv-worker-0`, `-1`, `-2`, …) |
| **ConfigMap / Secret** | Named bags of env vars / secret env vars | Your `.env` file, split into non-secret and secret parts |
| **PersistentVolume(Claim)** | A storage location a pod can mount | The `volumes:` section of `docker-compose.yml` |
| **Namespace** | A folder that groups related resources | `cctv/` vs `gpu-operator/` — just a label to keep things tidy |
| **kubectl** | The CLI you talk to the cluster with | `docker` CLI, but pointed at the cluster's API instead of one daemon |
| **containerd** | The actual thing that runs containers | Same role as Docker's engine — Kubernetes doesn't use Docker itself, just something Docker-compatible |

**The one fact that causes the most confusion:** Kubernetes does **not** use the Docker
daemon. It runs its own container runtime (`containerd`). So `docker ps` will **never**
show a Kubernetes pod, on any machine, ever — you have to ask Kubernetes's own tools
(`kubectl`, or `crictl` for the raw containerd view). This is not a bug, it's the whole
point: it's how a k3s pod and your existing `docker compose` worker can run side-by-side
on worker-1 without ever touching each other.

### Why bother, if Compose already works?

Compose scales one machine at a time, by hand, with no shared brain across machines.
Kubernetes gives you, for free, once it's set up:
- **Self-healing** — a crashed pod restarts itself; a dead node's pods move to a live one.
- **One command to scale** — `kubectl scale --replicas=5` instead of provisioning a box and
  SSHing in.
- **Rolling updates** — deploy a new image to 1 of N pods at a time, so N-1 workers keep
  running live jobs while one updates.
- **GPU-aware scheduling** — a pod that asks for 1 GPU will only ever land on a node that has
  one free; you don't manually track which box has a spare card.
- **One consistent picture of the fleet** — `kubectl get pods -A` across every worker,
  instead of SSHing into each one.

The cost is real too: it's another system to understand and operate, and for 3 boxes it's
arguably more machinery than the problem needs — see [§5](#5-honest-tradeoffs).

---

## 2. How this maps onto the CCTV app specifically

Nothing about the *application* changes. The worker is still the same Python code, same
Dockerfile, same image, same environment variables it always had
(`WORKER_ID`, `MAIN_BACKEND_URL`, `DB_ACCESS_MODE=backend`, etc.). Kubernetes only changes
**how the container gets started and kept alive** — not what it does once running.

```
                    ┌─────────────────────────────────────────┐
                    │              Kubernetes cluster           │
                    │            (k3s, on worker-1)              │
                    │                                            │
  You write:        │   StatefulSet "cctv-worker"                │
  "I want 1          │       │                                    │
   worker pod,  ───▶ │       ▼                                    │
   this image,       │   Pod "cctv-worker-0"                       │
   1 GPU"            │       │   ┌──────────────────────────┐    │
                    │       └──▶│ container: worker           │    │
                    │           │ image: cctv-worker:uat-<sha>│    │
                    │           │ 1× GPU attached              │    │
                    │           └──────────────────────────┘    │
                    └─────────────────────────────────────────┘
                                       │
                                       │ same HTTP calls as always
                                       ▼
                     Backend (this main VM) — /prod_cctv/api/*
                     MySQL, fleet registry, executions, etc.
```

The pod talks to the same backend, over the same HTTP API, the same way the Compose
worker does. It registers itself in the **fleet registry** (`worker-app/app/fleet_agent.py`)
exactly like every other worker, and shows up on the **GPU Monitor** page as a normal
server card — the dashboard has no idea (and doesn't need to) whether a given worker is a
Docker container or a Kubernetes pod.

---

## 3. What we actually built on worker-1 (step by step)

### 3.1 k3s: a small, single-binary Kubernetes

We used **k3s** (Rancher's lightweight Kubernetes distro) instead of full upstream
Kubernetes. Same API, same concepts, but it's one binary, one systemd service, and it
bundles its own `containerd` and networking — much less to install and operate than a
"real" `kubeadm` cluster, and it's what you'd actually run on 3 bare-metal boxes like ours.

```bash
curl -sfL https://get.k3s.io | sh -s - server \
  --cluster-init \            # embedded etcd — lets worker-2/3 join later as an HA cluster
  --disable traefik \         # no ingress/load-balancer controller needed (workers only call OUT to the backend)
  --disable servicelb \
  --write-kubeconfig-mode 644
```

This one command installs and starts everything: the API server, the scheduler, the
controllers, `containerd`, and a network layer (`flannel`) so pods can talk to each other.
Afterwards, `worker-1` is a fully-functional 1-node Kubernetes cluster — `kubectl get nodes`
already shows it `Ready`.

**Nothing about the existing Docker setup changed.** k3s runs its own `containerd` at
`/run/k3s/containerd`, completely separate from `/var/run/docker.sock`. The live worker
container (`cctv_script_server_worker_worker-1`, still Compose, still serving real exams)
never noticed k3s arrive.

### 3.2 GPU support: the GPU Operator

For a pod to be handed a GPU, the cluster's container runtime needs to know how to attach
one. We installed NVIDIA's **GPU Operator** via Helm:

```bash
helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace \
  --set driver.enabled=false \    # the NVIDIA driver is already installed on the host (Docker uses it)
  --set toolkit.enabled=false     # nvidia-container-toolkit is already installed too
```

**Gotcha worth remembering:** we initially assumed we'd need to hand-edit k3s's
`containerd` config to wire in the NVIDIA runtime (the way you'd do it for plain Docker).
That's *wrong* for k3s — **k3s auto-detects `nvidia-container-runtime` on the host and
wires it into its own containerd config by itself.** Our manual edit actually broke the
node (it replaced k3s's whole default containerd template, including the CNI network
config, and the node went `NotReady`). The fix was to delete our edit and let k3s
regenerate its own default — which already had the GPU runtime auto-configured. Lesson:
**with k3s, don't hand-edit generated config unless you've first looked at what it
generates on its own.**

Once the operator's pods were up, the node advertised a GPU as an allocatable resource:

```bash
kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'
# → 1
```

That single number is what lets a pod's `resources.requests: {nvidia.com/gpu: 1}` mean
something — the scheduler will now only place such a pod on a node that has a free GPU.

### 3.3 The worker manifests (`deploy/k8s/`)

Everything Kubernetes-specific for the worker lives in `worker/deploy/k8s/`:

```
deploy/k8s/
├── base/
│   ├── namespace.yaml       # the "cctv" namespace
│   ├── configmap.yaml       # non-secret env vars (same names as .env)
│   ├── storage.yaml         # PersistentVolume(Claim) for shared frames storage (NFS)
│   ├── statefulset.yaml     # the actual pod spec — 1 replica per GPU
│   └── pdb.yaml             # "never take down more than 1 worker at once"
└── overlays/
    ├── uat/                 # per-environment tweaks (Kustomize) — patches base/
    └── dev/
```

We use **Kustomize** (built into `kubectl -k`) rather than copy-pasting the YAML per
environment: `base/` holds the one true spec, and each `overlays/<env>/kustomization.yaml`
patches just the couple of fields that differ (which backend URL to call, replica count).
This is the Kubernetes-native version of "same `docker-compose.yml`, different `.env`
values per host."

**How it maps to the Compose file you already know**, side by side:

| `deploy/docker-compose.uat.yml` | `deploy/k8s/base/*.yaml` |
|---|---|
| `image: ...cctv-worker:uat` | `statefulset.yaml` → `spec.template.spec.containers[0].image` |
| `env_file: .env` + `environment:` | `configmap.yaml` (non-secret) + a `Secret` (API_KEY etc.) |
| `volumes: - ./models:/app/models` | *(not needed — models are baked into the image now, see §3.4)* |
| `volumes: - ${FRAMES_HOST_PATH}:/app/.../results` | `storage.yaml` PVC, mounted the same path |
| `deploy.resources.reservations.devices: [gpu]` | `resources.requests: {nvidia.com/gpu: 1}` |
| one `docker compose up -d` per host, by hand | `kubectl apply -k overlays/uat` — the cluster places it |

Applying it:

```bash
kubectl apply -k deploy/k8s/overlays/uat
```

This single command created the namespace, the ConfigMap, the storage claim, the pod, and
the disruption budget — Kubernetes diffed what we asked for against what existed and
created only what was missing.

### 3.4 Model weights: baked into the image, not copied by hand

Before this, a new worker host needed someone to manually `scp` YOLO `.pt` files into its
`./models/` folder — exactly the kind of manual step that doesn't scale. We fixed this at
the image level, independent of Kubernetes:

- `models.lock.json` pins each model file by **sha256**.
- The `Dockerfile` downloads and verifies them into `/opt/models` at *build* time.
- `docker-entrypoint.sh` runs `fetch_models.py` at *container start*, which seeds
  `/app/models` from `/opt/models` (fast, no network) or re-downloads and re-verifies if
  the image copy is somehow missing.

So a brand new node — Compose or Kubernetes, doesn't matter — gets working, verified model
weights just by running the image. No manual copying, ever again.

### 3.5 Pulling the image: no credentials needed

We initially assumed `ghcr.io/aman-tiwari18/cctv-worker` was private and planned to create a
Kubernetes `imagePullSecret` with a GitHub token. Turned out unnecessary — **the package is
public**, confirmed by `docker pull` succeeding with zero login. So the pod's spec has no
`imagePullSecrets` at all; it pulls the same way `docker pull` does, straight from GHCR.

### 3.6 Result

```bash
$ kubectl -n cctv get pods
NAME            READY   STATUS    RESTARTS   AGE
cctv-worker-0   1/1     Running   0          45m
```

The pod registered itself with the backend's fleet registry exactly like a Compose worker
does, and now shows up as a second server on the GPU Monitor page — same dashboard, same
API, no frontend changes needed.

---

## 4. Day-to-day commands (worker-1)

`kubectl` works directly as `root` on worker-1 (it reads `/etc/rancher/k3s/k3s.yaml`
automatically). This is your `docker ps` / `docker logs` / `docker restart` replacement
**for this one pod only** — the Compose worker on the same box still uses plain `docker`.

| What you want | Command |
|---|---|
| Is it running? | `kubectl -n cctv get pods` |
| Watch it live | `kubectl -n cctv get pods -w` |
| See its logs | `kubectl -n cctv logs cctv-worker-0 --tail 50` |
| Follow logs live | `kubectl -n cctv logs cctv-worker-0 -f` |
| Shell into it | `kubectl -n cctv exec -it cctv-worker-0 -- bash` |
| Why is it not starting? | `kubectl -n cctv describe pod cctv-worker-0` (read the Events at the bottom) |
| Restart it (new env/config) | `kubectl -n cctv rollout restart statefulset/cctv-worker` |
| Deploy a new image | edit `overlays/uat/kustomization.yaml`'s tag, `kubectl apply -k overlays/uat` |
| Scale to 2 pods | `kubectl -n cctv scale statefulset/cctv-worker --replicas=2` (needs a 2nd GPU node) |
| Is the node itself healthy? | `kubectl get nodes` |
| Raw containerd view (bypasses k8s) | `sudo k3s crictl ps` |

From the main VM, without SSHing anywhere, the fastest health check is the backend API
itself (same data the GPU Monitor page reads):

```bash
curl -s localhost:4000/prod_cctv/api/gpu-monitor/stats | python3 -c "
import sys,json
for s in json.load(sys.stdin)['data']['servers']:
    print(s['server_id'], s['status'], s.get('hostname'))"
```

---

## 5. Honest tradeoffs

- **This is one node.** The scaling story ("add a node, it just joins and takes work") is
  written into the manifests (`COORDINATION_MODE=lease`) but has not been exercised with a
  second/third node, and lease-based camera sharding has not been proven under a real
  live shift yet — it's currently dormant, same as it was before this work.
- **Three GPU boxes is a small fleet for Kubernetes.** The main payoff (self-healing across
  many machines, painless scale-out) grows with fleet size. At 3 nodes, Compose-per-host
  plus good monitoring gets you most of the reliability with a fraction of the operational
  surface. We're doing this because horizontal scaling is an explicit goal
  (see `docs/system_architecture_and_scaling.md` in the monorepo) — not because 3 Compose
  workers were broken.
- **Storage now depends on NFS.** Frames used to be a local bind mount on the same host as
  the backend (fine with one co-located worker). A remote pod needs shared storage, so we
  NFS-exported `/data/new_dev/frames` from the main VM. That's a new dependency worth
  knowing about: if that NFS export or the network between worker-1 and the main VM ever
  goes down, worker-1's pod can't write frames.
- **Cut-over, not yet automated.** Moving worker-2/3 to Kubernetes means installing k3s on
  them (join the existing cluster) and, at some point, retiring their Compose containers —
  a manual, one-host-at-a-time operation, not something this setup does for you.

See `deploy/k8s/README.md` for the exact bootstrap commands and known gotchas if you're
setting up worker-2 or worker-3 next.
