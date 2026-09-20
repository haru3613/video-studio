#!/usr/bin/env python3
"""Install the provisioned, signed macOS publish-approval operator app."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

# The public helper must never reuse the private helper's bundle, launcher, or
# Keychain namespace. Protocol schemas stay on their deployed haru.* versions
# until every verifier migrates together.
BUNDLE_ID = "org.videostudio.publish-approval"
APP_NAME = "Video Studio Publish Approval.app"
EXECUTABLE_NAME = "video-studio-publish-approve"
LAUNCHER_NAME = "video-studio-publish-approve"
LAUNCHER_MARKER = "# video-studio-publish-approve-launcher-v1"
SCHEMA = "haru.publish_approval_install.v1"
COMMANDS = ("/usr/bin/security", "/usr/bin/swift", "/usr/bin/codesign")


class InstallError(ValueError):
    pass


class Runner:
    def run(self, arguments):
        return subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _run(runner, arguments, description):
    result = runner.run([str(value) for value in arguments])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise InstallError(f"{description} failed" + (f": {detail}" if detail else ""))
    return result


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_sha256(package):
    digest = hashlib.sha256()
    roots = [package / "Package.swift", package / "Sources"]
    files = [roots[0]] + sorted(path for path in roots[1].rglob("*") if path.is_file())
    for path in files:
        if path.is_symlink():
            raise InstallError(f"source package contains a symlink: {path}")
        relative = path.relative_to(package).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "little"))
        digest.update(content)
    return digest.hexdigest()


def _covers(value, expected):
    return isinstance(value, str) and (value == expected or value.endswith("*") and expected.startswith(value[:-1]))


def _validate_identifiers(
    bundle_id=BUNDLE_ID,
    executable_name=EXECUTABLE_NAME,
    launcher_name=LAUNCHER_NAME,
):
    if (
        not isinstance(bundle_id, str)
        or len(bundle_id) > 255
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,62}(?:\.[A-Za-z0-9][A-Za-z0-9-]{0,62})+",
            bundle_id,
        )
        is None
    ):
        raise InstallError("publish approval bundle identifier is malformed")
    for label, value in (
        ("executable", executable_name),
        ("launcher", launcher_name),
    ):
        if (
            not isinstance(value, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value) is None
        ):
            raise InstallError(f"publish approval {label} name is malformed")


def _profile(path, repo_root, runner, now):
    if not path.is_absolute():
        raise InstallError("--profile must be an absolute path")
    if path.suffix != ".provisionprofile":
        raise InstallError("--profile must name a .provisionprofile file")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError("provisioning profile is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("provisioning profile must be a regular file, not a symlink")
    resolved = path.resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise InstallError("provisioning profile must remain outside the repository")
    raw = path.read_bytes()
    with tempfile.NamedTemporaryFile(
        prefix="video-studio-profile-snapshot-", suffix=".provisionprofile"
    ) as snapshot:
        os.fchmod(snapshot.fileno(), 0o600)
        snapshot.write(raw)
        snapshot.flush()
        decoded = _run(
            runner,
            ["/usr/bin/security", "cms", "-D", "-i", snapshot.name],
            "provisioning profile decode",
        ).stdout
    try:
        value = plistlib.loads(decoded)
    except (plistlib.InvalidFileException, ValueError) as error:
        raise InstallError("decoded provisioning profile is malformed") from error
    platforms = value.get("Platform")
    if not isinstance(platforms, list) or "OSX" not in platforms:
        raise InstallError("provisioning profile does not authorize macOS")
    expires = value.get("ExpirationDate")
    if not isinstance(expires, dt.datetime):
        raise InstallError("provisioning profile expiration is malformed")
    expires_utc = expires.replace(tzinfo=dt.timezone.utc) if expires.tzinfo is None else expires.astimezone(dt.timezone.utc)
    if expires_utc <= now:
        raise InstallError("provisioning profile is expired")
    teams = value.get("TeamIdentifier")
    if (
        not isinstance(teams, list)
        or len(teams) != 1
        or not isinstance(teams[0], str)
        or re.fullmatch(r"[A-Z0-9]{10}", teams[0]) is None
    ):
        raise InstallError("provisioning profile must name exactly one team")
    team = teams[0]
    expected = f"{team}.{BUNDLE_ID}"
    entitlements = value.get("Entitlements")
    if not isinstance(entitlements, dict) or not _covers(entitlements.get("com.apple.application-identifier"), expected):
        raise InstallError("provisioning profile does not authorize the app identifier")
    groups = entitlements.get("keychain-access-groups")
    if not isinstance(groups, list) or not any(_covers(group, expected) for group in groups):
        raise InstallError("provisioning profile does not authorize the keychain access group")
    uuid = value.get("UUID")
    if (
        not isinstance(uuid, str)
        or re.fullmatch(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-"
            r"[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}",
            uuid,
        )
        is None
    ):
        raise InstallError("provisioning profile UUID is malformed")
    certificates = value.get("DeveloperCertificates")
    if not isinstance(certificates, list) or not certificates or not all(isinstance(item, bytes) and item for item in certificates):
        raise InstallError("provisioning profile developer certificates are malformed")
    return {
        "raw": raw,
        "uuid": uuid,
        "expires": expires_utc,
        "team": team,
        "application_identifier": expected,
        "developer_certificates": certificates,
    }


def _validate_identity(identity, runner):
    if (
        not isinstance(identity, str)
        or not identity
        or identity != identity.strip()
        or identity == "-"
        or len(identity) > 512
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in identity)
    ):
        raise InstallError("a non-ad-hoc public codesign identity is required")
    result = _run(
        runner,
        ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"],
        "codesign identity lookup",
    )
    listing = (result.stdout + result.stderr).decode("utf-8", "replace")
    available = re.findall(
        r'(?m)^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"([^"\n]+)"\s*$',
        listing,
    )
    if not any(identity in pair for pair in available):
        raise InstallError("the requested codesign identity is not available")


def _existing_launcher(path, state_root):
    if not os.path.lexists(path):
        return
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("stable launcher exists but is not an installer-owned regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise InstallError("stable launcher exists but is not installer-owned") from error
    if len(lines) >= 2 and lines[0] == "#!/bin/sh -p" and lines[1] == LAUNCHER_MARKER:
        return
    # Migrate the one bootstrap format used before this installer existed, but
    # only when its declared release path and the bytes currently there agree.
    if len(lines) == 8 and lines[:2] == ["#!/bin/sh -p", "set -eu"]:
        path_match = re.fullmatch(r"issuer_path='([^'\n]+)'", lines[2])
        digest_match = re.fullmatch(r"issuer_expected=([0-9a-f]{64})", lines[3])
        if path_match and digest_match:
            digest = digest_match.group(1)
            binary = Path(path_match.group(1))
            expected = (
                state_root
                / "approval-issuer/releases"
                / digest
                / APP_NAME
                / f"Contents/MacOS/{EXECUTABLE_NAME}"
            )
            if (
                binary == expected
                and binary.is_file()
                and not binary.is_symlink()
                and _sha256(binary) == digest
                and lines[4] == '[ -f "$issuer_path" ] && [ ! -L "$issuer_path" ] || exit 5'
                and lines[5] == 'issuer_actual="$(/usr/bin/shasum -a 256 "$issuer_path" | /usr/bin/cut -d " " -f 1)"'
                and lines[6] == '[ "$issuer_actual" = "$issuer_expected" ] || { printf "%s\\n" "Video Studio approval issuer bytes changed" >&2; exit 5; }'
                and lines[7] == 'exec "$issuer_path" "$@"'
            ):
                return
    raise InstallError("refusing to replace an unrelated stable launcher")


def _atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _launcher(binary, digest):
    # Installed roots are operator-controlled and cannot contain a newline or quote.
    path = str(binary)
    if "\n" in path or "'" in path:
        raise InstallError("installed binary path cannot be represented safely")
    return f"""#!/bin/sh -p
{LAUNCHER_MARKER}
set -eu
BINARY='{path}'
EXPECTED_SHA256='{digest}'
ACTUAL_SHA256=$(/usr/bin/shasum -a 256 "$BINARY" | /usr/bin/awk '{{print $1}}')
if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
  echo "video-studio-publish-approve: installed binary hash mismatch" >&2
  exit 78
fi
exec "$BINARY" "$@"
""".encode("utf-8")


def install(profile_path, identity, *, repo_root, state_root, bin_root, runner=None, now=None, system_name=None):
    runner = runner or Runner()
    now = now or dt.datetime.now(dt.timezone.utc)
    system_name = system_name or platform.system()
    repo_root = Path(repo_root).resolve()
    state_root = Path(state_root).expanduser().resolve()
    bin_root = Path(bin_root).expanduser().resolve()
    if system_name != "Darwin":
        raise InstallError("publish approval installer requires macOS")
    _validate_identifiers()
    for command in COMMANDS:
        path = Path(command)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise InstallError(f"required command is unavailable: {command}")
    package = repo_root / "native/publish-approval"
    if not package.is_dir() or not (package / "Package.swift").is_file():
        raise InstallError("publish approval Swift package is missing")
    launcher_path = bin_root / LAUNCHER_NAME
    _existing_launcher(launcher_path, state_root)
    profile = _profile(Path(profile_path), repo_root, runner, now)
    _validate_identity(identity, runner)
    source_sha256 = _source_sha256(package)

    _run(
        runner,
        ["/usr/bin/swift", "build", "--package-path", package, "--configuration", "release"],
        "Swift release build",
    )
    if _source_sha256(package) != source_sha256:
        raise InstallError("publish approval source changed during the release build")
    built = package / f".build/release/{EXECUTABLE_NAME}"
    if built.is_symlink():
        built = built.resolve()
    if not built.is_file():
        raise InstallError(f"Swift release build did not produce {EXECUTABLE_NAME}")

    with tempfile.TemporaryDirectory(prefix="video-studio-publish-approval-install-") as temporary:
        stage = Path(temporary) / APP_NAME
        executable = stage / f"Contents/MacOS/{EXECUTABLE_NAME}"
        resources = stage / "Contents/Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir(parents=True)
        shutil.copy2(built, executable)
        executable.chmod(0o755)
        info = {
            "CFBundleDevelopmentRegion": "en",
            "CFBundleExecutable": EXECUTABLE_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleName": "Video Studio Publish Approval",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
        }
        (stage / "Contents/Info.plist").write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML, sort_keys=True))
        (stage / "Contents/embedded.provisionprofile").write_bytes(profile["raw"])
        entitlements_path = Path(temporary) / "entitlements.plist"
        entitlements = {
            "com.apple.application-identifier": profile["application_identifier"],
            "com.apple.developer.team-identifier": profile["team"],
            "keychain-access-groups": [profile["application_identifier"]],
        }
        entitlements_path.write_bytes(plistlib.dumps(entitlements, fmt=plistlib.FMT_XML, sort_keys=True))
        _run(
            runner,
            ["/usr/bin/codesign", "--force", "--sign", identity, "--entitlements", entitlements_path, "--options", "runtime", "--timestamp=none", stage],
            "app code signing",
        )
        _run(runner, ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", stage], "signed app verification")
        signed_entitlements_result = _run(
            runner,
            ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", stage],
            "signed entitlement inspection",
        )
        signed_entitlements_data = (
            signed_entitlements_result.stdout or signed_entitlements_result.stderr
        )
        try:
            signed_entitlements = plistlib.loads(signed_entitlements_data)
        except (plistlib.InvalidFileException, ValueError) as error:
            raise InstallError("signed app entitlements are malformed") from error
        if signed_entitlements != entitlements:
            raise InstallError("signed app entitlements do not match the restricted installer policy")
        certificate_prefix = Path(temporary) / "signing-certificate-"
        _run(
            runner,
            ["/usr/bin/codesign", "--display", f"--extract-certificates={certificate_prefix}", stage],
            "signed certificate extraction",
        )
        leaf_certificate = Path(f"{certificate_prefix}0")
        if not leaf_certificate.is_file() or leaf_certificate.is_symlink():
            raise InstallError("codesign did not expose a regular public leaf certificate")
        if leaf_certificate.read_bytes() not in profile["developer_certificates"]:
            raise InstallError("signing certificate is not authorized by the provisioning profile")
        details = _run(runner, ["/usr/bin/codesign", "-d", "--verbose=4", stage], "signed app inspection")
        signing_details = (details.stdout + details.stderr).decode("utf-8", "replace")
        identifier_match = re.search(r"(?m)^Identifier=(.+)$", signing_details)
        if identifier_match is None or identifier_match.group(1).strip() != BUNDLE_ID:
            raise InstallError("signed app Identifier does not match the public bundle identifier")
        match = re.search(r"(?m)^TeamIdentifier=(.+)$", signing_details)
        if match is None or match.group(1).strip() != profile["team"]:
            raise InstallError("signed app TeamIdentifier does not match the provisioning profile")
        binary_sha256 = _sha256(executable)

        release = state_root / "approval-issuer/releases" / binary_sha256 / APP_NAME
        installed_binary = release / f"Contents/MacOS/{EXECUTABLE_NAME}"
        if release.exists():
            if not installed_binary.is_file() or _sha256(installed_binary) != binary_sha256:
                raise InstallError("existing versioned release is malformed")
        else:
            release.parent.mkdir(parents=True, exist_ok=True)
            pending = release.parent / f".{APP_NAME}.{os.getpid()}.tmp"
            shutil.copytree(stage, pending)
            os.replace(pending, release)
        _atomic_write(launcher_path, _launcher(installed_binary, binary_sha256), 0o755)
        receipt = {
            "schema": SCHEMA,
            "installed_at": now.replace(microsecond=0).isoformat(),
            "source_sha256": source_sha256,
            "binary_sha256": binary_sha256,
            "profile_uuid": profile["uuid"],
            "profile_expiration": profile["expires"].replace(microsecond=0).isoformat(),
            "team_identifier": profile["team"],
            "bundle_identifier": BUNDLE_ID,
            "application_identifier": profile["application_identifier"],
            "keychain_access_groups": [profile["application_identifier"]],
            "codesign_identity": identity,
            "app_path": str(release),
            "binary_path": str(installed_binary),
            "launcher_path": str(launcher_path),
        }
        _atomic_write(state_root / "approval-issuer/install-receipt.json", json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        return receipt


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Install the provisioned Video Studio publish approval operator app.")
    parser.add_argument("--profile", required=True, type=Path, help="Absolute external .provisionprofile path")
    parser.add_argument("--identity", required=True, help="Public codesign identity name or SHA-1")
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/state/video-studio")
    parser.add_argument("--bin-root", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    try:
        receipt = install(args.profile, args.identity, repo_root=repo, state_root=args.state_root, bin_root=args.bin_root)
    except InstallError as error:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"schema": SCHEMA, "ok": True, **receipt}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
