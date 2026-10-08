# Reproduce Fleet UpdateRun Error Clearing

This guide demonstrates a status-observability issue in Azure Kubernetes Fleet
Manager:

1. A Fleet member update fails with a retryable error.
2. The member enters `Pending` and exposes the error in its UpdateRun status.
3. Fleet reevaluates the member after the retry delay.
4. If the maintenance window is closed, Fleet replaces the member message with
   `member maintenance window is unavailable for update` and clears the
   previously reported error.

The AKS upgrade is **not** attempted outside the maintenance window. The error
is cleared when Fleet reevaluates scheduling and rewrites the current member
status.

> This is an Azure Kubernetes Fleet Manager control-plane reproduction, not a
> local upstream KubeFleet `kind` demo. Use nonproduction resources.

## Expected transition

Before the maintenance window closes:

```json
{
  "message": "k8s version not available for update in the target region. ...",
  "status": {
    "state": "Pending",
    "error": {
      "code": "K8sVersionNotAvailable",
      "message": "... K8sVersionNotSupported ..."
    }
  }
}
```

After the scheduled reevaluation occurs outside the maintenance window:

```json
{
  "message": "member maintenance window is unavailable for update. next window opens at: ...",
  "status": {
    "state": "Pending"
  }
}
```

The second response no longer contains `status.error`, even though no
subsequent AKS upgrade attempt succeeded.

## Timing

Fleet delays a member with a retryable version error for approximately six
hours. Configure a four-hour maintenance window and start the UpdateRun near
the beginning of that window:

```text
T+0h  Maintenance window is open; AKS rejects the target version.
      Member becomes Pending with K8sVersionNotAvailable.

T+4h  Maintenance window closes.

T+6h  Fleet reevaluates the member.
      Fleet does not call the AKS upgrade API.
      Member remains Pending for the next maintenance window.
      The previous status.error is overwritten with null.
```

Allow for several minutes of scheduling and processing delay.

## Prerequisites

- Azure CLI authenticated to a nonproduction subscription.
- The `fleet` and `aks-preview` Azure CLI extensions:

  ```bash
  az extension add --name fleet --upgrade
  az extension add --name aks-preview --upgrade
  ```

- An Azure Kubernetes Fleet Manager resource with at least one joined AKS
  member.
- Permission to update the member cluster's maintenance configuration and
  create/start Fleet UpdateRuns.
- A target Kubernetes patch that Fleet accepts but the member's region has not
  received yet.

The final prerequisite is naturally timing-dependent because AKS versions roll
out progressively. Do not use a completely invalid version: Fleet may reject
the UpdateRun before member execution, which does not exercise this behavior.

## 1. Set variables

```bash
export SUBSCRIPTION_ID="<subscription-id>"
export FLEET_RESOURCE_GROUP="<fleet-resource-group>"
export FLEET_NAME="<fleet-name>"
export MEMBER_NAME="<fleet-member-name>"
export AKS_RESOURCE_GROUP="<member-aks-resource-group>"
export AKS_NAME="<member-aks-name>"
export AKS_LOCATION="<member-aks-region>"
export TARGET_VERSION="<patch-visible-to-fleet-but-not-yet-in-member-region>"
export UPDATE_RUN_NAME="error-clearing-repro-$(date -u +%Y%m%d%H%M)"

az account set --subscription "$SUBSCRIPTION_ID"
```

Confirm that the Fleet member points to the intended disposable AKS cluster:

```bash
az fleet member show \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$MEMBER_NAME" \
  --output json
```

## 2. Confirm the regional rollout gap

List the Kubernetes patches currently advertised in the member's region:

```bash
az aks get-versions \
  --location "$AKS_LOCATION" \
  --output table
```

Continue only when:

- `TARGET_VERSION` is accepted by Fleet or appears in the Fleet version
  selection experience; and
- the exact patch is not yet available in `AKS_LOCATION`.

If the target patch is already available in the region, wait for another
progressive rollout gap. There is no supported customer API for forcing a
regional AKS version-availability failure.

## 3. Configure a four-hour maintenance window

Choose a UTC start time shortly in the future. Substitute the placeholders
below with the current UTC date, weekday, and start time:

```bash
az aks maintenanceconfiguration add \
  --resource-group "$AKS_RESOURCE_GROUP" \
  --cluster-name "$AKS_NAME" \
  --name aksManagedAutoUpgradeSchedule \
  --schedule-type Weekly \
  --day-of-week "<UTC-weekday>" \
  --interval-weeks 1 \
  --duration 4 \
  --utc-offset "+00:00" \
  --start-date "<YYYY-MM-DD>" \
  --start-time "<HH:MM>"
```

Verify the configuration:

```bash
az aks maintenanceconfiguration show \
  --resource-group "$AKS_RESOURCE_GROUP" \
  --cluster-name "$AKS_NAME" \
  --name aksManagedAutoUpgradeSchedule \
  --output json
```

Wait until the configured window is open before starting the UpdateRun. Start
near the beginning of the window so the six-hour retry occurs after the
four-hour window has closed.

## 4. Create and start the UpdateRun

For a Fleet with one disposable member, the default update strategy is
sufficient:

```bash
az fleet updaterun create \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --upgrade-type Full \
  --kubernetes-version "$TARGET_VERSION" \
  --node-image-selection Latest

az fleet updaterun start \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME"
```

For a shared Fleet, use an update strategy that targets only the disposable
member. Do not run this experiment against unrelated Fleet members.

## 5. Capture the retryable version error

Poll the UpdateRun:

```bash
az fleet updaterun show \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --output json
```

Inspect:

```text
properties.status.stages[].groups[].members[]
```

Wait for the target member to show:

```text
status.state = Pending
status.error.code = K8sVersionNotAvailable
message contains "k8s version not available for update in the target region"
```

Save the response:

```bash
az fleet updaterun show \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --output json > before-maintenance-window-reevaluation.json
```

Record the member status's last-updated time. Fleet should schedule another
evaluation for approximately six hours after the failed attempt.

## 6. Wait for the window to close and Fleet to reevaluate

Do not manually retry or modify the UpdateRun.

After the four-hour window closes, wait until at least six hours after the
version failure. Poll until the member message changes:

```bash
az fleet updaterun show \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --output json
```

The expected current status is:

```text
status.state = Pending
status.error is absent or null
message = "member maintenance window is unavailable for update.
           next window opens at: ..."
```

Save the response:

```bash
az fleet updaterun show \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --output json > after-maintenance-window-reevaluation.json
```

Compare the responses:

```bash
diff -u \
  before-maintenance-window-reevaluation.json \
  after-maintenance-window-reevaluation.json
```

The important change is:

```text
- status.error.code: K8sVersionNotAvailable
- message: k8s version not available ...
+ status.error: absent
+ message: member maintenance window is unavailable ...
```

## What this proves

The six-hour event outside the maintenance window performs scheduling
reevaluation, not an AKS upgrade:

1. Fleet reads the previous retryable member error.
2. The six-hour retry delay has expired.
3. Fleet checks the maintenance configuration.
4. The maintenance window is closed.
5. Fleet schedules the member for the next window.
6. Fleet writes a new `Pending` status without an error.

The current scheduling reason replaces the last upgrade-attempt result. A
customer checking afterward can see when Fleet will retry, but can no longer
see why the previous attempt was deferred.

## Cleanup

Delete the reproduction UpdateRun:

```bash
az fleet updaterun delete \
  --resource-group "$FLEET_RESOURCE_GROUP" \
  --fleet-name "$FLEET_NAME" \
  --name "$UPDATE_RUN_NAME" \
  --yes
```

Restore or remove the temporary maintenance configuration. To remove it:

```bash
az aks maintenanceconfiguration delete \
  --resource-group "$AKS_RESOURCE_GROUP" \
  --cluster-name "$AKS_NAME" \
  --name aksManagedAutoUpgradeSchedule
```

Remove the local captures if they contain resource identifiers:

```bash
rm before-maintenance-window-reevaluation.json
rm after-maintenance-window-reevaluation.json
```
