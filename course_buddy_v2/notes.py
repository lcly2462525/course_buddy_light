import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .llm_providers import resolve_provider


def _active_proxy_envs() -> Dict[str, str]:
    keys = [
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    ]
    return {key: os.environ[key] for key in keys if os.environ.get(key)}


def _get_llm_config(llm_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = llm_cfg or {}
    enabled = cfg.get("enabled", True)
    key_env = cfg.get("api_key_env", "LLM_API_KEY")
    default_api_key = (
        os.environ.get(key_env)
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    default_base_url = cfg.get("base_url") or os.environ.get("OPENAI_BASE_URL") or "https://aihubmix.com/v1"
    model = cfg.get("model", "qwen3-max")
    temperature = cfg.get("temperature", 0.3)
    use_env_proxy = cfg.get("use_env_proxy", True)

    api_key = default_api_key
    base_url = default_base_url
    if model:
        resolved = resolve_provider(model, cfg)
        model = resolved["model"]
        if resolved["base_url"]:
            base_url = resolved["base_url"]
        if resolved["api_key"]:
            api_key = resolved["api_key"]

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "temperature": temperature,
        "use_env_proxy": use_env_proxy,
        "enabled": enabled,
        "request_timeout": int(cfg.get("request_timeout", 90)),
        "retries": int(cfg.get("retries", 1)),
        "notes_chunk_minutes": int(cfg.get("notes_chunk_minutes", 12)),
        "notes_chunk_max_chars": int(cfg.get("notes_chunk_max_chars", 12000)),
        "notes_chunk_output_tokens": int(cfg.get("notes_chunk_output_tokens", 2200)),
        "notes_merge_max_tokens": int(cfg.get("notes_merge_max_tokens", 8000)),
    }


def _call_llm(prompt: str, llm: Dict[str, Any], max_tokens: int = 16000) -> Optional[str]:
    if not llm["enabled"] or not llm["api_key"]:
        return None

    url = llm["base_url"] + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + llm["api_key"],
        "Content-Type": "application/json",
    }
    payload = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": llm["temperature"],
    }
    timeout = llm.get("request_timeout", 90)
    retries = max(1, int(llm.get("retries", 1)))
    use_env_proxy = llm.get("use_env_proxy", True)
    proxy_modes = [use_env_proxy]
    if use_env_proxy:
        proxy_modes.append(False)

    for trust_env in proxy_modes:
        for attempt in range(1, retries + 1):
            try:
                session = requests.Session()
                session.trust_env = trust_env
                response = session.post(url, json=payload, headers=headers, timeout=timeout)
                if response.status_code != 200:
                    if trust_env and attempt == retries:
                        break
                    if attempt == retries:
                        return None
                    continue
                result = response.json()
                choices = result.get("choices")
                if not choices:
                    if attempt == retries:
                        break
                    continue
                content = (choices[0].get("message") or {}).get("content")
                if not content:
                    if attempt == retries:
                        break
                    continue
                finish_reason = choices[0].get("finish_reason", "")
                if finish_reason == "length" and max_tokens < 65000:
                    max_tokens = min(max_tokens * 2, 65000)
                    payload["max_tokens"] = max_tokens
                    continue
                return content
            except requests.exceptions.ProxyError:
                if trust_env and attempt == retries:
                    break
                if attempt == retries:
                    return None
                continue
            except requests.exceptions.Timeout:
                if attempt == retries:
                    return None
                continue
            except Exception:
                if attempt == retries:
                    return None
                continue
    return None


def _call_llm_with_progress(
    prompt: str,
    llm: Dict[str, Any],
    *,
    max_tokens: int,
    progress: Optional[Callable[[str], None]] = None,
    label: str = "模型请求",
) -> Optional[str]:
    if progress:
        progress(f"{label}：正在请求模型...")
    result = _call_llm(prompt, llm, max_tokens=max_tokens)
    if progress:
        if result:
            progress(f"{label}：模型返回成功。")
        else:
            progress(f"{label}：模型未返回可用结果。")
    return result


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _transcript_to_lines(payload: dict) -> List[dict]:
    items = (((payload or {}).get("data") or {}).get("afterAssemblyList") or [])
    lines = []
    for item in items:
        text = (item.get("res") or "").strip()
        if not text:
            continue
        start = int(item.get("bg", 0)) // 1000
        lines.append({"start": start, "text": text})
    return lines


def _fmt_time(seconds: int) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


_INLINE_MATH_SPACE_RE = re.compile(r"(?<!\$)\$ ([^$\n]+?) \$(?!\$)")
_BLOCK_MATH_TRAILING_RE = re.compile(r"^(\s*\$\$)\s+$", re.MULTILINE)
_DISPLAY_MATH_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)


def _fix_inline_math(text: str) -> str:
    r"""修复模型输出的 LaTeX 格式问题：
    1. 行内公式 $ ... $ → $...$（去掉紧邻空格）
    2. 块级公式分隔符 $$   → $$（去掉尾随空格，避免被解析成 <br>）
    3. \[...\] → $$...$$（统一用 Markdown 标准块级公式语法，支持单行和多行）
    """
    text = _INLINE_MATH_SPACE_RE.sub(r"$\1$", text)
    text = _BLOCK_MATH_TRAILING_RE.sub(r"\1", text)
    text = _DISPLAY_MATH_BRACKET_RE.sub(r"$$\1$$", text)
    return text


_FILLER_RE = re.compile(
    r"^(嗯+|啊+|呃+|哦+|嗯啊|那个|就是说|OK+|okay|对吧|是吧|好的|好吧)$"
)
_SPAM_PATTERNS = [
    re.compile(r"请不吝点赞\s*订阅\s*转发\s*打赏.*?栏目"),
]
_REPEAT_CHAR_RE = re.compile(r"(\S)\s*(\1\s*){5,}")


def _clean_transcript(text: str) -> str:
    lines = text.split("\n")
    cleaned = []
    prev = ""
    repeat_count = 0
    for line in lines:
        stripped = line.strip()
        # 跳过纯语气词/填充词行
        if _FILLER_RE.match(stripped):
            continue
        # 连续重复行最多保留1次
        if stripped == prev:
            repeat_count += 1
            if repeat_count >= 1:
                continue
        else:
            repeat_count = 0
        prev = stripped
        cleaned.append(line)

    text = "\n".join(cleaned)
    text = _REPEAT_CHAR_RE.sub(r"\1...", text)
    for pattern in _SPAM_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


def _build_transcript_text(lines: List[dict], chunk_seconds: int = 300) -> str:
    if not lines:
        return ""
    output = []
    current_chunk = -1
    for line in lines:
        chunk = line["start"] // chunk_seconds
        if chunk != current_chunk:
            current_chunk = chunk
            output.append(f"\n[{_fmt_time(chunk * chunk_seconds)}]")
        output.append(line["text"])
    return _clean_transcript("\n".join(output).strip())


def _chunk_text(text: str, max_chars: int = 50000) -> List[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in text.split("\n"):
        if current and current_len + len(line) + 1 > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _build_transcript_chunks(
    lines: List[dict],
    *,
    chunk_minutes: int,
    max_chars: int,
) -> List[dict]:
    if not lines:
        return []

    chunks: List[dict] = []
    chunk_seconds = max(1, chunk_minutes) * 60
    current_lines: List[dict] = []
    current_char_len = 0
    chunk_start = lines[0]["start"]

    def flush() -> None:
        nonlocal current_lines, current_char_len, chunk_start
        if not current_lines:
            return
        text = _build_transcript_text(current_lines)
        chunks.append(
            {
                "start": current_lines[0]["start"],
                "end": current_lines[-1]["start"],
                "text": text,
                "lines": current_lines,
            }
        )
        current_lines = []
        current_char_len = 0

    for line in lines:
        line_len = len(line["text"]) + 16
        hit_time_boundary = current_lines and (line["start"] - chunk_start >= chunk_seconds)
        hit_char_boundary = current_lines and (current_char_len + line_len > max_chars)
        if hit_time_boundary or hit_char_boundary:
            flush()
            chunk_start = line["start"]
        if not current_lines:
            chunk_start = line["start"]
        current_lines.append(line)
        current_char_len += line_len
    flush()
    return chunks


def _fallback_notes(course_name: str, date_str: str, title: str, transcript_lines: List[dict], platform_summary: dict) -> str:
    summary_data = (platform_summary or {}).get("data") or {}
    key_points = summary_data.get("keyPoints") or []
    document_skims = summary_data.get("documentSkims") or []
    md = [
        f"# {course_name} · {date_str} · {title}",
        "",
        "> 未配置可用 LLM，以下为平台摘要和转录摘录。",
        "",
        "## 一、平台概要",
        "",
        summary_data.get("fullOverview") or "暂无平台概要。",
        "",
        "### 关键点",
    ]
    if key_points:
        md.extend(f"- {item}" for item in key_points)
    else:
        md.append("- 暂无")
    md.extend(["", "## 二、分段概要"])
    if document_skims:
        for item in document_skims:
            md.append(f"- [{item.get('time', '?')}] {item.get('overview', '')}")
    else:
        md.append("- 暂无")
    md.extend(["", "## 三、转录摘录"])
    for line in transcript_lines[:40]:
        md.append(f"- [{_fmt_time(line['start'])}] {line['text']}")
    md.append("")
    return "\n".join(md)


def _build_platform_ref(platform_summary: dict) -> str:
    summary_data = (platform_summary or {}).get("data") or {}
    parts = []
    overview = summary_data.get("fullOverview")
    if overview:
        parts.append(f"概要：{overview}")
    key_points = summary_data.get("keyPoints") or []
    if key_points:
        parts.append("关键点：" + "；".join(key_points))
    skims = summary_data.get("documentSkims") or []
    if skims:
        skim_lines = [f"[{s.get('time', '?')}] {s.get('overview', '')}" for s in skims]
        parts.append("分段：" + " | ".join(skim_lines))
    return "\n".join(parts) if parts else ""


def _build_prompt(
    *,
    course_name: str,
    date_str: str,
    title: str,
    transcript_text: str,
    platform_summary: dict,
) -> str:
    ref = _build_platform_ref(platform_summary)
    ref_block = f"\n参考（仅辅助纠错，与转录冲突时以转录为准）：\n{ref}\n" if ref else ""

    return rf"""将以下课堂语音转录整理成结构化笔记。来源：「{course_name}」{date_str}，{title}。
{ref_block}
规则：
- 纠正术语/公式/人名的转录错误，不确定标 `[?]`
- 口述数学用 LaTeX：行内 `$...$`，块级 `$$...$$`（禁用 `\[...\]`），范数 `\lVert x \rVert`
- 老师强调"重要/会考/注意"处用 `> ⚠️ **重点**：`
- 不添加转录外内容，不臆测，不重复，同一内容只写一次

输出格式：

# {course_name} · {date_str} · {title}

## 一、总体概要
- 分条列主题
### 重要知识点

## 二、详细内容
按讲课逻辑分小标题展开，含推导、公式、例子。

## 三、课堂事务
签到/互动 | 课程安排通知 | 课后任务（`- [ ]` 格式）
无则注明"无"。

---

转录文本：

{transcript_text}
"""


def _build_chunk_prompt(
    *,
    course_name: str,
    date_str: str,
    title: str,
    chunk_index: int,
    chunk_total: int,
    chunk_start: int,
    chunk_end: int,
    transcript_text: str,
) -> str:
    return rf"""整理课堂转录分片 {chunk_index}/{chunk_total}（{_fmt_time(chunk_start)}-{_fmt_time(chunk_end)}），课程「{course_name}」{date_str}。

规则：只基于本段，纠正术语错误（不确定标`[?]`），紧凑输出，保留公式/定理/结论/例子/强调点/课堂事务。公式用 `$...$`（行内）或 `$$...$$`（块级），禁用 `\[...\]`。

输出格式：
### 分片 {chunk_index}（{_fmt_time(chunk_start)}-{_fmt_time(chunk_end)}）
**主题**：…
**内容**：…
**公式与结论**：…
**事务**：…（无则省略）

转录：

{transcript_text}
"""


def _build_merge_prompt(partial_notes: List[str], course_name: str, date_str: str, title: str) -> str:
    combined = "\n---\n".join(partial_notes)
    return rf"""合并以下分段笔记为完整笔记。去重但不丢信息，不添加分段中没有的内容。

输出格式：
# {course_name} · {date_str} · {title}
## 一、总体概要（分条列主题 + 重要知识点）
## 二、详细内容（按逻辑分小标题，含推导/公式/例子，公式用 `$...$` 或 `$$...$$`，禁用 `\[...\]`）
## 三、课堂事务（签到/通知/作业，无则注明）

老师强调处用 `> ⚠️ **重点**：`，不确定标 `[?]`。

分段笔记：

{combined}
"""


def summarize_transcript_files(
    *,
    transcript_path: str,
    summary_path: str,
    course_name: str,
    llm_cfg: Optional[Dict[str, Any]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> str:
    transcript_payload = _load_json(transcript_path)
    platform_summary = _load_json(summary_path)
    transcript_lines = _transcript_to_lines(transcript_payload)
    stem = Path(transcript_path).stem
    if stem[:10].count("-") == 2:
        date_str = stem[:10]
        title = stem[11:] or stem
    else:
        date_str = datetime.fromtimestamp(Path(transcript_path).stat().st_mtime).strftime("%Y-%m-%d")
        title = stem

    transcript_text = _build_transcript_text(transcript_lines)
    llm = _get_llm_config(llm_cfg)
    if not transcript_text:
        return f"# {course_name} · {date_str} · {title}\n\n> 转录为空，无法生成笔记。\n"

    if not llm["enabled"] or not llm["api_key"]:
        if progress:
            progress("未启用 LLM，直接输出平台摘要和转录摘录。")
        return _fallback_notes(course_name, date_str, title, transcript_lines, platform_summary)

    transcript_chunks = _build_transcript_chunks(
        transcript_lines,
        chunk_minutes=llm["notes_chunk_minutes"],
        max_chars=llm["notes_chunk_max_chars"],
    )

    if len(transcript_chunks) <= 1:
        if progress:
            progress("笔记生成：单段模式，正在请求模型...")
        prompt = _build_prompt(
            course_name=course_name,
            date_str=date_str,
            title=title,
            transcript_text=transcript_text,
            platform_summary=platform_summary,
        )
        result = _call_llm_with_progress(
            prompt,
            llm,
            max_tokens=16000,
            progress=progress,
            label="整讲笔记",
        )
        if result:
            if progress:
                progress("笔记生成完成。")
            return _fix_inline_math(result)
    else:
        total = len(transcript_chunks)
        if progress:
            progress(f"笔记生成：分 {total} 段并发处理中...")

        def _process_chunk(idx: int, chunk: dict) -> tuple[int, Optional[str]]:
            if progress:
                progress(f"正在处理分片 {idx}/{total}（{_fmt_time(chunk['start'])}-{_fmt_time(chunk['end'])}）...")
            prompt = _build_chunk_prompt(
                course_name=course_name,
                date_str=date_str,
                title=title,
                chunk_index=idx,
                chunk_total=total,
                chunk_start=chunk["start"],
                chunk_end=chunk["end"],
                transcript_text=chunk["text"],
            )
            result = _call_llm(prompt, llm, max_tokens=llm["notes_chunk_output_tokens"])
            if progress:
                if result:
                    progress(f"分片 {idx}/{total}：模型返回成功。")
                else:
                    progress(f"分片 {idx}/{total}：未拿到结果，将依赖其他分片继续合并。")
            return idx, result

        max_workers = min(total, 4)
        results_by_idx: Dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_process_chunk, idx, chunk): idx
                for idx, chunk in enumerate(transcript_chunks, start=1)
            }
            for future in as_completed(futures):
                idx, result = future.result()
                if result:
                    results_by_idx[idx] = result

        partial_notes = [results_by_idx[k] for k in sorted(results_by_idx)]

        if partial_notes:
            if progress:
                progress(f"已完成 {len(partial_notes)} 个分片，正在进行总合并...")
            merge_prompt = _build_merge_prompt(partial_notes, course_name, date_str, title)
            merged = _call_llm_with_progress(
                merge_prompt,
                llm,
                max_tokens=llm["notes_merge_max_tokens"],
                progress=progress,
                label="总合并",
            )
            if merged:
                if progress:
                    progress("笔记生成完成。")
                return _fix_inline_math(merged)
            if len(partial_notes) == 1:
                return _fix_inline_math(partial_notes[0])
            return _fix_inline_math("\n\n---\n\n".join(partial_notes))
    if progress:
        progress("模型结果不可用，回退到平台摘要和转录摘录。")
    return _fallback_notes(course_name, date_str, title, transcript_lines, platform_summary)
