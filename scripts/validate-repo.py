#!/usr/bin/env python3
"""Validate YAML resources and cross-file references in this repository."""

import argparse
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YQ = shutil.which("yq")
EXPECTED_GITREPO_PATHS = ["clusterclasses", "identities", "clusters"]
errors = []
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--schemas", action="store_true", help="Download and validate pinned release schemas")
parser.add_argument("--deployment-check", action="store_true", help="Reject unfinished example values")
args = parser.parse_args()


def fail(message):
    errors.append(message)


def parse_yaml(path, content=None):
    command = [YQ, "-o=json", "-I=0", "."]
    if content is None:
        command.append(str(path))
    else:
        command.append("-")
    result = subprocess.run(
        command,
        input=content,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(f"{path}: invalid YAML: {result.stderr.strip()}")
        return []
    try:
        return [json.loads(line) for line in result.stdout.splitlines() if line]
    except json.JSONDecodeError as error:
        fail(f"{path}: yq returned invalid JSON: {error}")
        return []


if YQ is None:
    sys.exit("yq v4 is required; install https://github.com/mikefarah/yq")

yaml_files = sorted(
    path
    for pattern in ("*.yaml", "*.yml")
    for path in ROOT.rglob(pattern)
    if not any(part.startswith(".") and part != ".github" for part in path.relative_to(ROOT).parts)
)
resources = []
embedded_resources = []
bundle_configs = {}

for path in yaml_files:
    documents = parse_yaml(path)
    if ".github" in path.parts:
        continue
    if path.name == "fleet.yaml":
        if len(documents) != 1 or not isinstance(documents[0], dict):
            fail(f"{path.relative_to(ROOT)}: expected one Fleet configuration document")
        else:
            bundle_configs[path.parent] = documents[0]
        continue
    if path.parent == ROOT / "platform" and path.name.endswith("-values.yaml"):
        continue
    if not documents:
        fail(f"{path.relative_to(ROOT)}: expected at least one YAML document")
    for number, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            fail(f"{path.relative_to(ROOT)} document {number}: expected a resource mapping")
            continue
        metadata = document.get("metadata") or {}
        if not document.get("apiVersion") or not document.get("kind") or not metadata.get("name"):
            fail(
                f"{path.relative_to(ROOT)} document {number}: "
                "resource must define apiVersion, kind, and metadata.name"
            )
            continue
        resources.append((path, document))

gitrepos = [document for _, document in resources if document.get("kind") == "GitRepo"]
if len(gitrepos) != 1:
    fail(f"expected one root GitRepo, found {len(gitrepos)}")
gitrepo_prefix = (
    (gitrepos[0].get("metadata") or {}).get("name") if len(gitrepos) == 1 else None
)
if not gitrepo_prefix:
    fail("root GitRepo must define metadata.name")
if len(gitrepos) == 1:
    configured_paths = (gitrepos[0].get("spec") or {}).get("paths", [])
    if len(configured_paths) != len(EXPECTED_GITREPO_PATHS) or set(configured_paths) != set(EXPECTED_GITREPO_PATHS):
        fail(
            "root GitRepo paths must be exactly "
            f"{EXPECTED_GITREPO_PATHS}, got {configured_paths}"
        )
    for configured_path in configured_paths:
        if not (ROOT / configured_path).is_dir():
            fail(f"root GitRepo path does not exist: {configured_path}")

resource_index = {}
for path, resource in resources:
    metadata = resource["metadata"]
    identity = (
        resource["apiVersion"],
        resource["kind"],
        metadata["name"],
        metadata.get("namespace", "default"),
    )
    if identity in resource_index:
        fail(
            f"{path.relative_to(ROOT)}: duplicate resource "
            f"{identity[1]}/{identity[2]} in namespace {identity[3]}"
        )
    else:
        resource_index[identity] = resource

bundles = {}
for directory, config in bundle_configs.items():
    relative = directory.relative_to(ROOT).as_posix().replace("/", "-")
    name = f"{gitrepo_prefix}-{relative}"
    bundles[name] = config

dependency_graph = {}
for name, config in bundles.items():
    dependencies = []
    for dependency in config.get("dependsOn", []) or []:
        dependency_name = dependency.get("name") if isinstance(dependency, dict) else None
        if not dependency_name:
            fail(f"{name}: each dependsOn entry must have a name")
        elif dependency_name not in bundles:
            fail(f"{name}: depends on missing Fleet bundle {dependency_name}")
        else:
            dependencies.append(dependency_name)
    dependency_graph[name] = dependencies

visiting = set()
visited = set()


def visit_bundle(name):
    if name in visiting:
        fail(f"Fleet bundle dependency cycle includes {name}")
        return
    if name in visited:
        return
    visiting.add(name)
    for dependency in dependency_graph.get(name, []):
        visit_bundle(dependency)
    visiting.remove(name)
    visited.add(name)


for bundle_name in dependency_graph:
    visit_bundle(bundle_name)


def resource_exists(reference, namespace="default"):
    if "apiGroup" in reference:
        return any(
            api_version.split("/")[0] == reference["apiGroup"]
            and kind == reference.get("kind")
            and name == reference.get("name")
            and resource_namespace == namespace
            for api_version, kind, name, resource_namespace in resource_index
        )
    return (
        reference.get("apiVersion"),
        reference.get("kind"),
        reference.get("name"),
        reference.get("namespace", namespace),
    ) in resource_index


for path, resource in resources:
    kind = resource["kind"]
    api_version = resource["apiVersion"]
    metadata = resource["metadata"]
    spec = resource.get("spec") or {}
    namespace = metadata.get("namespace", "default")
    if args.deployment_check and path.is_relative_to(ROOT / "clusters"):
        if "REPLACE_WITH_" in json.dumps(resource):
            fail(f"{path.relative_to(ROOT)}: unresolved REPLACE_WITH_ deployment value")

    if kind in {"Cluster", "ClusterClass", "MachinePool"} and api_version.startswith(
        "cluster.x-k8s.io/"
    ):
        if api_version != "cluster.x-k8s.io/v1beta2":
            fail(
                f"{path.relative_to(ROOT)}: {kind} must use "
                "cluster.x-k8s.io/v1beta2"
            )

    if kind == "ClusterClass":
        refs = [
            spec.get("infrastructure", {}).get("templateRef"),
            spec.get("controlPlane", {}).get("templateRef"),
            spec.get("controlPlane", {}).get("machineInfrastructure", {}).get("templateRef"),
        ]
        for deployment in spec.get("workers", {}).get("machineDeployments", []) or []:
            refs.extend(
                [
                    deployment.get("bootstrap", {}).get("templateRef"),
                    deployment.get("infrastructure", {}).get("templateRef"),
                ]
            )
        for reference in filter(None, refs):
            if not resource_exists(reference, namespace):
                fail(
                    f"{path.relative_to(ROOT)}: ClusterClass references missing "
                    f"{reference.get('kind')}/{reference.get('name')}"
                )
        variable_names = {item["name"] for item in spec.get("variables", [])}
        for patch in spec.get("patches", []):
            for definition in patch.get("definitions", []):
                selector = definition["selector"]
                templates = [
                    item for item in filter(None, refs)
                    if item["kind"] == selector["kind"]
                    and item["apiVersion"] == selector["apiVersion"]
                ]
                if not templates:
                    fail(f"{path.relative_to(ROOT)}: patch {patch['name']} matches no templates")
                for operation in definition.get("jsonPatches", []):
                    indices = [int(part) for part in operation["path"].split("/") if part.isdigit()]
                    # Mirrors CAPI's validateIndexAccess webhook rule.
                    if indices and (operation["op"] in ("replace", "remove") or any(indices)):
                        fail(
                            f"{path.relative_to(ROOT)}: patch path {operation['path']} uses an "
                            f"array index CAPI rejects for {operation['op']}"
                        )
                    variable = operation.get("valueFrom", {}).get("variable")
                    if variable and variable.split(".")[0] not in variable_names | {"builtin"}:
                        fail(f"{path.relative_to(ROOT)}: patch references unknown variable {variable}")
                    for template in templates:
                        if not resource_exists(template, namespace):
                            continue
                        target = resource_index[
                            (template["apiVersion"], template["kind"], template["name"], namespace)
                        ]
                        parts = operation["path"].split("/")[1:]
                        if operation["op"] == "add":
                            parts = parts[:-1]
                        try:
                            for part in parts:
                                part = part.replace("~1", "/").replace("~0", "~")
                                target = target[int(part)] if isinstance(target, list) else target[part]
                        except (KeyError, IndexError, ValueError, TypeError):
                            fail(f"{path.relative_to(ROOT)}: invalid patch path {operation['path']}")

    if kind == "Cluster":
        topology = spec.get("topology") or {}
        class_ref = topology.get("classRef")
        if topology and not class_ref:
            fail(f"{path.relative_to(ROOT)}: topology must use classRef")
        if class_ref and (
            "cluster.x-k8s.io/v1beta2",
            "ClusterClass",
            class_ref.get("name"),
            namespace,
        ) not in resource_index:
            fail(
                f"{path.relative_to(ROOT)}: topology references missing "
                f"ClusterClass/{class_ref.get('name')}"
            )
        elif class_ref:
            cluster_class = resource_index[
                (
                    "cluster.x-k8s.io/v1beta2",
                    "ClusterClass",
                    class_ref["name"],
                    namespace,
                )
            ]
            required_variables = {
                variable["name"]
                for variable in cluster_class.get("spec", {}).get("variables", []) or []
                if variable.get("required")
                and "default" not in variable.get("schema", {}).get("openAPIV3Schema", {})
            }
            cluster_variables = {
                variable.get("name")
                for variable in topology.get("variables", []) or []
            }
            missing_variables = required_variables - cluster_variables
            if missing_variables:
                fail(
                    f"{path.relative_to(ROOT)}: topology is missing required "
                    f"ClusterClass variables: {', '.join(sorted(missing_variables))}"
                )
            values = {
                item["name"]: item.get("schema", {}).get("openAPIV3Schema", {}).get("default")
                for item in cluster_class.get("spec", {}).get("variables", [])
            }
            values.update({item["name"]: item.get("value") for item in topology.get("variables", [])})
            if values.get("vpcID") and not values.get("subnetID"):
                fail(f"{path.relative_to(ROOT)}: vpcID requires subnetID")
            if values.get("subnetID") and not values.get("vpcID"):
                fail(f"{path.relative_to(ROOT)}: subnetID requires vpcID")
            for name in ("apiServerAllowedCIDRs", "nodeCIDRs"):
                for value in values.get(name) or []:
                    if isinstance(value, str) and value.startswith("REPLACE_WITH_") and not args.deployment_check:
                        continue
                    try:
                        ipaddress.ip_network(value, strict=True)
                    except (ValueError, TypeError):
                        fail(f"{path.relative_to(ROOT)}: {name} contains invalid CIDR {value}")
            networks = []
            for field in ("pods", "services"):
                for value in spec.get("clusterNetwork", {}).get(field, {}).get("cidrBlocks", []):
                    try:
                        network = ipaddress.ip_network(value, strict=True)
                    except ValueError:
                        fail(f"{path.relative_to(ROOT)}: invalid {field} CIDR {value}")
                        continue
                    for other in networks:
                        if network.overlaps(other):
                            fail(f"{path.relative_to(ROOT)}: overlapping pod/service networks")
                    networks.append(network)
            if class_ref["name"].endswith("-v136"):
                if not re.fullmatch(r"v1\.36\.\d+\+rke2r\d+", topology.get("version", "")):
                    fail(f"{path.relative_to(ROOT)}: v136 class requires RKE2 v1.36")
                if metadata.get("labels", {}).get("infrastructure") == "aws":
                    labels = metadata.get("labels", {})
                    if labels.get("ccm") != "aws-v136" or labels.get("ccm-version") != "1.36":
                        fail(f"{path.relative_to(ROOT)}: v1.36 AWS cluster requires matching CCM labels")
        for field in ("controlPlaneRef", "infrastructureRef"):
            reference = spec.get(field)
            if reference and ("apiGroup" not in reference or "apiVersion" in reference):
                fail(f"{path.relative_to(ROOT)}: {field} must use apiGroup, not apiVersion")
            if reference and not resource_exists(reference, namespace):
                fail(
                    f"{path.relative_to(ROOT)}: {field} references missing "
                    f"{reference.get('kind')}/{reference.get('name')}"
                )

    if kind == "MachinePool":
        reference = (
            spec.get("template", {}).get("spec", {}).get("infrastructureRef")
        )
        if reference and ("apiGroup" not in reference or "apiVersion" in reference):
            fail(
                f"{path.relative_to(ROOT)}: MachinePool infrastructureRef must use "
                "apiGroup, not apiVersion"
            )
        if reference and not resource_exists(reference, namespace):
            fail(
                f"{path.relative_to(ROOT)}: infrastructureRef references missing "
                f"{reference.get('kind')}/{reference.get('name')}"
            )

    if kind in {"AWSManagedControlPlane", "AWSClusterTemplate"}:
        identity_ref = (
            spec.get("identityRef")
            if kind == "AWSManagedControlPlane"
            else spec.get("template", {}).get("spec", {}).get("identityRef")
        )
        if identity_ref and not any(
            item.get("kind") == identity_ref.get("kind")
            and item.get("metadata", {}).get("name") == identity_ref.get("name")
            for _, item in resources
        ):
            fail(
                f"{path.relative_to(ROOT)}: identityRef references missing "
                f"{identity_ref.get('kind')}/{identity_ref.get('name')}"
            )

    if kind == "AWSClusterStaticIdentity" and "namespace" in metadata:
        fail(f"{path.relative_to(ROOT)}: AWSClusterStaticIdentity is cluster-scoped")
    if kind == "AWSClusterStaticIdentity" and spec.get("allowedNamespaces") != {"list": ["default"]}:
        fail(f"{path.relative_to(ROOT)}: AWS identity must be restricted to namespace default")

    if kind == "ClusterResourceSet":
        for reference in spec.get("resources", []):
            if not resource_exists({"apiVersion": "v1", **reference}, namespace):
                fail(f"{path.relative_to(ROOT)}: missing ClusterResourceSet payload {reference['name']}")
        if metadata["name"] == "aws-ccm-v136":
            if spec.get("strategy") != "Reconcile" or spec.get("clusterSelector", {}).get("matchLabels") != {
                "ccm": "aws-v136", "ccm-version": "1.36"
            }:
                fail(f"{path.relative_to(ROOT)}: v136 CCM must reconcile only matching v1.36 clusters")

    if path.name == "clusterclass-v136.yaml":
        if "get.rke2.io" in json.dumps(resource) or "/etc/hosts" in json.dumps(resource):
            fail(f"{path.relative_to(ROOT)}: versioned classes must use native bootstrap without subnet scans")
        if metadata.get("annotations", {}).get("objectset.rio.cattle.io/ignore"):
            fail(f"{path.relative_to(ROOT)}: versioned templates must not hide spec drift")

    if kind == "ConfigMap":
        for key, value in (resource.get("data") or {}).items():
            if key.endswith((".yaml", ".yml")) and isinstance(value, str):
                for number, embedded in enumerate(parse_yaml(path, value), start=1):
                    embedded_metadata = (
                        embedded.get("metadata") or {}
                        if isinstance(embedded, dict)
                        else {}
                    )
                    if not isinstance(embedded, dict) or not embedded.get(
                        "apiVersion"
                    ) or not embedded.get("kind") or not embedded_metadata.get(
                        "name"
                    ):
                        fail(
                            f"{path.relative_to(ROOT)} ConfigMap/{metadata['name']} "
                            f"data.{key} document {number}: missing apiVersion, kind, "
                            "or metadata.name"
                        )
                    else:
                        embedded_resources.append((path, embedded))

platform_values = {}
for name in ("turtles", "providers"):
    path = ROOT / "platform" / f"{name}-values.yaml"
    documents = parse_yaml(path)
    if len(documents) != 1 or not isinstance(documents[0], dict):
        fail(f"platform/{name}-values.yaml: expected one values mapping")
    else:
        platform_values[name] = documents[0]

expected_providers = {
    "bootstrapRKE2", "controlplaneRKE2", "infrastructureAWS", "infrastructureDocker"
}
providers = platform_values.get("providers", {}).get("providers", {})
for name in expected_providers:
    config = providers.get(name, {})
    if config.get("enabled") is not True or config.get("enableAutomaticUpdate") is not False:
        fail(f"platform: {name} must be enabled with automatic updates disabled")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", config.get("version", "")):
        fail(f"platform: {name} requires an explicit stable release")
if providers.get("bootstrapRKE2", {}).get("version") != providers.get("controlplaneRKE2", {}).get("version"):
    fail("platform: RKE2 bootstrap and control-plane versions differ")
core = platform_values.get("turtles", {}).get("cluster-api-operator", {}).get("cluster-api", {}).get("core", {})
if core.get("enableAutomaticUpdate") is not False or not re.fullmatch(r"v\d+\.\d+\.\d+", core.get("version", "")):
    fail("platform: core CAPI requires a pinned stable version and automatic updates disabled")
if core.get("version") != providers.get("infrastructureDocker", {}).get("version"):
    fail("platform: core CAPI and CAPD versions differ")
core_settings = platform_values.get("turtles", {}).get("cluster-api-operator", {}).get("cluster-api", {})
core_secret = next((
    resource for path, resource in resources
    if path == ROOT / "platform/core-config.yaml" and resource["kind"] == "Secret"
), {})
if (
    core_secret.get("metadata", {}).get("name") != core_settings.get("configSecret", {}).get("name")
    or core_secret.get("metadata", {}).get("namespace") != core.get("namespace")
    or core_secret.get("stringData", {}).get("CLUSTER_TOPOLOGY") != "true"
):
    fail("platform: core config Secret must enable ClusterTopology and match chart values")
if any(resource["kind"] == "CAPIProvider" for _, resource in resources):
    fail("Fleet must not own CAPIProvider resources; configure the platform chart")

if args.schemas and not errors:
    from schema_validation import validate_schemas

    errors.extend(validate_schemas(ROOT, resources + embedded_resources, platform_values))

if errors:
    print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
    sys.exit(1)

print(
    f"Validated {len(yaml_files)} YAML files, {len(resources)} resources, "
    f"{len(bundles)} Fleet bundles, and their references."
)
