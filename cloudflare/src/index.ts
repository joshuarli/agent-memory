import { meterD1 } from "./db.js";
import { authorize, identityOf, parseReaders, type AccessIdentity, type Principal } from "./auth.js";
import { failure } from "./errors.js";
import { serverTiming, trace } from "./observe.js";
import { markInvocation, markOperation, operationFor, reportError, type RowMeter } from "./observe-channel.js";
import { handleGenerations } from "./routes/generations.js";
import { handleHtml } from "./routes/html.js";
import { handleLearning } from "./routes/learning.js";
import { handleMcp } from "./routes/mcp.js";
import { handleMemories } from "./routes/memories.js";
import { handleSnapshots } from "./routes/snapshots.js";
import { handleStatus } from "./routes/status.js";
import { handleSync } from "./routes/sync.js";
import { handleTombstones } from "./routes/tombstones.js";

export interface Env {
  readonly READERS?: string;
  readonly ACCESS_TEAM_DOMAIN: string;
  readonly ACCESS_AUD: string;
  readonly DB: D1Database;
  readonly BUCKET: R2Bucket;
}

export default {
  async fetch(request: Request, env: Env, context: ExecutionContext): Promise<Response> {
    markInvocation(context);
    const rows: RowMeter = { read: 0, written: 0, statements: 0 };
    const url = new URL(request.url);
    const operation = operationFor(url.pathname, request.method);
    const metered: Env = { ...env, DB: meterD1(env.DB, rows) };
    try {
      const timing = { d1: 0, r2: 0 };
      const authStarted = performance.now();
      let identity: AccessIdentity | null;
      let principal: Principal | null;
      try {
        identity = await identityOf(context, request, {
          teamDomain: metered.ACCESS_TEAM_DOMAIN,
          audience: metered.ACCESS_AUD,
        });
        principal = await trace(context, "auth", {}, async () =>
          authorize(metered.DB, identity, parseReaders(metered.READERS)));
      } catch (error) {
        reportError("request.auth", error);
        timing.d1 += performance.now() - authStarted;
        return failure(503, "unavailable", "authentication is unavailable", {
          "Server-Timing": serverTiming(timing),
        });
      }
      if (identity?.service_token_id !== undefined) {
        timing.d1 += performance.now() - authStarted;
      }
      if (principal === null) {
        return failure(403, "forbidden", "an authorized Access identity is required", {
          "Server-Timing": serverTiming(timing),
        });
      }
      const { pathname } = url;
      try {
        if (pathname === "/mcp") return await handleMcp(request, metered, principal);
        const status = await handleStatus(request, metered, principal, pathname, timing);
        if (status !== null) return status;
        const sync = await handleSync(request, metered, principal, pathname, timing);
        if (sync !== null) return sync;
        const generations = await handleGenerations(request, metered, principal, pathname, url, timing);
        if (generations !== null) return generations;
        const snapshots = await handleSnapshots(request, metered, principal, pathname, url, timing);
        if (snapshots !== null) return snapshots;
        const tombstones = await handleTombstones(request, metered, principal, pathname, url, timing);
        if (tombstones !== null) return tombstones;
        const learning = await handleLearning(request, metered, principal, pathname, url, timing);
        if (learning !== null) return learning;
        const memories = await handleMemories(request, metered, principal, pathname, url, timing);
        if (memories !== null) return memories;
        const page = await handleHtml(request, metered, principal, pathname, url, timing);
        if (page !== null) return page;
      } catch (error) {
        reportError("request.route", error);
        return failure(503, "unavailable", "sync is unavailable", { "Server-Timing": serverTiming(timing) });
      }
      return failure(404, "not_found", "route not found", { "Server-Timing": serverTiming(timing) });
    } finally {
      if (operation !== null) markOperation(context, operation, rows);
    }
  },
} satisfies ExportedHandler<Env>;
