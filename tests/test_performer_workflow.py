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
        self.source = self.shared / 'Movies' / 'Incoming'
        self.dest = self.shared / 'Movies' / 'Photos staging'
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
        self.assertEqual(Path(item['target']).name, 'A safe title [10].mp4')
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

    def test_missing_metadata_multi_performer_choice_and_exclusion(self):
        self.scene('1', '10', 'one.mp4', performers=[])
        pair = [{'id': '1', 'name': 'A', 'disambiguation': None}, {'id': '2', 'name': 'B', 'disambiguation': None}]
        self.scene('2', '11', 'two.mp4', performers=pair)
        self.scene('3', '12', 'three.mp4')
        p = self.preview(excluded=['12'])
        self.assertEqual([x['state'] for x in p['items']], ['skipped', 'needs_choice', 'excluded'])
        p = self.preview(choices={'2': '2'}, excluded=['12'])
        self.assertEqual([x['state'] for x in p['items']], ['skipped', 'ready', 'excluded'])
        self.assertEqual(p['items'][1]['album'], 'B')
        _, run = self.start_and_wait(p)
        self.assertEqual(len(run['items']), 1)
        self.assertEqual(len(self.fake.moves), 1)
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

    def test_untracked_nonempty_performer_folder_is_not_mixed(self):
        self.scene()
        existing = self.dest / 'Alex Example'
        existing.mkdir(); (existing / 'unrelated.mp4').write_bytes(b'other')
        plan = self.preview()
        self.assertEqual(plan['items'][0]['state'], 'skipped')
        self.assertIn('untracked files', plan['items'][0]['reason'])
        self.assertEqual((existing / 'unrelated.mp4').read_bytes(), b'other')

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
            p = app.workflow.preview('/storage/emulated/0/Movies', folder['path'], {'2': '11'})
            self.assertEqual([x['state'] for x in p['items']], ['ready', 'ready', 'skipped'])
            app.workflow.start(p['id'], True, True)
            app.workflow.worker.join(timeout=3)
            run = app.workflow.status()['runs'][0]
            self.assertEqual(run['state'], 'complete')
            self.assertEqual(len(run['items']), 2)


if __name__ == '__main__':
    unittest.main()
