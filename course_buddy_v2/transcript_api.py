import json
import re
from datetime import timedelta
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .replay_api import get_video_detail, get_video_list, get_video_platform_token

VIDEO_BASE = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu"


def _safe_filename(value: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", value).strip()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_since_date(since: Optional[str]) -> Optional[str]:
    if not since:
        return None
    try:
        amount = int(since[:-1])
        unit = since[-1].lower()
    except Exception:
        return None

    now = datetime.now()
    if unit == "d":
        start = now - timedelta(days=amount)
    elif unit == "w":
        start = now - timedelta(weeks=amount)
    elif unit == "m":
        start = now - timedelta(days=30 * amount)
    else:
        return None
    return start.strftime("%Y-%m-%d")


def sort_replays(videos: list[dict]) -> list[dict]:
    return sorted(videos, key=lambda item: (item.get("courseBeginTime") or "", item.get("videoName") or ""))


def filter_replays_since(videos: list[dict], since: Optional[str]) -> list[dict]:
    since_date = _parse_since_date(since)
    if not since_date:
        return sort_replays(videos)
    return [
        item
        for item in sort_replays(videos)
        if (item.get("courseBeginTime") or "")[:10] >= since_date
    ]


def _pick_replay(
    videos: list[dict],
    *,
    latest: bool,
    index: Optional[int],
    cour_id: Optional[int],
    since: Optional[str],
) -> dict:
    if cour_id is not None:
        for item in videos:
            if item.get("courId") == cour_id:
                return item
        raise RuntimeError(f"没有找到 courId={cour_id} 对应的回放。")

    sorted_videos = filter_replays_since(videos, since)
    if not sorted_videos:
        raise RuntimeError("筛选后没有可用回放。")
    if latest:
        return sorted_videos[-1]
    if index is not None:
        return sorted_videos[index]
    raise RuntimeError("需要提供 --latest、--index 或 --cour-id。")


def fetch_transcript_bundle(
    *,
    course_id: str,
    course_name: str,
    oc_cookies: dict,
    root_dir: str,
    latest: bool = False,
    index: Optional[int] = None,
    cour_id: Optional[int] = None,
    since: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    if progress:
        progress("正在进行 Canvas/LTI 认证...")
    access_token, canvas_course_id, session = get_video_platform_token(course_id, oc_cookies)
    if progress:
        progress("正在获取课程回放列表...")
    videos = get_video_list(access_token, canvas_course_id, session)
    replay = _pick_replay(videos, latest=latest, index=index, cour_id=cour_id, since=since)
    if progress:
        progress(f"已选中回放：{replay.get('videoName')}，正在获取回放详情...")
    detail = get_video_detail(replay["videoId"], access_token, session)
    target_course_id = replay["courId"]
    headers = {"token": access_token}

    if progress:
        progress("正在下载 transcript JSON...")
    transcript_response = session.post(
        f"{VIDEO_BASE}/transfer/translate/detail",
        json={"courseId": target_course_id, "platform": 1},
        headers=headers,
        timeout=30,
    )
    transcript_response.raise_for_status()
    transcript_payload = transcript_response.json()

    if progress:
        progress("正在下载平台 summary JSON...")
    summary_response = session.post(
        f"{VIDEO_BASE}/course/summary/canvas/detail",
        json={"courseId": target_course_id, "platform": 1},
        headers=headers,
        timeout=30,
    )
    summary_response.raise_for_status()
    summary_payload = summary_response.json()

    replay_title = replay.get("videoName") or f"replay_{target_course_id}"
    begin_time = replay.get("courseBeginTime") or detail.get("videBeginTime") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_prefix = begin_time[:10]
    file_prefix = _safe_filename(f"{date_prefix}_{replay_title}")

    course_root = Path(root_dir) / "downloads" / str(course_id)
    transcript_dir = _ensure_dir(course_root / "transcripts")
    summary_dir = _ensure_dir(course_root / "platform_summaries")

    transcript_path = transcript_dir / f"{file_prefix}.json"
    summary_path = summary_dir / f"{file_prefix}.json"
    transcript_path.write_text(json.dumps(transcript_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    items = (((transcript_payload or {}).get("data") or {}).get("afterAssemblyList") or [])
    text_lines = []
    for item in items:
        text = (item.get("res") or "").strip()
        if not text:
            continue
        start_seconds = int(item.get("bg", 0)) // 1000
        minutes, seconds = divmod(start_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        text_lines.append(f"[{hours:02}:{minutes:02}:{seconds:02}] {text}")
    transcript_txt_path = transcript_dir / f"{file_prefix}.txt"
    transcript_txt_path.write_text("\n".join(text_lines), encoding="utf-8")
    if progress:
        progress("已写入 transcript JSON / TXT 和平台 summary。")

    first_bg = items[0].get("bg") if items else None
    last_ed = items[-1].get("ed") if items else None
    transcript_seconds = None
    if first_bg is not None and last_ed is not None:
        transcript_seconds = max(0.0, (last_ed - first_bg) / 1000)

    return {
        "replay": replay,
        "detail": detail,
        "transcript_path": str(transcript_path),
        "transcript_txt_path": str(transcript_txt_path),
        "summary_path": str(summary_path),
        "segments": len(items),
        "transcript_seconds": transcript_seconds,
    }
