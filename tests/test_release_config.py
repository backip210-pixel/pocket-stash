"""Checks for release signing, tracked versions, and publication safeguards."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import release_version
from server import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/release.yml').read_text()
        cls.debug_workflow = (ROOT / '.github/workflows/android.yml').read_text()
        cls.gradle = (ROOT / 'android/app/build.gradle').read_text()
        cls.properties = (ROOT / 'android/version.properties').read_text()

    def test_signing_uses_private_environment_and_fixed_alias(self):
        self.assertIn("System.getenv('POCKET_STASH_KEYSTORE')", self.gradle)
        self.assertIn("System.getenv('POCKET_STASH_SIGNING_PASSWORD')", self.gradle)
        self.assertIn("keyAlias 'pocket-stash'", self.gradle)
        self.assertIn("it.name == 'preReleaseBuild'", self.gradle)
        self.assertIn('signingConfig signingConfigs.release', self.gradle)
        self.assertIn('secrets.POCKET_STASH_RELEASE_KEYSTORE', self.workflow)
        self.assertIn('secrets.POCKET_STASH_RELEASE_PASSWORD', self.workflow)
        self.assertNotIn('actions/upload-artifact', self.debug_workflow)

    def test_release_is_main_branch_only_and_uses_tracked_version(self):
        self.assertIn('branches:\n      - main', self.workflow)
        self.assertNotIn('pull_request:', self.workflow)
        self.assertIn('fetch-depth: 0', self.workflow)
        self.assertIn('python3 tools/release_version.py', self.workflow)
        self.assertIn("rootProject.file('version.properties')", self.gradle)
        self.assertNotIn("System.getenv('POCKET_STASH_VERSION_CODE')", self.gradle)

    def test_release_is_tested_and_identity_verified_before_publish(self):
        self.assertLess(self.workflow.index('Test Termux companion'), self.workflow.index('Publish GitHub release'))
        self.assertLess(self.workflow.index('testDebugUnitTest lintDebug assembleRelease'), self.workflow.index('apksigner'))
        self.assertLess(self.workflow.index('apksigner'), self.workflow.index('gh release upload'))
        self.assertIn('android/release-certificate.sha256', self.workflow)
        self.assertIn('versionCode=\'$POCKET_STASH_VERSION_CODE\'', self.workflow)
        self.assertIn('versionName=\'$POCKET_STASH_VERSION_NAME\'', self.workflow)
        self.assertIn('--draft=false --prerelease=false --latest', self.workflow)
        fingerprint = (ROOT / 'android/release-certificate.sha256').read_text().strip()
        self.assertRegex(fingerprint, r'^[a-f0-9]{64}$')

    def test_published_releases_and_unrelated_tags_are_not_overwritten(self):
        self.assertIn('"$existing_commit" != "$GITHUB_SHA"', self.workflow)
        self.assertIn('if [[ "$existing" == "false" ]]; then', self.workflow)
        self.assertLess(self.workflow.index('if [[ "$existing" == "false" ]]; then'), self.workflow.index('gh release upload'))
        self.assertIn('if: always()', self.workflow)

    def test_app_and_companion_versions_match_tracked_source(self):
        version = release_version.parse_version_properties(self.properties)
        self.assertEqual(version.tag, 'v' + version.name)
        self.assertEqual(version.apk_name, 'Pocket-Stash-v' + version.name + '.apk')
        self.assertGreater(version.code, 0)
        self.assertEqual(APP_VERSION, version.name)

    def test_invalid_version_files_are_rejected(self):
        for value in ['', 'VERSION_NAME=1.2.3\nVERSION_CODE=0',
                      'VERSION_NAME=1.02.3\nVERSION_CODE=4',
                      'VERSION_NAME=1.2.3\nVERSION_CODE=2100000001',
                      'VERSION_NAME=1.2.3\nVERSION_NAME=1.2.4\nVERSION_CODE=4']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                release_version.parse_version_properties(value)

    def test_existing_releases_require_increasing_name_and_code(self):
        def git(*args):
            if args == ('rev-parse', 'HEAD'): return 'abc'
            if args == ('tag', '--list', 'v*'): return 'v0.3.0\nv0.3.1'
            if args == ('rev-parse', 'v0.3.0^{commit}'): return 'old'
            raise AssertionError(args)
        with patch('tools.release_version._git', side_effect=git):
            release_version.validate_release_history(release_version.ReleaseVersion('0.3.2', 5))
            with self.assertRaises(ValueError):
                release_version.validate_release_history(release_version.ReleaseVersion('0.3.2', 4))
            with self.assertRaises(ValueError):
                release_version.validate_release_history(release_version.ReleaseVersion('0.3.0', 5))

    def test_versioned_tag_must_record_matching_properties(self):
        def git(*args):
            if args == ('rev-parse', 'HEAD'): return 'new'
            if args == ('tag', '--list', 'v*'): return 'v0.3.2'
            if args == ('show', 'v0.3.2:android/version.properties'):
                return 'VERSION_NAME=0.3.2\nVERSION_CODE=5'
            raise AssertionError(args)
        with patch('tools.release_version._git', side_effect=git):
            release_version.validate_release_history(release_version.ReleaseVersion('0.3.3', 6))
            with self.assertRaises(ValueError):
                release_version.validate_release_history(release_version.ReleaseVersion('0.3.3', 5))

    def test_ci_exports_the_tracked_version(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / 'github-env'
            with patch.dict(os.environ, {'GITHUB_ENV': str(env_path)}), \
                 patch('tools.release_version.validate_release_history'):
                release_version.main()
            version = release_version.parse_version_properties(self.properties)
            self.assertEqual(env_path.read_text().splitlines(), [
                'POCKET_STASH_VERSION_CODE=' + str(version.code),
                'POCKET_STASH_VERSION_NAME=' + version.name,
                'RELEASE_TAG=' + version.tag,
                'RELEASE_APK_NAME=' + version.apk_name,
            ])


if __name__ == '__main__':
    unittest.main()
