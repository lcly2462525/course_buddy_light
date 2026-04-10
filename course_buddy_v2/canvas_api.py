import os
from pathlib import Path
from typing import Dict, List, Optional

import requests

CANVAS_BASE = "https://oc.sjtu.edu.cn"
TOKEN_FILE = os.path.expanduser("~/.config/canvas/token")


def load_canvas_token() -> Optional[str]:
    if os.path.exists(TOKEN_FILE):
        return Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    return os.environ.get("CANVAS_TOKEN")


def get_active_courses(token: Optional[str] = None) -> List[Dict]:
    if not token:
        token = load_canvas_token()
    if not token:
        raise RuntimeError(
            "未找到 Canvas API Token。请先配置 ~/.config/canvas/token 或 CANVAS_TOKEN。"
        )

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{CANVAS_BASE}/api/v1/courses"
    params = {"enrollment_state": "active", "per_page": 100, "include[]": "term"}
    courses: List[Dict] = []

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=20)
        if response.status_code == 401:
            raise RuntimeError("Canvas API Token 无效或已过期。")
        response.raise_for_status()
        batch = response.json()
        if isinstance(batch, dict) and "errors" in batch:
            raise RuntimeError(f"Canvas API 错误: {batch['errors']}")
        courses.extend(batch)

        url = None
        params = {}
        link_header = response.headers.get("Link", "")
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url = part.split(";")[0].strip().strip("<>")
                break

    return courses


def filter_real_courses(courses: List[Dict]) -> List[Dict]:
    filtered = []
    skip_keywords = ["概览", "sandbox", "test", "template", "培训"]
    for course in courses:
        name = (course.get("name") or "").lower()
        code = (course.get("course_code") or "").strip()
        if any(keyword.lower() in name for keyword in skip_keywords):
            continue
        if not code:
            continue
        filtered.append(course)
    return filtered

