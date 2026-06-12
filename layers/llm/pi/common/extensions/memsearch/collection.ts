/**
 * Collection-name derivation — the contract shared between Pi and Claude Code.
 *
 * Kept in its own module (Node built-ins only, NO third-party imports) so it can be
 * unit-tested under bare `node --experimental-strip-types` without pulling in the Pi
 * runtime (typebox / @earendil-works/*). index.ts imports deriveCollection from here.
 *
 * Output MUST equal memsearch/scripts/derive-collection.sh byte-for-byte:
 *   ms_<sanitized basename>_<first-8-hex sha256(absolute path)>
 * so a given repo path maps to the same Milvus collection regardless of which agent
 * (Pi or Claude) writes to it.
 */
import * as path from "node:path";
import * as crypto from "node:crypto";

export function deriveCollection(projectDir: string): string {
  const abs = path.resolve(projectDir);
  const sanitized = path
    .basename(abs)
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 40);
  const hash = crypto.createHash("sha256").update(abs).digest("hex").slice(0, 8);
  return `ms_${sanitized}_${hash}`;
}
