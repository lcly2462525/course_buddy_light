import argparse
import glob
import json
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.table import Table

from .canvas_api import filter_real_courses, get_active_courses
from .config import load_config
from .notes import summarize_transcript_files
from .replay_api import ensure_cookies, get_video_list, get_video_platform_token
from .transcript_api import fetch_transcript_bundle, filter_replays_since

console = Console()


def _progress(message: str) -> None:
    console.print(f"[cyan]{message}[/cyan]")


def _default_config_path() -> str:
    return str(Path(__file__).resolve().parents[1] / "config.yaml")


def _load_courses():
    return filter_real_courses(get_active_courses())


def _find_course_meta(course_id: str, courses: list[dict]) -> dict:
    for course in courses:
        if str(course["id"]) == str(course_id):
            return course
    raise RuntimeError(f"没有找到课程 {course_id}。")


def _course_data_dir(root_dir: str, course_id: str) -> Path:
    return Path(root_dir) / "downloads" / str(course_id)


def _parse_index_list(value: str) -> list[int]:
    try:
        return [int(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"无效的索引值：{value!r}，请使用整数或逗号分隔的整数列表，如 12 或 12,13,14"
        )


def _pick_latest_file(paths: list[str]) -> str:
    if not paths:
        raise RuntimeError("没有找到匹配文件。")
    return sorted(paths)[-1]


def _read_text_file(path: Path, *, head: int | None, full: bool) -> str:
    text = path.read_text(encoding="utf-8")
    if full:
        return text
    lines = text.splitlines()
    if head is None:
        head = 120
    return "\n".join(lines[:head])


def _find_read_target(root: Path, kind: str, glob_pattern: str | None, latest: bool, index: int | None) -> tuple[Path, str]:
    candidates = []
    if kind == "notes":
        candidates = [
            ("notes", root / "notes", glob_pattern or "*.md"),
            ("txt", root / "transcripts", "*.txt"),
            ("transcript", root / "transcripts", "*.json"),
            ("summary", root / "platform_summaries", "*.json"),
        ]
    elif kind == "summary":
        candidates = [("summary", root / "platform_summaries", glob_pattern or "*.json")]
    elif kind == "txt":
        candidates = [("txt", root / "transcripts", glob_pattern or "*.txt")]
    elif kind == "transcript":
        candidates = [("transcript", root / "transcripts", glob_pattern or "*.json")]

    for resolved_kind, subdir, pattern in candidates:
        matches = sorted(glob.glob(str(subdir / pattern)))
        if not matches:
            continue
        if latest or index is None:
            return Path(_pick_latest_file(matches)), resolved_kind
        return Path(matches[index]), resolved_kind
    raise RuntimeError("没有找到匹配文件。")


def cmd_list_courses(args) -> int:
    courses = _load_courses()
    table = Table(title="Canvas 课程")
    table.add_column("ID")
    table.add_column("课程名")
    table.add_column("课程代码")
    table.add_column("学期")
    for course in courses:
        table.add_row(
            str(course["id"]),
            course.get("name") or "",
            course.get("course_code") or "",
            (course.get("term") or {}).get("name") or "",
        )
    console.print(table)
    return 0


def cmd_list_replays(args) -> int:
    cfg = load_config(args.config)
    course_meta = _find_course_meta(args.course, _load_courses())
    _progress("正在检查 Canvas 登录态...")
    cookies = ensure_cookies(cfg.get("cookies_path"), cfg.get("cookies_from_browser", "auto"))
    _progress("正在获取课程回放列表...")
    token, canvas_course_id, session = get_video_platform_token(str(args.course), cookies)
    videos = filter_replays_since(get_video_list(token, canvas_course_id, session), args.since)

    table = Table(title=f"回放列表 · {course_meta.get('name')}")
    table.add_column("Index")
    table.add_column("标题")
    table.add_column("开始时间")
    table.add_column("结束时间")
    table.add_column("courId")
    for index, video in enumerate(videos):
        table.add_row(
            str(index),
            video.get("videoName") or "",
            video.get("courseBeginTime") or "",
            video.get("courseEndTime") or "",
            str(video.get("courId") or ""),
        )
    console.print(table)
    return 0


def cmd_fetch_transcript(args) -> int:
    cfg = load_config(args.config)
    course_meta = _find_course_meta(args.course, _load_courses())
    _progress("正在检查 Canvas 登录态...")
    cookies = ensure_cookies(cfg.get("cookies_path"), cfg.get("cookies_from_browser", "auto"))
    indices = args.index if args.index else [None]
    for index in indices:
        result = fetch_transcript_bundle(
            course_id=str(args.course),
            course_name=course_meta.get("name") or str(args.course),
            oc_cookies=cookies,
            root_dir=cfg["root_dir"],
            latest=args.latest,
            index=index,
            cour_id=args.cour_id,
            since=args.since,
            progress=_progress,
        )
        replay = result["replay"]
        detail = result["detail"]
        duration_seconds = detail.get("videPlayTime") or 0
        transcript_seconds = result.get("transcript_seconds") or 0
        console.print(f"[green]已下载[/green] {replay.get('videoName')}")
        console.print(f"课程: {course_meta.get('name')}")
        console.print(f"回放开始: {replay.get('courseBeginTime')}")
        console.print(f"视频时长: {duration_seconds / 60:.2f} 分钟")
        console.print(f"转录覆盖: {transcript_seconds / 60:.2f} 分钟")
        console.print(f"分段数: {result['segments']}")
        console.print(f"Transcript JSON: {result['transcript_path']}")
        console.print(f"Transcript TXT: {result['transcript_txt_path']}")
        console.print(f"Platform Summary: {result['summary_path']}")
    return 0


def _resolve_note_sources(root_dir: str, course_id: str, glob_pattern: str | None) -> list[tuple[str, str]]:
    transcript_dir = Path(root_dir) / "downloads" / str(course_id) / "transcripts"
    summary_dir = Path(root_dir) / "downloads" / str(course_id) / "platform_summaries"
    pattern = glob_pattern or "*.json"
    transcript_files = sorted(glob.glob(str(transcript_dir / pattern)))
    pairs = []
    for transcript_path in transcript_files:
        summary_path = summary_dir / Path(transcript_path).name
        if summary_path.exists():
            pairs.append((transcript_path, str(summary_path)))
    return pairs


def _build_llm_cfg(cfg: dict, args) -> dict:
    llm_cfg = dict(cfg.get("llm", {}))
    if getattr(args, "no_llm", False):
        llm_cfg["enabled"] = False
    if getattr(args, "model", None):
        llm_cfg["model"] = args.model
    if getattr(args, "base_url", None):
        llm_cfg["base_url"] = args.base_url
    if getattr(args, "api_key", None):
        llm_cfg["api_key"] = args.api_key
    if getattr(args, "api_key_env", None):
        llm_cfg["api_key_env"] = args.api_key_env
    return llm_cfg


def cmd_notes(args) -> int:
    cfg = load_config(args.config)
    course_meta = _find_course_meta(args.course, _load_courses())
    notes_dir = Path(cfg["root_dir"]) / "downloads" / str(args.course) / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    pairs = _resolve_note_sources(cfg["root_dir"], str(args.course), args.glob)
    if not pairs and (args.latest or args.index is not None or args.cour_id is not None):
        _progress("本地没有匹配的 transcript，先去抓取回放文本...")
        cookies = ensure_cookies(cfg.get("cookies_path"), cfg.get("cookies_from_browser", "auto"))
        for index in (args.index if args.index else [None]):
            fetch_transcript_bundle(
                course_id=str(args.course),
                course_name=course_meta.get("name") or str(args.course),
                oc_cookies=cookies,
                root_dir=cfg["root_dir"],
                latest=args.latest,
                index=index,
                cour_id=args.cour_id,
                since=args.since,
                progress=_progress,
            )
        pairs = _resolve_note_sources(cfg["root_dir"], str(args.course), args.glob)

    if not pairs:
        raise RuntimeError("没有找到可用于生成笔记的 transcript/summary 文件。")
    _progress(f"共找到 {len(pairs)} 份可处理的 transcript。")

    llm_cfg = _build_llm_cfg(cfg, args)

    generated = 0
    for idx, (transcript_path, summary_path) in enumerate(pairs, start=1):
        out_path = notes_dir / (Path(transcript_path).stem + ".md")
        if out_path.exists() and not args.force:
            console.print(f"[dim]跳过已有笔记: {out_path.name}[/dim]")
            continue
        _progress(f"正在生成笔记 {idx}/{len(pairs)}：{Path(transcript_path).name}")
        md = summarize_transcript_files(
            transcript_path=transcript_path,
            summary_path=summary_path,
            course_name=course_meta.get("name") or str(args.course),
            llm_cfg=llm_cfg,
            progress=_progress,
        )
        out_path.write_text(md, encoding="utf-8")
        console.print(f"[green]笔记已写入[/green] {out_path}")
        generated += 1
    _progress(f"笔记阶段结束，共生成 {generated} 份笔记。")
    return 0


def cmd_all(args) -> int:
    _progress("开始执行 all：先抓 transcript，再生成笔记。")
    fetch_args = argparse.Namespace(**vars(args))
    cmd_fetch_transcript(fetch_args)
    notes_args = argparse.Namespace(**vars(args))
    if not notes_args.glob:
        notes_args.glob = "*.json"
    result = cmd_notes(notes_args)
    _progress("all 执行完成。")
    return result


def cmd_setup(args) -> int:
    """交互式初始化配置向导"""
    config_path = Path(args.config).expanduser().resolve()
    config_dir = config_path.parent

    console.print("[bold cyan]欢迎使用 course-buddy-v2 配置向导[/bold cyan]\n")

    # 选择 LLM 提供商
    console.print("[yellow]第一步：选择 LLM 提供商[/yellow]")
    console.print("请选择一个提供商（或输入 custom 自定义）：\n")
    providers = [
        ("1", "aihubmix（默认，推荐）", "https://aihubmix.com/v1", "LLM_API_KEY"),
        ("2", "OpenAI", "https://api.openai.com/v1", "OPENAI_API_KEY"),
        ("3", "DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
        ("4", "阿里云通义（Qwen）", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
        ("5", "硅基流动（SiliconFlow）", "https://api.siliconflow.cn/v1", "SILICONFLOW_API_KEY"),
        ("6", "自定义", None, None),
    ]
    for code, name, _, _ in providers:
        console.print(f"  {code}. {name}")
    
    choice = Prompt.ask("\n请选择 (1-6)", choices=["1", "2", "3", "4", "5", "6"])
    
    selected = next((p for p in providers if p[0] == choice), None)
    if not selected:
        console.print("[red]无效选择[/red]")
        return 1
    
    _, provider_name, base_url, api_key_env = selected
    
    if choice == "6":
        # 自定义提供商
        console.print("\n[yellow]自定义 LLM 提供商[/yellow]")
        base_url = Prompt.ask("请输入 API 基础地址", default="https://example.com/v1")
        api_key = Prompt.ask("请输入 API Key（或留空，稍后在环境变量中设置）", default="", password=True)
        api_key_env = "LLM_API_KEY"
        provider_name = "custom"
    else:
        # 内置提供商
        model_hint = {
            "1": "qwen-turbo",
            "2": "gpt-4o",
            "3": "deepseek-chat",
            "4": "qwen-max",
            "5": "Qwen3-235B-A22B",
        }.get(choice, "")
        api_key = Prompt.ask(f"\n请输入 {provider_name} 的 API Key（或留空，稍后在环境变量中设置）", default="", password=True)
    
    # 选择模型
    console.print("\n[yellow]第二步：选择模型[/yellow]")
    if choice == "6":
        model = Prompt.ask("请输入模型名称", default="your-model")
    else:
        model_hint = {
            "1": "qwen-turbo",
            "2": "gpt-4o",
            "3": "deepseek-chat",
            "4": "qwen-max",
            "5": "Qwen3-235B-A22B",
        }.get(choice, "")
        model = Prompt.ask(f"请输入模型名称", default=model_hint)
    
    # 其他配置
    console.print("\n[yellow]第三步：其他设置[/yellow]")
    use_proxy = Confirm.ask("是否使用环境变量中的代理（HTTP_PROXY/HTTPS_PROXY）？", default=True)
    
    # 构建配置
    config = {
        "root_dir": "data",
        "cookies_path": "~/.config/canvas/cookies.json",
        "cookies_from_browser": "auto",
        "courses": {},
        "llm": {
            "enabled": True,
            "api_key": api_key if api_key else "",
            "api_key_env": api_key_env,
            "base_url": base_url,
            "model": model,
            "temperature": 0.3,
            "use_env_proxy": use_proxy,
            "request_timeout": 600,
            "retries": 3,
            "notes_chunk_minutes": 12,
            "notes_chunk_max_chars": 12000,
            "notes_chunk_output_tokens": 2200,
            "notes_merge_max_tokens": 8000,
            "providers": {},
        },
    }
    
    # 保存配置
    config_dir.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    
    console.print(f"\n[green]✓ 配置已保存到：{config_path}[/green]")
    
    if not api_key:
        console.print(f"[yellow]⚠️  提醒：你选择了在环境变量中设置 API Key[/yellow]")
        console.print(f"   请在终端执行：[bold]export {api_key_env}=你的APIKey[/bold]")
    
    console.print("\n[cyan]下一步，请准备：[/cyan]")
    console.print("  1. Canvas API Token → ~/.config/canvas/token")
    console.print("  2. 浏览器 Cookie → 工具自动读取或手动配置")
    console.print("\n之后就可以开始使用了：[bold]cb list[/bold]")
    
    return 0


def cmd_read(args) -> int:
    cfg = load_config(args.config)
    _find_course_meta(args.course, _load_courses())
    root = _course_data_dir(cfg["root_dir"], str(args.course))

    kind = "notes"
    if args.transcript:
        kind = "transcript"
    elif args.summary:
        kind = "summary"
    elif args.txt:
        kind = "txt"

    _progress(f"正在查找 {kind} 文件...")
    target, resolved_kind = _find_read_target(root, kind, args.glob, args.latest or not args.glob, args.index)
    if resolved_kind != kind:
        console.print(f"[yellow]未找到 {kind}，已自动切换为 {resolved_kind}。[/yellow]")

    console.print(f"[green]文件[/green] {target}")
    content = _read_text_file(target, head=args.head, full=args.full)
    console.print(content)
    if not args.full:
        console.print("[dim]已输出文件前半部分；如需完整内容，追加 --full。[/dim]")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cb", description="SJTU Canvas transcript downloader")
    parser.add_argument("--config", default=_default_config_path(), help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="交互式配置向导").set_defaults(handler=cmd_setup)

    sub.add_parser("list-courses", aliases=["list"], help="列出当前学期 Canvas 课程").set_defaults(handler=cmd_list_courses)

    p = sub.add_parser("list-replays", aliases=["list-videos"], help="列出课程回放")
    p.add_argument("--course", required=True, help="Canvas 课程 ID")
    p.add_argument("--since", default=None, help="仅列出最近时间范围内回放，如 7d/2w/1m")
    p.set_defaults(handler=cmd_list_replays)

    p = sub.add_parser("fetch-transcript", aliases=["fetch"], help="下载 transcript 和平台 summary")
    p.add_argument("--course", required=True, help="Canvas 课程 ID")
    p.add_argument("--latest", action="store_true", help="下载最新一讲")
    p.add_argument("--index", type=_parse_index_list, help="按回放索引下载，支持逗号分隔多个，如 12 或 12,13,14")
    p.add_argument("--cour-id", type=int, help="按平台 courId 下载")
    p.add_argument("--since", default=None, help="结合 --latest 使用，只在最近时间范围内选回放")
    p.set_defaults(handler=cmd_fetch_transcript)

    p = sub.add_parser("notes", help="从 transcript 生成笔记")
    p.add_argument("--course", required=True, help="Canvas 课程 ID")
    p.add_argument("--glob", default=None, help="transcript 文件匹配模式")
    p.add_argument("--latest", action="store_true", help="若本地没有 transcript，先抓最新一讲")
    p.add_argument("--index", type=_parse_index_list, help="若本地没有 transcript，先抓指定索引，支持逗号分隔多个")
    p.add_argument("--cour-id", type=int, help="若本地没有 transcript，先抓指定 courId")
    p.add_argument("--since", default=None, help="结合 --latest 使用，只在最近时间范围内选回放")
    p.add_argument("--force", action="store_true", help="覆盖已有笔记")
    p.add_argument("--model", default=None, help="指定 LLM 模型，支持 provider/model 格式")
    p.add_argument("--base-url", default=None, help="自定义 LLM API 基础地址，例如 https://example.com/v1")
    p.add_argument("--api-key", default=None, help="自定义 LLM API Key")
    p.add_argument("--api-key-env", default=None, help="从指定环境变量读取 API Key")
    p.add_argument("--no-llm", action="store_true", help="不调用模型，直接输出平台摘要和转录摘录")
    p.set_defaults(handler=cmd_notes)

    p = sub.add_parser("all", help="下载 transcript 后直接生成笔记")
    p.add_argument("--course", required=True, help="Canvas 课程 ID")
    p.add_argument("--latest", action="store_true", help="处理最新一讲")
    p.add_argument("--index", type=_parse_index_list, help="处理指定索引，支持逗号分隔多个，如 12 或 12,13,14")
    p.add_argument("--cour-id", type=int, help="处理指定 courId")
    p.add_argument("--since", default=None, help="结合 --latest 使用，只在最近时间范围内选回放")
    p.add_argument("--glob", default=None, help="生成笔记时使用的 transcript 匹配模式")
    p.add_argument("--force", action="store_true", help="覆盖已有 transcript 文本和笔记")
    p.add_argument("--model", default=None, help="指定 LLM 模型，支持 provider/model 格式")
    p.add_argument("--base-url", default=None, help="自定义 LLM API 基础地址，例如 https://example.com/v1")
    p.add_argument("--api-key", default=None, help="自定义 LLM API Key")
    p.add_argument("--api-key-env", default=None, help="从指定环境变量读取 API Key")
    p.add_argument("--no-llm", action="store_true", help="不调用模型，直接输出平台摘要和转录摘录")
    p.set_defaults(handler=cmd_all)

    p = sub.add_parser("read", help="快速查看已生成的结果文件")
    p.add_argument("--course", required=True, help="Canvas 课程 ID")
    p.add_argument("--glob", default=None, help="文件匹配模式")
    p.add_argument("--latest", action="store_true", help="读取最新文件")
    p.add_argument("--index", type=int, help="读取匹配结果中的指定索引")
    p.add_argument("--head", type=int, default=120, help="默认只输出前多少行")
    p.add_argument("--full", action="store_true", help="输出完整文件内容")
    p.add_argument("--notes", action="store_true", help="读取笔记 Markdown（默认）")
    p.add_argument("--transcript", action="store_true", help="读取 transcript JSON")
    p.add_argument("--summary", action="store_true", help="读取平台 summary JSON")
    p.add_argument("--txt", action="store_true", help="读取 transcript 纯文本")
    p.set_defaults(handler=cmd_read)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
