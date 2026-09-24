#!/usr/bin/env node
/**
 * spec-init.ts
 *
 * 要件の骨組みを先に物理的に存在させる。
 * 「人間に白紙を書かせない」の担保: 埋めるのはAI、人間は差分を否定するだけ。
 *
 * usage:
 *   npx tsx scripts/spec-init.ts <slug> --title "メールリンクによるログイン"
 */
import { writeFileSync, mkdirSync, existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const [slug] = process.argv.slice(2).filter((a) => !a.startsWith("--"));
const ti = process.argv.indexOf("--title");
const title = ti !== -1 ? process.argv[ti + 1] : "";

if (!slug) {
  console.error('usage: npx tsx scripts/spec-init.ts <slug> --title "..."');
  process.exit(2);
}

const cfgPath = join(root, "conformance.config.json");
const specDir: string = existsSync(cfgPath) ? (JSON.parse(readFileSync(cfgPath, "utf8")).specDir ?? "specs") : "specs";
const dir = join(root, specDir, slug);
const file = join(dir, "requirements.yaml");
if (existsSync(file)) { console.error(`${file} は既に存在します`); process.exit(2); }

// 既存の FEAT 番号を見て採番
let maxNo = 0;
try {
  const { readdirSync } = await import("node:fs");
  for (const d of readdirSync(join(root, specDir))) {
    const p = join(root, specDir, d, "requirements.yaml");
    if (!existsSync(p)) continue;
    const m = readFileSync(p, "utf8").match(/^id:\s*FEAT-(\d+)/m);
    if (m) maxNo = Math.max(maxNo, Number(m[1]));
  }
} catch { /* specDir がまだ無い */ }
const featId = `FEAT-${String(maxNo + 1).padStart(3, "0")}`;

const template = `# ${featId} — 要件の単一の正。実装・テスト・レビューはこれに従属する。
#
# 埋め方: 人間に質問する前に、AIが全項目を推測で埋め切ること。
# 人間の仕事は白紙を埋めることではなく、埋まったものを否定・修正することにある。
# 確信が持てない箇所は open_questions か assumptions に置き、空欄で残さない。
id: ${featId}
title: ${JSON.stringify(title || "TBD")}
status: draft # draft -> (spec-lint --gate freeze) -> frozen

# 誰のどの困りごとを解くのか。1文。実装判断に迷った時の最終的な拠り所になる。
intent: TBD

# 過剰実装を止める外周。draft を抜けるには最低1件必要。
# 「やらないと決めたこと」であって「将来やること」ではない。
out_of_scope:
  - TBD

# 確信が持てないまま進める判断。コード側に @assumption AS-001 を残して回収可能にする。
# owner と resolve_by が無い仮決めは、誰も確定させないまま本番に入る。
assumptions: []
#  - id: AS-001
#    statement: リンクの有効期限は15分とする
#    status: assumed # assumed | confirmed | rejected
#    owner: TBD
#    resolve_by: "YYYY-MM-DD"

# 人間の判断が要る未決事項。frozen にするには空にする（確定させるか assumptions へ降格）。
open_questions: []
#  - id: OQ-001
#    question: 未登録アドレスへの応答を200に統一してよいか（列挙攻撃対策 vs UX）
#    default: 200に統一する
#    raised: "YYYY-MM-DD"
#    status: open # open | resolved

# 受入基準。ハッピーパスだけ書くと必ず後で破綻するので、異常系を必ず1件以上入れる。
# then には外から観測できる結果を書く（返す/表示/保存/拒否…）。「対応する」「できる」は不可。
acceptance:
  - id: AC-001
    kind: functional # functional | nfr（nfr は then に数値としきい値が必須）
    given: TBD
    when: TBD
    then: TBD
    priority: must # must | should | could
    verify: test # test | manual | inspection
    status: pending # pending | pass | fail | blocked（record-verdict.ts 以外で書き換えない）
    attempts: 0
`;

mkdirSync(dir, { recursive: true });
writeFileSync(file, template);
console.log(`created ${specDir}/${slug}/requirements.yaml (${featId})`);
console.log(`\n次: TBD をすべて推測で埋め切ってから、影響度の高い順に最大3問だけ人間に確認する。`);
console.log(`確認: npx tsx scripts/spec-lint.ts ${specDir}/${slug} --questions\n`);
