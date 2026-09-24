#!/usr/bin/env node
// @covers AC-024, AC-025, AC-026, AC-027
/**
 * record-decision.ts
 *
 * /reconcile が requirements.yaml を変更した理由を、spec単位の decisions.md に追記する。
 * record-verdict.ts の `--note`（そのACの最新の検証結果として上書きされる一時的な状態）とは異なり、
 * decisions.md は要件そのものがいつ・なぜ変わったかを残す追記専用の恒久ログ（AS-015, AS-017）。
 *
 * usage:
 *   npx tsx scripts/record-decision.ts --spec <slug> --ref <AC-XXX | AS-XXX | out_of_scope など自由文字列> --reason "..."
 * exit:
 *   0 = 記録成功  2 = 引数不正 / 対象specが存在しない
 */
import { existsSync, writeFileSync, appendFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : undefined;
}

const slug = arg("spec");
const ref = (arg("ref") ?? "").trim();
const reason = (arg("reason") ?? "").trim();

if (!slug || !ref || !reason) {
  console.error('usage: --spec <slug> --ref <ref> --reason "..."（--ref / --reason はtrim後に空だと記録できません）');
  process.exit(2);
}

const specDir = join(root, "specs", slug);
const reqPath = join(specDir, "requirements.yaml");
if (!existsSync(reqPath)) {
  console.error(`specs/${slug}/requirements.yaml が存在しません。--spec を確認してください`);
  process.exit(2);
}

const decisionsPath = join(specDir, "decisions.md");
const today = new Date().toISOString().slice(0, 10);
// AS-017: エントリは1行の箇条書き。reasonに改行が含まれていても1行に畳む
const oneLineReason = reason.replace(/\s*\n\s*/g, " ");
const entry = `- ${today} [${ref}] ${oneLineReason}\n`;

if (existsSync(decisionsPath)) {
  appendFileSync(decisionsPath, entry);
} else {
  writeFileSync(decisionsPath, `# ${slug} — 要件変更ログ\n\n${entry}`);
}

console.log(`recorded -> specs/${slug}/decisions.md`);
