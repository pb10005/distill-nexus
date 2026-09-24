#!/usr/bin/env node
/**
 * record-verdict.ts
 *
 * 検証結果を requirements.yaml に書き戻す唯一の経路。
 * ループの停止条件と「証拠なき pass 禁止」をプロンプトではなくコードで担保する:
 *   - pass には --evidence が必須
 *   - 同じACが max-attempts 回 fail したら自動で blocked にし、open_questions へ降格
 *   - blocked は常に open_questions に起票し、人間の判断を要求する
 *
 * usage:
 *   npx tsx scripts/record-verdict.ts --ac AC-002 --verdict pass|fail|blocked \
 *     --evidence tests/auth.test.ts:41 --note "..." [--max-attempts 2]
 */
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { parseDocument } from "yaml";

const root = process.cwd();
function arg(name: string, fallback?: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : fallback;
}

const acId = arg("ac");
const verdict = arg("verdict");
const note = arg("note", "") as string;
const evidence = arg("evidence", "") as string;
const maxAttempts = Number(arg("max-attempts", "2"));

if (!acId || !verdict || !["pass", "fail", "blocked"].includes(verdict)) {
  console.error("usage: --ac AC-001 --verdict pass|fail|blocked --evidence <file:line|cmd output> [--note ...] [--max-attempts 2]");
  process.exit(2);
}
if (verdict === "pass" && !evidence.trim()) {
  console.error(`✗ pass には --evidence が必須です（例: --evidence tests/auth.test.ts:41）。証拠なき pass は記録できません。`);
  process.exit(2);
}
if (verdict !== "pass" && !note.trim()) {
  console.error(`✗ ${verdict} には --note で失敗内容 / 判定不能の理由が必須です。`);
  process.exit(2);
}

const cfgPath = join(root, "conformance.config.json");
const specDir: string = existsSync(cfgPath) ? (JSON.parse(readFileSync(cfgPath, "utf8")).specDir ?? "specs") : "specs";

function* walk(dir: string): Generator<string> {
  let entries: string[];
  try { entries = readdirSync(dir); } catch { return; }
  for (const e of entries) {
    if (["node_modules", ".git", "dist"].includes(e)) continue;
    const p = join(dir, e);
    if (statSync(p).isDirectory()) yield* walk(p);
    else yield p;
  }
}

let target: string | null = null;
let doc: ReturnType<typeof parseDocument> | null = null;
let idx = -1;
for (const f of walk(join(root, specDir))) {
  if (!/requirements\.ya?ml$/.test(f)) continue;
  const d = parseDocument(readFileSync(f, "utf8"));
  const list = (d.toJS() as { acceptance?: { id: string }[] }).acceptance ?? [];
  const i = list.findIndex((a) => a.id === acId);
  if (i !== -1) { target = f; doc = d; idx = i; break; }
}
if (!doc || !target) {
  console.error(`${acId} が ${specDir}/ 配下のどの requirements.yaml にも存在しない`);
  process.exit(2);
}

const today = new Date().toISOString().slice(0, 10);
const prev = Number(doc.getIn(["acceptance", idx, "attempts"]) ?? 0);
const attempts = verdict === "fail" ? prev + 1 : prev;

doc.setIn(["acceptance", idx, "attempts"], attempts);
doc.setIn(["acceptance", idx, "verified_at"], today);
if (note) doc.setIn(["acceptance", idx, "note"], note);
if (evidence) doc.setIn(["acceptance", idx, "evidence"], evidence);

const escalate = verdict === "blocked" || (verdict === "fail" && attempts >= maxAttempts);
if (escalate) {
  doc.setIn(["acceptance", idx, "status"], "blocked");
  const oq = ((doc.toJS() as { open_questions?: { id?: string }[] }).open_questions ?? []);
  const maxNo = oq.reduce((m, q) => Math.max(m, Number((q.id ?? "").replace(/^OQ-/, "")) || 0), 0);
  const next = `OQ-${String(maxNo + 1).padStart(3, "0")}`;
  const question = verdict === "blocked"
    ? `${acId} は実装側で判定不能: ${note}`
    : `${attempts}回の修正で ${acId} が満たせない。受入基準自体が誤っている / 前提が欠けている可能性がある。最新の失敗: ${note}`;
  doc.setIn(["open_questions"], [...oq, { id: next, from: acId, question, raised: today, status: "open" }]);
} else {
  doc.setIn(["acceptance", idx, "status"], verdict);
}

writeFileSync(target, doc.toString());
console.log(`${acId} -> ${escalate ? "blocked" : verdict} (attempts ${attempts}) @ ${relative(root, target)}`);
if (escalate) {
  console.log(`\n⛔ 人間の判断待ちに降格しました（open_questions に起票）。実装の反復で解こうとしないでください。\n`);
  process.exit(1);
}
