# Mod Update Checker

给 MCDR 用的 **Mod 更新检查插件**：扫描服务端 `mods/` 目录里的每个 jar，去 **Modrinth** 和
**CurseForge** 比对，告诉你哪些 Mod 有新版本、哪些没有适配当前加载器/游戏版本的构建，以及
哪些压根查不到来源。

> **为什么需要它**：Fabric 服务端没有任何原生手段能发现 Mod 过期。加载器只是把 `mods/` 读进去、启动、
> 然后就不再过问了。这个判断只能从外部做——把本地 jar 与它真正发布的地方对比。

---

## 目录

- [功能](#功能)
- [安装](#安装)
- [命令](#命令)
- [报告怎么读](#报告怎么读)
- [配置](#配置)
- [CurseForge API Key](#curseforge-api-key)
- [网络与镜像](#网络与镜像)
- [它是怎么判断的](#它是怎么判断的)
- [可信度与已知限制](#可信度与已知限制)
- [这些结论是怎么核验的](#这些结论是怎么核验的)
- [常见问题](#常见问题)
- [开发](#开发)

---

## 功能

| | |
|---|---|
| **两级精确识别** | 先用 **文件哈希**（Modrinth，SHA-1）和 **文件指纹**（CurseForge，MurmurHash2）精确匹配；两者都查不到时，才按项目 slug **完全一致**地兜底匹配，并在报告里明确标注「近似匹配」。 |
| **结果可执行** | 报告里直接给出新版本号、项目页面链接、以及**可直接下载的文件地址**，不需要自己去搜。 |
| **自动提醒** | 服务端启动后自动查一次并把结果写进日志；管理员上线时也会收到一份结果（必要时先查一次）。 |
| **状态有意义** | 区分「可更新 / 已是最新 / 本地版本更新 / 无适配构建 / 无法定位上游 / 出错 / 不是 Mod / 已忽略」，而不是笼统地说「版本不同」。 |
| **附加提醒** | 同一个 mod id 装了两份、仅客户端 Mod 装在服务端、Mod 声明的 Minecraft 版本范围与当前服务端不符——这三件事比「版本旧」更能解释崩溃。 |
| **省请求** | 哈希查询是**批量**的：整个 mods 目录通常只需要个位数次请求，而不是每个 Mod 一次。 |
| **不打扰** | 默认只在控制台报告；游戏内提醒是可选项，且只发给权限足够的在线管理员。管理员上线时会单独收到一次结果——但他上线触发的那次检查不会再广播一遍。 |
| **中英双语** | `language: auto` 跟随 MCDR 的语言设置。 |
| **只读** | 插件**从不**改动 `mods/`。它只报告，动手的永远是管理员。 |

---

## 安装

1. 从 Releases 下载 `ModUpdateChecker-v*.mcdr`，放进 MCDR 的 `plugins/` 目录。
2. `!!MCDR reload plugin`（或重启 MCDR）。
3. 首次启动会生成 `config/mod_update_checker/config.json`。默认配置开箱可用：
   - **Modrinth 不需要 API key**，Fabric 生态里绝大多数 Mod 都在上面，直接就能用；
   - CurseForge 需要 key，没配的话插件会跳过它并给出申请提示，不影响其他功能。

没有额外 Python 依赖：`requests` 是 MCDR 自己的硬依赖，其余全部走标准库。

---

## 命令

命令需要 **MCDR 权限等级 3**（可在配置里调），两个写法都行：`!!modupdate` 与 `!!muc`。

| 命令 | 作用 |
|---|---|
| `!!modupdate` | 上次检查的汇总 |
| `!!modupdate check` | 立即检查（后台执行，不会卡住服务端） |
| `!!modupdate list` | 列出全部 Mod 及各自状态 |
| `!!modupdate list <状态>` | 只看某个状态，例如 `!!modupdate list update_available` |
| `!!modupdate status` | 显示识别到的服务端版本、加载器、上游开关、上次检查时间 |
| `!!modupdate reload` | 重载配置文件 |

检查结果同时写入 `config/mod_update_checker/last_report.json`（给脚本用）和 `last_report.txt`（给人看）。

---

## 报告怎么读

| 状态 | 含义 | 该做什么 |
|---|---|---|
| `update_available` | 存在适配当前加载器与游戏版本的新版本 | 下载并替换 jar |
| `no_compatible_build` | 项目存在，但**没有**适配当前加载器或游戏版本的构建 | 通常发生在大版本升级后。要么停用该 Mod，要么等作者更新 |
| `local_ahead` | 本地版本比上游发布过的都新 | 多半是自己编译的开发版，正常 |
| `up_to_date` | 本地文件就是最新构建 | 无需处理 |
| `unresolved` | 两个平台都不认识这个 jar | 自己编译的、重新打包过的，或者没发布过 |
| `not_a_mod` | 是合法 jar，但不含 Mod 元数据（库、数据包包等） | 无需处理 |
| `error` | 这个 jar 读取失败 | 看一下错误详情 |
| `ignored` | 在 `ignored_mods` 里被排除 | 无需处理 |

「无适配构建」还分两种，报告会写清楚是哪一种：项目**根本没发布过该加载器的构建**，还是
**发布了、但没有面向当前 Minecraft 版本**。这两种该做的事不一样，所以没有合并。

---

## 配置

`config/mod_update_checker/config.json`：

### 基本

| 选项 | 默认 | 说明 |
|---|---|---|
| `language` | `"auto"` | `auto` 跟随 MCDR；也可写 `zh_cn` / `en_us` |
| `enabled` | `true` | 总开关。关掉后只保留命令，不做任何自动检查 |
| `mods_directory` | `""` | 留空 = 服务端目录下的 `mods`。相对路径按**服务端目录**解析 |
| `loader` | `"fabric"` | `fabric` / `quilt` / `neoforge` / `forge` |
| `mc_version` | `"auto"` | `auto` = 依次从 MCDR 输出、`logs/latest.log`、Mod 元数据推断。**推断不准时请显式填写** |

### 自动检查

| 选项 | 默认 | 说明 |
|---|---|---|
| `check_on_server_start` | `true` | 服务端启动完成后自动检查一次，结果打印到控制台日志 |
| `start_check_delay_seconds` | `60` | 开服后延迟多久再查，避免和 Mod 加载抢资源 |
| `check_interval_hours` | `0` | 定时检查间隔（小时）。`0` = 关闭 |
| `notify_on_updates_only` | `true` | 自动检查时，没问题就只留一行 |
| `notify_in_game` | `false` | 有更新时是否在游戏内广播给在线管理员。默认关闭，避免打扰玩家 |
| `notify_in_game_permission` | `3` | 游戏内**广播**的最低 MCDR 权限等级 |
| `check_on_admin_join` | `true` | 管理员上线时自动检查并把结果发给他 |
| `admin_join_permission` | `3` | 多少权限等级算「管理员」。MCDR 等级 3 = admin，2 = helper |
| `admin_join_max_report_age_minutes` | `30` | 管理员上线时，多久以内的上次结果可直接复用。`0` = 每次都重查 |
| `write_report_file` | `true` | 是否把报告落盘 |

**两种游戏内通知是不同的东西**，可以分别开关：

- `notify_in_game` —— 每次检查发现更新时**广播**给当时在线的管理员；
- `check_on_admin_join` —— **管理员上线时**把结果单独发给他，即使他上线时别人已经收到过。

管理员上线触发的那次检查**不会**再广播一遍，否则刚进来的人会收到两份同样的内容。

`admin_join_max_report_age_minutes` 为什么默认 30 而不是 0：管理员进服时想要的是**立刻看到结论**，
而不是等一次完整扫描；而且几位管理员接连上线时，30 分钟的窗口能避免反复打 Modrinth 接口。
设成 `0` 就是「每次都重新检查」。

### 上游

| 选项 | 默认 | 说明 |
|---|---|---|
| `use_modrinth` | `true` | 启用 Modrinth（无需 key，按哈希精确匹配） |
| `use_curseforge` | `true` | 启用 CurseForge（需要 key，按指纹精确匹配） |
| `modrinth_api_base` | `""` | 留空 = 官方 `https://api.modrinth.com/v2`；可改成镜像 |
| `curseforge_api_base` | `""` | 留空 = 官方 `https://api.curseforge.com/v1` |
| `curseforge_api_key` | `""` | 见下一节。留空则跳过 CurseForge |
| `include_beta` | `false` | 把 beta 也算作「可用更新」 |
| `include_alpha` | `false` | 把 alpha 也算作「可用更新」 |
| `ignored_mods` | `[]` | 要忽略的 Mod，可填 mod id / jar 文件名 / 去掉扩展名的文件名（不区分大小写） |

### 网络与性能

| 选项 | 默认 | 说明 |
|---|---|---|
| `http_timeout_seconds` | `20` | 单次请求超时 |
| `http_retries` | `3` | 失败重试次数（含 429 限流与网络错误） |
| `concurrent_requests` | `4` | 并发数。调大更快，也更容易触发上游限流 |
| `requests_per_minute` | `240` | 自限速。Modrinth 官方上限 300/分钟，这里留了余量 |
| `use_resolve_cache` | `true` | 缓存「某个哈希属于哪个项目」，避免每次开服重复解析 |
| `resolve_cache_ttl_hours` | `24` | 缓存有效期。`0` = 永不过期；要彻底关闭请用 `use_resolve_cache` |

### 权限

| 选项 | 默认 | 说明 |
|---|---|---|
| `command_permission_level` | `3` | 执行 `!!modupdate` 所需的最低权限等级 |

---

## CurseForge API Key

CurseForge 的**所有**端点都要求 key：匿名访问项目查询返回 `403`，指纹查询返回 `401`，网页端接口被
Cloudflare 挡住——没有免 key 的替代路径。所以这一项要么配，要么就用 Modrinth（对 Fabric 服务端通常足够）。

申请步骤（免费，1 分钟左右）：

1. 注册 / 登录 <https://www.curseforge.com>
2. 打开 <https://console.curseforge.com>，创建 API Key
3. 填进 `curseforge_api_key`

拿到 key 之后，建议先验一次真实匹配：

```bash
python tools/cf_verify.py --api-key '$2a$10$...' --mods-dir /path/to/server/mods
```

它会把每个 jar 的指纹拿去线上查，报告命中情况，并在完全没命中时提醒你「这些 jar 可能本来就不在
CurseForge 上」——而不是让你怀疑代码。

---

## 网络与镜像

Modrinth 在中国大陆的访问质量不稳定。如果出现 `note.modrinth_unavailable`，可以：

- 把 `modrinth_api_base` 指向自建或可信的镜像；
- 或者用系统级代理（`requests` 会自动读取 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量）。

插件对这种失败是**降级而非中断**的：Modrinth 不可达时它会照样跑完 CurseForge 和名称匹配，并在报告里
写明哪个上游没连上。它绝不会因为连不上就把所有 Mod 报成「已是最新」。

---

## 它是怎么判断的

一次检查分四级，从最便宜最可靠开始，每一级只处理上一级答不出来的：

1. **Modrinth 按 SHA-1 批量识别。** 通常一两次请求就能识别整个 mods 目录，并拿到「适配当前加载器 +
   游戏版本的最新构建」。这一步能解决绝大多数 Fabric 服务端。
2. **CurseForge 按 MurmurHash2 指纹识别。** 原理相同，但 CurseForge 与 Modrinth 会把同一个发布
   重新打包成不同的字节，所以从 CurseForge 下载的 jar 的 SHA-1 在 Modrinth 上查不到，必须用指纹。
3. **按名称兜底。** 有些 jar 是自己编译的、被重新签名的，或者太旧。这时用 mod id 去匹配项目 slug，
   **只接受完全一致**的结果，并在报告里标注 `matched_by: name`。
4. **附加提醒。** 重复 mod id、仅客户端 Mod、声明的 MC 范围与当前服务端不符。

### 版本比对的取舍

MC 的 Mod 版本号是一团乱麻：`1.2.3`、`v1.2.3`、`0.162.0+26.3`、`1.19.2-0.5.3`、
`mc1.21.1-1.2.3`、`2.1.0-beta.4+build.31` 都存在。比对器的规则是：

- 数字段按**数值**比较，所以 `1.21.10 > 1.21.4`（按字典序会判反）；
- `+` 之后的构建元数据只作为**平局时**的判据，因为对 Mod 而言那段通常就是目标 MC 版本，是真实信息；
- 以 `alpha` / `beta` / `rc` / `pre` / `snapshot` 开头的多余段让版本**更旧**，所以 `1.0.0 > 1.0.0-rc.1`；
- `1.19.2-0.5.3` 里的 `-` 被当作**普通分隔符**而不是预发布标记——这是作者的写法（游戏版本 - Mod 版本），
  所以它比裸的 `1.19.2` 新；
- 完全不认识的输入**不会抛异常**，最差是返回一个字典序结论（一个畸形版本号不该让整次检查中断）。

CurseForge 的文件行不带版本字符串，所以那条路径**不看版本号**：先比文件身份，再比上传日期。
拿 `"11.68.0.1086 for Fabric 1.19.2"` 当版本号去比大小是没意义的。

---

## 可信度与已知限制

- **哈希/指纹匹配是精确的**，可以放心采信。
- **名称匹配是猜测。** 只接受完全一致的 slug，但改了作者、换了项目的情况仍可能误判。报告会标
  `matched_by: name` 并附上一句提醒。
- **CurseForge 的 key 是硬门槛。** 没有 key 就没有这条路。
- **不检查嵌套 jar。** Fabric 的 jar-in-jar 里打包的库不会单独检查（数量会在报告里说明）。
- **不做自动更新。** 插件不下载、不替换任何文件。Mod 更新可能改变配置格式或破坏存档，这个决定必须由人做。
- **`mc_version` 推断可能不准。** 报告里每一条都会写明版本来源（`config` / `server_info` / `log` /
  `mods`），来源是 `mods`（猜测）时还会额外提醒。**升级大版本后建议显式写死 `mc_version`。**

---

## 这些结论是怎么核验的

| 结论 | 核验方式 |
|---|---|
| 插件能在真实 MCDR 里加载、跑完检查、注册全部命令 | `tools/mcdr_matrix.py`：在指定解释器里起真实 MCDR + 假服务端 + 假上游，驱动全流程（`tests/test_e2e.py` 是它的 pytest 封装） |
| 能跑在 2.13 / 2.14 / 2.15 / 2.16 | 同上，跨 5 个 MCDR 版本跑矩阵；四个低版本与 2.16.0 的 19 项检查逐项一致 |
| Modrinth / CurseForge 的请求形状正确 | `tests/test_clients.py`。假上游的回答形状是**照线上实测抄的**（例如 `version_files/update` 无匹配时返回 `{}`） |
| Modrinth 哈希识别在真实数据上成立 | 拿真实 mod jar 对线上 API 跑完整流程，核对报告的版本号与下载链接 |
| CurseForge 指纹算法正确 | `tests/test_fingerprint.py` 与 `tools/murmur2_cf.js`（转写自 CurseForge 生态内的 C# 客户端）做**跨语言交叉验证**；端到端确认需要 key，用 `tools/cf_verify.py` 补 |
| 版本比对不会判反 | `tests/test_versioning.py`，含 `1.21.10 > 1.21.4` 这类反字典序用例 |
| MCDR 生命周期语义（重复注册、reload 不触发 `on_unload`） | 逐条对照安装的 MCDR 源码核实，并用 AST 级测试钉住「模块级按名注册、不得再显式注册」这条约束 |
| 真实 Modrinth 界面/行为与代码假设一致 | `tools/probe_upstream.py`——上线后上游若有变化，重跑它就能看出差别 |

测试套件 336 项，细节见 [`tests/README.md`](tests/README.md)。

**开发期间做过一次独立的只读代码审查**，确认了 7 个缺陷与 7 条存疑项并全部处理（其中一条
「上游失败被当成『没有适配构建』」会直接误导管理员，是最严重的一条）。逐条说明见
[`CHANGELOG.md`](CHANGELOG.md)。审查同时逐方法核对后确认：指纹实现忠实于参考实现、MCDR 2.13.0 API
全部可用、`tellraw` 构造无注入风险、打包器无缺陷。

---

## 常见问题

**Q：为什么大部分 Mod 都能查到，有几个查不到？**
A：那些多半不在 Modrinth 上，或者是你自己编译 / 重新打包过的。哈希对不上，插件会明说
`unresolved`，而不是猜一个给你。

**Q：装完却什么都没发生？**
A：默认要等服务端启动完成 + 60 秒（`start_check_delay_seconds`）。想立刻看到结果就
`!!modupdate check`。如果 Mods 目录识别错了，`!!modupdate status` 会显示它实际用的路径。

**Q：`no_compatible_build` 和「无法识别」有什么区别？**
A：前者是**项目找到了、但没有适配你当前配置的构建**（大版本升级后最常见）；后者是**根本不知道这个
jar 是什么**。两者该做的事完全不同，所以分成了两个状态。

**Q：升级 Minecraft 大版本后满屏 `no_compatible_build`？**
A：先确认 `!!modupdate status` 里的版本号是不是对的。如果是 `mods`（从 Mod 元数据猜的），请显式设置
`mc_version`——用错误的游戏版本去过滤，会把每个 Mod 都报成无适配构建，而这个结果**看起来和真答案一模一样**。

**Q：会不会很吃请求额度？**
A：不会。哈希识别是批量的：一个 100 Mod 的服务端通常是 3~5 次请求；只有「批量答不出来」和「需要按名称
兜底」的少数 Mod 才会各自再问一次。另有 240 次/分钟的自限速，以及 429/5xx 的自动重试与退避。

**Q：能自动更新 Mod 吗？**
A：不能，也不打算做。Mod 更新可能改配置格式、破坏存档，这个决定应该由人做。插件给你新版本号和下载链接。

---

## 开发

```bash
# 测试
python -m pip install --target .testlibs -r tests/requirements-test.txt
PYTHONPATH=.testlibs python -m pytest -m "not e2e"     # 快
PYTHONPATH=.testlibs python -m pytest                  # 含真实 MCDR 端到端

# 跨 MCDR 版本
python tools/mcdr_matrix.py --current

# 打包 + 校验产物（可复现、内容白名单、包内代码仍能编译）
python pack.py
python tools/check_artifact.py

# 上游 API 行为变了？重跑探测
python tools/probe_upstream.py
```

推送到 `main` 时会自动跑 CI（`.github/workflows/ci.yml`）：

| 作业 | 内容 |
|---|---|
| `unit` | Python 3.10 与 3.13 上跑单元与集成测试（3.10 无 `tomllib`，顺带覆盖 Forge 元数据的正则回退路径） |
| `mcdr-matrix` | **2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 各一个作业**，各自起真实 MCDR 跑完整流程 |
| `artifact` | 连打两次产物比字节（可复现）、检查包内不含 tests/tools/README、确认包内 `.py` 仍能编译 |

目录结构：

```
mod_update_checker/
├── __init__.py      MCDR 入口：配置、命令树、事件、调度、通知
├── checker.py       编排：四级识别与状态判定
├── scanner.py       mods 目录扫描与元数据解析（四种格式）
├── modrinth.py      Modrinth 客户端
├── curseforge.py    CurseForge 客户端
├── upstream.py      HTTP 层：重试、限速、可配置 base url
├── versioning.py    版本比较与 MC 版本范围匹配
├── fingerprint.py   MurmurHash2 指纹与单次遍历摘要
├── report.py        报告模型与渲染
├── serverinfo.py    MC 版本 / 加载器推断
├── i18n.py          多语言查表
└── lang/            en_us.json / zh_cn.json
```

除了 `__init__.py`，其余模块**都不 import MCDR**——这是它们能被直接单元测试的原因。

## License

MIT
