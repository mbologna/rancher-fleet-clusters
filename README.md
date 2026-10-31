# rancher-fleet-clusters

Fleet bundles that create Kubernetes clusters from a Rancher management cluster with Cluster API
(CAPI). The repository holds ClusterClasses, a shared AWS identity and three example clusters.
Rancher Turtles imports each cluster into Rancher once it is ready.

[rancher-platform](https://github.com/mbologna/rancher-platform) deploys the management cluster
and installs the CAPI providers with the values in [`platform/`](platform/).

| Example | Class | Infrastructure | Nodes |
|---|---|---|---|
| [`docker-rke2-example`](clusters/docker-rke2-example/cluster.yaml) | `docker-rke2-v136` | Docker containers on the management host (development only) | 1 control plane, 1 worker |
| [`aws-rke2-example`](clusters/aws-rke2-example/cluster.yaml) | `aws-rke2-v136` | EC2 with an internal NLB | 1 control plane, 2 workers |
| [`aws-eks-example`](clusters/aws-eks-example/cluster.yaml) | none | EKS with a managed node group | managed |

Registering the GitRepo applies every example. Once their `REPLACE_WITH_*` values are filled
in, the AWS examples create billable EC2 instances, load balancers, EKS control planes and
storage. Delete the examples you do not want from your fork first.

## Versions

| Component | Version |
|---|---|
| Rancher | v2.15.2, on management Kubernetes v1.34–v1.36 |
| Rancher Turtles | v0.27.2 |
| CAPI core and Docker provider (CAPD) | v1.13.3 |
| RKE2 providers (CAPRKE2) | v0.25.2 |
| AWS provider (CAPA) | v2.11.1 |
| Workload RKE2 | v1.36.4+rke2r1 |
| AWS cloud controller manager | v1.36.1 |
| Docker node image | kindest/node:v1.36.1 |

Provider versions follow [Turtles v0.27.2's provider catalogue][catalog]. CAPI resources use
the `v1beta2` API. The pins live in [`platform/`](platform/) and the cluster manifests.
Renovate follows the shared preset; the provider stack and RKE2 updates need manual review.

Live-tested on 17 October 2026: the Docker and AWS RKE2 examples reached `Available` and were
imported into Rancher. The EKS example has not been live-tested.

## Quick start

1. **Deploy the management cluster** with rancher-platform and the providers enabled. Add
   `capa_aws_access_key_id` and `capa_aws_secret_access_key` to its `secrets.yml` to get the
   AWS provider; without them only the Docker and RKE2 providers are installed.

   ```bash
   ./manage.sh deploy -e capi_providers_enabled=true
   ```

2. **Fork this repository and configure the examples.** Replace every `REPLACE_WITH_*` value
   (see [AWS](#aws)) or delete the examples you do not need. Commit and push: Fleet reads the
   remote branch, not local files.

3. **Validate** the manifests and the management cluster. Both checks are read-only.

   ```bash
   python3 scripts/validate-repo.py --schemas --deployment-check
   python3 scripts/check-platform.py --context <management-context>
   ```

   The first fails while placeholders remain. The second checks provider versions and
   ownership, controller availability, feature flags, CRDs, Rancher's `server-url`, and that
   no example cluster name already exists. It does not test AWS permissions or networking.

4. **Register the GitRepo.** Point `spec.repo` and `spec.branch` in
   [`gitrepo.yaml`](gitrepo.yaml) at your fork, then apply it:

   ```bash
   kubectl --context <management-context> apply -f gitrepo.yaml
   kubectl --context <management-context> get bundles -n fleet-local
   kubectl --context <management-context> get clusters -A
   ```

   rancher-platform can do this step with `-e fleet_gitrepo_enabled=true` and
   `fleet_gitrepo_manifest_url` set to your fork's raw `gitrepo.yaml`.

Fleet applies the identity, the ClusterClasses and then the clusters, in dependency order.
The dependency names include the GitRepo name, `capi-cluster-templates`; rename both together.
`keepResources: true` keeps CAPI clusters when the GitRepo is deleted or its paths change.

An installation that already runs these clusters or older provider versions needs the staged
upgrade in [docs/migration.md](docs/migration.md), not a fresh install.

## AWS

Create CAPA's IAM roles once per account, with a `clusterawsadm` release matching CAPA:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

CAPA reads two Secrets in `capa-system`. rancher-platform creates both from `secrets.yml`.

| Secret | Keys | Used by |
|---|---|---|
| `capa-credentials` | `AWS_B64ENCODED_CREDENTIALS`: a base64 AWS shared-credentials file | CAPA controller |
| `cluster-identity` | `AccessKeyID`, `SecretAccessKey` | `AWSClusterStaticIdentity` `cluster-identity`, usable from the `default` namespace |

**Network.** The RKE2 class puts the Kubernetes API behind an internal NLB, so the
management cluster needs a route into the workload VPC. The simplest layout reuses the
management host's VPC: set `vpcID`, a public `subnetID`, `publicIP: true`, and the VPC CIDR in
both `nodeCIDRs` and `apiServerAllowedCIDRs`. Otherwise connect the VPCs by peering or VPN.
A CAPA-created VPC uses `10.0.0.0/16`, which must not overlap the management, pod or service
networks. Nodes need outbound internet access to download RKE2 and images.

**EKS** needs a VPC and at least two subnets in different availability zones, and a
Kubernetes version that EKS currently offers in the region.

## Cluster classes

Published templates are immutable; a change needs a new class name. The `-v136` classes are
for new clusters. The original `aws-rke2` and `docker-rke2` classes stay for clusters that
already use them.

AWS RKE2 (`aws-rke2-v136`) variables:

| Variable | Default | Meaning |
|---|---|---|
| `region` | `eu-west-1` | AWS region |
| `amiID` | required | Base OS AMI; CAPRKE2 installs RKE2 at boot |
| `sshKeyName` | empty | EC2 key pair |
| `controlPlaneMachineType`, `workerMachineType` | `t3.large` | Instance types |
| `cni` | `calico` | RKE2 CNI |
| `awsClusterIdentityName` | `cluster-identity` | AWS identity |
| `vpcID`, `subnetID` | empty | Existing network; set both or neither |
| `publicIP` | `false` | Public node addresses |
| `apiServerAllowedCIDRs` | required | Clients allowed to reach the API load balancer |
| `nodeCIDRs` | required | Private node addresses |

Nodes take their hostnames from IMDSv2 and run the AWS cloud controller manager, which a
ClusterResourceSet installs on clusters labelled `ccm: aws-v136` and `ccm-version: "1.36"`. The
legacy `ccm: aws` label selects the old apply-once payload.

Docker RKE2 (`docker-rke2-v136`) takes one variable, `dockerImage`, the `kindest/node` image.
CAPD needs a Docker network called `kind` that pods can reach; rancher-platform creates it.

The EKS example uses `AWSManagedControlPlane` and `AWSManagedMachinePool` directly. Its
Kubernetes version is independent of the RKE2 examples.

## Providers without rancher-platform

Merge these files into the management cluster's existing Turtles releases. Do not install a
second `rancher-turtles` release.

| File | Purpose |
|---|---|
| [`platform/turtles-values.yaml`](platform/turtles-values.yaml) | `rancher-turtles` chart: core CAPI version and config Secret |
| [`platform/providers-values.yaml`](platform/providers-values.yaml) | `rancher-turtles-providers` chart: CAPA, CAPD and both CAPRKE2 providers |
| [`platform/core-config.yaml`](platform/core-config.yaml) | Secret enabling core CAPI's `ClusterTopology` feature |

Use the charts from the Turtles v0.27.2 tag or the matching Rancher-packaged chart. Apply
`core-config.yaml` in `cattle-capi-system` before the core provider reconciles. CAPA needs
its Secrets before it starts. The values keep RKE2 and CAPD in `cattle-turtles-system` to
match migrated installs; rancher-platform uses the chart's own namespaces instead because
Helm cannot adopt an existing namespace. `check-platform.py` accepts both.

## Repository layout

```text
clusterclasses/   ClusterClasses and the AWS CCM ClusterResourceSets (Fleet bundles)
identities/       AWS cluster identity (Fleet bundle)
clusters/         example clusters (Fleet bundles)
platform/         provider chart values; applied by the platform, never by Fleet
scripts/          validate-repo.py, check-platform.py and schema helpers
tests/            unit tests for the scripts
docs/             migration guide
gitrepo.yaml      Fleet GitRepo for the three bundle directories
```

## Development

Requirements: Python 3.12 or newer, [yq][yq] v4.44.3 or newer and Helm v3.18.6.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-validation.txt
python3 scripts/validate-repo.py --schemas
python3 -m unittest discover -s tests
```

`validate-repo.py` checks YAML, Fleet dependencies, template references, patch paths, CIDRs
and version conventions. `--schemas` downloads the pinned CRDs, Kubernetes schemas and Turtles
charts, then validates every resource and renders both platform charts offline. Static checks
do not cover CEL rules, admission defaults or a real bootstrap.

GitHub Actions runs two workflows:

- **Validate** runs the schema validation and unit tests on every push and pull request.
- **Platform check** builds a disposable management cluster on the runner with
  rancher-platform, installs this branch's `providers-values.yaml` and runs
  `check-platform.py`. It runs on changes to `platform/` or the check, weekly, and on demand.
  CAPA gets dummy credentials, so it creates no AWS resources.

Renovate uses the shared [mbologna/.github preset][preset], so routine updates automerge on
green CI after its release-age gate. Provider-stack and RKE2 updates are grouped, wait for
approval on the dependency dashboard and never automerge. RKE2 minor upgrades are disabled
because they need new classes and a CCM migration.

[preset]: https://github.com/mbologna/.github/blob/main/renovate-config.json
[catalog]: https://github.com/rancher/turtles/blob/v0.27.2/internal/controllers/clusterctl/config-prime.yaml
[yq]: https://github.com/mikefarah/yq
