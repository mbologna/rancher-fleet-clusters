# rancher-fleet-clusters

Fleet bundles for AWS EC2/RKE2, Docker/RKE2 and EKS managed node groups.
Rancher Turtles imports each cluster when it becomes Ready.

**All three examples remain enabled.** AWS examples contain `REPLACE_WITH_*` values.
Configure them before registering the GitRepo. Once configured, they can create billable
EC2 instances, EKS clusters, load balancers, NAT gateways and storage.

## Supported stack

The baseline follows [Turtles v0.27.2's pinned provider catalog][catalog] and
[Rancher v2.15.2][rancher-release]. Platform configuration owns the providers; Fleet owns
ClusterClasses, identities and clusters.

| Component | Version | API or contract |
|---|---|---|
| Rancher | v2.15.2 | Management Kubernetes v1.34–v1.36 |
| Turtles | v0.27.2 | `CAPIProvider`: `turtles-capi.cattle.io/v1alpha1` |
| CAPI core / CAPD | v1.13.3 | `v1beta2` |
| CAPRKE2 | v0.25.2 | `v1beta2` |
| CAPA | v2.11.1 | `v1beta1` provider contract; AWS resource APIs serve `v1beta2` |
| New AWS / Docker RKE2 clusters | v1.36.4+rke2r1 | Rancher-supported v1.36 line |
| Docker node image | kindest/node:v1.36.1 | Development only |
| AWS CCM for new clusters | v1.36.1 | Kubernetes v1.36 |

`v1beta2` is the current CAPI API used by official stable Turtles examples. Its name does
not imply a prerelease controller. There is no general `v1` migration. Turtles v0.28 RCs
are excluded. API maturity, controller versions and provider contracts are separate.

The Prime catalog pins the provider versions above; the community catalog pins only core
CAPI. Explicit chart values keep both installations reproducible. Newer independent provider
releases need compatibility review before adoption. CAPA's older contract uses CAPI's
transitional compatibility; do not upgrade core CAPI independently past that compatibility.

Rancher v2.15 no longer supports Kubernetes v1.33. Existing clusters need a staged upgrade,
not a direct application of these fresh-install defaults. See [migration](docs/migration.md).

## Repository layout

```text
platform/                         chart values and core configuration; never Fleet bundles
clusterclasses/aws-rke2/           legacy and versioned classes, AWS CCM resource sets
clusterclasses/docker-rke2/        legacy and versioned Docker classes
identities/aws/                   shared, cluster-scoped AWS identity
clusters/aws-rke2-example/         1 control plane + 2 workers
clusters/docker-rke2-example/      1 control plane + 1 worker
clusters/aws-eks-example/          EKS control plane + managed node group
```

The GitRepo reconciles only `clusterclasses`, `identities` and `clusters`.
`platform/`, automation and the GitRepo bootstrap manifest stay outside those paths.
The GitRepo name `capi-cluster-templates` is part of the Fleet dependency names. If you
rename it in a fork, update the `dependsOn` names too.

## Fresh installation

### 1. Configure platform-owned providers

Install the supported Rancher/Turtles stack on management Kubernetes v1.34–v1.36.
[rancher-platform](https://github.com/mbologna/rancher-platform) is the companion platform
repository. Rancher installs Turtles as a system component; check the installed chart
version rather than assuming Rancher bundles exactly v0.27.2.

Merge these files into the platform's existing release configuration:

| File | Platform release / purpose |
|---|---|
| `platform/turtles-values.yaml` | Main `rancher-turtles` chart: core CAPI version and config Secret |
| `platform/providers-values.yaml` | `rancher-turtles-providers` chart: AWS, Docker and both RKE2 providers |
| `platform/core-config.yaml` | Non-credential Secret enabling core CAPI's `ClusterTopology` feature |

Use provider chart sources from the **v0.27.2 tag**, or the matching Rancher-packaged chart,
not an independently newer chart. Source `Chart.yaml` versions can be build placeholders.
The schema check renders both charts from that tag.

Create `cattle-capi-system` and apply the core config Secret before the core provider
reconciles. Configure this through the platform's deployment process:

```bash
kubectl create namespace cattle-capi-system --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f platform/core-config.yaml
```

Do not install a second main Turtles release. For an existing Fleet-owned installation,
complete the [ownership migration](docs/migration.md) before enabling chart-owned providers.
The supplied values preserve the previous RKE2 and Docker namespace,
`cattle-turtles-system`, to avoid an unnecessary controller relocation.

MachinePool is enabled by default in core CAPI v1.13; CAPA still needs
`EXP_MACHINE_POOL: "true"`, supplied in its chart values. ClusterResourceSet no longer needs
the old `EXP_CLUSTER_RESOURCE_SET` flag. Topology must be enabled on **core CAPI**.
CAAPF is disabled; CCM uses CAPI ClusterResourceSet directly.

Docker requires a reachable Docker daemon configured for CAPD. It is a development
provider, not a production infrastructure option.

### 2. Configure AWS credentials and examples

Bootstrap IAM profiles once per AWS account with a `clusterawsadm` version matching CAPA:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

Store credentials outside Git. Before the AWS provider starts, supply:

- `capa-credentials` in `capa-system`, with an `AWS_B64ENCODED_CREDENTIALS` key containing
  a base64-encoded AWS shared-credentials INI document.
- `cluster-identity` in `capa-system`, with `AccessKeyID` and `SecretAccessKey` keys for
  the shared `AWSClusterStaticIdentity`.

For example, using files from a secret store:

```bash
kubectl create namespace capa-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic capa-credentials -n capa-system \
  --from-file=AWS_B64ENCODED_CREDENTIALS=/secure/aws-credentials.b64
kubectl create secret generic cluster-identity -n capa-system \
  --from-file=AccessKeyID=/secure/aws-access-key-id \
  --from-file=SecretAccessKey=/secure/aws-secret-access-key
```

Use the platform's secret-management process to update existing Secrets. The static identity
is cluster-scoped and permits use only from `default`; its credential Secret belongs in
the CAPA controller namespace, not the workload cluster namespace.

Replace every `REPLACE_WITH_*` in both AWS examples. Set AMI, key pair, trusted client
CIDRs, EKS Kubernetes version, VPC and subnet IDs as applicable. EKS needs at least two
subnets in different availability zones; check current regional EKS version availability.

The new AWS RKE2 class uses an **internal NLB**. Management controllers, Rancher and API
clients need routed access to its VPC through peering, VPN or equivalent. The CAPA-created
VPC uses `10.0.0.0/16`; avoid overlaps with your management, pod and service networks.
For an existing VPC, set `vpcID`, `subnetID` and `nodeCIDRs` together. `nodeCIDRs` must
cover the private node addresses. Nodes still need outbound access to download RKE2 and images.

### 3. Validate and register Fleet

Install validation dependencies as described below. Both checks are read-only:

```bash
python3 scripts/validate-repo.py --schemas --deployment-check
python3 scripts/check-platform.py --context <management-context>
```

The first check deliberately fails on the repository's unconfigured AWS placeholders.
The platform check verifies versions, single provider ownership, controller availability,
features, served CRDs and existing example cluster names. It refuses to treat an existing
cluster as a fresh installation. It does not test AWS IAM permissions or network reachability.

Commit the configured manifests to your fork and point `gitrepo.yaml` at that fork and
the reviewed branch before applying it. Fleet reads the remote commit, not your local files.

```bash
kubectl --context <management-context> apply -f gitrepo.yaml
kubectl --context <management-context> get gitrepo capi-cluster-templates -n fleet-local
kubectl --context <management-context> get bundles -n fleet-local
```

`keepResources: true` retains CAPI resources when the GitRepo is removed or paths change;
it does not transfer provider ownership or protect against explicit Kubernetes deletion.
Fleet dependencies order the repo-owned identity, ClusterClass and cluster bundles.
Platform readiness must be established before registering the GitRepo.

## Cluster configuration

Fresh RKE2 examples reference `aws-rke2-v136` and `docker-rke2-v136`.
The original `aws-rke2` and `docker-rke2` classes remain available for existing clusters.
Treat published templates as immutable: create a new name for future template changes.

### AWS RKE2 variables

| Variable | Default | Meaning |
|---|---|---|
| region | eu-west-1 | AWS region |
| sshKeyName | empty | Optional EC2 key pair |
| controlPlaneMachineType / workerMachineType | t3.large | Instance types |
| amiID | required | Supported base OS AMI; CAPRKE2 installs RKE2 |
| cni | calico | CNI plugin |
| awsClusterIdentityName | cluster-identity | Shared AWS identity |
| vpcID / subnetID | empty | Existing network; supply both or neither |
| publicIP | false | Public node addresses, if the selected subnet supports them |
| apiServerAllowedCIDRs | required | Trusted management/client CIDRs |
| nodeCIDRs | required | Private node CIDRs; example uses 10.0.0.0/16 |

CAPRKE2 installs the topology's RKE2 version. New templates use IMDSv2 for hostnames,
do not scan subnets and do not modify `/etc/hosts`. They disable RKE2's cloud controller
and retain AWS Secrets Manager bootstrap handling.

`ccm: aws-v136` and `ccm-version: "1.36"` select the reconciled v1.36 CCM payload.
Legacy `ccm: aws` selects the old ApplyOnce payload. Do not select a newer CCM before
planning the corresponding Kubernetes upgrade.

### Docker and EKS

Docker exposes `dockerImage`, required for the `kindest/node` image.
The image version and installed RKE2 patch need not match. Pod and service CIDRs remain
non-overlapping; anonymous API authentication is not enabled.

EKS retains `AWSManagedControlPlane` and `AWSManagedMachinePool`. The official Turtles EKS
ClusterClass uses self-managed workers and is not a drop-in replacement. This repo does
not force EKS onto the RKE2 Kubernetes version.

## Validation and updates

Use Python 3.12 or newer, [Mike Farah yq][yq] v4.44.3 or newer and Helm v3.18.6:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-validation.txt
python3 scripts/validate-repo.py --schemas
python3 -m unittest discover -s tests
```

`scripts/validate-repo.py` checks YAML, source boundaries, Fleet dependencies, template
references, patch paths, CIDRs and provider/version conventions. `--schemas` uses
`scripts/schema_validation.py` to download pinned CAPI/provider CRDs, Kubernetes v1.36.4
schemas and Turtles chart schemas, then render both platform charts offline with Helm.
Example placeholders retain their string type but skip string pattern/enum constraints;
`--deployment-check` rejects them entirely. Network access is required for schema downloads.
CEL rules, admission/defaulting and full CAPI topology generation require a live integration
test; these static checks do not prove successful bootstrap.

GitHub Actions runs schema/chart validation and the regression suite. The read-only
`scripts/check-platform.py` requires an explicit kubecontext and is never run by CI.
No live deployment or Docker lifecycle test has been performed for this baseline.

`renovate.json` groups stack updates for manual compatibility review, rejects prereleases,
pins GitHub Actions and excludes legacy templates. Workload minor upgrades are disabled:
they require new classes and a CCM migration. Renovate also tracks Python validation
dependencies and yq. Enable the Renovate GitHub App for this repository; configuration
alone does not schedule updates. Grouping upstream releases does not certify their compatibility.

## Further improvements

| Priority | Proposal | Prerequisite |
|---|---|---|
| P1 | Short-lived AWS credentials and least-privilege IAM boundaries | Platform workload identity / secret-store design |
| P1 | Opt-in Docker lifecycle CI: create, wait for Ready nodes, delete | Dedicated runner and management-cluster isolation |
| P1 | Automated backup/restore drill for Rancher, CAPI and credentials | Supported backup tooling and recovery environment |
| P2 | CSI/storage and workload health examples | Explicit ownership, compatible versions and cost controls |
| P2 | Separate EKS ClusterClass offering with self-managed workers | An explicit need for self-managed nodes |

AWS examples remain enabled by user choice. Opt-in deployment profiles would further
reduce accidental provisioning, but are not part of this configuration.

[catalog]: https://github.com/rancher/turtles/blob/v0.27.2/internal/controllers/clusterctl/config-prime.yaml
[rancher-release]: https://github.com/rancher/rancher/releases/tag/v2.15.2
[yq]: https://github.com/mikefarah/yq
