# Agent acceptance entry

Set `$myrkPython` and `$myrkProjectRoot` using [Project Python](../../../docs/repository-checks.md#项目-python). Run commands from the source checkout.

```powershell
& $myrkPython -B scripts/run_checks.py pytest tests/acceptance/test_selected.py -s --acceptance-list
& $myrkPython -B scripts/run_checks.py pytest tests/acceptance/test_selected.py --acceptance-case ordinary.attachment --acceptance-protocol deepseek
```

The entry uses the common project's pinned executor unless `MYRK_EXECUTOR_PATH` specifies another location. It requires an installed match and restricted execution.

The commit hook runs lightweight checks only. Choose acceptance coverage separately using the [check policy](../../../docs/agents/check-runs.md#检查责任与最终版本). With the managed command, use `pytest tests/acceptance --acceptance-profile core -q` for the offline core or `--acceptance-profile full` for full qualification. Explicit cases and test nodes still run in `core`.

Use `--acceptance-plan` for a fixed `AcceptancePlan` JSON. It rejects selector and mode overrides. Without a plan, repeat `--acceptance-case`, `--acceptance-capability` or `--acceptance-protocol`. Invalid selections fail before execution. Defaults are `ordinary.question` / `deepseek` / `offline`.

Find results and original SDK/HTTP/conversation evidence via `retained_reports.paths` in the check receipt. Relative links survive relocation. Public API callers choose a project-owned `root`.

Set `$preparedRoot = Join-Path $myrkProjectRoot '.scratch\prepare-example'` to an unused path. Pass `--acceptance-root $preparedRoot`. Choose `prepare-live` in the fixed plan or use `--acceptance-mode prepare-live` for a selector-based run. Preparation freezes sources, bindings, plan and paths without authorizing sends. Case hooks can freeze settings and rebuilt desktop hashes. Frozen adapters run here after cleanup. Verify with `& $myrkPython -B -m tests.support.agent_acceptance verify-prepared $preparedRoot`.

Capability modules expose `register(registry)` and public entry tests. Register `AcceptanceCase(case_id, capability, protocols, run, bindings=...)` with supporting execution modules, fixed material and criteria in `bindings`. The freeze covers product Python, provider/executor metadata, public support code, catalogue inputs, lock files and pytest config. `builtin_registry()` serves pytest, the frozen CLI and authority checks. Its catalogue lists case ID, owner ticket, capability group, native protocols, observation layer, static expectation, bindings, dependencies and applicability. The independent [required catalogue](../../acceptance/materials/catalogue/required.json) defines required cases and native protocol combinations. Missing entries fail verification.

The [catalogue](catalogue.py) defines capability groups. Local file or process facts without a model run once. Model cases run for their registered protocols. Use execution evidence for live coverage.

`async run(context: CaseContext) -> CaseOutcome` receives an isolated root, protocol, run/case IDs, actor and purpose. Offline cases use `context.session([Reply(...)], config=...)`; an injected `SessionFactory` shares the `AcceptanceSession` contract. The default session wraps the real provider SDK with finite `httpx2.MockTransport` responses. Live cases require a registered formal service adapter and a validated `LiveGate`; they cannot fall back to a local offline model or bypass the gate by injecting an arbitrary session factory. Model and HTTP are the only substitute boundaries. `tests/acceptance/conftest.py` blocks all uninjected HTTP transports only within acceptance checks.

`OfflineSession.save(...)` binds full SDK messages/settings/tool parameters, exact outgoing JSON, received response bytes, errors and the product's original request attempts to run/case/task/protocol. Product attempts keep `actor`, `purpose`, delegation identity and request number; they are not inferred from output names. Fixed responses always have `semantic_status=not_measured`. `CaseOutcome` separates tool, product and semantic results.

Isolated failures keep the first error and continue. `SharedControlError` stops the run, saves the original control failure and marks remaining combinations unexecuted. `LiveGate.validate(...)` checks the prepared binding, explicit `LiveAuthorization`, per-case/total bounds and persisted stop state before connection acquisition, without charging a request. `reserve(...)` repeats those checks and persists one attempt immediately before sending; retries and auxiliary purposes reserve separately. The gate is used by one serialized send owner, following the formal service's ownership boundary.

Ticket 05's `desktop_provider.register(registry)` adds settings/catalogue/model selection, explicit recipient switching, native retry/interruption/missing-usage cases, `desktop.handoff` and `desktop.lifecycle`. Three protocols apply to provider behavior and the formal handoff; file/process lifecycle is exercised once. The lifecycle case reuses the maintained desktop drivers through the actual Main and service, native encryption and Windows executor. It retains distinct save/restore/forget, close/reopen, normal exit and intentional crash/recovery originals.

The `formal.handoff(context)` route used by `desktop.handoff` rebuilds frozen Main, verifies output hashes and validates authority before importing owner Main. It then uses the production owner → relay → isolated Main → Python service → SDK chain. Opening a connection validates authority without charging a request. Plan-external catalogue HTTP is rejected; selected model catalogues are frozen. Mock/real model HTTP and actual `app.getPath('userData')`/ConnectionStore observations remain separate. Mock and real modes share one outer request/response-byte/error collector; the terminal Mock responder has its own private response-script list. SDK inputs and terminal requests join by unique `sdk_input_id`, with actor, purpose, delegation and product number read from the real `RequestAttempt`.

The managed pytest entry accepts separately authorized JSON via `--acceptance-live-request`. `LiveExecutionRequest` contains `run_root`, the explicit `grant` and an optional `case_id`/`protocol` pair. Without the pair it uses the prepared plan. Other selectors cannot override it. The fresh-process `formal_cli execute` child uses the same typed input and registry. `transport="mock"` uses synthetic encrypted profiles. Real execution requires a separately authorized `connection_profile`. One OS file lock enforces exclusive send ownership. Main work, child work, review, summaries and retries reserve a persisted slot immediately before terminal HTTP. Reopening retains the count. Live execution requires the formal entry with its validated gate.

The finite formal scenarios share materials, public `serve` commands and program-fact scoring across terminal Mock qualification and future real answers: question/attachment, exact file effects, extension calculation and native check, controlled loopback send, user approval, independent read-only review, delegated verification, memory lifecycle/isolation/review, compaction pending-material/explicit-request/adoption and controlled restart. Real output is not scored by fixed sentences or exact expected request counts. Fixed 503/interruption/fake-acceptance/receipt-loss/obstruction negatives stay offline-only. Offline qualification uses synthetic profiles and local substitutes for model and catalogue requests. It records semantics as unmeasured. Use the [bounded future plans](../../acceptance/plans/README.md) for per-case and per-root caps and stop/resume conditions. Live execution requires separate authorization and fresh roots from final stable main.

Ticket 06's `resume_queue.register(registry)` adds `queue.supplement`, `queue.saved-stop`, `parallel.stop`, `recovery.original`, `recovery.legacy`, `recovery.delegation`, and `recovery.desktop-restart`. Model paths run across the three native protocols; saved queue and desktop process facts run once. These cases remain offline: their real service/SDK calls end at a finite `MockTransport`, and no case is advertised as a qualified live adapter. The service keeps actual saved product request ownership alongside the original HTTP exchanges. Synthetic folders have their own `.git` boundary and an explicitly synthetic guide; only the declared parent/child, queued, concurrent, imported, or restarted relationships share state.

`service_channel.ServiceChannel(context, name, replies, gates=..., records=..., interrupt_after_edit=...)` drives the production service's public JSON commands in a real process. `arrived(index)` observes a terminal Mock request; `release(index)` releases its external response. The stopped task cannot send again; opening/importing/verifying are checked at zero sends. The planned edit interruption observes Python's public `os.rename` audit event and actually exits the fixture process with code 71 before the new saved record replaces the reliable one. It does not replace Conversation, filesystem or recovery methods. Reliable record, pending record, original file bytes, actual changed bytes and the local exit observation remain separate evidence. Imported records do not acquire the original machine's action ownership.

`resume_desktop.run_desktop_phases(context, driver=..., phases=..., extra_environment=...)` builds the current Main and starts each supplied Electron driver only after all owned processes from the prior phase have exited. Each phase contains a `name` and an `environment` dictionary; `desktop/restart.mjs` supplies `MYRK_ACCEPTANCE_RESTART_PHASE`. Callers can reuse this driver for combined recovery cases. Builds use an exclusive short directory within the current managed check root to respect Chromium's file-URL length limit; dependencies remain shared. `recovery.desktop-restart` performs a real file edit and specified native check, preserves the terminal Mock HTTP 400, quits normally, launches a fresh Main, opens without sending, explicitly verifies and resumes the same task. The real program's saved exit receipt and its single append marker establish that no successful action was repeated. Local planned exits, supplier Mock errors and unmeasured real service behavior have distinct facts; fixed replies always retain `semantic_status=not_measured`.

`integration.compaction-restart` creates repeated summaries through actual same-task SDK/tool exchanges, performs a real edit/check, exits normally, opens at zero sends, reads original summary evidence, verifies and resumes the same task without repeating effects. It retains original message prefixes, summary replacement/coverage, scope/approval, counts and an independently decoded check receipt. `recovery.delegation` preserves actual parent/child and applied-supplement relationships. `formal.restart` uses the formal owner chain and a controlled local pre-send transport failure after the actual completed check; the service saves the failure, Main exits normally, and a fresh Main verifies/resumes the same task. Its injected faults have no terminal HTTP and are not supplier responses. It does not cover forced service-process exit or uncommitted unknown-effect save recovery; `recovery.original` separately covers the actual exit-71 save boundary.

`result.json` separates tool contract, product status, static expectation and semantics. The coverage matrix lists offline, historical real, current real and unmeasured combinations separately. Intended obstruction, lost receipt and false acceptance remain product failures even when their tool expectation is met. Known symptoms require specialist exact matches; unrelated or changed symptoms fail. A normal current success does not close historical `actions.check`, `actions.files` or DeepSeek-only `review.service-retry` save failures. Early soft-trigger compaction with no compressible history must continue below the hard budget without inventing a summary. Memory filesystem save errors must report execution failure rather than authorization denial. Those two cases now require ordinary success; their original failures remain archived. Reopening logical-step identity remains inconclusive.

Acceptance tests and each formal owner/receiver service retain `save-diagnostics.json` observations: original save-call counts and filesystem exception metadata with stack locations. The observer rethrows the original exception and writes no record content, exception message, credentials or local variables. Diagnostic success is evidence collection, not a persistence repair or an exemption for an unexpected failure.

Every evidence link is checked and fingerprinted in `evidence-index.json`; missing required files, wrong SDK/wire ownership and mismatched independent action/exit receipts cannot be filtered into a pass. Shared control failure stops the run and leaves remaining cases unexecuted. Failed dependencies never start. Formal raw records retain original paths; `facts.evidence_relocation` resolves them relative to the current case directory after disposable roots or managed reports move. Versions contain actual consumed source/material fingerprints, installed SDK versions and native runtime observations, rather than a Git HEAD claim. Missing usage stays unknown; layout does not prove cache benefit.

`facts.observed_counts` counts distinct original product attempts, complete SDK inputs, terminal Mock or real HTTP, and planned local requests that were reserved but never sent. A local case without model evidence keeps those measurements unknown. Recovery evidence can additionally retain attempts from an uncommitted pending record: each links to the original pending bytes and SHA, remains explicitly uncommitted, and never replaces the reliable saved record. Formal results retain the prepared manifest, grant and final ledger; each ledger number joins exactly one original SDK/terminal observation with the same request role and identity.

`all-capability-review.json` binds every received main/child/reviewer/memory/summary output to its complete SDK input and original wire/response fingerprints. Parent and child are reviewed separately; memory reuses its five dedicated criteria and compaction its six. Program facts are automatic, meaning is reviewed by a named person. Fixed replies stay `not_measured`, and absent/not-applicable outputs never become semantic passes. Criteria are created after execution and are not model input.

The qualification fixture retains original asyncio errors and traces. One current symptom is classified as a known product failure: a single offline `review.stop/deepseek` through its dedicated test or the stable selected entry, with cancelled turn/review, two bound main/reviewer SDK/HTTP attempts, no candidate write, the exact original Myrk cross-context token reset plus both observed SDK generator-close errors, and the measured source/dependency versions. It preserves the original result bytes and raw diagnostic, then derives `product_status=failed`, `tool_status=passed`, `expectation_status=review_required`; the result, coverage matrix, evidence index and human review retain that failure. Fixed-output semantics stay `not_measured`, and the terminal summary prints the product failure. Every unmatched asyncio error still fails qualification; action stderr and Chromium/GPU logs do not become Python shutdown failures. A normal current success keeps the [historical shutdown issue](../../../.scratch/acceptance-capture-teardown-diagnosis-20261113/issues/01-cross-context-close.md) unknown and does not close it or establish a shared historical cause.


## 问答与共同审阅

`ordinary.question`、`ordinary.attachment`、`ordinary.encoded-long`仍从同一选择入口执行。目录在执行前登记D01/02/03的4/6/12请求帽和Gemini G01的6请求帽；它们是计划上限，不是发送许可。问答使用独立的6×7=42事实，附件保留原件与后续原生输入，编码长结果保留UTF-8 BOM、CRLF及完整响应。附件通过既有`full_access`方式的`read_only=true`设置组合执行，不增加批准方式。正式附件适配器只初始化合成项目的真实`permissions.json`，再沿Main、服务和SDK执行。

结果保留工具、程序事实、目标、证据、普通内容、K1—K5和用户接受。`case_status`是完整逐案结论：已知失败保持`failed`；缺证、必需内容待审或未解决的关键维度保持`pending_review`。兼容字段`product_status`可表示离线工具合同通过，不能据此宣布真实语义通过。

共同文件`all-capability-review.json`继续保留每次请求的原件，并登记实际产生的主回答、子报告、审核理由、记忆变更和每份摘要。纯工具调用留在请求轨迹；无法解析但已收到的原文保留为不可读输出，含原因且不自动通过。领域判据单独带版本，压缩的六项判据不会变成所有输出的共同要求。

审阅命令无网络请求。换成实际结果路径、尚未创建的审阅目录和最终审阅人姓名后导出。

```powershell
$resultPath = Join-Path $myrkProjectRoot '.scratch\acceptance-example\result.json'
$reviewDir = Join-Path $myrkProjectRoot '.scratch\review-example'
& $myrkPython -B -m tests.support.agent_acceptance export-review $resultPath $reviewDir --final-reviewer '审阅人姓名'
```

导出校验原执行的证据指纹，再复制所需原件和冻结标准。`unsigned-review-forms.json`是未签署表单：实际审阅人需填写`review_id`、`reviewer`、带时区的`signed_at`、实际范围、已审原件和逐维度结论，声明`authorship=human`并移除`unsigned`。AI整理内容不能据此冒充实际人审；此接口记录调用方提供的具名声明，不是身份认证。缺维度、缺原件、原件变化和不成立的不适用条件均不能导入为通过。

签署后将具名审阅 JSON 列表保存为审阅目录中的 `signed-reviews.json`。导入成功会打印报告。

```powershell
$reviewPackage = Join-Path $reviewDir 'review-package.json'
& $myrkPython -B -m tests.support.agent_acceptance import-review $reviewPackage (Join-Path $reviewDir 'signed-reviews.json')
```

回查已导入的审阅时，单独运行 `& $myrkPython -B -m tests.support.agent_acceptance review-report $reviewPackage`。

审阅记录只追加，同一ID不同内容拒绝；分歧保持待审。指定最终审阅人须引用全部原判并逐维度说明理由；同一输出及冻结标准只能最终复核一次，重复导出不能归零。相同记录重导入保持幂等。通过 `--standards` 指定新版标准的 JSON 文件并导出到新包，保留原包、原件和旧判。执行目录旁的 `review-exports.json` 保存包的索引；完成本批审阅前不要删除索引或被引用的包。

`review-report`另给`case_results`，表示本次具名审阅推导的新结论；`original_product_results`保留执行时的旧判。任何已证实的产品/工具/目标/事实/关键合同失败均不能被审阅洗成通过。必需关键维度仍未知时保持待审；可在冻结的领域标准中逐个关联K1—K5，程序事实和普通内容分别成立后才可能形成完整通过。固定回复即使导入测试表单，也保持`semantic_status=not_measured`，用户接受始终另记。

本票只验证离线传输和真实本地效果。采集端未保存的压缩响应字节、缺失原件或未验证的原生结构无法由审阅层补造，受影响项保持失败或未知；本票没有进行真实模型或实际人工签署验收。


整组历史复用、独立适用性复核、只读重评和当前材料使用核对见[历史复用入口](historical-reuse.md)。历史事实通过 `AcceptancePlan.historical_groups` 进入同一公开总报告，不增加当前执行分母或发送许可。
