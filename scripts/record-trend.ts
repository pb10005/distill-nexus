#!/usr/bin/env node
// @covers AC-036, AC-037, AC-038, AC-039, AC-040, AC-042, AC-043
/**
 * record-trend.ts
 *
 * AI駆動開発トレンドの調査結果（採用/見送り/却下の判断）を、プロジェクト全体で1つの
 * TREND_BACKLOG.md に追記する。record-decision.ts（spec単位のdecisions.md）とは記録先が異なる。
 * 特定specの要件変更を伴わない、プロジェクト横断の技術選定判断を扱う。
 * @assumption AS-024
 *
 * usage:
 *   npx tsx scripts/record-trend.ts --tech <名称> --decision adopt|defer|reject --reason "..." [--revisit "..."]
 * exit:
 *   0 = 記録成功  2 = 引数不正
 */
import { existsSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : undefined;
}

const DECISIONS = ["adopt", "defer", "reject"] as const;
type Decision = (typeof DECISIONS)[number];

const tech = (arg("tech") ?? "").trim();
const decision = arg("decision");
const reason = (arg("reason") ?? "").trim();
const revisitRaw = (arg("revisit") ?? "").trim();

if (!tech || !reason) {
  console.error(
    'usage: --tech <名称> --decision adopt|defer|reject --reason "..." [--revisit "..."]（--tech / --reason はtrim後に空だと記録できません）',
  );
  process.exit(2);
}
if (!decision || !DECISIONS.includes(decision as Decision)) {
  console.error(`--decision は adopt|defer|reject のいずれかを指定してください（指定値: ${decision ?? "(未指定)"}）`);
  process.exit(2);
}
if ((decision === "defer" || decision === "reject") && !revisitRaw) {
  console.error(`--decision ${decision} の場合、--revisit は必須です（trim後に空だと記録できません）`);
  process.exit(2);
}

// adoptでrevisit省略時は、未設定であることが後から判別できるよう固定文言を補う
// @assumption AS-025
const revisit = revisitRaw || "(再検討条件なし)";

const backlogPath = join(root, "TREND_BACKLOG.md");
const today = new Date().toISOString().slice(0, 10);
const oneLine = (s: string) => s.replace(/\s*\n\s*/g, " ");
// エントリは1行の箇条書き。追記専用、初回作成時のみ見出し行を付与する
// @assumption AS-026
const entry = `- ${today} [${tech}] ${decision} — ${oneLine(reason)} / 再検討条件: ${oneLine(revisit)}\n`;

if (existsSync(backlogPath)) {
  appendFileSync(backlogPath, entry);
} else {
  writeFileSync(backlogPath, `# TREND_BACKLOG — AI駆動開発トレンド判断ログ\n\n${entry}`);
}

console.log(`recorded -> TREND_BACKLOG.md`);
