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
        for directory in ("providers", "clusterclasses", "clusters", "identities", "scripts"):
            shutil.copytree(ROOT / directory, self.root / directory)
        shutil.copyfile(ROOT / "gitrepo.yaml", self.root / "gitrepo.yaml")

    def replace(self, relative_path, old, new):
        path = self.root / relative_path
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def validate(self, expected_error=None):
        result = subprocess.run(
            ["python3", str(self.root / "scripts/validate-repo.py")],
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
        self.replace("gitrepo.yaml", "    - providers\n", "    - .\n")
        self.validate("root GitRepo paths must be exactly")

    def test_required_variables_can_use_class_defaults(self):
        self.replace(
            "clusters/aws-rke2-example/cluster.yaml",
            '      - name: region\n        value: "eu-west-1"\n',
            "",
        )
        self.validate()


if __name__ == "__main__":
    unittest.main()
