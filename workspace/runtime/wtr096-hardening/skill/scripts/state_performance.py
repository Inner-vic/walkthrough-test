#!/usr/bin/env python3
"""Extract per-scenario CPU, PCM E2E P99, and PCM frame-rate metrics.

Only AVM_Service files from an approved ``log_fsdA_service`` download are
accepted. PCM latency is measured from a successful UroadPreNode start to the
paired UroadPostNode timing line, with PID/frame/ctx validation. PCM FPS uses
the logged 10-frame window only when all ten paired frames share one state.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


CHINA_TZ = timezone(timedelta(hours=8))
FRAME_STATE_MAX_AGE_SECONDS = 2.0  # Retained only for CLI/API compatibility.
MAX_E2E_LATENCY_MS = 5000.0
SCENARIOS = (
    ("slow_driver", "低速人驾", 0),
    ("fast_driver", "高速人驾", 4),
    ("parking", "泊车", 1),
    ("CNOA", "CNOA", 5),
)
TARGET_STATES = {state for _, _, state in SCENARIOS}
REPORT_FIELDS = ('pcm_cpu_percent', 'system_cpu_percent', 'pcm_p99_infer_delay_ms', 'pcm_frame_rate_hz')
INFERENCE_NODES = {"lidar", "img", "classifier", "transformer", "dit"}

ACTUAL_TIMESTAMP_RE = re.compile(
    r"\+0800 (?P<date>\d{4}-\d\d-\d\d)-(?P<h>\d\d)_(?P<m>\d\d)_(?P<s>\d\d)_(?P<ms>\d{3})"
)
PROCESS_RE = re.compile(r"\s+(?P<pid>\d+)\s+\d+\s+\S+\s+infer_expert_pcm\b")
STATE_RE = re.compile(r"perception_state=(\d+)")
PCM_CPU_RE = re.compile(r"infer_expert_pc_cpu=([\d.]+)%")
SYSTEM_CPU_RE = re.compile(r"sys_cpu=([\d.]+)%")
PRE_START_RE = re.compile(r"UroadPreNode::run\(\)\s+frame=(\d+)")
PRE_TIMING_RE = re.compile(r"UroadPreNode::run\(\)\s+timing\(ms\)\s+frame=(\d+)")
POST_TIMING_RE = re.compile(r"UroadPostNode::run\(\)\s+timing\(ms\)\s+frame=(\d+)")
CTX_RE = re.compile(r"\bctx=(\d+)\b")
INFERENCE_RE = re.compile(
    r"\[UnifiedInferenceNode_(?P<branch>low|high)_(?P<node>lidar|img|classifier|transformer|dit)\]"
    r"\s+runWithContext\s+ctx=(?P<ctx>\d+)\b"
)
FPS_RE = re.compile(r"FPSlast10frames:\s*([\d.]+)\s*Hz")
FPS_MARKER_RE = re.compile(r"FPSlast10frames:")


class InputError(ValueError):
    """Raised when an extraction request cannot be interpreted safely."""


def parse_request_time(value: str) -> datetime:
    """Parse an input boundary, treating timezone-free values as China time."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InputError("时间必须是 YYYY-MM-DD HH:MM:SS 或 ISO-8601 格式。") from exc
    return parsed.replace(tzinfo=CHINA_TZ) if parsed.tzinfo is None else parsed.astimezone(CHINA_TZ)


def parse_log_time(line: str) -> datetime | None:
    match = ACTUAL_TIMESTAMP_RE.search(line)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("date")[:4]), int(match.group("date")[5:7]), int(match.group("date")[8:10]),
            int(match.group("h")), int(match.group("m")), int(match.group("s")),
            int(match.group("ms")) * 1000, tzinfo=CHINA_TZ,
        )
    except ValueError:
        return None


def parse_pid(line: str) -> int:
    timestamp = ACTUAL_TIMESTAMP_RE.search(line)
    match = PROCESS_RE.match(line, timestamp.end()) if timestamp else None
    return int(match.group("pid")) if match else 0


def numeric(match: re.Match[str] | None) -> float | None:
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def nearest_rank_p99(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(0.99 * len(ordered))
    return round(ordered[rank - 1], 4)


def candidate_files(log_dir: Path) -> list[Path]:
    """Return only AVM service files; netbw and unrelated logs are excluded."""
    if not log_dir.is_dir():
        raise InputError("日志目录不存在或不是目录。")
    files = [path for path in log_dir.rglob("AVM_Service-*") if path.is_file()]
    return sorted(path for path in files if "netbw-" not in path.name.casefold())


def _event(timestamp: datetime, sequence: int, pid: int, line: str, match=None) -> dict:
    ctx = CTX_RE.search(line)
    return {
        "timestamp": timestamp,
        "sequence": sequence,
        "key": (timestamp, sequence),
        "pid": pid,
        "line": line,
        "frame": int(match.group(1)) if match else None,
        "ctx": int(ctx.group(1)) if ctx else None,
    }


def _latest_before(events: list[dict], key: tuple) -> dict | None:
    for item in reversed(events):
        if item["key"] <= key:
            return item
    return None


def _first_unused_after(events: list[dict], key: tuple, used: set[int]) -> dict | None:
    for item in events:
        if item["key"] > key and item["sequence"] not in used:
            return item
    return None


def _validated_ctx(pre_start: dict, pre_timing: dict, post_timing: dict,
                   inference_events: list[dict], inference_keys: list[tuple]) -> tuple[bool, str]:
    pre_direct = {value for value in (pre_start.get("ctx"), pre_timing.get("ctx")) if value is not None}
    direct = pre_direct | ({post_timing["ctx"]} if post_timing.get("ctx") is not None else set())
    if pre_direct and post_timing.get("ctx") is not None:
        return (len(direct) == 1, "direct" if len(direct) == 1 else "mismatch")

    lower = bisect.bisect_right(inference_keys, pre_timing["key"])
    upper = bisect.bisect_left(inference_keys, post_timing["key"])
    grouped: dict[tuple[str, int], set[str]] = defaultdict(set)
    for item in inference_events[lower:upper]:
        grouped[(item["branch"], item["ctx"])].add(item["node"])
    completed = [identity for identity, nodes in grouped.items() if INFERENCE_NODES <= nodes]
    if len(completed) != 1:
        return False, "unvalidated"
    inferred_ctx = completed[0][1]
    if direct and inferred_ctx not in direct:
        return False, "mismatch"
    return True, "inference_sequence"


def build_metrics(cpu_samples: dict[int, list[tuple[float, float]]],
                  latency_samples: dict[int, list[float]],
                  pcm_fps_samples: dict[int, list[float]]) -> dict:
    result = {}
    for key, label, state in SCENARIOS:
        cpu = cpu_samples[state]
        latency = latency_samples[state]
        fps = pcm_fps_samples[state]
        pcm_fps = {"mean": mean(fps), "count": len(fps)}
        result[key] = {
            "label": label,
            "perception_state": state,
            "pcm_cpu_percent": {"mean": mean([item[0] for item in cpu]), "count": len(cpu)},
            "system_cpu_percent": {"mean": mean([item[1] for item in cpu]), "count": len(cpu)},
            "pcm_p99_infer_delay_ms": {"p99": nearest_rank_p99(latency), "count": len(latency)},
            "pcm_frame_rate_hz": pcm_fps,
            # Compatibility alias; this now uses strict ten-frame attribution.
            "frame_rate_hz": dict(pcm_fps),
        }
    return result


def extract(log_dir: str | Path, start: str, end: str, *,
            frame_state_max_age_seconds: float = FRAME_STATE_MAX_AGE_SECONDS) -> dict:
    """Produce four-scenario performance metrics for one exact ``[start, end)`` window."""
    start_time, end_time = parse_request_time(start), parse_request_time(end)
    if end_time <= start_time:
        raise InputError("结束时间必须晚于开始时间。")
    if not math.isfinite(frame_state_max_age_seconds) or frame_state_max_age_seconds < 0:
        raise InputError("帧率状态关联窗口必须是非负有限秒数。")

    files = candidate_files(Path(log_dir))
    cpu_samples: dict[int, list[tuple[float, float]]] = defaultdict(list)
    latency_samples: dict[int, list[float]] = defaultdict(list)
    pcm_fps_samples: dict[int, list[float]] = defaultdict(list)
    pre_starts: dict[tuple[int, int], list[dict]] = defaultdict(list)
    pre_timings: list[dict] = []
    post_timings: list[dict] = []
    inference_by_pid: dict[int, list[dict]] = defaultdict(list)
    fps_events: list[dict] = []
    field_observations = {field: 0 for field in REPORT_FIELDS}
    coverage = {
        "files_read": len(files), "lines_read": 0, "records_outside_window": 0,
        "records_without_timestamp": 0, "complete_cpu_samples": 0,
        "complete_cpu_samples_ignored_state": 0, "latency_samples": 0,
        "fps_samples": 0, "fps_samples_invalid": 0,
        "fps_samples_unattributed": 0, "fps_samples_ignored_state": 0,
    }
    frame_diagnostics = {
        "pre_timing_without_start": 0, "post_missing": 0,
        "ctx_validated_direct": 0, "ctx_validated_inference_sequence": 0,
        "ctx_mismatch": 0, "ctx_unvalidated": 0,
        "latency_non_positive": 0, "latency_over_limit": 0,
    }
    fps_diagnostics = {
        "no_following_post": 0, "post_frame_not_multiple_of_10": 0,
        "first_window": 0, "missing_frames": 0, "mixed_state": 0,
    }

    sequence = 0
    for path in files:
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                sequence += 1
                coverage["lines_read"] += 1
                pre_start_match = PRE_START_RE.search(line)
                pre_timing_match = PRE_TIMING_RE.search(line)
                post_timing_match = POST_TIMING_RE.search(line)
                inference_match = INFERENCE_RE.search(line)
                fps_marker = FPS_MARKER_RE.search(line)
                has_relevant_field = bool(
                    STATE_RE.search(line) or PCM_CPU_RE.search(line) or SYSTEM_CPU_RE.search(line)
                    or pre_start_match or pre_timing_match or post_timing_match or inference_match or fps_marker
                )
                # Unrelated lines cannot produce samples or diagnostics below.
                # Keep the original line/sequence counts before this fast path.
                if not has_relevant_field:
                    continue
                timestamp = parse_log_time(line)
                if timestamp is None:
                    if has_relevant_field:
                        coverage["records_without_timestamp"] += 1
                    continue
                if timestamp < start_time or timestamp >= end_time:
                    if has_relevant_field:
                        coverage["records_outside_window"] += 1
                    continue

                pid = parse_pid(line)
                state_match = STATE_RE.search(line)
                state = int(state_match.group(1)) if state_match else None
                pcm_raw, system_cpu = numeric(PCM_CPU_RE.search(line)), numeric(SYSTEM_CPU_RE.search(line))
                if pcm_raw is not None:
                    field_observations["pcm_cpu_percent"] += 1
                if system_cpu is not None:
                    field_observations["system_cpu_percent"] += 1
                if state is not None and pcm_raw is not None and system_cpu is not None:
                    coverage["complete_cpu_samples"] += 1
                    if state in TARGET_STATES:
                        cpu_samples[state].append((pcm_raw / 19.0, system_cpu))
                    else:
                        coverage["complete_cpu_samples_ignored_state"] += 1

                if pre_start_match and not pre_timing_match:
                    item = _event(timestamp, sequence, pid, line, pre_start_match)
                    item["state"] = state
                    pre_starts[(pid, item["frame"])].append(item)
                if pre_timing_match:
                    pre_timings.append(_event(timestamp, sequence, pid, line, pre_timing_match))
                if post_timing_match:
                    post_timings.append(_event(timestamp, sequence, pid, line, post_timing_match))
                if inference_match:
                    item = _event(timestamp, sequence, pid, line)
                    item.update(
                        branch=inference_match.group("branch"), node=inference_match.group("node"),
                        ctx=int(inference_match.group("ctx")),
                    )
                    inference_by_pid[pid].append(item)
                if fps_marker:
                    fps = numeric(FPS_RE.search(line))
                    if fps is None:
                        coverage["fps_samples_invalid"] += 1
                    else:
                        coverage["fps_samples"] += 1
                        field_observations["pcm_frame_rate_hz"] += 1
                        item = _event(timestamp, sequence, pid, line)
                        item["fps"] = fps
                        fps_events.append(item)

    for events in pre_starts.values():
        events.sort(key=lambda item: item["key"])
    pre_timings.sort(key=lambda item: item["key"])
    post_timings.sort(key=lambda item: item["key"])
    fps_events.sort(key=lambda item: item["key"])
    for events in inference_by_pid.values():
        events.sort(key=lambda item: item["key"])

    post_by_frame: dict[tuple[int, int], list[dict]] = defaultdict(list)
    posts_by_pid: dict[int, list[dict]] = defaultdict(list)
    for item in post_timings:
        post_by_frame[(item["pid"], item["frame"])].append(item)
        posts_by_pid[item["pid"]].append(item)
    used_posts: set[int] = set()
    paired_posts: dict[int, list[dict]] = defaultdict(list)

    inference_keys_by_pid = {
        pid: [item["key"] for item in events] for pid, events in inference_by_pid.items()
    }
    for pre_timing in pre_timings:
        pid, pre_frame = pre_timing["pid"], pre_timing["frame"]
        pre_start = _latest_before(pre_starts[(pid, pre_frame)], pre_timing["key"])
        if pre_start is None:
            frame_diagnostics["pre_timing_without_start"] += 1
            continue
        post = _first_unused_after(post_by_frame[(pid, pre_frame + 1)], pre_timing["key"], used_posts)
        if post is None:
            frame_diagnostics["post_missing"] += 1
            continue
        used_posts.add(post["sequence"])
        pair = {"post": post, "state": pre_start.get("state"), "pre_start": pre_start, "pre_timing": pre_timing}
        paired_posts[pid].append(pair)
        # The complete parsed timing chain is evidence even if ctx or latency
        # validation later rejects its value. Isolated tags are not evidence.
        if pid > 0 and pre_start.get("state") is not None:
            field_observations["pcm_p99_infer_delay_ms"] += 1

        valid_ctx, ctx_method = _validated_ctx(
            pre_start, pre_timing, post, inference_by_pid[pid], inference_keys_by_pid.get(pid, [])
        )
        if not valid_ctx:
            frame_diagnostics["ctx_mismatch" if ctx_method == "mismatch" else "ctx_unvalidated"] += 1
            continue
        frame_diagnostics[f"ctx_validated_{ctx_method}"] += 1
        latency_ms = (post["timestamp"] - pre_start["timestamp"]).total_seconds() * 1000.0
        if latency_ms <= 0:
            frame_diagnostics["latency_non_positive"] += 1
            continue
        if latency_ms > MAX_E2E_LATENCY_MS:
            frame_diagnostics["latency_over_limit"] += 1
            continue
        state = pre_start.get("state")
        if state in TARGET_STATES:
            latency_samples[state].append(latency_ms)
            coverage["latency_samples"] += 1

    for pairs in paired_posts.values():
        pairs.sort(key=lambda item: item["post"]["key"])

    post_keys_by_pid = {pid: [item["key"] for item in items] for pid, items in posts_by_pid.items()}
    paired_by_post_sequence = {
        pair["post"]["sequence"]: pair for pairs in paired_posts.values() for pair in pairs
    }
    for fps_event in fps_events:
        pid = fps_event["pid"]
        posts = posts_by_pid.get(pid, [])
        index = bisect.bisect_right(post_keys_by_pid.get(pid, []), fps_event["key"])
        if index >= len(posts):
            fps_diagnostics["no_following_post"] += 1
            coverage["fps_samples_unattributed"] += 1
            continue
        target_post = posts[index]
        target_frame = target_post["frame"]
        if target_frame % 10:
            fps_diagnostics["post_frame_not_multiple_of_10"] += 1
            coverage["fps_samples_unattributed"] += 1
            continue
        if target_frame == 10:
            fps_diagnostics["first_window"] += 1
            coverage["fps_samples_unattributed"] += 1
            continue

        expected = list(range(target_frame - 9, target_frame + 1))
        window_pairs = []
        for post in posts[max(0, index - 9):index + 1]:
            pair = paired_by_post_sequence.get(post["sequence"])
            if pair:
                window_pairs.append(pair)
        if len(window_pairs) != 10 or [item["post"]["frame"] for item in window_pairs] != expected:
            fps_diagnostics["missing_frames"] += 1
            coverage["fps_samples_unattributed"] += 1
            continue
        states = {item.get("state") for item in window_pairs}
        if len(states) != 1:
            fps_diagnostics["mixed_state"] += 1
            coverage["fps_samples_unattributed"] += 1
            continue
        state = states.pop()
        if state not in TARGET_STATES:
            coverage["fps_samples_ignored_state"] += 1
            continue
        pcm_fps_samples[state].append(fps_event["fps"])

    metrics = build_metrics(cpu_samples, latency_samples, pcm_fps_samples)
    field_presence = {}
    for field in REPORT_FIELDS:
        numeric_samples = sum(row[field]["count"] for row in metrics.values())
        observations = max(field_observations[field], numeric_samples)
        field_presence[field] = {
            "status": "parsed" if observations else "unparsed",
            "observations": observations,
            "numeric_samples": numeric_samples,
            "scope": "requested_window",
            "evidence": "parsed_timing_chain_or_samples" if field == "pcm_p99_infer_delay_ms" else "parsed_numeric_field",
        }
    populated = sum(
        bool(row[metric]["count"])
        for row in metrics.values()
        for metric in ("pcm_cpu_percent", "system_cpu_percent", "pcm_p99_infer_delay_ms", "pcm_frame_rate_hz")
    )
    return {
        "status": "ok" if populated == len(SCENARIOS) * 4 else "partial" if populated else "failed",
        "time_window": {
            "start": start_time.isoformat(timespec="seconds"),
            "end": end_time.isoformat(timespec="seconds"),
            "semantics": "[start, end)",
        },
        "frame_state_max_age_seconds": frame_state_max_age_seconds,
        "max_e2e_latency_ms": MAX_E2E_LATENCY_MS,
        "metrics": metrics,
        "field_presence": field_presence,
        "coverage": coverage,
        "ctx_validation": {
            "direct": frame_diagnostics["ctx_validated_direct"],
            "inference_sequence": frame_diagnostics["ctx_validated_inference_sequence"],
        },
        "frame_diagnostics": frame_diagnostics,
        "fps_diagnostics": fps_diagnostics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", required=True, help="含 AVM_Service-* 文件的 log_fsdA_service 解压目录")
    parser.add_argument("--start", required=True, help="统计开始时间，含边界")
    parser.add_argument("--end", required=True, help="统计结束时间，不含边界")
    parser.add_argument("--frame-state-max-age-seconds", type=float, default=FRAME_STATE_MAX_AGE_SECONDS,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = extract(args.log_dir, args.start, args.end, frame_state_max_age_seconds=args.frame_state_max_age_seconds)
    except (InputError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
