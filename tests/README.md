# 测试说明

## 一次性准备

插件本身**没有**额外依赖：`requests` 是 MCDR 的硬依赖，其余全部走标准库。测试需要 `pytest`，
端到端测试还需要一个真的 MCDR。

按惯例装进仓库内的 `.testlibs/`，不污染系统环境（`.gitignore` 已忽略该目录）：

```bash
python -m pip install --target .testlibs -r tests/requirements-test.txt
```

## 跑测试

```bash
PYTHONPATH=.testlibs python -m pytest          # 全量（约 2 分钟，含端到端）
PYTHONPATH=.testlibs python -m pytest -m "not e2e"   # 跳过端到端，约 50 秒
```

`pytest.ini` 里已把 `tests/` 设为测试根，`conftest.py` 负责把仓库根和 `tests/` 放进 `sys.path`。

## 各文件在测什么

| 文件 | 覆盖 |
|---|---|
| `test_versioning.py` | 版本比较（数值比较、预发布、`+build`、`1.19.2-0.5.3`）、MC 版本范围匹配（`>=1.21 <1.22`、`~`、`^`、通配、区间、OR 列表），以及「垃圾输入不抛异常」 |
| `test_fingerprint.py` | CurseForge 指纹（固定向量 + **与独立 JavaScript 实现的跨语言比对**）、单次遍历的三个摘要 |
| `test_scanner.py` | 真 jar 的元数据解析（fabric / quilt / forge / neoforge 四种格式）、宽容 JSON、无法读取的文件降级、重复 mod id、仅客户端 Mod |
| `test_serverinfo.py` | MC 版本与加载器的推断顺序：配置覆盖 → MCDR ServerInformation → 日志 → Mod 元数据投票 |
| `test_clients.py` | 两个上游客户端的协议形状：批量哈希、**空过滤数组不发送**、chunk 分段、429/5xx 重试、401/403 与 404 处理、CurseForge 无 key 时不发任何请求 |
| `test_checker.py` | 完整检查流程对本地假上游的判定结果、**请求预算**、缓存复用、报告序列化与中英渲染 |
| `test_i18n.py` | 两份语言目录键集一致、代码里用到的键都在、没有失效键、占位符对齐 |
| `test_e2e.py` | 用 `pack.py` 打出 `.mcdr`，放进**真实 MCDR**里跑：加载、自动检查、命令树、别名、报告落盘 |

`tests/fake_upstream.py` 是 Modrinth + CurseForge 的本地假实现。它的回答形状是照着线上实测抄的
（例如：`version_files` 只返回认识的哈希；`version_files/update` 无匹配时返回 `{}`；
CurseForge 匿名访问 `/v1/fingerprints` 返回 401、其他端点返回 403），所以它测的是真实的协议路径，
而不是一个想当然的替身。

## 跨 MCDR 版本

`mcdreforged.plugin.json` 声明了最低版本，这个声明由 `tools/mcdr_matrix.py` 实地验证——它会在
指定解释器里各起一个真实 MCDR：

```bash
python tools/mcdr_matrix.py --current
python tools/mcdr_matrix.py /path/to/mcdr-2.13/python /path/to/mcdr-2.15.7/python /path/to/mcdr-2.16/python
```

每个版本会检查：插件加载（或被干净拒绝）、`language: auto` 确实跟随 MCDR、自动检查发现种下的更新、
`!!modupdate` / `help` / `status` / `list` / `list <状态>` / `reload` 与 `!!muc` 别名都有回应、
`last_report.json` 内容与控制台一致、没有任何 traceback。

单版本跑同一个流程可以走 pytest：

```bash
MCDR_TEST_PYTHON=/path/to/mcdr-2.14/python PYTHONPATH=.testlibs python -m pytest tests/test_e2e.py
```

## 无法在仓库内闭环的部分

CurseForge 的**真实**指纹匹配需要 API key，仓库里没有也不该有。因此：

- 指纹算法本身用跨语言的独立实现交叉验证（`tools/murmur2_cf.js`，转写自 CurseForge 生态内的 C# 客户端）；
- 拿到 key 之后，用 `tools/cf_verify.py` 对真实的 jar 做一次端到端确认：

```bash
python tools/cf_verify.py --api-key '$2a$10$...' --mods-dir /path/to/server/mods
```

## 产物校验

```bash
python tools/check_artifact.py            # 连打两次比字节 + 检查内容 + 确认包内代码能编译
python tools/check_artifact.py some.mcdr  # 检查一个已有的产物
```

## CI

推送与 PR 会跑 `.github/workflows/ci.yml`，三个作业：

- `unit`：Python 3.10 / 3.13，`pytest -m "not e2e"`；
- `mcdr-matrix`：五个 MCDR 版本各一个作业，跑 `tools/mcdr_matrix.py --current`（这就是真实 MCDR 的端到端覆盖）；
- `artifact`：`tools/check_artifact.py`。

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

