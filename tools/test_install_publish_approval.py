#!/usr/bin/env python3
import datetime as dt
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_publish_approval as installer  # noqa: E402


TEAM = "P326N6GQH6"
IDENTITY = "Apple Development: Operator (P326N6GQH6)"
CERTIFICATE = b"allowed-public-leaf-der"


def profile_value(
    *,
    expires=None,
    team=TEAM,
    application_identifier=None,
    groups=None,
    certificate=CERTIFICATE,
    uuid="11111111-2222-4333-8444-555555555555",
):
    expected = f"{team}.{installer.BUNDLE_ID}"
    return {
        "Platform": ["OSX"],
        "ExpirationDate": expires or dt.datetime(2030, 1, 1),
        "TeamIdentifier": [team],
        "UUID": uuid,
        "DeveloperCertificates": [certificate],
        "Entitlements": {
            "com.apple.application-identifier": application_identifier or expected,
            "keychain-access-groups": groups or [expected],
        },
    }


class FakeRunner:
    def __init__(
        self,
        package,
        profile,
        *,
        reported_team=TEAM,
        reported_identifier=installer.BUNDLE_ID,
        leaf=CERTIFICATE,
        reported_entitlements=None,
    ):
        self.package = package
        self.profile = profile
        self.reported_team = reported_team
        self.leaf = leaf
        self.reported_identifier = reported_identifier
        self.reported_entitlements = reported_entitlements
        self.calls = []
        self.signed_entitlements = None

    def run(self, arguments):
        self.calls.append(arguments)
        stdout = b""
        stderr = b""
        if arguments[:4] == ["/usr/bin/security", "cms", "-D", "-i"]:
            stdout = plistlib.dumps(self.profile)
        elif arguments[:5] == ["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"]:
            stdout = (
                f'  1) {"A" * 40} "{IDENTITY}"\n'
                "     1 valid identities found\n"
            ).encode()
        elif arguments[:3] == ["/usr/bin/swift", "build", "--package-path"]:
            binary = self.package / f".build/release/{installer.EXECUTABLE_NAME}"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"release-executable")
            binary.chmod(0o755)
        elif arguments[:3] == ["/usr/bin/codesign", "--force", "--sign"]:
            entitlements = Path(arguments[arguments.index("--entitlements") + 1])
            self.signed_entitlements = plistlib.loads(entitlements.read_bytes())
            app = Path(arguments[-1])
            executable = app / f"Contents/MacOS/{installer.EXECUTABLE_NAME}"
            executable.write_bytes(executable.read_bytes() + b"-signed")
        elif arguments[:3] == ["/usr/bin/codesign", "-d", "--entitlements"]:
            stdout = plistlib.dumps(
                self.reported_entitlements
                if self.reported_entitlements is not None
                else self.signed_entitlements
            )
        elif arguments[:2] == ["/usr/bin/codesign", "--display"]:
            # codesign optional arguments require =; a separate prefix becomes
            # another code path and fails on a real macOS installation.
            if not arguments[2].startswith("--extract-certificates="):
                return subprocess.CompletedProcess(arguments, 1, b"", b"prefix treated as code path")
            prefix = arguments[2].split("=", 1)[1]
            Path(f"{prefix}0").write_bytes(self.leaf)
        elif arguments[:3] == ["/usr/bin/codesign", "-d", "--verbose=4"]:
            stderr = (
                f"Identifier={self.reported_identifier}\n"
                f"TeamIdentifier={self.reported_team}\n"
            ).encode()
        return subprocess.CompletedProcess(arguments, 0, stdout, stderr)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        # FakeRunner models macOS commands; host prerequisites are tested separately.
        commands = mock.patch.object(installer, "COMMANDS", ())
        commands.start()
        self.addCleanup(commands.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "repo"
        self.package = self.repo / "native/publish-approval"
        (self.package / "Sources/App").mkdir(parents=True)
        (self.package / "Package.swift").write_text("// package", encoding="utf-8")
        (self.package / "Sources/App/main.swift").write_text("print(1)", encoding="utf-8")
        self.profile = self.root / "operator.provisionprofile"
        self.profile.write_bytes(b"external-profile")
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.now = dt.datetime(2026, 9, 9, tzinfo=dt.timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def run_install(self, value=None, runner=None):
        runner = runner or FakeRunner(self.package, value or profile_value())
        receipt = installer.install(
            self.profile,
            IDENTITY,
            repo_root=self.repo,
            state_root=self.state,
            bin_root=self.bin,
            runner=runner,
            now=self.now,
            system_name="Darwin",
        )
        return receipt, runner

    def test_missing_host_command_is_rejected_before_signing(self):
        with mock.patch.object(installer, "COMMANDS", (str(self.root / "missing-command"),)):
            with self.assertRaisesRegex(installer.InstallError, "required command is unavailable"):
                self.run_install()

    def test_installs_signed_version_with_restricted_entitlements_and_hash_launcher(self):
        receipt, runner = self.run_install()
        app = Path(receipt["app_path"])
        binary = Path(receipt["binary_path"])
        launcher = Path(receipt["launcher_path"])
        self.assertTrue((app / "Contents/embedded.provisionprofile").is_file())
        self.assertEqual(receipt["team_identifier"], TEAM)
        self.assertEqual(receipt["bundle_identifier"], installer.BUNDLE_ID)
        self.assertEqual(receipt["application_identifier"], f"{TEAM}.{installer.BUNDLE_ID}")
        self.assertEqual(
            receipt["keychain_access_groups"],
            [f"{TEAM}.{installer.BUNDLE_ID}"],
        )
        self.assertEqual(receipt["profile_uuid"], "11111111-2222-4333-8444-555555555555")
        self.assertEqual(receipt["binary_sha256"], installer._sha256(binary))
        self.assertIn(installer.LAUNCHER_MARKER, launcher.read_text(encoding="utf-8"))
        self.assertIn(receipt["binary_sha256"], launcher.read_text(encoding="utf-8"))
        self.assertEqual(
            runner.signed_entitlements,
            {
                "com.apple.application-identifier": f"{TEAM}.{installer.BUNDLE_ID}",
                "com.apple.developer.team-identifier": TEAM,
                "keychain-access-groups": [f"{TEAM}.{installer.BUNDLE_ID}"],
            },
        )
        self.assertNotIn("get-task-allow", runner.signed_entitlements)
        self.assertNotIn("com.apple.security.app-sandbox", runner.signed_entitlements)
        self.assertTrue(
            any(
                call[:5]
                == ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml"]
                for call in runner.calls
            )
        )

    def test_profile_must_be_external_current_and_authorize_bundle_and_keychain(self):
        cases = [
            (profile_value(expires=dt.datetime(2020, 1, 1)), "expired"),
            (profile_value(application_identifier=f"{TEAM}.wrong.bundle"), "app identifier"),
            (profile_value(groups=[f"{TEAM}.wrong.bundle"]), "keychain access group"),
            (profile_value(team="OTHERTEAM1", application_identifier=f"{TEAM}.{installer.BUNDLE_ID}"), "app identifier"),
            (profile_value(team="bad/team!"), "exactly one team"),
            (profile_value(uuid="not-a-uuid"), "UUID"),
        ]
        for value, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(installer.InstallError, message):
                    self.run_install(value)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.bin.exists())

        inside = self.repo / "operator.provisionprofile"
        inside.write_bytes(b"profile")
        with self.assertRaisesRegex(installer.InstallError, "outside the repository"):
            installer.install(
                inside,
                IDENTITY,
                repo_root=self.repo,
                state_root=self.state,
                bin_root=self.bin,
                runner=FakeRunner(self.package, profile_value()),
                now=self.now,
                system_name="Darwin",
            )

    def test_failed_signed_identity_validation_does_not_replace_installed_state(self):
        receipt, _ = self.run_install()
        launcher = Path(receipt["launcher_path"])
        before = launcher.read_bytes()
        bad_runner = FakeRunner(self.package, profile_value(), reported_team="OTHERTEAM")
        with self.assertRaisesRegex(installer.InstallError, "TeamIdentifier"):
            self.run_install(runner=bad_runner)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertEqual(Path(receipt["binary_path"]).read_bytes(), b"release-executable-signed")

        bad_certificate = FakeRunner(self.package, profile_value(), leaf=b"different-public-cert")
        with self.assertRaisesRegex(installer.InstallError, "not authorized"):
            self.run_install(runner=bad_certificate)
        self.assertEqual(launcher.read_bytes(), before)

        bad_identifier = FakeRunner(
            self.package,
            profile_value(),
            reported_identifier="com.example.wrong",
        )
        with self.assertRaisesRegex(installer.InstallError, "Identifier"):
            self.run_install(runner=bad_identifier)
        self.assertEqual(launcher.read_bytes(), before)

        bad_entitlements = FakeRunner(
            self.package,
            profile_value(),
            reported_entitlements={
                "com.apple.application-identifier": f"{TEAM}.{installer.BUNDLE_ID}",
                "com.apple.developer.team-identifier": TEAM,
                "keychain-access-groups": [f"{TEAM}.wrong.bundle"],
            },
        )
        with self.assertRaisesRegex(installer.InstallError, "entitlements"):
            self.run_install(runner=bad_entitlements)
        self.assertEqual(launcher.read_bytes(), before)

    def test_unrelated_existing_launcher_is_never_replaced(self):
        self.bin.mkdir()
        launcher = self.bin / installer.LAUNCHER_NAME
        launcher.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "unrelated"):
            self.run_install()
        self.assertEqual(launcher.read_text(encoding="utf-8"), "#!/bin/sh\necho unrelated\n")
        self.assertFalse(self.state.exists())

    def test_verified_bootstrap_launcher_can_be_migrated(self):
        old_binary = (
            self.state
            / "approval-issuer/releases"
            / ("0" * 64)
            / installer.APP_NAME
            / f"Contents/MacOS/{installer.EXECUTABLE_NAME}"
        )
        old_binary.parent.mkdir(parents=True)
        old_binary.write_bytes(b"old-signed-binary")
        digest = installer._sha256(old_binary)
        correct_binary = Path(str(old_binary).replace("0" * 64, digest))
        correct_binary.parent.mkdir(parents=True)
        correct_binary.write_bytes(old_binary.read_bytes())
        self.bin.mkdir()
        launcher = self.bin / installer.LAUNCHER_NAME
        launcher.write_text(
            f'''#!/bin/sh -p
set -eu
issuer_path='{correct_binary}'
issuer_expected={digest}
[ -f "$issuer_path" ] && [ ! -L "$issuer_path" ] || exit 5
issuer_actual="$(/usr/bin/shasum -a 256 "$issuer_path" | /usr/bin/cut -d " " -f 1)"
[ "$issuer_actual" = "$issuer_expected" ] || {{ printf "%s\\n" "Video Studio approval issuer bytes changed" >&2; exit 5; }}
exec "$issuer_path" "$@"
''',
            encoding="utf-8",
        )
        receipt, _ = self.run_install()
        self.assertIn(installer.LAUNCHER_MARKER, launcher.read_text(encoding="utf-8"))
        self.assertTrue(Path(receipt["binary_path"]).is_file())

    def test_public_identifiers_are_structural_and_do_not_collide_with_private_helper(self):
        installer._validate_identifiers()
        self.assertEqual(installer.BUNDLE_ID, "org.videostudio.publish-approval")
        self.assertEqual(installer.LAUNCHER_NAME, "video-studio-publish-approve")
        self.assertNotIn("haru", installer.BUNDLE_ID.lower())
        self.assertNotIn("haru", installer.APP_NAME.lower())
        self.assertNotIn("haru", installer.LAUNCHER_MARKER.lower())
        for values, message in (
            (("single", installer.EXECUTABLE_NAME, installer.LAUNCHER_NAME), "bundle"),
            ((installer.BUNDLE_ID, "../bad", installer.LAUNCHER_NAME), "executable"),
            ((installer.BUNDLE_ID, installer.EXECUTABLE_NAME, "Bad Name"), "launcher"),
        ):
            with self.subTest(values=values):
                with self.assertRaisesRegex(installer.InstallError, message):
                    installer._validate_identifiers(*values)

    def test_codesign_identity_rejects_control_characters_before_lookup(self):
        runner = FakeRunner(self.package, profile_value())
        for identity in ("", "-", " leading", "trailing ", "line\nbreak"):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(installer.InstallError, "non-ad-hoc"):
                    installer._validate_identity(identity, runner)
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
