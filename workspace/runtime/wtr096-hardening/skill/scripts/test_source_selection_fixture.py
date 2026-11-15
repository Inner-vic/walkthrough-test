"""Synthetic selection receipts shared by host integration/cache tests."""
import hashlib
import json

from native_selection import (POLICY, validate_excluded_basenames,
                              EXTRACT_LOGS_AST_SHA256, PATCHED_EXTRACT_LOGS_AST_SHA256)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def synthetic_selection_audit(mapping=None, excluded_basenames=()):
    mapping = dict(mapping or {'vla_synthetic_21.log': 'log_fsdA'})
    excluded = validate_excluded_basenames(excluded_basenames)
    ledger = []
    for name in excluded:
        group = [other for other in excluded if other.casefold() == name.casefold()]
        source_names = group if len(group) > 1 else group * 2
        variants = [{'name': value, 'source': 'log_fsdB' if index % 2 else 'log_fsdA',
                     'sha256': ('e' if index % 2 else 'd') * 64,
                     'bytes': 2 if index % 2 else 1, 'origin': 'synthetic-' + str(index)}
                    for index, value in enumerate(source_names)]
        ledger.append({'basename': name, 'reason': 'outside_source_aware_full_rule_selection', 'variants': variants})
    members = {name.casefold(): [['d' * 64, 1]] for name in mapping}
    members.update({name.casefold(): [['d' * 64, 1], ['e' * 64, 2]] for name in excluded})
    inventory = {
        'log_count': len(members), 'expanded_bytes': len(mapping) + 3 * len(set(name.casefold() for name in excluded)),
        'selection_sha256': 'c' * 64, 'selection_policy': POLICY,
        'source_by_basename': dict(mapping), 'selected_log_count': len(mapping),
        'selected_members': {name: {'sha256': 'd' * 64, 'bytes': 1, 'source': source}
                             for name, source in mapping.items()},
        'ignored_conflicts': [], 'excluded_conflicts': ledger,
        'basename_hashes': members,
    }
    return {
        'policy': POLICY, 'source_by_basename': mapping,
        'source_map_sha256': _digest(mapping),
        'source_count': len(set(mapping.values())), 'mapped_log_count': len(mapping),
        'legacy_source_used': 'legacy' in mapping.values(), 'ast_replacements': 3,
        'excluded_basenames': excluded, 'excluded_basenames_sha256': _digest(excluded),
        'original_extract_logs_ast_sha256': EXTRACT_LOGS_AST_SHA256,
        'patched_extract_logs_ast_sha256': PATCHED_EXTRACT_LOGS_AST_SHA256,
        'input_inventory': inventory, 'input_inventory_sha256': _digest(inventory),
    }


def attach_synthetic_execution(report, rules):
    """Explicit mock of an external-process boundary, never a real replay proof."""
    from test_analyzer_contract import synthetic_contract_audit
    from test_runtime_audit import synthetic_runtime_execution
    from native_execution_compat import expected_execution_compat_audit
    from test_native_execution_compat import synthetic_runtime_audit
    report['analyzer_contract']=synthetic_contract_audit()
    report['execution_compat']=expected_execution_compat_audit()
    report['execution_compat_runtime']=synthetic_runtime_audit(
        report['metadata']['time_range'],report['analyzer_contract']['contract_fingerprint'])
    report['runtime_execution']=synthetic_runtime_execution(
        inventory=report['source_selection']['input_inventory'],
        request_time_range=report['metadata']['time_range'],rules=rules)
    return report
