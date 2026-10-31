"""Assign a release version to a main-branch commit, without modifying source.

The first signed APK (v0.3.0, versionCode 3) came from commit 3 of this
repository's history. Later commits use a greater code and patch number.
Normal fast-forward pushes therefore cannot reuse a released Android version.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_COMMIT_COUNT = 3
ANDROID_MAX_VERSION_CODE = 2_100_000_000


@dataclass(frozen=True)
class ReleaseVersion:
    name: str
    code: int

    @property
    def tag(self):
        return 'v' + self.name

    @property
    def apk_name(self):
        return f'Pocket-Stash-{self.tag}.apk'


def calculate_release_version(commit_count, gradle):
    code_match = re.search(r'^def pocketBaselineVersionCode\s*=\s*(\d+)\s*$', gradle, re.MULTILINE)
    name_match = re.search(
        r"^def pocketBaselineVersionName\s*=\s*['\"](\d+)\.(\d+)\.(\d+)['\"]\s*$",
        gradle,
        re.MULTILINE,
    )
    if code_match is None or name_match is None:
        raise ValueError('Android baseline version was not found or is invalid')
    serial = commit_count - BASELINE_COMMIT_COUNT
    if serial < 1:
        raise ValueError('Not a new main-branch commit after the first signed release')
    code = int(code_match.group(1)) + serial
    if code > ANDROID_MAX_VERSION_CODE:
        raise ValueError('Android versionCode is too large')
    major, minor, patch = (int(n) for n in name_match.groups())
    return ReleaseVersion(f'{major}.{minor}.{patch + serial}', code)


def main():
    count = int(subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'], cwd=ROOT, text=True).strip())
    version = calculate_release_version(count, (ROOT / 'android/app/build.gradle').read_text())
    output = {
        'POCKET_STASH_VERSION_CODE': version.code,
        'POCKET_STASH_VERSION_NAME': version.name,
        'RELEASE_TAG': version.tag,
        'RELEASE_APK_NAME': version.apk_name,
    }
    # The values above contain only validated digits, dots and constant text.
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as env_file:
        for key, value in output.items():
            env_file.write(f'{key}={value}\n')
    print(f'Publishing {version.tag} with Android versionCode {version.code}')


if __name__ == '__main__':
    main()
