---
description: 検証で出た差分を要件ファイルへ還元する
argument-hint: [AC-XXX または blocked 全件]
allowed-tools: Read, Edit, Bash
---

検証結果と要件のズレを解消します。対象: $ARGUMENTS

まず `npx tsx scripts/trace-matrix.ts --json` で現状を取得し、fail / blocked / UNTRACED_CHANGE の各項目を次のどれかに分類してください。分類の根拠を必ず1行添えること。

- **実装のバグ** → 要件は触らない。修正内容だけ提案する
- **要件の欠落** → AC を追加、または `then` を具体化する。変更前後を並べて提示し、**人間の承認を得てから** requirements.yaml を編集する。編集したら
  `npx tsx scripts/record-decision.ts --spec <slug> --ref AC-XXX --reason "..."` で変更理由を記録する
- **スコープの誤り** → `out_of_scope` に追記する。追記したら
  `npx tsx scripts/record-decision.ts --spec <slug> --ref out_of_scope --reason "..."` で理由を記録する
- **仮定の確定** → `assumptions` の該当項目を `assumed` → `confirmed` にし、コード側の `@assumption` コメントを削除する。確定させたら
  `npx tsx scripts/record-decision.ts --spec <slug> --ref AS-XXX --reason "..."` で理由を記録する

要件を弱めることで検証を通すのは禁止です。ACの `then` から条件を削る提案をする場合、削る理由と、それによって守られなくなる挙動を明示してください。

編集後は `npx tsx scripts/trace-matrix.ts` を再実行し、整合を確認します。
