# 麦麦漫画

面向 [MaiBot](https://github.com/Mai-with-u/MaiBot) 的漫画查询与推荐插件。

支持按关键词或标签跨站搜索、查看作品详情、以及每天定时推送一部随机推荐（多渠道各抓一本，交给 LLM 挑选最合适的那本）。

---

## 一、功能

### 多渠道

| 渠道 | 搜索 | 详情 | 封面 | 分流 |
|---|---|---|---|---|
| 拷贝漫画 | ✅ | ✅ | ✅ | 8 个域名自动探活 |
| G站漫画 | ✅ | ✅ | ✅ | 7 个域名自动探活 |

两个站点都有大量镜像域名，插件内置域名列表：配置留空时按顺序探活取第一个能用的，也可以在 WebUI 里点「检测分流」看每个域名的可用性和延迟，再手动指定。

### 搜索

- **按关键词搜**：返回标题 + 链接，中文站优先
- **按标签搜**：按 tag 过滤
- 每个渠道都会分到名额，结果交错排列，不会出现「第一个渠道把名额吃光」

### 今日推荐

```
① 遍历所有已启用的渠道，每个渠道随机抓一本候选
        ↓
② 把候选列表交给 LLM 审核（标题吸引力 / 题材大众度）
        ↓
③ 选中那本补全详情（作者、简介）后推送
```

- 可以定时（每天 HH:MM 自动推）
- 也可以手动触发（`/今日推荐`）
- 群聊以聊天记录卡片发出，私聊自动降级为图文分开发
- 定时目标使用已有群号或真实会话 ID；先通过 MaiBot 聊天流接口解析，不会自动创建会话。多个账号匹配同一群时请填写唯一会话 ID。
- 每个目标分别应用会话设置；定时条数由 `daily_count` 控制，手动命令每次返回一条。
- 当天定时任务只自动尝试一次；部分发送失败会记录日志，可用手动命令补发，避免重试重复推送。

### 封面处理

封面默认原图直发。可选的本地打码服务基于 ONNX（不需要 torch），跑二次元分割 + 分类模型，检出敏感部位后做马赛克或模糊，并带漏检兜底。

### 其他

- 分群开关（`allowed_groups`）
- 按群 / 按私聊覆盖发图与打码行为
- 自动繁简转换（繁体站返回的标题和作者统一转简体）
- 代理支持（对所有渠道生效）

---

## 二、安装

```bash
cd MaiBot/plugins
git clone https://github.com/Akenari011/maibot-jm-lookup.git
```

重启 MaiBot：

```bash
systemctl restart maibot
```

然后在 WebUI「插件」页启用「麦麦漫画」。

### 依赖

```bash
pip install jmcomic httpx zhconv
```

| 包 | 用途 |
|---|---|
| `jmcomic` | 抓取漫画元数据 |
| `httpx` | 打码服务的 HTTP 调用 |
| `zhconv` | 繁简转换 |

---

## 三、命令

| 命令 | 别名 | 说明 |
|---|---|---|
| `/搜漫画 <关键词>` | `/本搜` `/bensearch` | 跨渠道搜标题，返回标题 + 链接 |
| `/搜标签 <标签>` | `/本标签` `/bentag` | 跨渠道按标签搜 |
| `/今日推荐` | `/每日推荐` `/推荐` `/daily` | 立刻推一条随机推荐 |
| `/copy <slug>` | `/拷贝` | 查拷贝漫画详情 |
| `/gm <slug>` | `/g站` | 查 G站漫画详情 |
| `/jmcheck` | — | 环境自检（打码 API / 代理 / 分流） |
| `/搜漫帮助` | `/本帮助` `/jmhelp` `/帮助` | 显示帮助 |

帮助内容会跟着配置变：关掉的功能不会出现在列表里。

---

## 四、配置

全部设置都在 WebUI「麦麦漫画 · 设置」页面，分五个折叠分组：

```
▼ 核心设置
    触发 · 渠道 · 搜索 · 今日推荐
▶ 回复与发送
▶ 打码
▶ 网络与分流
▶ 高级 · 分群 / 私聊覆盖
```

### 关键配置

| 配置 | 默认 | 说明 |
|---|---|---|
| `allowed_groups` | 空 | 启用群，留空 = 所有群 |
| `trigger_mode` | `prefix` | `prefix` 需要前缀+数字，`any` 纯数字也触发 |
| `prefixes` | 见配置页 | 触发前缀，大小写不敏感 |
| `search_enabled` | 开 | 允许 `/搜漫画` |
| `tag_search_enabled` | 开 | 允许 `/搜标签` |
| `search_max_results` | 5 | 搜索结果条数上限（1-10）|
| `t2s_enabled` | 开 | 自动繁转简 |
| `daily_enabled` | 关 | 今日推荐定时推送 |
| `daily_time` | `09:00` | 推送时间（24 小时制）|
| `daily_targets` | 空 | 推送目标，群号或会话 ID |
| `daily_count` | 1 | 每次推几条（1-5）|
| `daily_cover` | 开 | 推荐带封面 |
| `daily_forward` | 开 | 群聊以聊天记录形式发出 |
| `proxy_enabled` | 关 | 启用代理（所有渠道共用）|
| `proxy_url` | 空 | `http://127.0.0.1:7890` 或 `socks5://...` |

### 分流设置

「网络与分流」里有三个独立的检测区（jmcomic / 拷贝漫画 / G站漫画）：

- 点「检测」会**并发探活**所有内置域名，逐个显示可用性和耗时
- 下拉可手动指定，留空则自动选第一个能用的

### 分群 / 私聊覆盖

「高级」分组里可以按群号或会话 ID 单独覆盖：

| 项 | 可选值 |
|---|---|
| 发图 | 继承 / 开 / 关 |
| 打码 | 继承 / 开 / 关 |

留空表示继承全局设置。

---

## 五、本地打码服务（可选）

封面处理依赖一个本地 ONNX 服务。不部署也能用——关掉「启用打码服务」即可，插件会只发文字或原图。

### 接口

```
POST http://127.0.0.1:8000/process/
  file: 图片文件
  output_format: jpg
  mosaic_style: mosaic | blur
  fallback: mosaic | blur | skip | off
  fallback_cover_ratio: float
  min_boxes: int

返回: image/jpeg 二进制；或 {"skip": true} 表示不发
```

### 部署

推荐用单文件 Rust 服务 [jm-mask-rs](https://github.com/Akenari011/jm-mask-rs)：
不需要 Python、venv 或 pip，约 104 MB 共 4 个文件。

**自动安装**：插件首次加载时会检查打码服务，不可用就按当前平台从 Release
下载二进制和运行时包（约 53-80 MB），解压到插件数据目录并启动，不需要任何手动操作。

- 已经用 systemd 常驻打码服务时，插件检测到端口被占用会自动跳过，不会重复启动；
  也可以直接把「插件托管打码进程」关掉（`ahm_manage_process = false`）。
- 不想让插件自动下载，可以关闭「自动安装打码服务」（`ahm_auto_install = false`），
  改为按下面的步骤手动部署。

```bash
# 1) 准备四件套
#    jm-mask-rs 二进制、libonnxruntime.so、
#    segmentation_model.onnx、classification_model.onnx
mkdir -p ~/jm-mask && cd ~/jm-mask

# 2) 前台试跑
MASK_ADDR=127.0.0.1:8000 \
MASK_ORT_DYLIB="$PWD/libonnxruntime.so" \
MASK_SEG_MODEL="$PWD/segmentation_model.onnx" \
MASK_CLS_MODEL="$PWD/classification_model.onnx" \
./jm-mask-rs

# 3) 常驻
# [Service]
# WorkingDirectory=/home/user/jm-mask
# Environment=MASK_ADDR=127.0.0.1:8000
# Environment=MASK_ORT_DYLIB=/home/user/jm-mask/libonnxruntime.so
# Environment=MASK_SEG_MODEL=/home/user/jm-mask/segmentation_model.onnx
# Environment=MASK_CLS_MODEL=/home/user/jm-mask/classification_model.onnx
# ExecStart=/home/user/jm-mask/jm-mask-rs
# Restart=always
```

也可以用原版 Python 服务（AutoHajimiMosaic，`pip install onnxruntime fastapi uvicorn pillow numpy` + `uvicorn onnx_api:app`），但体积约 800 MB，启动也更慢。

> 二进制不静态链接 onnxruntime，**必须**通过 `MASK_ORT_DYLIB` 指向 `libonnxruntime.so`，否则启动即失败。只监听 `127.0.0.1`，不要暴露到公网。

### 实测性能

16 核 / 无 GPU，单张封面约 190-215 ms（模型加载后常驻）。

---

## 六、测试方法

```
/jmcheck                 # 环境自检：打码 API、代理、各站分流
/搜漫画 斗罗              # 搜索
/搜标签 yuri              # 标签搜索
/copy naoodngxueshenghui # 拷贝漫画详情
/gm wuliandianfeng-pikapi # G站详情
/今日推荐                 # 手动触发一次推荐
/搜漫帮助                 # 查看当前可用命令
```

---

## 七、已知限制

- **依赖第三方站点**：站点改版或换域名可能导致抓取失败，用「检测分流」换一个域名通常能解决
- **搜索接口不稳定**：拷贝漫画的搜索接口路径是从页面动态读取的，站点改版时需要跟进
- **G站搜索走老前端**：`www.webmota.com` 是唯一保留搜索接口的镜像，其余镜像只能查详情
- **今日推荐依赖 LLM**：LLM 不可用时会退回第一本候选
- **合并转发依赖适配器**：OneBot v11 标准模式不支持 `send_group_forward_msg`，需要适配器开启扩展模式；不支持时会自动降级为图文分开发

---

## 八、文件结构

```
jm_lookup/
├── _manifest.json       插件清单
├── plugin.py            主程序
├── config.example.toml  配置示例（config.toml 不进版本控制）
├── webui.json           WebUI 页面声明
├── webui/check.html     设置页
├── assets/              图标等资源
├── README.md
├── CHANGELOG.md
└── LICENSE
```

---

## 九、许可证

MIT，见 [LICENSE](LICENSE)。

## 十、第三方依赖

| 项目 | 许可 | 用途 |
|---|---|---|
| [jmcomic](https://github.com/hect0x7/JMComic-Crawler-Python) | MIT | 抓取漫画元数据 |
| [zhconv](https://github.com/gumblex/zhconv) | MIT | 繁简转换 |
| [httpx](https://github.com/encode/httpx) | BSD-3 | HTTP 客户端 |
| [AutoHajimiMosaic](https://github.com/AutoHajimiMosaic) | — | 可选，封面处理服务 |
