const captured = [];

export default {
  async tail(events) {
    for (const event of events) captured.push(event);
  },
  async fetch(request) {
    const { pathname } = new URL(request.url);
    if (pathname === "/__tail/events") return Response.json(captured);
    if (pathname === "/__tail/health") return new Response("ok");
    return new Response("not found", { status: 404 });
  },
};
