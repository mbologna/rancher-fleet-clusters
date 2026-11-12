"""Offline tests for scripts/sync-providers.py."""

import contextlib
import importlib.util
import io
import re
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("sync_providers", ROOT / "scripts/sync-providers.py")
sync_providers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync_providers)

CATALOGUE = """apiVersion: v1
kind: ConfigMap
data:
  clusterctl.yaml: |
    providers:
    # Cluster API core provider
    - name:         "cluster-api"
      url:          "https://github.com/rancher/cluster-api/releases/{core}/core-components.yaml"
      type:         "CoreProvider"
    - name:         "aws"
      url:          "https://github.com/rancher/cluster-api-provider-aws/releases/{aws}/infrastructure-components.yaml"
      type:         "InfrastructureProvider"
    - name:         "docker"
      url:          "https://github.com/kubernetes-sigs/cluster-api/releases/{docker}/infrastructure-components-development.yaml"
      type:         "InfrastructureProvider"
    - name:         "kubeadm"
      url:          "https://github.com/rancher/cluster-api/releases/{core}/bootstrap-components.yaml"
      type:         "BootstrapProvider"
    - name:         "rke2"
      url:          "https://github.com/rancher/cluster-api-provider-rke2/releases/{rke2}/bootstrap-components.yaml"
      type:         "BootstrapProvider"
    - name:         "rke2"
      url:          "https://github.com/rancher/cluster-api-provider-rke2/releases/{rke2}/control-plane-components.yaml"
      type:         "ControlPlaneProvider"
    images:
      cluster-api:
        repository: "registry.rancher.com/rancher"
"""


def pin(text, pattern):
    return re.search(pattern, text, re.MULTILINE)[1]


class SyncProvidersTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        shutil.copytree(ROOT / "platform", self.root / "platform")
        turtles = (self.root / sync_providers.TURTLES_VALUES).read_text()
        providers = (self.root / sync_providers.PROVIDERS_VALUES).read_text()
        self.rancher = pin(turtles, r"^# rancher-version: (\S+)$")
        # A catalogue that matches the repository as it is.
        self.release = {
            "turtles": pin(turtles, r"^# chart-version: (\S+)$"),
            "core": pin(turtles, r"^      version: (\S+)$"),
            "docker": pin(providers, r"infrastructureDocker:(?:\n    .*)*?\n    version: (\S+)"),
            "aws": pin(providers, r"infrastructureAWS:(?:\n    .*)*?\n    version: (\S+)"),
            "rke2": pin(providers, r"bootstrapRKE2:(?:\n    .*)*?\n    version: (\S+)"),
        }
        self.requests = []

    def read(self, repository, path, ref, token=None):
        self.requests.append((repository, path, ref, token))
        if repository == "rancher/rancher":
            self.assertEqual((path, ref), ("build.yaml", self.rancher))
            return f"webhookVersion: 110.0.4+up0.11.4\nturtlesVersion: 110.0.4+up{self.release['turtles'][1:]}\n"
        self.assertEqual((repository, path, ref), ("rancher/turtles", sync_providers.CATALOGUE, self.release["turtles"]))
        return CATALOGUE.format(**self.release)

    def run_main(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = sync_providers.main(list(arguments), root=self.root, read=self.read)
        return code, stdout.getvalue(), stderr.getvalue()

    def texts(self):
        return {path: (self.root / path).read_text() for path in (sync_providers.TURTLES_VALUES, sync_providers.PROVIDERS_VALUES)}

    def test_matching_catalogue_changes_nothing(self):
        before = self.texts()
        self.assertEqual(self.run_main("--check")[0], 0)
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("Already in sync", stdout)
        self.assertEqual(self.texts(), before)

    def test_new_catalogue_rewrites_every_pin_and_nothing_else(self):
        before = self.texts()
        self.release.update(turtles="v9.1.2", core="v9.2.3", docker="v9.2.3", aws="v9.3.4", rke2="v9.4.5")
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        after = self.texts()
        turtles = after[sync_providers.TURTLES_VALUES]
        providers = after[sync_providers.PROVIDERS_VALUES]
        self.assertIn("# chart-version: v9.1.2\n", turtles)
        self.assertIn("      version: v9.2.3\n", turtles)
        self.assertEqual(re.findall(r"version: (v\S+)", providers), ["v9.4.5", "v9.4.5", "v9.3.4", "v9.2.3"])
        self.assertIn("- CAPA: ", stdout)
        self.assertIn("- Turtles: ", stdout)
        # Only the version values change: comments, Renovate markers and layout stay.
        for path, text in after.items():
            old_lines, new_lines = before[path].splitlines(), text.splitlines()
            self.assertEqual(len(old_lines), len(new_lines))
            for old, new in zip(old_lines, new_lines):
                if old != new:
                    self.assertRegex(old, r"^(\s*version: |# chart-version: )v")
        # Idempotent: a second run is a no-op.
        self.assertEqual(self.run_main("--check")[0], 0)
        self.assertIn("Already in sync", self.run_main()[1])
        self.assertEqual(self.texts(), after)

    def test_check_reports_drift_without_writing(self):
        before = self.texts()
        self.release["rke2"] = "v9.4.5"
        code, _, stderr = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("CAPRKE2 bootstrap is", stderr)
        self.assertIn("expects v9.4.5", stderr)
        self.assertEqual(self.texts(), before)

    def test_turtles_follows_rancher(self):
        self.release["turtles"] = "v9.1.2"
        code, _, stderr = self.run_main("--check")
        self.assertEqual(code, 1)
        self.assertIn("Turtles is", stderr)
        self.assertEqual(self.requests[0][:3], ("rancher/rancher", "build.yaml", self.rancher))

    def test_unstable_catalogue_version_is_rejected(self):
        before = self.texts()
        self.release["aws"] = "v9.3.4-rc.1"
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("not a stable release", stderr)
        self.assertEqual(self.texts(), before)

    def test_missing_catalogue_provider_is_rejected(self):
        catalogue = CATALOGUE.replace('"docker"', '"kind"')
        read = self.read
        self.read = lambda *args: catalogue.format(**self.release) if args[0] == "rancher/turtles" else read(*args)
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("no InfrastructureProvider docker", stderr)

    def test_missing_pin_is_rejected(self):
        path = self.root / sync_providers.PROVIDERS_VALUES
        path.write_text(path.read_text().replace("    version: v", "    revision: v", 1))
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("no version key under providers.bootstrapRKE2", stderr)

    def test_turtles_version_parsing(self):
        self.assertEqual(sync_providers.turtles_version("turtlesVersion: 110.0.4+up0.27.4\n"), "v0.27.4")
        self.assertEqual(sync_providers.turtles_version('turtlesVersion: "110.0.4+up0.27.4"\n'), "v0.27.4")
        with self.assertRaises(sync_providers.SyncError):
            sync_providers.turtles_version("turtlesVersion: 0.27.4\n")

    def test_token_is_sent_but_never_reported(self):
        secret = "ghs_example-token-value"
        seen = {}

        def refuse(request, timeout):
            seen.update(request.header_items())
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with mock.patch.object(sync_providers.urllib.request, "urlopen", refuse):
            with self.assertRaises(sync_providers.SyncError) as raised:
                sync_providers.fetch("rancher/rancher", "build.yaml", "v2.15.3", secret)
        self.assertEqual(seen["Authorization"], f"Bearer {secret}")
        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("HTTP 404", str(raised.exception))

    def test_anonymous_fetch_sends_no_authorization(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def answer(request, timeout):
            seen.update(request.header_items())
            return Response(b"turtlesVersion: 110.0.4+up0.27.4\n")

        with mock.patch.object(sync_providers.urllib.request, "urlopen", answer):
            text = sync_providers.fetch("rancher/rancher", "build.yaml", "v2.15.3")
        self.assertIn("turtlesVersion", text)
        self.assertNotIn("Authorization", seen)


if __name__ == "__main__":
    unittest.main()
