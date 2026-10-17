#!/usr/bin/env python3
"""Validate YAML resources and cross-file references in this repository."""

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
YQ = shutil.which("yq")
EXPECTED_GITREPO_PATHS = ["providers", "clusterclasses", "identities", "clusters"]
errors = []


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
    if ".git" not in path.parts
)
resources = []
bundle_configs = {}

for path in yaml_files:
    documents = parse_yaml(path)
    if path.name == "fleet.yaml":
        if len(documents) != 1 or not isinstance(documents[0], dict):
            fail(f"{path.relative_to(ROOT)}: expected one Fleet configuration document")
        else:
            bundle_configs[path.parent] = documents[0]
        continue
    if ".github" in path.parts:
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
    if configured_paths != EXPECTED_GITREPO_PATHS:
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

if errors:
    print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
    sys.exit(1)

print(
    f"Validated {len(yaml_files)} YAML files, {len(resources)} resources, "
    f"{len(bundles)} Fleet bundles, and their references."
)
