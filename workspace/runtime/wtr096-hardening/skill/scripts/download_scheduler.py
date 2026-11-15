"""Bounded file concurrency with a shared streaming byte limit per input set."""
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from threading import Lock, Event

from request_model import InputError


def download_workers(settings):
    value = settings.get('local_analysis', {}).get('download_workers', 4)
    if type(value) is not int or not 1 <= value <= 4:
        raise InputError('local_analysis.download_workers 必须是 1 至 4 的整数。')
    return value


class DownloadBudget:
    def __init__(self, maximum):
        if type(maximum) is not int or maximum <= 0:
            raise InputError('下载总字节上限必须为正整数。')
        self.maximum = maximum
        self.used = 0
        self._lock = Lock()

    def consume(self, count):
        with self._lock:
            if type(count) is not int or count < 0 or self.used + count > self.maximum:
                raise InputError('下载输入超过总字节上限。')
            self.used += count

    def release(self, count):
        with self._lock:
            if type(count) is not int or not 0 <= count <= self.used:
                raise InputError('下载字节计数无效。')
            self.used -= count


def run_bounded(items, function, workers):
    """Return original order; stop queued work and never return partial success."""
    if type(workers) is not int or not 1 <= workers <= 4:
        raise InputError('文件下载并发数必须为 1 至 4。')
    if workers == 1:
        return [function(item) for item in items]
    stop = Event()

    def guarded(item):
        if stop.is_set():
            return None
        try:
            return function(item)
        except Exception:
            stop.set()
            raise

    results = [None] * len(items)
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(guarded, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            try:
                results[futures[future]] = future.result()
            except CancelledError as error:
                if not future.cancelled():
                    errors.append(error)
            except Exception as error:
                errors.append(error)
                stop.set()
                for pending in futures:
                    pending.cancel()
    if errors:
        raise errors[0]
    return results
