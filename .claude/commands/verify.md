---
description: 実装の適合性を独立に検証し、結果を要件へ書き戻す
argument-hint: [feature-id または AC-XXX（省略時は全件）]
allowed-tools: Read, Grep, Glob, Bash, Task
---

`conformance-verify` スキルに従って検証ループを回してください。対象: $ARGUMENTS

1. `npx tsx scripts/trace-matrix.ts --base origin/main` を実行し、機械検査の error をすべて先に解消する
2. `verifier` サブエージェントを起動する。**このセッションで書いた実装の意図・経緯を verifier に渡さないこと。** 渡してよいのは requirements.yaml のパスと成果物のパスのみ
3. `scope-auditor` サブエージェントを起動する
4. 結果を「実装のバグ」「要件の欠落」「人間の判断待ち」に仕分けて報告する

自分で pass 判定を下さないでください。判定は verifier の仕事です。
