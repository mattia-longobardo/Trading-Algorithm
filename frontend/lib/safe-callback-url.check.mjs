// Check riproducibile della sanitizzazione di ?callbackUrl (open redirect).
// Zero dipendenze, nessun test runner: `node lib/safe-callback-url.check.mjs`.
import assert from "node:assert/strict";

import { safeCallbackUrl } from "./safe-callback-url.mjs";

/** @type {[string | string[] | undefined, string][]} coppie input → output atteso */
const cases = [
  // Assoluti e schemi
  ["https://evil.example", "/"],
  ["http://callback.invalid/ok", "/"],
  ["javascript:alert(1)", "/"],
  // Protocol-relative e backslash
  ["//evil.example", "/"],
  ["/\\evil.example", "/"],
  ["\\\\evil.example", "/"],
  // Caratteri di controllo rimossi dal parser
  ["/\t/evil.example", "/"],
  ["/\n/evil.example", "/"],
  ["/\r/evil.example", "/"],
  // Traversal che risale oltre la radice e ricrea un "//host"
  ["/..//evil.example", "/"],
  ["/.//evil.example", "/"],
  ["/../..//evil.example", "/"],
  ["/..///evil.example", "/"],
  ["/../\\evil.example", "/"],
  // Encoding: %2F resta encoded, non diventa uno slash strutturale
  ["/%2F%2Fevil.example", "/%2F%2Fevil.example"],
  ["%2F%2Fevil.example", "/"],
  // Valori assenti o vuoti
  [undefined, "/"],
  ["", "/"],
  // Parametro ripetuto: Next passa un array, non deve sollevare
  [["/a", "/b"], "/"],
  [[], "/"],
  // Path interni legittimi: devono passare intatti
  ["/", "/"],
  ["/portfolio", "/portfolio"],
  ["/trades?tab=open#x", "/trades?tab=open#x"],
];

for (const [input, expected] of cases) {
  const actual = safeCallbackUrl(input);
  assert.equal(actual, expected, `safeCallbackUrl(${JSON.stringify(input)}) = ${JSON.stringify(actual)}, atteso ${JSON.stringify(expected)}`);
  // Invariante generale: mai un URL che porta fuori dominio.
  assert.ok(actual.startsWith("/") && !actual.startsWith("//"), `output non relativo per ${JSON.stringify(input)}: ${JSON.stringify(actual)}`);
}

console.log(`safe-callback-url: ${cases.length} casi ok`);
