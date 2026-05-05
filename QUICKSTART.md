# KubeFleet Sample App — End-to-End Quickstart

This guide walks you through deploying a sample banking configuration app across multiple Kubernetes clusters using [KubeFleet](https://kubefleet.dev), [Headlamp](https://headlamp.dev) with the KubeFleet plugin, and staged rollouts.

## Prerequisites

- [Docker](https://docs.docker.com/desktop/)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)
- [helm](https://helm.sh/docs/intro/install/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [gh](https://cli.github.com/) (GitHub CLI, for pushing images to ghcr.io)

## 1. Create Kind clusters

Create a hub cluster and two member clusters:

```bash
kind create cluster --name kf-hub-01
kind create cluster --name kf-member-01
kind create cluster --name kf-member-02
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

Verify the hub agent is running:

```bash
kubectl get pods -n fleet-system
```

## 3. Join member clusters

Get the hub cluster's internal IP:

```bash
HUB_IP=$(docker inspect kf-hub-01-control-plane --format='{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
echo $HUB_IP
```

Clone KubeFleet (if not already cloned) and run the join script for each member:

```bash
git clone https://github.com/kubefleet-dev/kubefleet.git ~/kubefleet  # skip if already cloned
cd ~/kubefleet/hack/quickstart

./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-01
./join-member-clusters.sh 0.3.1 kind-kf-hub-01 https://${HUB_IP}:6443/ kind-kf-member-02
```

Verify the member agents are running on each member cluster:

```bash
kubectl --context kind-kf-member-01 get pods -n fleet-system
kubectl --context kind-kf-member-02 get pods -n fleet-system
```

Each should show a `member-agent` pod in `Running` state.

Then verify both members joined the hub:

```bash
kubectl --context kind-kf-hub-01 get memberclusters
```

Both should show `JOINED: True`.

## 4. Label member clusters

Label member clusters for staged rollout:

```bash
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-01 environment=staging
kubectl --context kind-kf-hub-01 label membercluster kind-kf-member-02 environment=prod
```

## 5. Run Headlamp with the KubeFleet plugin

Start the Headlamp container with your kubeconfig and the plugin mounted:

```bash
# Install the KubeFleet Headlamp plugin
cd ~/kubefleet-headlamp-plugin
npm install
npm start  # keep this running — it builds and copies the plugin
```

In another terminal, start Headlamp:

```bash
docker run -d --name headlamp \
  --network=host \
  -u $(id -u):$(id -g) \
  -v ~/.kube:/home/headlamp/.kube:ro \
  -v ~/.config/Headlamp/plugins:/headlamp/plugins:ro \
  ghcr.io/headlamp-k8s/headlamp:v0.41.0 \
  -kubeconfig /home/headlamp/.kube/config -port 8080
```

Open Headlamp in your browser at `http://localhost:8080`. You should see your Kind clusters listed.

## 6. Build and push the sample app

```bash
cd ~/kubefleet-sample-app

# Log in to ghcr.io
gh auth token | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin

# Build and push with a versioned tag
VERSION=$(date +%Y%m%d%H%M%S)

docker build -t ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION ./backend
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION ./frontend

docker push ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION
docker push ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION
```

> **Note:** Make sure your ghcr.io packages are set to **public** so Kind nodes can pull them. You can change visibility at `https://github.com/users/YOUR_GITHUB_USERNAME/packages`.

## 7. Deploy the sample app on the hub

```bash
kubectl --context kind-kf-hub-01 apply -f k8s/namespace.yaml

kubectl --context kind-kf-hub-01 -n kubefleet-sample apply -f k8s/backend.yaml
kubectl --context kind-kf-hub-01 -n kubefleet-sample apply -f k8s/frontend.yaml

# Set the versioned image tags
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-backend backend=ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-frontend frontend=ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION
```

Verify pods are running:

```bash
kubectl --context kind-kf-hub-01 -n kubefleet-sample get pods
```

## 8. Create a ClusterResourcePlacement

In Headlamp, navigate to the **KubeFleet Manager** sidebar, select the `kind-kf-hub-01` cluster, and go to **Resource Placements**. Click **+ CREATE** and apply:

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
    type: External
```

> **Note:** Setting `strategy.type: External` means rollouts are controlled by staged update runs, not applied automatically.

## 9. Create a ClusterStagedUpdateStrategy

In Headlamp, go to **Staged Rollout Strategies** and click **+ CREATE**:

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateStrategy
metadata:
  name: sample-staged-strategy
spec:
  stages:
    - name: staging
      labelSelector:
        matchLabels:
          environment: staging
      afterStageTasks:
        - type: TimedWait
          waitTime: 1m
      maxConcurrency: 1
    - name: prod
      labelSelector:
        matchLabels:
          environment: prod
      beforeStageTasks:
        - type: Approval
      maxConcurrency: 1
```

This defines two stages:
1. **staging** — rolls out to `kind-kf-member-01`, then waits 1 minute
1. **prod** — requires manual approval before rolling out to `kind-kf-member-02`

## 10. Create and start a ClusterStagedUpdateRun

In Headlamp, go to **Staged Rollout Runs** and click **+ CREATE**:

```yaml
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: sample-run-001
spec:
  placementName: sample-crp
  stagedRolloutStrategyName: sample-staged-strategy
  state: Run
```

The run starts immediately. The staging stage rolls out to `kind-kf-member-01` automatically.

## 11. Verify the staging cluster

Port-forward the frontend service from the first member cluster:

```bash
kubectl --context kind-kf-member-01 port-forward -n kubefleet-sample svc/sample-frontend 8081:80 --address 0.0.0.0
```

Open `http://localhost:8081` in your browser. You should see the banking configuration grid.

## 12. Approve rollout to the prod cluster

After the staging stage completes and the timed wait passes, the run pauses waiting for approval.

In Headlamp, go to **Pending Approvals** under the KubeFleet Manager sidebar and approve the rollout to the prod stage.

Alternatively, approve via kubectl:

```bash
kubectl --context kind-kf-hub-01 patch clusterapproverequest \
  sample-run-001-prod -p '{"spec":{"approved":true}}' --type=merge
```

## 13. Verify the prod cluster

Port-forward the frontend service from the second member cluster:

```bash
kubectl --context kind-kf-member-02 port-forward -n kubefleet-sample svc/sample-frontend 8082:80 --address 0.0.0.0
```

Open `http://localhost:8082` in your browser. You should see the same banking configuration grid, now running on the prod cluster.

## Deploying updates

When you make changes to the app:

```bash
VERSION=$(date +%Y%m%d%H%M%S)

# Build and push
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION ./backend
docker build -t ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION ./frontend
docker push ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION
docker push ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION

# Update hub deployments (triggers a new resource snapshot)
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-backend backend=ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/backend:$VERSION
kubectl --context kind-kf-hub-01 -n kubefleet-sample \
  set image deploy/sample-frontend frontend=ghcr.io/YOUR_GITHUB_USERNAME/kubefleet-sample-app/frontend:$VERSION

# Create a new staged update run to roll out to members
kubectl --context kind-kf-hub-01 apply -f - <<EOF
apiVersion: placement.kubernetes-fleet.io/v1
kind: ClusterStagedUpdateRun
metadata:
  name: sample-run-$(date +%s)
spec:
  placementName: sample-crp
  stagedRolloutStrategyName: sample-staged-strategy
  state: Run
EOF
```

## Clean up

```bash
kubectl --context kind-kf-hub-01 delete crp sample-crp
kubectl --context kind-kf-hub-01 delete ns kubefleet-sample
docker stop headlamp && docker rm headlamp
kind delete cluster --name kf-hub-01
kind delete cluster --name kf-member-01
kind delete cluster --name kf-member-02
```
