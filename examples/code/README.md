# examples/code —— 示例用的"被测代码"

这个目录是示例的一部分，供 `examples/需求文档.md` 做核对演示。
它**故意写得不完整**，不要当成一个能编译的工程：

- 只保留 3 个文件（Controller / Service / ServiceImpl），用来演示
  "代码里有哪些接口、方法、字段"。
- 它引用的 `RefundApplyDTO`、`RefundApplyVO`、`RefundDetailVO`、
  `RefundWithdrawDTO`、`RefundOrder`、`RefundMapper`、`Result` 这些类型
  **不在这个目录里**，也没有 `pom.xml`。所以 Java IDE 会报
  "cannot resolve symbol" —— 这是**故意的**，不是缺 JDK。
- 本工具**从不编译 Java**，只把源码当文本解析，这些红杠不影响核对结果。

## 嫌 IDE 报错烦？本目录已排除

仓库根的 `.vscode/settings.json` 已经把它排除掉（`java.import.exclusions` +
`files.exclude`），用 VS Code 打开仓库不会再报错，也不会在文件树里碍眼。

- **IntelliJ IDEA**：右键 `examples/code` → Mark Directory as → **Excluded**
- 其它编辑器同理，把 `examples/` 排除出索引即可。

跑演示时仍然照常用它：

```bash
cd examples
python3 ../check_prd_code.py compare --code ./code
```
