# rancher-fleet-clusters

> A collection of Cluster API (CAPI) examples (providers, ClusterClasses, and clusters)
> that [Fleet](https://fleet.rancher.io) reconciles and deploys via
> [Rancher Turtles](https://turtles.docs.rancher.com) on Rancher Manager.

---

## What's included

```
providers/
  rke2/          CAPRKE2 — bootstrap + control-plane provider for RKE2 clusters
  docker/        CAPD   — Docker-based nodes (local dev + CI)
  capa/          CAPA   — AWS EC2 + EKS infrastructure provider

clusterclasses/
  docker-rke2/   reusable Docker + RKE2 topology (local dev)
  aws-rke2/      reusable AWS EC2 + RKE2 topology, including a ClusterResourceSet for AWS CCM

clusters/
  docker-rke2-example/   1 CP + 1 worker in Docker containers
  aws-rke2-example/      1 CP + 2 workers on AWS EC2
  aws-eks-example/       EKS managed control plane + node group

identities/
  aws/           shared AWSClusterStaticIdentity for CAPA clusters
```

Every cluster carries `cluster-api.cattle.io/rancher-auto-import: "true"`. Turtles imports it
into Rancher automatically when it becomes Ready.

---

## Setup

### 1. Rancher + Turtles

You need Rancher ≥ v2.13 (which bundles [Rancher Turtles](https://turtles.docs.rancher.com) and
CAPI core) and `kubectl` access to the management cluster.

Don't have that yet? → [rancher-platform](https://github.com/mbologna/rancher-platform)

### 2. Provider credentials *(skip sections that don't apply)*

#### Docker: no credentials needed

Requires Docker running on the management node. Ships out of the box with rancher-platform.

#### AWS (`aws-rke2-example`, `aws-eks-example`)

**Bootstrap IAM instance profiles** *(once per AWS account)*:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

**Create the AWS cluster credentials Secret** in the CAPA controller namespace
(`capa-system`). The `AWSClusterStaticIdentity` is cluster-scoped; Fleet creates it after
CAPA is installed:

```bash
kubectl create namespace capa-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic cluster-identity \
  --namespace capa-system \
  --from-literal='AccessKeyID=<your-access-key-id>' \
  --from-literal='SecretAccessKey=<your-secret-access-key>' \
  --dry-run=client -o yaml | kubectl apply -f -
```

After registering Fleet (step 3) and waiting for the CAPA CAPIProvider to appear, **patch**
`capa-credentials` (created by Turtles) with base64-encoded AWS credentials for provider
installation:

```bash
B64_CREDS=$(printf "[default]
aws_access_key_id = <id>
aws_secret_access_key = <secret>" \
  | base64 | tr -d '
')
kubectl patch secret capa-credentials -n capa-system \
  --type=merge \
  -p "{\"data\":{\"AWS_B64ENCODED_CREDENTIALS\":\"${B64_CREDS}\"}}"
```

> **Before applying AWS examples**: replace the explicit `REPLACE_WITH_*` values in the
> cluster manifests. The RKE2 example lets CAPA create a VPC by default; its API access
> CIDR must be restricted to trusted clients. The EKS example requires your VPC and at
> least two subnets in different availability zones.

> **Cost note:** EKS charges ~$0.10/hr for the managed control plane.

### 3. Register this repo in Fleet

A ready-made manifest lives at the root of this repo:

```bash
kubectl apply -f gitrepo.yaml
```

Fleet reconciles the provider, ClusterClass, identity, and cluster directories. It does not
bundle this bootstrap manifest or the repository automation. Check progress:

```bash
kubectl get gitrepo capi-cluster-templates -n fleet-local
kubectl get bundles -n fleet-local
```

> **`keepResources: true`** prevents Fleet from deleting CAPI resources if the GitRepo is
> removed or paths change, avoiding accidental cluster teardown.

For AWS, Fleet applies the shared `AWSClusterStaticIdentity` from `identities/aws/` after the
CAPA provider bundle. The secret must already exist in `capa-system`. Fleet then deploys each AWS
cluster example after the identity and its ClusterClass are available.
The AWS RKE2 example carries `ccm: aws`, so its ClusterResourceSet installs AWS CCM on that
cluster. The EKS example does not use that ClusterResourceSet.

---

## Re-deploying Rancher

When the Rancher management cluster is rebuilt (e.g. via
[rancher-platform](https://github.com/mbologna/rancher-platform)), CAPI loses its state but the
EC2 instances it previously created keep running. Follow these steps after every re-deploy:

### 1. Terminate orphaned CAPI EC2 instances

The old instances remain tagged with the CAPA cluster tag and get re-registered into the new NLB.
They serve stale TLS CA certs, causing worker nodes to fail joining the cluster.

```bash
# List all EC2 instances still tagged for a given CAPI cluster
aws ec2 describe-instances --region eu-west-1 \
  --filters "Name=tag:sigs.k8s.io/cluster-api-provider-aws/cluster/<cluster-name>,Values=owned" \
  --query 'Reservations[*].Instances[*].{ID:InstanceId,Name:Tags[?Key==`Name`]|[0].Value,State:State.Name}' \
  --output table

# Terminate any instances that are NOT tracked by a current CAPI Machine object
aws ec2 terminate-instances --region eu-west-1 --instance-ids <orphan-id> ...
```

Do this **before** (or immediately after) registering Fleet, before CAPI spawns new machines.

### 2. Re-register Fleet

Before re-registering, recreate the `cluster-identity` Secret in `capa-system` from your secret
store if it was lost with the management cluster. Fleet recreates the `AWSClusterStaticIdentity`.

```bash
kubectl apply -f gitrepo.yaml
```

### 3. Re-patch CAPA credentials

After the CAPA CAPIProvider reaches `ProviderInstalled=True`, patch the credentials secret
(Turtles recreates it on every deploy):

```bash
B64_CREDS=$(printf "[default]
aws_access_key_id = <id>
aws_secret_access_key = <secret>" \
  | base64 | tr -d '
')
kubectl patch secret capa-credentials -n capa-system \
  --type=merge \
  -p "{\"data\":{\"AWS_B64ENCODED_CREDENTIALS\":\"${B64_CREDS}\"}}"
```

### 4. Re-import previously provisioned clusters

Rancher's TLS certificate changes on every redeploy. Any cluster whose `cattle-cluster-agent`
was deployed against the old Rancher instance will fail to reconnect. Update each agent by
re-applying the cluster's import manifest:

```bash
# Get the import manifest URL for cluster <cluster-id> (e.g. c-8f7n5)
MANIFEST=$(kubectl get secret -n cattle-system \
  $(kubectl get clusterregistrationtoken -n cattle-system -o jsonpath='{.items[0].metadata.name}') \
  -o jsonpath='{.data.manifestUrl}' | base64 -d)

# Or retrieve from the Rancher API
curl -sk -H "Authorization: Bearer <token>" \
  "https://<rancher-host>/v3/clusterregistrationtokens?clusterId=<cluster-id>" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['manifestUrl'])"

# Apply to the downstream cluster
curl -sk <manifest-url> | kubectl --kubeconfig <downstream-kubeconfig> apply -f -
```

---

## Customising clusters

The `clusters/` examples are your starting point. Fork this repo, modify an example, push. Fleet reconciles the change automatically.

```bash
cp -r clusters/aws-rke2-example clusters/my-cluster
# edit clusters/my-cluster/cluster.yaml — change name, instance type, region, …
git add clusters/my-cluster && git commit -m "feat: add my-cluster" && git push
```

---

## ClusterClass reference

### docker-rke2

| Variable    | Required | Description                                |
|-------------|----------|--------------------------------------------|
| dockerImage | yes      | `kindest/node` image tag (e.g. `v1.34.6`) |

### aws-rke2

| Variable                 | Default    | Description                              |
|--------------------------|------------|------------------------------------------|
| region                   | eu-west-1  | AWS region                               |
| sshKeyName               | empty      | Optional EC2 key pair for SSH access     |
| controlPlaneMachineType  | t3.large   | Instance type for control plane nodes    |
| workerMachineType        | t3.large   | Instance type for worker nodes           |
| amiID                    | required   | AMI ID for EC2 instances                 |
| cni                      | calico     | CNI plugin                               |
| awsClusterIdentityName   | cluster-identity | AWS static identity name           |
| vpcID                    | empty      | Existing VPC; empty = CAPA creates one   |
| subnetID                 | empty      | Optional subnet used with vpcID          |
| publicIP                 | false      | Assign public IPs when using a public subnet |
| apiServerAllowedCIDRs    | required   | Trusted CIDRs for Kubernetes API access |

### aws-eks

No ClusterClass: control plane managed entirely by AWS.

| Field        | Default    | Description                                 |
|--------------|------------|---------------------------------------------|
| region       | eu-west-1  | AWS region                                  |
| sshKeyName   | set before use | EC2 key pair for node SSH access        |
| version      | set before use | EKS-supported Kubernetes version       |
| instanceType | t3.large   | EC2 instance type for managed node group    |
| replicas     | 2          | Node count (scales between minSize–maxSize) |

---

## Version matrix

| Component       | Version | CAPI contract |
|-----------------|---------|---------------|
| CAPI core       | v1.12.5 | v1beta2       |
| CAPA (AWS)      | v2.10.2 | v1beta1 (AWS resource CRDs serve v1beta2) |
| CAPRKE2         | v0.24.2 | v1beta2       |
| CAPD (Docker)   | v1.12.5 | v1beta2       |
| RKE2 (AWS)      | v1.33.10+rke2r3 | — |
| RKE2 (Docker)   | v1.34.6+rke2r3 | — |
| Rancher Turtles | bundled with Rancher ≥ v2.13 | — |

## Validation

Install [Mike Farah yq](https://github.com/mikefarah/yq) v4.44.3 or newer and Python 3, then run:

```bash
python3 scripts/validate-repo.py
python3 -m unittest discover -s tests
```

The check parses all YAML, verifies CAPI resource and ClusterClass template references, and checks
Fleet bundle dependencies. Regression tests cover invalid CAPI references, namespaced AWS
identities, missing bundle dependencies, malformed embedded CCM resources, GitRepo source
boundaries, and ClusterClass defaults. GitHub Actions runs both commands on pushes and pull requests.

---

## Related

**[rancher-platform](https://github.com/mbologna/rancher-platform)**: Terraform + Ansible to get
a running Rancher + Turtles instance (the prerequisite for this repo).