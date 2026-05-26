# KubeFleet + Cilium — Zero-Downtime Workload Migration

This guide demonstrates moving a running application from one Kubernetes cluster
to another with **zero downtime**, using [KubeFleet](https://kubefleet.dev) for
placement control and [Cilium Cluster Mesh](https://docs.cilium.io/en/stable/network/clustermesh/)
for cross-cluster traffic routing.

**What you'll see:** the browser URL never changes, the proxy never restarts,
yet the app seamlessly migrates from `member-01` to `member-02`.

## Architecture

```
                     ┌───────────────────────────────────┐
                     │          Hub Cluster               │
                     │  CRP (PickAll) + ResourceOverrides │
                     └───────────┬───────────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                ▼                                  ▼
    ┌──────────────────┐             ┌──────────────────┐
    │   member-01       │   Cilium   │   member-02       │
    │   (eastus)        │◄──Mesh────►│   (westus)        │
    │   Namespace ✓     │            │   Namespace ✓     │
    │   Service   ✓     │            │   Service   ✓     │
    │   Pods: controlled│            │   Pods: controlled│
    │   by override     │            │   by override     │
    └──────────────────┘             └──────────────────┘
            ▲
            │ socat proxy (localhost:8081 → NodePort)
            │
       🌐 Browser
```

The CRP uses **PickAll** so the namespace, Services, and Deployments exist on
**both** member clusters at all times. Pod replicas are controlled by
**ResourceOverride** — scaling a cluster to zero stops its pods but keeps the
Service, which is essential for Cilium global service routing.

## Prerequisites

| Tool | Purpose |
|------|---------|
| [Docker](https://docs.docker.com/desktop/) | Container runtime for Kind |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) | Local Kubernetes clusters |
| [helm](https://helm.sh/docs/intro/install/) | Install KubeFleet charts |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | Cluster management |
| [cilium CLI](https://docs.cilium.io/en/stable/gettingstarted/k8s-install-default/#install-the-cilium-cli) | Install and manage Cilium |
| [socat](https://linux.die.net/man/1/socat) | TCP proxy to Kind node (`apt install socat` / `brew install socat`) |

## 1. Create Kind clusters

Create three clusters with the default CNI and kube-proxy disabled (Cilium
replaces both):

```bash
kind create cluster --name kf-hub-01 --config kind-cilium-config.yaml
kind create cluster --name kf-member-01 --config kind-cilium-config.yaml
kind create cluster --name kf-member-02 --config kind-cilium-config.yaml
```

<details>
<summary>kind-cilium-config.yaml</summary>

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true   # Cilium will replace the default CNI
  kubeProxyMode: none        # Cilium replaces kube-proxy
nodes:
  - role: control-plane
```
</details>

## 2. Install Cilium

Install Cilium on all three clusters. The hub needs Cilium for pod networking
but doesn't join the mesh. The two member clusters get unique cluster IDs for
mesh identity:

```bash
# Member clusters (with mesh IDs)
cilium install --context kind-kf-member-01 --set cluster.id=1 --set cluster.name=member-01
cilium install --context kind-kf-member-02 --set cluster.id=2 --set cluster.name=member-02

# Hub (networking only, no mesh)
cilium install --context kind-kf-hub-01 --set cluster.id=3 --set cluster.name=hub

# Wait for all to be ready
cilium status --context kind-kf-member-01 --wait
cilium status --context kind-kf-member-02 --wait
cilium status --context kind-kf-hub-01 --wait
```

## 3. Enable Cilium Cluster Mesh

Connect the two member clusters so they share service endpoints:

```bash
cilium clustermesh enable --context kind-kf-member-01 --service-type NodePort
cilium clustermesh enable --context kind-kf-member-02 --service-type NodePort

# Wait for mesh control planes
cilium clustermesh status --context kind-kf-member-01 --wait
cilium clustermesh status --context kind-kf-member-02 --wait

# Connect them
# (--allow-mismatching-ca is needed because each Kind cluster generates its own Cilium CA)
cilium clustermesh connect \
  --context kind-kf-member-01 \
  --destination-context kind-kf-member-02 \
  --allow-mismatching-ca

# Verify — should show "connected" with 1 remote cluster
cilium clustermesh status --context kind-kf-member-01 --wait
```

## 4. Install KubeFleet

### Hub agent

```bash
kubectl config use-context kind-kf-hub-01

helm upgrade --install hub-agent oci://ghcr.io/kubefleet-dev/kubefleet/charts/hub-agent \
    --version 0.3.1 \
    --namespace fleet-system \
    --create-namespace \
    --set logFileMaxSize=100000

kubectl get pods -n fleet-system   # verify it's running
```

### Join member clusters

```bash
HUB_IP=$(docker inspect kf-hub-01-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

cd ~/kubefleet/hack/quickstart
./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-01
./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-02

kubectl --context kind-kf-hub-01 get memberclusters   # both should show JOINED: True
```

### Label member clusters

These labels are used by ResourceOverride to target specific clusters:

```bash
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-01 region=eastus
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-02 region=westus
```

## 5. Build and push the sample app

```bash
cd ~/kubefleet-sample-app

# Log in to ghcr.io (use a GitHub PAT with write:packages scope)
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u weng271190436 --password-stdin

VERSION=$(date +%Y%m%d%H%M%S)

docker build -t ghcr.io/weng271190436/kubefleet-sample-app/backend:$VERSION ./backend
docker build -t ghcr.io/weng271190436/kubefleet-sample-app/frontend:$VERSION ./frontend

docker push ghcr.io/weng271190436/kubefleet-sample-app/backend:$VERSION
docker push ghcr.io/weng271190436/kubefleet-sample-app/frontend:$VERSION
```

> **Note:** Make sure your ghcr.io packages are set to **public** so Kind nodes
> can pull them without image pull secrets.

## 6. Deploy the app on the hub

The hub holds the "source of truth" resources. KubeFleet propagates them to
member clusters via the CRP.

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/namespace.yaml
kubectl --context kind-kf-hub-01 -n kubefleet-sample apply -f k8s/backend.yaml
kubectl --context kind-kf-hub-01 -n kubefleet-sample apply -f k8s/frontend.yaml

# Set the versioned image tags
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-backend backend=ghcr.io/weng271190436/kubefleet-sample-app/backend:$VERSION
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-frontend frontend=ghcr.io/weng271190436/kubefleet-sample-app/frontend:$VERSION
```

The Service YAMLs include Cilium global service annotations that make the
Service discoverable across the mesh:

```yaml
annotations:
  io.cilium/global-service: "true"
  io.cilium/shared-service: "true"
```

## 7. Phase 1 — App runs on member-01 (eastus) only

### Create ResourceOverrides and CRP

ResourceOverrides control two things per cluster:
1. **Replica count** — 0 to stop pods, 1 (default) to run them
2. **CLUSTER_NAME env var** — so the UI shows which cluster is serving

> **Important:** Create the overrides *before* the CRP. KubeFleet captures
> overrides in the resource snapshot at CRP creation time.

```bash
cd ~/kubefleet-sample-app/k8s/cilium-demo

# Apply phase 1 overrides: member-01 (eastus) replicas=1, member-02 (westus) replicas=0
kubectl --context kind-kf-hub-01 apply -f override-phase1-member01-only.yaml

# Create the PickAll CRP — deploys to ALL member clusters
kubectl --context kind-kf-hub-01 apply -f crp-pickall.yaml
```

Wait for pods on member-01:

```bash
kubectl --context kind-kf-member-01 -n kubefleet-sample get pods -w
```

Verify member-02 has the namespace and Services but no running pods:

```bash
kubectl --context kind-kf-member-02 -n kubefleet-sample get deploy
# READY should be 0/0 for both deployments
```

### Open the app in your browser

Since both clusters have the Service, we use `socat` to proxy traffic through
member-01's NodePort. This is more resilient than `kubectl port-forward` because
it survives pod deletions — the proxy connects to the **Service**, not a pod.

```bash
# Get member-01's Kind node IP and the frontend NodePort
MEMBER01_IP=$(docker inspect kf-member-01-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
NODE_PORT=$(kubectl --context kind-kf-member-01 -n kubefleet-sample \
  get svc sample-frontend -o jsonpath='{.spec.ports[0].nodePort}')

echo "Proxying localhost:8081 → ${MEMBER01_IP}:${NODE_PORT}"

# Start socat in the background — keep this running for the entire demo
socat TCP-LISTEN:8081,fork,reuseaddr TCP:${MEMBER01_IP}:${NODE_PORT} &
SOCAT_PID=$!
echo "socat PID: $SOCAT_PID"
```

Open **http://localhost:8081** in your browser.

You should see the banking config app with a green chip saying
**"Serving from: member-01 (eastus)"**.

## 8. Phase 2 — Expand to both clusters (bridge)

Update the overrides to run pods on both clusters. The frontend-override is no
longer needed (no replicas to suppress), so delete it:

```bash
# Delete the frontend-override (both clusters should run frontend)
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  delete resourceoverride frontend-override

# Update the backend-override (remove replica suppression, keep CLUSTER_NAME)
kubectl --context kind-kf-hub-01 apply -f override-phase2-both.yaml

# Trigger re-reconciliation by annotating a hub resource
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  annotate deploy sample-backend demo-phase=2 --overwrite
```

Wait for pods on member-02:

```bash
kubectl --context kind-kf-member-02 -n kubefleet-sample get pods -w
```

**Refresh the browser** several times. You should see the chip alternate between
**"member-01 (eastus)"** and **"member-02 (westus)"** — Cilium Cluster Mesh is
load-balancing across both clusters.

> **Key insight:** The socat proxy still points at member-01's NodePort, but
> Cilium's global service routes requests to pods on *either* cluster. The
> Service object on member-01 sees both local and remote endpoints.

### Verify both clusters are serving

```bash
for i in $(seq 1 10); do
  curl -s http://localhost:8081/api/cluster-info | python3 -c "import sys,json; print(json.load(sys.stdin)['cluster'])"
done
```

You should see a mix of `member-01 (eastus)` and `member-02 (westus)`.

## 9. Phase 3 — Complete the migration (drain eastus)

Update the overrides to stop pods on member-01 and keep them on member-02.
Phase 3 re-creates the frontend-override (to scale eastus frontend to 0):

```bash
kubectl --context kind-kf-hub-01 apply -f override-phase3-member02-only.yaml

# Trigger re-reconciliation
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  annotate deploy sample-backend demo-phase=3 --overwrite
```

Wait ~20 seconds, then verify:

```bash
# member-01: deployments scaled to 0, but Service still exists
kubectl --context kind-kf-member-01 -n kubefleet-sample get deploy
# READY: 0/0

# member-02: app running
kubectl --context kind-kf-member-02 -n kubefleet-sample get pods
# READY: 1/1
```

**Refresh the browser.** The chip now shows **"Serving from: member-02 (westus)"**
exclusively.

```bash
for i in $(seq 1 5); do
  curl -s http://localhost:8081/api/cluster-info | python3 -c "import sys,json; print(json.load(sys.stdin)['cluster'])"
done
# All requests: member-02 (westus)
```

**The URL never changed. The proxy never restarted. Zero downtime.**

## 10. Verify

### How traffic flows after migration

```
Browser → localhost:8081
       → socat → member-01 NodePort
       → member-01 Service (sample-frontend)
       → Cilium sees no local endpoints, routes to remote
       → member-02 pod (sample-frontend)
       → member-02 pod (sample-backend) via global service
       → response flows back
```

### KubeFleet state

```bash
kubectl --context kind-kf-hub-01 get clusterresourceplacements sample-crp
kubectl --context kind-kf-hub-01 get clusterresourcebindings -l crp=sample-crp
kubectl --context kind-kf-hub-01 -n kubefleet-sample get resourceoverride
```

### Cilium state

```bash
# Service endpoints on member-01 — should show remote endpoints from member-02
cilium --context kind-kf-member-01 service list | grep sample

# Mesh status
cilium clustermesh status --context kind-kf-member-01
```

## Cleanup

```bash
# Stop the socat proxy
kill $SOCAT_PID 2>/dev/null

# Remove KubeFleet resources
kubectl --context kind-kf-hub-01 delete crp sample-crp
kubectl --context kind-kf-hub-01 -n kubefleet-sample delete resourceoverride backend-override frontend-override

# Tear down clusters
kind delete cluster --name kf-hub-01
kind delete cluster --name kf-member-01
kind delete cluster --name kf-member-02
```

## How it works

| Layer | Technology | Role |
|-------|-----------|------|
| **Placement** | KubeFleet CRP (**PickAll**) | Deploys namespace + resources to *all* member clusters |
| **Replica control** | KubeFleet **ResourceOverride** | Scales pods to 0 or 1 per cluster |
| **Networking** | Cilium **Cluster Mesh** | Routes traffic to pods across clusters via global services |
| **Customization** | ResourceOverride (env patch) | Sets per-cluster `CLUSTER_NAME` env var |

### Why PickAll + ResourceOverride instead of PickFixed?

The naive approach is to use **PickFixed** and change which clusters the CRP
targets. However, when KubeFleet removes a cluster from a PickFixed placement,
it deletes the **entire namespace** on that cluster — including the Service.

Cilium global services require the **Service object** to exist on both clusters.
If the Service is deleted on member-01, Cilium has no local endpoint record and
can't route traffic cross-cluster. The socat proxy (or any NodePort client)
gets connection refused.

**PickAll** solves this by keeping the namespace, Services, and Deployments on
every member cluster at all times. ResourceOverride controls which clusters
have *running pods* by patching `spec.replicas` to 0. The Service stays intact,
Cilium keeps its global routing table, and traffic flows seamlessly to whichever
cluster has pods.

### Phase flow summary

| Phase | member-01 pods | member-02 pods | Traffic goes to |
|-------|---------------|---------------|-----------------|
| 1. Initial | ✅ Running | ⬜ Scaled to 0 | eastus only |
| 2. Bridge | ✅ Running | ✅ Running | Both (Cilium LB) |
| 3. Migrated | ⬜ Scaled to 0 | ✅ Running | westus only |
