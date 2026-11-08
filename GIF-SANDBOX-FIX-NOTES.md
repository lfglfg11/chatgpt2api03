# GIF 生成失败根因与修复方案（2026-11-07 排查记录）

> 本文由 Hermes 侧排查产出，供 chatgpt2api03 会话接手修复。
> 结论：**上游 GIF 已生成成功，但 sandbox 文件链接没被解析，被误判成 `upstream_text_reply`。**

## 一、现象

用户请求：`建国后每年人口出生率柱状图GIF`（模型 `gpt-image-2.6-gif (API)`）

前端显示红色「消息生成失败」，内容是一段未解析的 UI DSL：

```
<badge color="success">GIF 已生成</badge>
<box gap={2}>
  <title size="lg">中国人口出生率变化（1949—2025）</title>
  <row align="center" gap={2}>
    <text color="secondary" size="sm">77帧 · 1440 × 810 · GIF 动画</text>
  </row>
  [下载高清 GIF 动画](sandbox:/mnt/data/china_birth_rate_1949_2025.gif)
</box>
```

**上游明确写了「GIF 已生成」，77 帧、1440×810。文件在 `sandbox:/mnt/data/` 里。**

## 二、服务端日志证据

```
[INFO] {"event": "image_poll_conversation_failure",
        "conversation_id": "6aee9e46-...",
        "attempt": 5,
        "failure_code": "upstream_text_reply",
        "task_count": 0,
        "message_preview": "<badge color=\"success\">GIF 已生成</badge>..."}

[WARNING] {"event": "image_resolve_failed",
           "error": "ImageFailureError('<badge ...>')",
           "poll_wait_ms": 50452, "poll_request_ms": 3824}

[WARNING] {"event": "image_attempt_failed",
           "failure_code": "upstream_text_reply",
           "status_code": 400,
           "account_failure": false,
           "stream_error_ms": 58817}
```

另一例（飞天猫 GIF）上游直接回中文错误文本，同样是 `upstream_text_reply`。

## 三、根因

`services/openai_backend_api.py` 第 103-104 行，提取上游 sandbox 产物的正则只认 psd/zip 和 pptx/zip：

```python
EDITABLE_PSD_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:psd|zip))", re.IGNORECASE)
EDITABLE_PPT_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.(?:pptx?|zip))", re.IGNORECASE)
```

**没有 `.gif`。**

图片结果提取走的是资产指针路径（`_extract_image_asset_pointer_ids` / `_has_image_asset_pointer`），只认：

- `content_type == "image_asset_pointer"`
- `asset_pointer` 以 `file-service://` 或 `sediment://` 开头

上游这次返回的是**文本卡片 + sandbox 路径**，两条路都匹配不上，于是 `task_count: 0` → 判为「上游只回了文字」→ `upstream_text_reply`（400）。

## 四、上游仓库状态（yukkcat/chatgpt2api）

本地 remote 配置：

```
origin    git@github.com:lfglfg11/chatgpt2api03.git
upstream  git@github.com:yukkcat/chatgpt2api.git
```

本地领先上游 **483** 提交，落后 **61** 提交。

上游 v3.2.4（2026-11-06）两条相关提交：

| 提交 | 内容 |
|---|---|
| `4cf1e17` | feat: add GIF image output and image 2.6 models |
| `19004eb` | feat: support image output format conversion |

上游做了：
- 注册 `gpt-image-2.6` / `-flare` / `-sunburst`
- 新增 `gif_models` 配置（默认这三个）
- 新增 `output_format` / `frame_count` / `frame_duration_ms` 请求字段
- GIF 字节透传（`GIF87a`/`GIF89a` 检测，跳过 Pillow 重编码和 upscale）
- 存 `.gif` + `image/gif`
- **禁止 GIF + stream**

**但上游同样没有解析 sandbox `.gif`**——`git grep` 上游 `openai_backend_api.py`，正则也只有 `psd|zip` 和 `pptx?|zip`。上游需求文档 `docs/requirements/0001-image-gif-output.md` 写的是「Preserve upstream GIF bytes」，假设上游直接返回 GIF 字节，没考虑 sandbox 卡片形态。

## 五、本地与上游的 GIF 实现差异

| 项 | 本地 | 上游 v3.2.4 |
|---|---|---|
| 模型注册 | `725a99f` 自实现 | `4cf1e17` |
| `-gif` 后缀别名 | ✅ `827b3ad` | ❌ |
| GIF + stream | ✅ **允许** `355886e` | ❌ **禁止** |
| 静图转单帧 GIF | ✅ `956094c` | ❌ |
| sandbox `.gif` 解析 | ❌ | ❌ |

注意：本地允许 `GIF + stream`，上游明确禁止。本次失败走的正是 stream 路径（`image_stream_terminal_break`、`stream_error_ms: 58817`）。

## 六、修复方案

### 6.1 加 GIF 的 sandbox 正则

`services/openai_backend_api.py` 第 103-104 行附近：

```python
IMAGE_GIF_EXPORT_FILE_RE = re.compile(r"(?:sandbox:)?(/mnt/data/[^\s\"'\)\]]+\.gif)", re.IGNORECASE)
```

### 6.2 在图片解析失败前加 sandbox 兜底

现有的下载能力是现成的，直接复用：

- `_extract_editable_export_paths(message_text, export_file_re)` —— 从文本里抽 sandbox 路径
- `_download_editable_artifact(...)` —— 下载文件

在判定 `upstream_text_reply` **之前**插一段：

1. 用 `IMAGE_GIF_EXPORT_FILE_RE` 扫上游返回的 message text
2. 命中则下载该 `.gif`
3. 下载到的字节直接当图片结果返回（走 `convert_image_bytes`，GIF 会原样透传，不掉帧）

伪代码：

```python
# 在 task_count == 0 判定 upstream_text_reply 之前
gif_paths = self._extract_editable_export_paths(text, IMAGE_GIF_EXPORT_FILE_RE)
if gif_paths:
    downloaded = self._download_editable_artifact(...)  # 复用现有实现
    if downloaded:
        return downloaded[0].read_bytes()  # 交给 convert_image_bytes 透传
```

### 6.3 错误信息清洗

现在 `upstream_text_reply` 会把上游原始 DSL（`<badge>`、`<box>`、`<title>`、`<row>`、`<text>`）直接透给前端，用户看到一堆 XML。

建议在 `image_failure.py` 或返回层加清洗：剥掉所有 `<...>` 标签，只保留纯文本，或统一换成一句人话（如「GIF 已生成但下载失败，请重试」）。

### 6.4 可选：先关掉 GIF + stream

本地 `355886e` 允许了 `GIF + stream`，上游是禁止的。如果 6.2 改动风险大，可以先回退这条（让 GIF 走非流式单图返回），可能更快见效。但根本解法还是 6.2。

## 七、验证方法

修复后需要真实验证，不要只看代码：

1. 用 `gpt-image-2.6-gif` 模型请求一个 GIF（如「一只飞的猫」）
2. 检查返回的是 `.gif` 文件，`content-type: image/gif`
3. 用 `ffprobe` 或 Pillow 确认帧数 > 1（真动图，不是单帧）
4. 检查服务端日志里 `failure_code` 不再出现 `upstream_text_reply`
5. 顺带验证静图请求（不带 `output_format`）没被这次改动影响

## 八、附：本次排查用到的命令

```bash
# 容器状态
docker inspect chatgpt2api3 --format '{{.State.Status}} {{.State.StartedAt}} {{.RestartCount}}'

# GIF 相关日志
docker logs --since 2h chatgpt2api3 2>&1 | grep -iE 'gif|animation|动图'

# 上游对比
cd E:/WechatProject/AIProject/chatgpt2api03
git fetch upstream
git log HEAD..upstream/main --oneline | grep -iE "gif|image"
git grep -n "sandbox:" upstream/main -- "*.py"
```
