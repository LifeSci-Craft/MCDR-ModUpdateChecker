# bench — 量数字用的脚本

`README-dev.md` 里的每个性能与宽度数字都来自这里的脚本。它们是**一次性测量工具**，不是回归门禁：
输出记在同目录的 `.md` 报告与 CHANGELOG 的「验证」节里，改动相关代码后需要重新跑一遍并更新数字。

| 脚本 | 量什么 | 依赖 |
|---|---|---|
| `help_width_model.py` | 帮助页命令栏的对齐：候选字宽模型在两种已知字体下的最大偏差，用来挑出被采用的那个模型 | 无（任何 Python） |
| `measure_list_columns.py` | 玩家截图里列表每一格的起始像素列（`[版本]` / 状态 / `[详细信息]`），把「歪了多少」变成数字 | Pillow |
| `read_screenshot_runs.py` | 截图里每个字形占据的列区间（游程），用来反推字体的 advance | Pillow |
| `read_band_runs.py` | 指定行带的**全部**字形游程（`read_screenshot_runs.py` 只打前 14 个） | Pillow |
| `bench_render.py` | 渲染路径的开销：200 个 Mod 的列表 + 汇总 + 详情，`timeit` 计时 + `cProfile` 找热点 | 仓库 `.testlibs` |
| `scan_bench.py` | 扫描阶段的四个数字：现在的 `scan_mods`、加回第二个摘要的旧写法、线程池写法、纯 `iterdir` 基线 | 仓库 `.testlibs` |
| `perf_v1.6.0.py` | 完整负担画像：导入、命令树构建、扫描、一次完整检查、空闲 CPU | 仓库 `.testlibs` 与 `tests/` |
| `audit_language.py` | 语言文件审计：只在注释里出现的键、没人传的占位符、值重复的孪生条目 | 无 |

运行方式（在仓库根目录）：

```
python bench/help_width_model.py
python bench/audit_language.py
PYTHONPATH=".testlibs;tests" python bench/perf_v1.6.0.py
PYTHONPATH=".testlibs" python bench/scan_bench.py
python bench/bench_render.py
```

`perf-v1.6.0.md` 是 `perf_v1.6.0.py` 的输出记录（v1.6.0 时的数字）。
