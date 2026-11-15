"""Bounded cleanup of one native window; CLI deletes only ROOT/analysis."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import stat

TARGETS = frozenset(('input', 'native-prepared', 'analysis'))


def _io_path(path):
    """Windows filesystem APIs need extended paths for deep native archives."""
    value = str(path)
    if os.name != 'nt' or value.startswith('\\\\?\\'):
        return value
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def _resolved(path, *, strict):
    value = str(Path(_io_path(path)).resolve(strict=strict))
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def _no_links(path):
    for item in (path, *path.parents):
        try:
            info = os.lstat(_io_path(item))
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
            raise ValueError('清理路径禁止符号链接或目录联接。')


def validated_target(root, name):
    """Validate absolute root and a fixed direct child before any deletion."""
    root = Path(root)
    if name not in TARGETS or not root.is_absolute() or '..' in root.parts:
        raise ValueError('清理仅允许绝对窗口根目录下的固定目标。')
    _no_links(root)
    resolved_root = _resolved(root, strict=True)
    if (resolved_root.parent == resolved_root or resolved_root != root
            or not stat.S_ISDIR(os.stat(_io_path(resolved_root)).st_mode)):
        raise ValueError('清理根目录无效，不能使用磁盘根目录或非目录。')
    target = resolved_root / name
    _no_links(target)
    resolved_target = _resolved(target, strict=False)
    if resolved_target != target or resolved_target.parent != resolved_root:
        raise ValueError('清理目标解析后超出窗口目录。')
    try:
        info = os.lstat(_io_path(target))
    except FileNotFoundError:
        return target
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('清理目标必须是目录。')
    return target


def remove_target(root, name):
    target = validated_target(root, name)
    if not os.path.lexists(_io_path(target)):
        return False
    # Refuse links/reparse points anywhere in the tree before rmtree. Native
    # workers are stopped and the caller holds the task lock during cleanup.
    def fail(error):
        raise error
    for current, dirs, files in os.walk(_io_path(target), followlinks=False, onerror=fail):
        for name in (*dirs, *files):
            info = os.lstat(os.path.join(current, name))
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)):
                raise ValueError('清理目标内含链接或目录联接，保留现场。')
    # Recheck the precise root/target after enumeration; never delete root.
    validated_target(root, target.name)
    shutil.rmtree(_io_path(target))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        target = validated_target(args.root, 'analysis')
        removed = remove_target(args.root, 'analysis')
        print(json.dumps({'status':'cleaned','target':str(target),'removed':removed}))
        return 0
    except (ValueError, OSError):
        print(json.dumps({'status':'failed','error':'Native analysis cleanup validation or deletion failed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
