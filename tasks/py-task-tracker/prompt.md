<!-- prompt_version: v1 -->
# 任务：按 SPEC 实现 task-tracker CLI 应用

当前目录下有 `SPEC.md`，完整定义了一个命令行任务跟踪器（`python -m tracker`）的行为规格，
以及一个空的 `tracker/` 包骨架。

要求：

1. 严格按 `SPEC.md` 实现全部命令（add / list / done / export / import），包括各错误路径的退出码与输出约定；
2. 采用 SPEC 驱动的工作方式：
   - 动手前把 SPEC 拆解为任务清单写入 `docs/TASKS.md`，实现过程中持续更新完成状态；
   - 实现与 SPEC 有任何偏差，须在 `docs/TASKS.md` 中说明原因；
3. 为你的实现编写自动化测试（框架自选），至少覆盖：正常路径、边界（如日期过滤边界）、
   错误路径（非法输入、损坏文件）与导入幂等性；
4. 只使用 Python 标准库；
5. 结束前运行你的全部测试并确保通过。
