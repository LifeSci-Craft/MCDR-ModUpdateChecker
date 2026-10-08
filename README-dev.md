# Mod Update Checker —— 开发文档

> 面向**改这个插件的人**：判断逻辑、取舍理由、核验方式、构建与发版。
>
> 面向**用这个插件的人**的说明（安装、命令、配置）在 [`README.md`](README.md)。

## 目录

- [它是怎么判断的](#它是怎么判断的)
- [这些结论是怎么核验的](#这些结论是怎么核验的)
- [开发](#开发)

## 它是怎么判断的

一次检查分四级，从最便宜最可靠开始，每一级只处理上一级答不出来的：

1. **Modrinth 按 SHA-1 批量识别。** 通常一两次请求就能识别整个 mods 目录，并拿到「适配当前加载器 +
   游戏版本的最新构建」。这一步能解决绝大多数 Fabric 服务端，而且不需要任何凭据。
2. **管理员自己写的映射表**（`sources.manual_map`，默认 `project-map.json`）。自己编译、改签名、
   fork 过的 jar 字节与发布版本不同，哈希对不上，mod id 也可能对不上任何 slug——但**管理员知道它是什么**。
   这一步排在第 3 步**之前**：它是一句陈述，而名称匹配是一次猜测，让猜测先跑就等于允许一个 slug 巧合
   静默推翻管理员的明确说法。命中后报告标 `matched_by: manual`。
3. **按名称兜底。** 用 mod id 去匹配项目 slug，**只接受完全一致**的结果，并标注 `matched_by: name`。
4. **附加提醒。** 重复 mod id、仅客户端 Mod、Modrinth 标记为不支持服务端、**缺少依赖**、声明的 MC
   范围与当前服务端不符。

### 两处刻意的「宁可多报」

「缺少依赖」和「不支持服务端」这两条都会误报，而且**都往多报的方向偏**——理由一样：漏报会让你找不到
崩溃原因，多报只是多看一眼。具体是：

- **缺少依赖**只认**必装**依赖（Fabric/Quilt 的 `depends`、Forge/NeoForge 里 `mandatory` 不为 false 的），
  平台本身（`minecraft` / `fabricloader` / `forge` / `neoforge` / `java` …，见 `scanner.PLATFORM_MOD_IDS`）
  已经剔除。会误报的两种情况都**看不见**：依赖被打包在别的 jar 内部（jar-in-jar 只数不打开），或者被
  放进了 `.disabled` / `.old`。`provides` 会被认作满足，因为那是加载器实际会用的替换声明。
- **不支持服务端**用的是 Modrinth 的 `server_side == "unsupported"`，不是 jar 自己的 `environment`。
  两者互补：jar 只声明 `environment: *` 而平台标了 `unsupported` 的 Mod，只有这一条能抓到。
  `optional` 和 `unknown` **都不报**——把「平台没分类」当成「这玩意不能跑」是纯粹的噪音。

### 两个状态文件，以及它们各自被读回来的原因

数据文件夹里有两份 JSON 会影响下一次检查的判断，两者的立场完全相反：

| 文件 | 什么时候读 | 为什么值得读回来 |
|---|---|---|
| `resolve-cache.json` | 每次检查 | **只存否定答案**：「这个哈希识别不了」，且只由第 3 步（名称搜索）写入——那一步为了证明一个 jar 不在平台上要花一次搜索加一次版本查询，是最贵的答案，而明天答案不会变 |
| `last_report.json` | 插件加载时 | 让 `report.reuse_report_minutes` 那个复用窗口**跨重启有效**。否则每次重启后第一个管理员上线都必然触发一次全量检查 |

`resolve-cache.json` **不存正向识别**，也不存任何关于更新的结论。前者是没必要的：正向识别来自
对整个目录的**一次**批量请求，记住它省不下什么，而一份过期的正向结论是在断言一批可能已经被换掉的字节。
后者是设计底线：有没有新版本正是检查的意义，必须每次现问。

`last_report.json` 的读回比看上去更需要小心，因为它**跨版本存活**：

- 写出的 JSON 带一个 `format` 标记（`report.py` 里的 `REPORT_FORMAT`），读的时候**不匹配就整个拒绝**。
  拒绝的代价只是多跑一次检查，所以严格是划算的——升级后第一次上线多查一次，好过按老形状乐观解读；
- 每一行都靠 `report.py` 里那几个 `_text` / `_integer` / `_notes` 强制转型，**一条坏记录只丢它自己**；
- 认不出的 `status` 一律降级成 `unresolved`。没有别的办法如实陈述一个这个版本不知道的状态，而让它回退成
  `status.something_new` 原样打进日志更糟；
- 只在 `report.write_file` 打开时才读——那个选项的字面意思就是「把结果留在盘上」。

### 「这份结果还算数吗」不只是「它有多旧」

`report.reuse_report_minutes` 原先只看时间。这在报告只存在内存里时够用，现在报告跨重启存活，
「多久以前生成的」和「还描不描述这台服务端」就成了两个问题。所以复用前还要过一遍
`_report_still_applies()`：

- `server.mods_directory` 必须和当前解析出来的目录一致（管理员可能换了目录）；
- `server.loader` 必须一致；
- `server.mc_version` **写死时**必须一致。

**已知漏洞**：`mc_version: auto` 时没有便宜的对照物（推断要读日志、读不到就要扫每个 jar），所以自动推断
出来的版本变化仍可能被漏掉。README 本来就建议升级大版本后写死版本号，这里是那条建议多出来的一个理由。

### 「自上次检查以来新出现了什么」

`update_available` 是一个**状态**而不是**事件**：一个等了一周的更新和一个一小时前刚发布的更新，报告里
长得一模一样。`Report.record_new_since(previous)` 用上一份报告做差集，`new_since_last` 只收
**新变成** `update_available` 的文件名。三个边界是刻意的：

- 只比 `update_available`。刚被下载下来的那些变成了 `awaiting_install`，那是管理员自己干的，不是新闻；
- 用**文件名**做身份，因为这是两份报告不需要重新解析任何东西就能对上的东西；
- 没有上一份报告时是空集，渲染时**什么都不写**——「没有新东西」和「没得比」会写出同一句话，
  而只有一句值得读。

### 版本比对的取舍

MC 的 Mod 版本号是一团乱麻：`1.2.3`、`v1.2.3`、`0.162.0+26.3`、`1.19.2-0.5.3`、`mc1.21.1-1.2.3`、
`2.1.0-beta.4+build.31` 都存在。比对器的规则是：

- 数字段按**数值**比较，所以 `1.21.10 > 1.21.4`（按字典序会判反）；
- `+` 之后的构建元数据只作为**平局时**的判据，因为对 Mod 而言那段通常就是目标 MC 版本，是真实信息；
- 以 `alpha` / `beta` / `rc` / `pre` / `snapshot` 开头的多余段让版本**更旧**，所以 `1.0.0 > 1.0.0-rc.1`；
- `1.19.2-0.5.3` 里的 `-` 被当作**普通分隔符**而不是预发布标记——这是作者的写法（游戏版本 - Mod 版本），
  所以它比裸的 `1.19.2` 新；
- 完全不认识的输入**不会抛异常**，最差是返回一个字典序结论（一个畸形版本号不该让整次检查中断）。

---

## 界面：一份数据，两种渲染

同一个检查结果有两个读者，需求正好相反，所以有两套渲染——但**分节、顺序、标题、截断的算术只有
一份**（`summarise()` 返回 `(context, blocks, closing)`）：

| | 谁看 | 行怎么排 | URL |
|---|---|---|---|
| `render_summary` / `render_full` | 控制台、`last_report.txt` | 对齐成一列（按**显示宽度**，见下） | 保留 |
| `_reply_summary` / `_reply_index` / `_reply_detail` | 游戏内聊天 | `[编号] 描述` + `[详细信息]` 按钮 | 不出现 |

日志行点不了，URL 是那里唯一能到达项目页的方式，而且还能复制；游戏里 URL 点不了又折行，所以换成
按钮。两边读同一个结构，因此**不会对「有几节、哪一节在前、截掉了几行」产生分歧**。

**颜色由「这一行代表什么」决定，不由它包含什么决定。** 规则就五条：金色=标题栏；aqua=可操作的
东西（按钮、命令、字段名）；白色=内容；黄色=需要有人处理的行；灰色=其余。上一版是按文本猜的
（`"->" in line` 就是黄色），于是同一个 Mod 在 `list` 里和在汇总里是两种颜色，一个名叫 `a->b`
的 Mod 还会把自己涂成黄色。

**列宽按显示宽度算，不按字符数。** `len()` 数字符，而游戏字库把汉字画成两个 `A` 宽——每一行都以
中文状态注释结尾，所以按字符数补出来的空格把链接列推得参差不齐，在中文界面上**每一行**都错。
`display_width()` 用 `unicodedata.east_asian_width` 数 W/F 为 2、其余为 1。

**页数预算是算出来的，不是估的。** `_INDEX_FIXED_LINES` 是「行以外还会打印几行」：标题栏、服务端
上下文、小节标题、统计行、提示行，以及被截断时那一行。加一个界面元素就要把它调大——它上次是 5，
这次因为每个界面都多了标题栏而变成 6，否则预算会悄悄不再是预算（`test_the_listing_fits_a_page_
whatever_the_server_holds` 是量它的那把尺）。

---

## 这些结论是怎么核验的

| 结论 | 核验方式 |
|---|---|
| 能在真实 MCDR 里加载、跑完检查、注册全部命令 | `tools/mcdr_matrix.py`：在指定解释器里起真实 MCDR + 假服务端 + 假上游，驱动全流程（`tests/test_e2e.py` 是它的 pytest 封装） |
| 能跑在 2.13 / 2.14 / 2.15 / 2.16 | 同上，跨 5 个 MCDR 版本跑矩阵；四个低版本与 2.16.0 的 **40 项检查逐项一致**（`--with-install` 那次是 38 项，差的两项是下载目录专属的断言，安装会把它搬空） |
| Modrinth 的请求形状正确 | `tests/test_clients.py`。假上游的回答形状是**照线上实测抄的**（例如 `version_files/update` 无匹配时返回 `{}`），不是一个想当然的替身 |
| 哈希识别在真实数据上成立 | 拿真实 Mod jar 对线上 API 跑完整流程，核对报告的版本号与下载链接 |
| 摘要计算正确，且只算该算的 | `tests/test_digests.py`：对整块缓冲区用 `hashlib` 交叉验证，并在**读块边界**两侧取样；另有一条断言证明内存不随文件大小增长 |
| 版本比对不会判反 | `tests/test_versioning.py`，含 `1.21.10 > 1.21.4` 这类反字典序用例 |
| 下载的拒绝与重试两条路径都会被走到 | 假 CDN 提供三种坏情况：校验必定失败的字节、前两次损坏随后正常的抖动、不带 `Content-Length` 的流式响应；真实 MCDR 运行证明「抖动的文件最终落盘且哈希通过」「被篡改的文件 4 次尝试后放弃且不留残留」 |
| 手动 `!!muc download` / `install` / `confirm` 真的能走通 | 上面那个矩阵运行里**真敲了** `!!muc download 1` → `!!muc confirm`：1 号的文件字节与哈希故意对不上，所以这条断言证明的是「真的开了 socket、真的被拒绝、下载目录里没留下残渣」；`!!muc install` 的两条分支（未下载要先 download、已下载要 `confirm`）也各有一条断言 |
| 授权安装只装点名的那一个 | `test_install_on_stop_installs_only_what_was_authorised`：清单里放两条、只授权一条，断言另一条的 jar 连 `.old` 都没出现过 |
| MCDR 生命周期语义（重复注册、reload 不触发 `on_unload`） | 逐条对照安装的 MCDR 源码核实，并用 AST 级测试钉住「模块级按名注册、不得再显式注册」这条约束 |
| 上游行为与代码假设一致 | `tools/probe_upstream.py`——上线后上游若有变化，重跑它就能看出差别 |
| 自己写的映射表确实赢了名称搜索 | `test_the_map_beats_the_name_search`：那个 jar 的 mod id **正好**等于另一个项目的 slug，所以名称搜索会兴高采烈地把它解析成**错的项目**；断言拿到的是映射表那个 |
| 映射表填错会说出来而不是猜 | `test_a_map_entry_pointing_nowhere_is_reported_rather_than_guessed`，以及 `tests/test_projectmap.py` 里每一种畸形 JSON 都走一遍 |
| 复用窗口不会被跨重启的报告骗到 | `test_a_report_for_a_different_mods_folder_is_not_reused` 等三条，加上 `test_a_report_that_still_applies_is_reused` 防止门禁「靠全部拒绝来通过」 |
| 存档报告能被读回来、坏的那份被拒 | `tests/test_checker.py` 的 round-trip 一组：`format` 不认识就整个拒绝、一条坏记录只丢它自己、认不出的 `status` 降级成 `unresolved` |
| 新依赖提醒不会对正常服务端开火 | `test_a_satisfied_dependency_is_not_reported`、`test_a_provided_id_satisfies_a_requirement`、`test_fabric_requires_ignores_recommends_and_suggests`，以及 `optional` / `unknown` 的 `server_side` 参数化用例 |
| `!!muc status` 不会读 jar 的字节 | `test_the_status_screen_does_not_hash_the_mods_folder`：**数**摘要函数的调用次数，而不是让它抛异常。`scan_mods` 会吞掉单个 jar 的异常并把它记成「读不了」，所以抛异常那个写法会让这个测试在错误的原因下通过；配套的 `test_the_status_screen_still_reads_the_metadata` 防止「不哈希」退化成「不读文件夹」 |
| 两个批量查询只花一个往返 | `test_the_two_batched_lookups_share_one_round_trip`：数同时在飞的调用数，并配一条断言证明 1c 失败只是少了标题、不会改变判定 |
| 版本号三处写法一致 | `tests/test_docs.py`：README 里那张样例状态屏的版本、CHANGELOG 的**第一个** `## ` 标题（`tools/release.py` 就是拿它当发布正文的）都必须等于 `mcdreforged.plugin.json` 的版本，且 CHANGELOG 只允许有一个版本段 |
| 配置文件是完整的，而且补齐之后插件会说出来 | 静态那一半：`Config` 的字段集与 `Config.get_default().serialize()` 的叶子集**必须相等**；每个选项都必须在 README 或本文件里出现过。运行时那一半：一份缺了选项的旧配置文件会被 MCDR 补齐并重写（`test_an_incomplete_config_file_is_healed_and_the_added_options_are_named` 等三条），**插件必须把补了什么、文件在哪报出来**——这段被矩阵在五个版本上端到端核对：`config_healed`（文件真的多了那两个选项）与 `config_reported`（控制台真的报了） |
| 列表编号扛得住状态变化 | `test_downloading_a_mod_does_not_move_it_in_the_listing`：`update_available` 与 `awaiting_install` 共用一个排序名次，所以 `download 1` 之后 `install 1` 指的还是同一个 Mod |
| 手柄查找的四种写法与三种失败 | `test_a_mod_can_be_looked_up_by_the_name_the_listing_shows` 等一组。**示例 Mod 的 id、文件名、显示名必须互不相同**，否则测试会经 mod id 命中而看起来通过 |
| 六个界面看上去属于同一个插件 | `test_every_screen_opens_with_the_same_title_bar`：五个界面各渲染一次，比对**发送给客户端的 segment**——标题栏那段金色文字必须逐字相同。另有一条 AST 断言防止某个界面把 RText 交给日志（颜色和按钮会一起丢掉） |
| 游戏里不会有裸 URL，日志里必须有 | `test_the_chat_summary_offers_a_button_where_the_log_offers_a_url`：同一次检查，聊天形式里既没有项目页也没有 CDN 地址、并且带一个 `!!modupdate info 1` 的点击事件；日志形式里项目页仍在 |
| 链接列按显示宽度对齐 | `test_the_link_column_is_measured_in_columns_not_characters`：两个**字符数相同、显示宽度不同**的名字（`AB` / `文本`），断言行内链接的起始显示列相同 |
| 编号不补空格 | `test_the_listing_never_pads_the_number`：短列表与二十个 Mod 的列表都断言 `[1] ` 在、`[ 1]` 不在——这条被用户报过两次，所以钉两遍 |
| 批量命令的集合语义 | `test_download_all_stages_one_plan_for_every_fetchable_mod`：计划点名的恰好是全部候选，没有可下载文件的那个被**数出来**而不是丢掉。`test_a_batch_confirmation_is_dropped_when_the_set_of_mods_changed` 证明集合一变整条作废——只跑对得上的那部分，会让「成功」的汇报盖住没做的那一半。`test_all_is_the_bulk_word_even_when_a_mod_answers_to_it` 钉住保留字：连 id、文件名、显示名三处都叫 `all` 的 Mod 也不能把它变成「指定那一个」 |
| 批量安装仍是一条一条授权 | `test_install_all_authorises_every_downloaded_build`：从磁盘读回 ledger，核对恰好是那三个 key 被授权。注入验证里把循环改成 `entries[:1]`（只装第一个）时确实失败 |
| 下载完成会通知，且不受 `report.in_game` 控制 | `test_a_finished_download_is_announced_to_the_console_and_to_admins`（控制台一行 + 权限够的管理员收到、权限不够的收不到）、`test_a_check_that_fetched_nothing_says_nothing`（非事件不发）、`test_a_join_check_keeps_the_completion_on_the_console_only`（`broadcast=False` 时只有控制台） |
| 源码行末统一 LF | `test_no_source_file_carries_windows_line_endings`：扫工作区（跳过二进制与缓存），任何 `\r\n` 都失败。它守的是 Git 看不见的那一侧——`.gitattributes` 只在**提交时**归一，而「读文件再写回」的脚本会在 Windows 上把整份文件翻成 CRLF，内容一个字没改，diff 却全是警告 |

测试套件共 **627 项**（其中 2 项是真实 MCDR 端到端，只在 CI 上跑；当前数量用
`pytest --collect-only -q | tail -1` 查；这一行是快照，
所以上面那张表里的「40 项检查」才是被测试自动核对的那个数字），细节见 [`tests/README.md`](tests/README.md)。

---

## 开发

```bash
# 代码风格（规则见根目录 ruff.toml；target-version 是 3.8，因为插件要支持 MCDR 2.13.0）
ruff check .

# 测试
python -m pip install --target .testlibs -r tests/requirements-test.txt
PYTHONPATH=.testlibs python -m pytest -m "not e2e"     # 快
PYTHONPATH=.testlibs python -m pytest                  # 含真实 MCDR 端到端

# 跨 MCDR 版本
python tools/mcdr_matrix.py --current
python tools/mcdr_matrix.py --current --with-install   # 额外验证关服安装

# 打包 + 校验产物（可复现、换行归一、内容白名单、注释剥离、包内代码仍能编译）
python pack.py
python tools/check_artifact.py

# 上游 API 行为变了？重跑探测
python tools/probe_upstream.py
```

> **Windows（Git Bash）**：`PYTHONPATH` 的多个路径要用**分号**分隔，即 `PYTHONPATH=".testlibs;tests"`。
> 用冒号的话 Python 会把整串当成一个目录名，症状是 `No module named pytest`——那看起来像 conftest
> 的问题，其实不是。

### 发布一个新版本

发布产物的内容有一套固定标准，`pack.py` 与 `tools/check_artifact.py` 负责把它钉住：

| 项目 | 规则 | 由谁保证 |
|---|---|---|
| `CHANGELOG.md` | **只保留最新一个版本**的条目，更早的条目属于 GitHub Releases | 发版时手工替换（本文件开头已写明） |
| `README.md` | **完全不进包**（MCDR 从不读它，发布页已经写了同样的内容） | `pack.py` 的白名单 + `check_artifact.py` 的 `FORBIDDEN_ROOTS` |
| 代码注释与 docstring | **只留在仓库里**；进包的 `.py` 全部被清空（行号保留，堆栈仍能对上源文件） | `pack.py` 的 `packaged_source()` + `check_artifact.py` 的 `check_stripped()` |
| 其它 | 只打包 `mcdreforged.plugin.json` / 插件包内的 `.py` / `lang/*.json` / `LICENSE` / `CHANGELOG.md` | 同上，白名单式打包 |

发版的顺序：**建分支 → 推分支 → 开 PR → 合并 → 再在合并后的 `main` 上打 tag 发 Release**。
这样每个版本「新增/删除了什么」在 PR 页面里是逐行可见的。

发版前还要看一眼**仓库地址**：`mcdreforged.plugin.json` 的 `links`、`checker.py` 的 `USER_AGENT`、
CHANGELOG 的 Releases 链接必须指向真实存在的仓库。四处是否**互相一致**由
`tests/test_docs.py` 自动核对，但「这个 owner 是不是对的」只有线上远端知道——
曾经这四处一起指向一个 404 的仓库，测试全绿而链接全废。

推送到 `main` 时会自动跑 CI（`.github/workflows/ci.yml`）：

| 作业 | 内容 |
|---|---|
| `unit` | Python 3.10 与 3.13 上跑单元与集成测试（3.10 无 `tomllib`，顺带覆盖 Forge 元数据的正则回退路径） |
| `mcdr-matrix` | **2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0 各一个作业**，各自起真实 MCDR 跑完整流程 |
| `artifact` | 连打两次产物比字节（可复现）、把源码临时改成 CRLF 再打一次要求字节一致（换行归一）、检查包内不含 tests/tools/README、确认包内 `.py` 已无注释与 docstring 且仍能编译 |

目录结构：

```
mod_update_checker/
├── __init__.py      MCDR 入口：配置、命令树、事件、调度、通知
├── checker.py       编排：四级识别与状态判定
├── scanner.py       mods 目录扫描与元数据解析（四种格式）、依赖收集与缺失判定
├── modrinth.py      Modrinth 客户端
├── projectmap.py    sources.manual_map：读管理员手写的「jar → 项目」清单（只读，从不写）
├── upstream.py      HTTP 层：重试、限速、限流、可配置 base url
├── versioning.py    版本比较与 MC 版本范围匹配
├── digests.py       算出 SHA-1 与大小（只算真的会被用到的那一个）
├── downloads.py     下载新版本、哈希校验、清单与旧版本清理；单一文件名安全校验
├── installer.py     关服后把下载好的版本装进 mods/（唯一会改动 mods/ 的模块）
├── report.py        报告模型、序列化与反序列化、渲染
├── serverinfo.py    MC 版本 / 加载器推断
├── i18n.py          多语言查表
└── lang/            en_us.json / zh_cn.json
```

除了 `__init__.py`，其余模块**都不 import MCDR**——这是它们能被直接单元测试的原因。

⚠️ **两条自相矛盾的注释已经在 v1.1.0 修掉，别改回去**：`digests.py` 曾声称扫描会阻塞开服
（实际跑在开服 60 秒后的守护线程上），`checker.py` 的 `ResolveCache` 曾声称缓存「哈希属于哪个项目」
（实际只存否定答案）。两处都是「文档比代码更乐观」，而它们都朝着**让人误判性能或行为**的方向。
