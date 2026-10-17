"""Validate repository resources against release-pinned CRD and chart schemas."""

import copy
import io
import json
import re
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from jsonschema import Draft7Validator


def fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read().decode()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Cannot download schema {url}: {error}") from error


def kubernetes_schema(value):
    """Translate Kubernetes nullable/unknown-field extensions for JSON Schema."""
    if isinstance(value, list):
        return [kubernetes_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: kubernetes_schema(item) for key, item in value.items()}
    if result.get("nullable") and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    if result.get("x-kubernetes-int-or-string") is True:
        result.pop("type", None)
        result["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    if result.get("type") == "object" and "properties" in result:
        if result.get("x-kubernetes-preserve-unknown-fields") is not True:
            result.setdefault("additionalProperties", False)
    return result


def validate_schemas(root, resources, platform):
    errors = []
    providers = platform["providers"]["providers"]
    capi = providers["infrastructureDocker"]["version"]
    capa = providers["infrastructureAWS"]["version"]
    rke2 = providers["bootstrapRKE2"]["version"]
    urls = [
        f"https://github.com/kubernetes-sigs/cluster-api/releases/download/{capi}/core-components.yaml",
        f"https://github.com/kubernetes-sigs/cluster-api/releases/download/{capi}/infrastructure-components-development.yaml",
        f"https://github.com/kubernetes-sigs/cluster-api-provider-aws/releases/download/{capa}/infrastructure-components.yaml",
        f"https://github.com/rancher/cluster-api-provider-rke2/releases/download/{rke2}/bootstrap-components.yaml",
        f"https://github.com/rancher/cluster-api-provider-rke2/releases/download/{rke2}/control-plane-components.yaml",
    ]
    schemas = {}
    for url in urls:
        result = subprocess.run(
            ["yq", "-o=json", "-I=0", "."], input=fetch(url),
            text=True, capture_output=True, check=True,
        )
        for line in result.stdout.splitlines():
            document = json.loads(line)
            if not isinstance(document, dict) or document.get("kind") != "CustomResourceDefinition":
                continue
            spec = document["spec"]
            for version in spec["versions"]:
                if version["served"]:
                    schemas[(f"{spec['group']}/{version['name']}", spec["names"]["kind"])] = (
                        kubernetes_schema(version["schema"]["openAPIV3Schema"])
                    )
    kubernetes = json.loads(fetch(
        "https://raw.githubusercontent.com/kubernetes/kubernetes/v1.36.4/api/openapi-spec/swagger.json"
    ))
    for definition in kubernetes["definitions"].values():
        for gvk in definition.get("x-kubernetes-group-version-kind", []):
            group = gvk["group"]
            if group not in ("", "apps", "rbac.authorization.k8s.io"):
                continue
            api_version = f"{group}/{gvk['version']}" if group else gvk["version"]
            schemas[(api_version, gvk["kind"])] = {
                **definition, "definitions": kubernetes["definitions"],
            }
    for path, resource in resources:
        schema = schemas.get((resource["apiVersion"], resource["kind"]))
        group = resource["apiVersion"].split("/")[0]
        if schema is None:
            if group.endswith("cluster.x-k8s.io"):
                errors.append(f"{path.relative_to(root)}: no pinned schema for {resource['kind']}")
            continue
        schema = copy.deepcopy(schema)
        # Kubernetes injects ObjectMeta and TypeMeta separately from CRD schemas.
        schema.setdefault("properties", {}).update({
            "apiVersion": {"type": "string"}, "kind": {"type": "string"},
            "metadata": {"type": "object"},
        })
        for error in Draft7Validator(schema).iter_errors(resource):
            # Template placeholders keep their string type; deployment preflight
            # rejects them before Kubernetes sees these example manifests.
            if error.validator in {"pattern", "enum"} and isinstance(error.instance, str):
                if re.fullmatch(r"REPLACE_WITH_[A-Z_]+", error.instance):
                    continue
            location = "/".join(str(item) for item in error.absolute_path)
            errors.append(f"{path.relative_to(root)} {resource['kind']}/{resource['metadata']['name']} /{location}: {error.message}")
        if resource["kind"] == "Cluster":
            topology = resource.get("spec", {}).get("topology", {})
            class_name = topology.get("classRef", {}).get("name")
            cluster_class = next((
                item for _, item in resources
                if item["kind"] == "ClusterClass" and item["metadata"]["name"] == class_name
            ), None)
            if cluster_class:
                values = {item["name"]: item["value"] for item in topology.get("variables", [])}
                for variable in cluster_class["spec"].get("variables", []):
                    if variable["name"] in values:
                        for error in Draft7Validator(variable["schema"]["openAPIV3Schema"]).iter_errors(values[variable["name"]]):
                            errors.append(f"{path.relative_to(root)} variable {variable['name']}: {error.message}")
    text = (root / "platform/turtles-values.yaml").read_text()
    turtles = re.search(r"# chart-version: (v\d+\.\d+\.\d+)", text)
    if turtles is None:
        errors.append("platform/turtles-values.yaml: missing pinned chart-version")
        return errors
    for chart, name in (("rancher-turtles", "turtles"), ("rancher-turtles-providers", "providers")):
        url = f"https://raw.githubusercontent.com/rancher/turtles/{turtles[1]}/charts/{chart}/values.schema.json"
        for error in Draft7Validator(json.loads(fetch(url))).iter_errors(drop_nulls(platform[name])):
            errors.append(f"platform/{name}-values.yaml: {error.message}")
    if not errors:
        errors.extend(validate_charts(root, turtles[1]))
    return errors


def drop_nulls(value):
    """Helm deletes keys set to null before it validates the merged values."""
    if isinstance(value, dict):
        return {key: drop_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [drop_nulls(item) for item in value]
    return value


def validate_charts(root, version):
    """Render both platform charts without contacting Kubernetes."""
    url = f"https://api.github.com/repos/rancher/turtles/tarball/{version}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"Cannot download chart source {url}: {error}") from error
    errors = []
    crd = subprocess.run(
        ["yq", "-o=json", "-I=0", "."],
        input=fetch(f"https://raw.githubusercontent.com/rancher/turtles/{version}/config/crd/bases/turtles-capi.cattle.io_capiproviders.yaml"),
        text=True, capture_output=True, check=True,
    )
    provider_schema = kubernetes_schema(json.loads(crd.stdout)["spec"]["versions"][0]["schema"]["openAPIV3Schema"])
    provider_schema["properties"].update({
        "apiVersion": {"type": "string"}, "kind": {"type": "string"}, "metadata": {"type": "object"},
    })
    with tempfile.TemporaryDirectory(prefix="turtles-chart-check-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            members = [member for member in source.getmembers() if "/charts/" in member.name]
            source.extractall(directory, members=members, filter="data")
        charts = next(Path(directory).glob("*/charts"))
        for chart, name in (("rancher-turtles", "turtles"), ("rancher-turtles-providers", "providers")):
            result = subprocess.run(
                ["helm", "template", chart, str(charts / chart), "--namespace", "cattle-turtles-system",
                 "--kube-version", "1.36.4", "-f", str(root / "platform" / f"{name}-values.yaml")],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                errors.append(f"{chart}: Helm render failed: {result.stderr}")
                continue
            documents = subprocess.run(
                ["yq", "-o=json", "-I=0", "."], input=result.stdout,
                text=True, capture_output=True, check=True,
            )
            providers = [
                item for line in documents.stdout.splitlines()
                if isinstance(item := json.loads(line), dict) and item.get("kind") == "CAPIProvider"
            ]
            expected_count = 1 if name == "turtles" else 4
            if len(providers) != expected_count:
                errors.append(f"{chart}: expected {expected_count} rendered providers, found {len(providers)}")
            for provider in providers:
                for error in Draft7Validator(provider_schema).iter_errors(provider):
                    errors.append(f"{chart} CAPIProvider/{provider['metadata']['name']}: {error.message}")
                spec = provider["spec"]
                if provider["apiVersion"] != "turtles-capi.cattle.io/v1alpha1":
                    errors.append(f"{chart}: unexpected CAPIProvider API")
                if not spec.get("version") or spec.get("enableAutomaticUpdate", False):
                    errors.append(f"{chart}: rendered provider must use a fixed version")
    return errors
