"""Optional synthetic real-Stash workflow test; never touches an existing library.

Set POCKET_STASH_TEST_STASH_BINARY to a Stash v0.31.1 executable and install
ffmpeg/ffprobe. Exercises rename, three performers, actual copies and rescan.
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
class RealStashSortingTests(unittest.TestCase):
    def test_plugin_filename_all_performer_copies_and_rescan(self):
        binary = Path(os.environ['POCKET_STASH_TEST_STASH_BINARY']).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix='pocket-stash-sort-test-') as tmp:
            root = Path(tmp)
            home, bins = root / 'home', root / 'bin'
            home.mkdir(); bins.mkdir(); (bins / 'stash').symlink_to(binary)
            shared = home / 'phone'; shared.mkdir(); (home / 'storage').mkdir()
            (home / 'storage' / 'shared').symlink_to(shared, target_is_directory=True)
            library = shared / 'Movies'; library.mkdir()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            with patch.dict(os.environ, {'PATH': str(bins) + os.pathsep + os.environ['PATH']}):
                app = Companion(home=home, stash_port=port, allow_non_termux=True)

                def wait_job(job_id):
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        job = app.gql('query($id: ID!) { findJob(input:{id:$id}) { id status error } }',
                                      {'id': job_id}).get('findJob')
                        self.assertIsNotNone(job)
                        if job['status'] == 'FINISHED': return
                        if job['status'] in ('FAILED', 'CANCELLED'): self.fail(str(job))
                        time.sleep(0.2)
                    self.fail('Timed out waiting for Stash job')

                def scenes():
                    return app.gql('query { findScenes(filter:{per_page:-1}) { scenes '
                                   '{ id title files { id path size } performers { id name } } } }')['findScenes']['scenes']
                try:
                    app.start_server()
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        if app.server_state().get('system') == 'SETUP': break
                        time.sleep(.2)
                    else: self.fail('Temporary Stash did not reach setup')
                    folders = [{'path': str(library), 'excludeVideo': False, 'excludeImage': True}]
                    result = app.save_folders(folders, folder_fingerprint([]), scan=False)
                    self.assertTrue(result['workflow_pairs'][0]['ready'])
                    unsorted, sorted_dir = library / 'Unsorted', library / 'Sorted'
                    original = unsorted / 'before-plugin.mp4'
                    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                                    '-i', 'testsrc2=size=160x120:rate=20', '-t', '3', '-c:v', 'mpeg4',
                                    '-pix_fmt', 'yuv420p', str(original)], check=True, timeout=30)
                    content = original.read_bytes()
                    wait_job(app.scan(False)['job_id'])
                    scene = scenes()[0]
                    people = []
                    for name in ('Alex', 'Jamie', 'Morgan'):
                        person = app.gql('mutation($input:PerformerCreateInput!) '
                                         '{ performerCreate(input:$input) { id name } }',
                                         {'input': {'name': name}})['performerCreate']
                        people.append(person['id'])
                    app.gql('mutation($input:SceneUpdateInput!) { sceneUpdate(input:$input) { id title } }',
                            {'input': {'id': scene['id'], 'title': 'Identified', 'performer_ids': people}})
                    # Exercise the same Stash API used by the rename plugin, in place.
                    plugin_name = 'Plugin name [studio] - unchanged.mp4'
                    fid = scene['files'][0]['id']
                    moved = app.gql('mutation($input:MoveFilesInput!) { moveFiles(input:$input) }',
                                    {'input': {'ids': [fid], 'destination_folder': str(unsorted),
                                               'destination_basename': plugin_name}}, timeout=30)
                    self.assertTrue(moved['moveFiles'])
                    plugin_file = unsorted / plugin_name
                    self.assertEqual(plugin_file.read_bytes(), content)
                    plan = app.workflow.preview(str(unsorted), str(sorted_dir), {})
                    self.assertEqual(plan['items'][0]['state'], 'ready')
                    self.assertEqual(plan['output_files'], 3)
                    self.assertTrue(all(Path(t['target']).name == plugin_name for t in plan['items'][0]['targets']))
                    # Simulate the Android/Termux exclusive-create fallback on Linux.
                    with patch.dict(os.environ, {'PREFIX': '/data/data/com.termux/files/usr'}):
                        app.workflow.start(plan['id'], True, True)
                        app.workflow.worker.join(timeout=60)
                    self.assertFalse(app.workflow.is_running())
                    run = app.workflow.status()['runs'][-1]
                    self.assertEqual(run['state'], 'complete', run['message'])
                    paths = [sorted_dir / name / plugin_name for name in ('Alex', 'Jamie', 'Morgan')]
                    self.assertFalse(plugin_file.exists())
                    self.assertTrue(all(path.read_bytes() == content for path in paths))
                    self.assertEqual(len({p.stat().st_ino for p in paths}), 3)
                    wait_job(app.scan(False)['job_id'])
                    after = scenes()
                    indexed = {f['path'] for item in after for f in item['files']}
                    # Exact duplicates may share one scene; whatever Stash does,
                    # all physical files and the original Stash path must survive.
                    self.assertTrue(all(path.exists() for path in paths))
                    self.assertEqual(indexed, {str(path) for path in paths})
                    self.assertEqual(len(after), 1)  # Stash 0.31.1 groups exact duplicates as scene files.
                    self.assertEqual(after[0]['title'], 'Identified')
                    self.assertEqual({p['name'] for p in after[0]['performers']}, {'Alex', 'Jamie', 'Morgan'})
                    print('Real Stash 0.31.1 after rescan: one scene with all three physical files indexed')
                finally:
                    if app._managed_alive(): app.stop_server()


if __name__ == '__main__':
    unittest.main()
