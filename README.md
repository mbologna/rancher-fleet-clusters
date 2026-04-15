# rancher-fleet-clusters

> CAPI cluster templates for Rancher Fleet.
> One command to get CAPI providers, ClusterClasses, and example clusters running on any Rancher instance.

---

## Quickstart

**Prerequisite**: Rancher ≥ 2.14 with [Turtles](https://turtles.docs.rancher.io/) installed.
Don't have Rancher yet? Start with [rancher-platform](https://github.com/mbologna/rancher-platform).

### Using Metal3 or Docker examples?

Just point Fleet at this repo:

```bash
kubectl apply -f - <<EOF
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: capi-cluster-templates
  namespace: fleet-local
spec:
  repo: https://github.com/mbologna/rancher-fleet-clusters
  branch: main
  targets:
    - clusterSelector: {}
EOF
```

That's it. Fleet installs the CAPI providers, deploys the ClusterClasses, and the example clusters
begin provisioning. They auto-import into Rancher when ready.

### Using AWS examples (`aws-rke2-example` or `aws-eks-example`)?

Do Step 1 first, then register Fleet.

**Step 1 — AWS credentials**

CAPA will create all the AWS infrastructure for you (VPCs, subnets, EC2 instances, EKS clusters)
from the cluster manifests. It just needs credentials to act on your behalf.

Bootstrap IAM instance profiles *(once per AWS account)*:

```bash
clusterawsadm bootstrap iam create-cloudformation-stack
```

Create the credentials secret on the management cluster:

```bash
kubectl create secret generic cluster-identity-secret \
  --namespace capa-system \
  --from-literal=AccessKeyID=<your-access-key-id> \
  --from-literal=SecretAccessKey=<your-secret-access-key>

kubectl apply -f - <<EOF
apiVersion: infrastructure.cluster.x-k8s.io/v1beta2
kind: AWSClusterStaticIdentity
metadata:
  name: cluster-identity
  namespace: default
spec:
  secretRef: cluster-identity-secret
  allowedNamespaces: {}
EOF
```

**Step 2 — Register this repo in Fleet** *(same command as above)*

```bash
kubectl apply -f - <<EOF
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: capi-cluster-templates
  namespace: fleet-local
spec:
  repo: https://github.com/mbologna/rancher-fleet-clusters
  branch: main
  targets:
    - clusterSelector: {}
EOF
```

That's it. Fleet installs the CAPI providers, deploys the ClusterClasses, and the example clusters
begin provisioning. They auto-import into Rancher when ready.

---

## What's included

```
providers/         CAPA (AWS), CAPRKE2, CAPM3 (Metal3), CAPD (Docker/local)
clusterclasses/    aws-rke2    — reusable RKE2-on-EC2 topology
                   metal3-rke2 — reusable RKE2-on-bare-metal topology
clusters/          aws-rke2-example      — 1 CP + 2 workers on AWS EC2 (RKE2 v1.33.x)
                   aws-eks-example       — EKS managed control plane + node group (k8s v1.31)
                   metal3-rke2-example   — 1 CP + 2 workers on bare metal via Metal3
```

Every cluster carries `cluster-api.cattle.io/rancher-auto-import: "true"` — Turtles imports it
into Rancher automatically when it becomes Ready.

---

## Advanced

### Customising and adding clusters

The `clusters/` examples are your starting point. Fork this repo, modify an example, push —
Fleet reconciles the change automatically.

```bash
cp -r clusters/aws-rke2-example clusters/my-cluster
# edit clusters/my-cluster/cluster.yaml — change name, instance type, region, …
git add clusters/my-cluster && git commit -m "add my-cluster" && git push
```

### ClusterClass reference

#### aws-rke2

| Variable                 | Default    | Description                              |
|--------------------------|------------|------------------------------------------|
| region                   | eu-west-1  | AWS region                               |
| sshKeyName               | (required) | EC2 key pair name                        |
| controlPlaneInstanceType | t3.large   | Instance type for control plane nodes    |
| workerInstanceType       | t3.large   | Instance type for worker nodes           |
| vpcID                    | (empty)    | Existing VPC ID; empty = CAPA creates    |
| amiID                    | (required) | AMI ID for EC2 instances                 |

#### aws-eks

EKS managed cluster — no ClusterClass; control plane managed entirely by AWS.

| Field        | Default    | Description                                 |
|--------------|------------|---------------------------------------------|
| region       | eu-west-1  | AWS region                                  |
| sshKeyName   | (required) | EC2 key pair for node SSH access            |
| version      | v1.31      | EKS-supported Kubernetes version            |
| instanceType | t3.large   | EC2 instance type for managed node group    |
| replicas     | 2          | Node count (scales between minSize–maxSize) |

> **Cost note:** EKS charges ~$0.10/hr for the managed control plane.

#### metal3-rke2

| Variable               | Default    | Description                       |
|------------------------|------------|-----------------------------------|
| imageURL               | (required) | HTTP URL of OS raw image          |
| imageChecksum          | (required) | SHA256 checksum of the image      |
| controlPlaneEndpointIP | (required) | VIP for the Kubernetes API server |

**Metal3 prerequisites**: Ironic running and reachable from the management cluster; DHCP + PXE boot
on the bare-metal network; `BareMetalHost` objects registered in the `default` namespace.

Create the CAPM3 Ironic credentials secret before registering Fleet:

```bash
kubectl create secret generic ironic-credentials \
  --namespace capm3-system \
  --from-literal=IRONIC_URL=http://<ironic-host>:6385/v1/ \
  --from-literal=IRONIC_INSPECTOR_URL=http://<ironic-host>:5050/v1/ \
  --from-literal=IRONIC_USERNAME=<username> \
  --from-literal=IRONIC_PASSWORD=<password>
```

### Version matrix

| Component       | Version |
|-----------------|---------|
| CAPI core       | v1.12.2 |
| CAPA (AWS)      | v2.10.2 |
| CAPRKE2         | v0.24.2 |
| CAPM3 (Metal3)  | v1.12.3 |
| RKE2            | v1.33.x |
| Rancher Turtles | v0.26.0 |

---

## Related

**[rancher-platform](https://github.com/mbologna/rancher-platform)** — Terraform + Ansible to get
a running Rancher instance (the prerequisite for this repo).
