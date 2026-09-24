// @covers AC-029, AC-030, AC-031, AC-033, AC-034, AC-035
//
// Playwright（@playwright/test）のE2Eテストファイル向けのAC-IDカバレッジ抽出。
// 既存のTEST_CALL_RE（it()/test()のタイトルのみ、modifierは only|skip|todo|each|concurrent のみ）を拡張する:
//
//   1. test.describe("AC-XXX: ...", () => { test(...); }) のようにdescribeタイトルに書かれたAC-IDを、
//      直下に有効(非skip)なtest()が1件以上ある場合にのみカバーとして検出する（AS-020, AC-029/AC-033）。
//      describe自体が .skip / .fixme 修飾されている場合は、直下のtest()の状態に関わらず常にskipped扱いにする
//      （AS-021, AC-030）。2階層より深いdescribeの入れ子は特別扱いしない（out_of_scope）。
//   2. test.fixme(...) はskip相当、test.fail(...) は実行される点でskipと異なりactiveとして扱う（AS-022,
//      AC-031/AC-034）。現行のTEST_CALL_REはこの2つのmodifierを認識できず、マッチ自体が丸ごと失敗して
//      完全に無視されていた（UNCOVERED_ACにすらならない不可視化のバグ）。
//   3. describeタイトルとtest()タイトルの両方にAC-IDが書かれている場合、両方を独立してカバーとして計上する
//      （AS-023, AC-035。優先順位・排他は設けない）。
//
// この検出は isPlaywrightFile() が true を返すファイル（@playwright/test をimportしているファイル）にのみ
// trace-matrix.ts から呼び出される。それ以外のファイルは従来通りTEST_CALL_REのみで判定され、
// この検出ロジックの影響を受けない（AC-032）。
// @assumption AS-019
const AC_RE = /\bAC-\d{3,}\b/g;
const PLAYWRIGHT_IMPORT_RE = /from\s+["']@playwright\/test["']/;

// it("...") / test("...") / .only / .skip / .todo / .each / .concurrent / .fixme / .fail / xit / xtest
const PW_TEST_CALL_RE =
  /\b(x?)(it|test)(?:\.(only|skip|todo|each|concurrent|fixme|fail))?\s*\(\s*(["'`])((?:(?!\4)[\s\S])*?)\4/g;

// test.describe("...") / .skip / .fixme / .only / .parallel / .serial
const DESCRIBE_HEADER_RE =
  /\btest\.describe(?:\.(skip|fixme|only|parallel|serial))?\s*\(\s*(["'`])((?:(?!\2)[\s\S])*?)\2\s*,/g;

export interface PlaywrightCoverage {
  active: Set<string>;
  skipped: Set<string>;
}

export function isPlaywrightFile(text: string): boolean {
  return PLAYWRIGHT_IMPORT_RE.test(text);
}

// fixmeはskip相当、fail（失敗することを期待して実行される）はactiveとして扱う。
// @assumption AS-022
function isTestCallSkipped(xPrefix: string, modifier: string | undefined): boolean {
  return xPrefix === "x" || modifier === "skip" || modifier === "todo" || modifier === "fixme";
}

// "{" から対応する "}" までを抽出する。文字列・テンプレートリテラル内の波括弧までは考慮しない、
// pytest-detect.tsと同水準の正規表現ベースの近似。
function extractBraceBlock(text: string, braceStart: number): { body: string; endIndex: number } | null {
  let depth = 0;
  for (let i = braceStart; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      depth--;
      if (depth === 0) return { body: text.slice(braceStart + 1, i), endIndex: i + 1 };
    }
  }
  return null;
}

function extractDescribeBody(text: string, fromIndex: number): string | null {
  const braceStart = text.indexOf("{", fromIndex);
  if (braceStart === -1) return null;
  return extractBraceBlock(text, braceStart)?.body ?? null;
}

// out_of_scope: 2階層より深いdescribeの入れ子は無視し、その配下のtest()は従来通りtest()自身のタイトルでの
// み判定する。ここで body 全体をそのまま test() 検索の対象にすると、2階層目以降のdescribeに包まれたtest()
// まで「直下」として誤検出してしまう（例: 直接のtest()を持たないdescribeでも、孫にtest()があるとactiveに
// なってしまう）。ネストしたdescribeブロックを丸ごと除去し、直下のtest()のみを残す。
function directChildrenOnly(body: string): string {
  let out = "";
  let cursor = 0;
  for (const m of body.matchAll(DESCRIBE_HEADER_RE)) {
    if (m.index < cursor) continue;
    const braceStart = body.indexOf("{", m.index + m[0].length);
    const nested = braceStart === -1 ? null : extractBraceBlock(body, braceStart);
    out += body.slice(cursor, m.index);
    cursor = nested !== null ? nested.endIndex : m.index + m[0].length;
  }
  out += body.slice(cursor);
  return out;
}

export function extractPlaywrightCoverage(text: string): PlaywrightCoverage {
  const active = new Set<string>();
  const skipped = new Set<string>();

  for (const m of text.matchAll(PW_TEST_CALL_RE)) {
    const target = isTestCallSkipped(m[1], m[3]) ? skipped : active;
    for (const id of m[5].match(AC_RE) ?? []) target.add(id);
  }

  for (const m of text.matchAll(DESCRIBE_HEADER_RE)) {
    const modifier = m[1];
    const ids = m[3].match(AC_RE) ?? [];
    if (ids.length === 0) continue;

    // describe自体が.skip/.fixmeなら、直下のtest()の状態に関わらず常にskipped扱いにする。
    // @assumption AS-021
    if (modifier === "skip" || modifier === "fixme") {
      for (const id of ids) skipped.add(id);
      continue;
    }

    // 直下（1階層）に有効(非skip)なtest()が1件以上ある場合にのみカバーとして扱う。
    // @assumption AS-020
    const body = extractDescribeBody(text, m.index + m[0].length);
    const directBody = body !== null ? directChildrenOnly(body) : null;
    const hasActiveChild =
      directBody !== null && [...directBody.matchAll(PW_TEST_CALL_RE)].some((cm) => !isTestCallSkipped(cm[1], cm[3]));
    // describe側とtest()側のAC-IDは優先順位を付けず独立して和集合で加える。
    // @assumption AS-023
    if (hasActiveChild) for (const id of ids) active.add(id);
  }

  return { active, skipped };
}
