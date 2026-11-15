"""Create independent settings from explicitly selected local identity/config sources."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from product_version import validate_config as validate_product_config
from delivery_config import validate_delivery_config
from request_model import InputError

IDENTITY_FIELDS = ('lark_cli','lark_home','project_id','lark_profile','report_lark_profile',
                   'expected_lark_app_id','auth_credential_replica_path','auth_expected_cli_version',
                   'report_parent_token','report_parent_position','report_secure_label_name')


def configure(args):
    environment = getattr(args, 'environment', 'prod_then_test')
    if environment not in ('prod_then_test', 'test_then_prod', 'test', 'prod'):
        raise ValueError('环境策略必须为 prod_then_test、test_then_prod、test 或 prod。')
    output = args.output.expanduser().resolve()
    cloud_path = output.with_name('walkthrough-version-cloud.settings.json')
    if output == cloud_path:
        raise ValueError('独立 Skill 配置和车云凭据配置不能使用同一个文件。')
    if output.exists() or cloud_path.exists():
        raise ValueError('走查配置已存在；请先检查，工具不会覆盖现有配置。')
    identity = json.loads(args.identity_settings.read_text(encoding='utf-8-sig'))
    cloud = json.loads(args.cloud_settings.read_text(encoding='utf-8-sig'))
    username = str(cloud.get('username') or '').strip()
    if not username:
        raise ValueError('所选车云配置没有 username。')
    workspace = args.workspace_root.expanduser().resolve()
    settings = {key:identity[key] for key in IDENTITY_FIELDS if key in identity}
    settings['expected_operator_open_id'] = identity.get('expected_operator_open_id') or identity.get('expected_dispatch_open_id')
    settings.update(workspace_root=str(workspace),
                    runtime_state_dir=str(workspace/'data/walkthrough-test-version/runtime'),
                    cloud_settings_path=str(cloud_path),project_isolation_required=True,
                    product_version={'query_chat_id':args.query_chat_id,
                                     'asgard_bot_id':args.asgard_bot_id,
                                     'timeout_seconds':args.product_version_timeout_seconds,
                                     'poll_interval_seconds':args.product_version_poll_interval_seconds},
                    local_analysis={'environment':environment,'backend':'native_full',
                                    'execution':args.execution,'distribution':args.distribution,
                                    'python':args.analyzer_python,'analyzer_root':args.analyzer_root,
                                    'timeout_seconds':7200,'window_workers':2,'keep_raw_logs':False})
    settings['report_delivery'] = {'format':'native_sheets','include_images':False}
    if args.execution == 'windows':
        settings['local_analysis']['windows_git_bin'] = getattr(args,'windows_git_bin',None)
        from native_windows_host import validate_windows_config
        validate_windows_config(settings['local_analysis'])
    if getattr(args,'native_template_token',None):
        settings['report_delivery']['native_template_spreadsheet_token'] = args.native_template_token
    reference_reader = getattr(args, 'native_template_reference_reader', 'bot')
    bridge_manifest = getattr(args, 'native_template_bridge_manifest', None)
    if reference_reader != 'bot':
        settings['report_delivery']['native_template_reference_reader'] = reference_reader
    if bridge_manifest is not None:
        if not bridge_manifest.is_absolute():
            raise ValueError('模板准备凭据必须使用本工作区中的绝对路径。')
        settings['report_delivery']['native_template_bridge_manifest'] = str(bridge_manifest)
    validate_delivery_config(settings)
    if args.asgard_app_id:
        settings['product_version']['asgard_app_id'] = args.asgard_app_id
    settings['product_version'] = validate_product_config(settings)
    if args.rules_json:
        settings['local_analysis']['rules_json'] = str(args.rules_json.expanduser().resolve())
    else:
        settings['local_analysis']['rules_spreadsheet_token'] = args.rules_token
    output.parent.mkdir(parents=True,exist_ok=True)
    cloud_path.write_text(json.dumps({'username':username},ensure_ascii=False,indent=2),encoding='utf-8')
    output.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'status':'configured','settings':str(output),'cloud_settings':str(cloud_path),
            'note':'未启动任务或修改来源配置；需继续运行 preflight 检查。'}


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('workspace-root','identity-settings','cloud-settings','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--environment', choices=('prod_then_test', 'test_then_prod', 'test', 'prod'),
                        default='prod_then_test',
                        help='每窗独立选择；默认 prod 全空后查询 test，也支持 test→prod 或固定环境；查询异常即停止。')
    parser.add_argument('--execution',choices=('wsl','linux','windows'),default='wsl')
    parser.add_argument('--distribution',default='Ubuntu')
    parser.add_argument('--analyzer-python',required=True,help='所选执行环境内实际分析解释器的绝对路径；Windows须为本地.exe。')
    parser.add_argument('--analyzer-root',required=True,help='所选执行环境内最新版外置源码的绝对路径。')
    parser.add_argument('--windows-git-bin',help='仅Windows模式：已验Git for Windows的usr/bin本地绝对路径，不自动安装或修改PATH。')
    parser.add_argument('--query-chat-id',required=True,
                        help='固定版本查询群的 oc_ ID；只用于查询，不从请求来源群推断。')
    parser.add_argument('--asgard-bot-id',required=True,
                        help='部署环境中已确认的阿斯加德机器人 ou_ ID。')
    parser.add_argument('--asgard-app-id',
                        help='可选的阿斯加德稳定 App ID，cli_ 开头；与项目应用身份分别配置。')
    parser.add_argument('--native-template-token',
                        help='可选的原生明细模板副本；不填时使用获准分析器自身模板，填写时须通过布局与占位兼容核验。')
    parser.add_argument('--native-template-reference-reader', choices=('bot', 'fixed_user'), default='bot',
                        help='默认Bot读取源模板；fixed_user只读源引用，最终报告仍由Bot创建，必须同时指定中继token及ready凭据。')
    parser.add_argument('--native-template-bridge-manifest', type=Path,
                        help='仅fixed_user模式：本工作区data/walkthrough-template-bridge内已验证ready凭据的绝对路径。')
    parser.add_argument('--product-version-timeout-seconds',type=float,default=120,
                        help='版本查询超时秒数，默认 120。')
    parser.add_argument('--product-version-poll-interval-seconds',type=float,default=2,
                        help='版本查询轮询间隔秒数，默认 2。')
    rules=parser.add_mutually_exclusive_group(required=True)
    rules.add_argument('--rules-json',type=Path)
    rules.add_argument('--rules-token')
    return parser


def main():
    args=build_parser().parse_args()
    try:
        print(json.dumps(configure(args),ensure_ascii=False))
        return 0
    except (InputError,ValueError,OSError):
        print(json.dumps({'status':'failed','error':'配置创建失败；检查路径、配置完整性及是否已存在，来源凭据未输出。'},ensure_ascii=False))
        return 1

if __name__=='__main__':
    raise SystemExit(main())
