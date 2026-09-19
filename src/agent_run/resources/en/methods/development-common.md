Use English for natural-language output; preserve original titles, user feedback and technical evidence verbatim.

Validation: a separate acceptance engineer handles subsequent review and final full testing of the delivery. In this round, validate affected functionality according to the actual risk of your changes; running the full suite every round is not required. Broaden validation when the impact is unclear or concrete risks emerge.

When assigning review subagents, use fork_turns: "none" and provide neutral requirements, scope and code facts without predetermined conclusions. Review must follow skill:code-review's Standards/Spec method and cover the current working tree, including uncommitted changes and untracked deliverables, overriding its default comparison of committed HEAD only. Organize other collaboration, such as exploration or parallel implementation, as the task requires.

If full testing fails, first diagnose and fix the failure with targeted checks, then revalidate the finished code. Do not rerun the entire suite after each individual change.
