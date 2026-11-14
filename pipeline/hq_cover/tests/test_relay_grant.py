"""Relay grant passthrough only: isolated files, mocked HTTP/transport/locks/workers.
No live queue, global delivery lock, child process, GPU, or QQ side effects.
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
import requests
import soundfile as sf
from hq_cover import delivery, jobs, queueing
from hq_cover.policy import PIPELINE, SINGING_BOUNDARY_POLICY

GRANT = 'opaque.bridge-signed.fixture-not-a-real-token'
SECRET = 'fixture-console-secret-never-persist'
TARGET = 'private:12345678'


class RelayJobTests(unittest.TestCase):
    def job(self):
        return dict(id='test_relay', song='Song Artist', target=TARGET,
                    mode='full', track_id=None, no_send=False, pipeline=PIPELINE,
                    voice_id='fixture', created=1, status='queued')

    def test_hmac_backward_compatibility_and_tamper(self):
        with mock.patch.dict(os.environ, {'QQ_CONSOLE_TOKEN': SECRET}):
            for extra in ({}, {'allow_variant': False},
                          {'queue_version': 2, 'queue_seq': 1, 'queue_deadline': 99,
                           'timeout': 123, 'allow_variant': False,
                           'video_url': 'fixture', 'source_kind': 'bilibili'}):
                job = dict(self.job(), **extra)
                names = ('id', 'song', 'target', 'mode', 'track_id', 'no_send',
                         'pipeline', 'voice_id', 'created')
                if job.get('queue_version') is not None:
                    names += ('queue_version', 'queue_seq', 'queue_deadline', 'timeout')
                if 'allow_variant' in job:
                    names += ('allow_variant',)
                if 'video_url' in job or 'source_kind' in job:
                    names += ('video_url', 'source_kind')
                old_payload = json.dumps({k: job.get(k) for k in names},
                                         ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()
                expected = hmac.new(SECRET.encode(), old_payload, hashlib.sha256).hexdigest()
                self.assertEqual(jobs.submission_binding(job), expected)
                job['submission_hmac'] = expected
                self.assertTrue(jobs.valid_binding(job))
                self.assertFalse(jobs.valid_binding(dict(job, relay_grant=GRANT)))
                job['relay_grant'] = GRANT
                job['submission_hmac'] = jobs.submission_binding(job)
                self.assertTrue(jobs.valid_binding(job))
                for value in (GRANT + 'tamper', '', None):
                    self.assertFalse(jobs.valid_binding(dict(job, relay_grant=value)))
                del job['relay_grant']
                self.assertFalse(jobs.valid_binding(job))

    def args(self, **changes):
        result = dict(song='Song Artist', target=TARGET, no_send=False,
                      mode='full', track_id=None)
        result.update(changes)
        return SimpleNamespace(**result)

    def test_submit_rejects_invalid_grants_before_queue_access(self):
        with mock.patch.object(queueing, 'mutex', side_effect=AssertionError('queue accessed')):
            for value in (123, True, [], {}, 'x' * 4097):
                with self.subTest(value_type=type(value).__name__):
                    with self.assertRaises(ValueError):
                        jobs.submit(self.args(relay_grant=value))
            for grant in (GRANT, ''):
                for changes in ({'no_send': True}, {'target': None}):
                    with self.assertRaises(ValueError):
                        jobs.submit(self.args(relay_grant=grant, **changes))

    def test_submit_persists_only_nonempty_and_keeps_credentials_private(self):
        for changes in ({}, {'relay_grant': None}, {'relay_grant': ''},
                        {'relay_grant': GRANT}, {'relay_grant': 'x' * 4096}):
            with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(jobs, 'JOBS', Path(tmp)))
                stack.enter_context(mock.patch.dict(os.environ, {'QQ_CONSOLE_TOKEN': SECRET}))
                stack.enter_context(mock.patch.object(queueing, 'mutex', return_value=contextlib.nullcontext()))
                rows = stack.enter_context(mock.patch.object(queueing, 'rows', return_value=[]))
                spawn = stack.enter_context(mock.patch.object(jobs, 'spawn_supervisor', return_value=SimpleNamespace(pid=1)))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                jobs.submit(self.args(**changes))
                rows.assert_called_once_with(Path(tmp))
                spawn.assert_called_once()
                files = list(Path(tmp).glob('*.json'))
                self.assertEqual(len(files), 1)
                text = files[0].read_text(encoding='utf-8')
                saved = json.loads(text)
                self.assertEqual('relay_grant' in saved, bool(changes.get('relay_grant')))
                if changes.get('relay_grant'):
                    self.assertEqual(saved['relay_grant'], changes['relay_grant'])
                    self.assertNotIn(changes['relay_grant'], output.getvalue())
                self.assertTrue(jobs.valid_binding(saved))
                self.assertNotIn(SECRET, text + output.getvalue())

    def test_cli_parses_opaque_value_unchanged(self):
        with mock.patch.object(jobs, 'submit') as submit:
            jobs.main(['submit', '--song', 'Song Artist', '--target', TARGET,
                       '--relay-grant', GRANT])
        self.assertEqual(submit.call_args.args[0].relay_grant, GRANT)

    def test_worker_forwards_only_present_field_and_legacy_mock_works(self):
        for present in (False, True):
            with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root = Path(tmp)
                stack.enter_context(mock.patch.object(jobs, 'JOBS', root))
                stack.enter_context(mock.patch.object(jobs, 'ROOT', root))
                stack.enter_context(mock.patch.dict(os.environ, {'QQ_CONSOLE_TOKEN': SECRET}))
                stack.enter_context(mock.patch.object(jobs, 'gpu_lock', return_value=contextlib.nullcontext()))
                work = root / 'runs' / 'test_relay'
                work.mkdir(parents=True)
                job = self.job()
                if present:
                    job['relay_grant'] = GRANT
                job['submission_hmac'] = jobs.submission_binding(job)
                jobs.save(job)
                process = mock.Mock(return_value={'cover': 'fixture-cover', 'vocal': 'fixture-vocal', 'quality': {}})
                stack.enter_context(mock.patch.dict(sys.modules, {'hq_cover.pipeline': SimpleNamespace(process=process)}))
                # Legacy callable has no relay_grant argument: compatibility is real, not Mock accepting everything.
                def legacy_send(cover, target, quality, workdir, checkpoint=None):
                    return {'verified': True, 'sent': [{'message_id': 'fixture-id'}]}
                sender = stack.enter_context(mock.patch.object(delivery, 'send_verified',
                    side_effect=None if present else legacy_send,
                    return_value={'verified': True, 'sent': [{'message_id': 'fixture-id'}]}))
                jobs._run_claimed(job['id'])
                sender.assert_called_once()
                self.assertEqual('relay_grant' in sender.call_args.kwargs, present)
                if present:
                    self.assertEqual(sender.call_args.kwargs['relay_grant'], GRANT)
                self.assertEqual(jobs.load(job['id'])['status'], 'done')


class Response:
    def __init__(self, body):
        self.body = body
        self.ok = True
        self.status_code = 200

    def json(self):
        return self.body


class RelayDeliveryTests(unittest.TestCase):
    def run_case(self, grant=None, probe_only=False, recover=False, tamper=None):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            cover = root / 'master.wav'
            sf.write(cover, np.zeros((800, 2)), 8000, subtype='PCM_24')
            quality = {'quality_passed': True, 'pipeline': PIPELINE,
                       'singing_boundary_policy': SINGING_BOUNDARY_POLICY,
                       'original_source_bypass': False,
                       'files': [{'path': str(cover), 'sha256': delivery.file_hash(cover)}]}
            stack.enter_context(mock.patch.dict(os.environ, {'QQ_CONSOLE_TOKEN': SECRET}))
            lock = stack.enter_context(mock.patch.object(delivery, 'delivery_lock', return_value=contextlib.nullcontext()))
            session_type = stack.enter_context(mock.patch.object(delivery.requests, 'Session'))
            stack.enter_context(mock.patch.object(delivery.time, 'sleep'))
            stack.enter_context(mock.patch('hq_cover.transport.prepare_transport_parts',
                side_effect=lambda snapshot, *args: ([snapshot], {'notice': 'fixture transport; no encoder invoked'})))
            calls = []

            def post(url, **kwargs):
                body = kwargs['json']
                calls.append((url, dict(body)))
                self.assertEqual(kwargs['headers'], {'X-Console-Token': SECRET})
                self.assertFalse(kwargs['allow_redirects'])
                self.assertNotIn(SECRET, json.dumps(body))
                self.assertEqual('relayGrant' in body, grant is not None)
                if grant is not None:
                    self.assertEqual(body['relayGrant'], grant)
                if url.endswith('/delivery-status'):
                    self.assertEqual(set(body), {'target', 'deliveryId', 'sha256'} | ({'relayGrant'} if grant is not None else set()))
                    receipt = {k: body[k] for k in ('target', 'deliveryId', 'sha256')}
                    receipt.update(status='verified', messageId='fixture-id')
                    if tamper:
                        receipt[tamper] = 'wrong-identity'
                    return Response({'ok': True, 'receipt': receipt})
                if body['dryRun']:
                    return Response({'ok': True, 'limits': {'maxSeconds': 300, 'maxBytes': 20000000}})
                if recover:
                    raise requests.ReadTimeout('fixture uncertain send')
                return Response({'ok': True, 'verified': True, 'messageId': 'fixture-id'})

            session_type.return_value.post.side_effect = post
            kwargs = {'probe_only': probe_only}
            if grant is not None:
                kwargs['relay_grant'] = grant
            if tamper:
                with self.assertRaises(delivery.DeliveryUncertain):
                    delivery.send_verified(cover, TARGET, quality, root, **kwargs)
            else:
                report = delivery.send_verified(cover, TARGET, quality, root, **kwargs)
                self.assertEqual(report['verified'], not probe_only)
                self.assertNotIn(SECRET, json.dumps(report))
                self.assertNotIn(GRANT, json.dumps(report))
            lock.assert_called_once()
            self.assertFalse(session_type.return_value.trust_env)
            dry = [b for u, b in calls if b.get('dryRun') is True]
            sends = [b for u, b in calls if b.get('dryRun') is False]
            receipts = [b for u, b in calls if u.endswith('/delivery-status')]
            self.assertEqual(len(dry), 2)
            self.assertEqual(len(sends), 0 if probe_only else 1)
            self.assertEqual(len(receipts), 1 if recover and not probe_only else 0)
            for receipt in receipts:
                for field in ('target', 'deliveryId', 'sha256'):
                    self.assertEqual(receipt[field], sends[0][field])
            for artifact in root.rglob('*.json*'):
                text = artifact.read_text(encoding='utf-8')
                self.assertNotIn(SECRET, text)
                self.assertNotIn(GRANT, text)

    def test_grant_all_preflight_and_send_payloads(self):
        self.run_case(GRANT)

    def test_grant_probe_only_payloads(self):
        self.run_case(GRANT, probe_only=True)

    def test_grant_receipt_recovery_never_resends(self):
        self.run_case(GRANT, recover=True)

    def test_legacy_payloads_omit_grant_even_in_receipt_recovery(self):
        self.run_case(recover=True)

    def test_receipt_binding_checks_unchanged(self):
        for field in ('target', 'sha256', 'deliveryId'):
            with self.subTest(field=field):
                self.run_case(GRANT, recover=True, tamper=field)


if __name__ == '__main__':
    unittest.main()
