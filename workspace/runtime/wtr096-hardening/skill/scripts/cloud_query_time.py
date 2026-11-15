"""Bind car-cloud textual collection times to the documented UTC+08 epoch fields."""
from datetime import datetime, timedelta, timezone
import re

SHANGHAI = timezone(timedelta(hours=8))
TEXT_KEYS = ('collectTimeMin', 'collectTimeMax')
MS_KEYS = ('collectTimeMinMs', 'collectTimeMaxMs')


def bind_collect_time_ms(params):
    """Copy parameters; reject contradictory bounds rather than silently changing them."""
    if not isinstance(params, dict):
        raise ValueError('车云查询参数必须是对象。')
    result = dict(params)
    present = [key in result for key in TEXT_KEYS]
    if not any(present):
        if any(key in result for key in MS_KEYS):
            raise ValueError('车云毫秒时间必须与文本时间成对绑定。')
        return result
    if not all(present):
        raise ValueError('车云查询起止时间必须成对提供。')
    epochs = []
    for text_key, ms_key in zip(TEXT_KEYS, MS_KEYS):
        text = result[text_key]
        if not isinstance(text, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', text):
            raise ValueError('车云文本时间须为上海时区的完整秒级日期时间。')
        text = text.replace('T', ' ')
        result[text_key] = text
        try:
            date = datetime.strptime(text, '%Y-%m-%d %H:%M:%S').replace(tzinfo=SHANGHAI)
        except ValueError:
            raise ValueError('车云文本时间不是有效日期时间。') from None
        epoch = int(date.timestamp()) * 1000
        if ms_key in result:
            existing = result[ms_key]
            if type(existing) is int:
                value = existing
            elif isinstance(existing, str) and re.fullmatch(r'-?\d+', existing):
                value = int(existing)
            else:
                raise ValueError('车云毫秒时间须为整数。')
            if value != epoch:
                raise ValueError('车云文本时间与毫秒时间不一致。')
        result[ms_key] = epoch
        epochs.append(epoch)
    if epochs[1] < epochs[0]:
        raise ValueError('车云查询结束时间早于开始时间。')
    return result
