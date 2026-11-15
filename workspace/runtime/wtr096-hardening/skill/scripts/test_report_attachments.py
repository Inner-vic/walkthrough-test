"""Synthetic transport/state-machine tests; no Feishu or cloud access."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import stat
from types import SimpleNamespace

import report_attachments as attachments
from request_model import InputError


META = {'vin': 'DEM0VEH1CLE000001', 'start': '2026-10-21T15:48:00', 'end': '2026-10-21T16:22:00'}
OTHER = {**META, 'start': '2026-10-21T14:50:00', 'end': '2026-10-21T15:24:00'}
URL = 'https://example.feishu.cn/docx/doxsynthetic'


class FakeLark:
    """Models file upload (unbound) separately from docx replace_file."""
    def __init__(self, record_path):
        self.record_path = record_path
        self.blocks = {}
        self.media = {}
        self.calls = []
        self.counts = {}
        self.fail = {}
        self.create_keys = {}
        self.bind_keys = {}
        self.page_size = 500
        self.bad_page = False
        self.bad_download = False
        self.business_error = False
        self.response_identity = 'bot'

    @staticmethod
    def flag(args, flag):
        return args[args.index(flag) + 1]

    def result(self, stage, effect):
        self.counts[stage] = self.counts.get(stage, 0) + 1
        failure = self.fail.pop(stage, None)
        if failure == 'before':
            raise InputError('synthetic permission/scope denied')
        data = effect()
        if failure == 'after':
            raise InputError('synthetic lost response')
        if failure == 'malformed':
            data = {}
        if self.business_error:
            data = {'code': 403, 'data': data}
        return {'ok': True, 'identity': self.response_identity, 'data': data}

    def __call__(self, settings, args, *, profile, timeout=120):
        if profile != 'synthetic-report-bot' or self.flag(args, '--as') != 'bot':
            raise AssertionError('wrong publication identity')
        self.calls.append(list(args))
        if args[:2] == ['docs', '+media-upload']:
            def upload():
                if self.flag(args, '--parent-type') != 'docx_file':
                    raise AssertionError('not document media')
                file_id = self.flag(args, '--parent-node')
                assert self.blocks[file_id]['block_type'] == 23
                assert self.flag(args, '--doc-id') == 'doxsynthetic'
                relative = self.flag(args, '--file')
                assert relative.startswith('./') and not Path(relative).is_absolute()
                path = Path(settings['workspace_root']) / relative
                token = 'boxsynthetic' + str(len(self.media) + 1)
                self.media[token] = {'bytes': path.read_bytes(), 'name': path.name, 'file_id': file_id}
                return {'file_token': token}
            return self.result('upload', upload)
        if args[:2] == ['docs', '+media-download']:
            def download():
                token = self.flag(args, '--token')
                data = self.media[token]['bytes']
                if self.bad_download:
                    data = data[:-1] + b'!'
                relative = self.flag(args, '--output')
                assert relative.startswith('./') and not Path(relative).is_absolute()
                path = Path(settings['workspace_root']) / relative
                path.write_bytes(data)
                return {'path': str(path)}
            return self.result('download', download)
        assert args[0] == 'api', args
        method, path = args[1:3]
        assert path.startswith('/open-apis/docx/v1/documents/doxsynthetic/'), path
        params = json.loads(self.flag(args, '--params'))
        if method == 'GET' and path.endswith('/blocks'):
            def listing():
                values = list(self.blocks.values())
                offset = int(params.get('page_token', '0'))
                end = min(len(values), offset + self.page_size)
                data = {'items': copy.deepcopy(values[offset:end]), 'has_more': end < len(values)}
                if data['has_more']:
                    data['page_token'] = str(end)
                if self.bad_page:
                    data.pop('has_more')
                return data
            return self.result('list', listing)
        if method == 'GET':
            return self.result('get', lambda: {'block': copy.deepcopy(self.blocks[path.rsplit('/', 1)[-1]])})
        body = json.loads(self.flag(args, '--data'))
        token = params['client_token']
        assert str(uuid.UUID(token)) == token
        checkpoint = json.loads(self.record_path.read_text('utf-8'))['report_attachments']['roles']
        if method == 'POST':
            def create():
                assert body == {'children': [{'block_type': 23, 'file': {'view_type': 1}}], 'index': -1}
                assert any(x['state'] == 'create_pending' and x['create_client_token'] == token for x in checkpoint.values())
                if token not in self.create_keys:
                    number = str(len(self.create_keys) + 1)
                    view_id, file_id = 'view' + number, 'file' + number
                    self.blocks[view_id] = {'block_id': view_id, 'block_type': 33, 'parent_id': 'doxsynthetic', 'children': [file_id]}
                    self.blocks[file_id] = {'block_id': file_id, 'block_type': 23, 'parent_id': view_id, 'file': {'token': '', 'name': ''}}
                    self.create_keys[token] = view_id
                return {'children': [copy.deepcopy(self.blocks[self.create_keys[token]])], 'client_token': token}
            return self.result('create', create)
        assert method == 'PATCH' and path.endswith('/blocks/batch_update')
        def bind():
            assert any(x['state'] == 'bind_pending' and x['bind_client_token'] == token for x in checkpoint.values())
            request = body['requests'][0]
            file_id, media_token = request['block_id'], request['replace_file']['token']
            media = self.media[media_token]
            assert media['file_id'] == file_id
            self.blocks[file_id]['file'] = {'name': media['name'], 'token': media_token}
            self.bind_keys[token] = file_id
            return {'client_token': token}
        return self.result('bind', bind)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        runtime = self.root / 'runtime'
        runtime.mkdir()
        self.path = runtime / 'walkthrough-synthetic.json'
        self.settings = {'workspace_root': str(self.root), 'runtime_state_dir': str(runtime),
            'lark_profile': 'synthetic-user', 'report_lark_profile': 'synthetic-report-bot',
            'expected_lark_app_id': 'cli_synthetic', 'lark_home': str(self.root / 'auth'), 'lark_cli': 'unused'}
        fingerprint = 'a' * 64
        self.record = {'job_key': 'walkthrough-synthetic', 'request_sha256': fingerprint,
            'vin': META['vin'], 'sender_id': 'ou_synthetic', 'source_chat_id': 'oc_synthetic',
            'source_message_id': 'om_synthetic', 'report_url': URL,
            'request': {'baseline_metadata': dict(META), 'replacement_metadata': dict(OTHER)}}
        self.result = {'request_sha256': fingerprint, 'vin': META['vin'],
            'pipeline': {'windows': {}}, 'full_workbooks': {}}
        for role, metadata, environment in (('baseline', META, 'testtwo'), ('replacement', OTHER, 'prod')):
            folder = self.path.with_suffix('.work') / role
            folder.mkdir(parents=True)
            file = folder / 'native-result.xlsx'
            data = b'Synthetic opaque XLSX transport fixture\x00' + role.encode()
            file.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            rule = ('b' if role == 'baseline' else 'c') * 64
            self.result['pipeline']['windows'][role] = {'request': dict(metadata), 'environment': environment, 'rules_sha256': rule}
            self.result['full_workbooks'][role] = {'path': str(file), 'sha256': digest, 'bytes': len(data),
                'role': role, 'request': dict(metadata), 'environment': environment,
                'request_sha256': attachments.request_digest(metadata), 'rules_sha256': rule, 'validation': {'status': 'ok'}}
        self.fake = FakeLark(self.path)
        self.api_patch = patch.object(attachments.policy, 'run_lark', side_effect=self.fake)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.auth_patch = patch.object(attachments.policy, 'ensure_report_bot_authorization', return_value={'profile': 'synthetic-report-bot', 'available': True})
        self.auth = self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.cwd_patch = patch.object(attachments.policy, 'WORKSPACE_ROOT', self.root)
        self.cwd_patch.start()
        self.addCleanup(self.cwd_patch.stop)

    def publish(self):
        return attachments.ensure_attachments(self.settings, self.path, self.record, self.result, URL)

    def reload(self):
        self.record = json.loads(self.path.read_text('utf-8'))

    def writes(self):
        return {name: self.fake.counts.get(name, 0) for name in ('create', 'upload', 'bind')}

    def test_complete_upload_is_inside_docx_and_two_roles_are_isolated(self):
        before = copy.deepcopy(self.result)
        roles = self.publish()
        self.assertEqual(self.result, before)
        self.assertEqual(self.writes(), {'create': 2, 'upload': 2, 'bind': 2})
        self.assertEqual(self.fake.counts['download'], 2)
        names = set()
        for role, item in roles.items():
            manifest = self.result['full_workbooks'][role]
            self.assertEqual(item['state'], 'ready')
            self.assertEqual(item['verified_sha256'], manifest['sha256'])
            self.assertEqual(item['verified_bytes'], manifest['bytes'])
            self.assertEqual(item['artifact']['request'], self.record['request'][role + '_metadata'])
            self.assertEqual(item['artifact']['environment'], manifest['environment'])
            self.assertTrue(item['file_block_id'].startswith('file'))
            self.assertTrue(item['view_block_id'].startswith('view'))
            names.add(item['artifact']['file_name'])
        self.assertEqual(len(names), 2)
        self.assertGreater(roles['baseline']['artifact']['request']['start'], roles['replacement']['artifact']['request']['start'])
        forbidden = ('permissions', 'transfer_owner', '/drive/v1/files', '+messages', 'auth', '+create')
        self.assertFalse(any(any(value in ' '.join(call) for value in forbidden) for call in self.fake.calls))
        self.assertFalse(list(self.path.with_suffix('.work').glob('.attachment-verification/*')))

    def test_ready_resume_performs_reads_only_and_keeps_same_tokens(self):
        first = copy.deepcopy(self.publish())
        before = self.writes()
        self.reload()
        resumed = self.publish()
        self.assertEqual(self.writes(), before)
        self.assertEqual([resumed[r]['file_token'] for r in attachments.ROLES], [first[r]['file_token'] for r in attachments.ROLES])
        self.assertEqual(self.fake.counts['download'], 4)

    def test_completed_upload_can_be_resent_without_local_originals(self):
        self.publish()
        for manifest in self.result['full_workbooks'].values():
            Path(manifest['path']).unlink()
        before = self.writes()
        self.reload()
        self.publish()
        self.assertEqual(self.writes(), before)

    def test_all_manifests_checked_before_first_mutation(self):
        mutations = [
            lambda m: m.update(role='baseline'),
            lambda m: m.update(environment='testtwo'),
            lambda m: m.update(request=dict(META)),
            lambda m: m.update(request_sha256='0' * 64),
            lambda m: m.update(rules_sha256='d' * 64),
            lambda m: m.update(validation={'status': 'failed'}),
            lambda m: m.update(bytes=True),
            lambda m: m.update(sha256='invalid'),
        ]
        original = copy.deepcopy(self.result['full_workbooks']['replacement'])
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.result['full_workbooks']['replacement'] = copy.deepcopy(original)
                mutate(self.result['full_workbooks']['replacement'])
                with self.assertRaises(InputError):
                    self.publish()
                self.assertFalse(self.fake.calls)

    def test_missing_or_modified_local_file_blocks_all_uploads(self):
        path = Path(self.result['full_workbooks']['replacement']['path'])
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(InputError, '文件缺失|摘要变化'):
            self.publish()
        self.assertFalse(self.fake.calls)

    def test_path_cannot_point_to_other_role_or_outside_task(self):
        original = self.result['full_workbooks']['replacement']['path']
        for bad in (self.result['full_workbooks']['baseline']['path'], str(self.root / 'outside.xlsx')):
            self.result['full_workbooks']['replacement']['path'] = bad
            with self.subTest(path=bad), self.assertRaises(InputError):
                self.publish()
        self.result['full_workbooks']['replacement']['path'] = original
        self.assertFalse(self.fake.calls)

    def test_symlink_and_windows_junction_artifacts_are_rejected(self):
        manifest = self.result['full_workbooks']['baseline']
        source = Path(manifest['path'])
        original = Path.lstat
        for mode, attributes in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o600, 0x400)):
            def lstat(path):
                if path == source:
                    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                return original(path)
            with self.subTest(attributes=attributes), patch.object(Path, 'lstat', lstat):
                with self.assertRaisesRegex(InputError, '符号链接|目录联接'):
                    self.publish()
        self.assertFalse(self.fake.calls)

    def test_cli_working_directory_must_match_workspace_before_any_write(self):
        with patch.object(attachments.policy, 'WORKSPACE_ROOT', self.root / 'wrong'):
            with self.assertRaisesRegex(InputError, '工作目录'):
                self.publish()
        self.assertFalse(self.fake.calls)

    def test_scope_or_permission_error_does_not_grant_or_retry_mutation(self):
        self.fake.fail['create'] = 'before'
        with self.assertRaisesRegex(InputError, 'scope denied'):
            self.publish()
        before = self.writes()
        self.reload()
        with self.assertRaisesRegex(InputError, '禁止重复插入'):
            self.publish()
        self.assertEqual(self.writes(), before)
        self.assertEqual(self.record['report_attachments']['roles']['baseline']['state'], 'create_pending')

    def test_unknown_create_response_does_not_create_second_placeholder(self):
        self.fake.fail['create'] = 'after'
        with self.assertRaisesRegex(InputError, 'lost response'):
            self.publish()
        self.assertEqual(len(self.fake.blocks), 2)
        self.reload()
        with self.assertRaisesRegex(InputError, '无法唯一认领'):
            self.publish()
        self.assertEqual(self.writes(), {'create': 1, 'upload': 0, 'bind': 0})

    def test_saved_create_response_resumes_without_new_create(self):
        self.fake.fail['get'] = 'before'
        with self.assertRaises(InputError):
            self.publish()
        self.reload()
        self.assertIn('create_response', self.record['report_attachments']['roles']['baseline'])
        self.publish()
        self.assertEqual(self.writes(), {'create': 2, 'upload': 2, 'bind': 2})

    def test_unknown_upload_keeps_empty_block_and_never_reuploads(self):
        self.fake.fail['upload'] = 'after'
        with self.assertRaises(InputError):
            self.publish()
        before = self.writes()
        self.reload()
        with self.assertRaisesRegex(InputError, '禁止重复上传'):
            self.publish()
        self.assertEqual(self.writes(), before)
        self.assertEqual(len(self.fake.media), 1)
        self.assertEqual(self.record['report_attachments']['roles']['baseline']['state'], 'upload_pending')

    def test_unknown_bind_after_effect_is_recognized_by_full_readback(self):
        self.fake.fail['bind'] = 'after'
        with self.assertRaises(InputError):
            self.publish()
        self.reload()
        self.publish()
        self.assertEqual(self.writes(), {'create': 2, 'upload': 2, 'bind': 2})
        self.assertEqual(self.fake.counts['download'], 2)

    def test_unknown_bind_without_effect_is_blocked_without_blind_replay(self):
        self.fake.fail['bind'] = 'before'
        with self.assertRaises(InputError):
            self.publish()
        before = self.writes()
        self.reload()
        with self.assertRaisesRegex(InputError, '禁止重复上传'):
            self.publish()
        self.assertEqual(self.writes(), before)

    def test_readback_corruption_does_not_mark_ready(self):
        self.fake.bad_download = True
        with self.assertRaisesRegex(InputError, '字节不一致'):
            self.publish()
        self.assertEqual(self.record['report_attachments']['roles']['baseline']['state'], 'bind_pending')
        self.assertEqual(self.record['report_attachments']['roles']['replacement']['state'], 'prepared')
        self.fake.bad_download = False
        self.reload()
        self.publish()
        self.assertEqual(self.writes(), {'create': 2, 'upload': 2, 'bind': 2})

    def test_read_only_recovery_uses_all_pages_and_exact_name_and_hash(self):
        self.publish()
        entry = self.record['report_attachments']['roles']['baseline']
        entry['state'] = 'create_pending'
        for field in ('create_response', 'file_block_id', 'view_block_id', 'file_token'):
            entry.pop(field, None)
        self.fake.page_size = 1
        before = self.writes()
        self.publish()
        self.assertEqual(self.writes(), before)
        self.assertEqual(entry['state'], 'ready')
        self.assertEqual(self.fake.counts['list'], 4)

    def test_incomplete_pagination_is_not_treated_as_no_attachment(self):
        self.fake.fail['create'] = 'after'
        with self.assertRaises(InputError):
            self.publish()
        self.fake.bad_page = True
        self.reload()
        with self.assertRaisesRegex(InputError, '分页响应无效'):
            self.publish()
        self.assertEqual(self.fake.counts['create'], 1)

    def test_duplicate_remote_names_are_not_claimed(self):
        self.publish()
        entry = self.record['report_attachments']['roles']['baseline']
        duplicate = copy.deepcopy(self.fake.blocks[entry['file_block_id']])
        duplicate['block_id'] = 'duplicate'
        self.fake.blocks['duplicate'] = duplicate
        entry['state'] = 'create_pending'
        for field in ('create_response', 'file_block_id', 'view_block_id', 'file_token'):
            entry.pop(field, None)
        before = self.writes()
        with self.assertRaisesRegex(InputError, '无法唯一认领'):
            self.publish()
        self.assertEqual(self.writes(), before)

    def test_profile_app_report_and_request_binding_cannot_change_on_resume(self):
        self.publish()
        original_settings, original_record = copy.deepcopy(self.settings), copy.deepcopy(self.record)
        for key, value in (('report_lark_profile', 'other-bot'), ('expected_lark_app_id', 'cli_other'), ('lark_home', str(self.root / 'other-auth'))):
            self.settings = copy.deepcopy(original_settings)
            self.settings[key] = value
            with self.subTest(field=key), self.assertRaisesRegex(InputError, '绑定发生变化'):
                self.publish()
        self.settings = original_settings
        for key in ('sender_id', 'source_chat_id', 'source_message_id'):
            self.record = copy.deepcopy(original_record)
            self.record[key] = 'different'
            with self.subTest(field=key), self.assertRaisesRegex(InputError, '绑定发生变化'):
                self.publish()

    def test_verified_checkpoint_or_idempotency_key_tamper_rejected(self):
        self.publish()
        original = copy.deepcopy(self.record)
        entry = self.record['report_attachments']['roles']['baseline']
        entry['verified_sha256'] = '0' * 64
        with self.assertRaisesRegex(InputError, '就绪检查点'):
            self.publish()
        self.record = original
        entries = self.record['report_attachments']['roles']
        entries['replacement']['create_client_token'] = entries['baseline']['create_client_token']
        with self.assertRaisesRegex(InputError, '不能复用'):
            self.publish()

    def test_changed_remote_token_or_parent_is_not_overwritten(self):
        self.publish()
        entry = self.record['report_attachments']['roles']['baseline']
        block = self.fake.blocks[entry['file_block_id']]
        token = block['file']['token']
        before = self.writes()
        block['file']['token'] = 'another_token'
        with self.assertRaisesRegex(InputError, '素材已变化'):
            self.publish()
        block['file']['token'] = token
        block['parent_id'] = 'other_document'
        with self.assertRaisesRegex(InputError, '不属于预期父块'):
            self.publish()
        self.assertEqual(self.writes(), before)

    def test_business_error_inside_ok_envelope_is_failure(self):
        self.fake.business_error = True
        with self.assertRaisesRegex(InputError, '非零业务错误'):
            self.publish()
        self.assertEqual(self.fake.counts.get('upload', 0), 0)

    def test_user_identity_response_is_rejected(self):
        self.fake.response_identity = 'user'
        with self.assertRaisesRegex(InputError, 'Bot 身份'):
            self.publish()
        self.assertEqual(self.fake.counts.get('upload', 0), 0)

    def test_bot_profile_preflight_failure_has_no_remote_mutation(self):
        self.auth.side_effect = InputError('synthetic bot-only profile failed')
        with self.assertRaisesRegex(InputError, 'bot-only'):
            self.publish()
        self.assertFalse(self.fake.calls)

    def test_same_bytes_and_original_filename_still_have_role_unique_names(self):
        source = Path(self.result['full_workbooks']['baseline']['path']).read_bytes()
        replacement = self.result['full_workbooks']['replacement']
        Path(replacement['path']).write_bytes(source)
        replacement.update(sha256=hashlib.sha256(source).hexdigest(), bytes=len(source))
        roles = self.publish()
        self.assertNotEqual(roles['baseline']['artifact']['file_name'], roles['replacement']['artifact']['file_name'])
        self.assertNotEqual(roles['baseline']['file_token'], roles['replacement']['file_token'])

    def test_report_url_must_be_persisted_docx(self):
        for url in ('https://example.feishu.cn/sheets/synthetic', 'https://other.invalid/docx/fake',
                    'https://example.feishu.cn/docx/doxsynthetic?redirect=bad'):
            with self.subTest(url=url), self.assertRaises(InputError):
                attachments.ensure_attachments(self.settings, self.path, self.record, self.result, url)
        self.assertFalse(self.fake.calls)


if __name__ == '__main__':
    unittest.main()
