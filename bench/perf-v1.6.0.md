# v1.6.0 的负担画像（2026-10-10）

**结论：每一项都是毫秒量级或零；检查跑在后台线程里；游戏进程完全不被触碰。**

测量环境：Windows、Python 3.13.14、本仓库 v1.6.0 工作树（= 线上 tag `v1.6.0`）。
脚本：`bench/perf_v1.6.0.py`（第 1–5 节）与 `bench/bench_render.py`（渲染）；
原始输出：`bench/perf-v1.6.0.txt`、`bench/bench-render-v1.6.0.txt`。

| 阶段 | 数字 | 口径 |
|---|---|---|
| 导入（宿主已加载 mcdreforged 与 requests） | **~68 ms** | 新解释器先 `import mcdreforged, requests` 再计时；5 次中位 |
| 导入（冷进程，连依赖一起） | ~930 ms | 上限参考——真实 MCDR 进程里这些早就加载好了 |
| 命令树构建 / 语言装载（on_load 的主要工作） | **各 ~0.1 ms** | timeit 50 次均值 |
| 扫描 20 个 jar / 50 MiB（用户的真实服务端） | **~90 ms** | 顺序读 + 单遍 SHA-1；两遍 93 / 87 ms |
| 扫描 100 个 jar / 100 MiB（合成） | **~0.2 s** | 同上 |
| 一次检查的比对与报告（100 Mod；本地假上游、真 HTTP 环回） | **~17 ms**（进程 CPU 15.6 ms） | 扫描已就绪；含比对、报告生成与环回往返；报告自记 0.02 s |
| 网络请求数 | **3 次**——`version_files` / `version_files/update` / `projects` | 全程批量；100 个 Mod 没有一 Mod 一请求 |
| 峰值分配（扫描 + 检查） | **~2 MiB** | tracemalloc 口径 |
| 空闲线程跑 3 秒的 CPU 增量 | **0.0 ms** | 调度线程平时就活在 `Event.wait` 里 |
| 渲染：列表 20 / 200 行 | **0.69 / 0.72 ms** | `bench/bench_render.py`，20 次均值 |
| 渲染：汇总 20 / 200、整份报告、详情页 | 0.02 / 0.04 / 0.08–0.59 / 0.01 ms | 同上 |

## 为什么它不会打扰服务端

- **检查都在守护线程里**：`!!modupdate check`、开服后的自动检查、管理员上线时的补查，都起
  ``threading.Thread(..., daemon=True)``（`mod_update_checker/__init__.py`）；MCDR 的事件线程上
  只做一次权限判断。
- **开服后的自动检查先等 60 秒**（`check.start_delay_seconds`），避开 Mod 加载高峰；定时检查
  默认关闭（`check.interval_hours = 0`），打开后线程也只是在 `Event.wait` 里睡觉——空闲 CPU 为零。
- **游戏是另一个进程**：插件跑在 MCDR（Python）进程里，不注册任何逐 tick 的东西，也不解析
  服务器逐行输出（没有 `on_info` 之类的处理器）；对游戏侧的唯一动静是几条聊天消息。
- **网络批量且有自限速**：100 个 Mod 3 次请求；上限 240 次/分钟，429/5xx 自动退避。
- **最重的扫描**只把 `mods/` 顺序读一遍：20 jar ≈ 90 ms、100 jar ≈ 0.2 s。不并行是量过才不做
  的（`bench/scan_bench.py`），而且它跑在开服 60 秒后的守护线程上，不在任何关键路径。

## 复现

```
PYTHONPATH=".testlibs;tests" python bench/perf_v1.6.0.py
PYTHONPATH=".testlibs;tests" python bench/bench_render.py
```

（都在仓库目录下运行；`.testlibs` 提供 mcdreforged；「假上游」是一个真的本地 HTTP 服务。）
