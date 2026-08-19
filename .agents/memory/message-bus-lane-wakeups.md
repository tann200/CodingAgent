---
name: Message-bus lane wakeups
description: Why bounded delivery lanes use explicit event wakeups instead of timed asyncio polling.
---

Keep delivery-lane bridges event-driven: notify the loop after admission completes, use nonblocking lane reads, and preserve explicit active-admission accounting across shutdown.

**Why:** Under real-thread contention, timed semaphore acquisition could consume a released permit while surfacing a timeout, and executor-based blocking queue polls could hold delivery capacity while the poll itself was delayed. Both failures stalled a non-empty reliable lane.

**How to apply:** When changing message-bus admission or shutdown, avoid timeout-driven semaphore/queue polling. Preserve lane wakeups for successful and failed admissions plus shutdown, and retain repeated contention tests that combine blocked handlers, pending publishers, and graceful drain.