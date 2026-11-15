"""AVM format adapter: selection/decompression only, no vehicle network calls.

The original PCM/CPU metric implementation remains in state_performance.py.
service_log_source prepares files; cloud_pipeline coordinates the phases.
"""
from __future__ import annotations
import os
import uuid
from pathlib import Path
from cloud_log_client import ordered_files

FILE_PREFIX = "AVM_Service-"

FILE_SUFFIX = ".zst"

MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

DECOMPRESS_CHUNK_BYTES = 1024 * 1024

def filter_avm_files(items: list[dict]) -> list[dict]:
    """Select only AVM service archives and explicitly exclude netbw logs."""
    selected = []
    for item in items:
        name = str(item.get("fileName") or item.get("name") or "")
        folded = name.casefold()
        if name.startswith(FILE_PREFIX) and folded.endswith(FILE_SUFFIX) and "netbw-" not in folded:
            selected.append(item)
    return ordered_files(selected)

def decompress_zst(source: Path, destination: Path) -> None:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise RuntimeError("缺少 zstandard 运行依赖，无法解压 AVM 日志。") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex}.part")
    try:
        with source.open("rb") as compressed, temporary.open("xb") as output:
            reader = zstd.ZstdDecompressor().stream_reader(compressed)
            total = 0
            with reader:
                while True:
                    chunk = reader.read(DECOMPRESS_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DECOMPRESSED_BYTES:
                        raise RuntimeError("AVM 日志解压后超过单文件大小限制。")
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
