---
description: 要件を draft から frozen へ遷移させ、実装に渡す
argument-hint: <slug>
allowed-tools: Read, Edit, Bash, Task
---

要件を凍結します。対象: $ARGUMENTS

1. `npx tsx scripts/spec-lint.ts specs/<slug> --gate freeze` を実行する
   freeze ゲートでは曖昧語・観測不能な then・異常系の不在・owner なし仮決めが error に昇格します
2. error が残っている場合、**freeze せずに** 何を確定させる必要があるかを報告する
3. `open_questions` が残っている場合、次のどちらかを人間に選ばせる
   - 今ここで確定させる
   - 既定値付きで `assumptions` に降格させ、owner と resolve_by を設定して進む
4. `spec-challenger` をまだ通していなければ通す
5. すべて解消したら `status: frozen` に変更し、実装フェーズへ渡す

freeze 後、要件を変更するには `/reconcile` を使います。実装の都合で要件を黙って書き換えるのは禁止です。
