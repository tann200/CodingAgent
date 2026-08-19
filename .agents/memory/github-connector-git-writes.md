---
name: GitHub connector Git writes
description: Safe fallback for publishing local Git commits when the shell has no GitHub credential.
---

When an authorized GitHub connector is available but normal HTTPS push cannot access its OAuth credential, publish through GitHub's Git Data API. Upload file blobs as base64, construct and verify each tree, create the commit chain, and update the branch ref with `force: false`.

**Why:** Plain UTF-8 blob request bodies containing source/test code can trigger an HTML 403 from the connector proxy, while base64 bodies produce the expected Git blob hashes. Large batched proxy operations can also fail in the replay layer. GitHub-created commit SHAs may differ from equivalent local commits even when parent, tree, identity, date, and message match.

**How to apply:** Keep proxy operations small, compare every returned blob/tree hash with the local Git object, and make the final ref update non-forced. Fetch the resulting remote chain and prove its final tree matches the locally tested tree before aligning the local branch reference.

The Replit workspace can replace a nested repository's `.git` history with a local `gitsafe-backup` checkpoint while retaining the project files. Treat a local checkout with no GitHub remote or an unrelated parent commit as a snapshot, not proof of the remote branch state.

**Why:** The project content remained current while the nested checkout's `main` pointed to a local checkpoint unrelated to GitHub `main`.

**How to apply:** Query the GitHub branch head before publishing and build the remote commit from that head's tree. Do not force-push or use the local parent unless its remote tracking relationship has been verified.