# Distill Nexus (`dn`)

雑多なファイルを **内容で分類・整理** し、そこに含まれるドメイン知識を **AI がそのまま読める知識ベース**
（Markdown + JSON）に蒸留する CLI です。Windows / Linux / macOS で同じコードが動きます。

- 既定はドライラン。ファイルが動くのは `dn apply` / `dn run --apply` のときだけ
- 元ファイルの内容は書き換えない。移動は `manifest.json` に記録され、`dn undo` で戻せる
- 分類・知識抽出の判断はすべて CLI 内のプロンプトで行う（Claude Code からも人間からも同じ結果）

要件は `specs/*/requirements.yaml`（FEAT-001〜008）が単一の正です。

## インストール

```bash
uv tool install .          # または: pipx install .
export ANTHROPIC_API_KEY=...
dn --version
```

開発時: `uv sync --group dev` → `uv run pytest`

## 使い方

```bash
dn init ~/Documents/inbox                       # .dn/config.yaml と taxonomy.yaml の雛形
dn plan ~/Documents/inbox --propose-taxonomy    # taxonomy が無ければカテゴリ案を .dn/taxonomy.proposed.yaml に出す
dn plan ~/Documents/inbox                       # 抽出 + 分類 + 移動計画（何も動かない）
dn apply ~/Documents/inbox                      # 確認プロンプトの後に実行（非TTYでは --yes 必須）
dn distill ~/Documents/inbox                    # organized/_knowledge/ を生成
dn run ~/Documents/inbox --apply --yes          # 全フェーズ一括
dn undo ~/Documents/inbox [--run-id ID]         # 直近（または指定）の apply を巻き戻す
dn status ~/Documents/inbox --json              # キャッシュ状況・未処理件数・推定コスト
dn export ~/Documents/inbox --format md|jsonl|zip
```

主なオプション: `--out DIR` `--copy` `--rename` `--dedupe move|trash|keep` `--model` `--synth-model`
`--concurrency N` `--max-cost USD` `--lang ja|en|auto` `--json` `--interactive` `--images`
`--dry-llm`（API を呼ばずスキーマ準拠のダミー。配線確認用）`-v/-vv`

終了コード: 0 成功 / 1 一般エラー / 2 設定・引数 / 3 一部ファイル失敗 / 4 コスト上限超過

## LLM 呼び出しを減らす設定（`.dn/config.yaml`）

```yaml
classify_batch_size: 10     # 1回の分類呼び出しにまとめるファイル数（本文合計 24,000 文字まで）
images: false               # true / --images で画像とスキャン PDF 頁を Claude vision に送る（既定は送らない）
rules:                      # LLM を呼ばずに決める分類（gitignore 構文、最初に一致したもの）
  - glob: "*.log"
    category: misc
  - glob: "contracts/**"
    category: contracts
```

## パイプライン

| フェーズ | 出力（`.dn/cache/`） | LLM |
|---|---|---|
| scan | `inventory.jsonl`（パス・サイズ・mtime・blake2b） | なし |
| extract | `text/<hash>.md`（正規化 Markdown + frontmatter） | 画像・スキャン PDF 頁のみ |
| classify | `labels.jsonl` | あり（ハッシュ単位で1回） |
| plan | `plan.json` | なし |
| apply | 移動 + `organized/manifest.json` | なし |
| distill | `facts/<hash>.json` | あり（見出し単位チャンク） |
| synthesize | `organized/_knowledge/` | あり |

`_knowledge/` の入口は `INDEX.md`。`overview.md` `glossary.md` `topics/*.md` `entities.json`
`facts.jsonl` `relations.json` `chunks.jsonl`（RAG 用）`sources.md` `open-questions.md` を毎回再生成します
（`topics/*.md` の `<!-- manual -->`〜`<!-- /manual -->` は保持）。

## テスト

```bash
uv run pytest --cov              # LLM は偽クライアント / --dry-llm で置き換え
DN_LLM_RECORD=1 dn run tests/fixtures/sample_tree   # 実 API 応答を tests/fixtures/llm/ に録画
DN_LLM_REPLAY=1 uv run pytest    # 録画を再生（録画に無い呼び出しはエラー）
python scripts/make_sample_tree.py   # サンプルツリー（44ファイル）を再生成
```

## Claude Code から使う

`.claude/skills/dn/SKILL.md` が「このフォルダを整理して」を `dn plan --json` → 要約提示 → 承認 → `dn apply --yes --json`
の手順に落とします。

## 適合性ループ（conformance-kit）

```bash
npx tsx scripts/spec-lint.ts             # 要件の機械検査
npx tsx scripts/trace-matrix.ts          # AC ⇄ テスト / @covers / @assumption のトレース
npm run gate                             # spec-lint + trace + pytest
```

pytest のテストは関数直後の docstring に AC-ID を書きます（例: `"""AC-001: ..."""`）。
注意: conformance-kit の pytest 検出は戻り値注釈付きのシグネチャ（`def test_x() -> None:`）を認識しないため、
テスト関数には戻り値注釈を付けていません。
