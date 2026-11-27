# Memory and project trust acceptance

`memory.register(registry)` and `trust.register(registry)` add independent cases to the maintained acceptance registry. Catalogue integration belongs to ticket 08. Run the public entry tests with:

```powershell
& E:\Myrk\.venv\Scripts\python.exe -B scripts/run_checks.py pytest tests/acceptance/test_memory.py tests/acceptance/test_trust.py
```

The cases create a new synthetic profile and project state per run. Personal recall explicitly shares one synthetic profile between separate chats. Project recall explicitly shares that profile between two synthetic projects, and checks the exact source and selected recipient before disclosure. No normal profile, earlier project notes, connection store, or credentials are read.

Every synthetic task project has its own `.git` root marker so guidance and skill discovery stop before reaching the owning Myrk checkout. Only the guidance cases deliberately create synthetic AGENTS/config files. Complete SDK input assertions reject the owning checkout's AGENTS source in the memory and recipient paths. The earlier pre-boundary evidence is retained as a historical fixture failure, not as the final isolation qualification.

The personal and project lifecycle cases exercise save, retrieval, replacement, patch, explicit removal, another chat, and retained original history. Failure cases cover denied and stopped writes, atomic batch validation, and an actual filesystem save obstruction. The last case preserves the product's erroneous `denied/scope` classification as a product failure; its strict signature and counterexamples prevent an unrelated error or an unreviewed fix from appearing successful.

The review cases keep the main delivery, permit one review under the original remaining budget, expose only the memory tool to that review, and leave batches containing corrections pending exact approval. Reopened approvals reject stale and repeated decisions. No-budget and stopped reviews have zero review SDK inputs; their unobserved tool and second-scheduling checks are `null` rather than fabricated successes.

`semantic-review.json` binds each main answer, memory action, and review output to task IDs and indexes in the original `exchange.json` SDK inputs. Memory-specific criteria cover supported facts, scope and recipient, exact changes, history deletion limits, and bounded review. Criteria are generated after execution and checked absent from model inputs. Fixed offline replies always keep `semantic_status=not_measured`; the bundle supplies review material, not a quality score.

`trust.guidance-trusted` and `trust.guidance-untrusted` use the real executor and Conversation. `trust.recipient-allow` and `trust.recipient-deny` exercise an exact new OpenAI-compatible recipient through the real SDK and a terminal MockTransport, including user approval under full access.

`trust.import-local-choice` observes the local machine trust store through the real `desktop_service.serve` standard input/output protocol in two managed subprocesses. It resumes a synthetic imported record, makes a local trust choice, starts a new task, restarts, revokes trust, and imports again. Raw service commands/events, process exits, actual SDK inputs, product attempt ownership, and Mock HTTP counts are retained. Import/open and the second process produce zero model requests; the first process makes exactly two Mock model requests.

This last case is a **local product observation**, not a live model quality case. Trust persistence and import handling are machine-owned operations; sending its artificial records to a real provider adds no evidence about those operations. Its dedicated offline subprocess cannot inherit an in-process SessionFactory, so it explicitly rejects a supplied live factory before opening a connection. It is excluded from the live model plan. The `formal.memory-*` cases use the authorized owner/relay/Main/service adapter with shared fixed material and program-fact criteria, rather than a local SessionFactory. The original conversation cases retain `context.session` for offline qualification.
