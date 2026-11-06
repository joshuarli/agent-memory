import { readFileSync, readdirSync } from "node:fs";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

function statements(sql) {
  const result = [];
  let current = "";
  let inTrigger = false;
  for (const line of sql.split("\n")) {
    const trimmed = line.trim();
    if (trimmed === "") continue;
    current += (current === "" ? "" : " ") + trimmed;
    if (/^CREATE TRIGGER/i.test(current)) inTrigger = true;
    if (inTrigger && /^END;$/i.test(trimmed)) {
      result.push(current);
      current = "";
      inTrigger = false;
    } else if (!inTrigger && current.endsWith(";")) {
      result.push(current);
      current = "";
    }
  }
  if (current !== "") result.push(current);
  return result;
}

export async function seedDatabase(db) {
  const files = readdirSync(MIGRATIONS).filter((name) => name.endsWith(".sql")).sort();
  for (const file of files) {
    const sql = readFileSync(new URL(file, MIGRATIONS), "utf8");
    for (const statement of statements(sql)) {
      await db.prepare(statement).run();
    }
  }
}
