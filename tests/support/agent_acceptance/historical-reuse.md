# 整组历史复用与原件重评

历史复用从原 `result.json` 开始，保留全部执行身份、失败、无效、未执行及争议；计划分母必须与原冻结计划一致。缺旧计划的记录仍可导入，但必须由独立审阅者结合另存的完整组原清单证明后才能继承事实。清单保留原 `run_id` 与按原顺序列出的 `execution_ids`。

公开 Python 入口由 `tests.support.agent_acceptance` 导出。命令入口使用同一模块，不请求模型、裁判、供应商目录、账户或连接：

```powershell
python -B -m tests.support.agent_acceptance import-history <原result.json> <新目录> <change.json> --review-package <原review-package.json> --state-originals <原状态清单.json>
python -B -m tests.support.agent_acceptance import-history-applicability <historical-group.json> <独立适用性审查.json>
python -B -m tests.support.agent_acceptance import-history-regrade <historical-group.json> <只读重评分.json>
python -B -m tests.support.agent_acceptance import-history-material-use <historical-group.json> <当前使用核对.json>
python -B -m tests.support.agent_acceptance history-report <historical-group.json>
python -B -m tests.support.agent_acceptance export-history-report <historical-group.json> <新报告.json>
```

`HistoricalChange`记录本次实施者、变动理由及当前身份。`HistoricalApplicabilityReview`记录独立审阅者、实际范围、理由、完整组绑定和逐事实矩阵。AI 适用性审查使用 `ai_applicability`，实际人工适用性审查使用 `human_applicability`；两者都不生成或替代原 `HumanReview`。该接口记录明确提供的身份声明，不承担身份认证。每项 `fact_path` 是原执行 JSON 中的路径；原人工整体判定使用 `/original_human/case_status`。

每项矩阵检查源码、模型、提示、协议、权限、任务范围、保存、摘要、环境、材料、时间及判据。每项明确标为 `unaffected`、`affected` 或 `unknown`，保留理由与实际原件键。某个变化与该事实无关时，独立审阅者可以凭原件说明无关；不要求所有旧格式都具有后来新增的相同字段。不能证明时保持未知；复用一项事实只标为 `scoped_facts`，不扩成整次执行或新批全部通过。

`ReadOnlyHistoricalRegrade`保存提取版本、评分版本、范围、理由、同一次执行的原件提取检查和新解释。新解释经过独立适用性复核后才用于对应事实。旧执行、旧判、实际人工身份、具名结论和用户接受原样保存。新评分版本、缺原件或取证修复不会清除已知产品失败；整体产品/目标失败及已知授权/审核失败不能靠评分修订变成通过。没有新产品执行或付费裁判预约。若需模型裁判或修复后重验，应另走既有冻结计划及授权入口。

`HistoricalMaterialUseReview`将完整质量组中的原输入和目标身份关联到项目内现有 `.scratch/quality-material-usage`。旧清单没有后来新增的 authority 字段时，独立审阅者可提供当前规范 authority 的字节指纹；它只供内存中的当前查询使用，不重写旧清单或旧接受基线的 as-of 快照。查询每次读取当前持久记录，后来的调优使用仍会显示。该入口不初始化或重置记录，不授予新未见任务资格。

`AcceptancePlan(historical_groups=[...])`把这些记录接入 `builtin_registry()` 所用的 `run_plan` 总报告。历史原分母与当前执行分母分别保存；离线、当前真实、历史复用、历史真实和未测分列。已知固定替身字节优先于调用方的 `live` 或真实请求标签；独立审阅声明也不能把替身变成真实来源。旧格式真实/本地来源可另由独立审阅者读取实际原件证明，不能只引用来源自报。版本准入、真实质量基线、真实裁判资格及用户接受仍分别判断。

原事实也可以是字典或列表。报告按完整JSON内容指纹比较解释，`current_value`保留已核对的实际值；结构值只记`verified`，不因此变成内容或整体产品通过。
