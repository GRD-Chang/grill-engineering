Use English for natural-language output; preserve original titles, user feedback and technical evidence verbatim.

Write the body for readers who have not seen the development conversation. By default, organize it into four sections:
- What Problem This Solves: describe the concrete usage scenario, the previous problem or limitation and the resulting behavior. Where useful, give a trigger and before/after example so readers understand what the change solves.
- Why This Change Was Made: explain why this implementation was chosen, its key design decisions and actual tradeoffs, and how they solve the problem. Focus on rationale without recounting the development process.
- User Impact: describe the affected users or maintainers, the changes they will experience and any actual operational, compatibility, configuration or migration requirements. If there is no user-visible change, say so and describe only the actual impact.
- Evidence: list actual tests or checks, results and coverage from the available evidence, including commands or report references where useful. State whether evidence applies to the current code and what remains unverified, failed or blocked. Passing results for old code must not be presented as passes for current code.

Scale length and combine sections according to the size of the change; avoid repetition between sections. Use Conventional Commit format for commit messages and PR titles by default.
