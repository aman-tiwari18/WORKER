# Scaling the worker on Kubernetes

Status: **manifests only — not yet applied to any cluster.** Workers 1–3 currently run under
Docker Compose. Nothing here has been validated against a live API server; run
`kubectl apply -k overlays/dev --dry-run=server` first.

## Why this scales without manual steps
| Concern | Mechanism |
|---|---|
| Model weights | Pinned in `models.lock.json` (sha256), baked into the image at build (`/opt/models`), verified/seeded at container start by `fetch_models.py`. No per-host copy. |
| Which cameras a worker owns | `COORDINATION_MODE=lease`: workers claim shards from the backend, so adding/removing a pod rebalances automatically. Hash mode (`WORKER_INDEX/TOTAL_WORKERS`) does **not** scale and must not be used here. |
| Identity | StatefulSet ordinal → `WORKER_ID=cctv-worker-N`; `WORKER_HOSTNAME` = the node. |
| One GPU per pod | `nvidia.com/gpu: 1` request + pod anti-affinity. |
| Safe rollouts / node drain | `maxUnavailable: 1` (StatefulSet + PDB), `preStop` marks the worker DRAINING in the fleet registry, 150 s grace for in-flight jobs. |
| Config | ConfigMap (non-secret) + `cctv-worker-secrets` Secret (API_KEY, CAMVIEW token) created out-of-band. |

**Scale out:** add a GPU node to the cluster, then `kubectl -n cctv scale sts/cctv-worker --replicas=4`.
**Scale in:** reduce replicas; the pod drains, releases its leases, others pick up its shards.

## Cluster bootstrap for workers 1–3 (192.168.19.41-43, 1×H200 each)
1. On every node: NVIDIA driver + `nvidia-container-toolkit` (already present for Docker).
2. `curl -sfL https://get.k3s.io | K3S_TOKEN=... sh -s - server --cluster-init` on worker-1;
   join worker-2/3 with `server --server https://192.168.19.41:6443` (3-node embedded-etcd HA;
   all three schedule GPU work).
3. GPU Operator (driver disabled — host driver is used):
   `helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace --set driver.enabled=false`
4. `kubectl create secret docker-registry ghcr-pull -n cctv ...` and `cctv-worker-secrets`.
5. `kubectl apply -k overlays/uat`.

## Not done / decisions needed
- **Automatic (metric-driven) scaling.** GPU pods can only scale up to the number of free GPUs, so
  autoscaling really means node autoscaling (cloud) or an operator adding hosts (bare metal). To let KEDA
  scale replicas from backlog, the backend must first expose a "pending camera-shards" metric — it does not today.
- Cut-over from Compose on w1–3 (stop the compose container on a node before its pod starts, one node at a time).
- The main VM's GPU (worker-0/uat) and the dev VM stay on Compose until the cluster is proven.
