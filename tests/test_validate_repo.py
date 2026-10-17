"""Regression tests for the repository's manifest checks."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ValidateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        for directory in ("platform", "clusterclasses", "clusters", "identities", "scripts"):
            shutil.copytree(ROOT / directory, self.root / directory)
        shutil.copyfile(ROOT / "gitrepo.yaml", self.root / "gitrepo.yaml")

    def replace(self, relative_path, old, new):
        path = self.root / relative_path
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def validate(self, expected_error=None, *arguments):
        result = subprocess.run(
            ["python3", str(self.root / "scripts/validate-repo.py"), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        if expected_error is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected_error, result.stderr)

    def test_repository_is_valid(self):
        self.validate()

    def test_indexed_replace_patch_is_reported(self):
        self.replace(
            "clusterclasses/aws-rke2/clusterclass.yaml",
            "path: /spec/template/spec/controlPlaneLoadBalancer/ingressRules\n",
            "path: /spec/template/spec/controlPlaneLoadBalancer/ingressRules/1\n",
        )
        self.validate("uses an array index CAPI rejects for replace")

    def test_eks_rejects_old_reference_fields(self):
        self.replace(
            "clusters/aws-eks-example/cluster.yaml",
            "apiGroup: infrastructure.cluster.x-k8s.io",
            "apiVersion: infrastructure.cluster.x-k8s.io/v1beta2",
        )
        self.validate("must use apiGroup, not apiVersion")

    def test_identity_rejects_namespace(self):
        self.replace(
            "identities/aws/aws-cluster-identity.yaml",
            "  name: cluster-identity\n",
            "  name: cluster-identity\n  namespace: default\n",
        )
        self.validate("AWSClusterStaticIdentity is cluster-scoped")

    def test_missing_dependency_is_reported(self):
        self.replace(
            "clusters/aws-eks-example/fleet.yaml",
            "capi-cluster-templates-identities-aws",
            "capi-cluster-templates-missing",
        )
        self.validate("depends on missing Fleet bundle")

    def test_malformed_embedded_ccm_is_reported(self):
        self.replace(
            "clusterclasses/aws-rke2/aws-ccm.yaml",
            "    kind: ClusterRoleBinding\n",
            "",
        )
        self.validate("missing apiVersion, kind, or metadata.name")

    def test_bootstrap_cannot_be_bundled(self):
        self.replace("gitrepo.yaml", "    - clusterclasses\n", "    - .\n")
        self.validate("root GitRepo paths must be exactly")

    def test_required_variables_can_use_class_defaults(self):
        self.replace(
            "clusters/aws-rke2-example/cluster.yaml",
            '      - name: region\n        value: "eu-west-1"\n',
            "",
        )
        self.validate()

    def test_platform_directory_cannot_be_bundled(self):
        self.replace("gitrepo.yaml", "    - identities\n", "    - platform\n")
        self.validate("root GitRepo paths must be exactly")

    def test_deployment_requires_real_values(self):
        self.validate("unresolved REPLACE_WITH_", "--deployment-check")

    def test_configured_examples_pass_deployment_check(self):
        for path in (self.root / "clusters").glob("*/cluster.yaml"):
            text = path.read_text()
            replacements = {
                "REPLACE_WITH_KEY_PAIR_NAME": "example-key",
                "REPLACE_WITH_SUPPORTED_EKS_VERSION": "v1.35",
                "REPLACE_WITH_VPC_ID": "vpc-0123456789abcdef0",
                "REPLACE_WITH_SUBNET_ID_A": "subnet-0123456789abcdef0",
                "REPLACE_WITH_SUBNET_ID_B": "subnet-0123456789abcdef1",
                "REPLACE_WITH_AMI_ID": "ami-0123456789abcdef0",
                "REPLACE_WITH_TRUSTED_CLIENT_CIDR": "192.0.2.0/24",
            }
            for placeholder, value in replacements.items():
                text = text.replace(placeholder, value)
            path.write_text(text)
        self.validate(None, "--deployment-check")

    def test_invalid_cidr_is_reported(self):
        self.replace(
            "clusters/aws-rke2-example/cluster.yaml",
            "REPLACE_WITH_TRUSTED_CLIENT_CIDR", "not-a-network",
        )
        self.validate("invalid CIDR")

    def test_overlapping_networks_are_reported(self):
        self.replace("clusters/docker-rke2-example/cluster.yaml", "10.11.0.0/16", "10.10.0.0/24")
        self.validate("overlapping pod/service networks")

    def test_v136_rejects_old_workload_minor(self):
        self.replace("clusters/aws-rke2-example/cluster.yaml", "v1.36.4+rke2r1", "v1.33.10+rke2r3")
        self.validate("v136 class requires RKE2 v1.36")

    def test_ccm_labels_must_match_workload_minor(self):
        self.replace("clusters/aws-rke2-example/cluster.yaml", "ccm: aws-v136", "ccm: aws")
        self.validate("requires matching CCM labels")

    def test_versioned_classes_reject_manual_install(self):
        self.replace(
            "clusterclasses/aws-rke2/clusterclass-v136.yaml",
            "hostnamectl set-hostname", "curl https://get.rke2.io; hostnamectl set-hostname",
        )
        self.validate("must use native bootstrap")

    def test_identity_must_limit_namespaces(self):
        self.replace(
            "identities/aws/aws-cluster-identity.yaml",
            "  allowedNamespaces:\n    list:\n      - default", "  allowedNamespaces: {}",
        )
        self.validate("must be restricted")

    def test_provider_versions_must_be_stable(self):
        self.replace("platform/providers-values.yaml", "v0.25.2", "v0.28.0-rc.1")
        self.validate("requires an explicit stable release")

    def test_automatic_provider_updates_are_rejected(self):
        self.replace("platform/providers-values.yaml", "enableAutomaticUpdate: false", "enableAutomaticUpdate: true")
        self.validate("automatic updates disabled")

    def test_bad_patch_path_is_reported(self):
        self.replace(
            "clusterclasses/aws-rke2/clusterclass-v136.yaml",
            "/spec/template/spec/instanceType", "/spec/template/spec/typo",
        )
        self.validate("invalid patch path")

    def test_unused_provider_bundle_is_not_required(self):
        self.assertFalse((self.root / "providers").exists())
        self.validate()


if __name__ == "__main__":
    unittest.main()
