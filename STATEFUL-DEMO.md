# KubeFleet Stateful Workload Demo: PostgreSQL Streaming Replication

This demo deploys a real PostgreSQL primary/replica pair across two Kind clusters
managed by KubeFleet, then demonstrates a **planned region migration** by
swapping the primary and replica roles through a staged rollout.

## Architecture

```
┌─────────────────┐
│   pg-hub        │   KubeFleet Hub Agent (0.3.1)
│   (kind)        │   CRP, ResourceOverrides, ClusterStagedUpdateRun
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───┴───┐ ┌──┴────┐
│us-west│ │us-east│   Member clusters (Kind)
│primary│ │replica│   Labels: role=primary/replica, region=us-west/us-east
│       │ │       │
│ PG ──────→ PG  │   Streaming replication via hostPort + Docker bridge
│Backend│ │Backend│   Read-write vs read-only (enforced by app)
│Frontend│ │Frontend│
└───────┘ └───────┘
```

## What this demonstrates

1. **Namespace-scoped placement** — a single CRP selects the `kubefleet-pg`
   namespace; KubeFleet propagates all resources inside it.
2. **ResourceOverride with JSON Patch** — per-cluster env vars set PG role,
   primary host IP, cluster display name, and read-only mode.
3. **Staged rollout (External strategy)** — a `ClusterStagedUpdateRun` deploys
   the primary first, waits 30 seconds, then deploys the replica.
4. **Planned region migration** — swap labels + update overrides + new rollout →
   the old replica becomes the new primary and vice versa.

## Prerequisites

- Docker, Kind, kubectl, Helm
- `~3 GB` free memory (3 Kind clusters)

## Quick Start

### 1. Create clusters and deploy KubeFleet

```bash
# Create 3 Kind clusters
kind create cluster --name pg-hub
kind create cluster --name pg-us-west
kind create cluster --name pg-us-east

# Install KubeFleet hub agent
kubectl config use-context kind-pg-hub
helm upgrade --install hub-agent \
  oci://ghcr.io/kubefleet-dev/kubefleet/charts/hub-agent \
  --version 0.3.1 \
  --namespace fleet-system --create-namespace \
  --set logFileMaxSize=100000

# Join member clusters (run from kubefleet repo)
HUB_IP=$(docker inspect pg-hub-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
cd ~/kubefleet/hack/quickstart
for member in kind-pg-us-west kind-pg-us-east; do
  ./join-member-clusters.sh 0.3.1 kind-pg-hub "https://${HUB_IP}:6443/" "$member"
done
```

> **Note:** The first member join may fail with a webhook connection error (the
> webhook pod isn't ready yet). Wait 10 seconds, then manually create the
> MemberCluster:
> ```bash
> kubectl --context kind-pg-hub apply -f - <<EOF
> apiVersion: cluster.kubernetes-fleet.io/v1
> kind: MemberCluster
> metadata:
>   name: kind-pg-us-west
> spec:
>   identity:
>     name: kind-pg-us-west-hub-cluster-access
>     kind: ServiceAccount
>     namespace: fleet-system
>     apiGroup: ""
>   heartbeatPeriodSeconds: 15
> EOF
> ```

### 2. Label clusters and build images

```bash
HUB=kind-pg-hub
kubectl --context $HUB label membercluster kind-pg-us-west region=us-west role=primary --overwrite
kubectl --context $HUB label membercluster kind-pg-us-east region=us-east role=replica --overwrite

# Build all images
cd ~/kubefleet-sample-app
docker build -t kubefleet-sample-postgres:dev ./postgres
docker build -t kubefleet-sample-backend-pg:dev -f ./backend/Dockerfile.pg ./backend
docker build -t kubefleet-sample-frontend:dev ./frontend

# Load into member clusters
for cluster in pg-us-west pg-us-east; do
  kind load docker-image kubefleet-sample-postgres:dev --name $cluster
  kind load docker-image kubefleet-sample-backend-pg:dev --name $cluster
  kind load docker-image kubefleet-sample-frontend:dev --name $cluster
done
```

### 3. Deploy resources to hub

```bash
HUB=kind-pg-hub
kubectl --context $HUB apply -f k8s/stateful/namespace.yaml
kubectl --context $HUB apply -f k8s/stateful/postgres.yaml
kubectl --context $HUB apply -f k8s/stateful/backend.yaml
kubectl --context $HUB apply -f k8s/stateful/frontend.yaml
```

### 4. Create KubeFleet CRP (External strategy)

```bash
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterResourcePlacement
metadata:
  name: pg-app
spec:
  resourceSelectors:
    - group: ""
      kind: Namespace
      version: v1
      name: kubefleet-pg
  policy:
    placementType: PickAll
  strategy:
    type: External
EOF
```

### 5. Create ResourceOverrides

The `PRIMARY_HOST` env var in the StatefulSet base manifest is set to `"none"`
(a non-empty placeholder). **K8s API omits `value: ""`**, which breaks JSON
Patch `replace` operations — see [Issues Discovered](#issues-discovered).

```bash
US_WEST_IP=$(docker inspect pg-us-west-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

# StatefulSet override (PG role + primary host)
kubectl --context $HUB apply -f - <<EOF
apiVersion: placement.kubernetes-fleet.io/v1
kind: ResourceOverride
metadata:
  name: pg-role-config
  namespace: kubefleet-pg
spec:
  placement:
    name: pg-app
  resourceSelectors:
    - group: apps
      kind: StatefulSet
      version: v1
      name: postgres
  policy:
    overrideRules:
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: primary
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "primary"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: replica
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "replica"
          - op: replace
            path: /spec/template/spec/containers/0/env/4/value
            value: "${US_WEST_IP}"
EOF

# Deployment override (cluster name + read-only mode)
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ResourceOverride
metadata:
  name: backend-config
  namespace: kubefleet-pg
spec:
  placement:
    name: pg-app
  resourceSelectors:
    - group: apps
      kind: Deployment
      version: v1
      name: sample-backend
  policy:
    overrideRules:
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: primary
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "US-WEST (primary)"
          - op: replace
            path: /spec/template/spec/containers/0/env/6/value
            value: "false"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: replica
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "US-EAST (replica)"
          - op: replace
            path: /spec/template/spec/containers/0/env/6/value
            value: "true"
EOF
```

### 6. Create staged rollout strategy + run

```bash
# Strategy: primary first, 30s wait, then replica
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateStrategy
metadata:
  name: pg-regional-strategy
spec:
  stages:
    - name: primary
      labelSelector:
        matchLabels:
          role: primary
      maxConcurrency: 1
      afterStageTasks:
        - type: TimedWait
          waitTime: 30s
    - name: replica
      labelSelector:
        matchLabels:
          role: replica
      maxConcurrency: 1
EOF

# Start the rollout
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: pg-deploy-001
spec:
  placementName: pg-app
  stagedRolloutStrategyName: pg-regional-strategy
  state: Run
EOF
```

### 7. Watch the rollout

```bash
# Poll stage status
kubectl --context $HUB get csur pg-deploy-001 \
  -o jsonpath='{range .status.stagesStatus[*]}{.stageName}: {range .conditions[*]}{.type}={.status}({.reason}) {end}{"\n"}{end}'
```

Expected progression:
1. `primary: Progressing=True` → pods deploy on us-west
2. `primary: Succeeded=True` → 30s timed wait
3. `replica: Progressing=True` → pods deploy on us-east, PG runs `pg_basebackup`
4. `replica: Succeeded=True` → done!

### 8. Open the application UI in the browser

Port-forward the frontend on each cluster to different local ports:

```bash
# Primary (us-west) on port 3001, Replica (us-east) on port 3002
kubectl --context kind-pg-us-west -n kubefleet-pg port-forward svc/sample-frontend 3001:80 &
kubectl --context kind-pg-us-east -n kubefleet-pg port-forward svc/sample-frontend 3002:80 &
```

Open two browser tabs:

| Tab | URL | Cluster |
|-----|-----|---------|
| Primary | http://localhost:3001 | US-WEST (primary) — read-write |
| Replica | http://localhost:3002 | US-EAST (replica) — read-only |

The cluster name and role are shown in the app bar at the top of the page.

### 9. Demonstrate stateful replication through the UI

1. **On the Primary tab** (http://localhost:3001):
   - Click the **+** button to add a new configuration entry
   - Set key to `demo.live`, value to `created during community call`, category
     to `operations`
   - The row appears in the data grid immediately

2. **Switch to the Replica tab** (http://localhost:3002):
   - **Refresh the page** — the same `demo.live` row appears, replicated from
     the primary's PostgreSQL to the replica via streaming replication
   - Try adding or editing a row — the app shows an error: **"This replica is
     read-only"**

This proves that data written on the primary cluster is automatically replicated
to the replica cluster through PostgreSQL streaming replication, while write
protection is enforced on the replica.

> **Tip:** You can also verify via CLI:
> ```bash
> kubectl --context kind-pg-us-west -n kubefleet-pg port-forward svc/sample-backend 8001:8000 &
> kubectl --context kind-pg-us-east -n kubefleet-pg port-forward svc/sample-backend 8002:8000 &
> sleep 2
>
> # Write on primary
> curl -s -X POST http://localhost:8001/api/configs \
>   -H "Content-Type: application/json" \
>   -d '{"key":"test.replication","value":"hello from primary!","category":"limits"}'
>
> # Read from replica (should appear instantly)
> curl -s http://localhost:8002/api/configs | python3 -c "
> import sys,json
> [print(f'{c[\"key\"]}={c[\"value\"]}') for c in json.load(sys.stdin) if 'replication' in c['key']]"
> ```

### 10. Visualize fleet state with Headlamp

[Headlamp](https://headlamp.dev/) with the KubeFleet Manager plugin provides
a visual dashboard for fleet resources.

#### Install Headlamp

Download the desktop app from https://headlamp.dev/docs/latest/installation/desktop/.

#### Install the KubeFleet Manager plugin

```bash
# From the Headlamp UI: Settings → Plugins → search "kubefleet-manager" → Install
# Or install from source:
cd ~/kubefleet-headlamp-plugin
npm install && npm run build
# Copy the dist/ folder to your Headlamp plugins directory
```

#### Connect to the hub cluster

1. Launch Headlamp and select the **kind-pg-hub** cluster context
2. Navigate to **KubeFleet** in the sidebar (added by the plugin)
3. You can see:
   - **Member Clusters**: `kind-pg-us-west` (role=primary) and `kind-pg-us-east`
     (role=replica) with their join status and labels
   - **Cluster Resource Placements**: `pg-app` with its scheduling status
   - **Cluster Staged Update Runs**: `pg-deploy-001` with stage-by-stage
     progression (primary → timed wait → replica)
   - **Resource Overrides**: `pg-role-config` and `backend-config` with their
     per-cluster JSON patches

This gives you a visual way to monitor the staged rollout and verify the fleet
state during the demo.

---

## Planned Region Migration

Migrate the primary from us-west to us-east in 4 steps:

### Step 1: Swap role labels

```bash
kubectl --context $HUB label membercluster kind-pg-us-west role=replica --overwrite
kubectl --context $HUB label membercluster kind-pg-us-east role=primary --overwrite
```

### Step 2: Update ResourceOverrides

Point `PRIMARY_HOST` to the new primary (us-east) and swap cluster display names:

```bash
US_EAST_IP=$(docker inspect pg-us-east-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')

# StatefulSet override: PRIMARY_HOST now points to us-east
kubectl --context $HUB apply -f - <<EOF
apiVersion: placement.kubernetes-fleet.io/v1
kind: ResourceOverride
metadata:
  name: pg-role-config
  namespace: kubefleet-pg
spec:
  placement:
    name: pg-app
  resourceSelectors:
    - group: apps
      kind: StatefulSet
      version: v1
      name: postgres
  policy:
    overrideRules:
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: primary
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "primary"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: replica
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "replica"
          - op: replace
            path: /spec/template/spec/containers/0/env/4/value
            value: "${US_EAST_IP}"
EOF

# Backend override: swap cluster display names
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ResourceOverride
metadata:
  name: backend-config
  namespace: kubefleet-pg
spec:
  placement:
    name: pg-app
  resourceSelectors:
    - group: apps
      kind: Deployment
      version: v1
      name: sample-backend
  policy:
    overrideRules:
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: primary
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "US-EAST (primary)"
          - op: replace
            path: /spec/template/spec/containers/0/env/6/value
            value: "false"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  role: replica
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "US-WEST (replica)"
          - op: replace
            path: /spec/template/spec/containers/0/env/6/value
            value: "true"
EOF
```

### Step 3: Delete PVCs

Role change requires PG reinitialization (primary→replica data formats differ):

```bash
for ctx in kind-pg-us-west kind-pg-us-east; do
  kubectl --context $ctx -n kubefleet-pg delete pod postgres-0 --force --grace-period=0
  kubectl --context $ctx -n kubefleet-pg delete pvc pgdata-postgres-0
done
```

### Step 4: Create migration rollout

```bash
# Stop old CSUR
kubectl --context $HUB patch csur pg-deploy-001 --type merge -p '{"spec":{"state":"Stop"}}'
kubectl --context $HUB delete csur pg-deploy-001

# The same strategy works unchanged — it selects by role label,
# so after relabeling, "primary" stage now targets us-east
kubectl --context $HUB apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: pg-migrate-east
spec:
  placementName: pg-app
  stagedRolloutStrategyName: pg-regional-strategy
  state: Run
EOF
```

The staged rollout deploys the new primary (us-east) first, waits 30s for it to
stabilize, then deploys the new replica (us-west) which runs `pg_basebackup`
from us-east and starts streaming WAL.

> **Note:** The seed data is reinitialized during migration (PVCs are deleted).
> In production, you would use logical replication or `pg_basebackup` from the
> old primary before the switch to preserve data.

---

## Issues Discovered

Real issues we hit while building this demo — useful for understanding KubeFleet
behavior with stateful workloads:

### 1. NodePort stripping during propagation

**Problem:** KubeFleet's resource snapshot omits the `nodePort` field from
Service specs. The hub had `nodePort: 30432`, but the member cluster received the
Service without it and allocated a random port (30883).

**Root cause:** The Kubernetes API server on the hub stores the Service response
without the allocated nodePort in the snapshot (it's considered a
cluster-specific allocated value).

**Fix:** Use `hostPort` on the container port spec instead of NodePort. The
container binds directly to the Kind node's IP on port 5432.

### 2. Empty string values omitted by K8s API

**Problem:** YAML `value: ""` on an env var is omitted during API serialization.
The stored object becomes `{"name": "PRIMARY_HOST"}` (no `value` key). A JSON
Patch `op: replace` on `/spec/template/spec/containers/0/env/4/value` then fails
with: `"doc is missing key: missing value"`.

**Fix:** Use a non-empty placeholder (`"none"`) for env vars that will be
overridden, or use `op: add` instead of `op: replace`.

### 3. External strategy skips snapshot creation

**Problem:** With `strategy.type: External`, the placement controller logs
`"Using external rollout strategy, skipping resource snapshot creation"`. If you
change resources on the hub, deleting the old snapshot does NOT trigger
recreation by the placement controller.

**Fix:** The CSUR controller creates snapshots itself. Create a new CSUR to get a
fresh snapshot with updated resources.

### 4. PG `initdb` doesn't set superuser password

**Problem:** The `initdb` command doesn't set a password for the `postgres`
superuser, but `pg_hba.conf` requires md5 authentication. Backend connections
fail with: `"password authentication failed for user postgres — User has no
password assigned"`.

**Fix:** After `initdb`, start PG temporarily and run
`ALTER ROLE postgres WITH PASSWORD '$POSTGRES_PASSWORD'` before the main
startup.

---

## Cleanup

Delete all Kind clusters:

```bash
kind delete cluster --name pg-hub
kind delete cluster --name pg-us-west
kind delete cluster --name pg-us-east
```

## File Structure

```
kubefleet-sample-app/
├── postgres/
│   ├── Dockerfile          # Custom PG 16 Alpine image
│   └── entrypoint.sh       # Primary/replica mode based on POSTGRES_ROLE
├── backend/
│   ├── Dockerfile.pg        # Python backend with psycopg2
│   └── main_pg.py          # FastAPI app with PG-backed CRUD + read-only mode
├── k8s/stateful/
│   ├── namespace.yaml       # kubefleet-pg namespace
│   ├── postgres.yaml        # StatefulSet + headless Service (hostPort: 5432)
│   ├── backend.yaml         # Deployment + Service
│   └── frontend.yaml        # Deployment + Service (reuses existing frontend)
└── STATEFUL-DEMO.md         # This file
```
