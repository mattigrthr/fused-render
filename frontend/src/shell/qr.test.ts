// The encoder is verified by READING its own symbols back — unmask, walk the
// placement, de-interleave, check the Reed-Solomon syndromes, decode the
// header. A decoder written against the spec rather than against the encoder
// is the only honest test here: asserting a hardcoded matrix would pass
// whatever the encoder happened to produce the day it was written.
import { describe, expect, it } from "bun:test";
import { QR_MAX_BYTES, ecCodewords, formatBits, qrMatrix, qrPath } from "./qr";

const ALIGN: Record<number, number[]> = {
  1: [],
  2: [6, 18],
  3: [6, 22],
  4: [6, 26],
  5: [6, 30],
  6: [6, 34],
};
// Per version: EC codewords per block, block count (error level M).
const LAYOUT: Record<number, { ec: number; blocks: number }> = {
  1: { ec: 10, blocks: 1 },
  2: { ec: 16, blocks: 1 },
  3: { ec: 26, blocks: 1 },
  4: { ec: 18, blocks: 2 },
  5: { ec: 24, blocks: 2 },
  6: { ec: 16, blocks: 4 },
};

/** Which modules a decoder knows are structure, derived from the spec's rules
 *  and NOT from the encoder's own bookkeeping. */
function functionMap(size: number, version: number): boolean[][] {
  const fixed = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const mark = (r: number, c: number) => {
    if (r >= 0 && r < size && c >= 0 && c < size) fixed[r][c] = true;
  };
  for (const [top, left] of [
    [0, 0],
    [0, size - 7],
    [size - 7, 0],
  ])
    for (let dr = -1; dr <= 7; dr++) for (let dc = -1; dc <= 7; dc++) mark(top + dr, left + dc);
  for (let i = 0; i < size; i++) {
    mark(6, i);
    mark(i, 6);
  }
  const centres = ALIGN[version];
  for (const r of centres)
    for (const c of centres) {
      if ((r === 6 && c === 6) || (r === 6 && c === size - 7) || (r === size - 7 && c === 6))
        continue;
      for (let dr = -2; dr <= 2; dr++) for (let dc = -2; dc <= 2; dc++) mark(r + dr, c + dc);
    }
  for (let i = 0; i < 9; i++) {
    mark(8, i);
    mark(i, 8);
  }
  for (let i = 0; i < 8; i++) {
    mark(8, size - 1 - i);
    mark(size - 1 - i, 8);
  }
  return fixed;
}

function maskAt(mask: number, r: number, c: number): boolean {
  const fns = [
    () => (r + c) % 2 === 0,
    () => r % 2 === 0,
    () => c % 3 === 0,
    () => (r + c) % 3 === 0,
    () => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
    () => ((r * c) % 2) + ((r * c) % 3) === 0,
    () => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
    () => ((((r + c) % 2) + ((r * c) % 3)) % 2) === 0,
  ];
  return fns[mask]();
}

/** Read the format strip beside the top-left eye and recover level + mask. */
function readFormat(m: boolean[][]): { level: number; mask: number } {
  let bits = 0;
  const take = (i: number, v: boolean) => {
    if (v) bits |= 1 << i;
  };
  for (let i = 0; i <= 5; i++) take(i, m[8][i]);
  take(6, m[8][7]);
  take(7, m[8][8]);
  take(8, m[7][8]);
  for (let i = 9; i <= 14; i++) take(i, m[14 - i][8]);
  const unmasked = bits ^ 0x5412;
  return { level: unmasked >>> 13, mask: (unmasked >>> 10) & 0b111 };
}

/** The whole trip back: matrix in, original text out. Throws on a symbol that
 *  does not decode, which is the failure mode the tests are looking for. */
function decode(m: boolean[][]): string {
  const size = m.length;
  const version = (size - 17) / 4;
  const { level, mask } = readFormat(m);
  expect(level).toBe(0b00); // error level M
  const fixed = functionMap(size, version);

  const bits: number[] = [];
  let upward = true;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let i = 0; i < size; i++) {
      const r = upward ? size - 1 - i : i;
      for (const c of [right, right - 1]) {
        if (fixed[r][c]) continue;
        bits.push((m[r][c] !== maskAt(mask, r, c)) ? 1 : 0);
      }
    }
    upward = !upward;
  }

  const stream: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8)
    stream.push(bits.slice(i, i + 8).reduce((acc, b) => (acc << 1) | b, 0));

  const { ec, blocks } = LAYOUT[version];
  const total = stream.length;
  const dataLen = total - ec * blocks;
  const perBlock = dataLen / blocks;
  const data: number[][] = Array.from({ length: blocks }, () => []);
  const parity: number[][] = Array.from({ length: blocks }, () => []);
  for (let i = 0; i < dataLen; i++) data[i % blocks].push(stream[i]);
  for (let i = 0; i < ec * blocks; i++) parity[i % blocks].push(stream[dataLen + i]);
  for (let b = 0; b < blocks; b++) {
    expect(data[b].length).toBe(perBlock);
    // A block whose parity is right re-derives its own EC codewords exactly.
    expect([...ecCodewords(Uint8Array.from(data[b]), ec)]).toEqual(parity[b]);
  }

  const flat = data.flat();
  expect(flat[0] >>> 4).toBe(0b0100); // byte mode
  const length = ((flat[0] & 0x0f) << 4) | (flat[1] >>> 4);
  const payload: number[] = [];
  for (let i = 0; i < length; i++)
    payload.push(((flat[1 + i] & 0x0f) << 4) | (flat[2 + i] >>> 4));
  return new TextDecoder().decode(Uint8Array.from(payload));
}

describe("qrMatrix", () => {
  it("round-trips the URL a publish actually hands back", () => {
    const url = "https://chinese-hsk-cards.pages.dev";
    const m = qrMatrix(url)!;
    expect(m).not.toBeNull();
    expect(m.length).toBe(29); // version 3
    expect(decode(m)).toBe(url);
  });

  it("round-trips at every version it claims to support", () => {
    // One string per version, chosen to land just inside each capacity.
    for (const [version, bytes] of [
      [1, 14],
      [2, 26],
      [3, 42],
      [4, 62],
      [5, 84],
      [6, 106],
    ] as const) {
      const text = "https://a" + "b".repeat(bytes - 9);
      const m = qrMatrix(text)!;
      expect(m.length).toBe(17 + 4 * version);
      expect(decode(m)).toBe(text);
    }
  });

  it("round-trips non-ASCII, which is UTF-8 in byte mode", () => {
    const text = "汉字 · café";
    expect(decode(qrMatrix(text)!)).toBe(text);
  });

  it("draws the three finder eyes and the timing lines", () => {
    const m = qrMatrix("https://x.pages.dev")!;
    const n = m.length;
    for (const [top, left] of [
      [0, 0],
      [0, n - 7],
      [n - 7, 0],
    ]) {
      expect(m[top][left]).toBe(true);
      expect(m[top + 1][left + 1]).toBe(false); // the light ring
      expect(m[top + 3][left + 3]).toBe(true); // the 3×3 core
    }
    for (let i = 8; i < n - 8; i++) {
      expect(m[6][i]).toBe(i % 2 === 0);
      expect(m[i][6]).toBe(i % 2 === 0);
    }
    expect(m[n - 8][8]).toBe(true); // the module that is always dark
  });

  it("gives up rather than truncating a URL it cannot hold", () => {
    expect(QR_MAX_BYTES).toBe(106);
    expect(qrMatrix("x".repeat(QR_MAX_BYTES))).not.toBeNull();
    expect(qrMatrix("x".repeat(QR_MAX_BYTES + 1))).toBeNull();
    // Multi-byte characters count as their UTF-8 length, not their length in
    // code points — a symbol sized by the latter would overflow.
    expect(qrMatrix("é".repeat(54))).toBeNull();
  });

  it("is deterministic", () => {
    expect(qrMatrix("https://x.pages.dev")).toEqual(qrMatrix("https://x.pages.dev"));
  });
});

describe("formatBits", () => {
  it("is a valid BCH codeword for every mask", () => {
    for (let mask = 0; mask < 8; mask++) {
      let rem = formatBits(mask) ^ 0x5412;
      for (let i = 14; i >= 10; i--) if ((rem >>> i) & 1) rem ^= 0x537 << (i - 10);
      expect(rem).toBe(0);
    }
  });
});

describe("qrPath", () => {
  it("emits one 1×1 square per dark module, offset by the quiet zone", () => {
    const path = qrPath([[true, false], [false, true]], 2);
    expect(path).toBe("M2 2h1v1h-1zM3 3h1v1h-1z");
  });
});
