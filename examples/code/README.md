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

## 嫌 IDE 报错烦？本目录已停止 Java 分析

仓库根的 `.vscode/settings.json` 配了 `java.import.exclusions`：用 VS Code 打开
仓库时，Java 语言服务不会分析本目录 —— **文件照常显示在文件树里，但不会报红**。

- **IntelliJ IDEA**：右键 `examples/code` → Mark Directory as → **Excluded**
  （IntelliJ 的"排除"会连显示一起隐藏，这是它和 VS Code 的区别）
- 其它编辑器同理，把 `examples/` 排除出 Java 索引即可。

跑演示时仍然照常用它：

```bash
cd examples
python3 ../check_prd_code.py compare --code ./code
```
