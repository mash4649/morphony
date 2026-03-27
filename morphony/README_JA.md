# Morphony

## タグライン
CLI で動く、自律的なリサーチワークフローの基盤。

---

# デモ

```bash
uv sync --all-extras
uv run agent --help
uv run agent run "新製品ローンチ計画を調べる"
uv run agent status
uv run agent review evaluate <task_id>
```

基本の流れは CLI 中心です。

`goal` -> `run` -> `status / review / improve` -> `checkpoint / memory` -> `summary`

---

# 要約

Morphony は **LLM 呼び出しを、再現可能な自律リサーチワークフローに変えるための基盤** です。

従来:
- スクリプトが場当たり的
- タスク状態が見えない
- メモリが散らばる
- チェックポイントや復旧がない
- レビューや改善ループがない

Morphony では:
- 型付きのタスクライフサイクル
- `run` / `status` / `approve` / `reject` / `pause` / `resume` / `review` / `memory` などの CLI
- 永続化された Episodic / Semantic Memory
- チェックポイントと復旧
- ツール登録と実行制御
- 進捗表示と計画管理

例えるなら:
- **Git のように扱えるタスク状態管理**
- **自律リサーチ用の操作シェル**

---

# なぜ必要か

多くのエージェントデモは、プロンプトと応答だけで終わります。実運用ではそれでは足りません。

Morphony が必要なのは、次の要素が現実のワークフローでは必須だからです。

- 明示的な状態遷移
- 再開可能な実行
- 永続メモリ
- チェックポイント付き進捗
- 承認とエスカレーションの安全制御
- 実行後のレビューと改善
- すべての状態が CLI から見えること

ワークフローが複数ステップ、リトライ、承認、メモリ書き込みを含むようになると、単一のプロンプトでは扱い切れなくなります。

---

# できること

```text
Goal -> Lifecycle Manager -> Tools / Memory / Review -> Checkpoints -> Output
                │                │         │               │
                │                │         │               └─ 再開可能な状態
                │                │         └─ Episodic / Semantic の知識
                │                └─ search / fetch / analysis / report ツール
                └─ タスク状態 / 承認 / キュー / バジェット / イベント
```

Morphony が提供するもの:

- タスクライフサイクル管理
- キューオーケストレーション
- レビューと自己評価ループ
- Episodic / Semantic Memory ストア
- メモリ抽出、インポート、マイグレーション
- ツール登録と実行ラッパー
- チェックポイントと復旧
- `status` / `log` / `health` / `config` / `version` コマンド

---

# クイックスタート

## インストール

```bash
uv sync --all-extras
```

## 実行

```bash
uv run agent --help
```

## 例

```bash
uv run agent run "現在のプロジェクト進捗を要約する"
uv run agent status
uv run agent memory list
uv run agent review assess <task_id>
```

---

# 主な機能

- タスクライフサイクル、レビュー、メモリ、ツール、設定を扱う型付き CLI
- チェックポイントと resume 対応の永続タスク状態
- Episodic / Semantic Memory の保存、抽出、インポート
- レビュー、自己評価、改善ループ
- 保留タスクを開始する queue runner
- 承認付きのツール登録
- 実行時オーバーライド対応の設定読み込み
- 進捗報告とロードマップ文書

---

# アーキテクチャ

```text
CLI
 ├─ lifecycle
 │   ├─ run / status / approve / reject / pause / resume
 │   └─ checkpoints / queue / audit log / feedback
 ├─ memory
 │   ├─ episodic store
 │   ├─ semantic store
 │   └─ extraction / import / migration
 ├─ review
 │   ├─ assess
 │   ├─ evaluate
 │   └─ improve
 ├─ tools
 │   ├─ registry
 │   ├─ built-in tools
 │   └─ plugin tools
 └─ config
     └─ YAML config + runtime overrides
```

このプロジェクトは CLI ファーストです。状態はファイルに保存され、テストで検証されます。

---

# 想定ユースケース

- チェックポイント付きでリサーチタスクを進める
- 完了タスクをレビューして承認可否を判断する
- JSON / YAML のメモリバッチを取り込む
- キュー内の次タスクを自動で開始する
- 承認フロー付きでツールを登録する
- 設定とヘルスを端末から確認する

---

# 比較

| 機能 | 既存のやり方 | Morphony |
| ------- | ---------------- | -------- |
| タスク状態 | 場当たり | 型付きライフサイクル + キュー |
| 再開 | 手動 | チェックポイント対応 |
| メモリ | 散在 | Episodic / Semantic ストア |
| レビュー | なし or 手動 | review / evaluate / improve |
| ツール制御 | 直接呼び出し | registry + 承認フロー |
| 可視性 | 低い | `status` / `log` / `health` / `config` |

---

# エコシステム上の位置づけ

| カテゴリ | ツール | 役割 |
| -------- | ----- | -------- |
| エージェント基盤 | Morphony | 自律リサーチ向け CLI ワークフローエンジン |
| パッケージ管理 | `uv` | インストール、テスト、実行 |
| CLI フレームワーク | Typer | コマンド定義 |
| データ検証 | Pydantic | 設定とモデルの型検証 |
| 永続化 | SQLite ファイル | タスク状態、チェックポイント、メモリ |

---

# ロードマップ

- Phase 4: 信頼と自律性の拡大
- Phase 5: マルチドメイン対応
- Phase 6: マルチエージェント協調
- Phase 7: 集合知と自己進化

---

# ドキュメント

| テーマ | リンク |
| ----- | ---- |
| プロジェクトドキュメントの入口 | [docs/README.md](docs/README.md) |
| PLAN インデックス | [docs/PLAN/README.md](docs/PLAN/README.md) |
| 進捗報告 | [docs/PLAN/8.進捗報告.txt](docs/PLAN/8.進捗報告.txt) |
| ロードマップ | [docs/PLAN/7.ロードマップ.txt](docs/PLAN/7.ロードマップ.txt) |
| バージョンとライセンス | [VERSION.txt](VERSION.txt) |

---

# コントリビュート

変更は小さく、型付きで、テスト可能に保ってください。

- 変更範囲は狭くする
- 挙動変更時はテストを追加または更新する
- マージ前に関連する pytest を実行する
- ワークフローや CLI を変えたらドキュメントも更新する

---

# ライセンス

MIT License。

現在のバージョン: `0.1.0`
