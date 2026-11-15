"""Finite native failure protocol; private exceptions stay in diagnostics."""
from __future__ import annotations

VERSION = 'native-analysis-failure-v1'
MESSAGES = {
    'input_validation': '日志来源、归档或输入完整性校验未通过，已停止解析。',
    'native_execution': '完整统计执行或结果校验未通过，已停止生成报告。',
}


def failure(stage):
    if stage not in MESSAGES:
        raise ValueError('未知原生分析失败阶段。')
    return {'version': VERSION, 'stage': stage, 'message': MESSAGES[stage]}


def describe(payload):
    if not isinstance(payload, dict) or payload.get('status') != 'failed':
        return None
    value = payload.get('failure')
    if not isinstance(value, dict) or value.get('version') != VERSION:
        return None
    stage = value.get('stage')
    if stage not in MESSAGES or value != failure(stage):
        return None
    return MESSAGES[stage]
