"""Synthetic-only filesystem and mocked-Stash tests for the review-first workflow."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from performer_workflow import MAX_VIDEO_BYTES, WorkflowError, component
from server import Companion


class FakeStash:
    def __init__(self):
        self.scenes = []
        self.moves = []
        self.backups = 0
        self.fail_backup = False
        self.ambiguous_move = False
        self.busy = False

    def call(self, query, variables=None, timeout=0):
        variables = variables or {}
        if 'PocketWorkflowJobs' in query:
            return {'jobQueue': [{'status': 'RUNNING'}] if self.busy else []}
        if 'PocketWorkflowScenes' in query:
            page = variables['page']
            return {'findScenes': {'count': len(self.scenes), 'scenes': copy.deepcopy(self.scenes[(page-1)*100:page*100])}}
        if 'PocketWorkflowBackup' in query:
            self.backups += 1
            if self.fail_backup:
                raise RuntimeError('backup failed')
            return {'backupDatabase': None}
        if 'PocketWorkflowScene(' in query:
            found = next((x for x in self.scenes if x['id'] == variables['id']), None)
            return {'findScene': copy.deepcopy(found)}
        if 'PocketMove' in query:
            data = variables['input']
            fid = data['ids'][0]
            file = next(f for s in self.scenes for f in s['files'] if f['id'] == fid)
            target = os.path.join(data['destination_folder'], data['destination_basename'])
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(file['path'], target)
            file['path'] = target
            self.moves.append((fid, target))
            if self.ambiguous_move:
                raise RuntimeError('connection lost after request')
            return {'moveFiles': True}
        if 'PocketWorkflowFile' in query or 'PocketResolveFile' in query:
            file = next((f for s in self.scenes for f in s['files'] if f['id'] == variables['id']), None)
            return {'findFile': copy.deepcopy(file)}
        raise AssertionError('Unexpected GraphQL query: ' + query)


class PerformerWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.shared = self.root / 'shared'
        self.home.mkdir(); self.shared.mkdir()
        (self.home / 'storage').mkdir()
        (self.home / 'storage' / 'shared').symlink_to(self.shared, target_is_directory=True)
        self.source = self.shared / 'Movies' / 'Unsorted'
        self.dest = self.shared / 'Movies' / 'Sorted'
        self.source.mkdir(parents=True); self.dest.mkdir(parents=True)
        self.app = Companion(home=self.home, allow_non_termux=True)
        self.fake = FakeStash()
        self.patches = [
            mock.patch.object(self.app, 'server_state', return_value={'state': 'running', 'system': 'OK'}),
            mock.patch.object(self.app, 'configured_folders', return_value=[{'path': str(self.shared / 'Movies'), 'excludeVideo': False}]),
            mock.patch.object(self.app, 'gql', side_effect=self.fake.call),
        ]
        for patch in self.patches: patch.start()

    def tearDown(self):
        if self.app.workflow.worker:
            self.app.workflow.worker.join(timeout=3)
        for patch in reversed(self.patches): patch.stop()
        self.temp.cleanup()

    def scene(self, sid='1', fid='10', name='original.mp4', title='Example', performers=None):
        path = self.source / name
        path.write_bytes(b'synthetic video bytes')
        scene = {'id': sid, 'title': title,
                 'performers': performers if performers is not None else [{'id': '1', 'name': 'Alex / Example', 'disambiguation': None}],
                 'files': [{'id': fid, 'path': str(path), 'size': path.stat().st_size}]}
        self.fake.scenes.append(scene)
        return scene, path

    def preview(self, choices=None, excluded=None):
        return self.app.workflow.preview(str(self.source), str(self.dest), choices or {}, excluded or [])

    def start_and_wait(self, plan):
        result = self.app.workflow.start(plan['id'], True, True)
        self.app.workflow.worker.join(timeout=5)
        self.assertFalse(self.app.workflow.is_running())
        return result, self.app.workflow.status()['runs'][-1]

    def test_dry_run_explicit_consent_moves_via_stash_and_leaves_sidecars(self):
        scene, original = self.scene(title='A / safe : title')
        sidecar = self.source / 'original.srt'; sidecar.write_text('caption')
        plan = self.preview()
        self.assertTrue(original.exists())
        self.assertEqual(self.fake.moves, [])
        item = plan['items'][0]
        self.assertEqual(item['state'], 'ready')
        self.assertEqual(Path(item['target']).name, 'original.mp4')
        self.assertEqual(Path(item['target']).parent.name, 'Alex Example')
        with self.assertRaises(WorkflowError): self.app.workflow.start(plan['id'], False, True)
        with self.assertRaises(WorkflowError): self.app.workflow.start(plan['id'], True, False)
        _, run = self.start_and_wait(plan)
        moved = run['items'][0]
        self.assertEqual(run['state'], 'complete')
        self.assertEqual(self.fake.backups, 1)
        self.assertEqual(len(self.fake.moves), 1)
        self.assertFalse(original.exists())
        self.assertEqual(Path(moved['target']).read_bytes(), b'synthetic video bytes')
        self.assertEqual(sidecar.read_text(), 'caption')
        self.assertFalse(moved['user_confirmed_backup'])
        self.assertTrue(moved['local_present'])
        token = moved['item_id']
        self.app.workflow.review(token, True, True)
        self.assertTrue(self.app.workflow.status()['runs'][0]['items'][0]['user_confirmed_backup'])
        self.assertEqual(self.app.workflow.resolve_item(token)['path'], moved['target'])
        self.assertEqual(self.app.workflow.scan_paths()['paths'], [moved['target']])
        journal = self.home / '.pocket-stash' / 'performer-workflow.json'
        self.assertTrue(journal.exists())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_missing_metadata_all_performer_copies_and_exclusion(self):
        self.scene('1', '10', 'one.mp4', performers=[])
        pair = [{'id': '1', 'name': 'A', 'disambiguation': None}, {'id': '2', 'name': 'B', 'disambiguation': None}]
        _, original = self.scene('2', '11', 'plugin renamed—two.mp4', performers=pair)
        content = original.read_bytes()
        self.scene('3', '12', 'three.mp4')
        p = self.preview(choices={'2': '2'}, excluded=['12'])
        self.assertEqual([x['state'] for x in p['items']], ['skipped', 'ready', 'excluded'])
        self.assertEqual([x['album'] for x in p['items'][1]['targets']], ['A', 'B'])
        self.assertEqual(p['copy_bytes'], len(content))
        self.assertEqual(p['output_files'], 2)
        _, run = self.start_and_wait(p)
        self.assertEqual([x['state'] for x in run['items']], ['copied', 'moved'])
        self.assertEqual(len(self.fake.moves), 1)
        self.assertEqual((self.dest / 'A' / original.name).read_bytes(), content)
        self.assertEqual((self.dest / 'B' / original.name).read_bytes(), content)
        self.assertNotEqual((self.dest / 'A' / original.name).stat().st_ino, (self.dest / 'B' / original.name).stat().st_ino)
        self.assertEqual(set(self.app.workflow.scan_paths()['paths']), {str(self.dest / 'A' / original.name), str(self.dest / 'B' / original.name)})
        self.assertTrue((self.source / 'one.mp4').exists())
        self.assertTrue((self.source / 'three.mp4').exists())

    def test_duplicate_file_id_and_existing_target_are_not_moved(self):
        scene, _ = self.scene()
        duplicate = copy.deepcopy(scene); duplicate['id'] = '2'; self.fake.scenes.append(duplicate)
        self.assertTrue(all(i['state'] == 'skipped' for i in self.preview()['items']))
        self.fake.scenes.pop()
        plan = self.preview(); target = Path(plan['items'][0]['target']); target.parent.mkdir(parents=True); target.write_bytes(b'unrelated')
        self.assertEqual(self.preview()['items'][0]['state'], 'skipped')
        self.assertEqual(target.read_bytes(), b'unrelated')

    def test_changed_source_or_metadata_stops_without_move(self):
        scene, path = self.scene()
        plan = self.preview()
        path.write_bytes(b'changed after the dry run - different length')
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['items'][0]['state'], 'failed')
        self.assertEqual(self.fake.moves, [])
        self.assertEqual(path.read_bytes(), b'changed after the dry run - different length')
        plan = self.preview(); scene['title'] = 'Changed in Stash'
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['items'][0]['state'], 'failed')
        self.assertEqual(self.fake.moves, [])

    def test_no_overwrite_if_target_appears_after_preview(self):
        _, original = self.scene()
        plan = self.preview(); target = Path(plan['items'][0]['target']); target.parent.mkdir(); target.write_bytes(b'do not touch')
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['items'][0]['state'], 'failed')
        self.assertEqual(target.read_bytes(), b'do not touch')
        self.assertTrue(original.exists())
        self.assertEqual(self.fake.moves, [])

    def test_null_queue_is_stash_empty_queue(self):
        self.scene(); plan = self.preview()
        original = self.fake.call
        def call(query, variables=None, timeout=0):
            if 'PocketWorkflowJobs' in query:
                return {'jobQueue': None}
            return original(query, variables, timeout)
        with mock.patch.object(self.app, 'gql', side_effect=call):
            _, run = self.start_and_wait(plan)
        self.assertEqual(run['state'], 'complete')

    def test_backup_failure_and_busy_jobs_never_start_a_move(self):
        _, original = self.scene(); plan = self.preview()
        self.fake.busy = True
        with self.assertRaises(WorkflowError): self.app.workflow.start(plan['id'], True, True)
        self.fake.busy = False; self.fake.fail_backup = True
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['state'], 'attention')
        self.assertEqual(run['items'][0]['state'], 'not_started')
        self.assertTrue(original.exists()); self.assertEqual(self.fake.moves, [])

    def test_ambiguous_result_is_journaled_stops_batch_and_requires_review(self):
        self.scene('1', '10', 'one.mp4'); self.scene('2', '11', 'two.mp4')
        self.fake.ambiguous_move = True
        plan = self.preview(); _, run = self.start_and_wait(plan)
        self.assertEqual([x['state'] for x in run['items']], ['uncertain', 'not_started'])
        self.assertEqual(len(self.fake.moves), 1)
        item = run['items'][0]
        with self.assertRaises(WorkflowError): self.app.workflow.start(self.preview()['id'], True, True)
        restored = Companion(home=self.home, allow_non_termux=True)
        self.assertEqual(restored.workflow.status()['runs'][0]['items'][0]['state'], 'uncertain')
        with self.assertRaises(WorkflowError): self.app.workflow.resolve_uncertain(item['item_id'], 'not_moved', True)
        self.app.workflow.resolve_uncertain(item['item_id'], 'moved', True)
        self.assertEqual(self.app.workflow.status()['runs'][0]['items'][0]['state'], 'moved')
        self.assertEqual(len(self.fake.moves), 1)

    def test_process_interruption_marks_only_sent_operation_uncertain(self):
        self.scene('1', '10', 'one.mp4'); self.scene('2', '11', 'two.mp4')
        plan = self.preview()
        items = copy.deepcopy(plan['items'])
        items[0].update(item_id='a' * 24, state='moving')
        items[1].update(item_id='b' * 24, state='queued')
        self.app.workflow.history['runs'].append({'id': '1', 'state': 'running', 'items': items,
                                                   'source': str(self.source), 'destination': str(self.dest)})
        self.app.workflow._save_locked()
        restarted = Companion(home=self.home, allow_non_termux=True)
        run = restarted.workflow.status()['runs'][0]
        self.assertEqual(run['state'], 'interrupted')
        self.assertEqual([x['state'] for x in run['items']], ['uncertain', 'not_started'])
        self.assertTrue((self.source / 'one.mp4').exists())
        self.assertTrue((self.source / 'two.mp4').exists())

    def test_colliding_performer_names_and_linked_destination_are_skipped(self):
        self.scene('1', '10', 'one.mp4', performers=[{'id':'1','name':'Alex / Example','disambiguation':None}])
        self.scene('2', '11', 'two.mp4', performers=[{'id':'2','name':'Alex Example','disambiguation':None}])
        p = self.preview()
        self.assertEqual([x['state'] for x in p['items']], ['ready', 'skipped'])
        self.assertIn('Two different performers', p['items'][1]['reason'])
        (self.dest / 'actual').mkdir()
        (self.dest / 'Alex Example').symlink_to(self.dest / 'actual', target_is_directory=True)
        p = self.preview()
        self.assertEqual(p['items'][0]['state'], 'skipped')
        self.assertIn('symlink', p['items'][0]['reason'])

    def test_existing_performer_folder_is_not_overwritten(self):
        self.scene()
        existing = self.dest / 'Alex Example'
        existing.mkdir(); (existing / 'unrelated.mp4').write_bytes(b'other')
        plan = self.preview()
        self.assertEqual(plan['items'][0]['state'], 'ready')
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['state'], 'complete')
        self.assertEqual((existing / 'unrelated.mp4').read_bytes(), b'other')
        self.assertEqual((existing / 'original.mp4').read_bytes(), b'synthetic video bytes')

    def test_performer_folder_collision_is_detected_across_batches(self):
        first = [{'id': '1', 'name': 'Same Performer', 'disambiguation': 'one'}]
        second = [{'id': '2', 'name': 'Same Performer', 'disambiguation': 'two'}]
        self.scene('1', '10', 'one.mp4', performers=first)
        self.start_and_wait(self.preview())
        self.scene('2', '11', 'two.mp4', performers=second)
        plan = self.preview()
        self.assertEqual(len(plan['items']), 1)
        self.assertEqual(plan['items'][0]['state'], 'skipped')
        self.assertIn('Two different performers', plan['items'][0]['reason'])
        self.assertEqual(len(self.fake.moves), 1)

    def test_post_move_journal_write_failure_remains_uncertain(self):
        self.scene(); plan = self.preview()
        original_save = self.app.workflow._save_locked
        failed = [False]
        def flaky_save():
            if not failed[0] and any(item.get('state') == 'moved' for run in self.app.workflow.history['runs'] for item in run['items']):
                failed[0] = True
                raise OSError('journal temporarily unavailable')
            return original_save()
        with mock.patch.object(self.app.workflow, '_save_locked', side_effect=flaky_save):
            _, run = self.start_and_wait(plan)
        self.assertEqual(run['items'][0]['state'], 'uncertain')
        self.assertEqual(len(self.fake.moves), 1)
        with self.assertRaises(WorkflowError): self.app.workflow.start(self.preview()['id'], True, True)

    def test_unsupported_oversized_nomedia_and_symlink_are_skipped(self):
        self.scene('1', '10', 'one.xyz')
        _, big = self.scene('2', '11', 'two.mp4')
        with big.open('wb') as f: f.truncate(MAX_VIDEO_BYTES + 1)  # sparse test file
        _, link = self.scene('3', '12', 'three.mp4')
        link.unlink(); link.symlink_to(big)
        p = self.preview()
        self.assertIn('not on the supported', p['items'][0]['reason'])
        self.assertIn('10 GB', p['items'][1]['reason'])
        self.assertIn('not a regular', p['items'][2]['reason'])
        (self.dest / '.nomedia').write_text('')
        with self.assertRaises(WorkflowError): self.preview()

    def test_invalid_paths_and_names_are_not_traversal(self):
        self.assertEqual(component('../A\\B / C\x00'), 'A B C')
        self.scene()
        outside = self.root / 'outside'; outside.mkdir()
        with self.assertRaises(Exception): self.app.workflow.preview(str(self.source), str(outside), {})
        with self.assertRaises(WorkflowError): self.app.workflow.preview(str(self.dest), str(self.dest), {})

    def test_explicit_folder_creation_is_scoped_and_no_overwrite(self):
        parent = self.shared / 'Movies'
        result = self.app.workflow.create_folder(str(parent), 'A new staging folder')
        self.assertEqual(Path(result['path']), parent / 'A new staging folder')
        self.assertEqual(list(Path(result['path']).iterdir()), [])
        with self.assertRaises(WorkflowError): self.app.workflow.create_folder(str(parent), 'A new staging folder')
        with self.assertRaises(WorkflowError): self.app.workflow.create_folder(str(parent), '../escape')
        with self.assertRaises(WorkflowError): self.app.workflow.create_folder(str(self.home), 'outside')
        self.assertFalse((self.home / 'outside').exists())

    def test_missing_local_copy_cannot_be_newly_marked_verified(self):
        self.scene(); _, run = self.start_and_wait(self.preview())
        item = run['items'][0]; Path(item['target']).unlink()  # test fixture only, never app behaviour
        with self.assertRaises(WorkflowError): self.app.workflow.review(item['item_id'], True, True)
        self.assertFalse(self.app.workflow.status()['runs'][0]['items'][0]['local_present'])

    def test_prepare_every_video_library_is_idempotent_and_leaves_old_files(self):
        other = self.shared / 'Other'; other.mkdir()
        images = self.shared / 'Images'; images.mkdir()
        old = self.dest / 'older.mp4'; old.write_bytes(b'unchanged')
        folders = [
            {'path': str(self.shared / 'Movies'), 'excludeVideo': False},
            {'path': str(other), 'excludeVideo': False},
            {'path': str(images), 'excludeVideo': True},
        ]
        with mock.patch.object(self.app, 'configured_folders', return_value=folders):
            pairs = self.app.workflow.prepare_pairs()['pairs']
            self.assertEqual(len(pairs), 2)
            self.assertTrue(all(x['ready'] for x in pairs))
            self.assertTrue((other / 'Unsorted').is_dir())
            self.assertTrue((other / 'Sorted').is_dir())
            self.assertFalse((images / 'Unsorted').exists())
            second = self.app.workflow.prepare_pairs()['pairs']
            self.assertTrue(all(not p['created'] for p in second))
        self.assertEqual(old.read_bytes(), b'unchanged')

    def test_folder_preparation_reports_symlinks_files_and_missing_libraries(self):
        bad = self.shared / 'Bad'; bad.mkdir()
        outside = self.root / 'outside'; outside.mkdir()
        (bad / 'Sorted').symlink_to(outside, target_is_directory=True)
        wrong = self.shared / 'Wrong'; wrong.mkdir()
        (wrong / 'Unsorted').write_bytes(b'do not replace')
        folders = [{'path': str(p), 'excludeVideo': False} for p in (bad, wrong, self.shared / 'Missing')]
        with mock.patch.object(self.app, 'configured_folders', return_value=folders):
            pairs = self.app.workflow.prepare_pairs()['pairs']
        self.assertTrue(all(not p['ready'] and p['error'] for p in pairs))
        self.assertTrue((bad / 'Sorted').is_symlink())
        self.assertEqual((wrong / 'Unsorted').read_bytes(), b'do not replace')
        self.assertEqual(list(outside.iterdir()), [])

    def test_scene_requires_both_title_and_performer_even_with_old_choice(self):
        self.scene('1', '10', 'no-title.mp4', title='  ')
        self.scene('2', '11', 'no-performer.mp4', performers=[])
        p = self.preview(choices={'1': '1'})
        self.assertEqual([i['state'] for i in p['items']], ['skipped', 'skipped'])
        self.assertIn('No title', p['items'][0]['reason'])
        self.assertIn('performer', p['items'][1]['reason'])
        self.assertEqual(self.fake.moves, [])

    def test_three_performers_receive_independent_byte_identical_files(self):
        people = [{'id': str(n), 'name': name, 'disambiguation': None}
                  for n, name in enumerate(['Maya', 'Lee', 'Ren'], 1)]
        _, original = self.scene(name='kept by plug-in [R1].MP4', performers=people)
        original.write_bytes(bytes(range(256)) * 4097)
        expected = original.read_bytes()
        p = self.preview()
        self.assertEqual(p['output_files'], 3)
        self.assertEqual(p['copy_bytes'], 2 * len(expected))
        _, run = self.start_and_wait(p)
        self.assertEqual([i['state'] for i in run['items']], ['copied', 'copied', 'moved'])
        outputs = [self.dest / name / original.name for name in ['Maya', 'Lee', 'Ren']]
        self.assertTrue(all(p.read_bytes() == expected for p in outputs))
        self.assertEqual(len({(p.stat().st_dev, p.stat().st_ino) for p in outputs}), 3)
        copied = run['items'][0]
        self.app.workflow.review(copied['item_id'], True, True)
        self.assertEqual(self.app.workflow.resolve_item(copied['item_id'])['path'], copied['target'])
        self.assertEqual(set(self.app.workflow.scan_paths()['paths']), {str(p) for p in outputs})

    def test_colliding_nested_source_filenames_skip_both(self):
        self.scene('1', '10', 'same.mp4')
        nested = self.source / 'nested'; nested.mkdir()
        second = nested / 'same.mp4'; second.write_bytes(b'different source')
        self.fake.scenes.append({'id': '2', 'title': 'Second',
            'performers': [{'id': '1', 'name': 'Alex / Example', 'disambiguation': None}],
            'files': [{'id': '11', 'path': str(second), 'size': second.stat().st_size}]})
        p = self.preview()
        self.assertEqual([i['state'] for i in p['items']], ['skipped', 'skipped'])
        self.assertIn('same destination', p['items'][0]['reason'])
        self.assertEqual(self.fake.moves, [])

    def test_overlapping_libraries_do_not_pull_nested_library_files(self):
        nested = self.source / 'sub-library'; nested.mkdir()
        (nested / 'Unsorted').mkdir(); (nested / 'Sorted').mkdir()
        self.scene('1', '10', 'ordinary.mp4')
        nested_file = nested / 'Unsorted' / 'nested.mp4'; nested_file.write_bytes(b'nested')
        self.fake.scenes.append({'id': '2', 'title': 'Nested',
            'performers': [{'id': '1', 'name': 'Alex / Example', 'disambiguation': None}],
            'files': [{'id': '11', 'path': str(nested_file), 'size': nested_file.stat().st_size}]})
        folders = [{'path': str(self.shared / 'Movies'), 'excludeVideo': False},
                   {'path': str(nested), 'excludeVideo': False}]
        with mock.patch.object(self.app, 'configured_folders', return_value=folders):
            p = self.preview()
            self.assertEqual([i['file_id'] for i in p['items']], ['10'])
            p = self.app.workflow.preview(str(nested / 'Unsorted'), str(nested / 'Sorted'), {})
            self.assertEqual([i['file_id'] for i in p['items']], ['11'])

    def test_low_disk_space_refuses_before_backup_or_copy(self):
        pair = [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]
        _, original = self.scene(performers=pair)
        plan = self.preview()
        with mock.patch('performer_workflow.shutil.disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(WorkflowError, 'Not enough free space'):
                self.app.workflow.start(plan['id'], True, True)
        self.assertTrue(original.exists())
        self.assertEqual(self.fake.backups, 0)
        self.assertEqual(self.fake.moves, [])

    def test_fuse_fallback_uses_exclusive_creation_and_no_overwrite(self):
        from performer_workflow import copy_verified, file_snapshot
        _, original = self.scene()
        folder = self.dest / 'Album'; folder.mkdir()
        target = folder / original.name
        target.write_bytes(b'do not overwrite')
        with mock.patch.dict(os.environ, {'PREFIX': '/data/data/com.termux/files/usr'}):
            with self.assertRaises(FileExistsError):
                copy_verified(str(original), str(self.dest), 'Album', original.name,
                              '.pocket-test.partial', file_snapshot(str(original)))
            self.assertEqual(target.read_bytes(), b'do not overwrite')
            target.unlink()
            digest = copy_verified(str(original), str(self.dest), 'Album', original.name,
                                   '.pocket-test.partial', file_snapshot(str(original)))
        self.assertEqual(len(digest), 64)
        self.assertEqual(target.read_bytes(), original.read_bytes())
        self.assertFalse((folder / '.pocket-test.partial').exists())
        self.assertEqual(original.read_bytes(), b'synthetic video bytes')

    def test_failed_exclusive_copy_removes_only_its_own_partial(self):
        from performer_workflow import copy_verified, file_snapshot
        _, original = self.scene()
        snapshot = file_snapshot(str(original))
        with mock.patch.dict(os.environ, {'PREFIX': '/data/data/com.termux/files/usr'}), \
             mock.patch('performer_workflow.os.write', side_effect=OSError('simulated disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                copy_verified(str(original), str(self.dest), 'Album', original.name,
                              '.pocket-test.partial', snapshot)
        self.assertTrue(original.exists())
        self.assertFalse((self.dest / 'Album' / original.name).exists())
        self.assertEqual(original.read_bytes(), b'synthetic video bytes')

    def test_copy_publish_journal_error_is_uncertain_and_resolvable_after_hashing(self):
        pair = [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]
        _, original = self.scene(performers=pair)
        plan = self.preview(); save = self.app.workflow._save_locked; failed = [False]
        def flaky_save():
            if not failed[0] and any(i.get('state') == 'copied' for r in self.app.workflow.history['runs'] for i in r['items']):
                failed[0] = True
                raise OSError('journal unavailable after copy')
            return save()
        with mock.patch.object(self.app.workflow, '_save_locked', side_effect=flaky_save):
            _, run = self.start_and_wait(plan)
        self.assertEqual([i['state'] for i in run['items']], ['uncertain', 'not_started'])
        self.assertTrue(original.exists())
        self.assertEqual(self.fake.moves, [])
        self.app.workflow.resolve_uncertain(run['items'][0]['item_id'], 'copied', True)
        self.assertEqual(self.app.workflow.status()['runs'][0]['items'][0]['state'], 'copied')
        self.assertTrue(original.exists())

    def test_interrupted_copy_refuses_partial_until_manually_resolved(self):
        pair = [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]
        _, original = self.scene(performers=pair)
        p = self.preview(); item = copy.deepcopy(p['items'][0]); dest = self.dest / 'B' / original.name
        dest.parent.mkdir(); dest.write_bytes(b'partial')
        item.update(item_id='c'*24, state='copying', operation='copy', target=str(dest),
                    temporary=str(dest.parent / '.pocket-copy.partial'))
        self.app.workflow.history['runs'].append({'id': '2', 'state': 'running', 'items': [item],
            'source': str(self.source), 'destination': str(self.dest)})
        self.app.workflow._save_locked()
        restarted = Companion(home=self.home, allow_non_termux=True)
        with mock.patch.object(restarted, 'gql', side_effect=self.fake.call):
            self.assertEqual(restarted.workflow.status()['runs'][0]['items'][0]['state'], 'uncertain')
            with self.assertRaises(WorkflowError): restarted.workflow.resolve_uncertain('c'*24, 'copied', True)
            with self.assertRaises(WorkflowError): restarted.workflow.resolve_uncertain('c'*24, 'not_copied', True)
            self.assertEqual(dest.read_bytes(), b'partial')
            dest.unlink()  # fixture simulates the user's inspected manual cleanup
            restarted.workflow.resolve_uncertain('c'*24, 'not_copied', True)
            self.assertEqual(restarted.workflow.status()['runs'][0]['items'][0]['state'], 'not_copied')
        self.assertTrue(original.exists())


    def test_source_mutation_during_copy_never_moves_and_cleans_partial(self):
        from performer_workflow import copy_verified, file_snapshot
        _, original = self.scene()
        snap = file_snapshot(str(original)); changed = [False]
        def progress(done, total):
            if not changed[0]:
                changed[0] = True
                original.write_bytes(b'modified while copying, leave me here')
        with mock.patch.dict(os.environ, {'PREFIX': '/data/data/com.termux/files/usr'}):
            with self.assertRaisesRegex(WorkflowError, 'source changed'):
                copy_verified(str(original), str(self.dest), 'Album', original.name,
                              '.pocket-test.partial', snap, progress)
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), b'modified while copying, leave me here')
        self.assertFalse((self.dest / 'Album' / original.name).exists())

    def test_new_second_performer_target_blocks_every_copy_and_move(self):
        pair = [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}]
        _, original = self.scene(performers=pair)
        plan = self.preview()
        conflict = self.dest / 'B' / original.name; conflict.parent.mkdir(); conflict.write_bytes(b'other')
        _, run = self.start_and_wait(plan)
        self.assertEqual(run['state'], 'attention')
        self.assertEqual(conflict.read_bytes(), b'other')
        self.assertTrue(original.exists()); self.assertEqual(self.fake.moves, [])
        self.assertFalse((self.dest / 'A' / original.name).exists())

    def test_older_single_move_journal_remains_readable(self):
        album = self.dest / 'Old'; album.mkdir(); target = album / 'legacy.mp4'; target.write_bytes(b'legacy')
        self.app.workflow.history['runs'].append({'id': 'old', 'state': 'complete', 'source': str(self.source),
            'destination': str(self.dest), 'items': [{'item_id': 'd'*24, 'state': 'moved', 'target': str(target),
            'album': 'Old', 'source': str(self.source / 'old.mp4'), 'file_id': '42', 'selected_performer': '1',
            'user_confirmed_backup': False, 'user_confirmed_album': False}]})
        self.app.workflow._save_locked()
        restored = Companion(home=self.home, allow_non_termux=True)
        self.assertEqual(restored.workflow.resolve_item('d'*24)['path'], str(target))
        restored.workflow.review('d'*24, True, True)
        self.assertTrue(restored.workflow.status()['runs'][0]['items'][0]['user_confirmed_backup'])
        self.assertEqual(target.read_bytes(), b'legacy')


    def test_partial_batch_keeps_original_and_prior_copy_on_later_failure(self):
        import performer_workflow as workflow
        pair = [{'id': '1', 'name': 'A'}, {'id': '2', 'name': 'B'}, {'id': '3', 'name': 'C'}]
        _, original = self.scene(performers=pair); content = original.read_bytes()
        plan = self.preview(); real_copy = workflow.copy_verified; calls = [0]
        def flaky(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError('simulated read failure')
            return real_copy(*args, **kwargs)
        with mock.patch.object(workflow, 'copy_verified', side_effect=flaky):
            _, run = self.start_and_wait(plan)
        self.assertEqual([i['state'] for i in run['items']], ['copied', 'failed', 'not_started'])
        self.assertEqual(original.read_bytes(), content)
        self.assertEqual((self.dest / 'B' / original.name).read_bytes(), content)
        self.assertFalse((self.dest / 'A' / original.name).exists())
        self.assertEqual(self.fake.moves, [])
        # Existing verified destination makes a subsequent preview stop safely.
        self.assertEqual(self.preview()['items'][0]['state'], 'skipped')

    def test_pair_preparation_reports_nomedia(self):
        (self.dest / '.nomedia').write_bytes(b'')
        pair = self.app.workflow.prepare_pairs()['pairs'][0]
        self.assertFalse(pair['ready'])
        self.assertIn('.nomedia', pair['error'])
        self.assertTrue((self.dest / '.nomedia').exists())



class DemoWorkflowTests(unittest.TestCase):
    def test_demo_never_uses_filesystem_or_stash_for_workflow(self):
        app = Companion(demo=True)
        app.demo_tools = True; app.demo_running = True; app.demo_configured = True
        app.demo_folders = [{'path': '/storage/emulated/0/Movies', 'excludeVideo': False, 'excludeImage': False}]
        with mock.patch('subprocess.Popen', side_effect=AssertionError('must not run processes')), \
             mock.patch.object(app, 'gql', side_effect=AssertionError('must not connect to Stash')):
            folder = app.workflow.create_folder('/storage/emulated/0/Movies', 'Demo staging')
            self.assertTrue(folder['demo'])
            self.assertEqual(app.browse(folder['path'])['directories'], [])
            app.workflow.prepare_pairs()
            p = app.workflow.preview('/storage/emulated/0/Movies/Unsorted', '/storage/emulated/0/Movies/Sorted', {'2': '11'})
            self.assertEqual([x['state'] for x in p['items']], ['ready', 'ready', 'skipped'])
            app.workflow.start(p['id'], True, True)
            app.workflow.worker.join(timeout=3)
            run = app.workflow.status()['runs'][0]
            self.assertEqual(run['state'], 'complete')
            self.assertEqual(len(run['items']), 3)


if __name__ == '__main__':
    unittest.main()
