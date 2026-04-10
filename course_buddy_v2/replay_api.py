import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlparse

import requests
from bs4 import BeautifulSoup

CANVAS_BASE = "https://oc.sjtu.edu.cn"
VIDEO_BASE = "https://v.sjtu.edu.cn/jy-application-canvas-sjtu"
COOKIE_FILE = os.path.expanduser("~/.config/canvas/cookies.json")


def save_cookies(cookies_dict: dict, path: Optional[str] = None) -> None:
    cookie_path = Path(path or COOKIE_FILE).expanduser()
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_format": "session_cookies", "cookies": cookies_dict}
    cookie_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(cookie_path, 0o600)


def load_cookies(path: Optional[str] = None) -> Optional[dict]:
    cookie_path = Path(path or COOKIE_FILE).expanduser()
    if not cookie_path.exists():
        return None
    data = json.loads(cookie_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("_format") == "session_cookies":
        return data.get("cookies", {})
    if isinstance(data, dict):
        return data
    return None


def validate_cookies(cookies: dict) -> bool:
    try:
        response = requests.get(
            f"{CANVAS_BASE}/courses",
            cookies=cookies,
            allow_redirects=False,
            timeout=10,
        )
    except Exception:
        return False
    if response.status_code == 200:
        return True
    if response.status_code == 302:
        return "login" not in response.headers.get("Location", "")
    return False


def get_cookies_from_browser(browser: str = "auto") -> Optional[dict]:
    try:
        import browser_cookie3
    except ImportError:
        return None

    browser_names = ["chrome", "safari", "firefox", "edge"] if browser == "auto" else [browser]
    for name in browser_names:
        try:
            loader = getattr(browser_cookie3, name, None)
            if not loader:
                continue
            cookiejar = loader(domain_name="oc.sjtu.edu.cn")
            cookies = {cookie.name: cookie.value for cookie in cookiejar}
            has_session = any(
                key in cookies
                for key in ("_normandy_session", "_legacy_normandy_session", "log_session_id")
            )
            if has_session:
                return cookies
        except Exception:
            continue
    return None


def ensure_cookies(cookie_path: Optional[str] = None, browser: str = "auto") -> dict:
    cached = load_cookies(cookie_path)
    if cached and validate_cookies(cached):
        return cached

    cookies = get_cookies_from_browser(browser)
    if cookies and validate_cookies(cookies):
        save_cookies(cookies, cookie_path)
        return cookies

    raise RuntimeError("没有找到有效的 Canvas 登录态。请先在浏览器里登录 oc.sjtu.edu.cn。")


def parse_redirect_params(url: str) -> dict:
    if not url:
        return {}
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "?" in parsed.fragment:
        _, _, fragment_query = parsed.fragment.partition("?")
        params.update(parse_qsl(fragment_query, keep_blank_values=True))
    return params


def get_video_platform_token(course_id: str, oc_cookies: dict) -> Tuple[str, str, requests.Session]:
    session = requests.Session()
    for key, value in oc_cookies.items():
        session.cookies.set(key, value, domain="oc.sjtu.edu.cn")

    tool_page = session.get(f"{CANVAS_BASE}/courses/{course_id}/external_tools/8329", timeout=20)
    tool_page.raise_for_status()
    soup = BeautifulSoup(tool_page.content, "html.parser")
    launch_form = soup.find("form", attrs={"action": f"{VIDEO_BASE}/oidc/login_initiations"})
    if not launch_form:
        raise RuntimeError("未找到视频工具的 LTI 登录表单。")

    launch_data = {
        input_tag["name"]: input_tag["value"]
        for input_tag in launch_form.find_all("input")
        if input_tag.get("name")
    }
    login_response = session.post(
        f"{VIDEO_BASE}/oidc/login_initiations",
        data=launch_data,
        allow_redirects=True,
        timeout=20,
    )
    login_response.raise_for_status()
    soup = BeautifulSoup(login_response.content, "html.parser")
    auth_form = soup.find("form", attrs={"action": f"{VIDEO_BASE}/lti3/lti3Auth/ivs"})
    if not auth_form:
        raise RuntimeError("未找到视频平台鉴权表单。")

    auth_data = {
        input_tag["name"]: input_tag["value"]
        for input_tag in auth_form.find_all("input")
        if input_tag.get("name")
    }
    auth_response = session.post(
        f"{VIDEO_BASE}/lti3/lti3Auth/ivs",
        data=auth_data,
        allow_redirects=False,
        timeout=20,
    )
    params = parse_redirect_params(auth_response.headers.get("location", ""))
    token_id = params.get("tokenId")
    if not token_id:
        raise RuntimeError("无法从 LTI 跳转里提取 tokenId。")

    token_response = session.get(
        f"{VIDEO_BASE}/lti3/getAccessTokenByTokenId",
        params={"tokenId": token_id},
        timeout=20,
    )
    token_response.raise_for_status()
    token_payload = token_response.json()["data"]
    access_token = token_payload["token"]
    access_params = token_payload.get("params") or {}
    canvas_course_id = (
        access_params.get("courId")
        or access_params.get("canvasCourseId")
        or access_params.get("courseId")
        or params.get("canvasCourseId")
        or course_id
    )
    return access_token, str(canvas_course_id), session


def _extract_records(payload) -> Optional[list]:
    if isinstance(payload, list):
        return payload
    candidates = [
        ("data", "records"),
        ("data", "list"),
        ("data", "rows"),
        ("data", "items"),
        ("data", "page", "records"),
        ("data",),
    ]
    for path in candidates:
        current = payload
        for key in path:
            current = current.get(key) if isinstance(current, dict) else None
        if isinstance(current, list):
            return current
    return None


def get_video_list(access_token: str, canvas_course_id: str, session: requests.Session) -> List[dict]:
    headers = {"token": access_token}
    encoded_cid = quote(canvas_course_id, safe="")
    bodies = [
        {"canvasCourseId": encoded_cid, "pageIndex": 1, "pageSize": 1000},
        {"canvasCourseId": encoded_cid},
        {"canvasCourseId": canvas_course_id, "pageIndex": 1, "pageSize": 1000},
        {"courId": encoded_cid, "pageIndex": 1, "pageSize": 1000},
        {"courId": encoded_cid},
        {"courId": canvas_course_id},
    ]
    for body in bodies:
        response = session.post(
            f"{VIDEO_BASE}/directOnDemandPlay/findVodVideoList",
            json=body,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        records = _extract_records(response.json())
        if records is not None:
            return records
    raise RuntimeError("视频列表接口没有返回可识别的数据。")


def get_video_detail(video_id: str, access_token: str, session: requests.Session) -> dict:
    response = session.post(
        f"{VIDEO_BASE}/directOnDemandPlay/getVodVideoInfos",
        data={"playTypeHls": "true", "id": video_id, "isAudit": "true"},
        headers={"token": access_token},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload

