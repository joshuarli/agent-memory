import assert from "node:assert/strict";
import { generateKeyPairSync, sign as nodeSign } from "node:crypto";
import test from "node:test";

import { keysFromJwks, verifyAssertion } from "../src/assertion.ts";

const CONFIG = { teamDomain: "team.example.test", audience: "aud-tag" };
const NOW = 1_800_000_000;
const CLIENT_ID = "0123456789abcdef0123456789abcdef.access";

function encode(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function token(claims, privateKey, header = { alg: "RS256", kid: "key-1", typ: "JWT" }) {
  const signingInput = `${encode(header)}.${encode(claims)}`;
  const signature = nodeSign("RSA-SHA256", Buffer.from(signingInput), privateKey).toString("base64url");
  return `${signingInput}.${signature}`;
}

function claims(overrides = {}) {
  return {
    type: "app",
    aud: [CONFIG.audience],
    exp: NOW + 3600,
    iat: NOW - 10,
    iss: `https://${CONFIG.teamDomain}`,
    sub: "",
    common_name: CLIENT_ID,
    ...overrides,
  };
}

function material() {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  return { privateKey, jwk: publicKey.export({ format: "jwk" }) };
}

function loaderFor(kid, jwk) {
  return async (domain, requested) => (domain === CONFIG.teamDomain && requested === kid ? jwk : null);
}

function keyset(materialForKeyset) {
  return { keys: [{ ...materialForKeyset.jwk, kid: "key-1", alg: "RS256", use: "sig" }] };
}

test("a service-token assertion verifies and yields its client id", async () => {
  const { privateKey, jwk } = material();
  const assertion = token(claims(), privateKey);
  assert.deepEqual(
    await verifyAssertion(assertion, CONFIG, loaderFor("key-1", jwk), NOW),
    { clientId: CLIENT_ID },
  );
  assert.deepEqual(
    await verifyAssertion(token(claims({ aud: CONFIG.audience }), privateKey), CONFIG, loaderFor("key-1", jwk), NOW),
    { clientId: CLIENT_ID },
  );
  assert.deepEqual(
    await verifyAssertion(token(claims({ nbf: NOW + 30 }), privateKey), CONFIG, loaderFor("key-1", jwk), NOW),
    { clientId: CLIENT_ID },
  );
});

test("a forged signature, unknown key, or non-RS256 algorithm is refused", async () => {
  const { privateKey } = material();
  const { jwk } = material();
  const forged = token(claims(), material().privateKey);
  assert.equal(await verifyAssertion(forged, CONFIG, loaderFor("key-1", jwk), NOW), null);
  assert.equal(await verifyAssertion(token(claims(), privateKey), CONFIG, async () => null, NOW), null);
  assert.equal(
    await verifyAssertion(token(claims(), privateKey, { alg: "none", kid: "key-1" }), CONFIG, loaderFor("key-1", jwk), NOW),
    null,
  );
  assert.equal(
    await verifyAssertion(token(claims(), privateKey, { alg: "HS256", kid: "key-1" }), CONFIG, loaderFor("key-1", jwk), NOW),
    null,
  );
});

test("claims are pinned to the issuer, audience, expiry, and service-token shape", async () => {
  const { privateKey, jwk } = material();
  const load = loaderFor("key-1", jwk);
  const rejected = [
    claims({ iss: "https://other.example.test" }),
    claims({ aud: ["other-audience"] }),
    claims({ exp: NOW }),
    claims({ exp: NOW - 1 }),
    claims({ nbf: NOW + 120 }),
    claims({ sub: "user-id" }),
    claims({ type: "org" }),
    claims({ common_name: "" }),
    claims({ common_name: "not-a-client-id" }),
    claims({ common_name: undefined }),
    claims({ aud: "aud-tag-extra" }),
  ];
  for (const value of rejected) {
    assert.equal(
      await verifyAssertion(token(value, privateKey), CONFIG, load, NOW),
      null,
      JSON.stringify(value),
    );
  }
});

test("malformed assertions, missing config, and invalid keysets are refused", async () => {
  const { privateKey, jwk } = material();
  const load = loaderFor("key-1", jwk);
  for (const value of ["", "a.b", "a.b.c.d", "!!!.???.###", `${encode({ alg: "RS256" })}.e30.sig`]) {
    assert.equal(await verifyAssertion(value, CONFIG, load, NOW), null);
  }
  assert.equal(await verifyAssertion(token(claims(), privateKey), { teamDomain: "", audience: "aud-tag" }, load, NOW), null);
  assert.equal(await verifyAssertion(token(claims(), privateKey), { ...CONFIG, audience: "" }, load, NOW), null);
  assert.equal(await verifyAssertion(token(claims(), privateKey), { ...CONFIG, teamDomain: "https://team.example.test" }, load, NOW), null);
});

test("the keyset parser accepts RSA keys and rejects ambiguous or malformed keys", () => {
  const { jwk } = material();
  const parsed = keysFromJwks(keyset({ jwk }));
  assert.equal(parsed.get("key-1").n, jwk.n);
  assert.equal(parsed.get("key-1").e, jwk.e);
  assert.equal(parsed.get("key-1").alg, "RS256");
  for (const value of [
    null,
    {},
    { keys: [] },
    { keys: [{ kty: "EC", kid: "key-1", n: "x", e: "AQAB" }] },
    { keys: [{ kty: "RSA", kid: "key-1", e: "AQAB" }] },
    { keys: [{ kty: "RSA", kid: "", n: "x", e: "AQAB" }] },
    { keys: [{ kty: "RSA", kid: "key-1", n: "x", e: "AQAB" }, { kty: "RSA", kid: "key-1", n: "y", e: "AQAB" }] },
  ]) {
    assert.throws(() => keysFromJwks(value));
  }
});
