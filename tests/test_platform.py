"""Offline regression tests for platform preflight and schema translation."""

import copy
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

from jsonschema import Draft7Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from schema_validation import kubernetes_schema

spec = importlib.util.spec_from_file_location("check_platform", ROOT / "scripts/check-platform.py")
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.provider = {
            "metadata": {
                "name": "aws", "namespace": "capa-system", "generation": 2,
                "annotations": {"meta.helm.sh/release-name": "rancher-turtles-providers"},
            },
            "spec": {"name": "aws", "type": "infrastructure", "version": "v2.11.1"},
            "status": {
                "installedVersion": "v2.11.1",
                "phase": "Ready",
                "conditions": [{"type": "ProviderInstalled", "status": "True", "observedGeneration": 2}],
            },
        }
        self.expected = [("infrastructure", "aws", "capa-system", "v2.11.1")]

    def test_ready_provider_passes(self):
        self.assertEqual(platform.provider_errors([self.provider], self.expected), [])

    def test_chart_default_namespace_passes(self):
        self.provider["metadata"]["namespace"] = "capd-system"
        self.provider["spec"]["name"] = "docker"
        expected = [("infrastructure", "docker", "cattle-turtles-system", "v2.11.1")]
        self.assertEqual(platform.provider_errors([self.provider], expected), [])

    def test_hook_owned_core_passes(self):
        self.provider["metadata"].update(namespace="cattle-capi-system", annotations={"helm.sh/hook": "post-install"})
        self.provider["spec"].update(name="cluster-api", type="core")
        expected = [("core", "cluster-api", "cattle-capi-system", "v2.11.1")]
        self.assertEqual(platform.provider_errors([self.provider], expected), [])

    def test_duplicate_owners_fail(self):
        duplicate = copy.deepcopy(self.provider)
        duplicate["metadata"]["namespace"] = "another-namespace"
        self.assertIn("found 2", platform.provider_errors([self.provider, duplicate], self.expected)[0])

    def test_uninstalled_provider_fails(self):
        self.provider["status"]["conditions"][0]["status"] = "False"
        self.assertIn("ProviderInstalled is not True", platform.provider_errors([self.provider], self.expected)[0])

    def test_stale_status_fails(self):
        self.provider["status"]["conditions"][0]["observedGeneration"] = 1
        self.assertIn("stale", platform.provider_errors([self.provider], self.expected)[0])

    def test_one_lagging_condition_passes(self):
        self.provider["status"]["conditions"].append({"type": "Ready", "status": "True", "observedGeneration": 1})
        self.assertEqual(platform.provider_errors([self.provider], self.expected), [])

    def test_wrong_version_fails(self):
        self.provider["status"]["installedVersion"] = "v2.10.2"
        self.assertIn("version must be", platform.provider_errors([self.provider], self.expected)[0])

    def test_fleet_owned_provider_fails(self):
        self.provider["metadata"]["annotations"]["meta.helm.sh/release-name"] = "old-fleet-bundle"
        self.assertIn("platform Helm release", platform.provider_errors([self.provider], self.expected)[0])

    def test_schema_properties_are_not_schema_extensions(self):
        schema = kubernetes_schema({
            "type": "object",
            "properties": {
                "type": {"type": "string"},
                "x-kubernetes-int-or-string": {"type": "boolean"},
            },
        })
        Draft7Validator(schema).validate({"type": "string", "x-kubernetes-int-or-string": False})

    def test_nullable_and_integer_or_string(self):
        Draft7Validator(kubernetes_schema({"type": "string", "nullable": True})).validate(None)
        schema = kubernetes_schema({"x-kubernetes-int-or-string": True})
        Draft7Validator(schema).validate("80")
        Draft7Validator(schema).validate(80)
        self.assertFalse(Draft7Validator(schema).is_valid([]))

    def test_unknown_fields_are_rejected_unless_preserved(self):
        schema = {"type": "object", "properties": {"known": {"type": "string"}}}
        self.assertFalse(Draft7Validator(kubernetes_schema(schema)).is_valid({"typo": "value"}))
        schema["x-kubernetes-preserve-unknown-fields"] = True
        self.assertTrue(Draft7Validator(kubernetes_schema(schema)).is_valid({"arbitrary": "value"}))

    def test_renovate_keeps_manual_review(self):
        config = json.loads((ROOT / "renovate.json").read_text())
        self.assertFalse(config["automerge"])
        self.assertTrue(config["ignoreUnstable"])
        self.assertIn("clusterclasses/aws-rke2/aws-ccm.yaml", config["ignorePaths"])

    def test_renovate_discovers_explicit_version_pins(self):
        config = json.loads((ROOT / "renovate.json").read_text())
        matches = []
        for manager in config["customManagers"]:
            for pattern in manager["matchStrings"]:
                expression = re.sub(r"\(\?<(\w+)>", r"(?P<\1>", pattern)
                for path in (
                    ROOT / "platform/turtles-values.yaml",
                    ROOT / "platform/providers-values.yaml",
                    ROOT / "clusters/aws-rke2-example/cluster.yaml",
                    ROOT / "clusters/docker-rke2-example/cluster.yaml",
                    ROOT / "clusterclasses/aws-rke2/aws-ccm-v136.yaml",
                    ROOT / ".github/workflows/validate.yaml",
                ):
                    matches.extend(match.groupdict() for match in re.finditer(expression, path.read_text()))
        dependencies = {match["depName"] for match in matches}
        self.assertTrue({
            "rancher/rancher", "rancher/turtles", "rancher/rke2",
            "kubernetes-sigs/cluster-api", "kubernetes-sigs/cluster-api-provider-aws",
            "rancher/cluster-api-provider-rke2", "kindest/node",
            "registry.k8s.io/provider-aws/cloud-controller-manager",
            "github.com/mikefarah/yq/v4",
        }.issubset(dependencies))
        rke2 = next(match for match in matches if match["depName"] == "rancher/rke2")
        self.assertIn("build", rke2["versioning"])


if __name__ == "__main__":
    unittest.main()
