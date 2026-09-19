Use English for natural-language output; preserve original titles, user feedback and technical evidence verbatim.

Use skill:code-review. Use the task above as its requirements source and the exact objects above as its review targets. When the target includes uncommitted content, override the Skill's default comparison of HEAD only.
In addition to the Skill's Standards/Spec review, you are responsible for E2E validation and the final results of all three dimensions.
All review or evaluation subagents must use fork_turns: "none" and receive only neutral task facts.

Select E2E entry points according to the requirements and delivery object, and establish that the critical flow from input through operation to final result completes:
- Directly usable features: perform critical operations through the actual usage entry point. Use a real browser for websites or browser extensions and actually execute commands for command-line tools. Verify user-visible results and expected changes to data, files and other outputs; merely opening a page or starting a program is insufficient.
- Libraries, code interfaces or internal modules: use the actual interface or a real caller to validate affected call chains and integration behavior, checking inputs, processed results and necessary side effects. Existing tests or temporary calling scripts may be used; do not reimplement the logic under test inside validation scripts.
- When both categories are involved, cover their affected paths. For documentation-only or other non-executable deliveries, validate content and usability for their actual purpose without adding artificial browser operations or unrelated runtime flows.

Code reading, isolated unit tests or simulated results for critical paths alone do not prove that these flows actually ran. Existing tests that actually cover the relevant entry point and complete flow may supply evidence from this round's execution. If dependencies or environment limits prevent validation of part of a flow, state the unverified scope accurately; do not present a local pass as an end-to-end pass.
Also complete the full tests and necessary checks required for the current delivery by applicable repository standards and testing guidance. Standards and Spec use static evidence and checks needed to validate concrete issues; do not repeat the same full suite unless a specific risk requires it.
Record the actual object, validation entry point, operations or commands, expected and actual results, necessary environment and exit codes where applicable. Do not claim unfinished checks passed.
When using a temporary runtime copy, briefly describe in evidence which acceptance object it represents and how content consistency was verified.
Reassess the applicability of old evidence after changes to code, tests, dependencies or environment. On failure, provide concrete evidence and revalidation requirements while leaving deliverable content unchanged.

When a previous independent review result is provided, prioritize its findings, the current changes and direct regressions. Without concrete risk evidence, do not repeat a full scan of unchanged code. Broaden review when current evidence or impact requires it.
