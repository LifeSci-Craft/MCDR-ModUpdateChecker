# 测试说明

面向改这个插件的人。面向使用者的说明在 [`../README.md`](../README.md)，
设计取舍与发版流程在 [`../README-dev.md`](../README-dev.md)。

## 一次性准备

插件本身**没有**额外依赖：`requests` 是 MCDR 的硬依赖，其余全部走标准库。测试需要 `pytest`，
端到端测试还需要一个真的 MCDR。

按惯例装进仓库内的 `.testlibs/`，不污染系统环境（`.gitignore` 已忽略该目录）：

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
```

## 跑测试

```bash
PYTHONPATH=.testlibs python -m pytest          # 全量（722 项，约 4 分钟，含端到端）
PYTHONPATH=.testlibs python -m pytest -m "not e2e"   # 跳过端到端，约 1.5 分钟
```

`pytest.ini` 里已把 `tests/` 设为测试根，`conftest.py` 负责把仓库根和 `tests/` 放进 `sys.path`；
`tests/support.py` 放跨文件复用的工具（`flatten_options`、合成 jar 等）。

> **Windows**：多个路径要用**分号**分隔，即 `PYTHONPATH=".testlibs;tests"`。用冒号的话 Python 会把整串
> 当成一个目录名，报 `No module named pytest`——看起来像 conftest 的问题，其实不是。

## 各文件在测什么

| 文件 | 覆盖 |
|---|---|
| `test_versioning.py` | 版本比较（数值比较、预发布、`+build`、`1.19.2-0.5.3`）、MC 版本范围匹配（`>=1.21 <1.22`、`~`、`^`、通配、区间、OR 列表），以及「垃圾输入不抛异常」 |
| `test_digests.py` | 单次遍历算出的 SHA-1 与大小；对整块缓冲区用 `hashlib` 交叉验证，并有一条断言证明内存不随文件大小增长 |
| `test_scanner.py` | 真 jar 的元数据解析（fabric / quilt / forge / neoforge 四种格式）、宽容 JSON、无法读取的文件降级、重复 mod id、仅客户端 Mod、**必装依赖的收集与「缺失」判定**（平台 id 剔除、`provides` 认可、`recommends` 忽略、`mandatory = false` 忽略） |
| `test_serverinfo.py` | MC 版本与加载器的推断顺序：配置覆盖 → MCDR ServerInformation → 日志 → Mod 元数据投票 |
| `test_clients.py` | Modrinth 客户端的协议形状：批量哈希、**空过滤数组不发送**、chunk 分段、429/5xx 重试、401/403 与 404 处理 |
| `test_projectmap.py` | 管理员手写的映射表：两种键的优先级、大小写、每一种畸形 JSON 都要「报告原因 + 忽略 + 不抛异常」、以及「一个文件名，不是一条路径」的安全性 |
| `test_mcdr_entry.py`（清理部分） | `cleanup.allow_delete` 那道总开关：默认关、关着时两条命令都拒绝并报出选项名与配置文件路径、提醒也被挡住、**已经在等确认的计划在开关关掉之后不执行**、详情页换成说明而不是按钮、状态屏只在有东西可删时才提这个开关、**第一次敲 `cleanup` 时那句「没有超过 N 天」会给出最老的天数与两条出路**（点名删除不看天数 / 调小阈值）、**每个状态的颜色逐个钉住**（绿=已是最新 / 蓝=有得更新 / 红=有问题 / 其余灰，编号永远黄、名称永远白，加新状态不来登记就失败）、**状态在一行里只说一次**、`delete all`（删全部、不看天数，与 `cleanup` 的差别、集合变了作废、关着开关被拒）、**删除成功后条目立刻从报告与 `last_report.json` 里消失**（否则列表里留着幽灵、再删一次还能列出删空气的计划）、**版本收进 `[版本]` 的浮窗**（旧(红) >>> 新(绿)；控制台保留数字）、**`[详细信息]` 悬停说明它执行什么**、**玩家帮助页按字宽模型补齐**（`list` / `install` 各比字符数补齐多一格——模型被真的用了）、**翻页条是标题栏形状**（走到头一侧灰掉、没有点击事件）、**帮助页短句 + 浮窗**（可见文案没有括号、`list` 的浮窗列出全部状态）、**每屏底部的收尾线与标题栏同宽**（玩家带提示、控制台素线；列表那条提示的词取自行自己用的键）、**聊天行分列**（名称截断成 `...` 且全名在浮窗里；`[版本]`/`[状态]`/`[详细信息]` 各起一列）、**状态是 `[状态: 图标]`、解释在浮窗里**（图标取自原版自带符号表，浮窗第一行是状态名 + 状态色；备份/文件格的事实同样在浮窗里）、**浮窗每一行 ≤ 32 显示列**（中英两份逐行量）、**列宽按量出来的字宽表算**（字宽表本身有一条钉在截图数值上的测试，另有一条量十一行长 Short 不一的名字的三列落点散布）、**帮助页说明文字可点**（填命令进输入框）、**计划里的 confirm 是红色可填按钮**（控制台拿到同一句话） |
| `test_jsonfile.py` | 插件自己的 JSON 文件：缩进与不转义（读的人是人）、排序只给账本、**写盘是原子的**（先写同目录的 `.tmp` 再改名——目标文件要么是旧的要么是新的，绝不会是半份，`test_a_failed_write_leaves_the_old_file_untouched` 是量它的那把尺）、以及读的时候缺失 / 坏 JSON / 目录一律给 `None` |
| `test_cleanup.py` | `.old` 备份的识别与删除：只有插件自己造的名字算备份（`config.yml.old` / `notes.old` / 目录 / `None` 都不算）、年龄按「变成备份」的时间戳算、阈值 `0` = 全部过期（不是「关闭」）、**删除那一刻重新核对名字**、文件已不在时报 `already-gone`、以及用 `..` 拼出来的路径碰不到文件夹外面 |
| `test_checker.py` | 完整检查流程对本地假上游的判定结果、**请求预算**、缓存复用、映射表优先于名称搜索、两条新提醒的误报方向、报告序列化**与反序列化**、增量对比、中英渲染、**分节结构（一份数据两种渲染）与显示宽度对齐**、**翻页**（每行恰好出现在一页、越界落在最后一页、翻页行给控制台的命令、第一页就是要处理的那批）、**前缀匹配**（唯一前缀命中、多个就拒绝并列候选、精确匹配绝不放宽、与精确同一套归一化）、**仅客户端的依赖被排除**、**提醒按角色上色** |
| `test_i18n.py` | 两份语言目录键集一致、代码里用到的键都在、没有失效键、占位符对齐、**每条消息开头的 `[方括号]` 都是元数据里的插件名**、`install.reason.*` / `download.reason.*` 与产出它们的模块双向对齐 |
| `test_mcdr_entry.py` | MCDR 入口：生命周期与事件注册的约束、**一份被改坏的 `resolve-cache.json` 不能拖垮插件加载**（这条是代码审查揪出来的回归）、配置分组的不变式、**配置文件的新建 / 补齐 / 补不上都会上报且状态页会显示文件路径**、**六个界面共用一个标题栏**、**控制台保留版本数字而不是 `[版本]` 标签**、`!!muc download` / `install` / `confirm` 的确认流程（超时、换人、报告换过后作废、只授权点名的那一个）、**批量形式 `download all` / `install all`**（保留字、计划计数、集合变了整条作废、逐条授权）、**下载完成通知的两条通道与开关**、**裸命令就是帮助页**（以及 `!!muc summary` 仍然可到汇总）、**游戏内的候选可以点、控制台的补全接口能列候选**、关服安装的授权路径、**复用前要不要先看「这份结果还算不算数」**、存档报告的读回、映射表路径的组装与拒绝 |
| `test_docs.py` | 文档与代码的约定：两份 README 的受众分工（使用者的 README 里不许出现实现细节，也不许把插件说成只读的）、文档里的选项路径必须真实存在**且每个选项都必须被文档提到**（反向漏掉一项，看起来就像「功能没做」）、README 的命令表必须列全命令树里注册的每个命令、所有仓库链接指向同一个仓库、**版本号三处一致**（plugin.json / CHANGELOG 首个标题 / README 样本）、**README 里的样例标题栏逐字等于插件画出来的那条**、CHANGELOG 只留一版、矩阵检查项数量由 `required_keys()` 现算、**每条 `#锚点` 内部链接都落在一个真实存在的标题上**（按 GitHub 的锚点规则算，CJK 标点要丢掉）、**源码行末统一 LF** |
| `test_e2e.py` | 用 `pack.py` 打出 `.mcdr`，放进**真实 MCDR**里跑：加载、自动检查、命令树、别名、报告落盘 |

`tests/fake_upstream.py` 是 Modrinth 的本地假实现。它的回答形状是照着线上实测抄的
（例如：`version_files` 只返回认识的哈希；`version_files/update` 无匹配时返回 `{}`），所以它测的是
真实的协议路径，而不是一个想当然的替身。下载用的假 CDN 还会额外提供三种「坏情况」——校验必定失败的
字节、前 N 次损坏随后正常的抖动、以及不带 `Content-Length` 的流式响应——用来证明拒绝与重试两条路径
都真的会被走到。

## 跨 MCDR 版本

`mcdreforged.plugin.json` 声明了最低版本，这个声明由 `tools/mcdr_matrix.py` 实地验证——它会在
指定解释器里各起一个真实 MCDR：

```bash
python tools/mcdr_matrix.py --current
python tools/mcdr_matrix.py /path/to/mcdr-2.13/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16/python
```

每个版本会检查：插件加载（或被干净拒绝）、`language: auto` 确实跟随 MCDR、自动检查发现种下的更新、
裸 `!!modupdate` / `help` / `summary` / `status` / `list` / `list <状态> [页码]` / `info` / `reload` 与 `!!muc` 别名都有回应（裸命令与带页码的列表各有一条专属断言）、
`!!muc download <编号>` 会暂存计划且 `confirm` 之后真的去抓（抓的正是那条哈希对不上的，所以断言的是
「真开了 socket、真被拒绝、没留残渣」）、`!!muc install` 的两条分支、批量形式 `!!muc download all` /
`install all` 的**计划计数**（钉在场景上：1 个待抓、3 个待装——批量少算一个模块照样会印出一份像样的
计划，所以断言的是数字而不是措辞）、提醒 payload 里标题段确实是白、待办行确实是黄、`last_report.json` 内容与控制台一致、没有任何 traceback。
加上 `--with-install` 会再跑一遍关服安装。

单版本跑同一个流程可以走 pytest：

```bash
MCDR_TEST_PYTHON=/path/to/mcdr-2.14/python PYTHONPATH=.testlibs python -m pytest tests/test_e2e.py
```

## 无法在仓库内闭环的部分

插件只依赖 Modrinth，而 Modrinth **不需要任何凭据**，所以这一版没有「需要外部 key 才能验证」的环节了。
（此前存在的 CurseForge 指纹匹配整条路径，正是因为无法在无 key 的情况下闭环验证而被移除。）

仍然依赖线上行为、只能靠探测确认的，是 Modrinth 本身的回答形状：

```bash
python tools/probe_upstream.py
```


## 产物校验

```bash
python tools/check_artifact.py            # 可复现 + 换行归一 + 内容白名单 + 注释已剥离 + 包内代码能编译
python tools/check_artifact.py some.mcdr  # 检查一个已有的产物
```

## CI

推送与 PR 会跑 `.github/workflows/ci.yml`，三个作业：

- `unit`：Python 3.10 / 3.13，`pytest -m "not e2e"`；
- `mcdr-matrix`：五个 MCDR 版本各一个作业，跑 `tools/mcdr_matrix.py --current`（这就是真实 MCDR 的端到端覆盖）；
- `artifact`：`tools/check_artifact.py`（可复现、换行归一、内容白名单、注释剥离、可编译）。

> **在 CI 布局下本地复现**：CI 用 `pip install --target .testlibs` 装 MCDR，而不是装进解释器的
> site-packages。要精确模拟，用一个**没装任何依赖**的解释器并给**绝对**路径：
> ```bash
> PYTHONPATH=/abs/path/to/.testlibs python -m pytest
> ```
> 相对路径会有一个坑：MCDR 子进程的 `cwd` 是临时目录，相对 `PYTHONPATH` 在那里解析不到。

### `.testlibs` 里的 MCDR 会不会遮蔽被测版本？（会，而且很隐蔽）

`PYTHONPATH` 优先于 site-packages，所以如果 `.testlibs` 里也有一份 `mcdreforged`，
它就会**盖住每个解释器自己装的版本** —— 结果是「跑了五个版本」实际是同一个版本跑五遍，
而输出看起来完全正常。

`tools/mcdr_matrix.py` 的 `_child_env(python)` 为此先探测「这个解释器能否独立 import
mcdreforged」：能则**不**加 `.testlibs`（尊重它自己的版本），不能才加（CI 布局）。
并且矩阵结尾会**硬失败**：两个不同解释器报出同一版本时直接报错，而不是打个警告。

