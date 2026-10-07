# CHANGELOG

## v1.0.0

首个版本。

**新增**

- 扫描服务端 `mods/` 目录，解析 Fabric / Quilt / NeoForge / Forge 四种元数据格式，识别每个 jar 的
  mod id、显示名、版本号、声明的 Minecraft 版本范围与运行环境。
- 单次遍历计算 SHA-1、SHA-512 与 CurseForge MurmurHash2 指纹三个摘要。
- **Modrinth** 支持：按文件哈希精确识别，批量查询「适配当前加载器 + 游戏版本的最新构建」，
  不需要 API key。
- **CurseForge** 支持：按指纹精确识别，支持 beta/alpha 开关；未配置 API key 时自动跳过并给出申请指引。
- 名称兜底：哈希查不到时按项目 slug 精确匹配（仅接受完全一致），并在报告中标注为「近似匹配」。
- 明确的状态判定：可更新 / 已是最新 / 本地版本更新 / 无适配构建 / 无法定位上游 / 出错 / 不是 Mod / 已忽略。
- 附加提醒：同一个 mod id 装了两份、仅客户端 Mod 装在服务端、Mod 声明的 MC 范围与当前服务端不符。
- 命令 `!!modupdate`（别名 `!!muc`）：汇总、`check`、`list [状态]`、`status`、`reload`。
- 开服后延迟自动检查 + 可选的定时检查；结果写控制台、可选择性写入游戏内 tellraw。
- 报告落盘为 `last_report.json`（供脚本）与 `last_report.txt`（给人看）。
- 识别结果本地缓存（默认 24 小时），只缓存「哈希属于哪个项目」，不缓存「是否有更新」。
- 中英双语；`language: auto` 跟随 MCDR 设置。

**仓库工程**

- `tools/mcdr_matrix.py`：在指定解释器里起**真实 MCDR** + 假 MC 服务端 + 假上游，验证插件加载、
  自动检查、全部命令（含 `!!muc` 别名）与报告落盘；`tests/test_e2e.py` 是它的 pytest 封装。
- `tools/check_artifact.py`：连打两次产物比字节（可复现性）、检查包内不含 tests/tools/README、
  确认包内 `.py` 经注释剥离后仍能编译。
- `tools/probe_upstream.py`：探测 Modrinth / CurseForge 的真实行为（可重跑，用于上游变更时复核）。
- `tools/cf_verify.py`：用你的 API key 对真实 jar 核验 CurseForge 指纹匹配。
- `tools/murmur2_cf.js`：独立语言的指纹实现，作为跨语言交叉验证的对照。
- CI（`.github/workflows/ci.yml`）：`unit`（Python 3.10 / 3.13，3.10 顺带覆盖无 `tomllib` 的
  正则回退路径）、`mcdr-matrix`（五个 MCDR 版本各一个作业）、`artifact`（可复现性与内容白名单）。

**实现注记**

- CurseForge 的指纹算法（MurmurHash2、seed=1、先剔除 `0x09 0x0A 0x0D 0x20`）用独立的 JavaScript
  实现做了交叉验证；该算法无法在无 API key 的情况下做真实端到端验证，`tools/cf_verify.py`
  可在拿到 key 后补齐这一步。
- Modrinth 的 `version_files/update` 在筛选条件为空时会退化成普通查询，因此空数组一律不发送。

**开发期间由独立代码审查发现并修复的问题**

下面每一条都先核实过（MCDR 源码 / 现场实测），并补了对应的回归测试。

| 问题 | 后果 | 修正 |
|---|---|---|
| 在 `on_load` 里显式 `register_event_listener`，而模块级又定义了同名 `on_*` 函数 | MCDR 会把**两者都注册**（LOADING 期间的注册只是暂存，随后与按名发现的合并），于是每个事件触发两次、每次开服起两个检查线程，且毫无报错 | 只保留按名发现这一种机制；新增 AST 级测试禁止再次出现显式注册 |
| MCDR 的 reload 路径不派发 `on_unload`（已核对 `plugin_manager.__reload_plugin`） | 每次 `!!MCDR reload plugin` 都会遗留一个调度线程，对着同一个服务端持续检查，越积越多 | `on_load` 收到 `prev_module` 时主动停掉上一个模块的调度器 |
| `!!modupdate reload` 先 `_stop_scheduler()` 再启动新线程，但 `_stop_event` 没清 | 新循环第一次 `wait` 立刻返回、线程退出 —— 定时检查被**静默关闭**，直到下次重启 MCDR | 在 `_start_interval_scheduler` 里清事件；补了回归测试 |
| 批量查询失败时，把「没有结果」当成「没有适配构建」 | 一次 503 会让插件对**每个** Mod 断言「该项目没有发布此加载器的构建」—— 一条由瞬时故障产生的、看起来很确定的错误陈述，还会引导管理员去停用 Mod | 失败与「确实为空」分开表达；无法判定时给 `error` 状态并说明原因，绝不伪造结论 |
| 同上，Per-project 回退查询与 CurseForge 文件查询也有同样问题 | 同上 | 查询失败集合与空结果分开；新增 `error` 状态路径 |
| 上游查询失败时仍把「两个平台都不认识」写入缓存 | 瞬时的网络故障被提升成 24 小时的盲区 | 只有「确实问过、确实没有」才写缓存；未问成时加说明且不缓存 |
| `covers(">= 1.21.4", "1.21.5")` 返回 False | 运算符与版本号之间的空格让范围退化成等值判断，导致 Mod 被误报「声明的 MC 版本不符」 | 分词前先归一化运算符旁的空白；补了带空格的用例 |
| `int()` 在 Python 3.11+ 拒绝超过 4300 位的数字串 | 一个畸形版本号就能让整次检查抛异常，违背「版本比较永不抛异常」的约定 | 数字段改用字符串按长度+字典序比较，任意长度都成立 |
| `logger.info(RText)` 的颜色被静默丢弃（已实测 `str(RText(...))` 不含 ANSI） | 代码看起来在给控制台上色，实际什么都没有 | 控制台走纯文本、回复走 RText（`source.reply` 会调 `to_colored_text()`，颜色确实生效），并写明这个不对称 |
| `GET /projects?ids=` 未容忍 404 | 一个失效的项目 id 会让整批 Mod 一起丢掉标题与链接 | 改为容忍 404，坏 id 只影响它自己 |
| CurseForge 上传时间用字符串直接比大小 | 只有格式与时区完全一致时才正确 | 优先解析成时间戳再比较，解析失败才回退字符串比较 |
| CurseForge 文件列表只取单页 | 理论上前 100 条之外的最新文件可能看不到 | 说明为什么单页足够（游戏版本与加载器在分页前过滤），页大小提到 100 |
| 文档/死代码 | 一处注释声称 `Literal` 集合写法「2.15 起才支持」（实测 2.13 已支持，只有类型注解写的是 `str`）；`modrinth.is_permanent` 无人调用 | 更正注释、删除死代码 |

