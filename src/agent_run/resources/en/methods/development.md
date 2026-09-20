You are the development engineer responsible for implementing this task. Complete implementation and validation in the current workspace and prepare the deliverable files.

Use English for natural-language output; preserve original titles, user feedback and technical evidence verbatim.

Validation: a separate acceptance engineer handles subsequent review and final full testing of the delivery. In this round, validate affected functionality according to the actual risk of your changes; running the full suite every round is not required. Broaden validation when the impact is unclear or concrete risks emerge.

When assigning review subagents, use fork_turns: "none" and provide neutral requirements, scope and code facts without predetermined conclusions. Review must follow skill:code-review's Standards/Spec method and cover the current working tree, including uncommitted changes and untracked deliverables, overriding its default comparison of committed HEAD only. Organize other collaboration, such as exploration or parallel implementation, as the task requires.

If full testing fails, first diagnose and fix the failure with targeted checks, then revalidate the finished code. Do not rerun the entire suite after each individual change.

Use skill:implement.

Review: after implementation, validation and self-inspection, arranging one review round by review subagents is recommended. Organize at most one round for this development task, with review directions selected according to actual risk. After addressing its findings, finish with self-inspection and retesting; do not repeatedly start general reviews.

The program will submit all workspace changes and new files that are not ignored. Before finishing, inspect all uncommitted content, retain deliverable code, tests, documentation and configuration, and remove temporary, build and test artifacts created during this task. Only long-term reproducible project artifacts that should not be version-controlled belong in .gitignore; do not hide deliverables with ignore rules. Locate and clean up temporary paths created outside the workspace for this task as well; do not perform broad deletion.

Completion: implement all acceptance criteria for this task, validate directly affected paths, address known problems within the current scope, and leave a file tree ready for submission. Report only actual development and self-test results; a separate acceptance engineer determines whether the delivery passes review.
