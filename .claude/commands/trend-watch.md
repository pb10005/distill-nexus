---
description: AI駆動開発トレンドを調査し、conformance-kit自身の採用可否判断をTREND_BACKLOG.mdに記録する
argument-hint: [調査したい技術名（省略可）]
allowed-tools: WebSearch, WebFetch, Bash, AskUserQuestion
---

conformance-kit自身のツール領域（spec駆動開発・エージェント検証・分類/判定特化モデル・coding agentエコシステム）に関連するAI駆動開発のトレンドを調査します。対象: $ARGUMENTS

対象範囲は conformance-kit 自身のツール領域に関連するものに限定してください。AI業界全般のニュース監視は対象外です（`specs/trend-watch/requirements.yaml` の out_of_scope を参照）。

## 手順

1. WebSearch / WebFetch で調査する。
2. 発見した技術について、以下を整理する:
   - 何ができるか / 何が変わるか
   - conformance-kitのどの箇所（機能）に関連しうるか
   - 導入した場合の具体的なコスト（依存追加・シークレット管理・CIへの影響・信頼境界の拡張など）
3. 発見事項と評価案（`adopt`/`defer`/`reject` のいずれにすべきか、その理由）を `AskUserQuestion` で人間に提示し、**承認を得てから** `record-trend.ts` を呼ぶ。人間の承認を得るまでは記録しない。
4. 承認が得られたら次を実行する:
   ```bash
   npx tsx scripts/record-trend.ts --tech "<名称>" --decision adopt|defer|reject --reason "..." [--revisit "..."]
   ```
   - `--decision` が `defer`/`reject` の場合、`--revisit`（再検討条件）を必ず指定する
   - `--decision` が `adopt` の場合、`--revisit` は省略できるが、将来の再評価条件があれば指定する

## specs/<slug>/decisions.md との使い分け

判断が特定1つのspecの `requirements.yaml`（AC/AS/out_of_scope）の変更を伴う場合は、そのspecの `decisions.md`（`record-decision.ts`）に記録する。判断がconformance-kit自身のツール構成に関するもので、特定のspecの要件変更を伴わない場合は `TREND_BACKLOG.md`（`record-trend.ts`）に記録する。両方に該当する場合は両方に記録してよい。

## 禁止事項

- 人間の承認を得ずに `record-trend.ts` を呼ぶこと
- 発見した技術を、承認なしにこのセッション内で実装・導入すること
- conformance-kit自身のツール領域と無関係な一般的なAIニュースを調査対象にすること
