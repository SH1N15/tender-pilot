---
name: docx_format
description: 智能排版
category: output
version: "1.0.0"
license: AGPL-3.0-only
metadata:
  author: 投标智航 / TenderPilot
  triggers:
    - 排版
    - 格式化
    - 格式调整
---

# 智能排版技能

## 工作流程
1. 提取文档段落
2. ONNX分类(标题/摘要/正文等)
3. 模板查表获取格式参数
4. python-docx应用格式
5. 输出排版后文档
