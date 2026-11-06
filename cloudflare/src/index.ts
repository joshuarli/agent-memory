import { authorize, identityOf, parseReaders, type AccessIdentity, type Principal } from "./auth.js";
import { failure } from "./errors.js";
import { serverTiming, trace } from "./observe.js";
import { markInvocation, reportError } from "./observe-channel.js";
import { handleGenerations } from "./routes/generations.js";
import { handleHtml } from "./routes/html.js";
import { handleLearning } from "./routes/learning.js";
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
    const timing = { d1: 0, r2: 0 };
    const authStarted = performance.now();
    let identity: AccessIdentity | null;
    let principal: Principal | null;
    try {
      identity = await identityOf(context, request, {
        teamDomain: env.ACCESS_TEAM_DOMAIN,
        audience: env.ACCESS_AUD,
      });
      principal = await trace(context, "auth", {}, async () =>
        authorize(env.DB, identity, parseReaders(env.READERS)));
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
    const url = new URL(request.url);
    const { pathname } = url;
    try {
      const status = await handleStatus(request, env, principal, pathname, timing);
      if (status !== null) return status;
      const sync = await handleSync(request, env, principal, pathname, timing);
      if (sync !== null) return sync;
      const generations = await handleGenerations(request, env, principal, pathname, url, timing);
      if (generations !== null) return generations;
      const snapshots = await handleSnapshots(request, env, principal, pathname, url, timing);
      if (snapshots !== null) return snapshots;
      const tombstones = await handleTombstones(request, env, principal, pathname, url, timing);
      if (tombstones !== null) return tombstones;
      const learning = await handleLearning(request, env, principal, pathname, url, timing);
      if (learning !== null) return learning;
      const memories = await handleMemories(request, env, principal, pathname, url, timing);
      if (memories !== null) return memories;
      const page = await handleHtml(request, env, principal, pathname, url, timing);
      if (page !== null) return page;
    } catch (error) {
      reportError("request.route", error);
      return failure(503, "unavailable", "sync is unavailable", { "Server-Timing": serverTiming(timing) });
    }
    return failure(404, "not_found", "route not found", { "Server-Timing": serverTiming(timing) });
  },
} satisfies ExportedHandler<Env>;
