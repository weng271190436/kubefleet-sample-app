# KubeFleet Sample App - Annotation-Based Placement and Flux

A copy/paste walkthrough for the [November 4, 2026 community call](https://github.com/kubefleet-dev/community/issues/13),
in the style of [QUICKSTART-ADVANCED.md](QUICKSTART-ADVANCED.md).

**The story:** annotate an object on the hub to say *where* it belongs.
Then annotate Flux API objects so that Fleet delivers the GitOps instructions
and Flux deploys the application independently on each selected member.

| Demo beat | What the audience sees |
| --- | --- |
| Place a ConfigMap with one annotation | No hand-written placement YAML |
| Change the annotation | Expand from one region to the whole fleet |
| Inspect the generated PlacementPolicy | The annotation is a shortcut, not a second placement engine |
| Annotate a GitRepository and a Flux Kustomization | Fleet distributes the GitOps instructions, not the application manifests |
| Open the sample app on both members | Member-local Flux controllers deploy the application |
| Patch the hub-side Flux Kustomization | Fleet delivers the new instructions; Flux scales the backend |
| Remove annotations | Fleet withdraws the Flux objects; Flux prunes the application |

Allow **10-15 minutes live**, with sections 1-5 completed beforehand.
Use two members so placement changes remain easy to see.

**Live rehearsal passed (November 3):** built both Fleet agents and app
images; joined two Kind members; verified regional placement, expansion,
data updates, and invalid-annotation recovery; deployed the app through
annotated Flux objects; checked frontend HTTP and the proxied backend API on
both members; scaled both backends to `2/2`; and verified withdrawal/pruning.
The corrected live commands were replayed end-to-end without intervention.
Browser rendering was not verified: the integrated browser could not reach
the remote host's loopback ports, although host-side HTTP checks passed.
Node-identity injection was also verified on both members through the
frontend's proxied `/api/cluster-info` endpoint.
That rehearsal used the previous kubeconfig-based join. The standard-script
join below replaces it; shell syntax and Helm rendering have been checked,
but this revised bootstrap has not been live-retested.

> **Experimental feature:** this guide targets the Fleet source checkout used
> to prepare the demo (`137abce1`, November 3, 2026). It uses
> `placement.kubefleet.dev/v1alpha1`, not the older
> `placement.kubernetes-fleet.io` placement APIs. Build the hub/member
> images and use the matching local charts below. The published `0.3.2`
> member chart is used only to bootstrap the standard join script; the
> subsequent local chart upgrade enables this experimental experience.
>
> **Trusted local lab only.** Annotation-based placement is an opt-in
> capability with security implications. This demo assumes a single trusted
> operator. A shared/untrusted hub needs the documented workload-controller
> restrictions and protection admission policy described in the
> [hub feature flags](https://github.com/kubefleet-dev/kubefleet/blob/137abce1/cmd/hubagent/options/featureflags.go).
> `enableWorkload=false` does **not** disable Kubernetes workload controllers.

## Architecture

```text
Hub: Flux CRDs installed, but NO Flux controllers
  ConfigMap + annotation -----------------> generated PlacementPolicy
                                                |
                                                v
                                            member ConfigMap

  GitRepository + annotation -------------> generated PlacementPolicy
  Flux Kustomization + annotation --------> generated PlacementPolicy
                                                |
                                                v
Members: Fleet member agent + Flux controllers
  GitRepository --> Git artifact --> Flux Kustomization --> sample app
```

Fleet owns the placed Flux objects. Flux owns the application Deployments and
Services. **Do not also place the application's namespace or Deployments with
a CRP/RP/PlacementPolicy**: that would give two systems ownership of the same
application resources.

## Prerequisites

- Docker, Kind **v0.32.0 or newer**, Helm 3, and kubectl compatible with Kubernetes 1.35.
  Rehearsed with Kind v0.32.0, Helm v3.16.4, and kubectl v1.36.1.
- Local clones at `~/kubefleet` and `~/kubefleet-sample-app`.
- Internet access for the initial image builds, Flux installation, and public Git fetches.
- At least **20 GiB free disk** (25 GiB recommended), plus enough memory for
  three additional single-node Kind clusters and image builds. Compiler and
  npm build caches can consume several GiB beyond the final image sizes.
  Run `df -h /`, `free -h`, and `docker system df` first; do not prune unrelated
  Docker resources to make room.
- Bash. Run the main blocks in order in the **same terminal**; use separate
  terminals only where specified. No Flux CLI, Git push, or Git credentials
  are required.

These commands are intended for an interactive terminal and do not enable
strict mode, which can exit your shell on a failed command or unset variable.
If you already enabled it, run `set +e; set +u; set +o pipefail` first.
If a command fails, stop and fix the error before continuing.

Pinned versions:

| Component | Version |
| --- | --- |
| Kubernetes | Kind node `v1.35.8` |
| Fleet | Local source + local charts, same checkout for both agents |
| Join bootstrap / refresh-token sidecar | Published chart `0.3.2` / image `v0.3.2` |
| Flux | `v2.10.0` (source-controller and kustomize-controller `v1.10.1`) |
| Git application manifests | Commit `dcf642603e012a7f785cffd342857d9053e6dd5a` |

Flux v2.10.0 supports Kubernetes 1.35.2 and newer in the 1.35 series;
the node image below is published with Kind v0.33.0.

## 1. Create a dedicated hub and two members

These names intentionally differ from the staged-update demo and any existing fleet.

```bash
export FLEET_DIR="$HOME/kubefleet"
export APP_DIR="$HOME/kubefleet-sample-app"
export HUB=kind-annotation-hub
export EAST=kind-annotation-east
export WEST=kind-annotation-west
export DEMO_DIR
DEMO_DIR=$(mktemp -d "${TMPDIR:-/tmp}/kubefleet-annotation-demo.XXXXXX")
echo "Demo manifests: $DEMO_DIR"

NODE_IMAGE=kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0
for cluster in annotation-hub annotation-east annotation-west; do
  kind create cluster --name "$cluster" --image "$NODE_IMAGE" --wait 120s
done
```

## 2. Build and load the Fleet and application images

The local charts and binaries must include both experimental feature flags.
Using `image.pullPolicy=Never` later makes a missing image an explicit error
instead of accidentally pulling a different published build.

```bash
docker build -f "$FLEET_DIR/docker/hub-agent.Dockerfile" \
  -t kubefleet/hub-agent:annotation-demo "$FLEET_DIR"
docker build -f "$FLEET_DIR/docker/member-agent.Dockerfile" \
  -t kubefleet/member-agent:annotation-demo "$FLEET_DIR"

kind load docker-image kubefleet/hub-agent:annotation-demo --name annotation-hub
for cluster in annotation-east annotation-west; do
  kind load docker-image kubefleet/member-agent:annotation-demo --name "$cluster"
done

docker build -t kubefleet-sample-backend:annotation-demo "$APP_DIR/backend"
docker build -t kubefleet-sample-frontend:annotation-demo "$APP_DIR/frontend"
for cluster in annotation-east annotation-west; do
  kind load docker-image kubefleet-sample-backend:annotation-demo --name "$cluster"
  kind load docker-image kubefleet-sample-frontend:annotation-demo --name "$cluster"
done
```

No application images are loaded onto the hub. The app will not run there.

## 3. Install the hub agent with annotation placement enabled

```bash
helm upgrade --install hub-agent "$FLEET_DIR/charts/hub-agent" \
  --kube-context "$HUB" \
  --namespace fleet-system --create-namespace \
  --set image.repository=kubefleet/hub-agent \
  --set image.tag=annotation-demo \
  --set image.pullPolicy=Never \
  --set enablePlacementPolicyAPIs=true \
  --set enableAnnotationBasedPlacement=true \
  --set logFileMaxSize=100000 \
  --wait --timeout 5m

kubectl --context "$HUB" get crd \
  placementpolicies.placement.kubefleet.dev \
  clusterplacementpolicies.placement.kubefleet.dev
```

Both CRDs must exist. Do not continue if Helm fails or either CRD is missing.

## 4. Join the members using the standard quickstart script

Use the same `join-member-clusters.sh` as
[QUICKSTART-ADVANCED.md](QUICKSTART-ADVANCED.md). It creates the member
ServiceAccount, its long-lived `kubernetes.io/service-account-token` Secret,
the MemberCluster, and the member-agent installation.

The script installs a published member chart. Immediately upgrade that
release using the local chart/image and enable the experimental placement
APIs. Keep the standard Secret provider and published refresh-token sidecar;
there is no custom kubeconfig or TokenRequest-based join flow.

These Secret-backed tokens have no built-in expiry, unlike `kubectl create
token`. Keep them out of source control, slides, and logs; do not enable shell
tracing. The sidecar reads the Secret; its name does not imply token expiry.

```bash
set +x
JOIN_VERSION=0.3.2
HUB_IP=$(docker inspect annotation-hub-control-plane \
  --format='{{(index .NetworkSettings.Networks "kind").IPAddress}}')
test -n "$HUB_IP"
HUB_CA=$(kubectl config view --raw \
  -o "jsonpath={.clusters[?(@.name==\"$HUB\")].cluster.certificate-authority-data}")
test -n "$HUB_CA"

for MEMBER in "$EAST" "$WEST"; do
  "$FLEET_DIR/hack/quickstart/join-member-clusters.sh" \
    "$JOIN_VERSION" "$HUB" "https://${HUB_IP}:6443/" "$MEMBER"

  helm upgrade --install member-agent "$FLEET_DIR/charts/member-agent" \
    --kube-context "$MEMBER" \
    --namespace fleet-system \
    --reset-values \
    --set image.repository=kubefleet/member-agent \
    --set image.tag=annotation-demo \
    --set image.pullPolicy=Never \
    --set config.hubURL="https://${HUB_IP}:6443/" \
    --set config.hubCA="$HUB_CA" \
    --set config.memberClusterName="$MEMBER" \
    --set refreshtoken.tag="v${JOIN_VERSION}" \
    --set enablePlacementPolicyAPIs=true \
    --set logFileMaxSize=100000 \
    --wait --timeout 5m
done

kubectl --context "$HUB" wait --for=condition=Joined \
  memberclusters.cluster.kubernetes-fleet.io/"$EAST" \
  memberclusters.cluster.kubernetes-fleet.io/"$WEST" --timeout=5m

kubectl --context "$HUB" label membercluster "$EAST" \
  topology.kubernetes.io/region=eastus --overwrite
kubectl --context "$HUB" label membercluster "$WEST" \
  topology.kubernetes.io/region=westus --overwrite

kubectl --context "$HUB" get memberclusters \
  -L topology.kubernetes.io/region,kubefleet.dev/cluster-alias
```

Both members should be joined. `region` in the annotation is shorthand for
`topology.kubernetes.io/region`, a label on the hub-side **MemberCluster**,
not on the member's Nodes.

On fresh members, the helper's cleanup commands can report that the old
`hub-kubeconfig-secret` or Helm release does not exist. Only those initial
cleanup errors are expected; installation errors or a failed `Joined` wait
must be resolved before continuing.

## 5. Prepare Flux: CRDs on the hub, controllers on the members

Install the source and Kustomization CRDs on the hub **before creating the
Flux objects**. Do not install Flux controllers there: otherwise the hub
would also reconcile the application.

```bash
curl -fL --retry 3 \
  https://github.com/fluxcd/source-controller/releases/download/v1.10.1/source-controller.crds.yaml \
  -o "$DEMO_DIR/source-controller.crds.yaml"
curl -fL --retry 3 \
  https://github.com/fluxcd/kustomize-controller/releases/download/v1.10.1/kustomize-controller.crds.yaml \
  -o "$DEMO_DIR/kustomize-controller.crds.yaml"
curl -fL --retry 3 \
  https://github.com/fluxcd/flux2/releases/download/v2.10.0/install.yaml \
  -o "$DEMO_DIR/flux-install.yaml"

kubectl --context "$HUB" apply --server-side \
  -f "$DEMO_DIR/source-controller.crds.yaml" \
  -f "$DEMO_DIR/kustomize-controller.crds.yaml"
kubectl --context "$HUB" wait --for=condition=Established \
  crd/gitrepositories.source.toolkit.fluxcd.io \
  crd/kustomizations.kustomize.toolkit.fluxcd.io --timeout=60s

for MEMBER in "$EAST" "$WEST"; do
  kubectl --context "$MEMBER" apply --server-side -f "$DEMO_DIR/flux-install.yaml"
  kubectl --context "$MEMBER" -n flux-system rollout status \
    deployment/source-controller --timeout=5m
  kubectl --context "$MEMBER" -n flux-system rollout status \
    deployment/kustomize-controller --timeout=5m
done

for CTX in "$HUB" "$EAST" "$WEST"; do
  kubectl --context "$CTX" create namespace annotation-demo
done
```

The published Flux install includes additional controllers; only
source-controller and kustomize-controller participate in this demo.
Fleet can discover custom resources added after the hub agent starts.

**Preflight before going live:** the three Fleet agents and both member
source/kustomize controllers must be ready. The remaining sections are the
live demo.

## 6. One annotation places a ConfigMap in one region

Create an ordinary hub-side object, without a placement.

```bash
kubectl --context "$HUB" -n annotation-demo create configmap call-message \
  --from-literal=message="Hello from the community call"

kubectl --context "$HUB" -n annotation-demo annotate configmap call-message \
  'kubefleet.dev/cluster-selectors=region=eastus,count=All'

kubectl --context "$EAST" -n annotation-demo wait \
  --for=create configmap/call-message --timeout=3m
kubectl --context "$EAST" -n annotation-demo get configmap call-message -o yaml

kubectl --context "$WEST" -n annotation-demo get configmap call-message \
  --ignore-not-found -o name
```

**Expected:** east has the ConfigMap; west prints nothing.

Show what Fleet generated:

```bash
kubectl --context "$HUB" -n annotation-demo get \
  placementpolicies.placement.kubefleet.dev \
  -l placement.kubefleet.dev/parent-kind=ConfigMap,placement.kubefleet.dev/parent-name=call-message \
  -o yaml
```

Point out:

- `apiVersion: placement.kubefleet.dev/v1alpha1`.
- `spec.resourceSelectors` selects this ConfigMap only.
- `spec.clusterSelectors` expands `region` into the full label key.
- The policy has provenance labels and an owner reference to the ConfigMap.
- The policy name is generated; discover it by labels rather than hard-coding it.

> **Say:** "I express placement on the object I already own. Fleet translates
> that into a normal placement policy and runs the normal reconciliation path."

Namespaced objects generate a **PlacementPolicy in their namespace**;
cluster-scoped objects generate a **ClusterPlacementPolicy**.
The annotation does not recursively select referenced objects or everything
inside an annotated Namespace.

## 7. Expand to the whole fleet and update the data

**Placement and resource rollout are separate in this alpha.** Changing the
cluster selector adds members using the current snapshot, but changing the
ConfigMap's data does not automatically roll out a new snapshot by default.
For this trusted lab, opt the generated policy into auto-rollout before
patching the data. The controller preserves these operator-added policy
annotations; its generated **spec** remains controller-managed.

```bash
kubectl --context "$HUB" -n annotation-demo annotate configmap call-message \
  'kubefleet.dev/cluster-selectors=count=All' --overwrite

kubectl --context "$WEST" -n annotation-demo wait \
  --for=create configmap/call-message --timeout=3m

kubectl --context "$HUB" -n annotation-demo annotate \
  placementpolicies.placement.kubefleet.dev \
  -l placement.kubefleet.dev/parent-kind=ConfigMap,placement.kubefleet.dev/parent-name=call-message \
  kubefleet.dev/auto-rollout-enabled=true \
  kubefleet.dev/auto-rollout-minimum-wait-period=1m --overwrite

kubectl --context "$HUB" -n annotation-demo patch configmap call-message \
  --type merge -p '{"data":{"message":"One annotation, both clusters"}}'

for MEMBER in "$EAST" "$WEST"; do
  kubectl --context "$MEMBER" -n annotation-demo wait \
    --for=jsonpath='{.data.message}'='One annotation, both clusters' \
    configmap/call-message --timeout=3m
  kubectl --context "$MEMBER" -n annotation-demo get configmap call-message \
    -o jsonpath='{.data.message}{"\n"}'
done
```

**Expected:** both print `One annotation, both clusters`.

Auto-rollout has no staged gates or baking period; it is appropriate here
for lab configuration. Use staged update runs for controlled production
changes. The minimum interval applies between auto-rollout attempts, not
between annotation-based placement decisions.

Annotation grammar cheat sheet:

| Value | Meaning |
| --- | --- |
| `region=eastus` | One matching cluster; omitted `count` defaults to **1** |
| `region=eastus,count=All` | Every eastus cluster |
| `count=All` | Every eligible member cluster |
| `region=eastus,count=1;region=westus,count=1` | One from each region |
| `alias=kind-annotation-east` | Select using the cluster-alias label |

Commas combine matchers within one selector; semicolons separate selectors.
Always quote the annotation argument, especially when it contains semicolons.
`All` is case-sensitive. Numeric counts are 1-999. An unsatisfied selector
can create a **ClusterClaim**; it does not automatically provision a Kind
cluster. Use labels/counts that the pre-created members can satisfy.

### Optional 30-second failure/recovery beat

```bash
kubectl --context "$HUB" -n annotation-demo annotate configmap call-message \
  'kubefleet.dev/cluster-selectors=count=all' --overwrite

kubectl --context "$HUB" -n annotation-demo get events \
  --field-selector involvedObject.kind=ConfigMap,involvedObject.name=call-message,reason=InvalidClusterSelectorsAnnotation \
  --watch
```

Once the warning appears, press **Ctrl-C**, then restore the valid annotation:

```bash
kubectl --context "$HUB" -n annotation-demo annotate configmap call-message \
  'kubefleet.dev/cluster-selectors=count=All' --overwrite
```

**Expected:** the warning explains the invalid spelling. An invalid edit
leaves the last valid generated policy in place; it does not withdraw a
running placement. Removing the annotation is different: that deletes the
generated policy.

## 8. Create annotated Flux API objects on the hub

We reuse this repository's backend/frontend manifests at a fixed public
commit, so no publish step is required during the call.

The GitRepository excludes the unrelated Cilium and stateful subdirectories.
Flux will generate a Kustomize build for the three remaining YAML files in
`./k8s`. The Flux Kustomization redirects the published image names to the
local images loaded in section 2. Its backend patch replaces the default
`CLUSTER_NAME=unknown` with the Pod's node name through the Kubernetes
Downward API. Each single-node Kind member therefore identifies itself
without a separate manifest per cluster.

```bash
cat > "$DEMO_DIR/flux-app.yaml" <<'EOF'
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: sample-app
  namespace: annotation-demo
  annotations:
    kubefleet.dev/cluster-selectors: "count=All"
spec:
  interval: 1m
  url: https://github.com/weng271190436/kubefleet-sample-app
  ref:
    commit: dcf642603e012a7f785cffd342857d9053e6dd5a
  ignore: |
    /k8s/cilium-demo/
    /k8s/stateful/
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: sample-app
  namespace: annotation-demo
  annotations:
    kubefleet.dev/cluster-selectors: "count=All"
spec:
  interval: 1m
  retryInterval: 15s
  timeout: 3m
  sourceRef:
    kind: GitRepository
    name: sample-app
  path: ./k8s
  targetNamespace: kubefleet-sample
  prune: true
  deletionPolicy: WaitForTermination
  wait: true
  images:
    - name: ghcr.io/weng271190436/kubefleet-sample-app/backend
      newName: kubefleet-sample-backend
      newTag: annotation-demo
    - name: ghcr.io/weng271190436/kubefleet-sample-app/frontend
      newName: kubefleet-sample-frontend
      newTag: annotation-demo
  patches:
    - target:
        kind: Deployment
        name: sample-backend
      patch: |
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: sample-backend
        spec:
          template:
            spec:
              containers:
                - name: backend
                  env:
                    - name: CLUSTER_NAME
                      value: null
                      valueFrom:
                        fieldRef:
                          apiVersion: v1
                          fieldPath: spec.nodeName
EOF

kubectl --context "$HUB" apply -f "$DEMO_DIR/flux-app.yaml"

kubectl --context "$HUB" -n annotation-demo get \
  placementpolicies.placement.kubefleet.dev \
  -l placement.kubefleet.dev/parent-name=sample-app -o wide
```

**Expected eventually:** two generated PlacementPolicies, one for each Flux
object. Both need the annotation: annotating the Kustomization does **not**
automatically place the GitRepository named by `sourceRef`.

For private repositories, referenced credentials must also be made available
on each member; this public-repository demo deliberately needs no Git Secret.
Flux CRDs/controllers are platform prerequisites, not dependencies Fleet
installs by following the annotation.

The policies are independent: the Kustomization can arrive before its source.
A transient `SourceNotFound` while they converge is normal; Flux retries.
Do not treat Fleet's policy availability as proof that the app is healthy:
check Flux `Ready` and the actual member Deployments next.

## 9. Verify Flux reconciliation on both members

```bash
for MEMBER in "$EAST" "$WEST"; do
  echo "=== $MEMBER ==="
  kubectl --context "$MEMBER" -n annotation-demo wait --for=create \
    gitrepositories.source.toolkit.fluxcd.io/sample-app --timeout=3m
  kubectl --context "$MEMBER" -n annotation-demo wait --for=create \
    kustomizations.kustomize.toolkit.fluxcd.io/sample-app --timeout=3m
  kubectl --context "$MEMBER" -n annotation-demo wait --for=condition=Ready \
    gitrepositories.source.toolkit.fluxcd.io/sample-app --timeout=5m
  kubectl --context "$MEMBER" -n annotation-demo wait --for=condition=Ready \
    kustomizations.kustomize.toolkit.fluxcd.io/sample-app --timeout=5m
  kubectl --context "$MEMBER" -n kubefleet-sample rollout status \
    deployment/sample-backend --timeout=3m
  kubectl --context "$MEMBER" -n kubefleet-sample rollout status \
    deployment/sample-frontend --timeout=3m
  kubectl --context "$MEMBER" -n kubefleet-sample get deployments,services
done
```

Show the revision and the resources Flux owns:

```bash
kubectl --context "$EAST" -n annotation-demo get \
  gitrepositories.source.toolkit.fluxcd.io/sample-app \
  -o jsonpath='{.status.artifact.revision}{"\n"}'
kubectl --context "$EAST" -n annotation-demo get \
  kustomizations.kustomize.toolkit.fluxcd.io/sample-app \
  -o jsonpath='{.status.inventory.entries}{"\n"}'

kubectl --context "$HUB" get deployments -A \
  -l app=sample-backend
```

**Expected:** the source revision contains the pinned commit, Flux's inventory
contains the app resources, and the hub has **no sample-backend Deployment**.
Hub-side Flux objects have no Flux `Ready` status because no Flux controller
runs there; that is intentional.

### Open both frontends

In two separate terminals:

```bash
kubectl --context kind-annotation-east -n kubefleet-sample \
  port-forward service/sample-frontend 8081:80
```

```bash
kubectl --context kind-annotation-west -n kubefleet-sample \
  port-forward service/sample-frontend 8082:80
```

Open <http://localhost:8081> and <http://localhost:8082>.
If either port is in use, choose another local port.

Both should display the sample app, with distinct identities:

- East: **Serving from: annotation-east-control-plane**
- West: **Serving from: annotation-west-control-plane**

These are node names, not Fleet MemberCluster names; they identify the
members here because each Kind cluster has one node. The backends are
independent, with in-memory data; UI edits are not replicated between
clusters. Refresh the browser after a backend rollout to reload the identity.

> **Say:** "Fleet decides where the GitOps objects go. Each member's Flux
> controllers fetch the same Git revision and reconcile the application locally.
> Fleet never needed a placement for the application's Deployments."

## 10. Change the GitOps instructions once on the hub

Patch the **Flux Kustomization**, not the generated PlacementPolicy and not
the member Deployment. Flux's Kustomize patch increases the backend to two
replicas on each member.

As with the ConfigMap, explicitly enable auto-rollout on the generated
Kustomization policy so that Fleet rolls out the changed Flux instructions.
This is a policy **annotation**, not a change to its generated spec.
Append the replica patch to the existing patch list so the node-identity
patch from section 8 remains in effect.

```bash
kubectl --context "$HUB" -n annotation-demo annotate \
  placementpolicies.placement.kubefleet.dev \
  -l placement.kubefleet.dev/parent-kind=Kustomization,placement.kubefleet.dev/parent-name=sample-app \
  kubefleet.dev/auto-rollout-enabled=true \
  kubefleet.dev/auto-rollout-minimum-wait-period=1m --overwrite

kubectl --context "$HUB" -n annotation-demo patch \
  kustomizations.kustomize.toolkit.fluxcd.io/sample-app \
  --type json --patch '
[
  {
    "op": "add",
    "path": "/spec/patches/-",
    "value": {
      "target": {
        "kind": "Deployment",
        "name": "sample-backend"
      },
      "patch": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: sample-backend\nspec:\n  replicas: 2\n"
    }
  }
]'

for MEMBER in "$EAST" "$WEST"; do
  kubectl --context "$MEMBER" -n kubefleet-sample wait \
    --for=jsonpath='{.spec.replicas}'=2 deployment/sample-backend --timeout=5m
  kubectl --context "$MEMBER" -n kubefleet-sample rollout status \
    deployment/sample-backend --timeout=3m
  kubectl --context "$MEMBER" -n kubefleet-sample get deployment sample-backend
done
```

**Expected:** `sample-backend` is `2/2` on each member.

This is a scaling demonstration, not a stateful/HA deployment: backend data
is in memory **per Pod**, so keep this beat read-only. Multiple replicas do
not share UI edits.

This is a hub-side instruction change, **not** a new Git commit. The pinned
Git revision stays unchanged. In a production GitOps setup, commit desired
changes to Git instead of maintaining imperative patches.

The generated policy **specs** are controller-managed; edit the source
annotations to change placement. Reconciliation restores edits to generated
specs, but preserves operator-added annotations such as the auto-rollout
opt-in used above.

## 11. Withdraw the placement and watch Flux prune

Stop both frontend port-forwards with **Ctrl-C** first.

Remove the Kustomization's annotation **before** the GitRepository's. The
default Fleet deletion behavior cleans up the placed object. The member
Flux Kustomization uses `prune: true` and `WaitForTermination`, so its
finalizer cleans up the app before the object disappears.

```bash
kubectl --context "$HUB" -n annotation-demo annotate \
  kustomizations.kustomize.toolkit.fluxcd.io/sample-app \
  kubefleet.dev/cluster-selectors-

for MEMBER in "$EAST" "$WEST"; do
  kubectl --context "$MEMBER" -n annotation-demo wait --for=delete \
    kustomizations.kustomize.toolkit.fluxcd.io/sample-app --timeout=5m
  kubectl --context "$MEMBER" wait --for=delete \
    namespace/kubefleet-sample --timeout=5m
done

kubectl --context "$HUB" -n annotation-demo annotate \
  gitrepositories.source.toolkit.fluxcd.io/sample-app \
  kubefleet.dev/cluster-selectors-

kubectl --context "$HUB" -n annotation-demo annotate configmap call-message \
  kubefleet.dev/cluster-selectors-

for MEMBER in "$EAST" "$WEST"; do
  kubectl --context "$MEMBER" -n annotation-demo wait --for=delete \
    gitrepositories.source.toolkit.fluxcd.io/sample-app --timeout=3m
  kubectl --context "$MEMBER" -n annotation-demo wait --for=delete \
    configmap/call-message --timeout=3m
done

kubectl --context "$HUB" -n annotation-demo get \
  placementpolicies.placement.kubefleet.dev
kubectl --context "$HUB" -n annotation-demo get configmap/call-message \
  gitrepositories.source.toolkit.fluxcd.io/sample-app \
  kustomizations.kustomize.toolkit.fluxcd.io/sample-app
```

**Expected eventually:** no generated policies remain in `annotation-demo`.
The original hub objects still exist, without the placement annotation.
Member app resources and the placed Flux objects are gone.

If cleanup stalls, inspect the member Flux Kustomization's events/finalizers
and keep its Flux controllers running. Do not remove finalizers to force a
successful-looking demo. If you choose `prune: false` or `deletionPolicy:
Orphan` in another scenario, removing placement will not imply app deletion.

## 12. Tear down only this demo's clusters

After the live call, and only if you no longer need these three clusters:

```bash
for cluster in annotation-east annotation-west annotation-hub; do
  kind delete cluster --name "$cluster"
done

rm -f "$DEMO_DIR/flux-app.yaml" "$DEMO_DIR/flux-install.yaml" \
  "$DEMO_DIR/source-controller.crds.yaml" "$DEMO_DIR/kustomize-controller.crds.yaml"
rmdir "$DEMO_DIR"
```

Local Docker images remain reusable. Do not run broad Docker cleanup commands
on a machine hosting other demos.

## Troubleshooting / presenter notes

| Symptom | Check |
| --- | --- |
| No generated policy | Both hub flags enabled? Source annotation spelled correctly? Inspect source-object events for `InvalidClusterSelectorsAnnotation`, `PlacementPolicyConflict`, or `AnnotationPlacementBlocked`. |
| Member not selected | Region labels belong on the hub MemberCluster; check case, count, taints, and cluster health. Inspect policy status and ClusterClaims. |
| Initial placement works but a source edit does not propagate | Inspect policy `Synchronized`: `ResourcesChangedPendingRollout` means a new rollout is needed. Enable auto-rollout on the generated policy as above, or create a staged update run. |
| Work exists but no Flux object on member | Member `enablePlacementPolicyAPIs=true`? Inspect `works.placement.kubefleet.dev` in that member's hub namespace and member-agent logs. |
| Flux `SourceNotFound` persists | Both Flux objects annotated with the same selectors? Both in `annotation-demo`? |
| Flux source not ready | Inspect member GitRepository events; members need HTTPS access to GitHub. |
| Flux build includes unrelated resources | Preserve the GitRepository `ignore` rules and the pinned Git commit. Do not point at all of `k8s` recursively without exclusions. |
| `ImagePullBackOff` / `ErrImageNeverPull` | Check exact local image names, Kind image loads, and Flux `spec.images`; inspect pod events. |
| `Serving from: unknown` | Confirm the hub Flux Kustomization includes the node-identity patch, enable policy auto-rollout if updating an existing placement, wait for the backend rollout, and refresh the browser. Do not patch member Deployments directly: Flux would revert those edits. |
| Member agent is unauthorized | Check the ServiceAccount/token Secret created by the join script and `hub-kubeconfig-secret` in the member's `default` namespace. Verify the MemberCluster identity and refresh-token sidecar logs; these Secret-backed tokens have no built-in expiry. |
| App resources stay after withdrawal | Check Flux prune/deletion policy and member finalizers; do not stop Flux before cleanup. |

Prefer CLI for the generated alpha PlacementPolicy objects unless the
Headlamp plugin version explicitly supports `placement.kubefleet.dev`.

### Suggested closing

> "An annotation is the simple entry point, a generated policy is the
> implementation, and Flux is still the application reconciler. We separated
> *where to deploy* from *how to reconcile*, without teaching the app about
> multiple clusters."

### References

- [Fleet annotation controller and parser](https://github.com/kubefleet-dev/kubefleet/tree/137abce1/pkg/v1/controllers/annotationplacement)
- [Fleet hub chart feature flags](https://github.com/kubefleet-dev/kubefleet/blob/137abce1/charts/hub-agent/values.yaml)
- [Flux v2.10.0 and Kubernetes compatibility](https://github.com/fluxcd/flux2/releases/tag/v2.10.0)
- [Kind v0.33.0 node images](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0)
- [Flux GitRepository](https://fluxcd.io/flux/components/source/gitrepositories/)
- [Flux Kustomization: plain YAML, patches, images, health, and deletion](https://fluxcd.io/flux/components/kustomize/kustomizations/)
