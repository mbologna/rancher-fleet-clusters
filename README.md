# rancher-fleet-clusters

> A collection of Cluster API (CAPI) examples — providers, ClusterClasses, and clusters —
> that [Fleet](https://fleet.rancher.io) reconciles and deploys via
> [Rancher Turtles](https://turtles.docs.rancher.com) on Rancher Manager.

---

## What's included

```
providers/
  rke2/          CAPRKE2 — bootstrap + control-plane provider for RKE2 clusters
  docker/        CAPD   — Docker-based nodes (local dev + CI)
  capa/          CAPA   — AWS EC2 + EKS infrastructure provider
  capm3/         CAPM3  — bare-metal via Metal3 + Ironic

clusterclasses/
  docker-rke2/   reusable Docker + RKE2 topology (local dev)
  aws-rke2/      reusable AWS EC2 + RKE2 topology
  metal3-rke2/   reusable bare-metal + RKE2 topology

clusters/
  docker-rke2-example/   1 CP + 1 worker in Docker containers
  aws-rke2-example/      1 CP + 2 workers on AWS EC2 (RKE2 v1.33.x)
  aws-eks-example/       EKS managed control plane + node group (k8s v1.31)
  metal3-rke2-example/   1 CP + 2 workers on bare metal via Metal3

applications/
  aws-ccm/       AWS Cloud Controller Manager — deployed to clusters labelled infrastructure=aws
```

Every cluster carries `cluster-api.cattle.io/rancher-auto-import: "true"` — Turtles imports it
into Rancher automatically when it becomes Ready.

---

## Setup

### 1. Rancher + Turtles

You need Rancher ≥ v2.13 (which bundles [Rancher Turtles](https://turtles.docs.rancher.com) and
CAPI core) and `kubectl` access to the management cluster.

Don't have that yet? → [rancher-platform](https://github.com/mbologna/rancher-platform)

### 2. Provider credentials *(skip sections that don't apply)*

#### Docker — no credentials needed

Requires Docker running on the management node. Ships out of the box with rancher-platform.

#### AWS (`aws-rke2-example`, `aws-eks-example`)

**Bootstrap IAM instance profiles** *(once per AWS account)*:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

**Create the credentials secrets** (the namespace and secrets can be created before Fleet runs):

```bash
kubectl create namespace capa-system --dry-run=client -o yaml | kubectl apply -f -

# Secret referenced by AWSClusterStaticIdentity — needs AccessKeyID + SecretAccessKey
kubectl create secret generic cluster-identity \
  --namespace capa-system \
  --from-literal=AccessKeyID=<your-access-key-id> \
  --from-literal=SecretAccessKey=<your-secret-access-key>
```

After registering Fleet (step 3) and waiting for the CAPA CAPIProvider to appear, **patch**
`capa-credentials` (created by Turtles) with base64-encoded AWS credentials for provider
installation:

```bash
B64_CREDS=$(printf "[default]\naws_access_key_id = <id>\naws_secret_access_key = <secret>" \
  | base64 | tr -d '\n')
kubectl patch secret capa-credentials -n capa-system \
  --type=merge \
  -p "{\"data\":{\"AWS_B64ENCODED_CREDENTIALS\":\"${B64_CREDS}\"}}"
```

> **VPC/subnet IDs**: `clusters/aws-rke2-example/cluster.yaml` and
> `clusters/aws-eks-example/cluster.yaml` contain hardcoded VPC and subnet IDs — update
> them to match your own infrastructure before use.

> **Cost note:** EKS charges ~$0.10/hr for the managed control plane.

#### Metal3 (`metal3-rke2-example`)

Requires Ironic running and reachable from the management cluster, DHCP + PXE on the bare-metal
network, and `BareMetalHost` objects registered in the `default` namespace.

**Create the Ironic credentials secret**:

```bash
kubectl create namespace capm3-system --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret generic ironic-credentials \
  --namespace capm3-system \
  --from-literal=IRONIC_URL=http://<ironic-host>:6385/v1/ \
  --from-literal=IRONIC_INSPECTOR_URL=http://<ironic-host>:5050/v1/ \
  --from-literal=IRONIC_USERNAME=<username> \
  --from-literal=IRONIC_PASSWORD=<password>
```

### 3. Register this repo in Fleet

A ready-made manifest lives at the root of this repo:

```bash
kubectl apply -f gitrepo.yaml
```

Fleet reconciles all paths — installs CAPI providers, deploys ClusterClasses, and provisions
the example clusters. Check progress:

```bash
kubectl get gitrepo capi-cluster-templates -n fleet-local
kubectl get bundles -n fleet-local
```

> **`keepResources: true`** prevents Fleet from deleting CAPI resources if the GitRepo is
> removed or paths change — avoids accidental cluster teardown.

### 4. Create the AWS cluster identity *(AWS only)*

`AWSClusterStaticIdentity` is a CRD installed by CAPA — it won't exist until Fleet has deployed
the provider. Wait for the CAPA provider to be ready, then create the identity:

```bash
kubectl wait capiprovider aws -n capa-system \
  --for=condition=ProviderInstalled=True --timeout=300s

kubectl apply -f - <<EOF
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSClusterStaticIdentity
metadata:
  name: cluster-identity
spec:
  secretRef: cluster-identity
  allowedNamespaces: {}
EOF
```

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

Do this **before** (or immediately after) registering Fleet — before CAPI spawns new machines.

### 2. Re-register Fleet

```bash
kubectl apply -f gitrepo.yaml
```

### 3. Re-patch CAPA credentials

After the CAPA CAPIProvider reaches `ProviderInstalled=True`, patch the credentials secret
(Turtles recreates it on every deploy):

```bash
B64_CREDS=$(printf "[default]\naws_access_key_id = <id>\naws_secret_access_key = <secret>" \
  | base64 | tr -d '\n')
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

The `clusters/` examples are your starting point. Fork this repo, modify an example, push —
Fleet reconciles the change automatically.

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
| sshKeyName               | (required) | EC2 key pair name                        |
| controlPlaneInstanceType | t3.large   | Instance type for control plane nodes    |
| workerInstanceType       | t3.large   | Instance type for worker nodes           |
| vpcID                    | (empty)    | Existing VPC ID; empty = CAPA creates    |
| amiID                    | (required) | AMI ID for EC2 instances                 |

### aws-eks

No ClusterClass — control plane managed entirely by AWS.

| Field        | Default    | Description                                 |
|--------------|------------|---------------------------------------------|
| region       | eu-west-1  | AWS region                                  |
| sshKeyName   | (required) | EC2 key pair for node SSH access            |
| version      | v1.31      | EKS-supported Kubernetes version            |
| instanceType | t3.large   | EC2 instance type for managed node group    |
| replicas     | 2          | Node count (scales between minSize–maxSize) |

### metal3-rke2

| Variable               | Required | Description                       |
|------------------------|----------|-----------------------------------|
| imageURL               | yes      | HTTP URL of OS raw image          |
| imageChecksum          | yes      | SHA256 checksum of the image      |
| controlPlaneEndpointIP | yes      | VIP for the Kubernetes API server |

---

## Version matrix

| Component       | Version |
|-----------------|---------|
| CAPI core       | v1.12.5 |
| CAPA (AWS)      | v2.10.2 |
| CAPRKE2         | v0.24.2 |
| CAPD (Docker)   | v1.12.5 |
| CAPM3 (Metal3)  | v1.12.3 |
| RKE2            | v1.33.x |
| Rancher Turtles | bundled with Rancher ≥ v2.13 |

---

## Related

**[rancher-platform](https://github.com/mbologna/rancher-platform)** — Terraform + Ansible to get
a running Rancher + Turtles instance (the prerequisite for this repo).
