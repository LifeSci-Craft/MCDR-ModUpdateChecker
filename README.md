# Mod Update Checker

> MCDR 的 **Mod 更新检查插件**：扫描服务端 `mods/` 里的每个 jar，与 **Modrinth** 比对，列出哪些
> Mod 有新版本、哪些没有适配当前加载器或游戏版本的构建、哪些查不到来源。

不需要任何 API key，也不需要额外依赖。**默认只读**：只有在你明确要求时它才会改动 `mods/`，方式见
[关服后自动安装](#关服后自动安装默认关闭)。

| | |
|---|---|
| 插件 ID | `mod_update_checker` |
| 命令 | `!!modupdate` / `!!muc` |
| 需要 | MCDR **2.13.0+**（实测 2.13.0 · 2.14.1 · 2.15.0 · 2.15.7 · 2.16.0） |
| License | MIT |

> 想了解判定逻辑与核验方式，或者想改代码？见 [`README-dev.md`](README-dev.md)。本页只讲怎么用：
> 安装、命令、配置、结果怎么读。

## 目录

- [为什么需要它](#为什么需要它)
- [它做什么](#它做什么)
- [安装](#安装)
- [命令](#命令)
  - [手动下载与安装：两步确认](#手动下载与安装两步确认)
  - [列表与详情是分开的](#列表与详情是分开的)
- [报告的读法](#报告的读法)
- [排除某些 Mod 不检查](#排除某些-mod-不检查)
- [自己编译的 Mod 查不到怎么办](#自己编译的-mod-查不到怎么办)
- [网络与镜像](#网络与镜像)
- [配置](#配置)
  - [`server`](#server扫描对象) · [`check`](#check更新检测) · [`report`](#report结果送到哪里) · [`sources`](#sources去哪里查)
  - [`download`](#download下载与关服安装默认关闭) · [`cleanup`](#cleanup清理旧版备份默认关闭) · [`network`](#network超时与限速) · [关服后自动安装](#关服后自动安装默认关闭)
- [可信度与已知限制](#可信度与已知限制)
- [常见问题](#常见问题)
- [License](#license)

## 为什么需要它

Fabric 服务端不会主动检查 Mod 是否过期：`mods/` 里放了什么就加载什么。要发现新版本，只能把本地 jar
与发布平台上的记录做比对。本插件用文件哈希确定每个 jar 的身份，比按文件名猜更可靠。

## 它做什么

| | |
|---|---|
| **按哈希识别** | 用 jar 自身的 **SHA-1** 精确匹配；查不到时才按项目 slug **完全一致**地兜底，并在报告里标注「近似匹配」 |
| **结果可执行** | 直接给出新版本号与**项目页链接**。每个界面只占一行，不刷屏 |
| **自动提醒** | 开服后自动检查一次，结果写进控制台；管理员上线时也会单独收到一份 |
| **状态明确** | 区分「可更新 / 已是最新 / 本地版本更新 / 无适配构建 / 查不到来源 / 出错 / 不是 Mod / 已忽略」，而不是笼统的「版本不同」 |
| **顺带诊断** | 同一个 mod id 装了两份、仅客户端 Mod 装在服务端、声明的 MC 版本范围不符、缺必装依赖。这些比「版本旧」更常解释崩溃 |
| **省请求额度** | 哈希查询是**批量**的：上百个 Mod 通常只要个位数次请求 |
| **可选下载** | （默认关闭）把新版本下载到插件自己的文件夹，并校验哈希 |
| **可选安装** | （默认关闭）在**服务端停止后**把下载好的版本装进 `mods/`，旧 jar 改名 `.old` 保留 |
| **不打扰** | 默认只在控制台报告。游戏内提醒是可选项，且只发给权限足够的在线管理员 |
| **中英双语** | `language: auto` 跟随 MCDR 的语言设置 |

> **默认状态下它不改动 `mods/`**：只检查、只报告，下载也只落到插件自己的数据文件夹。写入 `mods/`
> 需要显式开启 `download.install_on_stop`，或者用 `!!muc install` 逐个授权；两者都只在**服务端已经
> 停止**之后执行。Mod 更新可能改配置格式或破坏存档，所以这一步始终由人来决定，插件负责执行到位、
> 留下退路。

## 安装

1. 把 `ModUpdateChecker-v*.mcdr` 放进 MCDR 的 `plugins/` 目录；
2. 输入 `!!MCDR reload plugin`，或重启 MCDR；
3. 首次启动会生成 `config/mod_update_checker/config.json`。默认配置开箱可用，**不需要填任何 key**。

没有额外 Python 依赖：`requests` 本来就是 MCDR 的硬依赖，其余全部走标准库。

## 命令

命令需要 **MCDR 权限等级 3**（可在配置里调），`!!modupdate` 与 `!!muc` 两种写法都行。
不带子命令输入 `!!muc` 就是帮助页：每一行都可以直接点击，悬停还会显示一句说明（例如 `list` 那一行
会列出能筛选的全部状态）；点击说明文字同样会把命令填进输入框：

```
============  Mod Update Checker v1.6.0  ============
用法：!!muc <子命令>(!!modupdate 亦可)——不带子命令就是本页
以上命令均需 MCDR 权限等级 3(与游戏内是否为 OP 无关)
!!muc check    -- 立即检查一次
!!muc list      -- 列出所有 Mod
!!muc summary  -- 查看上次检查的汇总
!!muc info     -- 查看某个 Mod 的详情
!!muc download -- 下载 Mod 的新版本
!!muc install   -- 安排关服时安装
!!muc delete   -- 删除旧版备份
!!muc cleanup  -- 删除所有已过期的备份
!!muc confirm  -- 确认上一条操作
!!muc status   -- 显示服务端与插件状态
!!muc reload   -- 重新读取配置文件
!!muc help     -- 显示本帮助
==========  点击命令可直接执行或填入参数  ===========
```

| 命令 | 作用 |
|---|---|
| `!!modupdate` | 帮助页（与 `!!modupdate help` 相同） |
| `!!modupdate check` | 立即检查（在后台线程执行，不会卡住服务端） |
| `!!modupdate list` | 列出全部 Mod，**一行四格**：名称（过长截成 `...`，悬停看全名）、绿色的 `[版本]`（悬停看具体版本）、`[状态: ✔]`（图标说明状态，悬停解释），以及可点的 `[详细信息]`（悬停说明它会执行什么）。超出一页时底部出现 `[上一页]` / `[下一页]` |
| `!!modupdate list <状态> [页码]` | 只看某个状态，例如 `!!modupdate list update_available`；`!!modupdate list awaiting_install 2` 是它的第二页 |
| `!!modupdate summary` | 上次检查的汇总（不重新检查） |
| `!!modupdate info <编号 / Mod 名>` | 某个 Mod 的详情：版本变更、项目页，以及这一步该做什么的可点按钮 |
| `!!modupdate download <编号 / Mod 名 / all>` | 从 Modrinth 下载这一个 Mod 的新版本，`all` 表示所有待下载的（见下） |
| `!!modupdate install <编号 / Mod 名 / all>` | 安排下次关服时把它装进 `mods/`，`all` 表示所有已下载的（见下） |
| `!!modupdate delete <文件名 / 编号 / all>` | 删除一个**旧版备份**（`.old` 文件）；`all` = **全部备份，不看天数**。**默认关闭**：要先把 `cleanup.allow_delete` 设为 `true`，见[旧版备份](#cleanup清理旧版备份默认关闭) |
| `!!modupdate cleanup` | 删除**所有已过期**的旧版备份（超过 `cleanup.max_age_days` 天的那些），也就是 `delete` 里加了年龄那一条。同样要先打开 `cleanup.allow_delete` |
| `!!modupdate confirm` | 确认上一条 `download` / `install` / `delete` / `cleanup` |
| `!!modupdate status` | 显示识别到的服务端版本、加载器、配置文件路径、上游开关、本地映射表、上次检查时间、旧版备份、已排除的 Mod |
| `!!modupdate reload` | 重载配置文件 |
| `!!modupdate help` | 帮助页（每行可点击，悬停看每条的说明） |

填给 `info` / `download` / `install` 的位置**四种写法都认**（大小写、空格、连字符都不计较，
`fabric api` = `Fabric-API`）：

| 写法 | 例 |
|---|---|
| **编号** | `3`（来自 `!!modupdate list`，最短） |
| **Mod 名** | `Sodium`，**打一半也行** |
| **mod id** | `sodium` |
| **jar 文件名** | `sodium-0.5.9.jar`（带不带 `.jar` 都行） |

**名字只打开头几个字母就够了**：`!!modupdate download sod` 能唯一对上一个 Mod 时就直接用那个；
对不上唯一一个时会**列出候选**，每个候选都能点，点一下把完整命令填进输入框，按回车即可。
**在 MCDR 控制台里还能按 Tab 补全**（`!!muc download ` 会列出能下载的那些 Mod，
`!!muc list ` 会列出所有状态名）；游戏内做不到这一点（原版的 Tab 补全只对 `/` 开头的命令有效）。

名字**同时匹配到多个、又收窄不了时会被拒绝**并列出候选：猜错意味着下载或安装错的 jar，所以
宁可让你多打几个字。`all` 是保留字，永远指「全部」。

### 手动下载与安装：两步确认

`download` 与 `install` **都不依赖任何自动开关**：自动开关回答的是「发现了就全都抓下来」，
而这两个命令回答的是「我只要这一个」。两个命令都会先列出**将要发生什么**，再等你确认：

```
> !!modupdate download 1
即将从 Modrinth 下载 1 个：
Sodium  1.0.0 -> 1.1.0
文件名：sodium-fabric-1.1.0.jar(1.2 MB)
请在 120 秒内输入 !!modupdate confirm 确认；重新输入本命令可替换这次待确认的操作。

> !!modupdate confirm
[Mod Update Checker] 已开始下载 Sodium 1.1.0，完成后在这里告诉你结果。
```

游戏里那句 `!!modupdate confirm` 是**亮红色**的按钮：点一下只会把命令填进输入框（不会直接执行），
按回车才会开始。

把位置换成保留字 `all` 就是一次处理一批（`!!modupdate download all` / `!!modupdate install all`），
流程完全一样，只多一行结果统计：`批量下载结束：2 个已下载，0 个早已存在，0 个失败，0 个跳过`。

几条规则：

- **`install` 要求文件已经下载好**：没下载就提示你先用 `download`。
- **只有发起这条操作的人能 `confirm`**：别人输入会被拒绝，并说明是谁发起的。
- **确认 120 秒内有效**：过期、重载配置、或者中间跑过一次检查，都会让待确认的操作作废（编号是「当时那份报告」的映射，报告换了，同一个编号可能已经是另一个 Mod）。
- **`install` 只动你点名的那些**：授权记录是逐条的，`!!muc install all` 也是逐条写上去的。
- `!!modupdate info <编号>` 的详情页底部会出现对应的 `[下载此版本]` / `[安排安装]`，点一下就等于输入了上面的命令。

### 列表与详情是分开的

`!!modupdate list` 一行一个 Mod，正好在一页聊天框内读完：

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

每一屏的底部都有一条与标题栏同宽的 `====` 收尾线：列表与汇总用它写「哪两块能交互」
（`悬停 [版本] [状态]，点击 [详细信息]`），帮助页写「点击命令可直接执行或填入参数」；控制台那一侧
是一条素线，因为终端里没有悬停和点击。

行里每一块悬停都有更多信息：

- **名称**：悬停显示全名。
- **`[版本]`**（绿色）：悬停显示具体版本。有更新时是红色的旧版本 `>>>` 绿色的新版本；已是最新时是一个黄色版本。
- **`[状态: ✔]`**：图标本身就是状态，颜色按状态区分（打叉红、打勾绿、有更新与待安装蓝）：`✔` 已是最新 / `↑` 有新版可下载 / `↓` 已下载待安装 / `❌` 无适配构建 / `⏪` 旧版备份 / `✘` 出错 / `☆` 本地比上游新 / `?` 认不出上游 / `○` 不是 Mod / `—` 配置里忽略。悬停后浮窗第一行是状态名（同色），后面接「这是什么情况」与识别方式（例如「按名称近似匹配」）。
- **`[详细信息]`**（aqua）：悬停说明它执行的是 `!!modupdate info <Mod 名>`。
- **备份与无法识别的 jar**：没有版本可看，中格分别是 `[备份]` 与 `[文件]`，大小、天数、文件名在悬停里。

控制台与日志里没有悬停，所以那里的行直接印出版本数字、事实与整句状态。

列宽按实测的字宽表补白：游戏字体是比例字体，各字符宽度不同，按「东亚字符算两个」去补空格会让列
参差。补白的最小单位是一个空格，所以落点还有半个字母以内的误差；要做到分毫不差，客户端需要使用
等宽字体。

需要处理的 Mod 排在最前，并且不会有任何一行被丢掉。一页装不下时底部出现翻页条（标题栏样式，
走到头的一侧变灰、点不动）：

```
==============  [上一页] 2/3 [下一页]  ==============
```

页码可以直接手打（`!!modupdate list 3`、`!!modupdate list awaiting_install 2`）；越界页码会落在
最后一页，并把页码（`N/M`）如实写出；筛选过的列表继续在筛选内翻页。**控制台里没有按钮**，那一行
换成可以直接输入的命令（`第 2/3 页  下一页：!!modupdate list 3`）。

**旧版备份也在同一个列表里**（状态 `old_backup`），编号同样是手柄，`!!modupdate delete 3` 指的就是它，
前提是 `cleanup.allow_delete` 已经打开。一行长这样：
`[2] retired.jar.old          [备份]  [状态: ⏪]  [详细信息]`（中格是 `[备份]`，大小与留了多久在**悬停**里；四格与别的行一样对齐）。见
[旧版备份](#cleanup清理旧版备份默认关闭)。

**编号在筛选时也不变**，也不会因为一次下载而漂移：一个 Mod 从「待下载」变成「待安装」时，它在列表
里的位置不动，所以插件提示的 `!!modupdate install 3` 指的还是同一个 Mod。

点某一行的 `[详细信息]`（或输入 `!!modupdate info 3`）展开那一个 Mod：

```
============  Mod Update Checker v1.6.0  ============
Sodium
状态: 可更新
版本: 1.0.0 -> 1.1.0
项目页: [点击打开]
操作: [下载此版本]
声明支持 Minecraft >=26.1 <27
=====================================================
```

详情里只有**两个**可点的入口：`[点击打开]` 打开项目页（完整地址在鼠标悬停时显示）；`[下载此版本]`
就是输入 `!!modupdate download 3`。

检查结果同时写入 `config/mod_update_checker/last_report.json`（给脚本用）与 `last_report.txt`
（给人看，保留完整明细，不受聊天框页数限制）。

## 报告的读法

| 状态 | 含义 | 该做什么 |
|---|---|---|
| `update_available` | 有适配当前加载器与游戏版本的新版本，**尚未下载** | `!!modupdate download <编号>` 下载，或开启自动下载 |
| `awaiting_install` | 新版本**已经下载到插件文件夹**，还没放进 `mods/` | `!!modupdate install <编号>` 安排关服时替换，或自己复制进 `mods/` |
| `no_compatible_build` | 项目存在，但**没有**适配当前加载器或游戏版本的构建 | 通常发生在大版本升级后。停用该 Mod，或等作者更新 |
| `local_ahead` | 本地版本比上游发布过的都新 | 多半是自己编译的开发版，正常 |
| `up_to_date` | 本地文件就是最新构建 | 无需处理 |
| `unresolved` | Modrinth 不认识这个 jar | 自己编译、重新打包或从未发布过。**写一行 `project-map.json` 就能解决**，见[自己编译的 Mod 查不到怎么办](#自己编译的-mod-查不到怎么办) |
| `not_a_mod` | 是合法 jar，但不含 Mod 元数据（库、数据包等） | 无需处理 |
| `error` | 这个 jar 读取失败 | 看错误详情 |
| `ignored` | 在 `check.ignored_mods` 里被排除 | 无需处理 |
| `old_backup` | **旧版备份**：插件装更新时留下的 `.old` 文件 | 确认不需要回滚了就删掉（需先开 `cleanup.allow_delete`），见[旧版备份](#cleanup清理旧版备份默认关闭) |

「无适配构建」还分两种，报告会写清是哪一种：项目**根本没发布过该加载器的构建**，还是
**发布过、但没有面向当前 Minecraft 版本**。

报告末尾还会给出几条**与「有没有新版本」无关、但更常解释崩溃**的提醒（存在任何一条时才显示）：

| 提醒 | 含义 |
|---|---|
| 仅客户端 Mod | `mods/` 里装了只在客户端起作用的 Mod，服务端上等于没装 |
| Modrinth 标记为不支持服务端 | Modrinth 认为这个项目不能在服务端跑（有些 Mod 的 jar 自己没声明，所以这条能补上） |
| Mod ID 装了两份 | 同一个 Mod 有两个 jar，加载器只会挑一个，而且不一定是你要的那个 |
| **缺少依赖** | 某个 Mod 声明的**必装依赖**在 `mods/` 里找不到，这通常就是启动报错的原因 |
| Minecraft 版本靠猜 | 版本是从 Mod 元数据推断出来的，不是从服务端输出读到的；升级大版本后建议显式写死 |

**「缺少依赖」会误报**：依赖如果被打包在别的 jar 内部（Fabric 的 jar-in-jar），或者被放进了
`.disabled` / `.old` 文件里，插件都看不见，所以写的是「找不到」而不是「缺失」。反之，只可能在
客户端用的依赖不会出现在这条提醒里；被 Modrinth 标成「不支持服务端」的，只在那个 Mod 的详情里注明。

**「有更新待下载」和「已下载待安装」是两组**，因为下一步动作完全不同。一个 Mod 只会出现在其中一组，
所以**已经下载过的不会在下次启动时被重复提醒**，它会变成「已下载，待安装」。

## 排除某些 Mod 不检查

不想更新的 Mod（自己写的、固定在某个版本的）填进 `check.ignored_mods`，mod id、文件名、
去掉 `.jar` 的文件名三种写法都可以：

```json
"check": { "ignored_mods": ["my-own-mod", "pinned-lib.jar", "Fabric API"] }
```

**它是「不做检测」，不是「隐藏结果」**：被列出的 Mod **不会产生任何网络查询**，不占请求额度，
也不会出现在「有更新」或「已下载」列表里；但报告里仍会列出它、状态标为 `ignored`，方便你确认配置
真的生效了，而不是把名字拼错、静默地什么都没发生。

## 自己编译的 Mod 查不到怎么办

插件靠**文件字节**认人：哈希对不上就找不到，自己编译、重新打包、改过签名的 jar 都算这一类。
如果你知道它是哪个项目，告诉它就行：在插件数据文件夹里放 `config/mod_update_checker/project-map.json`
（文件名由 `sources.manual_map` 决定）：

```json
{
  "version": 1,
  "by_sha1":   { "把40位小写哈希填在这里": "sodium" },
  "by_mod_id": { "mycustommod": "lithium" }
}
```

- **`by_mod_id`**：按 Mod 自己的 id 认。**重新编译之后哈希会变，但 id 不变**，所以推荐先用这个；
- **`by_sha1`**：按文件字节认，最准。两个都写了以它为准（哈希指向这一个文件，mod id 指向所有用过这个名字的东西）；
- 值可以填项目的 **slug**（网址里 `modrinth.com/mod/<这里>` 那一段）或项目 id；
- 这份清单**只读不写**，改完 `!!modupdate reload` 或等下次检查即可生效；命中的 Mod 在报告里标为 `matched_by: manual`。

填错了会怎样？如果清单把某个 jar 指向一个 Modrinth 上不存在的项目，报告会**明确写出这个引用找不到**，
而不是退回按名字猜。`!!modupdate status` 会显示这份清单有没有被读到、里面有几条；没生效时它会说明
原因（文件不存在、JSON 写错、`sources.manual_map` 填成了路径…）。

## 网络与镜像

Modrinth 在中国大陆的访问质量不稳定。如果报告出现「上游不可达」，可以把 `sources.modrinth.api_base`
指向自建或可信的镜像，或者用系统级代理（`requests` 会读 `HTTP_PROXY` / `HTTPS_PROXY`）。

上游不可达时是**降级而非中断**：插件会照常跑完名称匹配，并在报告里写明上游没连上。它**不会**因为
连不上就把所有 Mod 报成「已是最新」；那看起来像好消息，却是最糟的失败方式。

## 配置

配置文件：`config/mod_update_checker/config.json`。**选项按功能分组**，顶层只有三个最常改的开关，
其余各成一节：

```jsonc
{
  "enabled": true,                    // 总开关
  "language": "auto",                 // 消息语言
  "command_permission_level": 3,      // 谁能用 !!modupdate

  "server":   { /* 扫描对象：目录、加载器、游戏版本 */ },
  "check":    { /* 更新检测：何时查、什么算更新、哪些不查 */ },
  "report":   { /* 结果怎么送到你手上：控制台、报告文件、游戏内通知 */ },
  "sources":  { /* 去哪里查更新（目前只有 Modrinth） */ },
  "download": { /* 可选：把新版本下到插件自己的文件夹 */ },
  "cleanup":  { /* 可选：能不能删、要不要提醒、多久算过期 .old 备份 */ },
  "network":  { /* HTTP 超时、重试、并发限速、识别缓存 */ }
}
```

下面每张表的选项名都带上章节，也就是文件里的完整路径（`check.ignored_mods` 表示 `check` 章节下的
忽略名单）。

**升级后被新增的选项会自动出现，而且会告诉你。** MCDR 加载插件时会把文件里缺少的选项按默认值补齐
并重写文件，插件把补了什么打进控制台：

```
[Mod Update Checker] 配置文件缺少 2 个选项，已按默认值补上：download.install_on_stop, sources.manual_map(config\mod_update_checker\config.json)
```

### 顶层

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关。关掉后只保留命令，不做任何自动检查 |
| `language` | `"auto"` | `auto` 跟随 MCDR；也可写 `zh_cn` / `en_us` |
| `command_permission_level` | `3` | 执行 `!!modupdate` 所需的最低 MCDR 权限等级 |

</details>

### `server`：扫描对象

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `server.mods_directory` | `""` | 留空 = 服务端目录下的 `mods`。相对路径按**服务端目录**解析 |
| `server.loader` | `"fabric"` | `fabric` / `quilt` / `neoforge` / `forge` |
| `server.mc_version` | `"auto"` | `auto` = 依次从 MCDR 输出、`logs/latest.log`、Mod 元数据推断。**推断不准时请显式填写** |

</details>

### `check`：更新检测

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `check.on_server_start` | `true` | 服务端启动完成后自动检查一次，结果写进控制台日志 |
| `check.start_delay_seconds` | `60` | 开服后延迟多久再查，避开 Mod 加载 |
| `check.interval_hours` | `0` | 定时检查间隔（小时）。`0` = 关闭 |
| `check.include_beta` | `false` | 把 beta 也算作「可用更新」 |
| `check.include_alpha` | `false` | 把 alpha 也算作「可用更新」 |
| `check.ignored_mods` | `[]` | **完全不做更新检测**的 Mod，见[排除某些 Mod 不检查](#排除某些-mod-不检查) |

</details>

### `report`：结果送到哪里

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `report.updates_only` | `true` | 自动检查时，没问题就只留一行 |
| `report.on_admin_join` | `true` | 管理员上线时把结果单独发给他（必要时先查一次） |
| `report.admin_permission` | `3` | 多少权限等级算「管理员」，即上面那条通知的收件人 |
| `report.reuse_report_minutes` | `1440` | 管理员上线时，多久以内的上次结果可直接复用。`0` = 每次都重查 |
| `report.in_game` | `false` | 发现更新时是否在游戏内**广播**给在线管理员。默认关闭，避免打扰玩家 |
| `report.in_game_permission` | `3` | 游戏内广播的最低 MCDR 权限等级 |
| `report.write_file` | `true` | 是否把报告落盘 |

几点说明：

- `report.in_game` 与 `report.on_admin_join` 是两个独立的功能：前者在检查发现更新时**广播**给当时在线的管理员，后者只在管理员上线时把结果单独发给他。管理员上线触发的那次检查不会重复广播，否则刚上线的人会收到两份。
- 两个权限阈值分开设置：`report.admin_permission` 决定「谁算管理员」，`report.in_game_permission` 决定「谁能收到广播」，可以只通知、不广播。
- 复用窗口不只按时间判断：Mods 目录、加载器或写死的游戏版本变了，旧结果一律作废，避免拿到上一个服务端的结论。报告文件在重启后会被读回，所以这个窗口跨重启有效；`report.write_file: false` 等于关掉这个功能。
- 每次检查还会和上次比一比，在「有更新」一节末尾多写一行「其中 N 项是上次检查之后新出现的」，用来区分刚发布的更新和已经存在多时的。

</details>

### `sources`：去哪里查

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `sources.modrinth.enabled` | `true` | 是否查询 Modrinth |
| `sources.modrinth.api_base` | `""` | 留空 = 官方 `https://api.modrinth.com/v2`；可改成镜像 |
| `sources.manual_map` | `"project-map.json"` | 你自己写的「哪个 jar 对应哪个项目」清单。填**文件名，不是路径**，放在插件数据文件夹里；填 `""` 即关闭 |

</details>

### `download`：下载与关服安装（默认关闭）

<details><summary>展开选项与说明</summary>

这一节管两件事：要不要自动抓取新版本，以及要不要在关服后自动安装。两个开关相互独立：只想安装一个
已经下载好的文件时，不必先允许它去抓更多。

| 选项 | 默认 | 说明 |
|---|---|---|
| `download.enabled` | `false` | 下载总开关。发现更新时把新版本抓到插件自己的文件夹 |
| `download.folder_name` | `"downloads"` | 下载到哪个子文件夹（**只能填文件夹名，不能填路径**） |
| `download.max_size_mb` | `128` | 单个文件大小上限，超过就跳过 |
| `download.install_on_stop` | `false` | **关服后自动安装**已下载的新版本到 `mods/`。不依赖 `download.enabled`，见下一节 |
| `download.retries` | `3` | 下载失败后**额外**重试几次。总尝试 = `1 + 该值`（默认最多 4 次） |

两项都关着时，插件仍然能做事，只是要你点名：

| 想做的事 | 命令 | 依赖开关吗 |
|---|---|---|
| 抓某一个 Mod 的新版本 | `!!modupdate download <编号>` | 不依赖 |
| 抓全部待下载的 | `!!modupdate download all` | 不依赖 |
| 安排下一次关服时装某一个 | `!!modupdate install <编号>` | 不依赖 |
| 安排下一次关服时装全部已下载的 | `!!modupdate install all` | 不依赖 |
| 发现更新就全都抓 | `download.enabled: true` | 是 |
| 抓到的全都装 | `download.install_on_stop: true` | 是 |

</details>

### `cleanup`：清理旧版备份（默认关闭）

<details><summary>展开选项与说明</summary>

安装更新时旧 jar 不会被删除，只会改名成 `<原名>.old`，作为回退路径。这一节管理删除权限、过期
提醒与过期天数。

| 选项 | 默认 | 说明 |
|---|---|---|
| `cleanup.allow_delete` | `false` | **能不能删**：关着时 `delete` / `cleanup` / 它们的确认一律拒绝，插件不删除任何文件 |
| `cleanup.enabled` | `false` | **要不要提醒**：检查发现有过期备份时，在控制台和游戏内提醒管理员 |
| `cleanup.max_age_days` | `30` | 备份存在多少天之后算「过期」。`0` = 任何备份立刻算过期 |

**两个开关有先后顺序：先 `cleanup.allow_delete`，再 `cleanup.enabled`。** 删除默认一律拒绝；
提醒建立在删除已允许的前提上。`allow_delete` 关闭时，即使 `enabled` 打开也不会有任何提醒，因为
提醒的下一步（`!!modupdate confirm`）在那种状态下必然被拒绝。

两个都关着时，备份仍然可见：它们照常出现在 `!!modupdate list`（状态 `old_backup`），可以用
`!!modupdate list old_backup` 单独筛出来，`!!modupdate status` 也会报个数。要让删除可用，把
`cleanup.allow_delete` 改成 `true` 再 `!!modupdate reload`；在没开的情况下敲 `!!modupdate delete`
或 `!!modupdate cleanup`，它会告诉你需要改哪个选项、文件在哪里。

打开 `allow_delete` 之后：

- `!!modupdate delete <文件名 / 编号>` 删一个、`!!modupdate delete all` 删**全部**（不看天数）、`!!modupdate cleanup` 删掉所有**过期的**，都需要 `!!modupdate confirm` 确认（见[命令](#命令)）。`delete all` 与 `cleanup` 的差别只有年龄那一条：前者是「把这一堆清掉」，后者是「留一个月的回滚点」；
- 详情页里那个 `[删除此备份]` 按钮出现（关着时它换成一行说明，不是一个点下去只会报错的按钮）。

**第一次敲 `!!modupdate cleanup` 时通常什么都不会删**：备份总是新的，而默认要满 30 天才算过期。
那种情况下它会说出现在有几个、最老的那个多少天，并给出两条路：`!!modupdate delete <编号>` 点名删
一个（**点名删除不看天数**），或者把 `cleanup.max_age_days` 调小（`0` = 立刻全部算过期）再
`!!modupdate reload`。想连没到期的一起清掉就用 `!!modupdate delete all`，它同样会先列清单，
每个文件的大小和天数都在上面。

**删掉之后就立刻从列表里消失**：列表、详情、汇总渲染的是上次检查的报告，而插件在删除成功后会
把那些条目从报告里（连同 `last_report.json`）一并去掉，避免列表里残留已经不存在的文件。

再打开 `enabled`，每次检查发现有过期备份就会说一句，并预先把删除计划准备好，所以那句话说得出
「输入 `!!modupdate confirm` 删除」。若当时恰好有别的操作等待确认（比如一个还没确认的
`download`），它不会顶掉那条，改为提示你敲 `!!modupdate cleanup` 自己列一遍。

</details>

### `network`：超时与限速

<details><summary>展开选项与说明</summary>

| 选项 | 默认 | 说明 |
|---|---|---|
| `network.timeout_seconds` | `20` | 单次请求超时 |
| `network.retries` | `3` | 失败重试次数（含 429 限流与网络错误） |
| `network.concurrent_requests` | `4` | 并发数。调大更快，也更容易触发上游限流 |
| `network.requests_per_minute` | `240` | 自限速。Modrinth 官方上限 300/分钟，这里留了余量 |
| `network.cache.enabled` | `true` | 记住「这个 jar 上游不认」，下次不再重复查它。**只存这个否定结论**，不缓存任何关于「有没有新版本」的答案 |
| `network.cache.ttl_hours` | `24` | 否定结论的有效期。`0` = 永不过期；要彻底关闭请用上面那项 |

> **从旧版升级**：选项以前是平铺在顶层的（`download_updates`、`notify_in_game` …）。如果配置文件里
> 还有那些旧名字，**它们不会被读取**——MCDR 对认不出的键不报错也不提示，只会静默忽略并重写文件。
> 插件会为此打一行警告，把每个旧名字对应的新路径列出来，照着重填即可。

</details>

### 关服后自动安装（默认关闭）

<details><summary>展开安装流程与规则</summary>

两种方式都会**在服务端停止后**把已下载的新版本装进 `mods/`，并把被替换的旧 jar 改名为
`<原名>.old` 保留下来：`download.install_on_stop: true` 装**所有**已下载的；`!!modupdate install <编号>`
只装**你点名的那个**。下次启动时列出明细，第一位上线的管理员也会收到同样的内容（各只发一次）：

```
[Mod Update Checker] 已替换 3 个 Mod(2026-10-08T10:12:03+08:00)：
  Lithium 0.15.0 —— 旧 jar 已保留为 [锂-性能优化]Lithium.jar.old
  Sodium 0.6.0 —— 旧 jar 已保留为 sodium.jar.old
```

备份会一直保留。想清掉过期的，见 [`cleanup`](#cleanup清理旧版备份默认关闭)。

几条硬性规则：

- **只在关服之后动手**，不会在运行中替换 jar；**只处理插件自己下载过的文件**，你手动放进 `mods/` 的东西一概不碰。
- **旧 jar 只改名、不删除**；已经有同名 `.old` 时会用 `.old.2`，**绝不覆盖**任何已有备份。
- **安装前校验哈希**，对不上就不装；**目标文件名被占用时跳过并说明**，不会为了腾位置去替换别的文件。
- **`mods/` 里那个 jar 已经不在了就跳过**：那多半是你主动删掉的，插件不会把它加回来。
- **安装失败会回滚**：旧 jar 先挪走，新 jar 放不进去就把它挪回来。

`!!modupdate status` 会显示这一项的当前状态；如果是「关，但有 N 个已授权」，说明 `!!muc install`
已经排好了队但还没到关服那一刻。

**文件名里的中括号备注会被保留。** 管理员常给 jar 加备注（`[锂-性能优化]Lithium.jar`），替换时会
把**文件名开头**的中括号备注跟到新文件名上，后面接上游发布的真实文件名（版本号就在那里）：
`[锂-性能优化]Lithium.jar` → `[锂-性能优化]lithium-fabric-0.15.0.jar`。`[...]` 与 `【...】` 都认，
可以连着写（`[A][B]x.jar`）。

</details>

## 可信度与已知限制

- **哈希匹配是精确的**，可以放心采信。**名称匹配是猜测**：只接受完全一致的 slug，但作者改了项目名或换了项目的情况仍可能误判，报告会标 `matched_by: name` 并附一句提醒。
- **重新打包过的 jar 查不到**，只能靠名称兜底；兜底不上就是 `unresolved`，报告会明说而不是猜一个给你。**这一条可以自己解决**：写一份 `project-map.json`，见[自己编译的 Mod 查不到怎么办](#自己编译的-mod-查不到怎么办)。
- **「缺少依赖」会误报**（jar-in-jar 或 `.disabled` / `.old` 里的依赖看不见）。这是**宁可多报**的方向：漏报会让你找不到崩溃原因，多报只是多看一眼。
- **不检查嵌套 jar**：Fabric 的 jar-in-jar 里打包的库不会单独检查（数量会在报告里说明）。
- **「不支持服务端」的标记来自 Modrinth，可能滞后**：只在 Modrinth 明确写了 `unsupported` 时才出现，宁可不报也不猜。
- **`server.mc_version` 的推断可能不准**：报告里每一条都会写明版本来源（`config` / `server_info` / `log` / `mods`），来源是 `mods`（推断）时还会额外提醒。用错误的游戏版本去过滤会把每个 Mod 都报成无适配构建，而结果看起来和真答案一样，所以升级大版本后建议显式写死。
- **不会自己决定装什么**：写 `mods/` 的能力要先开启 `download.install_on_stop` 或逐个 `!!muc install` 点名，两者都只在**服务端停止后**执行，旧 jar 一律改名保留。
- **只查 Modrinth**：其他站点要么需要 API key、要么不提供直链下载。不在 Modrinth 上的 Mod 会明确报 `unresolved`。

## 常见问题

<details><summary><b>Q：为什么大部分 Mod 都能查到，有几个查不到？</b></summary>

A：那些多半不在 Modrinth 上，或者是你自己编译 / 重新打包过的。想让它别再被检查，用
`check.ignored_mods`；想知道它到底是什么，用 `project-map.json`。

</details>

<details><summary><b>Q：每次开服都提醒同一批更新，有没有办法只看新的？</b></summary>

A：有。报告会在「有更新」那一节末尾多写一行「其中 N 项是上次检查之后新出现的」。这份对比来自上一
份报告文件，所以跨重启有效；用 `report.write_file: false` 关掉落盘就等于关掉这个对比。

</details>

<details><summary><b>Q：装完却什么都没发生？</b></summary>

A：默认要等服务端启动完成 + 60 秒（`check.start_delay_seconds`）。想立刻看到结果就
`!!modupdate check`。如果 Mods 目录识别错了，`!!modupdate status` 会显示它实际用的路径。

</details>

<details><summary><b>Q：我升级了插件，但配置文件里没有新选项？</b></summary>

A：先看控制台有没有这一行：`配置文件缺少 N 个选项，已按默认值补上：…`。有的话文件已经补好了，
这行还会告诉你补了哪几个、文件在哪。**没有这行、文件也没变化**，说明插件没有真正重新加载：
替换 `.mcdr` 文件后必须重启服务端（或 `!!MCDR reload plugin`），MCDR 启动时打印的
`插件 mod_update_checker@版本 已加载` 那行就是版本证据。

</details>

<details><summary><b>Q：升级 Minecraft 大版本后满屏 `no_compatible_build`？</b></summary>

A：先确认 `!!modupdate status` 里的版本号是不是对的。如果来源显示 `mods`（从 Mod 元数据猜的），
请显式设置 `server.mc_version`。

</details>

<details><summary><b>Q：`mods/` 里那些 `.old` 越攒越多，能自动清吗？</b></summary>

A：能，但**默认不删**：那是你的回退路径。它们会出现在 `!!modupdate list`（状态 `old_backup`）和
`!!modupdate status` 里。要删的话先打开 `cleanup.allow_delete`（删除的总开关，关着时任何删除命令
都会被拒绝），然后可以按文件名或编号一个个删（`!!modupdate delete`）、一次清掉所有过期的
（`!!modupdate cleanup`），或者把全部备份一次清掉（`!!modupdate delete all`）。想让插件在备份过期
时主动提醒你，再打开 `cleanup.enabled` 并设好 `cleanup.max_age_days`；它排在 `allow_delete` 之后。

</details>

<details><summary><b>Q：会不会很吃请求额度？</b></summary>

A：不会。哈希识别是批量的：一个 100 Mod 的服务端通常是 3~5 次请求；只有「批量答不出来」和「需要按
名称兜底」的少数 Mod 才会各自再问一次。另有 240 次/分钟的自限速，以及 429/5xx 的自动重试与退避。
被 `check.ignored_mods` 排除的 Mod 完全不产生请求。

</details>

<details><summary><b>Q：会不会拖慢服务端？</b></summary>

A：不会。检查在**自己的后台线程**里跑，最重的部分只是**把 `mods/` 顺序读一遍**：实测 20 个 jar
（50 MiB）约 90 ms，100 个 jar（100 MiB）约 0.2 秒；比对与生成报告约 17 ms；网络查询是**批量**的
（100 个 Mod 只要 3~5 次请求）。开服后的自动检查会先等 60 秒（`check.start_delay_seconds`）避开
Mod 加载高峰；空闲时它**零 CPU**，默认配置下查完一次就结束了。它还碰不到游戏本身：插件跑在 MCDR
进程里，游戏是另一个进程，唯一的动静是几条聊天消息。完整数字与测量方法见
[`README-dev.md`](README-dev.md)。

</details>

<details><summary><b>Q：能自动更新 Mod 吗？</b></summary>

A：能，但**只有在你明确要求时**，而且永远发生在服务端停止之后：开启 `download.install_on_stop`
（装所有已下载的），或者逐个 `!!modupdate install <编号>`（只装你点名的）。两者都会先把旧 jar 改名
成 `.old` 保留，所以不满意就改回来。默认状态是**不装**。

</details>

## License

MIT
