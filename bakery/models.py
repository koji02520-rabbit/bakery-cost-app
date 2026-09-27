"""データモデル（spec.md 第64項）。

方針:
- 内部主キーは自動連番。画面に出すID（FOOD-A001 等）は code 列で、IdSequence が採番する
- 業務データは company を持つ（将来の複数会社対応）
- 金額・重量は DecimalField。1g単価や「使用中かどうか」は保存せず都度計算する
- 参照されているデータは物理削除しない（on_delete=PROTECT）。使用停止で対応する
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Q

from bakery.services.costing import CostRounding, UnitPriceRounding, cost_rate_percent, unit_price

# --------------------------------------------------------------------------
# 共通
# --------------------------------------------------------------------------


class Status(models.TextChoices):
    ACTIVE = "active", "使用中"
    STOPPED = "stopped", "使用停止"


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("作成日時", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="作成者", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField("更新日時", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="更新者", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        abstract = True


class CompanyOwnedModel(TimeStampedModel):
    company = models.ForeignKey("Company", verbose_name="会社", on_delete=models.PROTECT)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------
# 組織・ユーザー
# --------------------------------------------------------------------------


class Company(models.Model):
    name = models.CharField("会社名", max_length=100)

    class Meta:
        verbose_name = verbose_name_plural = "会社"

    def __str__(self):
        return self.name


class Store(models.Model):
    company = models.ForeignKey(Company, verbose_name="会社", on_delete=models.PROTECT, related_name="stores")
    name = models.CharField("店舗名", max_length=100)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "店舗"

    def __str__(self):
        return self.name


class Role(models.TextChoices):
    GENERAL = "general", "一般ユーザー"
    ADMIN = "admin", "管理者"


class Membership(models.Model):
    """ユーザーの所属と権限。将来の中間権限は Role に追加する（第4項）。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, verbose_name="ユーザー", on_delete=models.CASCADE, related_name="membership"
    )
    company = models.ForeignKey(Company, verbose_name="会社", on_delete=models.PROTECT)
    store = models.ForeignKey(Store, verbose_name="所属店舗", on_delete=models.PROTECT, null=True, blank=True)
    role = models.CharField("権限", max_length=10, choices=Role.choices, default=Role.GENERAL)

    class Meta:
        verbose_name = verbose_name_plural = "所属・権限"

    def __str__(self):
        return f"{self.user} / {self.company} / {self.get_role_display()}"

    @property
    def is_admin(self):
        return self.role == Role.ADMIN


class SystemSetting(models.Model):
    company = models.OneToOneField(Company, verbose_name="会社", on_delete=models.CASCADE, related_name="setting")
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=Decimal("0.08"),
                                   help_text="8% は 0.08")
    unit_price_rounding = models.CharField(
        "1g単価の丸め", max_length=20, default=UnitPriceRounding.NONE,
        choices=[(UnitPriceRounding.NONE.value, "丸めない"),
                 (UnitPriceRounding.EXCEL_ROUNDUP_2.value, "小数第2位へ切り上げ（既存Excelと同じ）")],
    )
    cost_rounding = models.CharField(
        "商品原価の丸め", max_length=20, default=CostRounding.CEIL_1,
        choices=[(CostRounding.CEIL_1.value, "小数第1位へ切り上げ"), (CostRounding.NONE.value, "丸めない")],
        help_text="丸めた値を正式な商品原価とし、原価率・原価記録にも使う",
    )
    default_yield_rate = models.DecimalField("中間レシピの歩留まり", max_digits=5, decimal_places=4,
                                             default=Decimal("0.98"))

    class Meta:
        verbose_name = verbose_name_plural = "システム設定"

    def __str__(self):
        return f"{self.company} の設定"

    @classmethod
    def for_company(cls, company):
        obj, _ = cls.objects.get_or_create(company=company)
        return obj


class IdSequence(models.Model):
    """表示用IDの採番台帳。削除後も番号を再利用しないため、最大値＋1ではなく台帳で管理する（第8項）。"""

    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    prefix = models.CharField(max_length=20)  # FOOD-A / RECIPE / ITEM / SUP
    next_value = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = verbose_name_plural = "採番台帳"
        constraints = [models.UniqueConstraint(fields=["company", "prefix"], name="uniq_sequence_prefix")]

    WIDTH = 3

    @classmethod
    def format(cls, prefix, value):
        return f"{prefix}{value:0{cls.WIDTH}d}" if prefix.startswith("FOOD-") else f"{prefix}-{value:0{cls.WIDTH}d}"

    @classmethod
    def peek(cls, company, prefix):
        """発行予定ID（第62項4）。確定時に変わることがある。"""
        row = cls.objects.filter(company=company, prefix=prefix).first()
        return cls.format(prefix, row.next_value if row else 1)

    @classmethod
    def issue(cls, company, prefix):
        """正式なIDを1つ発行する。同時登録でも重複しないよう行ロックする。"""
        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(company=company, prefix=prefix)
            value = row.next_value
            row.next_value = value + 1
            row.save(update_fields=["next_value"])
        return cls.format(prefix, value)


# --------------------------------------------------------------------------
# マスタ
# --------------------------------------------------------------------------


class Supplier(CompanyOwnedModel):
    code = models.CharField("仕入先ID", max_length=20, editable=False)
    name = models.CharField("仕入先名", max_length=100)
    contact_person = models.CharField("担当者", max_length=100, blank=True)
    phone = models.CharField("電話番号", max_length=30, blank=True)
    email = models.EmailField("メールアドレス", blank=True)
    address = models.CharField("住所", max_length=200, blank=True)
    note = models.TextField("備考", blank=True)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "仕入先"
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_supplier_code")]

    def __str__(self):
        return self.name


class IngredientCategory(CompanyOwnedModel):
    code = models.CharField("分類コード", max_length=5, help_text="A、B … 食材IDの FOOD-A001 の A")
    name = models.CharField("カテゴリー名", max_length=50)
    sort_order = models.PositiveIntegerField("並び順", default=0)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "食材カテゴリー"
        ordering = ["sort_order", "code"]
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_category_code")]

    def __str__(self):
        return f"{self.code}. {self.name}"

    @property
    def id_prefix(self):
        return f"FOOD-{self.code}"


class AllergenKind(models.TextChoices):
    MANDATORY = "mandatory", "義務"
    RECOMMENDED = "recommended", "推奨"


class AllergenItem(CompanyOwnedModel):
    name = models.CharField("アレルゲン名", max_length=50)
    kind = models.CharField("区分", max_length=15, choices=AllergenKind.choices)
    sort_order = models.PositiveIntegerField("並び順", default=0)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "アレルゲン項目"
        ordering = ["sort_order"]
        constraints = [models.UniqueConstraint(fields=["company", "name"], name="uniq_allergen_name")]

    def __str__(self):
        return self.name

    @property
    def is_mandatory(self):
        return self.kind == AllergenKind.MANDATORY


# --------------------------------------------------------------------------
# 食材
# --------------------------------------------------------------------------


class FoodComposition(models.Model):
    """日本食品標準成分表（八訂）の食品（v26）。会社に関係ない参照データ。可食部100gあたり。

    出典：日本食品標準成分表（八訂）増補2023年（文部科学省）。取り込みは import_food_composition コマンド。
    値の「Tr」（微量）は0、括弧付きの推定値は括弧内の数値、「-」（未測定）は空欄として取り込む。
    """

    food_number = models.CharField("食品番号", max_length=10, unique=True)
    group = models.CharField("食品群", max_length=40)
    name = models.CharField("食品名", max_length=200)
    energy_kcal = models.DecimalField("エネルギー（kcal）", max_digits=8, decimal_places=2, null=True)
    protein_g = models.DecimalField("たんぱく質（g）", max_digits=8, decimal_places=2, null=True)
    fat_g = models.DecimalField("脂質（g）", max_digits=8, decimal_places=2, null=True)
    carbohydrate_g = models.DecimalField("炭水化物（g）", max_digits=8, decimal_places=2, null=True)
    salt_g = models.DecimalField("食塩相当量（g）", max_digits=8, decimal_places=2, null=True)

    class Meta:
        verbose_name = verbose_name_plural = "食品成分表"
        ordering = ["food_number"]

    def __str__(self):
        return f"{self.food_number} {self.name}"


class AllergenStatus(models.TextChoices):
    UNCONFIRMED = "unconfirmed", "未確認"
    CONFIRMED = "confirmed", "確認済み"


class Ingredient(CompanyOwnedModel):
    code = models.CharField("食材ID", max_length=20, editable=False)
    name = models.CharField("食材名", max_length=100)
    category = models.ForeignKey(IngredientCategory, verbose_name="カテゴリー", on_delete=models.PROTECT)
    supplier = models.ForeignKey(Supplier, verbose_name="仕入先", on_delete=models.PROTECT)
    purchase_weight_g = models.DecimalField(
        "購入重量（g）", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("1"))]
    )
    purchase_price = models.DecimalField(
        "購入価格（円）", max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))]
    )
    legacy_code = models.CharField("旧コード", max_length=30, blank=True)
    additives = models.TextField("添加物", blank=True)
    origin_country = models.CharField("原産国", max_length=100, blank=True)
    note = models.TextField("備考", blank=True)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    allergens = models.ManyToManyField(AllergenItem, verbose_name="アレルゲン", blank=True, related_name="ingredients")
    allergen_status = models.CharField(
        "アレルゲン確認", max_length=15, choices=AllergenStatus.choices, default=AllergenStatus.UNCONFIRMED
    )
    allergen_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="アレルゲン確認者", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )
    allergen_confirmed_at = models.DateTimeField("アレルゲン確認日時", null=True, blank=True)

    # 栄養成分（v26）：可食部100gあたり。5項目すべて入っていれば「入力済み」。未入力を0とみなさない
    energy_kcal = models.DecimalField("熱量（kcal/100g）", max_digits=8, decimal_places=2, null=True, blank=True)
    protein_g = models.DecimalField("たんぱく質（g/100g）", max_digits=8, decimal_places=2, null=True, blank=True)
    fat_g = models.DecimalField("脂質（g/100g）", max_digits=8, decimal_places=2, null=True, blank=True)
    carbohydrate_g = models.DecimalField("炭水化物（g/100g）", max_digits=8, decimal_places=2, null=True, blank=True)
    salt_g = models.DecimalField("食塩相当量（g/100g）", max_digits=8, decimal_places=2, null=True, blank=True)
    nutrition_food = models.ForeignKey(
        FoodComposition, verbose_name="参照した成分表の食品", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    nutrition_note = models.CharField("栄養値の出どころ", max_length=200, blank=True)

    NUTRITION_FIELDS = ["energy_kcal", "protein_g", "fat_g", "carbohydrate_g", "salt_g"]

    class Meta:
        verbose_name = verbose_name_plural = "食材"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_ingredient_code"),
            models.CheckConstraint(condition=Q(purchase_weight_g__gte=1), name="ingredient_weight_min_1g"),
            models.CheckConstraint(condition=Q(purchase_price__gte=0), name="ingredient_price_not_negative"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"

    def unit_price(self, rounding=UnitPriceRounding.NONE):
        return unit_price(self.purchase_price, self.purchase_weight_g, rounding)

    @property
    def allergen_confirmed(self):
        return self.allergen_status == AllergenStatus.CONFIRMED

    @property
    def nutrition_complete(self):
        return all(getattr(self, f) is not None for f in self.NUTRITION_FIELDS)


class IngredientPriceHistory(models.Model):
    """価格変更のたびに1行追加する（第62項6）。登録時の価格も1行目として記録する。"""

    ingredient = models.ForeignKey(Ingredient, on_delete=models.CASCADE, related_name="price_history")
    purchase_weight_g = models.DecimalField("購入重量（g）", max_digits=12, decimal_places=2)
    purchase_price = models.DecimalField("購入価格（円）", max_digits=12, decimal_places=2)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    changed_at = models.DateTimeField("変更日時", auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = "食材価格履歴"
        ordering = ["-changed_at", "-id"]

    @property
    def unit_price(self):
        return unit_price(self.purchase_price, self.purchase_weight_g)


# --------------------------------------------------------------------------
# レシピ
# --------------------------------------------------------------------------


class RecipeKind(models.TextChoices):
    INTERMEDIATE = "intermediate", "中間レシピ"
    FINAL = "final", "最終商品レシピ"


class RecipeType(models.TextChoices):
    """中間レシピの種類（第22項、第62項11）。"""

    DOUGH = "dough", "生地"
    FILLING = "filling", "フィリング"
    CREAM = "cream", "クリーム"
    SAUCE = "sauce", "ソース"
    AN = "an", "あん"
    DAMANDE = "damande", "ダマンド"
    STARTER = "starter", "発酵種"
    OTHER = "other", "その他副材料"


class Recipe(CompanyOwnedModel):
    code = models.CharField("レシピID", max_length=20, editable=False)
    name = models.CharField("レシピ名", max_length=100)
    version = models.PositiveIntegerField("バージョン", default=1)
    kind = models.CharField("区分", max_length=15, choices=RecipeKind.choices)
    recipe_type = models.CharField("種類", max_length=15, choices=RecipeType.choices, blank=True)
    yield_rate = models.DecimalField("歩留まり", max_digits=5, decimal_places=4)
    copied_from = models.ForeignKey(
        "self", verbose_name="コピー元", on_delete=models.SET_NULL, null=True, blank=True, related_name="copies"
    )
    preparation = models.TextField("下処理", blank=True)
    procedure = models.TextField("手順", blank=True)
    memo = models.TextField("メモ", blank=True)
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "レシピ"
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "code"], name="uniq_recipe_code"),
            models.CheckConstraint(
                condition=(Q(kind=RecipeKind.FINAL, recipe_type="")
                           | (Q(kind=RecipeKind.INTERMEDIATE) & ~Q(recipe_type=""))),
                name="recipe_type_only_for_intermediate",
            ),
            models.CheckConstraint(condition=Q(yield_rate__gt=0, yield_rate__lte=1), name="recipe_yield_range"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}（v{self.version}）"

    @property
    def is_intermediate(self):
        return self.kind == RecipeKind.INTERMEDIATE

    def is_in_use(self):
        """使用中＝商品に採用中、または他のレシピの材料として使用中（第30項 v14）。"""
        return self.adopted_by_products.exists() or self.used_in_items.exists()


class RecipeItem(models.Model):
    """レシピ明細1行。材料は食材か中間レシピのどちらか一方（第62項8・P62）。"""

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="items")
    ingredient = models.ForeignKey(
        Ingredient, verbose_name="食材", on_delete=models.PROTECT, null=True, blank=True, related_name="used_in_items"
    )
    material_recipe = models.ForeignKey(
        Recipe, verbose_name="中間レシピ", on_delete=models.PROTECT, null=True, blank=True,
        related_name="used_in_items",
    )
    quantity_g = models.DecimalField("使用量（g）", max_digits=10, decimal_places=2)
    step_label = models.CharField("工程", max_length=50, blank=True)
    sort_order = models.PositiveIntegerField("表示順", default=0)

    class Meta:
        verbose_name = verbose_name_plural = "レシピ明細"
        ordering = ["sort_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(ingredient__isnull=False, material_recipe__isnull=True)
                           | Q(ingredient__isnull=True, material_recipe__isnull=False)),
                name="recipe_item_exactly_one_material",
            ),
            models.CheckConstraint(condition=Q(quantity_g__gt=0), name="recipe_item_quantity_positive"),
        ]

    def __str__(self):
        return f"{self.material} {self.quantity_g}g"

    @property
    def material(self):
        return self.ingredient or self.material_recipe


def photo_path(instance, filename):
    """推測できない名前で会社ごとのフォルダに置く。拡張子は保存時に JPEG に変換するため固定。"""
    owner = instance.owner
    return f"{owner._meta.model_name}_photos/{owner.company_id}/{uuid.uuid4().hex}.jpg"


def recipe_photo_path(instance, filename):  # 0003 のマイグレーションが参照している名前
    return photo_path(instance, filename)


class PhotoBase(models.Model):
    """写真の共通部分（v24）。表示順の1枚目が代表写真。原価に影響しないので使用中でも変更できる。

    子クラスは写真を持つ側への外部キー（related_name="photos"）と OWNER_FIELD を定義する。
    """

    MAX_PHOTOS = 20
    OWNER_FIELD = ""

    image = models.ImageField("写真", upload_to=recipe_photo_path)
    thumbnail = models.ImageField("縮小画像", upload_to=recipe_photo_path)
    caption = models.CharField("説明", max_length=100, blank=True)
    sort_order = models.PositiveIntegerField("表示順", default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="追加した人", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField("追加日時", auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["sort_order", "id"]

    @property
    def owner(self):
        return getattr(self, self.OWNER_FIELD)

    def __str__(self):
        return f"{self.owner.code} の写真 {self.caption or self.pk}"


class RecipePhoto(PhotoBase):
    OWNER_FIELD = "recipe"
    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name="photos")

    class Meta(PhotoBase.Meta):
        verbose_name = verbose_name_plural = "レシピ写真"


# --------------------------------------------------------------------------
# 商品
# --------------------------------------------------------------------------


class Product(CompanyOwnedModel):
    code = models.CharField("商品ID", max_length=20, editable=False)
    name = models.CharField("商品名", max_length=100)
    price_excluding_tax = models.PositiveIntegerField("税抜売価（円）")
    adopted_recipe = models.ForeignKey(
        Recipe, verbose_name="採用レシピ", on_delete=models.PROTECT, null=True, blank=True,
        related_name="adopted_by_products", limit_choices_to={"kind": RecipeKind.FINAL},
    )
    status = models.CharField("使用状態", max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        verbose_name = verbose_name_plural = "商品"
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["company", "code"], name="uniq_product_code")]

    def __str__(self):
        return f"{self.code} {self.name}"


class ProductPhoto(PhotoBase):
    """商品そのものの写真（v25）。ない場合は採用レシピの代表写真を表示する。"""

    OWNER_FIELD = "product"
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="photos")

    class Meta(PhotoBase.Meta):
        verbose_name = verbose_name_plural = "商品写真"


class StoreProductSetting(models.Model):
    """店舗ごとの販売設定（第46項）。プロトタイプでは画面なし。"""

    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    is_sold = models.BooleanField("販売する", default=True)

    class Meta:
        verbose_name = verbose_name_plural = "店舗販売設定"
        constraints = [models.UniqueConstraint(fields=["store", "product"], name="uniq_store_product")]


# --------------------------------------------------------------------------
# 履歴・監査
# --------------------------------------------------------------------------


class AuditLog(models.Model):
    """誰が・いつ・何を・変更前・変更後（第52項、P61）。"""

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name="ユーザー", on_delete=models.SET_NULL, null=True)
    at = models.DateTimeField("日時", auto_now_add=True)
    action = models.CharField("操作", max_length=50)
    target_type = models.CharField("対象の種類", max_length=50)
    target_id = models.BigIntegerField("対象の内部ID", null=True)
    target_label = models.CharField("対象", max_length=200)
    before = models.JSONField("変更前", null=True, blank=True)
    after = models.JSONField("変更後", null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = "変更履歴"
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["company", "target_type", "target_id"])]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.user} {self.action} {self.target_label}"


class RecipeAdoption(models.Model):
    """採用レシピの切替履歴（第39項）。"""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="adoptions")
    old_recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, null=True, related_name="+")
    new_recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, related_name="+")
    old_cost = models.DecimalField(max_digits=18, decimal_places=6, null=True)
    new_cost = models.DecimalField(max_digits=18, decimal_places=6)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = "採用レシピ切替履歴"
        ordering = ["-changed_at", "-id"]


class RecipeReplacement(models.Model):
    """中間レシピの置き換え履歴（第39項 v15）。"""

    company = models.ForeignKey(Company, on_delete=models.PROTECT)
    old_recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, related_name="+")
    new_recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, related_name="+")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = "中間レシピ置き換え履歴"
        ordering = ["-changed_at", "-id"]


class SnapshotReason(models.TextChoices):
    PRICE_CHANGE = "price_change", "食材価格変更"
    ADOPTION = "adoption", "採用レシピ切替"
    REPLACEMENT = "replacement", "中間レシピ置き換え"


class CostSnapshot(models.Model):
    """その時点の商品原価の記録（第62項2）。"""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="cost_snapshots")
    recipe = models.ForeignKey(Recipe, on_delete=models.PROTECT, null=True, related_name="+")
    cost = models.DecimalField("原価", max_digits=18, decimal_places=6)
    price_excluding_tax = models.PositiveIntegerField("税抜売価")
    reason = models.CharField("原因", max_length=20, choices=SnapshotReason.choices)
    audit_log = models.ForeignKey(AuditLog, on_delete=models.SET_NULL, null=True, related_name="+")
    taken_at = models.DateTimeField("記録日時", auto_now_add=True)

    class Meta:
        verbose_name = verbose_name_plural = "原価記録"
        ordering = ["-taken_at", "-id"]

    @property
    def cost_rate(self):
        return cost_rate_percent(self.cost, self.price_excluding_tax)
