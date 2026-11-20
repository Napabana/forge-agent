"""Project trust is a separate control from tool permissions."""

import json
import time


TRUST_SCOPES = ("project_skills", "project_guidance", "project_mcp", "project_hooks")


def read_trust(store) -> set[str]:
    row = store._conn.execute(
        "SELECT granted_scopes_json, repo_identity FROM workspace_trust WHERE project_id=?",
        (store.identity.project_id,),
    ).fetchone()
    if row is None or row["repo_identity"] != store.identity.key:
        return set()
    return set(json.loads(row["granted_scopes_json"])) & set(TRUST_SCOPES)


def change_trust(store, scope: str, *, grant: bool) -> set[str]:
    if scope not in TRUST_SCOPES:
        raise ValueError("unknown Workspace Trust scope")
    owner = store.claim("workspace-trust")
    with store.transaction(owner, owner.revision) as db:
        scopes = read_trust(store)
        if grant:
            scopes.add(scope)
        else:
            scopes.discard(scope)
        now = time.time()
        db.execute(
            """INSERT INTO workspace_trust VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET granted_scopes_json=excluded.granted_scopes_json,
               trust_version=workspace_trust.trust_version+1, trusted_at=excluded.trusted_at,
               revoked_at=excluded.revoked_at""",
            (store.identity.project_id, store.identity.key, json.dumps(sorted(scopes)),
             now, None if grant else now),
        )
    return scopes
