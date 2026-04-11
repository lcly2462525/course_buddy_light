# course-buddy-v2

从交大 Canvas 课程回放中自动生成结构化笔记。

> 工具不下载视频文件，也不做语音识别（ASR）。  
> 它抓取平台已有的**转录文本**，再交给大模型整理成 Markdown 笔记。

---

## 它能做什么

| 功能 | 说明 |
|------|------|
| `list` | 列出 Canvas 当前学期所有课程 |
| `list-videos` | 列出某门课的回放列表 |
| `fetch` | 下载转录 JSON/TXT 和平台摘要 |
| `notes` | 调用 LLM 从转录生成结构化笔记 |
| `all` | 一键完成 fetch + notes |
| `read` | 快速查看已生成的笔记/转录/摘要 |

---

## 快速上手

### 第一步：安装

需要 Python 3.10 及以上版本。

```bash
# 创建虚拟环境（推荐，避免污染系统 Python）
python3 -m venv .venv
source .venv/bin/activate   # Windows 用 .venv\Scripts\activate

# 安装本项目
pip install -e .
```

安装后，`cb` 命令就可以在当前虚拟环境中使用了。

### 第二步：运行配置向导

最简单的方式就是运行一条命令，根据提示完成配置：

```bash
cb setup
```

这会引导你：
1. 选择 LLM 提供商（aihubmix、OpenAI、DeepSeek、阿里云、硅基流动 或自定义）
2. 输入对应的 API Key
3. 选择使用的模型
4. 设置是否使用代理

配置会自动保存到 `config.yaml`。

> 如果你喜欢手动编辑，也可以跳过 setup，直接复制 `cp config.yaml.example config.yaml` 后修改。

### 第三步：准备其他凭据

**Canvas API Token**（用于获取课程和回放列表）

- 登录 Canvas → 账户 → 设置 → 滚到最底部 → 生成访问令牌
- 把令牌写入文件：

```bash
mkdir -p ~/.config/canvas
echo "你的token粘贴到这里" > ~/.config/canvas/token
```

**浏览器 Cookie**（用于访问回放平台 oc.sjtu.edu.cn）

- 用浏览器正常登录 [oc.sjtu.edu.cn](https://oc.sjtu.edu.cn)，保持登录状态即可
- 工具默认会自动从 Chrome/Safari/Firefox 读取 Cookie（`cookies_from_browser: auto`）
- 如果自动读取失败，可用 [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg) 导出 JSON 后在 `config.yaml` 中指定路径

---

## 常用命令

```bash
# 查看所有课程
cb list

# 查看某课程的回放列表（课程 ID 从 cb list 拿到）
cb list-videos --course 87081
cb list-videos --course 87081 --since 2w   # 只看最近两周的

# 下载最新一讲的转录文本
cb fetch --course 87081 --latest

# 下载指定索引的回放（0 是最新，1 是次新，以此类推）
cb fetch --course 87081 --index 2

# 从已下载的转录生成笔记（自动选最新）
cb notes --course 87081 --latest

# 一键下载 + 生成笔记
cb all --course 87081 --latest

# 指定模型
cb notes --course 87081 --latest --model deepseek/deepseek-chat

# 不用 LLM，只输出平台摘要（免费，速度快）
cb notes --course 87081 --latest --no-llm

# 查看最新笔记
cb read --course 87081 --latest
cb read --course 87081 --latest --full      # 完整内容
cb read --course 87081 --latest --txt       # 查看转录原文
cb read --course 87081 --latest --summary   # 查看平台摘要
```

如果还没安装脚本，可以用 `python -m` 方式调用：

```bash
python -m course_buddy_v2.cli list
```

---

## 输出文件结构

```text
data/downloads/<课程ID>/
  transcripts/
    2026-03-27_数理统计(第12讲).json   ← 转录原始数据
    2026-03-27_数理统计(第12讲).txt    ← 转录纯文本
  platform_summaries/
    2026-03-27_数理统计(第12讲).json   ← 平台生成的摘要
  notes/
    2026-03-27_数理统计(第12讲).md     ← 生成的结构化笔记 ✓
```

---

## 性能与成本参考

以一节 **55 分钟**的课（数理统计第12讲）为例，使用当前默认模型 `qwen3-max`：

| 指标 | 数值 |
|------|------|
| 转录总字符数 | 约 13,000 字 |
| 实际 LLM 输入 | ~10,000 tokens（5 片 prompt + 合并输入） |
| 实际 LLM 输出 | ~3,000 tokens（5 片摘要 + 最终合并） |
| 分片数（12分钟/片） | 5 片，最多 4 片并发 |
| 预计总耗时 | **1.5 ~ 3 分钟** |
| 预计费用（qwen3-max） | **约 ¥0.5+** |

**模型选择参考：**

| 模型 | 单节课费用 | 说明 |
|------|-----------|------|
| `qwen-turbo` | ~¥0.05 | 速度快、成本低 |
| `qwen-plus` | ~¥0.015 | 更便宜，质量略低 |
| `qwen3-max`（默认） | ~¥0.5+ | 质量更稳，适合更完整的课堂笔记 |

> `--no-llm` 完全免费，但只输出平台原始摘要，质量较低。

---

## 支持的 LLM 提供商

用 `--model provider/model` 格式切换：

| 提供商 | 示例 | 所需环境变量 |
|--------|------|-------------|
| aihubmix（默认） | `qwen3-max` | `LLM_API_KEY` |
| DeepSeek | `deepseek/deepseek-chat` | `DEEPSEEK_API_KEY` |
| OpenAI | `openai/gpt-4o` | `OPENAI_API_KEY` |
| 阿里云通义 | `qwen/qwen-max` | `DASHSCOPE_API_KEY` |
| 硅基流动 | `siliconflow/Qwen3-235B-A22B` | `SILICONFLOW_API_KEY` |

如果你的服务是兼容 OpenAI 接口的，也可以直接配置：

```yaml
llm:
  base_url: https://your-gateway.example.com/v1
  api_key: sk-xxxx
  model: your-model-name
```

也可在 `config.yaml` 的 `llm.providers` 中添加自定义提供商。

---

## 常见问题

**Q: `cb list` 报错说找不到 token？**  
A: 检查 `~/.config/canvas/token` 文件是否存在且内容正确（不要有多余的空格或换行）。

**Q: `fetch` 失败，提示 Cookie 问题？**  
A: 先确认浏览器已登录 oc.sjtu.edu.cn。如果自动读取仍失败，用 EditThisCookie 手动导出并在配置文件中指定 `cookies_path`。

**Q: 笔记生成失败或返回空内容？**  
A: 检查 `LLM_API_KEY` 是否正确设置；也可以先用 `--no-llm` 验证转录下载是否正常。

**Q: 想覆盖已有笔记怎么办？**  
A: 加上 `--force` 参数：`cb notes --course 87081 --latest --force`。
