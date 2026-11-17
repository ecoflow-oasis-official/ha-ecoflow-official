"""Offline preflight checks; complements, but does not replace, HACS/Hassfest."""

import argparse
import ast
import json
import os
from pathlib import Path
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def key_paths(value, prefix=()):
    if not isinstance(value, dict):
        return {prefix}
    return {path for key, child in value.items() for path in key_paths(child, (*prefix, key))}


def check(tag=None):
    components = [path for path in (ROOT / "custom_components").iterdir()
                  if path.is_dir() and path.name != "__pycache__"]
    require(len(components) == 1, "HACS requires exactly one integration directory")
    component = components[0]
    manifest = load_json(component / "manifest.json")
    require(manifest["domain"] == component.name == component.name.lower(),
            "Manifest domain and lowercase integration folder must match")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        tracked = subprocess.run(
            ["git", "ls-files", "--", "custom_components"], cwd=ROOT,
            check=True, capture_output=True, text=True).stdout.splitlines()
        expected_prefix = f"custom_components/{manifest['domain']}/"
        require(tracked and all(path.startswith(expected_prefix) for path in tracked),
                "Git-tracked component paths must use the lowercase manifest domain")
    for key in ("name", "version", "codeowners", "documentation", "issue_tracker"):
        require(manifest.get(key), f"Missing manifest field: {key}")
    require(list(manifest) == ["domain", "name", *sorted(set(manifest) - {"domain", "name"})],
            "Manifest keys must be domain, name, then alphabetical order")
    for key in ("documentation", "issue_tracker"):
        require(manifest[key].startswith("https://"), f"{key} must use HTTPS")
    require(manifest.get("config_flow") is True, "Expected UI configuration support")
    require(load_json(ROOT / "hacs.json").get("name"), "hacs.json must have a name")
    require((ROOT / "LICENSE").is_file(), "Missing LICENSE")
    require((ROOT / "README.md").is_file(), "Missing README.md")

    source = load_json(component / "strings.json")
    for path in sorted((component / "translations").glob("*.json")):
        translated = load_json(path)
        require(key_paths(translated) == key_paths(source),
                f"Translation keys differ from strings.json: {path.name}")
        if path.stem == "en":
            require(translated == source, "English translation must match strings.json")

    for name, dimensions in (("icon.png", (256, 256)), ("icon@2x.png", (512, 512))):
        image = (component / "brand" / name).read_bytes()
        require(image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) >= 24,
                f"Invalid PNG brand asset: {name}")
        require(struct.unpack(">II", image[16:24]) == dimensions,
                f"Incorrect dimensions for {name}: expected {dimensions}")

    # Check source bytes as well as syntax to catch accidentally encrypted files.
    count = 0
    for directory in (component, ROOT / "tests", ROOT / "scripts"):
        for path in sorted(directory.rglob("*.py")):
            raw = path.read_bytes()
            require(not raw.startswith(b"%TSD-Header"), f"Encrypted Python source: {path}")
            ast.parse(raw, filename=str(path))
            count += 1

    if tag is None:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if os.environ.get("GITHUB_EVENT_NAME") == "release" and event_path:
            tag = load_json(Path(event_path))["release"]["tag_name"]
        elif os.environ.get("GITHUB_REF_TYPE") == "tag":
            tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag is not None:
        require(tag.removeprefix("v") == manifest["version"],
                f"Release tag {tag!r} does not match manifest version {manifest['version']!r}")
    print(f"Repository preflight passed: {component.name} {manifest['version']}, {count} Python files")
    if tag is not None:
        print(f"Release version verified: {tag}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Check an intended release tag locally")
    args = parser.parse_args()
    try:
        check(args.tag)
    except (ValueError, KeyError, SyntaxError, OSError) as error:
        parser.exit(1, f"Repository preflight failed: {error}\n")
