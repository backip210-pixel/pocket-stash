"""Optional real-Stash test; uses only synthetic media in a temporary home.

Set POCKET_STASH_TEST_STASH_BINARY to a local Stash v0.31.1 executable and
install ffmpeg/ffprobe to run this test. No existing Stash data is touched.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Companion, folder_fingerprint


@unittest.skipUnless(os.environ.get('POCKET_STASH_TEST_STASH_BINARY') and
                     shutil.which('ffmpeg') and shutil.which('ffprobe'),
                     'Set POCKET_STASH_TEST_STASH_BINARY and install ffmpeg/ffprobe')
class RealStashPhashTests(unittest.TestCase):
    def test_scan_and_scoped_non_overwriting_backfill(self):
        stash_binary = Path(os.environ['POCKET_STASH_TEST_STASH_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='pocket-stash-phash-test-') as tmp:
            root = Path(tmp)
            home, binaries = root / 'home', root / 'bin'
            home.mkdir()
            binaries.mkdir()
            (binaries / 'stash').symlink_to(stash_binary)
            videos, excluded = home / 'videos', home / 'excluded'
            videos.mkdir()
            excluded.mkdir()

            def make_video(path, source):
                subprocess.run([
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', source + '=size=160x120:rate=20', '-t', '5', '-c:v', 'mpeg4',
                    '-pix_fmt', 'yuv420p', str(path),
                ], check=True, timeout=30)

            make_video(videos / 'a.mp4', 'testsrc2')
            make_video(excluded / 'b.mp4', 'smptebars')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]

            with patch.dict(os.environ, {'PATH': str(binaries) + os.pathsep + os.environ['PATH']}):
                app = Companion(home=home, stash_port=port, allow_non_termux=True)

                def wait_job(job_id):
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        job = app.gql('query($id: ID!) { findJob(input:{id:$id}) { id status error } }',
                                      {'id': job_id}).get('findJob')
                        self.assertIsNotNone(job, 'Stash job disappeared')
                        if job['status'] == 'FINISHED':
                            return
                        if job['status'] in ('FAILED', 'CANCELLED'):
                            self.fail('Stash job did not finish: ' + str(job))
                        time.sleep(0.2)
                    self.fail('Timed out waiting for Stash job ' + job_id)

                def files():
                    result = app.gql('query { findScenes(filter:{per_page:-1}) { scenes '
                                     '{ files { id path fingerprints { type value } } } } }')
                    return {Path(f['path']).name: f for scene in result['findScenes']['scenes']
                            for f in scene['files']}

                def phash(item):
                    return next((fp['value'] for fp in item['fingerprints']
                                 if fp['type'].lower() == 'phash'), None)

                try:
                    app.start_server()
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        if app.server_state().get('system') == 'SETUP':
                            break
                        time.sleep(0.2)
                    else:
                        self.fail('Temporary Stash did not reach setup')
                    folders = [
                        {'path': str(videos), 'excludeVideo': False, 'excludeImage': True},
                        {'path': str(excluded), 'excludeVideo': False, 'excludeImage': True},
                    ]
                    result = app.save_folders(folders, folder_fingerprint([]),
                                              scan=True, generate_phashes=False)
                    self.assertIsNone(result['scan_error'])
                    wait_job(result['job_id'])
                    rows = files()
                    self.assertEqual(set(rows), {'a.mp4', 'b.mp4'})
                    self.assertIsNone(phash(rows['a.mp4']))
                    self.assertIsNone(phash(rows['b.mp4']))

                    current = app.configured_folders()
                    changed = [dict(folder) for folder in current]
                    for folder in changed:
                        if folder['path'] == str(excluded):
                            folder.update(excludeVideo=True, excludeImage=False)
                    app.save_folders(changed, folder_fingerprint(current))
                    wait_job(app.generate_missing_phashes()['job_id'])
                    rows = files()
                    self.assertTrue(phash(rows['a.mp4']))
                    self.assertIsNone(phash(rows['b.mp4']))

                    # A distinct valid hash shows that overwrite:false really preserves it.
                    hash_type = next(fp['type'] for fp in rows['a.mp4']['fingerprints']
                                     if fp['type'].lower() == 'phash')
                    sentinel = '123456789abcdef0'
                    app.gql('mutation($input: FileSetFingerprintsInput!) '
                            '{ fileSetFingerprints(input:$input) }',
                            {'input': {'id': rows['a.mp4']['id'], 'fingerprints':
                                       [{'type': hash_type, 'value': sentinel}]}})
                    self.assertEqual(phash(files()['a.mp4']), sentinel)
                    wait_job(app.generate_missing_phashes()['job_id'])
                    self.assertEqual(phash(files()['a.mp4']), sentinel)

                    make_video(videos / 'new.mp4', 'testsrc2')
                    wait_job(app.scan()['job_id'])
                    rows = files()
                    self.assertTrue(phash(rows['new.mp4']))
                    self.assertEqual(phash(rows['a.mp4']), sentinel)
                finally:
                    if app._managed_alive():
                        app.stop_server()


if __name__ == '__main__':
    unittest.main()
