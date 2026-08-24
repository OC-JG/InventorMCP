/*
 * Write one of the DFM tool's own fixture shapes out as a binary STL.
 *
 *   node tests/dfm_shapes.mjs <dfm-root> <out.stl> hollowFrustum 20 30 3 2
 *
 * The shapes come from the DFM repository's test fixtures because they have
 * known answers -- a 2 mm hollow cylinder measures 2 mm, a 3 degree frustum
 * reads 3.000 degrees. A shape invented here would only prove that this project
 * and the analyser agree about a shape neither of them has checked.
 */
import { writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const [root, out, shape, ...rest] = process.argv.slice(2);
const shapes = await import(
  pathToFileURL(path.join(root, 'test', 'lib', 'shapes.mjs')).href
);
if (typeof shapes[shape] !== 'function') {
  process.stderr.write(`No such fixture shape: ${shape}\n`);
  process.exit(2);
}

const { positions, triCount } = shapes[shape](...rest.map(Number));
const buf = Buffer.alloc(84 + triCount * 50);
buf.write('inventor-mcp dfm target test', 0);
buf.writeUInt32LE(triCount, 80);
let off = 84;
for (let t = 0; t < triCount; t++) {
  off += 12;                                  // the stored normal, recomputed on read
  for (let j = 0; j < 9; j++) { buf.writeFloatLE(positions[t * 9 + j], off); off += 4; }
  off += 2;                                   // attribute byte count
}
writeFileSync(out, buf);
process.stdout.write(`${triCount}\n`);
