# KubeFleet + Cilium — Zero-Downtime Workload Migration

This guide demonstrates moving a running application from one Kubernetes cluster
to another with **zero downtime**, using [KubeFleet](https://kubefleet.dev) for
placement control and [Cilium Cluster Mesh](https://docs.cilium.io/en/stable/network/clustermesh/)
for cross-cluster traffic routing.

**What you'll see:** the browser URL never changes, the port-forward never
restarts, yet the app seamlessly migrates from `member-01` to `member-02`.

## Prerequisites

- [Docker](https://docs.docker.com/desktop/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [helm](https://helm.sh/docs/intro/install/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [cilium CLI](https://docs.cilium.io/en/stable/gettingstarted/k8s-install-default/#install-the-cilium-cli)
- [gh](https://cli.github.com/) (GitHub CLI, for pushing images to ghcr.io)

## 1. Create Kind clusters with Cilium

Create three clusters with the default CNI and kube-proxy disabled (Cilium
replaces both):

```bash
kind create cluster --name kf-hub-01 --config kind-cilium-config.yaml
kind create cluster --name kf-member-01 --config kind-cilium-config.yaml
kind create cluster --name kf-member-02 --config kind-cilium-config.yaml
```

## 2. Install Cilium on member clusters

The hub cluster only runs the KubeFleet control plane — it doesn't need Cilium
mesh. Install Cilium on the two member clusters with unique cluster IDs:

```bash
cilium install --context kind-kf-member-01 --set cluster.id=1 --set cluster.name=member-01
cilium install --context kind-kf-member-02 --set cluster.id=2 --set cluster.name=member-02
```

Wait for Cilium to be ready:

```bash
cilium status --context kind-kf-member-01 --wait
cilium status --context kind-kf-member-02 --wait
```

Install Cilium on the hub too (needed for pod networking, but no mesh):

```bash
cilium install --context kind-kf-hub-01 --set cluster.id=3 --set cluster.name=hub
cilium status --context kind-kf-hub-01 --wait
```

## 3. Enable Cilium Cluster Mesh

Connect the two member clusters so they share service endpoints:

```bash
cilium clustermesh enable --context kind-kf-member-01 --service-type NodePort
cilium clustermesh enable --context kind-kf-member-02 --service-type NodePort

# Wait for mesh to be ready
cilium clustermesh status --context kind-kf-member-01 --wait
cilium clustermesh status --context kind-kf-member-02 --wait

# Connect them (--allow-mismatching-ca needed because each Kind cluster has its own Cilium CA)
cilium clustermesh connect --context kind-kf-member-01 --destination-context kind-kf-member-02 --allow-mismatching-ca

# Verify the connection
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
```

Verify the hub agent is running:

```bash
kubectl get pods -n fleet-system
```

### Join member clusters

```bash
HUB_IP=$(docker inspect kf-hub-01-control-plane --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

cd ~/kubefleet/hack/quickstart
./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-01
./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-02
```

Verify members joined:

```bash
kubectl --context kind-kf-hub-01 get memberclusters
```

Both should show `JOINED: True`.

### Label member clusters

```bash
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-01 environment=staging
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-02 environment=prod
```

## 5. Build and push the sample app

```bash
cd ~/kubefleet-sample-app

# Log in to ghcr.io (use a GitHub PAT with write:packages scope)
echo "YOUR_GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Build and push with a versioned tag
VERSION=$(date +%Y%m%d%H%M%S)

docker build -t ghcr.io/weng271190436/kubefleet-sample-app/backend:$VERSION ./backend
docker build -t ghcr.io/weng271190436/kubefleet-sample-app/frontend:$VERSION ./frontend

docker push ghcr.io/weng271190436/kubefleet-sample-app/backend:$VERSION
docker push ghcr.io/weng271190436/kubefleet-sample-app/frontend:$VERSION
```

> **Note:** Make sure your ghcr.io packages are set to **public** so Kind nodes
> can pull them.

## 6. Deploy the app on the hub

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

## 7. Create ResourceOverrides

These set the `CLUSTER_NAME` env var per member cluster so the UI shows which
cluster is serving. **Create these before the CRP** so the override is included
in the first resource snapshot:

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/cilium-demo/cluster-overrides.yaml
```

## 8. Deploy to member-01 only

Create the CRP targeting just member-01:

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/cilium-demo/crp-member01-only.yaml
```

Wait for the app to be running on member-01:

```bash
kubectl --context kind-kf-member-01 -n kubefleet-sample get pods -w
```

### Open the app in your browser

Start a port-forward from member-01 — **keep this running for the entire demo**:

```bash
kubectl --context kind-kf-member-01 port-forward -n kubefleet-sample svc/sample-frontend 8081:80 --address 0.0.0.0
```

Open `http://localhost:8081` in your browser.

You should see the banking config app with a green chip saying
**"Serving from: member-01 (staging)"**.

## 9. Expand to both clusters (bridge phase)

Update the CRP to target both members:

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/cilium-demo/crp-both.yaml
```

Wait for pods on member-02:

```bash
kubectl --context kind-kf-member-02 -n kubefleet-sample get pods -w
```

Refresh the browser a few times. You should see the chip alternate between
**"member-01 (staging)"** and **"member-02 (prod)"** — Cilium Cluster Mesh is
load-balancing across both clusters.

> **Key insight:** The port-forward is still connected to member-01's Service,
> but Cilium's global service routes requests to pods on *either* cluster.

## 10. Complete the migration (drain member-01)

Update the CRP to target only member-02:

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/cilium-demo/crp-member02-only.yaml
```

KubeFleet removes the workload from member-01. Verify:

```bash
kubectl --context kind-kf-member-01 -n kubefleet-sample get pods
# Should show no pods (or pods terminating)
```

Refresh the browser. The chip now shows **"Serving from: member-02 (prod)"**
exclusively.

**The URL never changed. The port-forward never restarted. Zero downtime.**

## 11. Verify

### KubeFleet state

```bash
kubectl --context kind-kf-hub-01 get clusterresourceplacements sample-crp -o yaml
kubectl --context kind-kf-hub-01 get clusterresourcebindings -l crp=sample-crp
```

### Cilium state

```bash
# Service endpoints on member-01 — should show remote endpoints from member-02
cilium --context kind-kf-member-01 service list | grep sample

# Confirm mesh connectivity
cilium clustermesh status --context kind-kf-member-01
```

### Cluster workloads

```bash
# member-01: no app pods
kubectl --context kind-kf-member-01 -n kubefleet-sample get pods

# member-02: app running
kubectl --context kind-kf-member-02 -n kubefleet-sample get pods
```

## Cleanup

```bash
kubectl --context kind-kf-hub-01 delete crp sample-crp
kubectl --context kind-kf-hub-01 delete clusterresourceoverride cluster-name-member01 cluster-name-member02
kind delete cluster --name kf-hub-01
kind delete cluster --name kf-member-01
kind delete cluster --name kf-member-02
```

## How it works

| Layer | Technology | Role |
|-------|-----------|------|
| **Placement** | KubeFleet CRP (PickFixed) | Decides *which* clusters run the app |
| **Networking** | Cilium Cluster Mesh | Routes traffic to pods across clusters |
| **Customization** | KubeFleet ClusterResourceOverride | Sets per-cluster env vars |

The bridge phase (step 9) is the key: by briefly running on both clusters,
traffic shifts gracefully from the old to the new location. Cilium's global
service annotation makes the Kubernetes Service span cluster boundaries — any
pod behind the Service, in any meshed cluster, can receive traffic.
