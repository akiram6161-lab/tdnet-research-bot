# tdnet-research-bot

TDnet適時開示通知・オンデマンドリサーチBot。
TDnetを15分ごとに監視し、Tier 1・Tier 2の開示のみSlackへ速報する。
Slackスレッドで「リサーチ」と返信すると、TDnet・EDINET DB・J-QuantsのデータをClaude APIで分析したレポートを同スレッドに投稿する(Phase 2/3で実装)。

## 実装状況

| Phase | 内容 | 状態 |
|---|---|---|
| Phase 1 | 速報MVP(TDnet取得・Tier判定・Slack速報・重複防止・GitHub Actions・dry-run) | ✅ 実装済み |
| Phase 2 | Slackリサーチコマンド(スレッドポーリング・コマンド検知) | 未着手 |
| Phase 3 | リサーチ処理(EDINET DB・J-Quants・Claude API分析) | 未着手 |
| Phase 4 | Claude Code保守(claude-code.yml・Issue/PR運用) | 未着手 |

## データソース

- **TDnet**: 公開APIが存在しないため、公式開示リストページ
  `https://www.release.tdnet.info/inbs/I_list_NNN_YYYYMMDD.html` を取得する
  (認証不要・既存 japan-equities パイプラインで動作実績のある方式)。
  開示IDはPDF URLに含まれる18桁の安定IDを使用する。
- **EDINET DB / J-Quants / Claude API**: Phase 3で接続。

## ローカル実行

```bash
uv venv --python 3.12 && uv pip install -e . --group dev
cp .env.example .env   # 必要な値を設定

# 接続確認(TDnetは認証不要なのでそのまま動く)
python -m src.main --check-connections

# dry-run: 取得・Tier分類・投稿予定内容の表示のみ。Slack投稿/state変更なし
python -m src.main --dry-run

# タイトル1件の分類テスト
python -m src.main --classify-title "業績予想の修正に関するお知らせ"

# Slack投稿テスト(SLACK_BOT_TOKEN / SLACK_CHANNEL_ID が必要)
python -m src.main --test-slack

# 本番相当(監視時間内のみ動作し、Tier 1/2をSlackへ投稿)
python -m src.main
```

## テスト・品質チェック

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

外部APIはすべてmockしており、テストで実際のTDnet/Slack等は呼ばない。

## Slack App設定

[slack-app-manifest.yaml](slack-app-manifest.yaml) を
https://api.slack.com/apps → **Create New App → From an app manifest** に貼り付けて作成し、
ワークスペースへインストール後、Bot User OAuth Token(xoxb-)を取得する。
`#tdnet-alerts` を作成してBotを `/invite` すること。

Slackトークン未設定の間は、定期実行されても何もせず正常終了する(dry-runは利用可能)。

## GitHub Actions設定

1. リポジトリをGitHubへpush
2. **Settings → Secrets and variables → Actions** に以下を登録:
   - `SLACK_BOT_TOKEN`(xoxb-)… chat:write 権限。Botを対象チャンネルに招待すること
   - `SLACK_CHANNEL_ID`(例: `C0123456789`)
   - `SLACK_USER_TOKEN` / `SLACK_ALLOWED_USER_IDS`(Phase 2で使用)
   - `ANTHROPIC_API_KEY` / `EDINET_DB_API_KEY` / `JQUANTS_API_KEY`(Phase 3で使用)
3. `.github/workflows/monitor.yml` が15分ごとに起動する。JSTの監視時間
   (平日07:45〜20:00)外はアプリが即終了する。
4. stateに変更があった場合のみ `state/state.json` をcommitする。

### 無料枠・課金防止

- 監視時間帯のみcronを起動し、時間外はアプリ側でも即終了(数秒で完了)
- 想定実行回数: 約50回/営業日 × 22営業日 ≒ 1,100回/月(1回あたり1分未満)
- 超過課金を防ぐには **Settings → Billing → Spending limits** で
  Actionsの支出上限を $0 に設定する(無料枠を使い切ると実行が止まるだけで課金されない)

## 設定ファイル

- `config/disclosure_rules.yaml` — Tier判定ルール(除外 > Tier 1 > Tier 2 の優先度)
- `config/portfolio.csv` — 保有銘柄(`security_code,company_name,shares,average_cost,active`)
- `config/watchlist.csv` — 監視銘柄(`security_code,company_name,label,active`)

証券コードは4桁に正規化される。該当銘柄の通知には 💼 PORTFOLIO / 👀 WATCHLIST ラベルが付く。

## 状態管理

`state/state.json` に処理済み開示ID・スレッド対応・最終成功時刻を保持する
(atomic write・90日保持・変更がない場合はcommitしない)。

## 既知の制約(Phase 1時点)

- 概要は生成せず「概要:詳細は開示資料をご確認ください。」を常に表示
  (推測で概要を作らない方針。PDF解析はPhase 3)
- 英文開示は日本語版と同一コード・同一時刻の場合のみ重複として除外
- 取引所区分(exchange)はリストページから取得できないため未設定
