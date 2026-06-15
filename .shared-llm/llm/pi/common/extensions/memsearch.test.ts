// Unit test for memsearch's collection derivation (pure function — no Pi runtime).
// Zero dependencies; runs on Node 22.6+ via native type-stripping.
//   task test:memsearch
//   # or: node --experimental-strip-types layers/llm/pi/common/extensions/memsearch.test.ts
//
// The derivation MUST match memsearch/scripts/derive-collection.sh byte-for-byte so
// Pi and Claude Code converge on the same Milvus collection per repo. The example case
// is the golden value: a fixed project path must resolve to exactly one collection name,
// so Pi and Claude agree on where a given repo's memory lives.
import { deriveCollection } from "./memsearch/collection.ts";

let pass = 0;
const fails: string[] = [];

// 1. Example golden value — exact match (path + hash).
{
  const got = deriveCollection("/home/user/code/my-project");
  const want = "ms_my_project_a26ceb5d";
  if (got === want) pass++;
  else fails.push(`  ✗ example golden: expected ${want}, got ${got}`);
}

// 2. Sanitization rules: basename lowercased, non-alnum → _, collapsed, trimmed, ≤40.
//    (Hash differs per path; assert only the sanitized prefix.)
const prefixCases: Array<[string, string]> = [
  ["/home/user/My-App", "ms_my_app_"],
  ["/x/Foo..Bar__Baz", "ms_foo_bar_baz_"],
  ["/x/--leading-trailing--", "ms_leading_trailing_"],
  ["/x/UPPER", "ms_upper_"],
];
for (const [p, prefix] of prefixCases) {
  const got = deriveCollection(p);
  if (got.startsWith(prefix) && /_[0-9a-f]{8}$/.test(got)) pass++;
  else fails.push(`  ✗ sanitize ${p}: expected prefix ${prefix} + 8-hex, got ${got}`);
}

// 3. Determinism: same path → same collection every time.
{
  const a = deriveCollection("/home/user/code/my-project");
  const b = deriveCollection("/home/user/code/my-project/"); // trailing slash normalizes
  if (a === b) pass++;
  else fails.push(`  ✗ determinism: ${a} !== ${b}`);
}

const total = 1 + prefixCases.length + 1;
console.log(`memsearch deriveCollection: ${pass}/${total} passed`);
if (fails.length) {
  console.log("FAILURES:");
  console.log(fails.join("\n"));
  process.exit(1);
}
console.log("ALL PASS ✓");
