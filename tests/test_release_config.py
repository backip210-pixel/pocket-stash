"""Checks for the release pipeline's important fail-closed safeguards.

The actual signed build and publishing are exercised by GitHub Actions; these
fast checks guard against accidentally dropping critical pipeline steps.
"""
import unittest
from pathlib import Path

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

    def test_release_is_tag_only_and_requires_matching_version(self):
        self.assertIn("- 'v*'", self.workflow)
        self.assertNotIn('pull_request:', self.workflow)
        self.assertIn('"$GITHUB_REF_NAME" != "v$version"', self.workflow)

    def test_release_is_tested_and_signature_verified_before_publish(self):
        self.assertLess(self.workflow.index('Test Termux companion'), self.workflow.index('Publish GitHub pre-release'))
        self.assertLess(self.workflow.index('testDebugUnitTest lintDebug assembleRelease'), self.workflow.index('apksigner'))
        self.assertLess(self.workflow.index('apksigner'), self.workflow.index('gh release upload'))
        self.assertIn('SHA256SUMS.txt', self.workflow)
        self.assertIn('--verify-tag --draft --prerelease', self.workflow)
        self.assertIn('--draft=false --prerelease', self.workflow)

    def test_published_releases_are_not_overwritten(self):
        self.assertIn('if [[ "$existing" == "false" ]]; then', self.workflow)
        self.assertLess(self.workflow.index('if [[ "$existing" == "false" ]]; then'), self.workflow.index('gh release upload'))
        self.assertIn('Remove temporary signing key', self.workflow)
        self.assertIn('if: always()', self.workflow)


if __name__ == '__main__':
    unittest.main()
