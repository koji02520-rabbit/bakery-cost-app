# レシピ・原価管理アプリ（プロトタイプ）

パン・食品製造の現場向けの、レシピ・原価・食材・販売価格の管理アプリです。
スマートフォンとPCのどちらでも使えます。要件は `spec.md` にまとめています。

**このリポジトリは試用のためのプロトタイプです。** 試用データの仕入先・価格はすべて架空です。

## できること

- 食材：購入重量・価格から1g単価を自動計算、アレルゲン、栄養成分（日本食品標準成分表から選択）
- レシピ：中間レシピ（生地・クリームなど）と最終商品レシピ、多階層、材料の検索・追加、並べ替え、写真
- 原価：歩留まり98%、商品原価（小数第1位へ切り上げ）、税込売価（8%・切り捨て）、原価率
- 影響確認：食材価格の変更・採用レシピの切替・中間レシピの置き換えの前に、商品ごとの原価の変化を表示
- アレルゲンと栄養成分：食材 → 中間レシピ → 商品へ自動で集計、由来の表示、栄養成分表示（推定値）
- 履歴：誰が・いつ・何を変えたかを記録

## 試してみる（GitHub Codespaces・インストール不要）

1. このリポジトリのページで **Code → Codespaces → Create codespace on main** を押す
2. 数分待つと、試用データの入ったアプリが起動する
3. `demo_admin`（管理者）または `demo_staff`（一般）でログイン。パスワードは `demo-bakery-2026`

詳しい手順と試してほしいことは [docs/試用ガイド.md](docs/試用ガイド.md) にあります。
GitHub の個人アカウントの無料枠（毎月、2コアの環境で約60時間）の範囲で使えます。

## 構成

- Python 3.12 / Django 6.0
- PostgreSQL
- 画面：Django テンプレート ＋ htmx（並べ替えは SortableJS）

| 場所 | 内容 |
|---|---|
| `bakery/services/costing.py` | 原価計算エンジン（DB・画面から独立） |
| `bakery/services/nutrition.py` | 栄養成分の計算と栄養成分表示の丸め |
| `bakery/services/sources.py` | DB の内容を計算エンジンに渡す |
| `bakery/services/recipes.py` | レシピの操作ルール（使用中の保護、循環参照、置き換えと影響確認） |
| `bakery/services/products.py` | 商品の原価・税込売価・原価率、採用レシピの切替 |
| `bakery/services/pricing.py` | 食材価格の変更と影響確認 |
| `bakery/services/photos.py` | 写真（縮小・向き補正・付帯情報の除去） |
| `bakery/services/audit.py` | 変更履歴（AuditLog）の記録 |
| `bakery/models.py` | データモデル（spec.md 第64項） |
| `bakery/views/` | 画面の処理（食材・レシピ・商品・価格変更・写真） |
| `bakery/permissions.py` | ログイン・所属・管理者のチェック |
| `bakery/templates/`, `bakery/static/` | 画面（htmx・SortableJS は `static/bakery/vendor/` に同梱） |
| `bakery/management/commands/` | 初期データ・成分表の取り込み・試用データ |
| `data/mext_food_composition_8th.xlsx` | 日本食品標準成分表（八訂）増補2023年（文部科学省） |
| `.devcontainer/` | GitHub Codespaces 用の設定 |
| `bakery/tests/` | 自動テスト |

## 自分のPCで動かす（Windows）

1. PostgreSQL をインストールし、`bakery` という名前のデータベースを作る
2. このフォルダで以下を実行

```sh
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
copy .env.example .env   # .env を開いて DATABASE_URL のパスワードを書き換える
.venv/Scripts/python manage.py migrate
.venv/Scripts/python manage.py import_food_composition   # 栄養計算用の成分表を取り込む
.venv/Scripts/python manage.py setup_demo                # 試用データ（demo_admin / demo_staff）
.venv/Scripts/python manage.py runserver
```

実際に使う場合は `setup_demo` の代わりに、`createsuperuser` で管理者を作ってから
`setup_initial_data --company "会社名" --store "店舗名" --admin ユーザー名` を実行します。

## テスト

```sh
.venv/Scripts/python manage.py test
```

## 出典・ライセンス

- 栄養値：日本食品標準成分表（八訂）増補2023年（文部科学省）を加工して作成
- 同梱している外部の部品とそのライセンスは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照
- このアプリ自体のソースコードは、閲覧・試用のために公開しています（オープンソースとしての利用許諾はしていません）
