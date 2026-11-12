#!/usr/bin/env python3
"""Pin Turtles and the CAPI providers to what the pinned Rancher release ships.

Rancher's build.yaml names the Turtles chart it bundles, and that Turtles release's provider
catalogue (internal/controllers/clusterctl/config-prime.yaml) names the core CAPI, CAPD, CAPA
and CAPRKE2 releases it is tested with. This reads the Rancher pin in
platform/turtles-values.yaml, fetches both files at their release tags from GitHub (with
GITHUB_TOKEN when set) and rewrites the Turtles chart version and the provider versions to
match. Running it again changes nothing. --check reports drift instead of writing.
"""

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TURTLES_VALUES = "platform/turtles-values.yaml"
PROVIDERS_VALUES = "platform/providers-values.yaml"
CATALOGUE = "internal/controllers/clusterctl/config-prime.yaml"
STABLE = re.compile(r"v\d+\.\d+\.\d+")
# Each pinned version key, by file and YAML path, and the catalogue entry it follows.
PINS = {
    (TURTLES_VALUES, ("cluster-api-operator", "cluster-api", "core")): ("CoreProvider", "cluster-api", "core CAPI"),
    (PROVIDERS_VALUES, ("providers", "infrastructureDocker")): ("InfrastructureProvider", "docker", "CAPD"),
    (PROVIDERS_VALUES, ("providers", "infrastructureAWS")): ("InfrastructureProvider", "aws", "CAPA"),
    (PROVIDERS_VALUES, ("providers", "bootstrapRKE2")): ("BootstrapProvider", "rke2", "CAPRKE2 bootstrap"),
    (PROVIDERS_VALUES, ("providers", "controlplaneRKE2")): ("ControlPlaneProvider", "rke2", "CAPRKE2 control plane"),
}


class SyncError(Exception):
    pass


def fetch(repository, path, ref, token=None):
    """Return a file from a GitHub repository at a tag, authenticated when a token is given."""
    url = f"https://api.github.com/repos/{repository}/contents/{path}?ref={ref}"
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rancher-fleet-clusters-sync-providers",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        raise SyncError(f"Cannot read {repository}/{path} at {ref}: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise SyncError(f"Cannot read {repository}/{path} at {ref}: {getattr(error, 'reason', error)}") from None


def rancher_version(text):
    match = re.search(r"^# rancher-version: (\S+)$", text, re.MULTILINE)
    if not match or not STABLE.fullmatch(match[1]):
        raise SyncError(f"{TURTLES_VALUES}: expected a stable '# rancher-version: vX.Y.Z' pin")
    return match[1]


def turtles_version(build_yaml):
    """Turtles release from Rancher's build.yaml, e.g. turtlesVersion: 110.0.4+up0.27.4."""
    match = re.search(r"^turtlesVersion:\s*[\"']?[^\s\"']*\+up(\d+\.\d+\.\d+)[\"']?\s*$", build_yaml, re.MULTILINE)
    if not match:
        raise SyncError("Rancher build.yaml: no turtlesVersion of the form <chart>+upX.Y.Z")
    return f"v{match[1]}"


def catalogue_versions(text):
    """Map (type, name) to the release each catalogue provider URL points at."""
    versions = {}
    for match in re.finditer(
        r"-\s*name:\s*\"(?P<name>[^\"]+)\"\s*\n\s*url:\s*\"(?P<url>[^\"]+)\"\s*\n\s*type:\s*\"(?P<type>[^\"]+)\"",
        text,
    ):
        release = re.search(r"/releases/([^/]+)/", match["url"])
        if release:
            versions[(match["type"], match["name"])] = release[1]
    return versions


def version_lines(lines):
    """Yield (index, parent key path) for every `version:` key in a block-style YAML file."""
    stack = []
    for index, line in enumerate(lines):
        match = re.match(r"( *)([A-Za-z0-9_.-]+):", line)
        if not match:
            continue
        indent = len(match[1])
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if match[2] == "version":
            yield index, tuple(key for _, key in stack)
        stack.append((indent, match[2]))


def rewrite(relative_path, text, turtles, catalogue):
    """Return the file's text with its pins set from the catalogue, and the changes made."""
    lines = text.splitlines(keepends=True)
    changes = []
    found = set()
    for index, parents in version_lines(lines):
        entry = PINS.get((relative_path, parents))
        if entry is None:
            continue
        provider_type, name, label = entry
        found.add(parents)
        expected = catalogue.get((provider_type, name))
        if expected is None:
            raise SyncError(f"Turtles {turtles}'s catalogue has no {provider_type} {name}")
        if not STABLE.fullmatch(expected):
            raise SyncError(f"Turtles {turtles}'s catalogue pins {provider_type} {name} to {expected}, not a stable release")
        match = re.match(r"(\s*version:\s*)(\S+)(.*)", lines[index], re.DOTALL)
        if match[2] != expected:
            changes.append((relative_path, label, match[2], expected))
            lines[index] = f"{match[1]}{expected}{match[3]}"
    missing = {parents for path, parents in PINS if path == relative_path} - found
    if missing:
        raise SyncError(f"{relative_path}: no version key under {', '.join('.'.join(item) for item in sorted(missing))}")
    if relative_path == TURTLES_VALUES:
        for index, line in enumerate(lines):
            match = re.match(r"(# chart-version: )(\S+)(.*)", line, re.DOTALL)
            if match:
                if match[2] != turtles:
                    changes.append((relative_path, "Turtles", match[2], turtles))
                    lines[index] = f"{match[1]}{turtles}{match[3]}"
                break
        else:
            raise SyncError(f"{relative_path}: missing '# chart-version:' pin")
    return "".join(lines), changes


def sync(root, read=fetch, token=None):
    """Return (rancher, turtles, {path: new text}, changes) for the repository at root."""
    texts = {path: (root / path).read_text() for path in (TURTLES_VALUES, PROVIDERS_VALUES)}
    rancher = rancher_version(texts[TURTLES_VALUES])
    turtles = turtles_version(read("rancher/rancher", "build.yaml", rancher, token))
    catalogue = catalogue_versions(read("rancher/turtles", CATALOGUE, turtles, token))
    updated, changes = {}, []
    for path, text in texts.items():
        updated[path], file_changes = rewrite(path, text, turtles, catalogue)
        changes.extend(file_changes)
    return rancher, turtles, updated, changes


def main(argv=None, root=ROOT, read=fetch):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report drift and exit 1; write nothing")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        rancher, turtles, updated, changes = sync(root, read, token)
    except SyncError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(f"Rancher {rancher} ships Turtles {turtles}; pins follow its provider catalogue.")
    if args.check:
        for path, label, old, new in changes:
            print(f"ERROR: {path}: {label} is {old}, Turtles {turtles} expects {new}", file=sys.stderr)
        if changes:
            print("Run python3 scripts/sync-providers.py and commit the result.", file=sys.stderr)
            return 1
        return 0
    for path, text in updated.items():
        if (root / path).read_text() != text:
            (root / path).write_text(text)
    for path, label, old, new in changes:
        print(f"- {label}: {old} -> {new} ({path})")
    if not changes:
        print("Already in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
