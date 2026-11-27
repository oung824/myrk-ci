# 补充、排队与并行停止验收入口

`builtin_registry()`登记正式`formal.queue-supplement`（D14/G03，帽16）和`formal.parallel-stop`（D15，帽24）。公开`run_plan`/`prepare_live`沿既有formal.handoff、真实Main/服务/SDK/保存/执行器；只在终端模型HTTP使用固定替身。一个D15执行包含两个真实会话，共一个原执行/父预算/4在途上限。

D14保存实际received/applied来源ID、request2时序、实际edit一次、未采纳材料无影响、两排队Task原goal/scope/approval、精确同聊历史批准和实际启动顺序。D15在同一个Main PID内使用不同record/Task、私有目标与本地接收端；A保真实编辑/流片段停，B继续自己的精确send后再独立停。

只有D15显式冻结`product_stop_scope=task`。Gate从实际保存RequestAttempt的number/step/actor/purpose/delegation绑定唯一产品Task，原记录字节以base64及SHA/bytes存同ledger；不收调用方task标签。Task停态约束后续所有step、子助理和审核归属，独立B继续共原父帽。未知费用不释放。旧v2完整原JSON指纹先校验再应用默认，不改写旧原件或重置停态。

服务控制`queue.supplement-stopped-saved`、`queue.partial-service-error`、`queue.partial-stream-error`经ServiceChannel真实公开send/open/start_next/stop验证，错误和部分影响不当正确stop。后两案工具可以观测通过，原产品仍failed；原服务失败恢复保护要求核查原任务，未执行的下一目标不能称完成。

固定回答的语义为not_measured；具名人审not_performed，用户接受pending。原生Send没有调用方投递ID或单独撤销补充API，两项合同保持unknown；停止整项未采用补充的控制不冒充单独撤销。QG5只贡献1/2/3/4/7/10及独立调优材料，实际质量冻结/执行交20/24。
