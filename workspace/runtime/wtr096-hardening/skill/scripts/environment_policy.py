"""Environment names and selection policy; no network or credential access."""
from request_model import InputError

PROVIDERS = {'test': 'testtwo', 'prod': 'prod'}
DEFAULT_POLICY = 'prod_then_test'
POLICY_ORDERS = {'prod_then_test': ('prod', 'test'),
                 'test_then_prod': ('test', 'prod'),
                 'test': ('test',), 'prod': ('prod',)}


def provider(environment):
    if not isinstance(environment, str) or environment not in PROVIDERS:
        raise InputError('日志环境必须是 test 或 prod。')
    return PROVIDERS[environment]


def policy(settings):
    value = settings.get('local_analysis', {}).get('environment', DEFAULT_POLICY)
    if not isinstance(value, str) or value not in POLICY_ORDERS:
        raise InputError('local_analysis.environment 必须是 prod_then_test、test_then_prod、test 或 prod。')
    return value


def candidates(settings):
    value = policy(settings)
    return POLICY_ORDERS[value]
