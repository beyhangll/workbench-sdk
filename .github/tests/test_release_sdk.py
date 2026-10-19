"""Release preparation tests. Registry, identity and upload calls are always mocked."""

import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "release_sdk", ROOT / ".github/release_sdk.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def package(tmp_path):
    p = tmp_path / "package"
    p.mkdir()
    (p / "pyproject.toml").write_text(
        '[project]\nname="aq-sdk"\nversion="0.1.0"\nlicense="LicenseRef-Synthetic-Test-Only"\n'
    )
    return p


def config():
    return {
        "PATH": os.environ["PATH"],
        "SDK_REGISTRY_KIND": "codeartifact",
        "SDK_REGISTRY_REGION": "eu-central-1",
        "SDK_REGISTRY_DOMAIN": "synthetic",
        "SDK_REGISTRY_OWNER": "111122223333",
        "SDK_REGISTRY_REPOSITORY": "synthetic-sdk",
        "SDK_REGISTRY_ROLE": "arn:aws:iam::111122223333:role/synthetic-release",
        "AWS_ACCESS_KEY_ID": "synthetic-access",
        "AWS_SECRET_ACCESS_KEY": "synthetic-secret",
        "AWS_SESSION_TOKEN": "synthetic-session",
        "AQ_API_KEY": "PRIVATE_EVALUATION_KEY",
        "GITHUB_TOKEN": "PRIVATE_REPOSITORY_TOKEN",
    }


def artifacts(tmp_path, version="0.1.0"):
    d = tmp_path / "dist"
    d.mkdir()
    data = f"Metadata-Version: 2.1\nName: aq-sdk\nVersion: {version}\n".encode()
    with zipfile.ZipFile(d / f"aq_sdk-{version}-py3-none-any.whl", "w") as z:
        z.writestr(f"aq_sdk-{version}.dist-info/METADATA", data)
    with tarfile.open(d / f"aq_sdk-{version}.tar.gz", "w:gz") as t:
        m = tarfile.TarInfo(f"aq_sdk-{version}/PKG-INFO")
        m.size = len(data)
        t.addfile(m, io.BytesIO(data))
    return d


def test_current_undeclared_license_blocks_publication_before_any_process(monkeypatch):
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: pytest.fail(
            "No credentials or publication before configured terms"
        ),
    )
    with pytest.raises(release.Refused, match="license"):
        release.publish(ROOT / "packages/aq-sdk", Path("unused"), "v0.1.0", config())


@pytest.mark.parametrize(
    "key",
    [
        "SDK_REGISTRY_KIND",
        "SDK_REGISTRY_REGION",
        "SDK_REGISTRY_DOMAIN",
        "SDK_REGISTRY_OWNER",
        "SDK_REGISTRY_REPOSITORY",
        "SDK_REGISTRY_ROLE",
    ],
)
def test_missing_registry_configuration_fails_before_authentication(
    package, monkeypatch, key
):
    e = config()
    e.pop(key)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: pytest.fail("No process on missing configuration"),
    )
    with pytest.raises(release.Refused):
        release.publish(package, Path("unused"), "v0.1.0", e)


@pytest.mark.parametrize("tag", ["main", "v0.2.0", "v0.1.0-rc.1", "v00.1.0"])
def test_wrong_or_unsupported_tag_cannot_release(package, tag):
    with pytest.raises(release.Refused):
        release.validate(package, tag, config(), publishing=False)


def test_tag_build_can_be_checked_without_registry_or_license():
    assert (
        release.validate(ROOT / "packages/aq-sdk", "v0.1.0", {}, publishing=False)
        == "0.1.0"
    )


def test_artifact_version_mismatch_fails_before_aws(package, tmp_path, monkeypatch):
    d = artifacts(tmp_path, "0.2.0")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: pytest.fail("No AWS call on wrong artifact"),
    )
    with pytest.raises(release.Refused, match="version"):
        release.publish(package, d, "v0.1.0", config())


@pytest.mark.parametrize("lookup", ["existing", "denied", "uncertain"])
def test_existing_or_unresolved_registry_version_never_uploads(
    package, tmp_path, monkeypatch, lookup
):
    d = artifacts(tmp_path)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        assert args[2] == "describe-package-version"
        return SimpleNamespace(
            returncode=0 if lookup == "existing" else 254, stdout="{}", stderr=lookup
        )

    monkeypatch.setattr(release.subprocess, "run", command)
    with pytest.raises(release.Refused):
        release.publish(package, d, "v0.1.0", config())
    assert len(calls) == 1


@pytest.mark.parametrize("success", [True, False])
def test_mocked_publish_once_keeps_credentials_out_of_args_logs_and_other_processes(
    package, tmp_path, monkeypatch, capsys, success
):
    d = artifacts(tmp_path)
    calls = []
    e = config()
    token = "PRIVATE_REGISTRY_TOKEN"

    def command(args, **kwargs):
        calls.append((args, kwargs["env"]))
        assert (
            token not in str(args)
            and e["AQ_API_KEY"] not in str(kwargs)
            and e["GITHUB_TOKEN"] not in str(kwargs)
        )
        if args[:2] == ["aws", "codeartifact"]:
            assert (
                kwargs["env"]["AWS_MAX_ATTEMPTS"] == "1"
                and "TWINE_PASSWORD" not in kwargs["env"]
            )
            op = args[2]
            if op == "describe-package-version":
                return SimpleNamespace(
                    returncode=254,
                    stdout="",
                    stderr="An error occurred (ResourceNotFoundException)",
                )
            return SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(
                    {"authorizationToken": token}
                    if op == "get-authorization-token"
                    else {"repositoryEndpoint": "https://synthetic.test/pypi/sdk/"}
                ),
            )
        assert kwargs["env"]["TWINE_PASSWORD"] == token
        assert "--skip-existing" not in args
        return SimpleNamespace(
            returncode=0 if success else 1, stdout=token, stderr=token
        )

    monkeypatch.setattr(release.subprocess, "run", command)
    if success:
        release.publish(package, d, "v0.1.0", e)
    else:
        with pytest.raises(release.Refused, match="unconfirmed"):
            release.publish(package, d, "v0.1.0", e)
    assert len(calls) == 4 and sum("twine" in args for args, _ in calls) == 1
    assert token not in capsys.readouterr().out


def test_release_workflow_only_tags_and_preflight_before_credentials():
    w = yaml.safe_load((ROOT / ".github/workflows/release-sdk.yml").read_text())
    trigger = w.get("on", w.get(True))
    assert trigger == {"push": {"tags": ["v*"]}}
    assert w["permissions"] == {} and not w["concurrency"]["cancel-in-progress"]
    build, publish = w["jobs"]["build"], w["jobs"]["publish"]
    assert "id-token" not in build["permissions"] and publish["needs"] == "build"
    steps = publish["steps"]
    preflight = next(
        i for i, s in enumerate(steps) if s.get("run", "").endswith("check-publish")
    )
    credentials = next(
        i
        for i, s in enumerate(steps)
        if "configure-aws-credentials" in s.get("uses", "")
    )
    assert preflight < credentials and publish["environment"] == "package-release"
    assert all(
        s.get("with", {}).get("persist-credentials") is False
        for s in steps
        if "checkout" in s.get("uses", "")
    )
    assert "--check SHA256SUMS" in str(steps) and "skip-existing" not in str(steps)


def test_publish_without_assumed_credentials_does_not_use_ambient_profile(
    package, tmp_path, monkeypatch
):
    e = config()
    e.pop("AWS_SESSION_TOKEN")
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: pytest.fail("No ambient-profile lookup"),
    )
    with pytest.raises(release.Refused, match="ambient"):
        release.publish(package, artifacts(tmp_path), "v0.1.0", e)


@pytest.mark.parametrize("content", ["", " \n\t", "unreadable"])
def test_empty_or_unreadable_license_file_stops_before_credentials(
    package, tmp_path, monkeypatch, content
):
    (package / "pyproject.toml").write_text(
        '[project]\nname="aq-sdk"\nversion="0.1.0"\nlicense={file="LICENSE"}\n'
    )
    license_file = package / "LICENSE"
    license_file.write_text(content)
    if content == "unreadable":
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == license_file:
                raise PermissionError("Synthetic unreadable license")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(
        release.subprocess,
        "run",
        lambda *a, **k: pytest.fail("No credential lookup or publication"),
    )
    with pytest.raises(release.Refused, match="license"):
        release.publish(package, artifacts(tmp_path), "v0.1.0", config())


def test_nonempty_in_package_license_file_satisfies_only_declaration_preflight(package):
    (package / "pyproject.toml").write_text(
        '[project]\nname="aq-sdk"\nversion="0.1.0"\nlicense={file="LICENSE"}\n'
    )
    (package / "LICENSE").write_text("Synthetic terms for offline validation only.\n")
    assert release.validate(package, "v0.1.0", config(), publishing=True) == "0.1.0"
