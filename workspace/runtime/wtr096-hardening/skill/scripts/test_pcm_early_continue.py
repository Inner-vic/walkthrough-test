"""Offline invariants for skipping unrelated PCM log lines."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import state_performance as pcm


class PcmEarlyContinueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    @staticmethod
    def line(milliseconds, text):
        second, fraction = divmod(milliseconds, 1000)
        return f'+0800 2026-10-21-14_01_{second:02d}_{fraction:03d} 42 9 I infer_expert_pcm {text}\n'

    def extract(self, records):
        (self.root / 'AVM_Service-synthetic').write_text(''.join(records), encoding='utf-8')
        return pcm.extract(self.root, '2026-10-21T14:00:00', '2026-10-21T14:10:00')

    def test_unrelated_lines_keep_coverage_without_parsing_time_or_pid(self):
        records = [self.line(0, 'ordinary health heartbeat'), 'invalid time ordinary message\n']
        with patch.object(pcm, 'parse_log_time', side_effect=AssertionError('unrelated timestamp work')), \
                patch.object(pcm, 'parse_pid', side_effect=AssertionError('unrelated PID work')):
            result = self.extract(records)
        self.assertEqual(result['coverage']['lines_read'], 2)
        self.assertEqual(result['coverage']['records_without_timestamp'], 0)
        self.assertEqual(result['coverage']['records_outside_window'], 0)
        self.assertTrue(all(field['status'] == 'unparsed' for field in result['field_presence'].values()))

    def test_noise_does_not_change_sequence_values_metrics_or_diagnostics(self):
        records = [
            self.line(0, 'ordinary noise'),
            'invalid timestamp perception_state=0\n',
            'invalid timestamp unrelated noise\n',
            '+0800 2026-10-21-13_59_59_000 42 9 I infer_expert_pcm sys_cpu=50.0%\n',
            '+0800 2026-10-21-13_59_59_000 42 9 I infer_expert_pcm unrelated noise\n',
            self.line(1, 'UroadPreNode::run() frame=1 perception_state=0 ctx=1'),
            self.line(1, 'ordinary noise between frame events'),
            self.line(2, 'UroadPreNode::run() timing(ms) frame=1 ctx=1'),
            self.line(2, 'ordinary noise between frame events'),
            self.line(3, 'UroadPostNode::run() timing(ms) frame=2 ctx=1'),
            self.line(4, 'perception_state=0 infer_expert_pc_cpu=38.0% sys_cpu=50.0%'),
            self.line(5, 'FPSlast10frames: 8.0 Hz'),
            self.line(6, 'ordinary trailing noise'),
            '+0800 2026-10-21-14_10_00_000 42 9 I infer_expert_pcm perception_state=0\n',
        ]
        with patch.object(pcm, '_event', wraps=pcm._event) as events:
            result = self.extract(records)
        self.assertEqual([call.args[1] for call in events.call_args_list], [6, 8, 10, 12])
        self.assertEqual(result['coverage']['lines_read'], 14)
        self.assertEqual(result['coverage']['records_without_timestamp'], 1)
        self.assertEqual(result['coverage']['records_outside_window'], 2)
        self.assertEqual(result['metrics']['slow_driver']['pcm_p99_infer_delay_ms'], {'p99': 2.0, 'count': 1})
        self.assertEqual(result['metrics']['slow_driver']['pcm_cpu_percent'], {'mean': 2.0, 'count': 1})
        self.assertEqual(result['metrics']['slow_driver']['system_cpu_percent'], {'mean': 50.0, 'count': 1})
        self.assertEqual(result['frame_diagnostics']['ctx_validated_direct'], 1)
        self.assertEqual(result['fps_diagnostics']['no_following_post'], 1)
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['observations'], 1)
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['numeric_samples'], 0)

    def test_state_only_line_remains_relevant_to_timestamp_diagnostics(self):
        with patch.object(pcm, 'parse_log_time', wraps=pcm.parse_log_time) as parse:
            result = self.extract(['invalid timestamp perception_state=4\n'])
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(result['coverage']['records_without_timestamp'], 1)

    def test_unparsed_numeric_marker_still_preserves_invalid_fps_diagnostic(self):
        result = self.extract([self.line(0, 'FPSlast10frames: unknown Hz')])
        self.assertEqual(result['coverage']['fps_samples_invalid'], 1)
        self.assertEqual(result['field_presence']['pcm_frame_rate_hz']['status'], 'unparsed')


if __name__ == '__main__':
    unittest.main()
