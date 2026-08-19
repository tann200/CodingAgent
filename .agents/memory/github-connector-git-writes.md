---
name: GitHub connector Git writes
description: Safe fallback for publishing local Git commits when the shell has no GitHub credential.
---

When an authorized GitHub connector is available but normal HTTPS push cannot access its OAuth credential, publish through GitHub's Git Data API. Upload file blobs as base64, construct and verify each tree, create the commit chain, and update the branch ref with `force: false`.

**Why:** Plain UTF-8 blob request bodies containing source/test code can trigger an HTML 403 from the connector proxy, while base64 bodies produce the expected Git blob hashes. Large batched proxy operations can also fail in the replay layer. GitHub-created commit SHAs may differ from equivalent local commits even when parent, tree, identity, date, and message match.

**How to apply:** Keep proxy operations small, compare every returned blob/tree hash with the local Git object, and make the final ref update non-forced. Fetch the resulting remote chain and prove its final tree matches the locally tested tree before aligning the local branch reference.