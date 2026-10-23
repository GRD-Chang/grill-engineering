# Delivery kernel glossary

**Delivery subject**: One stable Ticket, Parent-only delivery or final Parent
delivery whose work and completion can be identified separately.

**Semantic attempt**: One authorized role's work for one subject and generation,
including its interrupted or human-assisted continuations.

**Invocation**: One authorized execution period of a semantic attempt, with a
fixed deadline and Thread binding. Format corrections share that period;
ordinary or capacity recovery starts a new Invocation of the same attempt.

**Task**: Delivery of one Parent in one workspace and repository, independent
of which Run carries out that delivery.

**Host**: The managed process owning one execution epoch. Its startup,
execution and exit evidence do not by themselves prove that the Runner's
shared usage lease has been handed over or released.

**Source observation**: The Parent, Issues and dependencies returned by one
source read. Observing changed requirements does not accept them or reopen an
operator control.

**Effect intent**: The precise formal operation the program has authorized,
whose actual outcome can be confirmed independently of a response.

**Effect observation**: Evidence that an original intent reached its target,
remains at its prior state, conflicts with another value, or is still unknown.

**Gate**: A retained condition preventing further work without discarding its
subject's progress or evidence.

**Execution epoch**: One executor's bounded authority to start work. Retirement
permanently prevents that executor from starting subsequent work.
