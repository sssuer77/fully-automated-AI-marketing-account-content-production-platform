// 构建产物体积门禁（T4.1 验收：产物 < 3 MB）。
// 为什么要有：体积是唯一会悄悄劣化、且没人会主动去量的指标。
import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const LIMIT_BYTES = 3 * 1024 * 1024;
const DIST = new URL("../dist/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function walk(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });
}

let total = 0;
for (const file of walk(DIST)) total += statSync(file).size;
const mb = (total / 1024 / 1024).toFixed(2);
if (total > LIMIT_BYTES) {
  console.error(`[size] dist 共 ${mb} MB，超过门禁 3.00 MB`);
  process.exit(1);
}
console.log(`[size] dist 共 ${mb} MB（门禁 3.00 MB）OK`);
