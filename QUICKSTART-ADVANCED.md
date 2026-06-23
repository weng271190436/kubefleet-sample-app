# KubeFleet Sample App — Advanced Staged Update Quickstart

This guide goes deeper than [QUICKSTART.md](QUICKSTART.md) and exercises the full
surface of the KubeFleet **staged update** feature. After completing it you will
have demonstrated:

| Feature | Where it appears |
| --- | --- |
| Multiple stages with **ordered** clusters via `sortingLabelKey` | step 5, canary stage |
| Mixed `maxConcurrency` — **percentage** and **absolute** | step 5 |
| `beforeStageTasks: Approval` + `afterStageTasks: TimedWait + Approval` | step 5 |
| `state: Initialize` → review the computed plan → flip to `Run` | step 7 |
| **Pause / Resume** mid-rollout via `state: Stop` / `state: Run` | step 9 |
| **Rollback** by pinning a previous `resourceSnapshotIndex` | step 12 |
| Browse versioned `ClusterResourceSnapshot`s | step 11 |
| `ResourceOverride` — different config per stage, snapshot-and-roll-back | step 13 |
| Namespace-scoped surface (`ResourcePlacement` + `StagedUpdateRun`) — second persona | step 14 |

The setup uses **4 member clusters** so ordering and concurrency are visible.

Steps 1–12 are one continuous demo on a single placement. Steps 13 and 14 each
exercise a distinct part of the staged-update feature surface — step 13 layers
overrides onto the existing placement; step 14 is a parallel walkthrough that
repeats the flow with the namespace-scoped CRDs.

## Prerequisites

Same as the basic quickstart:

- [Docker](https://docs.docker.com/desktop/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [helm](https://helm.sh/docs/intro/install/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [gh](https://cli.github.com/) (only if you want to push fresh images)

You should already have built and pushed `backend` / `frontend` images at least
once. This guide reuses the same images and only changes deployment data
(config values) to drive multiple rollouts and a rollback.

## 1. Create Kind clusters (1 hub + 4 members)

```bash
kind create cluster --name kf-hub-01
kind create cluster --name kf-member-01
kind create cluster --name kf-member-02
kind create cluster --name kf-member-03
kind create cluster --name kf-member-04
```

## 2. Deploy KubeFleet hub agent

```bash
kubectl config use-context kind-kf-hub-01

helm upgrade --install hub-agent oci://ghcr.io/kubefleet-dev/kubefleet/charts/hub-agent \
    --version 0.3.1 \
    --namespace fleet-system \
    --create-namespace \
    --set logFileMaxSize=100000
```

## 3. Join all 4 member clusters

```bash
HUB_IP=$(docker inspect kf-hub-01-control-plane \
  --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
echo "$HUB_IP"

cd ~/kubefleet/hack/quickstart
for i in 01 02 03 04; do
  ./join-member-clusters.sh 0.3.1 kind-kf-hub-01 \
    "https://${HUB_IP}:6443/" "kind-kf-member-${i}"
done
```

Verify all four members joined:

```bash
kubectl --context kind-kf-hub-01 get memberclusters
```

All four should show `JOINED: True`.

## 4. Label member clusters for staged rollout

We split the fleet into **three stages**:

| Cluster | environment | order |
| --- | --- | --- |
| `kind-kf-member-01` | `staging` | — |
| `kind-kf-member-02` | `canary` | `2` |
| `kind-kf-member-03` | `canary` | `1` |
| `kind-kf-member-04` | `prod` | — |

```bash
HUB=kind-kf-hub-01

kubectl --context $HUB label membercluster kind-kf-member-01 environment=staging --overwrite
kubectl --context $HUB label membercluster kind-kf-member-02 environment=canary  order=2 --overwrite
kubectl --context $HUB label membercluster kind-kf-member-03 environment=canary  order=1 --overwrite
kubectl --context $HUB label membercluster kind-kf-member-04 environment=prod    --overwrite
```

Note: `kf-member-03` is intentionally labeled `order=1` so the **sortingLabelKey**
in the canary stage updates member-03 *before* member-02 even though member-02
was joined first.

## 5. Start Headlamp with the KubeFleet plugin

Same as the basic quickstart:

```bash
cd ~/kubefleet-headlamp-plugin
npm install
npm start                     # keep running in this terminal
```

In another terminal:

```bash
# If something else is already on 8090 (Tilt, Spring Boot, etc.), pick another
# free port and update --port and the URL below.
docker rm -f headlamp 2>/dev/null
docker run -d --name headlamp \
  --network=host \
  -u $(id -u):$(id -g) \
  -v ~/.kube:/home/headlamp/.kube:ro \
  -v ~/.config/Headlamp/plugins:/headlamp/plugins:ro \
  ghcr.io/headlamp-k8s/headlamp:v0.41.0 \
  -kubeconfig /home/headlamp/.kube/config -port 8090
```

> **Port-in-use?** `--network=host` binds directly to the host. Common
> offenders on 8080/8090: `tilt`, Spring Boot, other dev servers. Check with
> `ss -ltnp 'sport = :8090'`. Also remember to `docker rm -f headlamp` before
> re-running — otherwise `docker run` fails with exit 125 (name conflict).

Open `http://localhost:8090` → **KubeFleet Manager → Configure Plugin** and pick
`kind-kf-hub-01` as the hub cluster.

In **Member Clusters** you should now see four entries with the labels you just
applied.

## 6. Deploy the sample app on the hub

```bash
HUB=kind-kf-hub-01

kubectl --context $HUB apply -f k8s/namespace.yaml
kubectl --context $HUB -n kubefleet-sample apply -f k8s/backend.yaml
kubectl --context $HUB -n kubefleet-sample apply -f k8s/frontend.yaml
```

(If you have a custom image tag, run the `kubectl set image ...` commands from
the basic quickstart now.)

## 7. Create the placement (External strategy)

In Headlamp → **Resource Placements → + CREATE** (or `kubectl apply`):

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterResourcePlacement
metadata:
  name: sample-crp
spec:
  resourceSelectors:
    - group: ""
      kind: Namespace
      version: v1
      name: kubefleet-sample
  policy:
    placementType: PickAll
  strategy:
    type: External       # required — rollouts driven by StagedUpdateRun
```

> **Important:** Because the strategy is `External`, no resource snapshot exists
> yet. The first UpdateRun will create one on initialization.

## 8. Create an advanced ClusterStagedUpdateStrategy

In Headlamp → **Staged Rollout Strategies → + CREATE**:

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateStrategy
metadata:
  name: sample-advanced-strategy
spec:
  stages:
    # Stage 1 — staging. Single cluster. 30s soak before promoting.
    - name: staging
      labelSelector:
        matchLabels:
          environment: staging
      maxConcurrency: 100%        # one cluster, but expressed as a percentage
      afterStageTasks:
        - type: TimedWait
          waitTime: 30s

    # Stage 2 — canary. Ordered rollout, manual gate at entry,
    # both approval AND soak at exit.
    - name: canary
      labelSelector:
        matchLabels:
          environment: canary
      sortingLabelKey: order      # member-03 (order=1) updates before member-02 (order=2)
      maxConcurrency: 1           # absolute number — sequential within stage
      beforeStageTasks:
        - type: Approval
      afterStageTasks:
        - type: TimedWait
          waitTime: 1m
        - type: Approval

    # Stage 3 — prod. Pure manual gate on entry.
    - name: prod
      labelSelector:
        matchLabels:
          environment: prod
      maxConcurrency: 50%         # 1 of 1 cluster → still 1, but shows percentage usage
      beforeStageTasks:
        - type: Approval
```

Open the strategy detail page — you should see each stage rendered with its
label-selector chips, `maxConcurrency`, `sortingLabelKey`, and the before/after
task lists.

## 9. Create the UpdateRun in `Initialize` state (preview the plan)

The plugin's **+ CREATE** form in **Staged Rollout Runs** checks "Start
immediately" by default. **Uncheck it** so the run starts in `Initialize`. Or
apply directly:

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: sample-run-001
spec:
  placementName: sample-crp
  stagedRolloutStrategyName: sample-advanced-strategy
  state: Initialize
```

The `Initialize` state **computes the rollout plan without executing it**.
Inspect what was computed:

```bash
kubectl --context kind-kf-hub-01 get csur sample-run-001 -o yaml | less
```

Key fields to look at in `status`:

- `resourceSnapshotIndexUsed: "0"` — a snapshot was just **created** during init
- `policySnapshotIndexUsed`
- `stagedUpdateStrategySnapshot` — frozen copy of the strategy at run start
- `stagesStatus[].clusters` — the **ordered list** of clusters per stage; in the
  canary stage you should see `member-03` listed *before* `member-02`

Once you're satisfied, flip the run into `Run`:

```bash
kubectl --context kind-kf-hub-01 patch csur sample-run-001 \
  --type='merge' -p '{"spec":{"state":"Run"}}'
```

Or, from Headlamp's **Staged Rollout Runs** details page, use the action menu →
**Resume**.

## 10. Drive the rollout through all three stages — with live visual proof

Before patching the run to `Run`, line up the tools you'll use to watch each
stage land on a member cluster:

- **Terminal A** — `watch kubectl --context kind-kf-hub-01 get csur sample-run-001`
  to see stage/cluster progress.
- **Headlamp tab** — **KubeFleet Manager → Staged Rollout Runs →
  `sample-run-001`** → **Stage Status** table updates live.
- **Four spare terminals** for port-forwards (we'll start them one at a time as
  each member receives the workload).
- **Browser** ready to open `http://localhost:8081` … `8084` as the
  port-forwards come up.

Right now none of the members has the `kubefleet-sample` namespace —
port-forwards will fail. That's part of the demo: each stage of the run
**creates** the workload on its target cluster.

Flip the run from `Initialize` into `Run`:

```bash
kubectl --context kind-kf-hub-01 patch csur sample-run-001 \
  --type='merge' -p '{"spec":{"state":"Run"}}'
```

Or, from Headlamp's **Staged Rollout Runs** details page, use the action menu →
**Resume**.

### 10a. Staging stage lands on member-01

The staging stage starts immediately. The Stage Status row for `staging` flips
from `Started` → `Succeeded`, then the after-stage `TimedWait: 30s` begins.
Total: ~30 s rollout + 30 s soak.

When the staging row reads `Succeeded`, in a new terminal:

```bash
kubectl --context kind-kf-member-01 port-forward -n kubefleet-sample \
  svc/sample-frontend 8081:80 --address 0.0.0.0
```

Open `http://localhost:8081`. The page loads with chip
**`Serving from: unknown`** — that's the hub-side baseline. The other three
members still don't have the namespace.

### 10b. Pause and resume mid-rollout (optional)

While the canary stage is updating (next sub-step), demonstrate pause/resume:

```bash
kubectl --context kind-kf-hub-01 patch csur sample-run-001 \
  --type='merge' -p '{"spec":{"state":"Stop"}}'
# wait for status to flip to Stopped (any in-flight cluster reaches a terminal state first)

kubectl --context kind-kf-hub-01 get csur sample-run-001
# Resume
kubectl --context kind-kf-hub-01 patch csur sample-run-001 \
  --type='merge' -p '{"spec":{"state":"Run"}}'
```

### 10c. Approve before-canary, watch ordered rollout land

The canary stage waits on its `Approval` before-task. In Headlamp →
**Pending Approvals**, approve `sample-run-001-before-canary`.

Equivalent kubectl:

```bash
NOW=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
kubectl --context kind-kf-hub-01 patch clusterapprovalrequest \
  sample-run-001-before-canary --type='merge' --subresource=status \
  -p "{\"status\":{\"conditions\":[{\"type\":\"Approved\",\"status\":\"True\",\"reason\":\"approved\",\"message\":\"approved\",\"lastTransitionTime\":\"$NOW\",\"observedGeneration\":1}]}}"
```

Because of `sortingLabelKey: order` and `maxConcurrency: 1`,
**member-03 (`order=1`) updates first**, then member-02. Watch the Stage
Status table — only member-03 should be `Updating` at first; member-02 stays
pending.

When member-03 turns `Succeeded`, in a new terminal:

```bash
kubectl --context kind-kf-member-03 port-forward -n kubefleet-sample \
  svc/sample-frontend 8083:80 --address 0.0.0.0
```

Open `http://localhost:8083` — chip **`Serving from: unknown`**.
Tab `:8082` still refuses connections — proof that the stage is serialised,
not parallel.

When member-02 turns `Succeeded`, in another terminal:

```bash
kubectl --context kind-kf-member-02 port-forward -n kubefleet-sample \
  svc/sample-frontend 8082:80 --address 0.0.0.0
```

Open `http://localhost:8082` — chip **`Serving from: unknown`**. Three members
are now serving.

### 10d. Approve after-canary (after the 1 min soak)

The canary after-stage has **two** tasks — `TimedWait: 1m` and `Approval`.
Both must complete. After the wait elapses, approve
`sample-run-001-after-canary` the same way as before.

### 10e. Approve before-prod, prod stage lands on member-04

Approve `sample-run-001-before-prod` when it appears. The prod stage updates
member-04. When member-04 turns `Succeeded`, in the last terminal:

```bash
kubectl --context kind-kf-member-04 port-forward -n kubefleet-sample \
  svc/sample-frontend 8084:80 --address 0.0.0.0
```

Open `http://localhost:8084` — chip **`Serving from: unknown`**. All four tabs
now show the app — every member received the workload in the strict order
defined by the strategy.

```bash
kubectl --context kind-kf-hub-01 get csur sample-run-001
# NAME            ... INITIALIZED  PROGRESSING  SUCCEEDED
# sample-run-001  ... True         False        True
```

The chip values are all `unknown` because there are no overrides yet — step 13
will make them flip per stage. Keep all four port-forward terminals running.

## 11. Inspect the auto-created resource snapshot

```bash
kubectl --context kind-kf-hub-01 get clusterresourcesnapshots \
  -l kubernetes-fleet.io/parent-CRP=sample-crp --show-labels
```

You should see one snapshot, e.g. `sample-crp-0-snapshot`, with label
`kubernetes-fleet.io/is-latest-snapshot=true` and
`kubernetes-fleet.io/resource-index=0`.

In Headlamp → **Staged Resources** click into the snapshot to see exactly which
resources were captured. Look at the `sample-backend` Deployment under
`spec.selectedResources` — you should see `replicas: 1` and
`env: [{name: CLUSTER_NAME, value: unknown}]`. **This is the hub-side
baseline** — every member starts from this; per-cluster differences come from
overrides (step 13) or rolling forward to a later snapshot (step 12).

### 11a. Inspect what's actually on each member (Headlamp)

If you'd rather check the manifests than the running app, the KubeFleet
plugin reads from the **hub** — it shows placements, snapshots, runs and
approvals. To see the **applied Deployment on a member cluster**, use
Headlamp's built-in cluster switcher:

1. Top-left cluster dropdown → switch to `kind-kf-member-01`.
2. **Workloads → Deployments** → namespace `kubefleet-sample` → `sample-backend`.
3. Open the **Pod template → Containers** section and confirm
   `env: CLUSTER_NAME = unknown` and `replicas: 1`.
4. Repeat for `kind-kf-member-02`, `kind-kf-member-03`, `kind-kf-member-04`.
   Right now all four show the same values — there are no overrides yet, and
   only one snapshot exists.

Remember to switch the dropdown **back to `kind-kf-hub-01`** before using the
**KubeFleet Manager** pages, otherwise they will be empty (the fleet CRDs
only live on the hub).

## 12. Roll out a new version, then roll back

### 12a. Make a hub-side change that creates a new snapshot

A new snapshot is only created when the **hash of the selected resources
changes**. Fleet normalizes certain metadata (e.g. it strips
`metadata.annotations` from Deployments) before hashing, so cosmetic metadata
patches typically **won't** produce a new snapshot — UpdateRuns will quietly
reuse `sample-crp-0-snapshot`.

Use a real spec change. Scaling the backend is the simplest:

```bash
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  scale deployment sample-backend --replicas=2
```

Create the second UpdateRun, this time starting immediately:

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: sample-run-002
spec:
  placementName: sample-crp
  stagedRolloutStrategyName: sample-advanced-strategy
  state: Run
```

After init, confirm the new snapshot was created and the run picked it up:

```bash
kubectl --context kind-kf-hub-01 get csur sample-run-002 \
  -o jsonpath='{.status.resourceSnapshotIndexUsed}'; echo
# 1
```

> **If `resourceSnapshotIndexUsed` is still `0`**, fleet didn't see your hub
> change. Verify with
> `kubectl --context kind-kf-hub-01 -n kubefleet-sample get deploy sample-backend -o jsonpath='{.spec.replicas}'; echo`
> and pick a change to a field that's definitely captured (replicas, env value,
> image, resource requests).

Walk it through the same approvals as before. Once finished, you should see two
snapshots:

```bash
kubectl --context kind-kf-hub-01 get clusterresourcesnapshots \
  -l kubernetes-fleet.io/parent-CRP=sample-crp --show-labels
# sample-crp-0-snapshot   ... resource-index=0  is-latest-snapshot=false
# sample-crp-1-snapshot   ... resource-index=1  is-latest-snapshot=true
```

### 12b. Roll back by pinning the old snapshot index

The plugin's create form does **not** expose `resourceSnapshotIndex`, so apply
this via YAML (Headlamp's generic editor or kubectl):

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: sample-rollback-to-v1
spec:
  placementName: sample-crp
  resourceSnapshotIndex: "0"                    # ← pin previous version
  stagedRolloutStrategyName: sample-advanced-strategy
  state: Run
```

Approve each gate as before. When it completes, all 4 member clusters are back
on the resources captured in `sample-crp-0-snapshot`. Verify on any member —
the backend should be back to **1 replica**:

```bash
kubectl --context kind-kf-member-04 -n kubefleet-sample \
  get deploy sample-backend -o jsonpath='{.spec.replicas}'; echo
# 1
```

### 12c. See the rollback diff in Headlamp

To make the version flip visible in the UI:

1. **Hub** (`kind-kf-hub-01`) → **KubeFleet Manager → Staged Resources** →
   click `sample-crp-1-snapshot` → confirm `replicas: 2`. Then click
   `sample-crp-0-snapshot` → confirm `replicas: 1`. Side-by-side these are the
   two versions you rolled between.
2. **Per-member** (e.g. `kind-kf-member-04`) → Workloads → Deployments →
   `kubefleet-sample` / `sample-backend`. After the rollback finishes the live
   spec shows `replicas: 1` again, matching snapshot 0.
3. **Running app** (the tabs from step 11a) → reload each of
   `http://localhost:8081-8084`. While the run is on snapshot 1 you should see
   two backend pods responding (round-robin) on the loaded clusters; after the
   rollback the pod count drops back to 1 per cluster.

## 13. Per-stage configuration via ResourceOverride

Staged updates and overrides are designed to work together: when an UpdateRun
initializes, it captures **override snapshots** alongside the resource snapshot.
That means overrides participate in rollback (step 12) and roll out through the
same stages and gates.

We'll give each stage a different `CLUSTER_NAME` value. The sample backend
already reads `CLUSTER_NAME` from its env, so no app changes are needed.

> **Why `ResourceOverride`, not `ClusterResourceOverride`?**
>
> `ClusterResourceOverride` selectors only accept cluster-scoped resources or a
> whole namespace (`selectionScope: NamespaceWithResources`). If you select the
> namespace, the JSON patches are applied to **every** resource in it. With
> `op: replace` on `/spec/template/spec/containers/0/env/0/value`, the patch
> fails for the Namespace and the two Services (they don't have that path),
> which blocks the rollout with
> `OverriddenFailed: doc is missing path: …`.
>
> `ResourceOverride` is namespaced and lets us target **only the Deployment**,
> so the patch path always exists.

```yaml
apiVersion: placement.kubernetes-fleet.io/v1alpha1
kind: ResourceOverride
metadata:
  name: backend-cluster-name
  namespace: kubefleet-sample
spec:
  placement:
    name: sample-crp
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
                  environment: staging
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "STAGING"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  environment: canary
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "CANARY"
      - clusterSelector:
          clusterSelectorTerms:
            - labelSelector:
                matchLabels:
                  environment: prod
        jsonPatchOverrides:
          - op: replace
            path: /spec/template/spec/containers/0/env/0/value
            value: "PROD"
```

Trigger another UpdateRun (`sample-run-003`, `state: Run`) and walk it through
gates. If a stage gets stuck on **`StageUpdatingStarted`** without progress,
inspect the binding for that cluster:

```bash
kubectl --context kind-kf-hub-01 get clusterresourcebinding \
  -l kubernetes-fleet.io/parent-CRP=sample-crp,kubernetes-fleet.io/cluster-name=<member-name> \
  -o jsonpath='{range .items[*].status.conditions[?(@.type=="Overridden")]}{.status} {.reason} {.message}{"\n"}{end}'
```

`Overridden=False` with reason `OverriddenFailed` means the JSON patch couldn't
apply — fix the override path/scope before the rollout can proceed.

### 13a. Live walkthrough — flip each chip as its stage lands

The most compelling demo is to keep the four browser tabs from step 11a open
and reload each one *as its stage finishes*. Before starting, every tab should
read `Serving from: unknown`.

Open a watch window on the run in one terminal:

```bash
watch kubectl --context kind-kf-hub-01 get csur sample-run-003
```

And keep the four port-forward terminals running so the tabs can be reloaded
on demand. Also keep Headlamp open on the hub at
**KubeFleet Manager → Staged Rollout Runs → `sample-run-003`** — the
**Stage Status** table updates live and shows which cluster is "updating"
right now.

Now drive the rollout:

1. **Apply the override + create the run** (from above). Watch the run move
   from `Initialized=True` to `Progressing=True`. The staging stage begins
   immediately.
2. **Staging completes** (~30s rollout + 30s `TimedWait`).
   → Reload `http://localhost:8081`. Chip: **`STAGING`**.
   The other three tabs still show `unknown`.
3. **Approve `sample-run-003-before-canary`** in
   **Pending Approvals** (or via the kubectl patch from step 10b).
   The canary stage starts. Because `sortingLabelKey: order` and
   `maxConcurrency: 1`, member-03 (`order=1`) updates first, then member-02.
4. **member-03 succeeds** (the Stage Status table shows it as completed
   while member-02 is still pending).
   → Reload `http://localhost:8083`. Chip: **`CANARY`**.
   Tab :8082 still shows `unknown` for a few more seconds — proof that the
   stage is serialised, not parallel.
5. **member-02 succeeds**.
   → Reload `http://localhost:8082`. Chip: **`CANARY`**.
6. Canary after-stage tasks run — the **1 m `TimedWait`** elapses, then the
   `Approval` task creates `sample-run-003-after-canary`. **Approve it.**
7. **Approve `sample-run-003-before-prod`** when it appears. The prod stage
   updates member-04.
8. **member-04 succeeds.**
   → Reload `http://localhost:8084`. Chip: **`PROD`**.

End state — reload all four tabs once more:

| Tab | URL | Expected `Serving from:` chip |
| --- | --- | --- |
| member-01 | `http://localhost:8081` | `STAGING` |
| member-02 | `http://localhost:8082` | `CANARY` |
| member-03 | `http://localhost:8083` | `CANARY` |
| member-04 | `http://localhost:8084` | `PROD` |

If a chip still says `unknown` after the matching stage shows `Succeeded`,
the old pod is still terminating — wait a few seconds for the override-rolled
pod to come up, then refresh.

You can also confirm everything via kubectl:

```bash
for i in 01 02 03 04; do
  echo -n "kind-kf-member-$i: "
  kubectl --context kind-kf-member-$i -n kubefleet-sample \
    get deploy sample-backend -o jsonpath='{.spec.template.spec.containers[0].env[0].value}'
  echo
done
```

### 13b. Hub-side metadata in the KubeFleet Manager plugin

Once the run finishes, switch Headlamp's cluster dropdown to `kind-kf-hub-01`
and confirm the override machinery from the hub side:

- **Staged Resources** → latest snapshot → `CLUSTER_NAME = unknown` on the
  baseline. Overrides are *not* part of the resource snapshot — they live as
  separate override snapshots.
- **Resource Overrides** → `backend-cluster-name` → the details page lists
  the three per-environment JSON patches.
- **Staged Rollout Runs → `sample-run-003`** → Stage Status table; each
  cluster row lists its `clusterResourceOverrideSnapshots` (e.g.
  `backend-cluster-name-0`), proving the override was bound to the run.

Proof that overrides are versioned with the run: if you now create another
rollback run pinning `resourceSnapshotIndex: "0"`, the overrides revert along
with the resources — reload the four tabs again and every chip flips back to
`unknown`.

## 14. Namespace-scoped staged update (second persona)

KubeFleet exposes the same staged-update machinery at **two scopes**:

- **Cluster-scoped** (steps 1–13) — fleet admin, cluster-wide CRDs.
- **Namespace-scoped** (this step) — app team, namespaced CRDs, no
  cluster-admin required.

This is a parallel walkthrough that uses `ResourcePlacement` /
`StagedUpdateStrategy` / `StagedUpdateRun` / `ApprovalRequest` instead of the
`Cluster*` variants. The Headlamp plugin lists both side by side in the same
pages.

Set up a namespace and an app to place:

```bash
HUB=kind-kf-hub-01
kubectl --context $HUB create ns my-app-ns

# Propagate just the namespace (no contents) to every member.
kubectl --context $HUB apply -f - <<EOF
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterResourcePlacement
metadata:
  name: my-app-ns-only
spec:
  resourceSelectors:
    - group: ""
      kind: Namespace
      version: v1
      name: my-app-ns
      selectionScope: NamespaceOnly
  policy:
    placementType: PickAll
  strategy:
    type: RollingUpdate
EOF

# Create the workload inside the namespace on the hub.
kubectl --context $HUB -n my-app-ns create deploy web --image=nginx:1.25
kubectl --context $HUB -n my-app-ns expose deploy web --port=80
```

Now the namespace-scoped objects. **Apply via `kubectl` rather than
Headlamp's + CREATE form** — that form expects a single object per submission
and rejects multi-document YAML with
`metadata.resourceVersion: Invalid value: 0: must be specified for an update`.
If you prefer the UI, create the `ResourcePlacement`, `StagedUpdateStrategy`
and `StagedUpdateRun` as three separate creates.

> **Apply the Placement and Strategy first, then the Run.** Creating all three
> in a single `kubectl apply` can race the controller's informer cache: if the
> run is reconciled before the strategy is visible, it terminates with
> `Initialized=False` (`UpdateRunInitializedFailed: referenced updateStrategy
> not found`) and **will not retry**. Recovery is to `delete sur <name>` and
> recreate it.

Step 14a — placement + strategy:

```bash
kubectl --context kind-kf-hub-01 apply -f - <<'EOF'
---
apiVersion: placement.kubernetes-fleet.io/v1
kind: ResourcePlacement
metadata:
  name: web-placement
  namespace: my-app-ns
spec:
  resourceSelectors:
    - group: apps
      kind: Deployment
      version: v1
      name: web
    - group: ""
      kind: Service
      version: v1
      name: web
  policy:
    placementType: PickAll
  strategy:
    type: External
---
apiVersion: placement.kubernetes-fleet.io/v1
kind: StagedUpdateStrategy
metadata:
  name: web-strategy
  namespace: my-app-ns
spec:
  stages:
    - name: dev
      labelSelector:
        matchLabels:
          environment: staging
      maxConcurrency: 1
      afterStageTasks:
        - type: TimedWait
          waitTime: 30s
    - name: prodlike
      labelSelector:
        matchExpressions:
          - key: environment
            operator: In
            values: [canary, prod]
      maxConcurrency: 50%        # 2 of 3 clusters → rounds to 1
      beforeStageTasks:
        - type: Approval
EOF
```

Step 14b — once the strategy exists, create the run:

```bash
kubectl --context kind-kf-hub-01 apply -f - <<'EOF'
apiVersion: placement.kubernetes-fleet.io/v1
kind: StagedUpdateRun
metadata:
  name: web-rollout-001
  namespace: my-app-ns
spec:
  placementName: web-placement
  stagedRolloutStrategyName: web-strategy
  state: Run
EOF
```

Note the differences:

- All three objects live **inside** `my-app-ns`
- Approval is on `approvalrequests` (short name `areq`) instead of
  `clusterapprovalrequests`
- The plugin lists these alongside the cluster-scoped ones in the same pages
- Stage names must match `^[a-z0-9]+$` — no hyphens or uppercase letters

Approve `web-rollout-001-before-prodlike` from **Pending Approvals** when
prompted.

## 15. Clean up

```bash
HUB=kind-kf-hub-01
kubectl --context $HUB delete csur --all
kubectl --context $HUB delete clusterapprovalrequest --all
kubectl --context $HUB delete clusterstagedupdatestrategy sample-advanced-strategy
kubectl --context $HUB -n kubefleet-sample delete resourceoverride backend-cluster-name --ignore-not-found
kubectl --context $HUB delete crp sample-crp my-app-ns-only --ignore-not-found
kubectl --context $HUB delete ns kubefleet-sample my-app-ns --ignore-not-found

docker stop headlamp && docker rm headlamp
for i in 01 02 03 04; do kind delete cluster --name kf-member-$i; done
kind delete cluster --name kf-hub-01
```

## What you exercised

By the end of this guide you have:

1. Built a 3-stage strategy with **per-stage gates** that mix `Approval` and
   `TimedWait`, before *and* after.
2. Demonstrated **ordered** intra-stage rollout via `sortingLabelKey`.
3. Demonstrated both **percentage** and **absolute** `maxConcurrency`.
4. Used **`Initialize`** to preview a computed plan before triggering it.
5. **Paused and resumed** a rollout mid-flight using `Stop` / `Run`.
6. Driven multiple rollouts and inspected the **immutable snapshots** that the
   system creates.
7. Performed a **rollback** by pinning a previous `resourceSnapshotIndex`.
8. Applied **`ResourceOverride`** to ship different config per stage and
   confirmed that override snapshots are captured per run and revert on
   rollback.
9. Mirrored the flow with the **namespace-scoped** CRDs to exercise the
   second persona (app team, no cluster-admin).
