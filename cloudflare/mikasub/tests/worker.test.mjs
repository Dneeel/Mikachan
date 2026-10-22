import assert from "node:assert/strict";
import { test } from "node:test";
import worker, { testHooks } from "../src/index.mjs";

test("admin authorization is separate from the public route", async () => {
  const env = { MIKASUB_ADMIN_TOKEN: "local-secret" };
  const request = new Request("https://worker.example/api/v1/sources");
  const response = await worker.fetch(request, env);
  assert.equal(response.status, 401);
  assert.equal((await response.json()).error, "unauthorized");
  assert.equal(response.headers.get("access-control-allow-origin"), null);
});

test("public token has strict shape and never reaches D1 for malformed input", async () => {
  const response = await worker.fetch(new Request("https://worker.example/sub/short"), {
    VPN_DB: { prepare: () => { throw Error("DB must not be used"); } },
  });
  assert.equal(response.status, 404);
});

test("source URL validator and bounded request reader", async () => {
  assert.equal(testHooks.validSource("https://public.example/path?key=abc"), true);
  for (const value of ["http://127.0.0.1/x", "http://localhost/x",
    "https://person:secret@public.example/x", "https://public.example:8080/x"])
    assert.equal(testHooks.validSource(value), false, value);
  assert.equal(testHooks.equal("same", "same"), true);
  assert.equal(testHooks.equal("same", "diff"), false);
  await assert.rejects(testHooks.body(new Request("https://worker.example/", {
    method: "POST", body: "a".repeat(200),
  }), 100), /too large/);
});
