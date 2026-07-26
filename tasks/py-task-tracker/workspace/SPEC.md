# SPEC：task-tracker 命令行任务跟踪器

以 Python 包 `tracker` 实现，通过 `python -m tracker <command> ...` 调用。

## 存储

- 任务数据持久化为 JSON 文件；路径取环境变量 `TRACKER_DB`，未设置时为当前目录下 `tasks.json`。
- 文件不存在视为空任务列表（首个写操作时创建）。
- 存储文件内容损坏（非法 JSON 或结构不符）时：任何命令都应输出含 `corrupt` 字样的错误信息到 stderr 并以退出码 2 结束，**不得**抛出未捕获的 traceback，也不得覆盖损坏文件。

## 数据模型

每个任务包含：

| 字段 | 说明 |
|---|---|
| `id` | 正整数，从 1 起自增（取当前最大 id + 1）；导入的任务保留原 id |
| `title` | 非空字符串 |
| `status` | `open` 或 `done`，新建为 `open` |
| `priority` | `high` / `medium` / `low`，默认 `medium` |
| `due` | `YYYY-MM-DD` 格式字符串或 null |

## 命令

### add

```
python -m tracker add <title> [--due YYYY-MM-DD] [--priority high|medium|low]
```

- 成功：向 stdout 输出新任务 id（单独一行，仅数字），退出码 0；
- `--due` 非法日期（格式错误或不存在的日期，如 2026-02-30）：stderr 输出错误，退出码 2，不写入；
- `--priority` 非法取值：stderr 输出错误，退出码 2，不写入。

### list

```
python -m tracker list [--status open|done] [--due-before YYYY-MM-DD]
```

- 每个任务输出一行，字段以制表符 `\t` 分隔：`id<TAB>status<TAB>priority<TAB>due<TAB>title`（due 为 null 输出 `-`）；
- 按 id 升序排列；无匹配任务时不输出任何内容；退出码恒为 0（存储损坏除外）；
- `--status` 按状态过滤；
- `--due-before D`：仅列出 `due` 非空且 `due < D`（严格早于）的任务。

### done

```
python -m tracker done <id>
```

- 将任务标记为 `done`，退出码 0（重复 done 同一任务也返回 0）；
- id 不存在：stderr 输出错误，退出码 3。

### export / import

```
python -m tracker export <file>
python -m tracker import <file>
```

- `export`：把全部任务写入 `<file>`，JSON 格式为 `{"version": 1, "tasks": [ ...任务对象... ]}`，退出码 0；
- `import`：读取 `<file>` 合并进当前存储：
  - id 不存在的任务新增（保留原 id）；id 已存在的任务跳过（不覆盖）；
  - 导入**幂等**：对同一文件重复 import，任务不重复、内容不变；
  - stdout 输出 `imported <新增数>`（单独一行），退出码 0；
  - `<file>` 不存在或不是合法的导出格式：stderr 输出错误，退出码 2，存储不变。

## 通用

- 所有错误信息输出到 stderr；正常输出到 stdout；
- 未知子命令：退出码 2。
