<!-- conformance-kit -->
# 適合性ループの規約（このリポジトリで実装するときに必ず守る）

要件は `specs/**/requirements.yaml` が単一の正。実装・テストはこれに従属する。

## 着手時（要件が無い場合）

`specs/**/requirements.yaml` が無い機能の実装を始めてはいけない。`/spec <slug>` で要件を確定させる。
要件を人間に書かせず、こちらが推測で埋め切ってから、影響度の高い最大3問だけを既定値付きで確認する。
`status: frozen` になるまで実装に入らない。

## 実装時

- 変更するファイルの先頭コメントに `@covers AC-XXX` を書く（複数可: `@covers AC-001, AC-002`）。
  無いファイルは逆方向トレースで「仕様外実装」として検出される。
- 要件に無い前提で実装が進む場合、勝手に決めない。`requirements.yaml` の `assumptions` に `AS-XXX` を起票し、
  コード側に `@assumption AS-XXX` を残す。片方だけだと検出される。
- `out_of_scope` に触れる実装が必要になったら、実装せず人間に確認する。

## テスト時

- `it()` / `test()` の**タイトル**に AC-ID を含める: `it("AC-001: 未登録でも200を返す", ...)`
  コメントや `describe` では数えない。`it.skip` はカバーではなく `SKIPPED_TEST` エラーになる。
- ACの `then` に「かつ」があれば、その全部をアサーションする。半分だけ見たテストは verifier が fail にする。
- テストを弱めて通すのは禁止。ACが間違っているなら `/reconcile` で要件側を直す。

## 完了判断

「テストが通った」は完了ではない。`/verify` を回し、全ACが証拠付き pass か、blocked として人間に返っている状態が完了。
自分で `status: pass` を書かない。判定は `verifier` サブエージェントが `scripts/record-verdict.ts` 経由で記録する。

```bash
/spec <slug>                                         # 要件を確定させる
/freeze <slug>                                       # draft -> frozen
npx tsx scripts/spec-lint.ts specs/<slug> --questions # 要件の機械検査
npx tsx scripts/trace-matrix.ts --base origin/main   # 機械検査
/verify                                              # 独立検証 + 逆方向監査
/reconcile                                           # 差分を要件へ還元
npm run gate                                         # マージ前ゲート
```
