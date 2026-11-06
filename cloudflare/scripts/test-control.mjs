export async function handleControl(request, url, stack, body) {
  if (url.pathname === "/__test/d1") {
    if (request.method !== "POST") return Response.json({ error: "method" }, { status: 405 });
    const payload = JSON.parse(body.toString("utf8") || "{}");
    const statement = (await stack.d1()).prepare(payload.sql).bind(...(payload.params ?? []));
    if (payload.mode === "first") return Response.json({ row: await statement.first() });
    if (payload.mode === "run") return Response.json({ meta: await statement.run() });
    return Response.json({ results: (await statement.all()).results });
  }
  if (url.pathname === "/__test/r2") {
    const bucket = await stack.r2();
    const key = url.searchParams.get("key");
    if (request.method === "GET" && key === null) {
      const prefix = url.searchParams.get("prefix");
      const listed = await bucket.list(prefix === null ? {} : { prefix });
      return Response.json({
        keys: listed.objects.map((object) => ({ key: object.key, size: object.size, etag: object.etag })),
        truncated: listed.truncated,
      });
    }
    if (request.method === "GET" && key !== null) {
      const object = await bucket.get(key);
      if (object === null) return Response.json({ error: "missing" }, { status: 404 });
      return new Response(object.body, {
        headers: {
          "content-type": "application/octet-stream",
          "x-test-size": String(object.size),
          "x-test-etag": object.etag ?? "",
        },
      });
    }
    if (request.method === "DELETE" && key !== null) {
      await bucket.delete(key);
      return Response.json({ deleted: true });
    }
    if (request.method === "PUT" && key !== null) {
      await bucket.put(key, body);
      return Response.json({ stored: true, size: body.byteLength });
    }
    return Response.json({ error: "method" }, { status: 405 });
  }
  if (url.pathname === "/__test/tail") {
    return Response.json({ events: await stack.tailEvents() });
  }
  return undefined;
}
