---
name: check-prd-code
description: >
  核对需求文档（PRD）与代码是否对得上，产出「待修复」「待确认」两份报告，适用于 Java 工程。
  当用户要核对需求与代码、检查需求有没有漏做、判断 AI 生成的代码是否偏离需求时使用。
---

# check-prd-code 操作说明

你是这套工具的**判断环节**。脚本负责翻代码、检索、核对、验真，
你只负责它做不了的那件事：**读懂代码逻辑，判断需求到底实现了没有。**

## 适用范围

- 只支持 **Java 工程 + MyBatis XML**；其它语言工程不适用。
- 需要用户提供：PRD 文件（.md）和代码目录。

## 脚本在哪

脚本与本文件同目录。下面命令里的 `<SKILL_DIR>` 指本 skill 所在目录
（即 SKILL.md 的父目录），执行时替换成实际绝对路径：

```bash
python3 "<SKILL_DIR>/check_prd_code.py" extract --prd <PRD 文件或目录>
python3 "<SKILL_DIR>/check_prd_code.py" compare --code <代码目录>
python3 "<SKILL_DIR>/check_prd_code.py" report  --code <代码目录> --out <报告目录>
python3 "<SKILL_DIR>/check_prd_code.py" status
```

## 分工

- 能用「搜得到 / 搜不到」回答的 → 脚本自己判，不会找你
- 必须读懂意思才能判的 → 脚本打包成任务包丢给你
- 谁都不能替你决定的 → 直接进「待确认」，交给人

## 三个环节

### 环节一：把需求改写成"能判对错的话"

跑完 `extract` 之后，读这个文件：

```
.checkprd/extract/条目化任务.md
```

按里面的要求写出两个文件：

```
.checkprd/extract/items.json     需求条目
.checkprd/extract/skipped.md     哪些内容没拆成条目、为什么
```

要点：

- 每条必须是**一句能判对错的话**。「提升用户体验」这种判不了对错的，不要产出条目。
- `kind` 字段定生死，只能填两个值：
  - `existence` —— 能用「搜得到 / 搜不到」回答的，比如「接口 `/refund/withdraw` 存在」
  - `behavior` —— 要读懂代码逻辑才知道的，比如「撤销必须按批次整批原子关闭」
  - **标错了两头都浪费**：`existence` 会被脚本直接判掉，`behavior` 才会交到你手上。
- 一条断言只判一件事。「接口 X 存在，且入参必填 Y」这种要拆成两条。
- 拆不动的（背景、术语、流程图说明、写得含糊的）**不要硬凑成假的可验证断言**，
  老老实实记进 `skipped.md`。**漏拆比错拆危险**——错了能发现，漏了永远没人知道。

**完成判据**：每个内容块都过了一遍；`items.json` 的 `id` 从 R-001 连续编号；
拆不动的全部在 `skipped.md` 里逐条注明原因。

### 环节二：判断每条需求实现了没有

跑完 `compare` 之后，读：

```
.checkprd/compare/tasks/batch-NNN.md
```

每个任务包里都有：需求条目、需求出处、脚本找到的候选位置、相关代码片段。
把结果写回同名的 json：

```
.checkprd/compare/tasks/batch-NNN.json
```

判定值只有五个：

| verdict | 什么时候用 |
| :--- | :--- |
| `implemented` | 代码里确实做了 |
| `partial` | 做了一部分，还缺 |
| `missing` | 完全没做 |
| `deviated` | 做了，但跟需求说的不一样 |
| `undecidable` | 给的代码不够判断 |

**四条硬规矩：**

1. `quote` 一律从代码里**逐字照抄**：原样复制，不加省略号、不改写、不概括。
   脚本会逐字比对，对不上这条判定当场作废，转进「待确认」。
2. `quote` 只写代码本身，**不带行号前缀**（脚本会剥掉行号再比，带了更容易出错）。
3. `file` 用任务包里给的相对路径；`line` 用代码块左边标着的那一列数字。
4. 判不了就写 `undecidable`：**宁可老实说"判不了"，也不编一个 `implemented`**。

关于 `missing`：判它的时候不需要给引用（本来就没东西可引）。
但**如果你能指出相关代码在哪、缺的是哪一段，就一定要写进去** ——
脚本会拿这个决定这条是"能直接改"还是"要人先看一眼"。

**完成判据**：`tasks/` 里每个 `batch-NNN.json` 都已回填；每条 `verdict` 五选一；
所有 `implemented / partial / deviated` 的 `quote` 都是逐字原文。

### 环节三：交报告

跑完 `report` 之后，把这两份文件交给用户：

```
01-待修复.md     证据齐全，可以照着改
02-待确认.md     需要人拍板，不要自作主张执行
```

如果用户接着说"帮我改"，**最多只能动 `01` 里的条目**，
并且改完要重跑一遍三步，确认那些条目消失了。

**完成判据**：两份报告都已交给用户，并说明 01 可直接执行、02 需人先勾选。

## 硬性要求

- 每个判定都要有代码原文撑着；拿不出原文，就写 `undecidable`。
- 只执行 `01-待修复.md` 里的条目；`02-待确认.md` 等用户勾完再动。
- 报告里的判定和位置由脚本写；你只在"依据"栏补自己的判断。
- 只依据 PRD 写到的内容判定；PRD 没写的（如"一般都要做参数校验"）不进报告。

## 安装

本 skill 的脚本与 `SKILL.md` 同目录，**安装时要把整个目录放进去** ——
只软链 `SKILL.md` 会让命令找不到脚本。

```bash
# 方式一：整目录软链（推荐，git pull 可同步更新）
./install.sh                       # 自动挑选已存在的 skills 目录
./install.sh ~/.claude/skills      # 或显式指定目标目录

# 方式二：手动整目录软链
ln -s "$(pwd)" ~/.agents/skills/check-prd-code
```

常见的 skills 目录：`~/.agents/skills`、`~/.config/opencode/skills`、
`~/.claude/skills`、`~/.codebuddy/skills`。
