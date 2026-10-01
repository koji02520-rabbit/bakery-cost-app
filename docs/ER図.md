# E-R図

`bakery/models.py` のテーブル構成です。DB は PostgreSQL で、テーブル名は `bakery_` ＋モデル名の小文字（例：`bakery_recipeitem`）です。
図のエンティティ名は、コードと照らし合わせやすいようにモデル名のままにしています。

## 図の読み方

- `||` は「ちょうど1」、`|o` は「0または1」、`o{` は「0以上」を表します
- 次の共通の列は、図では省いています
  - 業務データ（仕入先・食材カテゴリー・アレルゲン項目・食材・レシピ・商品）が持つ `company_id`（会社）。会社ごとにデータを分けるための列です
  - 同じく業務データが持つ `created_at` / `created_by_id` / `updated_at` / `updated_by_id`（作成・更新の日時とユーザー）
  - 履歴の `changed_by_id` などから User への線
- 主キーはすべて自動連番の `id` です。画面に出す ID（`FOOD-A001`、`RECIPE-001` など）は `code` 列に入れ、採番台帳（IdSequence）で発行します

## 全体

主なテーブルどうしのつながりです。列は分野別の図に載せています。

```mermaid
erDiagram
  Supplier ||--o{ Ingredient : "仕入れる"
  IngredientCategory ||--o{ Ingredient : "分類する"
  Ingredient }o--o{ AllergenItem : "含む"
  FoodComposition |o--o{ Ingredient : "栄養値の参照元"
  Ingredient ||--o{ IngredientPriceHistory : "価格の履歴"
  Recipe ||--o{ RecipeItem : "材料の行"
  Ingredient |o--o{ RecipeItem : "材料（食材）"
  Recipe |o--o{ RecipeItem : "材料（中間レシピ）"
  Recipe |o--o{ Product : "採用する"
  Product ||--o{ CostSnapshot : "原価の記録"
  Product ||--o{ RecipeAdoption : "採用の切替履歴"
```

## 組織・ユーザー

会社・店舗・ユーザーの所属と、会社ごとの設定です。User は Django 標準のユーザーです。

```mermaid
erDiagram
  Company ||--o{ Store : "持つ"
  Company ||--o{ Membership : "所属させる"
  User ||--o| Membership : "所属・権限"
  Store |o--o{ Membership : "所属店舗"
  Company ||--o| SystemSetting : "設定"
  Company ||--o{ IdSequence : "採番"
  Store ||--o{ StoreProductSetting : "販売設定"
  Product ||--o{ StoreProductSetting : "販売設定"

  Company {
    bigint id PK
    varchar name "会社名"
  }
  Store {
    bigint id PK
    bigint company_id FK
    varchar name "店舗名"
    varchar status "使用中／使用停止"
  }
  User {
    int id PK
    varchar username
  }
  Membership {
    bigint id PK
    int user_id FK,UK "1ユーザー1行"
    bigint company_id FK
    bigint store_id FK "空欄可"
    varchar role "一般／管理者"
  }
  SystemSetting {
    bigint id PK
    bigint company_id FK,UK "1社1行"
    decimal tax_rate "税率（0.08）"
    varchar unit_price_rounding "1g単価の丸め"
    varchar cost_rounding "商品原価の丸め"
    decimal default_yield_rate "中間レシピの歩留まり（0.98）"
  }
  IdSequence {
    bigint id PK
    bigint company_id FK
    varchar prefix "FOOD-A／RECIPE／ITEM／SUP"
    int next_value "次に発行する番号"
  }
  StoreProductSetting {
    bigint id PK
    bigint store_id FK
    bigint product_id FK
    bool is_sold "販売する"
  }
  Product {
    bigint id PK
  }
```

- IdSequence は（company_id, prefix）で一意です。削除したデータの番号を再利用しないよう、「最大値＋1」ではなく台帳で採番します
- StoreProductSetting は（store_id, product_id）で一意です。プロトタイプでは画面がありません

## 食材

食材と、その仕入先・カテゴリー・アレルゲン・栄養値の参照元です。

```mermaid
erDiagram
  Supplier ||--o{ Ingredient : "仕入れる"
  IngredientCategory ||--o{ Ingredient : "分類する"
  Ingredient ||--o{ Ingredient_allergens : ""
  AllergenItem ||--o{ Ingredient_allergens : ""
  FoodComposition |o--o{ Ingredient : "栄養値の参照元"
  Ingredient ||--o{ IngredientPriceHistory : "価格の履歴"

  Supplier {
    bigint id PK
    varchar code UK "SUP-001"
    varchar name "仕入先名"
    varchar contact_person "担当者"
    varchar phone "電話番号"
    varchar email "メールアドレス"
    varchar address "住所"
    text note "備考"
    varchar status "使用中／使用停止"
  }
  IngredientCategory {
    bigint id PK
    varchar code UK "A、B …"
    varchar name "カテゴリー名"
    int sort_order "並び順"
    varchar status "使用中／使用停止"
  }
  Ingredient {
    bigint id PK
    varchar code UK "FOOD-A001"
    varchar name "食材名"
    bigint category_id FK
    bigint supplier_id FK
    decimal purchase_weight_g "購入重量（1g以上）"
    decimal purchase_price "購入価格（0円以上）"
    varchar legacy_code "旧コード"
    text additives "添加物"
    varchar origin_country "原産国"
    text note "備考"
    varchar status "使用中／使用停止"
    varchar allergen_status "未確認／確認済み"
    int allergen_confirmed_by_id FK "確認者"
    datetime allergen_confirmed_at "確認日時"
    decimal energy_kcal "熱量（100gあたり）"
    decimal protein_g "たんぱく質"
    decimal fat_g "脂質"
    decimal carbohydrate_g "炭水化物"
    decimal salt_g "食塩相当量"
    bigint nutrition_food_id FK "空欄可"
    varchar nutrition_note "栄養値の出どころ"
  }
  AllergenItem {
    bigint id PK
    varchar name UK "アレルゲン名"
    varchar kind "義務／推奨"
    int sort_order "並び順"
    varchar status "使用中／使用停止"
  }
  Ingredient_allergens {
    bigint id PK
    bigint ingredient_id FK
    bigint allergenitem_id FK
  }
  FoodComposition {
    bigint id PK
    varchar food_number UK "食品番号"
    varchar group "食品群"
    varchar name "食品名"
    decimal energy_kcal "可食部100gあたり"
    decimal protein_g
    decimal fat_g
    decimal carbohydrate_g
    decimal salt_g
  }
  IngredientPriceHistory {
    bigint id PK
    bigint ingredient_id FK
    decimal purchase_weight_g "購入重量"
    decimal purchase_price "購入価格"
    int changed_by_id FK
    datetime changed_at "変更日時"
  }
```

- 図の `UK` は「会社の中で一意」です（例：Ingredient は（company_id, code）で一意）。FoodComposition の `food_number` だけは全体で一意です
- Ingredient_allergens は、食材とアレルゲン項目の多対多を表す中間テーブルです（Django の ManyToManyField が作るもの）
- FoodComposition（日本食品標準成分表）は会社に属さない参照データです
- 1g単価は保存せず、購入価格 ÷ 購入重量 で毎回計算します
- IngredientPriceHistory には、登録時の価格も1行目として記録します

## レシピ・商品

レシピは、材料として食材か中間レシピ（生地・クリームなど）のどちらかを持ちます。中間レシピがさらに中間レシピを使うことで、多階層になります。

```mermaid
erDiagram
  Recipe ||--o{ RecipeItem : "材料の行"
  Ingredient |o--o{ RecipeItem : "材料（食材）"
  Recipe |o--o{ RecipeItem : "材料（中間レシピ）"
  Recipe |o--o{ Recipe : "コピー元"
  Recipe ||--o{ RecipePhoto : "写真"
  Recipe |o--o{ Product : "採用する"
  Product ||--o{ ProductPhoto : "写真"

  Recipe {
    bigint id PK
    varchar code UK "RECIPE-001"
    varchar name "レシピ名"
    int version "バージョン"
    varchar kind "中間／最終商品"
    varchar recipe_type "生地・クリームなど（中間のみ）"
    decimal yield_rate "歩留まり（0より大きく1以下）"
    bigint copied_from_id FK "空欄可"
    text preparation "下処理"
    text procedure "手順"
    text memo "メモ"
    varchar status "使用中／使用停止"
  }
  RecipeItem {
    bigint id PK
    bigint recipe_id FK "どのレシピの行か"
    bigint ingredient_id FK "食材（どちらか一方）"
    bigint material_recipe_id FK "中間レシピ（どちらか一方）"
    decimal quantity_g "使用量（0より大きい）"
    varchar step_label "工程"
    int sort_order "表示順"
  }
  Ingredient {
    bigint id PK
  }
  RecipePhoto {
    bigint id PK
    bigint recipe_id FK
    varchar image "写真"
    varchar thumbnail "縮小画像"
    varchar caption "説明"
    int sort_order "1枚目が代表写真"
    int created_by_id FK
    datetime created_at
  }
  Product {
    bigint id PK
    varchar code UK "ITEM-001"
    varchar name "商品名"
    int price_excluding_tax "税抜売価"
    bigint adopted_recipe_id FK "最終商品レシピのみ・空欄可"
    varchar status "使用中／使用停止"
  }
  ProductPhoto {
    bigint id PK
    bigint product_id FK
    varchar image "写真"
    varchar thumbnail "縮小画像"
    varchar caption "説明"
    int sort_order "1枚目が代表写真"
    int created_by_id FK
    datetime created_at
  }
```

- RecipeItem は、`ingredient_id` と `material_recipe_id` の**ちょうど一方だけ**に値が入ります（DB の CHECK 制約で保証）
- `recipe_type` は中間レシピのときだけ入り、最終商品レシピでは空です（CHECK 制約）
- 循環参照（A の材料に B、B の材料に A）は、DB ではなく保存前の処理で止めます
- 原価・アレルゲン・栄養成分は保存せず、RecipeItem を下までたどって毎回計算します
- 商品写真がない場合は、採用レシピの代表写真を表示します

## 履歴・監査

変更の記録です。原価に関わる変更（食材価格の変更・採用レシピの切替・中間レシピの置き換え）では、影響を受けた商品ごとに原価記録（CostSnapshot）を残します。

```mermaid
erDiagram
  Company ||--o{ AuditLog : ""
  AuditLog |o--o{ CostSnapshot : "きっかけの変更"
  Product ||--o{ CostSnapshot : "原価の記録"
  Recipe |o--o{ CostSnapshot : "その時の採用レシピ"
  Product ||--o{ RecipeAdoption : "切替履歴"
  Recipe |o--o{ RecipeAdoption : "切替前"
  Recipe ||--o{ RecipeAdoption : "切替後"
  Recipe ||--o{ RecipeReplacement : "置き換え前"
  Recipe ||--o{ RecipeReplacement : "置き換え後"

  AuditLog {
    bigint id PK
    bigint company_id FK
    int user_id FK
    datetime at "日時"
    varchar action "操作"
    varchar target_type "対象の種類"
    bigint target_id "対象の内部ID"
    varchar target_label "対象"
    json before "変更前"
    json after "変更後"
  }
  CostSnapshot {
    bigint id PK
    bigint product_id FK
    bigint recipe_id FK "空欄可"
    decimal cost "原価"
    int price_excluding_tax "税抜売価"
    varchar reason "価格変更／採用切替／置き換え"
    bigint audit_log_id FK "空欄可"
    datetime taken_at "記録日時"
  }
  RecipeAdoption {
    bigint id PK
    bigint product_id FK
    bigint old_recipe_id FK "空欄可"
    bigint new_recipe_id FK
    decimal old_cost
    decimal new_cost
    int changed_by_id FK
    datetime changed_at
  }
  RecipeReplacement {
    bigint id PK
    bigint company_id FK
    bigint old_recipe_id FK
    bigint new_recipe_id FK
    int changed_by_id FK
    datetime changed_at
  }
  Product {
    bigint id PK
  }
  Recipe {
    bigint id PK
  }
```

- AuditLog の対象は、`target_type`（種類）と `target_id`（内部ID）の組で指します。どのテーブルの行も記録できるよう、外部キーにはしていません
- 原価率は保存せず、CostSnapshot の `cost` と `price_excluding_tax` から計算します

## 削除のルール

- ほかから参照されているマスタ・食材・レシピは、物理削除できません（外部キーは `on_delete=PROTECT`）。使わなくなったものは「使用停止」にします
- 明細・写真・価格履歴・原価記録など、親に付属するデータは、親と一緒に削除されます（`CASCADE`）
- 作成者・更新者などのユーザーへの参照は、ユーザーを削除すると空欄になります（`SET_NULL`）
