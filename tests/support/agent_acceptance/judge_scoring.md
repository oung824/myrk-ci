# 普通裁判评分与人工抽查

入口位于`judge_scoring.py`。当前实际裁判资格仍为not-started，因此actual模式全部回人工、零模型发送；工具测试的qualified只属于明确合成参照。首次全人工基线及20的全人工路线独立使用，不需要开启本模块。

- `freeze_judge_scoring`：绑定22原资格包、候选实际字节、普通维度、五种输出及语言/领域，冻结原件、登记顺序和抽查；超320、超完整输入上限、争议、新风险、范围/提示改变或缺3.13工具均回人工。返回包不授予模型授权。
- `scoring_executions`：将每输出一次的scoring执行追加到原父计划；调用方明确传入已有依赖ID、order_offset，并继续原parent_id/accounting_root/账本，绝不单领320额度。`register_judge_scoring`的`preceding_cases`须对应依赖的case。
- `freeze_audit_plan`：用实际CPython3.13.0的`random.Random.sample`和种子20261116，按main_answer/child_report/review_reason/memory/summary固定顺序逐类抽，再从未抽中ID等概率补至十份。ID登记顺序、选择、实际样本数、调用方3.12与工具3.13真实版本分别保存；不读取裁判结论。
- `scoring_report`：只从实际SDK终端原件读取普通原判；独立读取既有人审包的完整历史和共同最终结论。确定性检查及关键维度均不能由普通AI填充，暂停不改写原产品结果；任何误放暂停相应类型并列全部judge通过项，至少两次误否且超过抽查人工通过分母10%时列该类型全部结论复核。其他范围必须有实际影响依据，不自动全停。
- `FixtureAudit`只用于明确合成测试，不产生HumanReview或实际签署；`fixture_final`、actual `final`、实际人审、AI原判和用户接受分别记录。缺证、异常及回人工均保留原输出分母。裁判调用单列，不产生第六种产品输出或新增产品任务。

在3.12项目环境运行抽查时，调用方明确给出项目内的独立3.13.0解释器。票23准备的官方嵌入式工具位于`E:/Myrk/.scratch/judge-sampling-python-3.13.0/runtime/python.exe`，官方URL、校验和及保留条件见实施资料`implementation/t23/sampler-provenance.json`；没有修改共享venv。没有该工具时可继续全人工，不下载或修改全局环境。永久测试可设置`MYRK_JUDGE_SAMPLING_PYTHON`，本项目默认查找上述E内工具；没有明确3.13.0工具则抽样相关测试标为未运行。

报告中的随机样本数表示冻结算法已选中的ID数，不表示实际人员已检查；`reviewed_sample_count`另列。未完成裁判段的报告保留原计划及`incomplete_judge_segment`，不宣称完成抽查或授予准入。

## 2026-11-16来源修复

`source_kind`与调用方的`deterministic_status`都是声明，不能提供实际通过证据。冻结包另存共同导出的原程序事实及其原始包；实际状态读取这些事实，保留全部已知失败。

`human_ordinary`、`human_critical`只记录已有具名人审历史产生的实际结论，逐维标注来源；未签模板不算人审。`FixtureAudit`只进入`fixture_ordinary`、`fixture_critical`和`fixture_final`，不能填actual维度或final。当前真实裁判资格未开始，合成native裁判原判也不能填actual普通维度。

actual全人工回退不以AI资格为门槛：若真实原件、程序事实、实际普通和全部关键人审都齐全，可据此判分；没有这些证据则unknown，已知程序或实际审阅失败优先。完整本地Mock原件仍明确标为非真实模型来源，不因计划回人工或FixtureAudit全通过而升级。产品原分母、旧AI原判、原失败与用户接受状态保持各自记录。

实际结论按各适用维度合并，不直接把共同content_status当全量人审门。全人工回退需全部适用维度的实际人审；若未来取得真正适用的实际资格，且原始终端证实real_http，AI只可承担其冻结普通维度。关键、其它未覆盖、抽中、争议或暂停范围仍需实际人审；缺证维度保留unknown。当前真实资格not-started，所以该实际AI分支关闭，不能用合成资格或Mock原判进入。
