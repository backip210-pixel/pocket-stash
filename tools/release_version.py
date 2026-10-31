"""Validate the tracked Android version and prepare a GitHub release.

android/version.properties is the single source of truth for APK versionName
and versionCode. Published release tags are checked so an update cannot
accidentally reuse or decrease either value.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID_MAX_VERSION_CODE = 2_100_000_000
VERSION_RE = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z')
# These releases preceded the tracked version file. Their APK codes were
# verified from the actual published packages, not inferred from old source.
LEGACY_RELEASE_CODES = {'v0.3.0': 3, 'v0.3.1': 4}


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

    @property
    def parts(self):
        return tuple(int(n) for n in self.name.split('.'))


def parse_version_properties(text):
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError('Invalid version.properties line')
        key, value = (part.strip() for part in line.split('=', 1))
        if key in fields or key not in ('VERSION_NAME', 'VERSION_CODE'):
            raise ValueError('Duplicate or unknown version property')
        fields[key] = value
    name, code = fields.get('VERSION_NAME', ''), fields.get('VERSION_CODE', '')
    if not VERSION_RE.fullmatch(name) or not re.fullmatch(r'[1-9][0-9]*', code):
        raise ValueError('Android version name or code is invalid')
    code = int(code)
    if code > ANDROID_MAX_VERSION_CODE:
        raise ValueError('Android versionCode is too large')
    return ReleaseVersion(name, code)


def _git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()


def validate_release_history(current):
    head = _git('rev-parse', 'HEAD')
    # Check all fetched release tags, including those outside rewritten history.
    for tag in _git('tag', '--list', 'v*').splitlines():
        name = tag[1:]
        if not VERSION_RE.fullmatch(name):
            continue
        if tag == current.tag:
            if _git('rev-parse', tag + '^{commit}') != head:
                raise ValueError('The requested version tag already refers to another commit')
            # A retry of an interrupted draft for this exact commit is allowed.
            continue
        if tag in LEGACY_RELEASE_CODES:
            prior_code = LEGACY_RELEASE_CODES[tag]
        else:
            try:
                previous = parse_version_properties(_git('show', tag + ':android/version.properties'))
            except (subprocess.CalledProcessError, ValueError) as exc:
                raise ValueError('Cannot verify the tracked version of release ' + tag) from exc
            if previous.tag != tag:
                raise ValueError('Tracked version does not match release tag ' + tag)
            prior_code = previous.code
        prior_parts = tuple(int(n) for n in name.split('.'))
        if current.parts <= prior_parts or current.code <= prior_code:
            raise ValueError('Increase both VERSION_NAME and VERSION_CODE beyond ' + tag)


def main():
    version = parse_version_properties((ROOT / 'android/version.properties').read_text())
    validate_release_history(version)
    output = {
        'POCKET_STASH_VERSION_CODE': version.code,
        'POCKET_STASH_VERSION_NAME': version.name,
        'RELEASE_TAG': version.tag,
        'RELEASE_APK_NAME': version.apk_name,
    }
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as env_file:
        for key, value in output.items():
            env_file.write(f'{key}={value}\n')
    print(f'Publishing tracked version {version.tag} with Android versionCode {version.code}')


if __name__ == '__main__':
    main()
