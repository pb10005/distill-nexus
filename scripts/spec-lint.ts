#!/usr/bin/env node
/**
 * spec-lint.ts
 *
 * 要件そのものを検査する。実装前に落とせる欠陥をここで落とす。
 * 「人間が要件を過不足なく書けたか」ではなく「機械が検証可能な形になっているか」を見る。
 *
 *   構造   : 必須フィールド、ID形式、重複
 *   曖昧性 : 曖昧語、数値なし非機能要件、観測不能な then
 *   網羅性 : 異常系ACの不在、スコープ外の不在
 *   遷移   : draft -> frozen の可否
 *
 * usage:
 *   npx tsx scripts/spec-lint.ts [specs/foo] [--json] [--gate freeze] [--questions]
 * exit:
 *   0 = 合格  1 = 不合格  2 = 実行エラー
 */
import { readFileSync, readdirSync, statSync, existsSync } from "node:fs";
import { join, relative } from "node:path";
import { parse } from "yaml";

type Level = "error" | "warn";
interface AC {
  id?: string; kind?: "functional" | "nfr"; given?: string; when?: string; then?: string;
  priority?: string; verify?: string; status?: string; evidence?: string;
}
interface Assumption { id?: string; statement?: string; status?: string; owner?: string; resolve_by?: string }
interface OQ { id?: string; question?: string; raised?: string; status?: string }
interface Spec {
  id?: string; title?: string; status?: string; out_of_scope?: string[];
  assumptions?: Assumption[]; open_questions?: OQ[]; acceptance?: AC[]; __file: string;
}
interface Finding { level: Level; code: string; ref: string; message: string; question?: string }

const root = process.cwd();
const argv = process.argv.slice(2);
const asJson = argv.includes("--json");
const withQuestions = argv.includes("--questions");
const gateIdx = argv.indexOf("--gate");
const freezeGate = gateIdx !== -1 && argv[gateIdx + 1] === "freeze";
const positional = argv.filter((a) => !a.startsWith("--") && a !== "freeze");

const cfgPath = join(root, "conformance.config.json");
const specDir: string = existsSync(cfgPath) ? (JSON.parse(readFileSync(cfgPath, "utf8")).specDir ?? "specs") : "specs";

// ---------- 辞書 ----------
// 部分一致の誤検知を避けるため正規表現で持つ（「等しい」「未定義」「高速道路」を拾わない）
const jp = (w: string, notFollowedBy?: string) => new RegExp(w + (notFollowedBy ? `(?!${notFollowedBy})` : ""));
const en = (w: string) => new RegExp(`\\b${w}\\b`, "i");
const AMBIGUOUS: [string, RegExp][] = [
  ["適切に", jp("適切に")], ["適宜", jp("適宜")], ["適当に", jp("適当に")], ["いい感じ", jp("いい感じ")],
  ["良い感じ", jp("良い感じ")], ["柔軟に", jp("柔軟に")], ["高速", jp("高速", "道路")], ["軽量", jp("軽量")],
  ["使いやすい", jp("使いやすい")], ["分かりやすい", jp("分かりやすい")], ["わかりやすい", jp("わかりやすい")],
  ["シンプルに", jp("シンプルに")], ["直感的", jp("直感的")], ["必要に応じて", jp("必要に応じて")],
  ["可能な限り", jp("可能な限り")], ["基本的に", jp("基本的に")], ["原則として", jp("原則として")],
  ["極力", jp("極力")], ["なるべく", jp("なるべく")], ["ちゃんと", jp("ちゃんと")], ["しっかり", jp("しっかり")],
  ["きちんと", jp("きちんと")], ["十分な", jp("十分な")], ["スムーズ", jp("スムーズ")], ["ストレスなく", jp("ストレスなく")],
  ["モダン", jp("モダン")], ["リッチ", jp("リッチ")], ["セキュアに", jp("セキュアに")], ["堅牢", jp("堅牢")],
  ["スケーラブル", jp("スケーラブル")], ["最適化する", jp("最適化する")], ["など", jp("など")],
  ["等", jp("等", "[しくさ]")], ["その他", jp("その他")], ["一部の", jp("一部の")],
  ["appropriately", en("appropriately")], ["properly", en("properly")], ["fast", en("fast")],
  ["quickly", en("quickly")], ["efficiently", en("efficiently")], ["user-friendly", en("user-friendly")],
  ["intuitive", en("intuitive")], ["robust", en("robust")], ["scalable", en("scalable")],
  ["seamless", en("seamless")], ["as needed", en("as needed")], ["if necessary", en("if necessary")], ["etc", en("etc")],
];
const PLACEHOLDER = /TBD|TODO|\?\?\?|未定(?!義)|検討中|要相談/;
// 観測可能な結果を示す語（これが無い then は検証手順に落ちない）
const OBSERVABLE = [
  "返す", "返却", "表示", "出力", "保存", "記録", "送信", "遷移", "拒否", "作成", "生成",
  "更新", "削除", "通知", "含む", "一致", "等しい", "以内", "以下", "以上", "ならない",
  "しない", "できない", "受け付けない", "変わらない", "残る", "returns", "displays", "throws", "rejects",
];
const FAILURE_HINTS = [
  "失敗", "エラー", "不正", "無効", "権限", "存在しない", "タイムアウト", "重複", "超過", "超え",
  "空文字", "空の", "空欄", "上限", "拒否", "期限切れ", "オフライン", "競合", "使用済",
  "400", "401", "403", "404", "409", "422", "429", "500",
];
const NUMERIC = /\d+\s*(ms|ミリ秒|秒|分|時間|日|件|回|%|％|MB|GB|KB|バイト|文字|px|rps|req\/s|同時|ユーザー|人|並列|回\/)/;
const COMPOUND = /かつ|\band\b/;

const QUESTION: Record<string, string> = {
  AMBIGUOUS_TERM: "この語を測定可能な条件に置き換えるとどうなりますか（既定案を1つ提示して否定してもらう）",
  UNMEASURABLE_NFR: "しきい値の数値と測定条件（対象環境・パーセンタイル）は何にしますか",
  UNOBSERVABLE_THEN: "満たされたことを外から観測する手段は何ですか（応答・画面・DB・ログのどれか）",
  NO_FAILURE_PATH: "失敗・不正入力・権限なしのときの期待挙動は何ですか",
  NO_SCOPE_FENCE: "今回やらないことを3つ挙げるとしたら何ですか",
  MISSING_GWT: "この基準の前提条件（given）と操作（when）は何ですか",
  ASSUMPTION_NO_OWNER: "この仮決めは誰がいつまでに確定させますか",
  COMPOUND_THEN: "この条件は分割できますか（片方だけ満たされた状態を許すか）",
};

// ---------- 収集 ----------
function* walk(dir: string): Generator<string> {
  let entries: string[];
  try { entries = readdirSync(dir); } catch { return; }
  for (const e of entries) {
    if (["node_modules", ".git", "dist"].includes(e)) continue;
    const p = join(dir, e);
    if (statSync(p).isDirectory()) yield* walk(p); else yield p;
  }
}
const targets = positional.length ? positional : [specDir];
const specs: Spec[] = [];
for (const t of targets) {
  for (const f of walk(join(root, t))) {
    if (!/requirements\.ya?ml$/.test(f)) continue;
    try {
      const d = parse(readFileSync(f, "utf8")) as Spec;
      d.__file = relative(root, f);
      specs.push(d);
    } catch (err) {
      console.error(`spec parse failed: ${f}\n${(err as Error).message}`);
      process.exit(2);
    }
  }
}
if (!specs.length) { console.error(`no requirements.yaml under ${targets.join(", ")}`); process.exit(2); }

const findings: Finding[] = [];
const F = (level: Level, code: string, ref: string, message: string) =>
  findings.push({ level, code, ref, message, ...(withQuestions && QUESTION[code] ? { question: QUESTION[code] } : {}) });

// freeze ゲートで error に昇格するコード
const FREEZE_BLOCKING = new Set([
  "AMBIGUOUS_TERM", "UNMEASURABLE_NFR", "UNOBSERVABLE_THEN", "COMPOUND_THEN",
  "NO_FAILURE_PATH", "MISSING_GWT", "ASSUMPTION_NO_OWNER", "PLACEHOLDER_LEFT", "NO_SCOPE_FENCE",
]);
// draft 以外（frozen / reconciling）は常に freeze 基準。--gate freeze は draft にもその基準を適用する。
const lv = (code: string, base: Level, spec?: Spec): Level =>
  (freezeGate || (spec && spec.status && spec.status !== "draft")) && FREEZE_BLOCKING.has(code) ? "error" : base;

const today = new Date().toISOString().slice(0, 10);
const seen = new Set<string>();

for (const s of specs) {
  const where = s.__file;
  const scan = (text: string, ref: string) => {
    for (const [w, re] of AMBIGUOUS) {
      if (re.test(text)) F(lv("AMBIGUOUS_TERM", "warn", s), "AMBIGUOUS_TERM", ref, `「${w}」— 検証手順に落ちない。測定可能な条件へ置換する`);
    }
    if (PLACEHOLDER.test(text)) F(lv("PLACEHOLDER_LEFT", "warn", s), "PLACEHOLDER_LEFT", ref, `プレースホルダが残っている: ${text.slice(0, 40)}`);
  };

  // --- 構造 ---
  for (const k of ["id", "title", "status", "acceptance"] as const) {
    if (!s[k]) F("error", "SCHEMA_MISSING", s.id ?? where, `${where}: 必須フィールド ${k} が無い`);
  }
  if (s.id && !/^[A-Z]+-\d{3,}$/.test(s.id)) F("error", "BAD_ID", String(s.id), `${where}: id は FEAT-001 形式`);
  if (s.status && !["draft", "frozen", "reconciling"].includes(s.status)) F("error", "BAD_STATUS", s.id ?? where, `${where}: status は draft | frozen | reconciling`);
  if (!(s.out_of_scope ?? []).length) F(lv("NO_SCOPE_FENCE", "warn", s), "NO_SCOPE_FENCE", s.id ?? where, `${where}: out_of_scope が空。過剰実装を止める外周が無い`);
  scan(s.title ?? "", s.id ?? where);
  for (const o of s.out_of_scope ?? []) scan(o, `${s.id}:out_of_scope`);

  const acs = s.acceptance ?? [];
  if (acs.length === 0) F("error", "NO_AC", s.id ?? where, `${where}: 受入基準が1件も無い`);
  if (acs.length > 15) F("warn", "TOO_MANY_ACS", s.id ?? where, `${where}: AC が ${acs.length} 件。機能を分割した方が検証が回る`);

  // --- 各AC ---
  for (const ac of acs) {
    const ref = ac.id ?? `${s.id}:?`;
    if (!ac.id || !/^AC-\d{3,}$/.test(ac.id)) { F("error", "BAD_ID", ref, `${where}: AC の id は AC-001 形式`); continue; }
    if (seen.has(ac.id)) F("error", "DUPLICATE_ID", ac.id, `${where}: ID がリポジトリ内で重複`);
    seen.add(ac.id);

    if (!ac.then) { F("error", "SCHEMA_MISSING", ref, `then が無い。何が起きれば満たされるのかが未定義`); continue; }
    if (!ac.given || !ac.when) F(lv("MISSING_GWT", "warn", s), "MISSING_GWT", ref, `given / when が欠けている。再現手順に落ちない`);

    scan([ac.given, ac.when, ac.then].filter(Boolean).join(" "), ref);

    if (!OBSERVABLE.some((w) => ac.then!.includes(w))) {
      F(lv("UNOBSERVABLE_THEN", "warn", s), "UNOBSERVABLE_THEN", ref, `then に観測可能な結果が無い: 「${ac.then}」`);
    }
    if (COMPOUND.test(ac.then) && ac.then.length > 30) {
      F(lv("COMPOUND_THEN", "warn", s), "COMPOUND_THEN", ref, `条件が複合している。分割すると片側だけ失敗した時に原因が特定できる`);
    }
    if (ac.kind === "nfr" && !NUMERIC.test(ac.then)) {
      F(lv("UNMEASURABLE_NFR", "warn", s), "UNMEASURABLE_NFR", ref, `非機能要件に数値としきい値が無い: 「${ac.then}」`);
    }
    if (ac.verify && !["test", "manual", "inspection"].includes(ac.verify)) F("error", "BAD_VERIFY", ref, `verify は test | manual | inspection`);
    if (ac.priority && !["must", "should", "could"].includes(ac.priority)) F("error", "BAD_PRIORITY", ref, `priority は must | should | could`);
  }

  // --- 網羅性 ---
  const allText = acs.map((a) => [a.given, a.when, a.then].filter(Boolean).join(" ")).join(" ");
  if (acs.length > 0 && !FAILURE_HINTS.some((w) => allText.includes(w))) {
    F(lv("NO_FAILURE_PATH", "warn", s), "NO_FAILURE_PATH", s.id ?? where, `${where}: 異常系のACが1件も無い。ハッピーパスだけの要件は必ず後で破綻する`);
  }

  // --- 仮決めと未決 ---
  for (const a of s.assumptions ?? []) {
    const ref = a.id ?? `${s.id}:assumption`;
    if (!a.id || !/^AS-\d{3,}$/.test(a.id)) F("error", "BAD_ID", ref, `${where}: assumption の id は AS-001 形式`);
    if (!a.statement) F("error", "SCHEMA_MISSING", ref, `statement が無い`);
    if (!["assumed", "confirmed", "rejected"].includes(a.status ?? "")) F("error", "BAD_STATUS", ref, `status は assumed | confirmed | rejected`);
    if (a.status === "assumed" && (!a.owner || !a.resolve_by)) {
      F(lv("ASSUMPTION_NO_OWNER", "warn", s), "ASSUMPTION_NO_OWNER", ref, `仮決めに owner / resolve_by が無い。誰も確定させないまま本番に入る`);
    }
    scan(a.statement ?? "", ref);
  }
  for (const q of s.open_questions ?? []) {
    const ref = q.id ?? `${s.id}:oq`;
    if (q.status === "open" && q.raised) {
      const days = Math.floor((Date.parse(today) - Date.parse(q.raised)) / 86400000);
      if (days > 14) F("warn", "STALE_QUESTION", ref, `${days}日間未回答。要件が止まっているのか放置されているのかを判断する`);
    }
  }

  // --- 遷移 ---
  const openCount = (s.open_questions ?? []).filter((q) => q.status !== "resolved").length;
  if (s.status === "frozen" && openCount > 0) F("error", "FROZEN_WITH_OPEN", s.id ?? where, `${where}: 未決事項 ${openCount} 件を残して frozen`);
  if (freezeGate && s.status === "draft" && openCount > 0) F("error", "CANNOT_FREEZE", s.id ?? where, `${where}: 未決事項 ${openCount} 件。確定させるか assumptions へ降格させてから freeze する`);
}

// ---------- 出力 ----------
const errors = findings.filter((f) => f.level === "error");
const warns = findings.filter((f) => f.level === "warn");

if (asJson) {
  console.log(JSON.stringify({ summary: { specs: specs.length, errors: errors.length, warns: warns.length, gate: freezeGate ? "freeze" : "default" }, findings }, null, 2));
} else {
  console.log("");
  for (const f of [...errors, ...warns]) {
    console.log(`  ${f.level === "error" ? "✗" : "!"} [${f.code}] ${f.ref} — ${f.message}`);
    if (f.question) console.log(`      ? ${f.question}`);
  }
  console.log(`\n  ${specs.length} spec / error ${errors.length} / warn ${warns.length}${freezeGate ? "  [freeze gate]" : ""}\n`);
  if (freezeGate && errors.length === 0) console.log("  ✔ freeze 可能\n");
}
process.exit(errors.length > 0 ? 1 : 0);
