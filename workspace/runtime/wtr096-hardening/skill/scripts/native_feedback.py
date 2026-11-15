"""Coalesced ordinary source replies; progress failures never fail analysis."""
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import threading
import time

MESSAGES={
    'queued':'任务正在排队，执行服务会按容量启动。',
    'waiting_capacity':'任务正在等待解析资源，已有数据会保留。',
    'selecting_task_environment':'正在分别查询两段日志并确认数据环境。',
    'processing_windows':'正在获取并解析两段日志。',
    'metrics_ready':'两段日志统计已完成，正在生成报告。',
    'analyzing':'正在解析两段日志。',
    'delivering':'报告已生成，正在完成权限检查和发送。',
}


def business_status(record):
    status=record.get('status')
    if status=='completed':return '报告已发送。'
    if status=='failed':return str(record.get('error') or '任务未完成，诊断信息已保留。')
    if status in ('queued','waiting_capacity'):return MESSAGES[status]
    stage=record.get('analysis_task_stage') or record.get('analysis_stage') or status
    if record.get('execution_stage') in ('create_report','report_bot_authorization','source_access','restrict_collaborator_management'):
        return MESSAGES['delivering']
    if isinstance(stage,str) and ':' in stage:stage=stage.split(':',1)[1]
    if stage in MESSAGES:return MESSAGES[stage]
    if status=='queued':return MESSAGES['queued']
    if status=='waiting_capacity':return MESSAGES['waiting_capacity']
    return MESSAGES['analyzing']


@contextmanager
def monitor(path,settings,policy,*,initial_delay=45,interval=120):
    stop=threading.Event();path=Path(path);feedback=path.with_suffix('.feedback.json')
    def run():
        if stop.wait(initial_delay):return
        last_text=None;sequence=0
        while not stop.is_set():
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                if record.get('status') in ('completed','failed'):return
                text=business_status(record)
                if text!=last_text:
                    sequence+=1
                    value={'version':'walkthrough-feedback-v1','sequence':sequence,'pending':True,
                           'source_message_id':record['source_message_id'],'text':text}
                    if feedback.exists():
                        previous=json.loads(feedback.read_text(encoding='utf-8'))
                        if previous.get('pending') is True:return
                        sequence=max(sequence,previous.get('sequence',0)+1);value['sequence']=sequence
                    policy.atomic_json(feedback,value)
                    receipt=policy.run_lark(settings,['im','+messages-reply','--as','bot',
                        '--message-id',record['source_message_id'],'--msg-type','text',
                        '--content',json.dumps({'text':text},ensure_ascii=False),'--idempotency-key',
                        hashlib.sha256(f"{record['job_key']}:progress:{sequence}".encode()).hexdigest()[:32]],
                        profile=settings['report_lark_profile'],timeout=15)
                    data=receipt.get('data') or {};message_id=data.get('message_id') or (data.get('message') or {}).get('message_id')
                    if (receipt.get('identity')!='bot' or not isinstance(message_id,str)
                            or not message_id.startswith('om_') or message_id==record['source_message_id']):return
                    value.update(pending=False,message_id=message_id,sent_at=datetime.now(timezone.utc).isoformat())
                    policy.atomic_json(feedback,value);last_text=text
            except Exception:
                return
            if stop.wait(interval):return
    thread=threading.Thread(target=run,name='walkthrough-feedback',daemon=True);thread.start()
    def close():
        stop.set();thread.join(timeout=35)
        if thread.is_alive():raise RuntimeError('进度发送尚未结束；报告已保留，未并发发送终态结果。')
    try:yield close
    finally:close()
