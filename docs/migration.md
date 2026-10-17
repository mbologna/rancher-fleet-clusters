# Migrating an existing installation

These manifests contain fresh-install defaults. Pause Fleet reconciliation before changing
provider ownership, ClusterClass references or Kubernetes versions on an existing installation.
A Git update can roll machines or change cloud infrastructure.

## Inventory and backup

Record the installed Rancher/Turtles chart versions, all `CAPIProvider` resources and their
namespaces, provider controller deployments, ClusterClasses, Clusters and cloud resources.
Use an explicit management kubecontext for every command.

Take a supported Rancher/management-cluster backup covering CAPI state, provider resources
and credentials. Check the backup's restore coverage and protect credentials in a secret
store. Workload etcd snapshots alone cannot reconstruct the management cluster's CAPI state.

Keep the previous Git revision and Helm values. Inventory existing clusters named
`aws-rke2-example`, `docker-rke2-example` or `aws-eks-example`: their names match this
repository's examples. Preserve their existing class, version, variables and CCM labels in
your fork before adopting the new layout.

## Provider ownership

The previous repository declared AWS in `capa-system`, Docker and both RKE2 providers in
`cattle-turtles-system`. New provider-chart defaults use separate Docker and RKE2
namespaces; the supplied values deliberately preserve the previous namespaces.

The platform must own exactly one provider for each type/name pair:

| Type | Provider | Namespace in supplied values |
|---|---|---|
| core | cluster-api | cattle-capi-system |
| bootstrap | rke2 | cattle-turtles-system |
| controlPlane | rke2 | cattle-turtles-system |
| infrastructure | aws | capa-system |
| infrastructure | docker | cattle-turtles-system |

Pause the affected Fleet bundles. Compare the chart-rendered provider names/specs with
the installed objects. Use the platform's supported Helm/Fleet ownership-adoption process;
existing Helm release annotations can prevent adoption. Confirm that old bundle removal
retains the provider objects before changing ownership metadata. `keepResources` alone does
not adopt resources into another Helm release.

Do not delete `CAPIProvider` resources, provider namespaces, CRDs or controller inventory to
transfer ownership. Do not run `clusterctl init` alongside Turtles. Moving an object between
namespaces creates a different object and may install a second provider. If adoption requires
a namespace change or the chart cannot represent the installed resources, stop and plan
that migration separately.

Configure `platform/core-config.yaml` and both chart-values files through the platform.
Upgrade providers using their supported release sequence and verify installed versions,
readiness and controller availability. Ensure AWS credentials remain in the controller's
namespace. Only then remove the old Fleet provider bundles from reconciliation.

The GitRepo no longer includes `providers/`; its remaining dependencies concern repo-owned
bundles only. Resume Fleet after the platform owns and has reconciled the required providers.
The fresh-install preflight deliberately refuses existing example cluster names; existing
installations need a reviewed migration rather than bypassing the warning.

## Kubernetes and immutable templates

Rancher v2.15 and Turtles v0.27 require management Kubernetes v1.34–v1.36. Move unsupported
workloads through supported intermediate versions before upgrading Rancher beyond their
support window.

An existing v1.33 RKE2 cluster needs supported v1.34 and v1.35 upgrades before v1.36.
Check RKE2, CAPRKE2 and CCM compatibility at each step. Wait for control-plane health,
worker readiness and application health before proceeding. These manifests supply the
v1.36 destination; they do not automate the intermediate rollouts.

Legacy classes and AWS templates remain in the same bundles with their original names.
Their old ignore rules are scoped to those exact names. The new `*-v136` templates do not
hide spec drift. Future template changes need new names, not edits to already-published specs.

Changing a ClusterClass can replace infrastructure. Rebase a cluster only after checking
the rendered changes, endpoint reachability, maintenance window and replacement capacity.
The v1.36 AWS class uses an internal NLB; adding VPN/peering routes may be necessary.
It does not preserve the old public-NLB/subnet-scan workaround.

Keep the old `aws-ccm` ApplyOnce resource set for legacy `ccm: aws` clusters.
The new `aws-ccm-v136` set uses Reconcile and matches both `ccm: aws-v136` and
`ccm-version: "1.36"`. Plan a selector/payload handover at the appropriate Kubernetes
upgrade step; ensure two sets never manage conflicting CCM versions. Do not mutate the old
set's strategy or label an older cluster as v1.36 merely to receive updates.

The shared AWS identity now allows only `default`. If you have workloads in other namespaces,
include those namespaces explicitly in its allowed list and update the repository policy check
as part of that reviewed change.

## Recovery and rollback

Pause reconciliation before recovery. Restore management CAPI state and credential material
using the supported backup procedure before re-registering Fleet. Do not recreate a cluster
with the same name against orphaned infrastructure as a substitute for restoring state.

Inventory untracked EC2 instances, load balancers, volumes, network resources and EKS clusters.
Confirm ownership and data-retention requirements before authorising any deletion; losing
management state does not make running infrastructure disposable.

If Rancher certificates change, re-import workload clusters using the intended cluster's
registration manifest. Verify TLS with the trusted CA (`curl --fail --cacert <ca.pem>` when
needed); do not disable certificate verification or reuse a token selected from an unrelated
cluster.

Restore the previous Git and chart configuration only where the relevant versions support
it. Kubernetes downgrades and arbitrary provider downgrades are not rollback procedures.
Retain legacy templates and backups until all consumers have migrated and recovery has
been exercised.

## Upstream references

- [Turtles v0.27.2 release](https://github.com/rancher/turtles/releases/tag/v0.27.2)
- [Provider chart defaults](https://github.com/rancher/turtles/blob/v0.27.2/charts/rancher-turtles-providers/values.yaml)
- [CAPI v1.13 compatibility and API removal schedule](https://github.com/kubernetes-sigs/cluster-api/blob/v1.13.3/docs/book/src/reference/versions.md)
- [Official AWS RKE2 class](https://github.com/rancher/turtles/blob/v0.27.2/examples/clusterclasses/aws/rke2/clusterclass-ec2-rke2-example.yaml)
- [Turtles application delivery](https://turtles.docs.rancher.com/turtles/v0.27/en/user/applications.html)
