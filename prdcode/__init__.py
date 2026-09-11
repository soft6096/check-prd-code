"""check-prd-code —— 把 PRD 和代码摆在一起核对。

挑出三类东西：
  1. 需求里写了、代码里没做的
  2. 代码里做了、跟需求对不上的
  3. 代码里做了、需求里没提的（可能是多余的）

设计原则：能用脚本确定的，绝不交给 AI；只有"必须读懂意思"的才交给 AI；
AI 说出的每个结论，脚本都要回头验一遍。
"""

__version__ = "0.1.0"

# 各个阶段的名字，命令行与进度文件都用这一套
STAGE_EXTRACT = "extract"
STAGE_COMPARE = "compare"
STAGE_VERIFY = "verify"
STAGE_REPORT = "report"

# 中间产物统一放在这个目录
WORK_DIR_NAME = ".checkprd"
