"""Check the release pipeline's fail-closed and automatic-version safeguards."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import release_version

ROOT = Path(__file__).resolve().parents[1]


class ReleaseConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / '.github/workflows/release.yml').read_text()
        cls.gradle = (ROOT / 'android/app/build.gradle').read_text()

    def test_signing_uses_private_environment_and_fixed_alias(self):
        self.assertIn("System.getenv('POCKET_STASH_KEYSTORE')", self.gradle)
        self.assertIn("System.getenv('POCKET_STASH_SIGNING_PASSWORD')", self.gradle)
        self.assertIn("keyAlias 'pocket-stash'", self.gradle)
        self.assertIn("it.name == 'preReleaseBuild'", self.gradle)
        self.assertIn('Release signing needs POCKET_STASH_KEYSTORE', self.gradle)
        self.assertIn('signingConfig signingConfigs.release', self.gradle)

    def test_release_is_main_branch_only_and_uses_full_history(self):
        self.assertIn('branches:\n      - main', self.workflow)
        self.assertNotIn('pull_request:', self.workflow)
        self.assertNotIn("- 'v*'", self.workflow)
        self.assertIn('fetch-depth: 0', self.workflow)
        self.assertIn('python3 tools/release_version.py', self.workflow)

    def test_release_is_tested_and_signature_verified_before_publish(self):
        self.assertLess(self.workflow.index('Test Termux companion'), self.workflow.index('Publish GitHub release'))
        self.assertLess(self.workflow.index('testDebugUnitTest lintDebug assembleRelease'), self.workflow.index('apksigner'))
        self.assertLess(self.workflow.index('apksigner'), self.workflow.index('gh release upload'))
        self.assertIn('SHA256SUMS.txt', self.workflow)
        self.assertIn('--target "$GITHUB_SHA" --draft', self.workflow)
        self.assertIn('--draft=false --prerelease=false --latest', self.workflow)

    def test_published_releases_and_unrelated_tags_are_not_overwritten(self):
        self.assertIn('"$existing_commit" != "$GITHUB_SHA"', self.workflow)
        self.assertIn('if [[ "$existing" == "false" ]]; then', self.workflow)
        self.assertLess(self.workflow.index('if [[ "$existing" == "false" ]]; then'), self.workflow.index('gh release upload'))
        self.assertIn('Remove temporary signing key', self.workflow)
        self.assertIn('if: always()', self.workflow)

    def test_main_commits_increment_the_installed_android_version(self):
        first = release_version.calculate_release_version(4, self.gradle)
        second = release_version.calculate_release_version(5, self.gradle)
        self.assertEqual((first.name, first.code, first.tag, first.apk_name),
                         ('0.3.1', 4, 'v0.3.1', 'Pocket-Stash-v0.3.1.apk'))
        self.assertEqual((second.name, second.code), ('0.3.2', 5))
        self.assertGreater(second.code, first.code)

    def test_invalid_baseline_or_old_commit_is_rejected(self):
        with self.assertRaises(ValueError):
            release_version.calculate_release_version(3, self.gradle)
        with self.assertRaises(ValueError):
            release_version.calculate_release_version(4, 'invalid gradle')
        with self.assertRaises(ValueError):
            release_version.calculate_release_version(2_100_000_004, self.gradle)

    def test_ci_exports_the_exact_calculated_version(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / 'github-env'
            with patch.dict(os.environ, {'GITHUB_ENV': str(env_path)}), \
                 patch('tools.release_version.subprocess.check_output', return_value='4\n'):
                release_version.main()
            self.assertEqual(env_path.read_text().splitlines(), [
                'POCKET_STASH_VERSION_CODE=4',
                'POCKET_STASH_VERSION_NAME=0.3.1',
                'RELEASE_TAG=v0.3.1',
                'RELEASE_APK_NAME=Pocket-Stash-v0.3.1.apk',
            ])


if __name__ == '__main__':
    unittest.main()
