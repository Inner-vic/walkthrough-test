"""Bounded synthetic input-normalization tests; never touch actual logs."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import zstandard
import native_inputs as inputs


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'input';self.source.mkdir()
        self.destination=self.root/'normalized'
        self.manifest={'request':{'window':'synthetic'},'environment':'testtwo','files':[]}

    def add(self,name,payload,kind='log_xcu_mcu',*,compressed=True):
        path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(zstandard.ZstdCompressor().compress(payload) if compressed else payload)
        row={'relative_path':name,'sha256':inputs.sha(path),'bytes':path.stat().st_size,'log_class':kind}
        self.manifest['files'].append(row)
        return path

    def prepare(self):
        return inputs.prepare_native_inputs(self.source,self.destination,self.manifest)

    def checkpoint(self):
        return self.destination/'normalized-manifest.json'

    def test_multiframe_utf8_streaming_and_unchanged_inputs(self):
        # The first byte of a multibyte character lands at a 1 MiB read boundary.
        prefix=b'x'*(1024**2-1)
        body=prefix+'中\n'.encode('utf-8')
        original=self.add('xcu/XVM_MCU-synthetic.zst',body)
        original.write_bytes(original.read_bytes()+zstandard.ZstdCompressor().compress('第二帧\n'.encode('utf-8')))
        self.manifest['files'][0].update(sha256=inputs.sha(original),bytes=original.stat().st_size)
        plain=self.add('fsda/synthetic.zip',b'not parsed at normalization boundary','log_fsdA',compressed=False)
        source_hashes={p:inputs.sha(p) for p in (original,plain)}
        result=self.prepare()
        self.assertEqual((self.destination/'xcu/XVM_MCU-synthetic.log').read_bytes(),body+'第二帧\n'.encode('utf-8'))
        self.assertEqual((self.destination/'fsda/synthetic.zip').read_bytes(),plain.read_bytes())
        self.assertEqual(result['xvm_expanded_bytes'],len(body)+len('第二帧\n'.encode('utf-8')))
        self.assertEqual([r['normalization'] for r in result['files']],['xvm_zstd_utf8_text','unchanged'])
        self.assertEqual(source_hashes,{p:inputs.sha(p) for p in (original,plain)})

    def test_verified_cache_does_not_decompress_again(self):
        self.add('xcu/XVM_MCU-synthetic.zst',b'synthetic log\n')
        expected=self.prepare()
        with patch.object(zstandard,'ZstdDecompressor',side_effect=AssertionError('cache decompressed again')):
            self.assertEqual(self.prepare(),expected)

    def test_input_hash_change_is_rejected(self):
        path=self.add('xcu/XVM_MCU-synthetic.zst',b'first')
        path.write_bytes(zstandard.ZstdCompressor().compress(b'changed'))
        with self.assertRaisesRegex(ValueError,'哈希'):self.prepare()
        self.assertFalse(self.checkpoint().exists())

    def test_cached_output_hash_change_and_missing_file_are_rejected(self):
        self.add('xcu/XVM_MCU-synthetic.zst',b'first')
        value=self.prepare();target=self.destination/value['files'][0]['relative_path']
        target.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'缓存|归整'):self.prepare()
        target.unlink()
        with self.assertRaisesRegex(ValueError,'缓存|归整'):self.prepare()

    def test_cache_binding_changes_with_environment_or_request(self):
        self.add('xcu/XVM_MCU-synthetic.zst',b'first');self.prepare()
        for key,value in [('environment','prod'),('request',{'window':'other'})]:
            altered=copy.deepcopy(self.manifest);altered[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'当前输入'):
                inputs.prepare_native_inputs(self.source,self.destination,altered)

    def test_cached_output_path_escape_is_rejected(self):
        self.add('xcu/XVM_MCU-synthetic.zst',b'first');value=self.prepare()
        outside=self.root/'outside.log';outside.write_bytes(b'synthetic')
        value['files'][0].update(relative_path='../outside.log',sha256=inputs.sha(outside))
        self.checkpoint().write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'缓存'):self.prepare()
        self.assertEqual(outside.read_bytes(),b'synthetic')

    def test_source_relative_and_absolute_path_escape_are_rejected(self):
        outside=self.root/'outside.log';outside.write_bytes(b'synthetic')
        for relative in ('../outside.log',str(outside.resolve())):
            manifest={**self.manifest,'files':[{'relative_path':relative,'sha256':inputs.sha(outside),'log_class':'log_fsdA'}]}
            with self.subTest(relative=relative),self.assertRaisesRegex(ValueError,'身份/哈希'):
                inputs.prepare_native_inputs(self.source,self.destination,manifest)
        self.assertEqual(outside.read_bytes(),b'synthetic')

    def test_destination_must_be_separate_from_source(self):
        self.add('xcu/XVM_MCU-synthetic.zst',b'first')
        for destination in (self.source,self.source/'child'):
            with self.subTest(destination=destination),self.assertRaisesRegex(ValueError,'独立'):
                inputs.prepare_native_inputs(self.source,destination,self.manifest)

    def test_binary_invalid_utf8_and_empty_expansion_rejected(self):
        for index,payload in enumerate((b'abc\x00def',b'\xff\xfe\x01',b'')):
            with self.subTest(payload=payload):
                self.manifest['files']=[]
                self.add(f'xcu/XVM_MCU-{index}.zst',payload)
                destination=self.root/f'bad-{index}'
                with self.assertRaises(ValueError):inputs.prepare_native_inputs(self.source,destination,self.manifest)
                self.assertFalse((destination/'normalized-manifest.json').exists())
                self.assertEqual(list(destination.rglob('*.partial')),[])

    def test_decompression_limit_is_cumulative(self):
        self.add('xcu/XVM_MCU-one.zst',b'123456')
        self.add('bzcu/XVM_BZCU-two.zst',b'abcdef','log_bzcu_eth')
        with patch.object(inputs,'MAX_EXPANDED_BYTES',10),self.assertRaisesRegex(ValueError,'上限'):
            self.prepare()
        self.assertFalse(self.checkpoint().exists())
        self.assertEqual(list(self.destination.rglob('*.partial')),[])

    def test_unknown_compressed_name_or_class_is_rejected(self):
        for index,(name,kind) in enumerate((('unknown.zst','log_xcu_mcu'),('XVM_MCU-name.zst','log_fsdA'))):
            self.manifest['files']=[];self.add(name,b'first',kind)
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'已核实'):
                inputs.prepare_native_inputs(self.source,self.root/f'unknown-{index}',self.manifest)

    def test_incomplete_destination_and_target_collision_are_rejected(self):
        self.destination.mkdir();(self.destination/'unfinished.log').write_bytes(b'keep diagnostic')
        self.add('xcu/XVM_MCU-synthetic.zst',b'first')
        with self.assertRaisesRegex(ValueError,'未完成'):self.prepare()
        self.assertEqual((self.destination/'unfinished.log').read_bytes(),b'keep diagnostic')
        self.add('xcu/XVM_MCU-synthetic.log',b'second','log_xcu_mcu',compressed=False)
        with self.assertRaisesRegex(ValueError,'重名'):
            inputs.prepare_native_inputs(self.source,self.root/'collision',self.manifest)

    def test_empty_input_does_not_create_success_checkpoint(self):
        with self.assertRaises(ValueError):self.prepare()
        self.assertFalse(self.checkpoint().exists())

    def test_cache_cannot_drop_a_verified_source_record(self):
        self.add('xcu/XVM_MCU-one.zst',b'first')
        self.add('bzcu/XVM_BZCU-two.zst',b'second','log_bzcu_eth')
        value=self.prepare();value['files'].pop()
        self.checkpoint().write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):self.prepare()

    def test_cache_rejects_forged_original_hash_class_and_size(self):
        self.add('xcu/XVM_MCU-one.zst',b'first');original=self.prepare()
        for key,value in [('original_sha256','0'*64),('log_class','log_fsdB'),('bytes',999),
                          ('normalization','unchanged')]:
            changed=copy.deepcopy(original);changed['files'][0][key]=value
            self.checkpoint().write_text(json.dumps(changed),encoding='utf-8')
            with self.subTest(key=key),self.assertRaises(ValueError):self.prepare()

    def test_cache_rejects_duplicate_or_renamed_entries(self):
        self.add('xcu/XVM_MCU-one.zst',b'first');original=self.prepare()
        duplicate=copy.deepcopy(original);duplicate['files'].append(copy.deepcopy(duplicate['files'][0]))
        self.checkpoint().write_text(json.dumps(duplicate),encoding='utf-8')
        with self.assertRaises(ValueError):self.prepare()
        renamed=copy.deepcopy(original)
        old=self.destination/renamed['files'][0]['relative_path'];new=old.with_name('renamed.log')
        old.rename(new);renamed['files'][0]['relative_path']=new.relative_to(self.destination).as_posix()
        self.checkpoint().write_text(json.dumps(renamed),encoding='utf-8')
        with self.assertRaises(ValueError):self.prepare()

    def test_unchanged_input_cache_hash_cannot_be_rebased_to_new_content(self):
        self.add('fsda/synthetic.zip',b'original archive','log_fsdA',compressed=False)
        value=self.prepare();target=self.destination/value['files'][0]['relative_path']
        target.unlink();target.write_bytes(b'forged replacement')
        value['files'][0].update(sha256=inputs.sha(target),bytes=target.stat().st_size)
        self.checkpoint().write_text(json.dumps(value),encoding='utf-8')
        with self.assertRaises(ValueError):self.prepare()

    def test_extra_normalized_file_is_rejected(self):
        self.add('xcu/XVM_MCU-one.zst',b'first');self.prepare()
        (self.destination/'extra.log').write_bytes(b'unbound data')
        with self.assertRaisesRegex(ValueError,'额外|归整'):self.prepare()

    @staticmethod
    def integrity_frame():
        # Enough blocks for a truncated frame to yield a substantial valid UTF-8
        # prefix. A nonempty decoder output does not establish frame integrity.
        payload=b''.join(f'{index:08d} synthetic state0 latency={index%997}\n'.encode()
                         for index in range(50000))
        return zstandard.ZstdCompressor(write_checksum=True).compress(payload)

    def assert_compressed_rejected(self,data,case,*,allow_incomplete_frames=False):
        self.manifest['files']=[]
        self.add(f'xcu/XVM_MCU-{case}.zst',data,compressed=False)
        destination=self.root/case
        with self.assertRaises((ValueError,zstandard.ZstdError)):
            inputs.prepare_native_inputs(self.source,destination,self.manifest,allow_incomplete_frames=allow_incomplete_frames)
        self.assertFalse((destination/'normalized-manifest.json').exists())
        self.assertEqual(list(destination.rglob('*.partial')),[])

    def test_truncated_frame_with_valid_utf8_prefix_is_rejected(self):
        self.assert_compressed_rejected(self.integrity_frame()[:-20],'truncated-data')

    def test_missing_checksum_bytes_are_rejected(self):
        frame=self.integrity_frame()
        for count in (1,4):
            with self.subTest(removed=count):
                self.assert_compressed_rejected(frame[:-count],f'checksum-missing-{count}')

    def test_complete_first_frame_does_not_hide_truncated_second_frame(self):
        frame=self.integrity_frame()
        self.assert_compressed_rejected(frame+frame[:-20],'truncated-second-frame')

    def test_corrupt_checksum_and_trailing_garbage_are_rejected(self):
        frame=self.integrity_frame()
        for name,data in [('bad-checksum',frame[:-1]+bytes([frame[-1]^1])),('trailing-garbage',frame+b'garbage')]:
            with self.subTest(case=name):self.assert_compressed_rejected(data,name)

    def recover(self,compressed):
        self.add('xcu/XVM_MCU-recovery.zst',compressed,compressed=False)
        return inputs.prepare_native_inputs(self.source,self.destination,self.manifest,allow_incomplete_frames=True)

    @staticmethod
    def unchecked_prefix(compressed):
        # Independent reference for bytes available before EOF; this deliberately
        # uses the old permissive reader only to specify the expected prefix.
        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(compressed),read_across_frames=True) as stream:
            return stream.read()

    def test_opt_in_recovery_keeps_only_complete_decoded_lines_and_audit(self):
        compressed=self.integrity_frame()[:-20]
        decoded=self.unchecked_prefix(compressed);expected=decoded[:decoded.rfind(b'\n')+1]
        result=self.recover(compressed);row=result['files'][0]
        self.assertEqual((self.destination/row['relative_path']).read_bytes(),expected)
        self.assertEqual(result['version'],'native-input-v2')
        self.assertTrue(result['policy']['allow_incomplete_frames'])
        self.assertFalse(row['frame_complete'])
        self.assertEqual(row['recovery'],'complete_decoded_lines')
        self.assertEqual(row['discarded_tail_bytes'],len(decoded)-len(expected))
        self.assertEqual(result['xvm_expanded_bytes'],len(decoded))
        self.assertEqual(result['quality'],{'status':'partial','issues':[{
            'kind':'incomplete_zstd_frame','relative_path':row['relative_path'],
            'source_sha256':self.manifest['files'][0]['sha256'],
            'recovery':'complete_decoded_lines','discarded_tail_bytes':len(decoded)-len(expected)}]})
        self.assertEqual(inputs.sha(self.source/self.manifest['files'][0]['relative_path']),row['original_sha256'])
        with patch.object(zstandard,'ZstdDecompressor',side_effect=AssertionError('cached recovery must not decompress')):
            self.assertEqual(inputs.prepare_native_inputs(self.source,self.destination,self.manifest,allow_incomplete_frames=True),result)

    def test_recovery_mode_leaves_complete_frame_and_tail_line_unchanged(self):
        payload='完整行\n末行没有换行'.encode('utf-8')
        result=self.recover(zstandard.ZstdCompressor(write_checksum=True).compress(payload))
        row=result['files'][0]
        self.assertEqual((self.destination/row['relative_path']).read_bytes(),payload)
        self.assertEqual(result['quality'],{'status':'ok','issues':[]})
        self.assertTrue(row['frame_complete']);self.assertIsNone(row['recovery'])
        self.assertEqual(row['discarded_tail_bytes'],0)

    def test_recovery_preserves_complete_first_frame_and_complete_second_frame_lines(self):
        first=b'first frame complete line\n'
        compressed=zstandard.ZstdCompressor(write_checksum=True).compress(first)+self.integrity_frame()[:-20]
        decoded=self.unchecked_prefix(compressed);expected=decoded[:decoded.rfind(b'\n')+1]
        result=self.recover(compressed)
        actual=(self.destination/result['files'][0]['relative_path']).read_bytes()
        self.assertEqual(actual,expected);self.assertTrue(actual.startswith(first))
        self.assertEqual(len(result['quality']['issues']),1)

    def test_recovery_discards_unterminated_line_with_split_utf8_tail(self):
        good='第一行\n第二行\n'.encode('utf-8')
        tail=b'unfinished-'+ '中'.encode('utf-8')[:2]
        compressed=zstandard.ZstdCompressor(write_checksum=True).compress(good+tail)[:-4]
        result=self.recover(compressed);row=result['files'][0]
        self.assertEqual((self.destination/row['relative_path']).read_bytes(),good)
        self.assertEqual(row['discarded_tail_bytes'],len(tail))
        self.assertEqual(result['quality']['status'],'partial')

    def test_recovery_does_not_require_discard_when_only_checksum_is_missing(self):
        payload=b'complete line\n'
        result=self.recover(zstandard.ZstdCompressor(write_checksum=True).compress(payload)[:-1])
        self.assertEqual(result['quality']['status'],'partial')
        self.assertEqual(result['files'][0]['discarded_tail_bytes'],0)

    def test_recovery_requires_at_least_one_complete_line(self):
        frame=zstandard.ZstdCompressor(write_checksum=True).compress(b'unterminated line')
        self.assert_compressed_rejected(frame[:-4],'recovery-no-lines',allow_incomplete_frames=True)

    def test_recovery_still_rejects_checksum_format_utf8_nul_empty_and_limits(self):
        frame=self.integrity_frame()
        cases=[('recovery-checksum',frame[:-1]+bytes([frame[-1]^1])),
               ('recovery-garbage',frame+b'garbage')]
        for name,payload in [('utf8',b'valid line\ninvalid\xff'),('nul',b'valid line\n\x00'),('empty',b'')]:
            cases.append(('recovery-'+name,zstandard.ZstdCompressor(write_checksum=True).compress(payload)[:-4]))
        for name,data in cases:
            with self.subTest(case=name):self.assert_compressed_rejected(data,name,allow_incomplete_frames=True)
        data=zstandard.ZstdCompressor(write_checksum=True).compress(b'1234567890123456\n')[:-4]
        with patch.object(inputs,'MAX_EXPANDED_BYTES',10):
            self.assert_compressed_rejected(data,'recovery-limit',allow_incomplete_frames=True)

    def test_cache_cannot_cross_strict_and_recovery_policies(self):
        self.recover(self.integrity_frame()[:-20])
        with self.assertRaisesRegex(ValueError,'策略'):
            self.prepare()
        other=self.root/'strict-valid';complete=zstandard.ZstdCompressor().compress(b'complete\n')
        self.manifest['files']=[];self.add('xcu/XVM_MCU-complete.zst',complete,compressed=False)
        inputs.prepare_native_inputs(self.source,other,self.manifest)
        with self.assertRaisesRegex(ValueError,'策略'):
            inputs.prepare_native_inputs(self.source,other,self.manifest,allow_incomplete_frames=True)

    def test_cache_recovery_quality_and_row_audit_cannot_be_removed_or_changed(self):
        original=self.recover(self.integrity_frame()[:-20])
        mutations=[lambda v:v['quality'].update(issues=[]),
                   lambda v:v['quality'].update(status='ok'),
                   lambda v:v['files'][0].update(frame_complete=True,recovery=None,discarded_tail_bytes=0),
                   lambda v:v['files'][0].update(discarded_tail_bytes=v['files'][0]['discarded_tail_bytes']+1),
                   lambda v:v['quality']['issues'][0].update(source_sha256='0'*64),
                   lambda v:v.pop('quality'),lambda v:v.pop('audit_sha256')]
        for index,mutate in enumerate(mutations):
            changed=copy.deepcopy(original);mutate(changed)
            self.checkpoint().write_text(json.dumps(changed),encoding='utf-8')
            with self.subTest(change=index),self.assertRaises(ValueError):
                inputs.prepare_native_inputs(self.source,self.destination,self.manifest,allow_incomplete_frames=True)

    def test_recovery_policy_requires_explicit_boolean(self):
        self.add('xcu/XVM_MCU-one.zst',b'complete\n')
        for value in ('true',1,None):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'布尔'):
                inputs.prepare_native_inputs(self.source,self.destination,self.manifest,allow_incomplete_frames=value)


if __name__=='__main__':
    unittest.main()
