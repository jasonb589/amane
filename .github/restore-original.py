import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def api(path: str, method: str = "GET") -> object:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        method=method,
        headers={
            "Authorization": f'Bearer {os.environ["GH_TOKEN"]}',
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response) if response.status != 204 else None
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Package API returned HTTP {exc.code}") from None


def versions(package_path: str) -> list[dict]:
    result = []
    page = 1
    while True:
        batch = api(f"{package_path}/versions?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise SystemExit("Invalid package version response")
        result.extend(batch)
        if len(batch) < 100:
            return result
        page += 1


def raw_manifest(image: str) -> dict:
    return json.loads(subprocess.check_output(["skopeo", "inspect", "--raw", f"docker://{image}"], text=True))


def collect_manifests(image: str, digest: str) -> set[str]:
    result = {digest}
    for manifest in raw_manifest(f"{image}@{digest}").get("manifests", []):
        result.update(collect_manifests(image, manifest["digest"]))
    return result


def main() -> None:
    owner, name = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    if owner != "jasonb589" or name != "amane":
        raise SystemExit("Unexpected maintenance repository")
    package_path = f"users/{owner}/packages/container/{urllib.parse.quote(name, safe='')}"
    package = api(package_path)
    if not isinstance(package, dict) or package.get("visibility") != "public":
        raise SystemExit("Unexpected package visibility")
    before = versions(package_path)
    Path("package-before.json").write_text(json.dumps(before, ensure_ascii=False), encoding="utf-8")
    print(f"Package permission verified; {len(before)} versions found", flush=True)
    if os.environ.get("DO_RESTORE") != "1":
        return

    source = "ghcr.io/sqzw-x/amane"
    destination = f"ghcr.io/{owner}/{name}".lower()
    print(f"::add-mask::{destination}", flush=True)
    subprocess.run(
        ["skopeo", "login", "ghcr.io", "--username", os.environ["GITHUB_ACTOR"], "--password-stdin"],
        input=os.environ["GH_TOKEN"],
        text=True,
        check=True,
        capture_output=True,
    )
    digest = subprocess.check_output(
        ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{source}:latest"], text=True
    ).strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise SystemExit("Invalid upstream image digest")
    config = json.loads(
        subprocess.check_output(["skopeo", "inspect", "--config", f"docker://{source}@{digest}"], text=True)
    )
    labels = config["config"].get("Labels") or {}
    version = labels.get("org.opencontainers.image.version", "").removeprefix("v")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise SystemExit("Unexpected upstream version")
    if labels.get("org.opencontainers.image.source") != "https://github.com/sqzw-x/amane":
        raise SystemExit("Unexpected upstream source")
    manifest = raw_manifest(f"{source}@{digest}")
    platforms = {
        (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        for item in manifest.get("manifests", [])
    }
    if not {("linux", "amd64"), ("linux", "arm64")}.issubset(platforms):
        raise SystemExit("Upstream architecture missing")
    keep = collect_manifests(source, digest)
    tags = ("latest", version, version.rsplit(".", 1)[0], version.split(".", 1)[0])
    for tag in tags:
        subprocess.run(
            [
                "skopeo", "copy", "--all", "--preserve-digests",
                f"docker://{source}@{digest}", f"docker://{destination}:{tag}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    for tag in tags:
        actual = subprocess.check_output(
            ["skopeo", "inspect", "--format", "{{.Digest}}", f"docker://{destination}:{tag}"], text=True
        ).strip()
        if actual != digest:
            raise SystemExit("Restored image digest mismatch")
    print("Upstream image and both architectures verified", flush=True)

    obsolete = [item for item in versions(package_path) if item["name"] not in keep]
    for index, item in enumerate(obsolete, start=1):
        api(f'{package_path}/versions/{item["id"]}', "DELETE")
        if index % 20 == 0 or index == len(obsolete):
            print(f"Removed obsolete versions: {index}/{len(obsolete)}", flush=True)
    after = versions(package_path)
    if any(item["name"] not in keep for item in after):
        raise SystemExit("Obsolete image versions remain")
    Path("package-after.json").write_text(json.dumps(after, ensure_ascii=False), encoding="utf-8")
    print(f"Original package restored; {len(after)} versions retained", flush=True)


if __name__ == "__main__":
    main()
