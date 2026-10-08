# Mod Update Checker —— 开发文档

> 面向**改这个插件的人**：判断逻辑、取舍理由、核验方式、构建与发版。
>
> 面向**用这个插件的人**的说明（安装、命令、配置）在 [`README.md`](README.md)。

## 目录

- [它是怎么判断的](#它是怎么判断的)
- [这些结论是怎么核验的](#这些结论是怎么核验的)
- [开发](#开发)

## 它是怎么判断的

一次检查分三级，从最便宜最可靠开始，每一级只处理上一级答不出来的：

1. **Modrinth 按 SHA-1 批量识别。** 通常一两次请求就能识别整个 mods 目录，并拿到「适配当前加载器 +
   游戏版本的最新构建」。这一步能解决绝大多数 Fabric 服务端，而且不需要任何凭据。
2. **按名称兜底。** 有些 jar 是自己编译的、被重新签名或重新打包过的，字节与发布版本不同，哈希自然对不上。
   这时用 mod id 去匹配项目 slug，**只接受完全一致**的结果，并在报告里标注 `matched_by: name`。
3. **附加提醒。** 重复 mod id、仅客户端 Mod、声明的 MC 范围与当前服务端不符。

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

## 这些结论是怎么核验的

| 结论 | 核验方式 |
|---|---|
| 能在真实 MCDR 里加载、跑完检查、注册全部命令 | `tools/mcdr_matrix.py`：在指定解释器里起真实 MCDR + 假服务端 + 假上游，驱动全流程（`tests/test_e2e.py` 是它的 pytest 封装） |
| 能跑在 2.13 / 2.14 / 2.15 / 2.16 | 同上，跨 5 个 MCDR 版本跑矩阵；四个低版本与 2.16.0 的 **36 项检查逐项一致**（`--with-install` 那次是 34 项，差的两项是下载目录专属的断言，安装会把它搬空） |
| Modrinth 的请求形状正确 | `tests/test_clients.py`。假上游的回答形状是**照线上实测抄的**（例如 `version_files/update` 无匹配时返回 `{}`），不是一个想当然的替身 |
| 哈希识别在真实数据上成立 | 拿真实 Mod jar 对线上 API 跑完整流程，核对报告的版本号与下载链接 |
| 单次遍历的摘要计算正确 | `tests/test_digests.py`：对整块缓冲区用 `hashlib` 交叉验证，并在**读块边界**两侧取样；另有一条断言证明内存不随文件大小增长 |
| 版本比对不会判反 | `tests/test_versioning.py`，含 `1.21.10 > 1.21.4` 这类反字典序用例 |
| 下载的拒绝与重试两条路径都会被走到 | 假 CDN 提供三种坏情况：校验必定失败的字节、前两次损坏随后正常的抖动、不带 `Content-Length` 的流式响应；真实 MCDR 运行证明「抖动的文件最终落盘且哈希通过」「被篡改的文件 4 次尝试后放弃且不留残留」 |
| 手动 `!!muc download` / `install` / `confirm` 真的能走通 | 上面那个矩阵运行里**真敲了** `!!muc download 1` → `!!muc confirm`：1 号的文件字节与哈希故意对不上，所以这条断言证明的是「真的开了 socket、真的被拒绝、下载目录里没留下残渣」；`!!muc install` 的两条分支（未下载要先 download、已下载要 `confirm`）也各有一条断言 |
| 授权安装只装点名的那一个 | `test_install_on_stop_installs_only_what_was_authorised`：清单里放两条、只授权一条，断言另一条的 jar 连 `.old` 都没出现过 |
| MCDR 生命周期语义（重复注册、reload 不触发 `on_unload`） | 逐条对照安装的 MCDR 源码核实，并用 AST 级测试钉住「模块级按名注册、不得再显式注册」这条约束 |
| 上游行为与代码假设一致 | `tools/probe_upstream.py`——上线后上游若有变化，重跑它就能看出差别 |

测试套件共 **504 项**（当前数量用 `pytest --collect-only -q | tail -1` 查；这一行是快照，
所以上面那张表里的「36 项检查」才是被测试自动核对的那个数字），细节见 [`tests/README.md`](tests/README.md)。

---

## 开发

```bash
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
├── checker.py       编排：三级识别与状态判定
├── scanner.py       mods 目录扫描与元数据解析（四种格式）
├── modrinth.py      Modrinth 客户端
├── upstream.py      HTTP 层：重试、限速、限流、可配置 base url
├── versioning.py    版本比较与 MC 版本范围匹配
├── digests.py       单次遍历算出 SHA-1 / SHA-512 / 大小
├── downloads.py     下载新版本、哈希校验、清单与旧版本清理
├── installer.py     关服后把下载好的版本装进 mods/（唯一会改动 mods/ 的模块）
├── report.py        报告模型与渲染
├── serverinfo.py    MC 版本 / 加载器推断
├── i18n.py          多语言查表
└── lang/            en_us.json / zh_cn.json
```

除了 `__init__.py`，其余模块**都不 import MCDR**——这是它们能被直接单元测试的原因。
