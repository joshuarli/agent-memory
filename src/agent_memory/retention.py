from typing import Any

from agent_memory import archive
from agent_memory import experience

HISTORICAL_EVENTS = (
    "SELECT count(*) FROM ("
    "SELECT s.session, s.position FROM learning_experience_sources s "
    "JOIN learning_experiences e ON e.id = s.experience "
    "GROUP BY s.session, s.position "
    "HAVING sum(CASE WHEN e.state IN ('retained', 'retired') THEN 1 ELSE 0 END) = 0 "
    "AND sum(CASE WHEN e.state IN ('withdrawn', 'superseded') THEN 1 ELSE 0 END) > 0)"
)


def _historical_events(db):
    if not archive._tables_available(db, "learning_experiences", "learning_experience_sources"):
        return 0
    return int(db.execute(HISTORICAL_EVENTS).fetchone()[0])


def plan(store, now) -> dict[str, Any]:
    db = store.db
    retire = []
    for experience_id in experience.retire_candidates(db, now):
        row = db.execute("SELECT id, claim FROM learning_experiences WHERE id = ?", (experience_id,)).fetchone()
        if row is not None:
            retire.append({"id": int(row["id"]), "claim": row["claim"]})
    generation_ids = {int(row[0]) for row in db.execute("SELECT id FROM archive_generation")}
    keep = archive.latest_generations(db) | archive.pinned_generations(db)
    prunable = generation_ids - keep
    sizes = {row["digest"]: int(row["size"]) for row in db.execute("SELECT digest, size FROM archive_object")}
    prunable_digests, kept_digests = set(), set()
    for generation in generation_ids:
        digests = archive._generation_digests(db, generation)
        if generation in prunable:
            prunable_digests |= digests
        else:
            kept_digests |= digests
    return {
        "retire": retire,
        "generations": {
            "prunable": len(prunable),
            "prunable_ids": sorted(prunable),
            "prunable_bytes": sum(sizes.get(digest, 0) for digest in prunable_digests - kept_digests),
        },
        "historical_events": _historical_events(db),
    }
