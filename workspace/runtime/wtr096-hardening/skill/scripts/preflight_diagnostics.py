"""Finite preflight errors; never inspect raw exceptions, paths or auth payloads."""

PREFLIGHT_REASONS = {
    'settings': frozenset({'check_failed'}),
    'host_deps': frozenset({'check_failed', 'host_runtime_unsupported',
                          'host_dependency_unavailable', 'host_dependency_version_mismatch'}),
    'cloud_config': frozenset({'check_failed'}),
    'cloud_identity': frozenset({'check_failed'}),
    'rules': frozenset({'check_failed'}),
    'product_identity': frozenset({'check_failed', 'product_configuration_invalid',
        'fixed_identity_not_configured', 'identity_reader_unavailable', 'auth_status_unavailable',
        'auth_status_invalid', 'project_app_missing', 'project_app_mismatch',
        'user_identity_unavailable', 'user_identity_mismatch', 'user_token_status_invalid',
        'user_token_expiry_unavailable', 'user_token_refresh_required',
        'user_token_refresh_failed', 'user_reauthorization_required'}),
    'native': frozenset({'check_failed'}),
    'delivery': frozenset({'check_failed'}),
    'preflight': frozenset({'check_failed'}),
}


def _valid(stage, reason):
    return (type(stage) is str and stage in PREFLIGHT_REASONS and type(reason) is str
            and reason in PREFLIGHT_REASONS[stage])


class PreflightError(ValueError):
    """A reason is valid only for its explicitly assigned read-only stage."""
    def __init__(self, stage, reason):
        if not _valid(stage, reason):
            raise ValueError('Invalid bounded preflight failure')
        self.stage = stage
        self.reason = reason
        super().__init__(preflight_failure_message(preflight_failure_diagnostic(self)))


def preflight_failure_diagnostic(error, *, stage=None):
    """Ignore exception text, args, payloads and chains, even for typed errors."""
    fallback = stage if type(stage) is str and stage in PREFLIGHT_REASONS else 'preflight'
    result = {'category': 'walkthrough_preflight_failed', 'stage': fallback, 'reason': 'check_failed'}
    if isinstance(error, PreflightError):
        known_stage, reason = getattr(error, 'stage', None), getattr(error, 'reason', None)
        if _valid(known_stage, reason):
            result.update(stage=known_stage, reason=reason)
    return result


def preflight_failure_message(diagnostic):
    """Compose public text only from exact stage/reason enum values."""
    stage, reason = diagnostic.get('stage'), diagnostic.get('reason')
    if not _valid(stage, reason):
        stage, reason = 'preflight', 'check_failed'
    specific = {
        'host_runtime_unsupported': '走查专用运行环境不符合要求。',
        'host_dependency_unavailable': '走查专用依赖无法载入。',
        'host_dependency_version_mismatch': '走查专用依赖版本不符合要求。',
        'product_configuration_invalid': '制品查询配置预检失败。',
        'fixed_identity_not_configured': '制品查询未配置完整的固定应用和用户身份。',
        'identity_reader_unavailable': '固定身份的本地读取工具不可用。',
        'auth_status_unavailable': '无法读取固定用户的本地认证状态。',
        'auth_status_invalid': '固定用户的本地认证状态格式无法确认。',
        'project_app_missing': '本地认证状态缺少项目应用身份。',
        'project_app_mismatch': '本地认证状态的项目应用身份不一致。',
        'user_identity_unavailable': '本地认证状态缺少可确认的固定用户身份。',
        'user_identity_mismatch': '本地认证状态的固定用户身份不一致。',
        'user_token_status_invalid': '固定用户的访问凭据状态无法确认。',
        'user_token_expiry_unavailable': '固定用户的访问凭据到期时间无法确认。',
        'user_token_refresh_required': '固定用户访问凭据已过期，需要先续期；本地预检未执行续期操作。',
        'user_token_refresh_failed': '固定用户访问凭据续期未完成。',
        'user_reauthorization_required': '固定用户授权需要重新完成授权。',
    }
    if reason in specific:
        return specific[reason]
    return {
        'settings': '走查独立配置预检失败。',
        'host_deps': '走查专用运行环境或依赖预检失败。',
        'cloud_config': '车云环境配置预检失败。',
        'cloud_identity': '车云本地查询凭据配置预检失败。',
        'rules': '完整统计规则配置预检失败。',
        'product_identity': '制品查询的固定身份预检失败。',
        'native': '原生分析环境预检失败。',
        'delivery': '报告交付配置预检失败。',
        'preflight': '走查本地预检失败。',
    }[stage]


def preflight_failure_fields(error, *, stage=None):
    diagnostic = preflight_failure_diagnostic(error, stage=stage)
    return {'error': preflight_failure_message(diagnostic), 'error_diagnostic': diagnostic}
