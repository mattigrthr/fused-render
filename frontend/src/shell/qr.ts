// A QR encoder, because the point of publishing is that the app is now on a
// phone — and the shortest path from "here is your URL" to a phone holding it
// is a camera, not retyping a hostname with a thumb.
//
// Small on purpose. Byte mode, error level M, versions 1–6 (up to 106 bytes),
// which covers every address a Pages project or a custom domain produces with
// room to spare; anything longer returns null and the page shows the link
// alone. Capping at version 6 is what lets this file skip the version-info
// block entirely (only 7 and up carry one), and error level M is what makes it
// scan off a laptop screen at an angle in a room with overhead lights.
//
// No dependency: a QR encoder is a few hundred lines of finite-field
// arithmetic that will never change again, and it is worth less than the
// supply-chain surface of a package that does the same thing.
//
// Structure follows the spec's own order — encode, error-correct, interleave,
// place, mask — so each step reads against ISO/IEC 18004 rather than against
// itself.

/** Error level M's own two bits, and the level every matrix here uses. */
const EC_LEVEL_BITS = 0b00;

/** Per version (1–6): total data codewords, EC codewords per block, block count.
 *  Every version through 6 is a single group of equal-sized blocks, which is
 *  the other reason the cap pays for itself. */
const VERSIONS: ReadonlyArray<{ data: number; ecPerBlock: number; blocks: number }> = [
  { data: 16, ecPerBlock: 10, blocks: 1 },
  { data: 28, ecPerBlock: 16, blocks: 1 },
  { data: 44, ecPerBlock: 26, blocks: 1 },
  { data: 64, ecPerBlock: 18, blocks: 2 },
  { data: 86, ecPerBlock: 24, blocks: 2 },
  { data: 108, ecPerBlock: 16, blocks: 4 },
];

/** Alignment-pattern centre coordinates per version (1 has none). */
const ALIGN: ReadonlyArray<readonly number[]> = [
  [],
  [6, 18],
  [6, 22],
  [6, 26],
  [6, 30],
  [6, 34],
];

/** The largest byte payload any version here can hold. */
export const QR_MAX_BYTES = VERSIONS[VERSIONS.length - 1].data - 2;

// ---- GF(256), the field the error correction lives in ------------------------

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
{
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d; // the QR primitive polynomial
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
}

function gfMul(a: number, b: number): number {
  if (a === 0 || b === 0) return 0;
  return EXP[LOG[a] + LOG[b]];
}

/** The generator polynomial for `degree` error-correction codewords. */
function generator(degree: number): Uint8Array {
  let poly = new Uint8Array([1]);
  for (let d = 0; d < degree; d++) {
    const next = new Uint8Array(poly.length + 1);
    for (let i = 0; i < poly.length; i++) {
      next[i] ^= poly[i];
      next[i + 1] ^= gfMul(poly[i], EXP[d]);
    }
    poly = next;
  }
  return poly;
}

/** The `degree` EC codewords for one block — the remainder of the data
 *  polynomial divided by the generator. */
export function ecCodewords(data: Uint8Array, degree: number): Uint8Array {
  const gen = generator(degree);
  const rem = new Uint8Array(degree);
  for (const byte of data) {
    const factor = byte ^ rem[0];
    rem.copyWithin(0, 1);
    rem[degree - 1] = 0;
    for (let i = 0; i < degree; i++) rem[i] ^= gfMul(gen[i + 1], factor);
  }
  return rem;
}

// ---- encode ------------------------------------------------------------------

/** The smallest version that holds `length` bytes, or null past version 6. */
function pickVersion(length: number): number | null {
  for (let v = 1; v <= VERSIONS.length; v++) {
    // 4 mode bits + 8 count bits = 12, so the header costs two codewords.
    if (length <= VERSIONS[v - 1].data - 2) return v;
  }
  return null;
}

/** Data codewords for `bytes` at `version`: header, payload, terminator, pad. */
function dataCodewords(bytes: Uint8Array, version: number): Uint8Array {
  const capacity = VERSIONS[version - 1].data;
  const out = new Uint8Array(capacity);
  let bit = 0;
  const push = (value: number, width: number) => {
    for (let i = width - 1; i >= 0; i--) {
      if ((value >>> i) & 1) out[bit >> 3] |= 0x80 >> (bit & 7);
      bit++;
    }
  };
  push(0b0100, 4); // byte mode
  push(bytes.length, 8); // an 8-bit count is correct through version 9
  for (const b of bytes) push(b, 8);
  push(0, Math.min(4, capacity * 8 - bit)); // terminator
  bit = (bit + 7) & ~7; // to the byte boundary
  // The two alternating pad codewords the spec names, for the rest.
  for (let i = bit >> 3, alt = 0; i < capacity; i++, alt ^= 1) out[i] = alt ? 0x11 : 0xec;
  return out;
}

/** Split into blocks, error-correct each, and interleave — the order the
 *  matrix is filled in, and the reason a scratch on the print costs one
 *  codeword in each block rather than a whole block. */
export function codewordStream(bytes: Uint8Array, version: number): Uint8Array {
  const { ecPerBlock, blocks } = VERSIONS[version - 1];
  const data = dataCodewords(bytes, version);
  const perBlock = data.length / blocks;
  const dataBlocks: Uint8Array[] = [];
  const ecBlocks: Uint8Array[] = [];
  for (let b = 0; b < blocks; b++) {
    const block = data.subarray(b * perBlock, (b + 1) * perBlock);
    dataBlocks.push(block);
    ecBlocks.push(ecCodewords(block, ecPerBlock));
  }
  const out = new Uint8Array(data.length + ecPerBlock * blocks);
  let i = 0;
  for (let c = 0; c < perBlock; c++) for (const block of dataBlocks) out[i++] = block[c];
  for (let c = 0; c < ecPerBlock; c++) for (const block of ecBlocks) out[i++] = block[c];
  return out;
}

// ---- the matrix --------------------------------------------------------------

type Grid = { size: number; dark: boolean[][]; fixed: boolean[][] };

function blank(version: number): Grid {
  const size = 17 + 4 * version;
  const row = () => new Array<boolean>(size).fill(false);
  return {
    size,
    dark: Array.from({ length: size }, row),
    fixed: Array.from({ length: size }, row),
  };
}

function set(g: Grid, r: number, c: number, dark: boolean) {
  g.dark[r][c] = dark;
  g.fixed[r][c] = true;
}

function finder(g: Grid, top: number, left: number) {
  // The 7×7 eye plus its one-module separator, clipped to the matrix.
  for (let dr = -1; dr <= 7; dr++) {
    for (let dc = -1; dc <= 7; dc++) {
      const r = top + dr;
      const c = left + dc;
      if (r < 0 || r >= g.size || c < 0 || c >= g.size) continue;
      const ring = Math.max(Math.abs(dr - 3), Math.abs(dc - 3));
      set(g, r, c, ring !== 2 && ring <= 3);
    }
  }
}

function functionPatterns(g: Grid, version: number) {
  finder(g, 0, 0);
  finder(g, 0, g.size - 7);
  finder(g, g.size - 7, 0);

  for (let i = 8; i < g.size - 8; i++) {
    const dark = i % 2 === 0;
    set(g, 6, i, dark);
    set(g, i, 6, dark);
  }

  const centres = ALIGN[version - 1];
  for (const r of centres) {
    for (const c of centres) {
      // Not where a finder already is: the three corners are spoken for.
      if ((r === 6 && c === 6) || (r === 6 && c === g.size - 7) || (r === g.size - 7 && c === 6))
        continue;
      for (let dr = -2; dr <= 2; dr++)
        for (let dc = -2; dc <= 2; dc++)
          set(g, r + dr, c + dc, Math.max(Math.abs(dr), Math.abs(dc)) !== 1);
    }
  }

  // Reserve the format strips, and the one module that is always dark.
  for (let i = 0; i < 9; i++) {
    if (!g.fixed[8][i]) set(g, 8, i, false);
    if (!g.fixed[i][8]) set(g, i, 8, false);
  }
  for (let i = 0; i < 8; i++) {
    set(g, 8, g.size - 1 - i, false);
    set(g, g.size - 1 - i, 8, false);
  }
  set(g, g.size - 8, 8, true);
}

/** Walk the data modules in placement order: two-module columns, right to
 *  left, alternating upward and downward, skipping the timing column. */
function* dataCells(g: Grid): Generator<[number, number]> {
  let upward = true;
  for (let right = g.size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5; // column 6 is timing, never data
    for (let i = 0; i < g.size; i++) {
      const r = upward ? g.size - 1 - i : i;
      for (const c of [right, right - 1]) if (!g.fixed[r][c]) yield [r, c];
    }
    upward = !upward;
  }
}

function maskAt(mask: number, r: number, c: number): boolean {
  switch (mask) {
    case 0:
      return (r + c) % 2 === 0;
    case 1:
      return r % 2 === 0;
    case 2:
      return c % 3 === 0;
    case 3:
      return (r + c) % 3 === 0;
    case 4:
      return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
    case 5:
      return ((r * c) % 2) + ((r * c) % 3) === 0;
    case 6:
      return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
    default:
      return ((((r + c) % 2) + ((r * c) % 3)) % 2) === 0;
  }
}

/** The 15-bit BCH format string for error level M and one mask. */
export function formatBits(mask: number): number {
  const data = (EC_LEVEL_BITS << 3) | mask;
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  return (((data << 10) | rem) ^ 0x5412) & 0x7fff;
}

function drawFormat(g: Grid, mask: number) {
  const bits = formatBits(mask);
  const bit = (i: number) => ((bits >>> i) & 1) === 1;
  // The copy that hugs the top-left eye, split around the timing lines.
  for (let i = 0; i <= 5; i++) g.dark[8][i] = bit(i);
  g.dark[8][7] = bit(6);
  g.dark[8][8] = bit(7);
  g.dark[7][8] = bit(8);
  for (let i = 9; i <= 14; i++) g.dark[14 - i][8] = bit(i);
  // The second copy, so a damaged corner does not cost the whole symbol.
  for (let i = 0; i <= 7; i++) g.dark[g.size - 1 - i][8] = bit(i);
  for (let i = 8; i <= 14; i++) g.dark[8][g.size - 15 + i] = bit(i);
}

/** The spec's four penalty rules — runs, blocks, finder look-alikes, and the
 *  dark/light balance. Lower is better; the chosen mask is the lowest. */
function penalty(g: Grid): number {
  const n = g.size;
  let score = 0;

  const runScore = (line: boolean[]) => {
    let total = 0;
    let run = 1;
    for (let i = 1; i < n; i++) {
      if (line[i] === line[i - 1]) {
        run++;
        if (run === 5) total += 3;
        else if (run > 5) total += 1;
      } else run = 1;
    }
    return total;
  };
  for (let r = 0; r < n; r++) score += runScore(g.dark[r]);
  for (let c = 0; c < n; c++) score += runScore(g.dark.map((row) => row[c]));

  for (let r = 0; r < n - 1; r++)
    for (let c = 0; c < n - 1; c++) {
      const v = g.dark[r][c];
      if (v === g.dark[r][c + 1] && v === g.dark[r + 1][c] && v === g.dark[r + 1][c + 1])
        score += 3;
    }

  // 1:1:3:1:1 with four light modules on one side, either orientation.
  const PATTERN = [true, false, true, true, true, false, true];
  const looksLikeFinder = (line: boolean[], i: number) => {
    for (let k = 0; k < 7; k++) if (line[i + k] !== PATTERN[k]) return false;
    const before = line.slice(Math.max(0, i - 4), i);
    const after = line.slice(i + 7, i + 11);
    return (
      (before.length === 4 && before.every((v) => !v)) ||
      (after.length === 4 && after.every((v) => !v))
    );
  };
  const lines: boolean[][] = [];
  for (let r = 0; r < n; r++) lines.push(g.dark[r]);
  for (let c = 0; c < n; c++) lines.push(g.dark.map((row) => row[c]));
  for (const line of lines)
    for (let i = 0; i + 7 <= n; i++) if (looksLikeFinder(line, i)) score += 40;

  let dark = 0;
  for (const row of g.dark) for (const v of row) if (v) dark++;
  const percent = (dark * 100) / (n * n);
  score += Math.floor(Math.abs(percent - 50) / 5) * 10;

  return score;
}

/**
 * The QR matrix for `text` as rows of booleans (true = dark), or null when the
 * text is longer than {@link QR_MAX_BYTES} once UTF-8 encoded.
 *
 * No quiet zone: the caller draws it, because how much white surrounds the
 * symbol is a layout decision and four modules of margin baked into the data
 * would be four modules the caller cannot style.
 */
export function qrMatrix(text: string): boolean[][] | null {
  const bytes = new TextEncoder().encode(text);
  const version = pickVersion(bytes.length);
  if (version === null) return null;

  const stream = codewordStream(bytes, version);
  const g = blank(version);
  functionPatterns(g, version);

  let i = 0;
  for (const [r, c] of dataCells(g)) {
    // Past the codewords lie the remainder bits, which are light.
    g.dark[r][c] = i < stream.length * 8 && ((stream[i >> 3] >>> (7 - (i & 7))) & 1) === 1;
    i++;
  }

  // Every mask is tried because the spec says to: the winner is the one whose
  // pattern is least likely to be misread as structure.
  let best: boolean[][] | null = null;
  let bestScore = Infinity;
  for (let mask = 0; mask < 8; mask++) {
    const trial = { ...g, dark: g.dark.map((row) => row.slice()) };
    for (let r = 0; r < g.size; r++)
      for (let c = 0; c < g.size; c++)
        if (!g.fixed[r][c] && maskAt(mask, r, c)) trial.dark[r][c] = !trial.dark[r][c];
    drawFormat(trial, mask);
    const score = penalty(trial);
    if (score < bestScore) {
      bestScore = score;
      best = trial.dark;
    }
  }
  return best;
}

/**
 * The matrix as one SVG path — every dark module a 1×1 rect in a viewBox of
 * `size + 2 * quiet`. One path rather than N rects so a 45×45 symbol is one
 * DOM node, and integer coordinates so no module lands on a half pixel.
 */
export function qrPath(matrix: boolean[][], quiet = 2): string {
  const parts: string[] = [];
  for (let r = 0; r < matrix.length; r++)
    for (let c = 0; c < matrix[r].length; c++)
      if (matrix[r][c]) parts.push(`M${c + quiet} ${r + quiet}h1v1h-1z`);
  return parts.join("");
}
