# 有限危险文字规则的来源

`hermes_threat_patterns.py`（按固定文字特征检查内容的程序）直接来自 Hermes Agent（通用个人助理）正式0.22.5，固定版本编号为 `bf0203a794a07aac3ff764972ab9087baa4e4f4e`：

https://github.com/NousResearch/hermes-agent/blob/bf0203a794a07aac3ff764972ab9087baa4e4f4e/tools/threat_patterns.py

原始文件 SHA256（根据文件内容算出的固定核对编号）：`9e93a48960d55b7403c04bb30ca24ea178ba60dfc79e1a3938ce4e19a792fa02`。唯一附加为跳过本项目严格格式检查的文件头，不改规则行为。版权为 Copyright (c) 2025 Nous Research，完整 MIT License（允许复用、要求保留版权和许可的授权文字）随附于 `HERMES_LICENSE`。

严格范围只检查前65536个字符，可能漏判。它不保证识别所有密码、中文敏感事实或相似字母；事实笔记筛查也不代表聊天、待审提议或备份中没有敏感原文。

## 官方独立执行程序

文件和命令的系统限制复用 Codex（OpenAI 的编程助理）正式0.166.0的独立执行程序，通过本机输入输出管道使用；本项目的回答服务与聊天核心仍由本项目管理。固定发布来源和文件指纹（根据文件内容算出的核对编号）集中在[依赖资料](E:/Myrk/myrk/executor-release.json)。

上游 Apache-2.0（允许按列明条件使用和修改代码的开放约定）原文保存在[许可](E:/Myrk/myrk/vendor/CODEX_LICENSE)，原始归属声明保存在[声明](E:/Myrk/myrk/vendor/CODEX_NOTICE)，两份文字均从[固定正式发布](https://github.com/openai/codex/tree/rust-v0.166.0)读取并保持原文。

开发环境使用[组件准备脚本](E:/Myrk/scripts/setup-executor.ps1)取得并核对程序。实际程序位于本项目专用运行目录，不进入代码版本，也不使用用户电脑上另一份助理程序的自动更新版本。该脚本只准备本项目文件，不初始化系统账号或要求管理员设置。
