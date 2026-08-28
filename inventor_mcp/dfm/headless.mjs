/*
 * Run the OnlyCat DFM analyser on an STL, headlessly, and print the same JSON
 * the browser tool exports.
 *
 * The point of this file is what it does *not* contain. Every threshold, every
 * rule and every word of every finding comes from the DFM tool's own modules,
 * imported from a checkout of that repository; nothing here re-implements or
 * re-states any of it. A duplicated threshold is a threshold that will drift,
 * and then the loop in `loop.py` would be optimising against a copy of the
 * rules rather than the rules.
 *
 * That is possible because the DFM tool's analysis is pure: `analyseMesh` and
 * `runDFM` take every input as an argument and touch no DOM. Its own unit tests
 * import those modules straight into Node, which is the proof this works.
 *
 * Usage:
 *   node headless.mjs --stl part.stl [--settings s.json] [--out report.json]
 *                     [--dfm-root DIR] [--gate x,y,z] [--pull-axis +z]
 *   node headless.mjs --before old.json --after new.json [--out diff.json]
 *
 * The DFM checkout is found from --dfm-root, then $DFM_ROOT, then
 * $INVENTOR_MCP_DFM_ROOT. Settings default to the tool's own DEFAULT_SETTINGS,
 * so an omitted field means "whatever the tool would have used".
 *
 * STL only. The tool reads STEP too, but through a 6 MB OpenCascade WASM
 * module fetched from a CDN, and Inventor writes STL perfectly well.
 */

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(2);
}

/* ── arguments ─────────────────────────────────────────────────────────── */

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (!arg.startsWith('--')) fail(`Unexpected argument ${arg}`);
    const key = arg.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      out[key] = true;
    } else {
      out[key] = next;
      i++;
    }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const comparing = Boolean(args.before || args.after);
if (comparing) {
  if (!args.before || !args.after) fail('comparing needs both --before and --after');
} else if (!args.stl) {
  fail('--stl is required (or --before and --after to compare two reports)');
}

const root = args['dfm-root'] || process.env.DFM_ROOT || process.env.INVENTOR_MCP_DFM_ROOT;
if (!root) {
  fail('Where the DFM tool is checked out is not set. Pass --dfm-root DIR, or set '
    + 'DFM_ROOT / INVENTOR_MCP_DFM_ROOT to a checkout of the OnlyCat DFM repository.');
}
if (!existsSync(path.join(root, 'src', 'rules', 'engine.js'))) {
  fail(`${root} does not look like a DFM checkout: src/rules/engine.js is not there.`);
}

/* Dynamic import, because the location is only known at run time. */
const mod = async (rel) => import(pathToFileURL(path.join(root, rel)).href);

/*
 * Comparing two runs is the tool's own `compareRuns`, not a diff written here.
 * It knows which way is better for each measurement, and it declines to mislead:
 * a score that moved because the material changed, or because a different set of
 * checks ran, comes back with that stated above the diff. A hand-rolled
 * comparison would report the five points and not the reason.
 */
if (comparing) {
  const { compareRuns } = await mod('src/rules/compare.js');
  const read = (where) => JSON.parse(readFileSync(where, 'utf8'));
  const diff = compareRuns(read(args.before), read(args.after));
  if (diff === null) fail('One of those records could not be read as a DFM report.');
  diff.source = {
    by: 'inventor-mcp headless bridge',
    before: path.resolve(args.before),
    after: path.resolve(args.after),
  };
  const rendered = JSON.stringify(diff, null, 2);
  if (args.out) writeFileSync(args.out, rendered);
  else process.stdout.write(rendered);
  process.exit(0);
}

const { parseSTL } = await mod('src/geometry/stl.js');
const { validateGeometry } = await mod('src/geometry/validate.js');
const { analyseMesh } = await mod('src/analysis/mesh.js');
const { runDFM } = await mod('src/rules/engine.js');
const { estimateShot } = await mod('src/analysis/shot.js');
const { buildExportJSON } = await mod('src/export/json.js');
const { MATERIALS } = await mod('src/core/materials.js');
const { effectiveMinDraft } = await mod('src/core/finishes.js');
const { DEFAULT_SETTINGS } = await mod('src/app/state.js');

/* ── settings ──────────────────────────────────────────────────────────── */

/* Merged onto the tool's own defaults one level deep, the same way its
   loadSettings does, so an unknown key is a hard error here rather than a
   silently ignored one -- a misspelled `wallThk` that scores as 2.0 mm is
   exactly the kind of quiet wrong answer this bridge must not produce. */
const settings = structuredClone(DEFAULT_SETTINGS);
if (args.settings) {
  const given = JSON.parse(readFileSync(args.settings, 'utf8'));
  for (const [key, value] of Object.entries(given)) {
    if (!(key in DEFAULT_SETTINGS)) fail(`Unknown DFM setting ${JSON.stringify(key)}`);
    const base = DEFAULT_SETTINGS[key];
    if (base && typeof base === 'object' && !Array.isArray(base)) {
      for (const sub of Object.keys(value || {})) {
        if (!(sub in base)) fail(`Unknown DFM setting ${key}.${sub}`);
        settings[key][sub] = value[sub];
      }
    } else {
      settings[key] = value;
    }
  }
}
if (!MATERIALS[settings.material]) {
  fail(`Unknown material ${JSON.stringify(settings.material)}. Known: ${Object.keys(MATERIALS).join(', ')}`);
}

/* ── geometry ──────────────────────────────────────────────────────────── */

const file = readFileSync(args.stl);
const buffer = file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength);
const geom = parseSTL(buffer);
const validation = validateGeometry(geom);

const material = MATERIALS[settings.material];
const pullAxis = args['pull-axis'] || '+z';
const gate = args.gate
  ? args.gate.split(',').map((n) => Number(n.trim()))
  : null;
if (gate && (gate.length !== 3 || gate.some((n) => !Number.isFinite(n)))) {
  fail('--gate wants three numbers, as x,y,z');
}

const analysis = analyseMesh(geom, {
  material,
  finishKey: settings.surfaceFinish,
  moldType: settings.moldType,
  minDraft: effectiveMinDraft(material, settings.surfaceFinish),
  manualWall: settings.wallThk,
  pullAxis,
  gateLocation: gate,
});

/* ── rules ─────────────────────────────────────────────────────────────── */

const input = {
  wallThk: settings.wallThk,
  wallMin: settings.wallMin,
  wallMax: settings.wallMax,
  draftAngle: settings.draftAngle,
  ribThk: settings.ribThk,
  ribH: settings.ribH,
  ribRadius: settings.ribRadius,
  bossOD: settings.bossOD,
  bossWall: settings.bossWall,
  hasUndercut: settings.hasUndercut,
  material: settings.material,
  surfaceFinish: settings.surfaceFinish,
  moldType: settings.moldType,
  fpc: {
    enabled: settings.fpcEnabled,
    thickness: settings.fpcThickness,
    cover: settings.fpcCover,
    anchors: settings.fpcAnchors,
  },
  runChecks: { ...settings.checks },
  mesh: analysis,
};

const result = runDFM(input);

/* Volume only when the validator judged the surface closed: an enclosed volume
   is undefined otherwise, and a shot weight derived from one is invented. This
   mirrors what the app does rather than deciding it again. */
const shot = estimateShot({
  material,
  volume: validation && validation.volume != null ? validation.volume : null,
  projectedArea: analysis.projectedArea,
});

const report = buildExportJSON({
  /*
   * Named after the mesh, not a constant. The browser panel labels a comparison
   * with timestamp-to-the-minute plus session id, so two rounds of the same loop
   * -- which routinely finish inside one minute -- both rendered
   * "10:31 HEADLESS ABS" and the before and after were indistinguishable on
   * screen. The file name is the one thing that always differs.
   */
  sessionId: (args.session || path.basename(args.stl).replace(/\.[^.]+$/, '')).toUpperCase(),
  dfm: { input, result },
  analysis,
  twoShot: null,
  interface: null,
  validation,
  shot,
  settings,
});

/*
 * The material's own limits, straight from the tool's table.
 *
 * The rules state their thresholds as inline literals -- "below ABS minimum
 * (1.2 mm)" -- and the export carries them only inside display strings like
 * "1.2-3.5 mm". A remediation step that parsed those strings, or worse held its
 * own copy of the table, would be computing targets against numbers that can
 * drift away from the ones the check is actually applying. So the numbers come
 * across as numbers, from MATERIALS, and nothing downstream needs a copy.
 */
report.material_limits = {
  key: settings.material,
  name: material.name,
  wall_lo_mm: material.wallLo,
  wall_hi_mm: material.wallHi,
  draft_min_deg: material.draftMin,
  required_draft_deg: effectiveMinDraft(material, settings.surfaceFinish),
  lt_max: material.ltMax,
  shrink_lo_pct: material.shrinkLo,
  shrink_hi_pct: material.shrinkHi,
  warp_risk: material.warpRisk,
  density_g_cm3: material.density,
};

/* Which mesh this is a report about, so a loop cannot mistake a stale report
   for a fresh one. Namespaced to keep it clear this is not the tool's own. */
report.source = {
  by: 'inventor-mcp headless bridge',
  stl: path.resolve(args.stl),
  pull_axis: pullAxis,
  gate: gate,
};

const text = JSON.stringify(report, null, 2);
if (args.out) writeFileSync(args.out, text);
else process.stdout.write(text);
