# 质量材料与全人工准入入口

`quality.py`扩展统一验收入口，调用方从`tests.support.agent_acceptance`导入。它只准备、验证和汇总，不取得实际模型发送权。

1. 每组按I6的1—10模板准备10个正式任务，再准备2个独立调优任务。每项明确`group/slot/purpose/template/shape`、独立材料家族与目标、原件路径、金标准路径、授权、允许路径、禁止影响、必需输出及适用兼容合同。每组至少4种形态；材料家族、目标和实际输入字节重复、留出家族交叉或已用于调整的正式材料会被拒绝。程序核对字节和声明；实质独立性仍须材料负责人审查，不能把改文件名当成独立任务。
2. `freeze_quality(..., destination=..., provenance=..., source_version=..., environment=..., models=..., criteria_version=..., qualification_families=...)`在输出出现前冻结材料和标准实际内容。真实候选用`reporting.environment_facts()`及`quality_model_settings()`读取本地环境与03现有路线快照；后者不访问供应商。金标准单独保存，不能当作模型输入。
3. `quality_plan(frozen, registry=..., adapters=..., parent_budget=...)`把72个任务映射到已经实现的领域用例。调优在前；正式Gemini固定每组3/9，合84次服务执行、72个不同任务。DS请求帽2160、Gemini360，费用帽39,076,920微美元（一个微美元为0.000001美元），仍使用04的同一父预算、根和账本。`prepare_live`、`run_plan`继续是唯一准备与执行入口；不会另领授权。缺实际材料消费适配器即拒绝，不能把所有模板套成简单问答。真实候选还要通过正式Main的`builtin_registry`重建同一原件、金标准和冻结清单绑定；只改调用方注册信息不能宣布真实就绪。
4. `quality_material_report(frozen, plan=plan, registry=registry)`核对实际公开执行计划，再分别显示合成材料就绪、真实材料就绪及发送权。仅看材料文件不能报告执行路径已就绪。
5. 如需初筛共享，在正式执行前把完整初筛安排传入`screening=`，使用04的`SharedCoverage`将最多12项正式任务与同家族、同原件、同候选版本和完整分支绑定。标准绑定本冻结文件，采用更严格帽；一个生成只有一笔费用及一个质量样本。不能共享调优任务，也不把节省换成新任务。
6. 执行后调用`export_quality_review(frozen, result_path, destination, final_reviewer=...)`。它仍只导出五类实际产品输出；输入原字节、金标准、授权及评分冻结清单是评审背景原件，按执行ID和SHA-256（内容指纹）绑定并去重复制，不增加输出或任务分母。这些背景原件不是人工参照标签或理由，后续裁判不能据此混入人工判定。默认`export_review`行为保持兼容。
7. 冻结格式v3保存原source相对身份；`quality.input/quality.gold/quality.freeze`各有执行ID与SHA。审核、参照读取和只读重判可使用包内`quality.freeze`路径，源工作树及原材料回收后仍校验包内原字节，不把副本当当前执行源已核验。`quality_plan`和材料就绪检查继续严格核当前源文件。

实际人审记录继续用`HumanReview`，通过`import_quality_reviews`转到共同的`import_reviews`校验，具名、时区时间、实际范围、全部原件及冻结维度均不可省略。合成验证另用`SyntheticQualityReview`并显式给`synthetic_destination`；它没有人名或签名字段，不写入人审目录，也不转换成`HumanReview`。
8. `quality_report`保留原计划、每次开始/有效/通过/失败/未知/无效/未执行及缺口，单列DS六组和Gemini有限兼容。DS各组≥8且总≥54；Gemini总≥10且各组≥1，并满足全部适用兼容、关键合同及零严重安全违规。普通失败不会自动变成兼容违约；数字达线也不能洗掉必需未知、缺摘要或关键失败。用户接受另记。
9. `save_quality_reference`只显式新建参照文件，拒绝覆盖。真实首基线必须绑定实际原件、全部必需人工审阅和完整准入；合成参照永远分开，重算指纹或改标签不能升格。首次明确“尚无跨版本退步结论”。匹配比较绑定材料、模型设置、环境和标准实际字节，分别保留源码版本，允许代码版本变化。DS净降最多3项且每组最多1项，同时满足绝对线；Gemini只列逐项/组变化，不设置小样本零退步。异材料/模型/环境/标准不得直接比百分比；旧版60次/1800请求/26,542,800微美元另列提议，不在当前预算或授权内。
10. 正式材料被用于实现、提示、阈值或裁判调整后，调用`record_quality_adjustment(..., evidence=实际决定原件)`保留用途和指纹。该材料转为回归，旧判、旧失败和旧分母保留；不能继续当未参与调整的新质量集。只读审阅、原质量执行和历史身份本身不等于调优；21仍须独立验证完整历史组适用性，不能自报历史来源便通过。

## 冻结评分材料隔离范围

制备者在 `prepare_live(plan, root=..., registry=..., evaluator_isolation_directories=[...])` 传入一份共同项目根下的相对目录清单，路径以 `/` 分隔。同次冻结的规范质量用例共用该清单，清单参与冻结指纹。将临时评分副本所在的目录一并声明。使用 `[]` 表示无需额外目录，评分原件、源码和运行控制资料的基础隔离继续生效。绝对路径、越出项目或经链接重定向的路径会在制备时拒绝。

正式 CLI、pytest 和 Main 从冻结原件校验声明。声明缺失，或运行中的声明和拒绝路径与冻结原件不符时，在预留请求额度前停止。旧包可通过快照或归档读取入口只读复核。`verify-prepared` 仍要求与原包相同的源码绑定。符合原父计划复冻条件且尚未尝试的用例，可通过 `gate.propose_candidate(evaluator_isolation_directories=[...])` 首次补齐声明，再按原流程审阅及激活，继承原额度、用量和停止状态。后续同父候选沿用声明。其余旧用例重新执行时，按现有流程重新制定计划并取得授权。

## 工单24待补的阶段接缝

当前既有04在准备时冻结全部源码。独立调优后改变候选代码/提示，`quality_candidate_status`会显示`candidate_refreeze_required`且零发送，保留原prepared/ledger和累计用量；已停止控制继续停止。它不实现重新冻结，也不把失败调优改成必须12/12成功。

24还须把实际质量材料包加入可重建的领域注册；当前`formal_scenarios.material_bindings`仅列固定领域目录，不自动接纳任意`.scratch`包。20合成注册证明流程，未证明全部60模板已具备真实Main适配器。

24必须在同一个`parent_id/accounting_root/ledger`内补显式正式候选阶段冻结，保留原包/旧结果/全部次数金额占用，不增加任务或预算、不复活停止根、不自动授真实许可。依据见`.scratch/retro-agent-live-acceptance-20261116/implementation/integration-gaps.md`及24 Comments，父已在`ae5d53ca3b5c7e2d14f2d9ffc1882109ebbd70e8`记录。公开拒绝证明为本票`test_candidate_source_drift_requires_same_parent_stage_refreeze_and_preserves_closed_original`，原绿回执`b-jvser36i`。

本票`synthetic.json`只用于流程合同检查，已用于实现，不是未见正式材料、裁判留出或真实人工基线。工具验证、材料准备、真实基线、真实模型质量与用户接受分别记录；本票没有实际模型、裁判、账户/目录/计数API调用，也没有继续旧停止根。


## 逐任务域标准

`QualityTask.domain_standards`按五种输出类型分别冻结专有标准的version、全部dimensions及critical/适用属性。`AcceptanceCase.output_standards`是适配器事前声明：None表示未知，{}才表示明确只用通用标准；legacy选择/运行不因此改变。质量计划在调用方和canonical重建两端比对完整声明，未知、缺失、版本冲突或critical降级均在执行前拒绝。24仍须装配各域真实标准实例。

入跑前`version_facts.catalogue`对声明做深拷贝；输出后不能把None改写成{}，也不能看过结果后增加维度并冒称事前冻结。导出再次核对实际`output_standard`，缺失/漂移/冲突不形成就绪包。共同`export_review`可选`execution_standards[execution_id][输出类型]`路由，只作用于该执行；默认入口兼容原行为。合并后的维度同名只保留一次，基础K1—K5/关键状态及域critical属性不能丢失或降级，domain_criteria原文仍在。每任务的原始标准实际字节及组合版本进入参照匹配；critical域维度失败不能平均进一般质量。


## 当前用途与历史参照

冻结格式v4把持久用途authority的实际路径和SHA绑定在清单内；v3原件仍可只读校验，但没有当前用途authority时只显示未知，不能当当前未见材料准入。验收负责人首次显式调用`initialize_quality_usage_authority()`，在Git共同项目根`.scratch/quality-material-usage/`创建唯一authority、完整索引、用途记录和实际决定原件；已有目录拒绝覆盖，丢失或截断不自动重建空历史。这是持续领域资料，不是运行缓存，须在所有依赖冻结包不再要求当前用途核验后另行退休。

`record_quality_adjustment`以完整任务输入原字节SHA和实际任务目标联合识别任务。源版本、任务ID、材料家族标签、金标准修订、冻结清单和包路径变化都不重置同输入/目标的已观察用途；复用某个附件但目标不同不自动等于同一任务。决定证据保留原身份、实际字节副本及SHA；原冻结同级`.uses`继续留作审计副本，当前用途以项目authority的完整索引为准。明确synthetic fixture给目标附独立实验ID，只用于隔离各次工具实验，不能用来洗掉真实任务用途或宣称正式材料独立性。

`quality_report`每次从冻结绑定的项目authority核验当前用途；`material_usage.scope=current`与`materials.usage_verified`明确范围。authority缺失、不匹配或索引缺失时当前用途未知，`formal_unseen_eligible=null`且当前qualified=false；截断、变更、越界原件直接拒绝。包内副本和导出时的快照都不能单独证明当前未用。`quality_plan`继续核当前源文件并拒绝未知/已用材料，不从便携包获得发送权。

`save_quality_reference`只在当前用途可核且准入通过时保存不可覆盖的`usage_snapshot`，含实际观察时点和输入/目标身份。以后用途改变不追溯改写该参照当时的通过、原失败或原分母；比较重新核原输出、原人审和冻结判据，并明确`baseline_usage_scope=historical_as_of`，当前准入仍独立依当前用途判定。已有旧版已接受参照保留`legacy_accepted_reference`时间范围，不据此声称当前未见或新建当前基线。actual人审/首基线与真实发送权限边界不变。
