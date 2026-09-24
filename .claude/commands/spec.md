---
description: 新機能の要件を検証可能な形に確定させる
argument-hint: <slug> "機能の説明"
allowed-tools: Read, Write, Edit, Bash, Task
---

`spec-intake` スキルに従って要件を確定させてください。対象: $ARGUMENTS

守ること:

1. `npx tsx scripts/spec-init.ts <slug> --title "..."` で骨組みを作る
2. **人間に質問する前に、TBD をすべて推測で埋め切る。** 白紙を埋めさせるのではなく、埋めたものを否定してもらう
3. `npx tsx scripts/spec-lint.ts specs/<slug> --questions` で機械的に潰せる欠陥を先に潰す
4. `spec-challenger` サブエージェントで異常系・境界・矛盾を突く
5. 残った未決のうち、影響度×不可逆性が高い**最大3問だけ**を、既定値付きで human に提示する
   形式: 「A にします。違う場合は B か C」。沈黙したら A で進む
6. 回答を confirmed / assumed / open に振り分ける

質問リストの丸投げは禁止です。10項目の箇条書きを投げた時点でこのスキルは失敗しています。
