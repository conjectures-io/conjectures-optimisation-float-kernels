"""Make locked Git revisions available before Lake checks them out."""

import json
import subprocess
from pathlib import Path


def fetch(workspace: Path) -> None:
    manifest = json.loads((workspace / "lake-manifest.json").read_text())
    packages = workspace / manifest["packagesDir"]
    packages.mkdir(parents=True, exist_ok=True)
    for package in manifest["packages"]:
        if package["type"] != "git":
            continue
        # Lake serializes escaped identifiers with guillemets, but omits them on disk.
        name = package["name"]
        if name.startswith("«") and name.endswith("»"):
            name = name[1:-1]
        directory = packages / name
        if not directory.exists():
            subprocess.run(
                ["git", "clone", package["url"], str(directory)], check=True
            )
        revision = package["rev"]
        present = subprocess.run(
            ["git", "-C", str(directory), "cat-file", "-e", f"{revision}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if present.returncode:
            # A pinned commit can remain fetchable after leaving advertised branches.
            subprocess.run(
                ["git", "-C", str(directory), "fetch", package["url"], revision], check=True
            )
        # Repair empty working trees left by the earlier --no-checkout helper.
        # Never reset a populated dependency or discard local edits.
        if all(entry.name == ".git" for entry in directory.iterdir()):
            subprocess.run(
                ["git", "-C", str(directory), "checkout", "--detach", revision], check=True
            )


if __name__ == "__main__":
    fetch(Path(__file__).resolve().parents[1] / "lean")
