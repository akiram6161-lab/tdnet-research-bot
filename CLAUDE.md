# CLAUDE.md

TDnet適時開示通知・オンデマンドリサーチBot(仕様書: TDnet適時開示リサーチBot仕様書.pdf)。

## アーキテクチャ(2026-07-30改訂)

全開示をルールベースでスコアリングし、閾値(既定80点)以上のみ個別速報+
Claude Code(`claude -p`・サブスクリプション認証)で自動リサーチしてスレッドへ投稿。
閾値未満のTier 1/2は夕方ダイジェスト1通にまとめる。Claude API(従量課金)は使わない。

## 構成

- `src/tdnet/` — TDnet公式リストの取得(client)・HTMLパース(parser)・Tier判定(classifier)
- `src/slack/` — Slack投稿(client)・Block Kit整形(formatter)
- `src/research/runner.py` — Claude Codeサブプロセスによる自動リサーチ実行
- `src/state/repository.py` — state/state.json(atomic write・90日保持・ダイジェストキュー・日次リサーチカウンタ)
- `src/main.py` — CLIエントリポイント
- `config/disclosure_rules.yaml` — Tier判定ルール(除外ルールが常に優先)

## コマンド

- テスト: `uv run pytest`
- lint: `uv run ruff check src tests`
- type-check: `uv run mypy`

## 規約

- Python 3.12、全主要関数に型ヒント、構造化データはdataclasses
- 誤検知(Tier誤判定)を修正するときは必ず `tests/test_classifier.py` にregression testを追加する
- Tierルール・スコアの変更は `config/disclosure_rules.yaml` のみで行い、コード側のロジックは変えない
- リポジトリはパブリック。Actionsログも公開されるため、リサーチ本文・トークン・個人情報をログに出さない
- テスト中に実際のTDnet・Slack・Claude・EDINET DB・J-Quants APIを呼ばない(すべてmock)
- secretsをcommitしない。トークン・APIキーをログに出さない
- `state/state.json` の後方互換性を維持する(未知キーは保持)
- テストなしで本番挙動を変更しない
