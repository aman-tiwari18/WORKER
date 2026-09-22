# Scaling the worker on Kubernetes

Status: **single-node k3s bootstrapped on worker-1 (192.168.19.43)** as a first test —
GPU Operator is up and `nvidia.com/gpu: 1` is allocatable; the CCTV worker StatefulSet
itself has not been applied yet. Worker-1's existing Docker Compose worker
(`cctv_script_server_worker_worker-1`) was left running throughout — k3s uses its own
containerd (`/run/k3s/containerd`), a separate runtime from the host Docker daemon, so the
two don't collide. Workers 2–3 are still Compose-only.

## Why this scales without manual steps
| Concern | Mechanism |
|---|---|
| Model weights | Pinned in `models.lock.json` (sha256), baked into the image at build (`/opt/models`), verified/seeded at container start by `fetch_models.py`. No per-host copy. |
| Which cameras a worker owns | `COORDINATION_MODE=lease`: workers claim shards from the backend, so adding/removing a pod rebalances automatically. Hash mode (`WORKER_INDEX/TOTAL_WORKERS`) does **not** scale and must not be used here. |
| Identity | StatefulSet ordinal → `WORKER_ID=cctv-worker-N`; `WORKER_HOSTNAME` = the node. |
| One GPU per pod | `nvidia.com/gpu: 1` request + pod anti-affinity. |
| Safe rollouts / node drain | `maxUnavailable: 1` (StatefulSet + PDB), `preStop` marks the worker DRAINING in the fleet registry, 150 s grace for in-flight jobs. |
| Config | ConfigMap (non-secret) + `cctv-worker-secrets` Secret (API_KEY, CAMVIEW token) created out-of-band. |
| Image pull | `ghcr.io/aman-tiwari18/cctv-worker` is a **public** package — plain `docker pull` needs no login, confirmed by pulling it anonymously. No `imagePullSecret`. If the package is ever made private, add one back. |

**Scale out:** add a GPU node to the cluster, then `kubectl -n cctv scale sts/cctv-worker --replicas=4`.
**Scale in:** reduce replicas; the pod drains, releases its leases, others pick up its shards.

## Cluster bootstrap for workers 1–3 (192.168.19.41-43, 1×H200 each)
Done so far on worker-1 only; steps 2 (join) and later apply to worker-2/3 and are untested.

1. On every node: NVIDIA driver + `nvidia-container-toolkit` (already present — same toolkit Docker uses).
2. `curl -sfL https://get.k3s.io | sh -s - server --cluster-init --disable traefik --disable servicelb
   --write-kubeconfig-mode 644` on worker-1 (done); join worker-2/3 with
   `server --server https://192.168.19.43:6443 --token <from /var/lib/rancher/k3s/server/node-token>`
   (3-node embedded-etcd HA; all three schedule GPU work). Traefik/servicelb are disabled — this
   cluster has no ingress/LoadBalancer need yet, workers only call out to the backend.
3. Helm, then GPU Operator with **both driver and toolkit disabled** — the host already has the
   NVIDIA driver and nvidia-container-toolkit for Docker, and k3s **auto-detects**
   `nvidia-container-runtime` on the host and wires it into its own bundled containerd on its own
   (visible as `runtimes.'nvidia'` in `/var/lib/rancher/k3s/agent/etc/containerd/config.toml`) —
   no manual containerd/config.toml.tmpl editing needed, and hand-editing that template actively
   broke the node (see "gotchas" below) before this was discovered:
   `helm repo add nvidia https://helm.ngc.nvidia.com/nvidia && helm install gpu-operator nvidia/gpu-operator
   -n gpu-operator --create-namespace --set driver.enabled=false --set toolkit.enabled=false`
   Confirms via: `kubectl get nodes -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}'` → `1`.
4. `kubectl create secret generic cctv-worker-secrets -n cctv --from-literal=API_KEY=... --from-literal=CAMVIEW_TOKEN_SERVER=...`
   (no image pull secret needed — see table above).
5. `kubectl apply -k overlays/uat`. **Not run yet** — next step for this node.

### Gotcha hit during bootstrap
Running `nvidia-ctk runtime configure --config=.../config.toml.tmpl` against k3s's containerd
wrote a *replacement* template (just `imports=[...]` + `version=2`), which dropped ALL of k3s's own
containerd defaults — including the CNI bin/conf dirs — and the node went `NotReady`
(`cni plugin not initialized`). Fix was to delete the `.tmpl` override entirely and let k3s
regenerate its default `config.toml`, which already had the `nvidia` runtime auto-wired. If a
future host needs a *manual* containerd tweak, copy the live generated `config.toml` as the `.tmpl`
base and append to it — never let a tool write a fresh minimal template over it.

## Not done / decisions needed
- **Automatic (metric-driven) scaling.** GPU pods can only scale up to the number of free GPUs, so
  autoscaling really means node autoscaling (cloud) or an operator adding hosts (bare metal). To let KEDA
  scale replicas from backlog, the backend must first expose a "pending camera-shards" metric — it does not today.
- Cut-over from Compose on w1–3 (stop the compose container on a node before its pod starts, one node at a time).
- The main VM's GPU (worker-0/uat) and the dev VM stay on Compose until the cluster is proven.
