"""Run exactly one Windows analysis tree in a kill-on-close Job Object.

The launcher waits for a private named event. The parent assigns it to the Job
before releasing that event, so analysis cannot spawn descendants outside the
job during a post-Popen assignment race. This module changes no global process
state, sends no network request and never kills by executable name.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import uuid

POLICY = 'windows-analysis-job-object-v1'


class WindowsProcessError(RuntimeError):
    pass


def _api():
    if os.name != 'nt':
        raise WindowsProcessError('Windows process isolation requires a Windows host.')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    for name, args, result in [
        ('CreateJobObjectW', [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ('SetInformationJobObject', [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        ('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ('CreateEventW', [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ('OpenEventW', [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        ('SetEvent', [wintypes.HANDLE], wintypes.BOOL),
        ('WaitForSingleObject', [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ('CloseHandle', [wintypes.HANDLE], wintypes.BOOL),
    ]:
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


class _BasicLimits(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _Job:
    def __init__(self):
        self.api, self.handle = _api(), None

    def __enter__(self):
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise WindowsProcessError('Cannot create the private Windows analysis Job.')
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise WindowsProcessError('Cannot enforce kill-on-close on the analysis Job.')
        return self

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise WindowsProcessError('Cannot isolate the analysis process; no analysis was released.')

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise WindowsProcessError('Cannot terminate the private analysis Job.')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __exit__(self, *_):
        self.close()


class _Event:
    def __init__(self):
        self.api, self.handle = _api(), None
        self.name = 'Local\\walkthrough-native-' + uuid.uuid4().hex

    def __enter__(self):
        self.handle = self.api.CreateEventW(None, True, False, self.name)
        if not self.handle:
            raise WindowsProcessError('Cannot create the analysis release event.')
        return self

    def signal(self):
        if not self.api.SetEvent(self.handle):
            raise WindowsProcessError('Cannot release the isolated analysis process.')

    def __exit__(self, *_):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def run_windows_tree(argv, *, env, timeout, cwd=None):
    """subprocess.run-compatible return/timeout, bounded to this one Job."""
    if (not isinstance(argv, (tuple, list)) or not argv
            or any(not isinstance(x, str) or not x or '\x00' in x for x in argv)
            or type(timeout) not in (int, float) or timeout <= 0):
        raise WindowsProcessError('Invalid explicit analysis argv or timeout.')
    with _Job() as job, _Event() as event:
        command = [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                   '--release-event', event.name, '--', *argv]
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        assigned = False
        try:
            job.assign(process)
            assigned = True
            event.signal()
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            job.terminate()
            stdout, stderr = process.communicate(timeout=15)
            raise subprocess.TimeoutExpired(list(argv), timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            if assigned:
                job.terminate()
            else:
                # The event has not been released, so the launcher has no analysis children.
                process.terminate()
            process.communicate(timeout=15)
            raise


def _launcher(event_name, argv):
    api = _api()
    handle = api.OpenEventW(0x00100000, False, event_name)  # SYNCHRONIZE
    if not handle:
        return 125
    try:
        if api.WaitForSingleObject(handle, 60000) != 0:
            return 125
    finally:
        api.CloseHandle(handle)
    # All descendants inherit membership in the parent's Job. No breakaway flag.
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
                               creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
    return process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-event', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command or not args.release_event.startswith('Local\\walkthrough-native-'):
        return 125
    return _launcher(args.release_event, command)


if __name__ == '__main__':
    raise SystemExit(main())
