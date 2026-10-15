# CHANGELOG

本插件所有值得一提的变更都记录在此。插件包内也附带同一份 `CHANGELOG.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

**本文件只保留最新一个版本的条目**，更早版本的记录见
[GitHub Releases](https://github.com/LifeSci-Craft/MCDR-ModUpdateChecker/releases)。

---

## v1.6.2

### 修复

- **一个畸形 mod jar 不再让整次检查没有报告**：`depends.minecraft` 写成数字或对象（例如 `1.21` 忘了引号）时，以前会让检查整体失败、只剩一句 check failed。
- **下载请求也计入 `network.requests_per_minute`**：以前只有 JSON 请求限速，批量下载不受限。
- 响应体读到一半断连会重试；深嵌套响应抛出的 `RecursionError` 归类为上游错误，不再穿透检查流程。
- 安装成功后会删除已消费的下载文件并清扫下载目录，磁盘占用不再随更新次数单调增长。
- **并发写同一 JSON 文件不再失败**：按目标目录串行化写入并重试改名，缓存与批准状态不再静默丢失。
- 配置里写错类型会被点名并备份为 `config.json.broken.<时间戳>`，不再被 MCDR 静默重置为默认值。
- `ResolveCache` 只回写自己新增的记录，多个实例不再互相抹掉对方的解析结果。
- `!!muc list ²` 这类输入不再让命令静默失败；数值选项范围表补全（`network.retries`、`download.retries`、`network.cache.ttl_hours`、`report.reuse_report_minutes`）。
- 版本比较满足传递性，`is_prerelease` 与之一致：单字母后缀不再被当成预发布标记。
- `download all` 也遵守「同一文件不并发下载」；备份名改为原子抢占，不会再覆盖已有备份；`.part` 临时名唯一。
- 元数据解压改为分块读并设上限。
- i18n 不再依赖 Python 3.9+ 的 API，`language: auto` 在声明的 3.8 下限上不会退化成只有 `en_us`。
- 语言文件里让用户设置的键此前写的是缺少 `server.` 前缀的旧路径，照做的键会被 MCDR 丢掉；现已更正为 `server.mc_version`。
- `!!muc` 与 `!!modupdate` 两种别名下，屏内提示都指向用户实际输入的那个别名。
- `last_report.json` 改为原子写入；reload 后 `!!help` 的权限等级文案与实际一致。

### 工具与工程

- `tools/mcdr_matrix.py` 固定子进程编码并在中文终端下保住失败证据；`tools/probe_upstream.py` 接受参数、离线时给出结论而不是回溯。
- `tools/check_python_floor.py` 改为检查「模块 + 属性」对，`importlib.resources.files` 这类问题不会再漏过。
- CI 增加端到端作业、pip 缓存与固定 action 版本；补英文 README 与徽章。

### 验证

- 测试 **884 项**全部通过（含 2 项真实 MCDR 端到端）。
- 变异检查 **12 条全部被抓**。
- 真实 MCDR **2.13.0 / 2.14.1 / 2.15.0 / 2.15.7 / 2.16.0** 五版本矩阵通过，54 项跨版本判据一致。
- 产物重建与提交件逐字节一致；包内 changelog 只留本节且与版本号一致。
