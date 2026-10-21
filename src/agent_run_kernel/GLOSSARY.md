# Delivery kernel glossary

**Delivery subject**: One stable Ticket, Parent-only delivery or final Parent
delivery whose work and completion can be identified separately.

**Semantic attempt**: One authorized role's work for one subject and generation,
including its interrupted or human-assisted continuations.

**Invocation**: One actual execution of a semantic attempt, with its own input
context, thread binding and original returned result.

**Effect intent**: The precise formal operation the program has authorized,
whose actual outcome can be confirmed independently of a response.

**Effect observation**: Evidence that an original intent reached its target,
remains at its prior state, conflicts with another value, or is still unknown.

**Gate**: A retained condition preventing further work without discarding its
subject's progress or evidence.

**Execution epoch**: One executor's bounded authority to start work. Retirement
permanently prevents that executor from starting subsequent work.
