#!/usr/bin/env python3
"""Read-only deployment preflight. Requires an explicit management kubecontext."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def yaml_documents(path):
    result = subprocess.run(
        ["yq", "-o=json", "-I=0", ".", str(path)],
        text=True, capture_output=True, check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def provider_errors(items, expected):
    errors = []
    for provider_type, name, namespace, version in expected:
        matches = [
            item for item in items
            if item.get("spec", {}).get("type") == provider_type
            and item.get("spec", {}).get("name", item["metadata"]["name"]) == name
        ]
        if len(matches) != 1:
            errors.append(f"{provider_type}/{name}: expected one owner, found {len(matches)}")
            continue
        provider = matches[0]
        release = "rancher-turtles" if provider_type == "core" else "rancher-turtles-providers"
        if provider["metadata"].get("annotations", {}).get("meta.helm.sh/release-name") != release:
            errors.append(f"{provider_type}/{name}: expected platform Helm release {release}")
        if provider["metadata"]["namespace"] != namespace:
            errors.append(f"{provider_type}/{name}: expected namespace {namespace}")
        if provider["spec"].get("version") != version or provider["spec"].get("enableAutomaticUpdate", False):
            errors.append(f"{provider_type}/{name}: spec must pin {version} without automatic updates")
        status = provider.get("status", {})
        if status.get("installedVersion") != version:
            errors.append(f"{provider_type}/{name}: installed version must be {version}")
        conditions = status.get("conditions", [])
        if status.get("phase") != "Ready" or not any(item["type"] == "ProviderInstalled" and item["status"] == "True" for item in conditions):
            errors.append(f"{provider_type}/{name}: ProviderInstalled is not True")
        if any(item.get("observedGeneration", provider["metadata"].get("generation")) != provider["metadata"].get("generation") for item in conditions if item["type"] == "ProviderInstalled"):
            errors.append(f"{provider_type}/{name}: provider status is stale")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Management kubecontext; no default")
    args = parser.parse_args()
    errors = []

    def kubectl(*arguments):
        result = subprocess.run(
            ["kubectl", "--context", args.context, "--request-timeout=30s", *arguments, "-o", "json"],
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    subprocess.run([sys.executable, str(ROOT / "scripts/validate-repo.py"), "--deployment-check"], check=True)
    values = yaml_documents(ROOT / "platform/providers-values.yaml")[0]["providers"]
    core = yaml_documents(ROOT / "platform/turtles-values.yaml")[0]["cluster-api-operator"]["cluster-api"]["core"]
    expected = [("core", "cluster-api", core["namespace"], core["version"])]
    for key, provider_type, name in (
        ("bootstrapRKE2", "bootstrap", "rke2"),
        ("controlplaneRKE2", "controlPlane", "rke2"),
        ("infrastructureAWS", "infrastructure", "aws"),
        ("infrastructureDocker", "infrastructure", "docker"),
    ):
        expected.append((provider_type, name, values[key]["namespace"], values[key]["version"]))
    errors.extend(provider_errors(kubectl("get", "capiproviders.turtles-capi.cattle.io", "-A")["items"], expected))
    server = kubectl("version")["serverVersion"]
    minor = int(re.match(r"\d+", server["minor"])[0])
    if server["major"] != "1" or minor not in (34, 35, 36):
        errors.append("management Kubernetes must be >=1.34 and <1.37")
    deployments = kubectl("get", "deployments", "-A")["items"]
    for name in ("capi-controller-manager", "capa-controller-manager", "capd-controller-manager",
                 "rke2-bootstrap-controller-manager", "rke2-control-plane-controller-manager",
                 "rancher-turtles-controller-manager", "rancher"):
        matches = [item for item in deployments if item["metadata"]["name"] == name]
        if len(matches) != 1:
            errors.append(f"Deployment/{name}: expected one controller, found {len(matches)}")
            continue
        deployment = matches[0]
        if deployment.get("status", {}).get("observedGeneration", 0) < deployment["metadata"]["generation"]:
            errors.append(f"Deployment/{name}: status is stale")
        if deployment.get("status", {}).get("availableReplicas", 0) < 1:
            errors.append(f"Deployment/{name}: no available replicas")
        if name in ("rancher", "rancher-turtles-controller-manager"):
            label = "rancher" if name == "rancher" else "chart"
            text = (ROOT / "platform/turtles-values.yaml").read_text()
            version = re.search(rf"# {label}-version: (v\d+\.\d+\.\d+)", text)[1]
            images = [
                item["image"].split("@")[0]
                for item in deployment["spec"]["template"]["spec"]["containers"]
            ]
            if not any(image.endswith(f":{version}") for image in images):
                errors.append(f"Deployment/{name}: expected image version {version}")
        if name == "capi-controller-manager":
            arguments = " ".join(
                arg for container in deployment["spec"]["template"]["spec"]["containers"]
                for arg in container.get("args", [])
            )
            if "ClusterTopology=true" not in arguments:
                errors.append("core CAPI must enable ClusterTopology")
            if "MachinePool=false" in arguments:
                errors.append("core CAPI must enable MachinePool")
        if name == "capa-controller-manager":
            arguments = " ".join(
                arg for container in deployment["spec"]["template"]["spec"]["containers"]
                for arg in container.get("args", [])
            )
            if "MachinePool=true" not in arguments or "EKS=false" in arguments:
                errors.append("CAPA must enable MachinePool and EKS")
    required = {
        "clusters.cluster.x-k8s.io", "clusterclasses.cluster.x-k8s.io",
        "machinepools.cluster.x-k8s.io", "clusterresourcesets.addons.cluster.x-k8s.io",
        "awsclustertemplates.infrastructure.cluster.x-k8s.io",
        "awsmachinetemplates.infrastructure.cluster.x-k8s.io",
        "awsclusterstaticidentities.infrastructure.cluster.x-k8s.io",
        "awsmanagedcontrolplanes.controlplane.cluster.x-k8s.io",
        "awsmanagedmachinepools.infrastructure.cluster.x-k8s.io",
        "rke2controlplanetemplates.controlplane.cluster.x-k8s.io",
        "rke2configtemplates.bootstrap.cluster.x-k8s.io",
        "dockermachinetemplates.infrastructure.cluster.x-k8s.io",
        "dockerclustertemplates.infrastructure.cluster.x-k8s.io",
    }
    for crd in kubectl("get", "crds")["items"]:
        if crd["metadata"]["name"] not in required:
            continue
        required.remove(crd["metadata"]["name"])
        if not any(version["name"] == "v1beta2" and version["served"] for version in crd["spec"]["versions"]):
            errors.append(f"{crd['metadata']['name']}: v1beta2 must be served")
        if not any(item["type"] == "Established" and item["status"] == "True" for item in crd.get("status", {}).get("conditions", [])):
            errors.append(f"{crd['metadata']['name']}: CRD is not Established")
    if required:
        errors.append(f"Missing CRDs: {', '.join(sorted(required))}")
    for name, keys in (
        ("cluster-identity", {"AccessKeyID", "SecretAccessKey"}),
        ("capa-credentials", {"AWS_B64ENCODED_CREDENTIALS"}),
    ):
        secret = kubectl("get", "secret", name, "-n", values["infrastructureAWS"]["namespace"])
        if not all(secret.get("data", {}).get(key) for key in keys):
            errors.append(f"Secret/{name}: missing required credential keys")
    local_names = {
        (item["metadata"].get("namespace", "default"), item["metadata"]["name"])
        for path in (ROOT / "clusters").glob("*/cluster.yaml")
        for item in yaml_documents(path) if item["kind"] == "Cluster"
    }
    for cluster in kubectl("get", "clusters.cluster.x-k8s.io", "-A")["items"]:
        key = (cluster["metadata"]["namespace"], cluster["metadata"]["name"])
        if key in local_names:
            errors.append(f"Cluster/{key[0]}/{key[1]} already exists: review migration; do not apply fresh-install defaults")
    if errors:
        sys.exit("\n".join(f"ERROR: {error}" for error in errors))
    print("Platform providers, controllers, CRDs and fresh-install cluster names passed preflight.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        sys.exit(f"Preflight command failed: {error.cmd}\n{error.stderr or ''}")
