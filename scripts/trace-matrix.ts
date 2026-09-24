#!/usr/bin/env node
// @covers AC-002, AC-005, AC-006, AC-010, AC-011, AC-016, AC-021, AC-023, AC-029, AC-030, AC-031, AC-032, AC-033, AC-034, AC-035
/**
 * trace-matrix.ts
 *
 * 要件適合性を機械的に検査する。LLMの自己申告に依存しないための土台。
 *
 *   順方向: 受入基準(AC) -> テスト      … 未実装ACの検出
 *   逆方向: 変更されたコード -> AC       … 仕様外実装(スコープクリープ)の検出
 *   仮定  : @assumption -> 要件の assumptions … 暗黙の仮決めの検出
 *
 * カバーの定義: 有効な it()/test() の **タイトル文字列** に AC-ID が含まれること。
 * コメント・describe・skip/todo/xit は数えない。
 * `.py` ファイルは代わりに pytest 用の検出（トップレベル関数直後のdocstring）を使う。
 * 拡張子ごとに使う検出ロジックを完全に分けており、混在しない（FEAT-004 AC-021）。
 * @playwright/test をimportしているJS/TSファイルは、describeタイトル由来のAC-ID検出を追加で行う（FEAT-006 AC-029〜035）。
 *
 * usage:
 *   npx tsx scripts/trace-matrix.ts [--json] [--strict] [--base origin/master] [--root <dir>]
 *
 * --root <dir> : conformance.config.json / specDir / srcDirs の探索基準を <dir> にする。
 *                git diff は常に実行時のcwdから `--relative=<dir>` で取得し、<dir> 相対パスに揃える
 *                （@assumption AS-005: examples/with-samples/ のような別ツリーを検査するため）。
 * exit:
 *   0 = 適合  1 = 不適合(error あり)  2 = 実行エラー
 */
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative, extname } from "node:path";
import { execSync } from "node:child_process";
import { parse } from "yaml";
import { toPosixPath } from "./lib/posix-path.ts";
import { extractPytestCoverage } from "./lib/pytest-detect.ts";
import { extractPlaywrightCoverage, isPlaywrightFile } from "./lib/playwright-detect.ts";

// ---------- types ----------
type Level = "error" | "warn";
type Priority = "must" | "should" | "could";

interface AC {
  id: string;
  then?: string;
  priority?: Priority;
  verify?: "test" | "manual" | "inspection";
  status?: string;
  attempts?: number;
  evidence?: string;
}
interface Assumption { id: string; statement?: string; status?: string; resolve_by?: string }
interface Spec {
  id: string;
  title: string;
  status?: string;
  out_of_scope?: string[];
  assumptions?: Assumption[];
  open_questions?: unknown[];
  acceptance?: AC[];
  __file: string;
}
interface Finding { level: Level; code: string; ref: string; message: string }
interface Config {
  specDir: string;
  srcDirs: string[];
  testFilePattern: string;
  codeExtensions: string[];
  ignore: string[];
  baseRef: string;
}

const DEFAULTS: Config = {
  specDir: "specs",
  srcDirs: ["src", "tests", "app", "lib"],
  testFilePattern: "\\.(test|spec)\\.[cm]?[jt]sx?$",
  codeExtensions: ["ts", "tsx", "js", "jsx", "mjs", "cjs"],
  ignore: ["node_modules", ".git", "dist", "build", ".next", "coverage"],
  baseRef: "origin/master",
};

// ---------- args ----------
const argv = process.argv.slice(2);
const asJson = argv.includes("--json");
const strict = argv.includes("--strict");
const baseArg = argv.indexOf("--base");
const rootArg = argv.indexOf("--root");
const invocationDir = process.cwd();
const rootRel = rootArg !== -1 ? argv[rootArg + 1] : undefined;
const root = rootRel ? join(invocationDir, rootRel) : invocationDir;

const cfgPath = join(root, "conformance.config.json");
const cfg: Config = {
  ...DEFAULTS,
  ...(existsSync(cfgPath) ? JSON.parse(readFileSync(cfgPath, "utf8")) : {}),
};
if (baseArg !== -1 && argv[baseArg + 1]) cfg.baseRef = argv[baseArg + 1];

const testRe = new RegExp(cfg.testFilePattern);
const extSet = new Set(cfg.codeExtensions.map((e) => "." + e.replace(/^\./, "")));
const AC_RE = /\bAC-\d{3,}\b/g;
const COVERS_RE = /@covers\s+((?:AC-\d{3,}[,\s]*)+)/g;
const ASSUME_RE = /@assumption\s+(AS-\d{3,})/g;
// it("...") / test("...") / it.only / it.skip / it.todo / xit / xtest
const TEST_CALL_RE = /\b(x?)(it|test)(?:\.(only|skip|todo|each|concurrent))?\s*\(\s*(["'`])((?:(?!\4)[\s\S])*?)\4/g;

// ---------- fs ----------
function* walk(dir: string): Generator<string> {
  let entries: string[];
  try { entries = readdirSync(dir); } catch { return; }
  for (const e of entries) {
    if (cfg.ignore.includes(e)) continue;
    const p = join(dir, e);
    let st;
    try { st = statSync(p); } catch { continue; }
    if (st.isDirectory()) yield* walk(p);
    else yield p;
  }
}

const findings: Finding[] = [];
const F = (level: Level, code: string, ref: string, message: string) => findings.push({ level, code, ref, message });

// ---------- load specs ----------
const specs: Spec[] = [];
for (const f of walk(join(root, cfg.specDir))) {
  if (!/requirements\.ya?ml$/.test(f)) continue;
  try {
    const doc = parse(readFileSync(f, "utf8")) as Spec;
    doc.__file = relative(root, f);
    specs.push(doc);
  } catch (err) {
    console.error(`spec parse failed: ${f}\n${(err as Error).message}`);
    process.exit(2);
  }
}
if (specs.length === 0) {
  console.error(`no requirements.yaml under ${cfg.specDir}/`);
  process.exit(2);
}

// draft は検査対象外（書きかけの要件でゲートを落とさない）
const active = specs.filter((s) => {
  if (s.status === "draft") { F("warn", "DRAFT_SPEC", s.id, `${s.__file}: draft のため検査をスキップ`); return false; }
  return true;
});

const acIndex = new Map<string, { ac: AC; spec: Spec }>();
const asIndex = new Map<string, { as: Assumption; spec: Spec }>();
for (const s of active) {
  for (const ac of s.acceptance ?? []) {
    if (acIndex.has(ac.id)) F("error", "DUPLICATE_ID", ac.id, `${acIndex.get(ac.id)!.spec.__file} と ${s.__file} で重複。IDはリポジトリ全体で一意にする`);
    else acIndex.set(ac.id, { ac, spec: s });
  }
  for (const a of s.assumptions ?? []) {
    if (asIndex.has(a.id)) F("error", "DUPLICATE_ID", a.id, `${asIndex.get(a.id)!.spec.__file} と ${s.__file} で重複`);
    else asIndex.set(a.id, { as: a, spec: s });
  }
}

// ---------- scan code ----------
interface FileScan {
  path: string; isTest: boolean;
  mentions: Set<string>;       // ファイル内のあらゆる AC-ID 言及
  activeTitles: Set<string>;   // 有効なテストタイトル内の AC-ID
  skippedTitles: Set<string>;  // skip/todo/xit のタイトル内の AC-ID
  covers: Set<string>;
  assumptions: Set<string>;
}
const files: FileScan[] = [];
for (const dir of cfg.srcDirs) {
  for (const f of walk(join(root, dir))) {
    if (!extSet.has(extname(f))) continue;
    const text = readFileSync(f, "utf8");
    const covers = new Set<string>();
    for (const m of text.matchAll(COVERS_RE)) for (const id of m[1].match(AC_RE) ?? []) covers.add(id);
    const activeTitles = new Set<string>();
    const skippedTitles = new Set<string>();
    if (extname(f) === ".py") {
      const { active, skipped } = extractPytestCoverage(text);
      for (const id of active) activeTitles.add(id);
      for (const id of skipped) skippedTitles.add(id);
    } else if (isPlaywrightFile(text)) {
      const { active, skipped } = extractPlaywrightCoverage(text);
      for (const id of active) activeTitles.add(id);
      for (const id of skipped) skippedTitles.add(id);
    } else {
      for (const m of text.matchAll(TEST_CALL_RE)) {
        const skipped = m[1] === "x" || m[3] === "skip" || m[3] === "todo";
        for (const id of m[5].match(AC_RE) ?? []) (skipped ? skippedTitles : activeTitles).add(id);
      }
    }
    const posixPath = toPosixPath(relative(root, f));
    files.push({
      path: posixPath,
      // testFilePattern はパス区切りに "/" を使う前提(例: "(^|/)test_[^/]+\.py$")で書かれうるため、
      // Windowsのバックスラッシュを含む生パス f ではなく正規化済みの posixPath に対して判定する
      // （FEAT-004のWindows実機検証で、この判定漏れによりpytestのテストファイルが isTest=false に
      // なる不具合として発見された。JSの既存パターンはファイル名末尾のみを見るため today まで顕在化しなかった）
      // @assumption AS-014
      isTest: testRe.test(posixPath),
      mentions: new Set(text.match(AC_RE) ?? []),
      activeTitles, skippedTitles, covers,
      assumptions: new Set([...text.matchAll(ASSUME_RE)].map((m) => m[1])),
    });
  }
}

// ---------- changed files (reverse trace) ----------
let changed: string[] | null = null;
try {
  const relFlag = rootRel ? `--relative=${rootRel} ` : "";
  const out = execSync(`git diff --name-only ${relFlag}--diff-filter=d ${cfg.baseRef}...HEAD`, { cwd: invocationDir, stdio: ["ignore", "pipe", "ignore"] }).toString();
  changed = out.split("\n").map((s) => s.trim()).filter(Boolean);
} catch { changed = null; }

// ---------- checks ----------
const coverage = new Map<string, string[]>();
for (const [id] of acIndex) coverage.set(id, []);
for (const f of files) {
  if (!f.isTest) continue;
  for (const id of f.activeTitles) if (coverage.has(id)) coverage.get(id)!.push(f.path);
}

// 1. 順方向
for (const [id, { ac, spec }] of acIndex) {
  const mode = ac.verify ?? "test";
  const prio: Priority = ac.priority ?? "must";
  const uncoveredLevel: Level = prio === "could" ? "warn" : "error";

  if (mode === "test" && coverage.get(id)!.length === 0) {
    const onlySkipped = files.some((f) => f.isTest && f.skippedTitles.has(id));
    const onlyMentioned = files.some((f) => f.isTest && f.mentions.has(id));
    if (onlySkipped) F("error", "SKIPPED_TEST", id, `テストが skip/todo になっている。skip はカバーではない`);
    else if (onlyMentioned) F(uncoveredLevel, "UNCOVERED_AC", id, `テストファイル内に言及はあるが it()/test() のタイトルに含まれていない`);
    else F(uncoveredLevel, "UNCOVERED_AC", id, `${spec.__file}: 受入基準にテストが1件も紐づいていない`);
  }
  if ((mode === "manual" || mode === "inspection") && !(ac.evidence ?? "").trim()) {
    F("error", "NO_EVIDENCE", id, `verify: ${mode} だが evidence が空`);
  }
  if (ac.status === "pass" && !(ac.evidence ?? "").trim()) F("error", "PASS_WITHOUT_EVIDENCE", id, `pass なのに evidence が無い。record-verdict.ts 以外で書き換えられた疑い`);
  if (ac.status === "fail") F("error", "AC_FAILING", id, `検証結果が fail のまま`);
  if (ac.status === "blocked") F("error", "AC_BLOCKED", id, `人間へエスカレーション済み。判断が必要`);
  if (!ac.status || ac.status === "pending") F(prio === "could" ? "warn" : "warn", "AC_PENDING", id, `未検証`);
}

// 2. 宙に浮いた参照
for (const f of files) {
  for (const id of f.mentions) if (!acIndex.has(id)) F("error", "DANGLING_AC", id, `${f.path}: 要件に存在しないACを参照`);
  for (const id of f.assumptions) if (!asIndex.has(id)) F("error", "UNRECORDED_ASSUMPTION", id, `${f.path}: 要件に記録のない仮定がコードに埋まっている`);
}

// 3. 逆方向
if (changed === null) {
  F("warn", "NO_BASE_REF", cfg.baseRef, `差分が取得できず逆方向トレースをスキップ (CI では fetch-depth: 0 が必要)`);
} else {
  const byPath = new Map(files.map((f) => [f.path, f]));
  for (const p of changed) {
    if (!extSet.has(extname(p))) continue;
    if (p.startsWith(cfg.specDir + "/")) continue;
    const f = byPath.get(p);
    if (!f) { F("warn", "UNSCANNED_CHANGE", p, `srcDirs の外で変更されたコード。conformance.config.json の srcDirs に追加を検討`); continue; }
    if (f.isTest) continue;
    if (f.covers.size === 0) F(strict ? "error" : "warn", "UNTRACED_CHANGE", p, `変更されたが @covers AC-XXX が無い。要件に無い実装の可能性`);
  }
}

// 4. 仮定・未決事項・スコープ
for (const [id, { as, spec }] of asIndex) {
  const used = files.some((f) => f.assumptions.has(id));
  if (as.status === "assumed") {
    F("warn", "OPEN_ASSUMPTION", id, `${as.statement ?? ""} (期限 ${as.resolve_by ?? "未設定"}) — 確定させて confirmed へ`);
    if (!used) F("warn", "ASSUMPTION_UNANCHORED", id, `${spec.__file}: 仮決めだがコード側に @assumption ${id} が無く、回収漏れを検出できない`);
    if (as.resolve_by && as.resolve_by < new Date().toISOString().slice(0, 10)) F("error", "ASSUMPTION_EXPIRED", id, `期限 ${as.resolve_by} を過ぎても未確定`);
  }
  if (as.status === "confirmed" && used) F("warn", "STALE_ASSUMPTION_TAG", id, `確定済みなのにコードに @assumption ${id} が残っている。削除する`);
}
for (const s of active) {
  const oq = (s.open_questions ?? []).filter((q) => (q as { status?: string }).status !== "resolved").length;
  if (s.status === "frozen" && oq > 0) F("error", "FROZEN_WITH_OPEN", s.id, `${s.__file}: 未決事項が ${oq} 件あるまま frozen`);
  if (s.status === "frozen" && (s.out_of_scope ?? []).length === 0) F("error", "NO_SCOPE_FENCE", s.id, `${s.__file}: out_of_scope が空。スコープの外周が定義されていない`);
}

// ---------- report ----------
const errors = findings.filter((f) => f.level === "error");
const warns = findings.filter((f) => f.level === "warn");
const total = acIndex.size;
const covered = [...coverage.values()].filter((v) => v.length > 0).length;
const passed = [...acIndex.values()].filter((x) => x.ac.status === "pass").length;

if (asJson) {
  console.log(JSON.stringify({ summary: { total, covered, passed, errors: errors.length, warns: warns.length }, coverage: Object.fromEntries(coverage), findings }, null, 2));
} else {
  console.log("\n  AC      verify   prio    status    tests");
  console.log("  " + "-".repeat(54));
  for (const [id, { ac }] of acIndex) {
    const t = coverage.get(id)!;
    console.log(`  ${id}  ${(ac.verify ?? "test").padEnd(8)} ${(ac.priority ?? "must").padEnd(7)} ${(ac.status ?? "pending").padEnd(9)} ${t.length ? t.join(", ") : "—"}`);
  }
  console.log(`\n  カバー ${covered}/${total}   pass ${passed}/${total}\n`);
  for (const f of [...errors, ...warns]) console.log(`  ${f.level === "error" ? "✗" : "!"} [${f.code}] ${f.ref} — ${f.message}`);
  console.log(`\n  error ${errors.length} / warn ${warns.length}\n`);
}
process.exit(errors.length > 0 ? 1 : 0);
