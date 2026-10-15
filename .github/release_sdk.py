"""Validate a stable SDK release and publish once to an explicitly configured registry."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from urllib.parse import urlsplit


class Refused(ValueError):
    pass


def validate(package: Path, tag: str, env: dict[str, str], *, publishing: bool) -> str:
    project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
    version = project["version"]
    if (
        not isinstance(version, str)
        or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
        )
        or tag != "v" + version
    ):
        raise Refused(
            "Use a stable vMAJOR.MINOR.PATCH tag matching the declared package version."
        )
    if not publishing:
        return version
    license_value = project.get("license")
    if isinstance(license_value, dict):
        text = license_value.get("text")
        path = (package / license_value.get("file", "")).resolve()
        try:
            file_has_terms = (
                path.is_file()
                and path.is_relative_to(package.resolve())
                and bool(path.read_text(encoding="utf-8").strip())
            )
        except (OSError, UnicodeError):
            file_has_terms = False
        license_value = (isinstance(text, str) and bool(text.strip())) or file_has_terms
    elif isinstance(license_value, str):
        license_value = bool(license_value.strip())
    else:
        license_value = False
    if not license_value:
        raise Refused(
            "Owner-selected customer license terms must be declared before publication."
        )
    if env.get("SDK_REGISTRY_KIND") != "codeartifact":
        raise Refused(
            "Configure the approved CodeArtifact registry; other registries are not enabled here."
        )
    patterns = {
        "SDK_REGISTRY_REGION": r"[a-z]{2}(?:-[a-z]+)+-[0-9]+",
        "SDK_REGISTRY_DOMAIN": r"[a-z][a-z0-9-]{0,48}[a-z0-9]|[a-z]",
        "SDK_REGISTRY_OWNER": r"[0-9]{12}",
        "SDK_REGISTRY_REPOSITORY": r"[A-Za-z0-9][A-Za-z0-9._-]{1,99}",
        "SDK_REGISTRY_ROLE": r"arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+",
    }
    if any(
        not re.fullmatch(pattern, env.get(key, "")) for key, pattern in patterns.items()
    ):
        raise Refused(
            "Approved registry region, domain, owner, repository and publish role are required."
        )
    return version


def publish(package: Path, dist: Path, tag: str, env: dict[str, str]) -> None:
    version = validate(package, tag, env, publishing=True)
    if not all(
        env.get(key)
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    ):
        raise Refused(
            "Explicit short-lived publish-role credentials are required; ambient profiles are not used."
        )
    files = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if (
        len(files) != 2
        or len(list(dist.glob("*.whl"))) != 1
        or len(list(dist.glob("*.tar.gz"))) != 1
    ):
        raise Refused("Exactly one built wheel and one source archive are required.")
    for path in files:
        if path.is_symlink() or path.parent.resolve() != dist.resolve():
            raise Refused(
                "Release artifacts must be regular files in the selected output directory."
            )
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                names = [
                    n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
                ]
                if len(names) != 1:
                    raise Refused("The wheel has no unique package metadata.")
                metadata = BytesParser().parsebytes(archive.read(names[0]))
        else:
            with tarfile.open(path, "r:gz") as archive:
                members = [
                    m
                    for m in archive.getmembers()
                    if m.name.count("/") == 1
                    and m.name.endswith("/PKG-INFO")
                    and m.isfile()
                ]
                if len(members) != 1:
                    raise Refused("The source archive has no unique package metadata.")
                metadata = BytesParser().parsebytes(
                    archive.extractfile(members[0]).read()
                )
        if (
            metadata.get("Version") != version
            or re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower() != "aq-sdk"
        ):
            raise Refused(
                "Built package identity or version differs from the release tag."
            )
    common = [
        "--domain",
        env["SDK_REGISTRY_DOMAIN"],
        "--domain-owner",
        env["SDK_REGISTRY_OWNER"],
        "--region",
        env["SDK_REGISTRY_REGION"],
        "--output",
        "json",
    ]
    aws_env = {
        k: env[k]
        for k in (
            "PATH",
            "LANG",
            "LC_ALL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
        )
        if k in env
    }
    aws_env.update(AWS_MAX_ATTEMPTS="1", AWS_EC2_METADATA_DISABLED="true")

    def aws(*args):
        return subprocess.run(
            ["aws", "codeartifact", *args, *common],
            env=aws_env,
            capture_output=True,
            text=True,
            check=False,
        )

    existing = aws(
        "describe-package-version",
        "--repository",
        env["SDK_REGISTRY_REPOSITORY"],
        "--format",
        "pypi",
        "--package",
        "aq-sdk",
        "--package-version",
        version,
    )
    if existing.returncode == 0:
        raise Refused(
            "That package version already exists. No overwrite or replacement was attempted."
        )
    if "(ResourceNotFoundException)" not in existing.stderr:
        raise Refused(
            "Registry absence could not be confirmed. No upload was attempted."
        )
    token = aws("get-authorization-token", "--duration-seconds", "900")
    endpoint = aws(
        "get-repository-endpoint",
        "--repository",
        env["SDK_REGISTRY_REPOSITORY"],
        "--format",
        "pypi",
    )
    if token.returncode or endpoint.returncode:
        raise Refused(
            "Registry authentication or endpoint lookup failed. No upload was attempted."
        )
    password = json.loads(token.stdout)["authorizationToken"]
    url = json.loads(endpoint.stdout)["repositoryEndpoint"]
    parts = urlsplit(url)
    if (
        not password
        or parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise Refused("The registry returned an invalid authenticated endpoint.")
    upload_env = {k: env[k] for k in ("PATH", "LANG", "LC_ALL") if k in env}
    upload_env.update(
        TWINE_USERNAME="aws", TWINE_PASSWORD=password, TWINE_REPOSITORY_URL=url
    )
    uploaded = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "twine",
            "upload",
            "--non-interactive",
            "--disable-progress-bar",
            *map(str, files),
        ],
        env=upload_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if uploaded.returncode:
        raise Refused(
            "Upload is unconfirmed. Reconcile registry state before another release attempt."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check-tag", "check-publish", "publish"])
    parser.add_argument("--package", type=Path, default=Path("packages/aq-sdk"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if args.command == "publish":
            publish(args.package, args.dist, tag, dict(os.environ))
        else:
            validate(
                args.package,
                tag,
                dict(os.environ),
                publishing=args.command == "check-publish",
            )
        print("SDK release step completed.")
        return 0
    except Refused as exc:
        print("SDK release stopped: " + str(exc))
    except Exception:
        print(
            "SDK release stopped. Inspect the selected artifact and approved configuration."
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
