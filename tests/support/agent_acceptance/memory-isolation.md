# 记忆验收适配器

公开入口仍为`run_plan(AcceptancePlan(...), registry=builtin_registry())`。`formal.memory-lifecycle`、`formal.memory-review`、`formal.memory-isolation`、`formal.memory-cross-project`、`formal.memory-withdrawal`走真实Main、服务、会话、记忆文件及原生SDK（服务库实际接收的请求）。只有最终模型HTTP使用固定替身，真实服务、账户和裁判请求保持关闭。

`memory_isolation.py`提供材料、正式场景与专有证据；`desktop/memory-isolation.mjs`只驱动公开桌面命令。D12每次24、D13每次12；R13—15每次16，全部角色和重试沿原execution_id及父账本。`materials/memory-isolation/risk-materials.json`固定每风险案两份相同基准和一份等价变体；每次独立合成资料根，不能补成功替换失败。Gemini仅初筛R13三次，其他协议离线能力继续登记。

D12在保存、更正、删除后分别新建聊天核对当前事实及SDK/终端输入，原聊天消息和执行轮保留。当前事实删除不声称抹除历史、备份、导出或外部副本。D13核对原主交付、有来源经验、一次复盘、实际memory_review角色、只含记忆工具以及原父预算。已完成任务后的桌面send是新任务；同一Task重复复盘拒绝、无额度、停止和待批批量提议复用`memory.review-*`公开Conversation分支。保存障碍、拒绝、停止和原子批量失败复用`memory.{save-failure,write-denied,write-stop,batch-failure}`，失败/未知影响保持原样。

R13直接比较B的实际SDK及终端请求，不只查看最终文字。R15以公开stop取消待批取用，并核对停后无新请求或动作。`formal.memory-revoked`是明确补充撤回后拒绝取用的离线控制。

## 已知产品缺口

当前跨项目待批对象只展示/绑定来源路径和服务接收方，没有展示或冻结确切笔记正文。测试驱动另经公开memory_list看到正文，只能证明观察值与实际效果一致，不能补造产品的正文批准合同。正常R14因此保持`product_status=inconclusive`、`approval_content_binding=unknown`。

`formal.memory-changed-source`是最小离线控制：实际Main形成待批取用后，受控外部变化只改本执行的合成笔记（7319→8842）；原approval_id、准备动作和批准背景指纹不变，旧允许实际取用新正文并进入B的SDK/终端输入。该运行`tool_status=passed`表示工具准确保留结果；产品是failed，K1/K2是failed。来源前后原件、SHA、待批参数、决定、保存记录及实际请求均保留。控制不计正式成功分母，但其已知问题不能因非正式身份而从组合报告抹去。本票没有改生产记忆、保存或权限，也没有声称版本准入通过。

## 审阅与质量材料

`memory-domain-review.json`关联实际事实、原件及逐次身份；共同输出仍保存主回答、记忆改动和复盘输出的真实输入、动作及来源。`criteria.json`沿用有来源事实、范围/接收方、改动保真、历史删除限制、有界复盘五维，普通事实错误不一律升级为关键安全违规。缺少正文批准保持未知，实际旧批准误用按已证实关键失败保留。固定回复始终不测语义，没有实际人工审阅或用户接受。

`qg4-templates.json`贡献十个候选正式材料家族和两个独立调优家族，列出独立金标准事实（只供审阅）、授权、禁止影响和人审判据；1—6常见、7—8变体、9—10恢复/预期拒绝，包含多种任务形态。它们没有参与本票模型执行、提示或阈值调优；正式选择/冻结与实际全人工准入由20/24完成。已经用过的初筛卡和固定回应明确为实施回归，不冒充未见正式集。
