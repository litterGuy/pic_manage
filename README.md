# pic_manage

扫描散乱的图片、视频、音频，**按烙印在文件内部的真实创建时间**去重并归档到 `年/月` 目录，
并提供一个网页按年、月方便地查阅。

## 它解决什么问题

把手机、相机、微信、QQ 传出来的文件倒到电脑上之后，通常会遇到三件事：

1. 同一个文件被复制了好几份，占空间；
2. 文件的「修改时间」是**复制到电脑的时间**，不是拍摄时间，按它排序完全乱掉；
3. 想找某年某月拍的照片，只能靠翻文件夹。

pic_manage 对这三件事各做一件事：

* **去重**：按文件内容哈希判定重复，保留**时间最早**的那一个；被判定重复的文件移入
  `data/quarantine/` 隔离区，不删除，随时可以恢复。
* **取时间**：只读取文件内部的元数据 —— 图片读 EXIF `DateTimeOriginal`，视频读容器
  `creation_time`，音频读 ID3 / MP4 / Vorbis / APE / RIFF 标签。**不拿文件系统时间当拍摄时间**；
  确实找不到内嵌时间的文件进 `_unsorted/`，宁可归到未分类也不猜。
* **归档**：放进 `data/library/YYYY/YYYY-MM/`，一个月一个文件夹。
* **浏览**：Flask 网页，左侧年/月树，右侧缩略图网格，点开是大图/播放器，支持方向键翻页。

## 安装

用 [uv](https://docs.astral.sh/uv/) 管理依赖（会自动建好 `.venv` 并装好全部依赖）：

```powershell
uv sync
```

之后所有命令都通过 `uv run` 执行，例如 `uv run python run.py scan --source D:\照片`。

依赖：Pillow、pillow-heif（HEIC/HEIF 的 EXIF）、mutagen（音频标签）、Flask（网页）。
视频元数据读取和视频缩略图需要 [ffmpeg](https://ffmpeg.org/)（`ffprobe`/`ffmpeg` 在 PATH 中）。
没有 ffmpeg 时图片和音频仍然正常工作，只有视频的时间和缩略图会缺失。

> ffmpeg 不是 pip 包，`uv sync` 不会装它，需要单独安装并确保在 PATH 中。

装成命令行工具（可选，会得到一个 `pic-manage` 命令）：

```powershell
uv pip install -e .
pic-manage scan --source D:\照片
```

## Docker Compose

复制 `.env.example` 为 `.env`，设置宿主机上的 `PICMANAGE_SOURCE_DIR`（待扫描目录）和
`PICMANAGE_DATA_DIR`（索引、归档文件、隔离区与缩略图的持久化目录）。Windows 绝对路径建议写成
`D:/Photos`；两个目录应分开，数据目录需要可写。`PICMANAGE_PORT` 和 `PICMANAGE_TZ` 也可调整。

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose exec picmanage python run.py scan --source /source
```

打开 <http://127.0.0.1:8000/>。首次启动会初始化空索引；扫描后刷新页面即可看到文件。
宿主机目录始终分别挂到容器内 `/source` 和 `/data`，更换宿主机路径只需修改 `.env` 并重新启动。
如果需要扫描多个子目录，可重复传入 `--source /source/子目录`。

源目录挂载为**可写**：默认扫描会复制保留的文件，但会把识别出的重复文件从源目录移入
`/data/quarantine`。扫描前可先运行
`docker compose exec picmanage python run.py scan --source /source --dry-run` 查看计划。
Linux 上若希望容器以当前用户写入挂载目录，可在 `compose.yaml` 的服务下设置
`user: "UID:GID"`，并确保数据目录对该用户可写。

单独使用 Docker 时也可指定挂载路径：

```powershell
docker build -t picmanage .
docker run -d --name picmanage -p 127.0.0.1:8000:8000 `
  --mount "type=bind,source=D:/Photos,target=/source" `
  --mount "type=bind,source=D:/PicManageData,target=/data" picmanage
docker exec picmanage python run.py scan --source /source
```

## 快速开始

```powershell
# 1. 先预演，看看会怎么处理（不写任何文件）
uv run python run.py scan --source D:\照片 --dry-run

# 2. 正式扫描（保留的文件默认复制，重复文件会移入隔离区）
uv run python run.py scan --source D:\照片 --source E:\旧手机备份

# 3. 看看整理结果
uv run python run.py status

# 4. 打开网页
uv run python run.py serve --port 8000
```

浏览器打开 <http://127.0.0.1:8000/>。

三种入口等价：`uv run python run.py …`、`uv run python -m picmanage …`、装好之后的
`pic-manage …`。

## 命令行

```
uv run python run.py scan --source <目录> [--source <目录2> ...] [选项]
uv run python run.py status [--json]
uv run python run.py serve [--host 127.0.0.1] [--port 8000] [--no-browser] [--debug]
uv run python run.py thumbs [--limit N] [--jobs N] [--gc]
uv run python run.py quarantine list | restore <id>... | purge <id>... --yes
uv run python run.py unsorted list
uv run python run.py verify [--fix]
uv run python run.py db path | init | vacuum
uv run python run.py clean-partials
```

### `scan` 的选项

| 选项 | 说明 |
|---|---|
| `--dry-run` / `-n` | 只打印计划，不写文件、不改数据库记录 |
| `--move --yes` | 整理后删除源文件（复制前会校验 sha256，删源失败不会丢数据） |
| `--phash` | 额外启用图片感知哈希，识别「重新保存过」的近似重复 |
| `--phash-threshold N` | 感知哈希判定阈值，默认 6（越小越严格） |
| `--mtime-fallback` | 没有内嵌时间时退回用文件修改时间（界面会标注来源） |
| `--min-precision year\|month\|day` | 归档所需的最低时间精度，默认 `year` |
| `--types image,video,audio` | 只处理这些类型 |
| `--jobs N` / `-j` | 并发数，默认 8 |
| `--min-size` / `--max-size` | 文件大小窗口（字节） |
| `--include-hidden` | 包含隐藏文件和目录 |
| `--skip-hash-singletons` | 跳过唯一大小文件的哈希计算（更快，但失去跨目录精确去重） |

## 时间是怎么取出来的

优先级从高到低，取到第一个就停：

**图片**（Pillow）
1. EXIF `DateTimeOriginal`(36867) → `DateTimeDigitized`(36868) → `DateTime`(306)
2. XMP：`xmp:CreateDate` / `photoshop:DateCreated` / `exif:DateTimeOriginal` …
3. PNG 文本块：`date:create` / `Creation Time` / `DateCreated` …
4. PNG 的 `eXIf` chunk

支持 JPEG、PNG、WebP、AVIF、HEIC/HEIF、TIFF；RAW（dng/cr2/nef/arw/…）尝试读取，读不了就
记 `none`，不会崩。

**视频**（ffprobe）
`format.tags.creation_time` → `com.apple.quicktime.creationdate` → 流级 `creation_time`
→ `WM/OriginalReleaseTime` → `date` / `originaldate` / `ICRD` …

MP4 / MOV / MKV 通常带 `creation_time`；TS / M2TS / VOB / FLV / RMVB 基本不带，
这类文件会进 `_unsorted/`。

**音频**（mutagen）

| 格式 | 读取的标签 |
|---|---|
| MP3 / DSF | ID3 `TDRC` → `TDRL` → `TDOR` → `TYER`+`TDAT`+`TIME` |
| M4A / M4B / AAX | MP4 `©day` → `purd` |
| FLAC / OGG / Opus | `originaldate` → `date` → `year` |
| WMA / ASF | `WM/OriginalReleaseTime` → `WM/Year` → `WM/EncodingTime` |
| APE / WV / TTA / MPC | APEv2 `Year` → `Date` → `Record Date` |
| WAV | ID3 chunk，以及手工解析 RIFF `LIST/INFO` 的 `ICRD` |

> WAV 需要手工解析 RIFF：实测 mutagen 在 ffmpeg 写了真实 `ICRD` chunk 时
> `WAVE(...).tags` 仍然是 `None`。

**时区**：EXIF / ID3 / Vorbis 里是**本地墙上时间**，原样保存（相机显示几点就是几点）；
ffprobe 的 `creation_time` 是 **UTC**，会转成本地时间再归档 —— 否则东八区所有视频都会
偏移 8 小时、落到错误的月份。

每个文件的 `time_source` 都记在数据库里，网页上会显示成徽章
（`EXIF` / `QuickTime` / `ID3` / `RIFF ICRD` / `文件修改时间` / `未找到内嵌时间`），
精度（`精确到秒` … `仅精确到年`）也会标出来。

## 去重规则

1. 先按 sha256 判完全重复；
2. `--phash` 再按 8×8 平均哈希判近似重复（只比较**尺寸相同**的图片，避免误判）；
3. 每一组里选出要保留的那个，排序键依次是：
   * 已在库中的文件优先（重扫重叠目录不会把自己的副本隔离掉）
   * **有真实内嵌时间的优先**于兜底时间的
   * **时间最早的优先** ← 你要的规则
   * 文件更大的优先
   * 路径更短的、字典序更小的优先（保证结果可复现）

败者移入 `data/quarantine/<来源名>/<原相对路径>`，永不覆盖、永不删除。

## 目录结构

```
pic_manage/
├── run.py                     # 命令行入口
├── pyproject.toml             # uv / pip 共用的项目与依赖声明
├── uv.lock                    # 锁定依赖版本
├── .python-version            # uv 选用的 Python 版本
├── picmanage/
│   ├── cli.py                 # 子命令与进度输出
│   ├── config.py              # 路径、扩展名表、可调参数
│   ├── console.py             # Windows 控制台编码适配
│   ├── db.py                  # SQLite：schema、upsert、审计流水
│   ├── longpath.py            # \\?\ 长路径助手
│   ├── timeparse.py           # 日期字符串解析（纯函数）
│   ├── thumbs.py              # 缩略图生成与缓存
│   ├── scanner/
│   │   ├── classify.py        # 扩展名→类型、垃圾名过滤、文件名净化
│   │   ├── walker.py          # 目录遍历与过滤
│   │   ├── hashing.py         # sha256、感知哈希
│   │   ├── probe_image.py     # 图片元数据
│   │   ├── probe_audio.py     # 音频元数据（含 WAV RIFF）
│   │   ├── probe_video.py     # 视频元数据（ffprobe）
│   │   ├── timeextract.py     # 时间提取链
│   │   ├── dedupe.py          # 去重与胜出者选择
│   │   ├── planner.py         # 目标路径与冲突解决
│   │   ├── actions.py         # 原子复制/移动/隔离
│   │   └── pipeline.py        # 阶段编排、并发、断点续跑
│   └── web/                   # Flask：路由、查询、模板、静态资源
│       ├── icons.py           # 内联 SVG 图标（发丝线、跟着 currentColor）
│       ├── static/app.css     # 唯一的样式表，自定义属性做主题
│       └── static/app.js      # 渐进增强：侧栏开关、自动提交、滑动翻页
└── data/                      # 运行时状态（已 gitignore）
    ├── index.db               # + -wal / -shm
    ├── library/YYYY/YYYY-MM/  # 归档后的媒体库
    ├── library/_unsorted/     # 没有内嵌时间的文件
    ├── quarantine/<来源>/…    # 被去重掉的重复文件
    ├── cache/thumbs/          # 缩略图缓存（按内容哈希命名）
    └── logs/
```

数据目录默认是 `<项目>/data`，可以用 `--data-dir <路径>` 或环境变量 `PICMANAGE_HOME` 改。

## 网页界面

* **左侧年/月树** —— 每个月份后面跟图片/视频/音频的数量徽章；底部是「未分类」和「重复文件」。
* **月份画廊** `/m/2021/03` —— 缩略图网格，可按类型筛选、按时间/文件名/大小排序、分页。
* **全部文件** `/all` —— 平铺列表，支持按文件名搜索、按年份筛选。
* **查看器** `/f/<id>` —— 大图 / `<video>` / `<audio>`，`←` `→` 翻页，`Esc` 返回列表，
  `i` 开关信息面板，手机可左右滑动。
* **未分类** `/unsorted` —— 列出没有内嵌时间的文件，可以直接填年月归档。
* **重复文件** `/quarantine` —— 按来源分组列出被隔离的重复文件，附原路径和判定类型。
* **状态总览** `/status` —— 各状态/类型计数、时间来源分布、最近的扫描记录。

界面文案中文，代码和注释英文；深色模式跟随系统；不依赖任何前端框架，禁用 JS 也能正常浏览。

视觉上是「仪表盘」路线而不是「卡片墙」：1px 发丝线代替投影、所有数字用等宽表格数字对齐、
单一的冷色强调色只用于表示状态（选中/悬停/聚焦）而不做装饰、背景一层会自行淡出的细网格。
图标是内联 SVG 线条图标（`picmanage/web/icons.py`），跟着 `currentColor` 走，不额外发请求，
也不会像 emoji 那样在不同平台上长得不一样。

## 可靠性

* **幂等**：重跑 `scan` 会跳过「已处理且 (大小, mtime) 未变」的文件，不会重复复制或重复隔离。
* **可中断**：每 200 个文件一个事务；复制先写 `.part` 再 `os.replace`，中断不会留下半截文件，
  残留的 `.part` 由 `uv run python run.py clean-partials` 清理。
* **不误删**：归档文件默认复制，重复文件移入隔离区；`--move` 必须显式加 `--yes`，
  且删源前校验大小和 sha256。
* **一个坏文件不影响全局**：读不了的文件记 `status='error'`，下次运行会重试。
* **不扫自己**：data 目录下的一切在遍历阶段就被排除，扫项目的父目录也不会把媒体库再吃进去。
* **长路径**：超过 240 字符的源路径自动加 `\\?\` 前缀（仅用于源侧读写）。
* **并发安全**：SQLite 用 WAL 模式，扫描写入时网页可以同时读；缩略图按内容哈希缓存，
  并发生成同一张不会互相破坏。
* **缩略图自给自足**：HEIC/AVIF 的解码器（pillow-heif）由缩略图生成路径自己注册，
  网页进程不必先跑过 `scan` 也能出图。生成不了的文件（RAW、损坏文件）缩略图位置返回
  503，网格图上显示占位图标，不会让页面挂掉。

## 常见问题

**Q：为什么有些文件在「未分类」里？**
A：文件里没有创建时间（既没有 EXIF 也没有容器元数据）。程序不拿「复制时间」冒充拍摄时间。
可以：在网页上手动指定年月；或者加 `--mtime-fallback` 重扫（界面会标注时间来源是修改时间）。

**Q：重复文件被删了吗？**
A：没有，在 `data/quarantine/` 里，路径结构和源目录一致。确认没用后
`uv run python run.py quarantine purge <id> --yes` 才会真删。

**Q：视频都没有时间？**
A：先确认 `ffprobe` 在 PATH 里（`ffprobe -version`）。另外很多 TS/M2TS/VOB/FLV/RMVB
格式本身不携带创建时间，这是格式限制，不是程序问题。

**Q：想整理完把源目录清掉？**
A：`uv run python run.py scan --source D:\照片 --move --yes`。删源前会校验副本的 sha256。
