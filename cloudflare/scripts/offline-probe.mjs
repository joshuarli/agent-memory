import { createServer } from "node:http";

const server = createServer((_request, response) => response.end("loopback"));
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
const port = typeof address === "object" && address !== null ? address.port : 0;
const loopback = await fetch(`http://127.0.0.1:${port}/`);
const payload = await loopback.text();
server.close();
if (loopback.status !== 200 || payload !== "loopback") {
  throw new Error("offline probe: loopback request failed");
}
let blocked = false;
try {
  await fetch("https://1.1.1.1", { signal: AbortSignal.timeout(3000) });
} catch {
  blocked = true;
}
if (!blocked) throw new Error("offline probe: a non-loopback request succeeded");
console.log("offline-probe: loopback ok, non-loopback blocked");
