const PROJECT_KEYS = Array.from({ length: 100 }, (_value, index) => index.toString(16).padStart(16, "0"));
const DIGEST = "a".repeat(64);

function record(id) {
  return {
    id,
    project: PROJECT_KEYS[id % 100],
    claim: `${id % 100 === 0 ? "retentionmarker " : ""}synthetic projection record ${id}.`,
    applies: `Synthetic applicability for projection record ${id}.`,
    observed: 1_700_000_000 - id,
  };
}

export async function seedLearning(db, offset, count, total) {
  await db.prepare(
    "INSERT INTO learning_state(origin_host, revision, epoch, digest, records, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
    + "ON CONFLICT(origin_host) DO UPDATE SET records = excluded.records",
  ).bind("origin-a", 1, 1, null, total, 0).run();
  for (let start = 0; start < count; start += 19) {
    const records = Array.from({ length: Math.min(19, count - start) }, (_value, index) => record(offset + start + index + 1));
    const statements = [];
    for (let index = 0; index < records.length; index += 4) {
      const chunk = records.slice(index, index + 4);
      const params = chunk.flatMap((item) => [
        "origin-a", 1, item.id, item.project, "bench", "method", item.claim, item.applies,
        `token-${item.id}`, `fingerprint-${item.id}`, "deterministic", "synthetic",
        null, null, item.observed, item.observed, "[]", "", "retained", null, null, 1, 0,
      ]);
      statements.push(db.prepare(
        "INSERT INTO learning_experience(origin_host, epoch, remote_id, project_key, project_label, kind, claim, "
        + "applies_when, identifiers, fingerprint, basis, detector, revision, worktree, observed_at, captured_at, "
        + "validity, tags, state, state_reason, state_at, sync_revision, pending) VALUES "
        + chunk.map(() => "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)").join(","),
      ).bind(...params));
    }
    for (let index = 0; index < records.length; index += 12) {
      const chunk = records.slice(index, index + 12);
      const params = chunk.flatMap((item) => [
        "origin-a", 1, "experience", item.id, `codex:bench-${item.id}@origin-a`, 0, DIGEST, "trigger",
      ]);
      statements.push(db.prepare(
        "INSERT INTO learning_source(origin_host, epoch, record, remote_id, session, position, digest, role) "
        + `VALUES ${chunk.map(() => "(?,?,?,?,?,?,?,?)").join(",")}`,
      ).bind(...params));
    }
    const searchParams = records.flatMap((item) => [
      "origin-a", 1, "experience", item.id, `${item.claim}\n${item.applies}`,
    ]);
    statements.push(db.prepare(
      "INSERT INTO learning_search(origin_host, epoch, record, remote_id, text) "
      + `VALUES ${records.map(() => "(?,?,?,?,?)").join(",")}`,
    ).bind(...searchParams));
    await db.batch(statements);
  }
}
