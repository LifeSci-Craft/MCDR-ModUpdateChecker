# Mod Update Checker

> MCDR 的 Mod 更新检查插件：检查服务端 `mods/` 中的 Mod 是否有适配当前 Minecraft 版本与加载器
> 的新版本，更新信息来自 **Modrinth**。

**默认只检查和报告**：不会下载文件，也不会改动 `mods/`。下载到插件文件夹、安装到 `mods/`，
都需要显式开启或用命令指定；安装只会在服务端停止后进行，并保留旧版备份。不需要 API key，也没有
额外依赖。

| | |
|---|---|
| 插件 ID | `mod_update_checker` |
| 命令 | `!!modupdate` / `!!muc` |
| 需要 | MCDR **2.13.0+**（实测 2.13.0 · 2.14.1 · 2.15.0 · 2.15.7 · 2.16.0） |
| License | MIT |

> 想改代码，或了解判定逻辑与核验方式？见 [`README-dev.md`](README-dev.md)。

## 目录

- [功能](#功能)
- [安装](#安装)
- [命令](#命令)
- [状态](#状态)
- [配置](#配置)
- [注意](#注意)
- [License](#license)

## 功能

- 用文件哈希（SHA-1）识别每个 jar 并与 Modrinth 比对；哈希查不到时按项目 slug 兜底（报告中标注「近似匹配」）
- 结果分为可更新 / 已是最新 / 无适配构建 / 查不到来源等状态（见[状态](#状态)），需要处理的问题永远排在最前
- 顺带诊断同一个 Mod 装了两份、缺必装依赖、仅客户端 Mod 装在服务端等常见问题
- 检查在自己的后台线程里运行，默认开服 60 秒后自动查一次，也可定时检查；哈希查询是批量的（上百个 Mod 通常只要个位数次请求）
- 可选下载：把新版本抓到插件文件夹，并校验哈希（`download.enabled`）
- 可选关服安装：服务端停止后装进 `mods/`，旧 jar 改名 `.old` 保留（`download.install_on_stop`）
- 旧版备份可见、可清理（见 [`cleanup`](#cleanup清理旧版备份默认关闭)，默认只展示不删除）
- 默认只在控制台报告；管理员上线通知与游戏内广播可选；中英双语

游戏内列表效果：

```
============  Mod Update Checker v1.6.0  ============
服务端: 26.3 / Fabric(版本来源：server_info)
[1] Lithium                 [版本]  [状态: ↑]  [详细信息]
[2] Sodium                 [版本]  [状态: ↑]  [详细信息]
[3] Iris                    [版本]  [状态: ✔]  [详细信息]
共 3 个 Mod。
共检查 3 个 jar：1 个最新，2 个有更新待下载，0 个已下载待安装，0 个无适配构建，0 个无法识别。
某个 Mod 的详情(版本变更与链接)：!!modupdate info <编号>。
=======  悬停 [版本] [状态]，点击 [详细信息]  =======
```

## 安装

1. 把 `ModUpdateChecker-v*.mcdr` 放进 MCDR 的 `plugins/` 目录；
2. 输入 `!!MCDR reload plugin`（或重启 MCDR）；
3. 首次启动会生成 `config/mod_update_checker/config.json`，默认配置开箱可用；
4. 输入 `!!muc check` 立即检查，或等开服 60 秒后的自动检查。

没有额外 Python 依赖：`requests` 由 MCDR 自带，其余只用标准库。

## 命令

需要 MCDR 权限等级 3（可配置）。`!!modupdate` 与 `!!muc` 等价；不带子命令打开帮助页。

| 命令 | 作用 |
|---|---|
| `!!modupdate`（= `!!modupdate help`） | 帮助页 |
| `!!modupdate check` | 立即检查一次 |
| `!!modupdate list [状态] [页码]` | 列出 Mod，可按状态筛选、翻页 |
| `!!modupdate summary` | 上次检查的汇总 |
| `!!modupdate info <编号 / Mod 名>` | 某个 Mod 的详情 |
| `!!modupdate download <编号 / Mod 名 / all>` | 下载新版本；`all` = 全部待下载 |
| `!!modupdate install <编号 / Mod 名 / all>` | 安排关服时安装（文件需已下载）；`all` = 全部已下载 |
| `!!modupdate delete <文件名 / 编号 / all>` | 删除旧版备份（需先开 `cleanup.allow_delete`） |
| `!!modupdate cleanup` | 删除所有已过期的旧版备份（同上） |
| `!!modupdate confirm` | 确认上一条 `download` / `install` / `delete` / `cleanup` |
| `!!modupdate status` | 显示服务端、配置与上次检查的状态 |
| `!!modupdate reload` | 重载配置文件 |

位置可以填四种写法：**编号**（来自 `list`）、**Mod 名**（打一半也行）、**mod id**、**jar 文件名**；
控制台支持 Tab 补全。

`download` / `install` / `delete` / `cleanup` 都是两步操作：先列出将要做什么，再输入
`!!modupdate confirm` 确认（120 秒内有效，只有发起人能确认）。

## 状态

列表里每个 Mod 显示一个状态图标：

| 图标 | 含义 |
|---|---|
| `✔` | 已是最新（`up_to_date`） |
| `↑` | 有新版可下载（`update_available`）——用 `!!modupdate download <编号>` |
| `↓` | 已下载待安装（`awaiting_install`）——用 `!!modupdate install <编号>`，关服时执行 |
| `❌` | 没有适配当前加载器或游戏版本的构建（`no_compatible_build`） |
| `☆` | 本地版本比上游新（`local_ahead`） |
| `?` | Modrinth 认不出（`unresolved`）——自己编译或重新打包过，见[手动对应](#sources去哪里查) |
| `○` | 不是 Mod（`not_a_mod`，库、数据包等） |
| `✘` | 读取失败或查询出错（`error`） |
| `⏪` | 旧版备份（`old_backup`） |
| `—` | 已在 `check.ignored_mods` 中忽略（`ignored`） |

哈希匹配是精确的；按名称兜底是近似判断，报告中会标注。

## 配置

配置文件：`config/mod_update_checker/config.json`。升级后新增的选项会自动按默认值补齐并重写文件。
下面每张表的选项名就是文件中的完整路径。

### 顶层

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关。关掉后只保留命令 |
| `language` | `"auto"` | `auto` 跟随 MCDR；可写 `zh_cn` / `en_us` |
| `command_permission_level` | `3` | 执行 `!!modupdate` 的最低 MCDR 权限等级 |

</details>

### `server`：扫描对象

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `server.mods_directory` | `""` | 留空 = 服务端目录下的 `mods`（相对路径按服务端目录解析） |
| `server.loader` | `"fabric"` | `fabric` / `quilt` / `neoforge` / `forge` |
| `server.mc_version` | `"auto"` | `auto` = 自动推断；**推断不准时请显式填写** |

</details>

### `check`：更新检测

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `check.on_server_start` | `true` | 启动完成后自动检查一次，结果写进控制台 |
| `check.start_delay_seconds` | `60` | 开服后延迟多久再查，避开 Mod 加载 |
| `check.interval_hours` | `0` | 定时检查间隔（小时）；`0` = 关闭 |
| `check.include_beta` | `false` | beta 也算作可用更新 |
| `check.include_alpha` | `false` | alpha 也算作可用更新 |
| `check.ignored_mods` | `[]` | 完全不做检测的 Mod（填 mod id / 文件名 / 去掉 `.jar` 的文件名）；不产生请求，报告中仍会列出 |

</details>

### `report`：结果送到哪里

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `report.updates_only` | `true` | 自动检查时没问题就只留一行 |
| `report.on_admin_join` | `true` | 管理员上线时把结果单独发给他（必要时先查一次） |
| `report.admin_permission` | `3` | 多少权限等级算「管理员」 |
| `report.reuse_report_minutes` | `1440` | 上线通知可复用的最近结果时限；`0` = 每次都重查 |
| `report.in_game` | `false` | 发现更新时在游戏内广播给在线管理员 |
| `report.in_game_permission` | `3` | 广播的最低权限等级 |
| `report.write_file` | `true` | 报告落盘（`last_report.json` / `last_report.txt`） |

</details>

### `sources`：去哪里查

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `sources.modrinth.enabled` | `true` | 是否查询 Modrinth |
| `sources.modrinth.api_base` | `""` | 留空 = 官方；可改镜像，也支持系统代理（`HTTP_PROXY` / `HTTPS_PROXY`） |
| `sources.manual_map` | `"project-map.json"` | 手动对应清单的文件名，放在插件数据文件夹（`""` = 关闭） |

自己编译或重新打包过的 jar（上游认不出）可以放一份 `project-map.json` 手动对应，例如
`{"by_mod_id": {"mycustommod": "lithium"}}`：`by_mod_id` 按 Mod id 匹配（推荐，重编译后 id 不变），
`by_sha1` 按文件字节匹配（最准）；值填项目 slug 或项目 id，改完 `!!modupdate reload` 生效。

</details>

### `download`：下载与关服安装（默认关闭）

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `download.enabled` | `false` | 发现更新时自动下载到插件文件夹 |
| `download.folder_name` | `"downloads"` | 下载子文件夹名（不能填路径） |
| `download.max_size_mb` | `128` | 单个文件大小上限 |
| `download.install_on_stop` | `false` | 关服后自动安装已下载的更新（不依赖 `download.enabled`） |
| `download.retries` | `3` | 下载重试次数（总尝试 = 1 + 该值） |

两个开关都关着也能用：`!!modupdate download` / `install` 可随时手动指名。

打开 `install_on_stop` 后，安装始终在**服务端停止后**执行，且只处理插件自己下载过的文件：旧 jar
改名 `<原名>.old` 保留（同名时用 `.old.2`，绝不覆盖），安装前校验哈希、失败回滚；文件名开头的中
括号备注（如 `[锂]`）会跟到新文件名上。

</details>

### `cleanup`：清理旧版备份（默认关闭）

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `cleanup.allow_delete` | `false` | 删除总开关；关着时一切删除命令被拒绝 |
| `cleanup.enabled` | `false` | 有过期备份时提醒管理员（前提：`allow_delete` 已开） |
| `cleanup.max_age_days` | `30` | 多少天算过期；`0` = 立即过期 |

旧版备份照常显示在列表里（状态 `⏪`），一行长这样：

`[2] retired.jar.old          [备份]  [状态: ⏪]  [详细信息]`

删除默认关闭。先打开 `cleanup.allow_delete`，`delete` / `cleanup` 才可用（都要 `!!modupdate confirm`）；
再打开 `cleanup.enabled` 才会有到期提醒（排在 `allow_delete` 之后）。默认保留期 30 天，刚产生的备份
不会被 `cleanup` 删除。

</details>

### `network`：超时与限速

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `network.timeout_seconds` | `20` | 单次请求超时 |
| `network.retries` | `3` | 失败重试次数（含 429 限流与网络错误） |
| `network.concurrent_requests` | `4` | 并发数。调大更快，也更容易触发上游限流 |
| `network.requests_per_minute` | `240` | 自限速（Modrinth 官方上限 300/分钟） |
| `network.cache.enabled` | `true` | 记住「这个 jar 上游不认」，下次不重复查询 |
| `network.cache.ttl_hours` | `24` | 该结论的有效期；`0` = 永不过期 |

</details>

## 注意

- 重新打包过的 jar 可能认不出（哈希会变），可用 `project-map.json` 手动映射。
- 不单独检查 jar 内嵌套的 jar；「缺少依赖」提醒可能误报（依赖打包在其他 jar 内时看不到）。
- Modrinth 不可达时检查照常完成，报告会写明上游不可达，不会把 Mod 错报成「已是最新」；可配置镜像或系统代理。
- 游戏版本是自动推断的；升级大版本后如大量出现 `no_compatible_build`，先用 `!!modupdate status` 确认识别到的版本。

## License

MIT
