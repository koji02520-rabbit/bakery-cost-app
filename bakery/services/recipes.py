"""レシピの操作と、画面に出す原価・アレルゲンの組み立て。

ビューからはここを呼ぶ。原価の計算そのものは costing.py、DBの読み込みは sources.py が担当する。

ルール（spec.md）:
- 使用中（商品に採用中／他レシピの材料）のレシピは材料・使用量を直接変えない（第30項 v14）
  ただし工程・表示順・レシピ名・下処理・手順・メモは変えてよい（第30項 v16、第62項16）
- 材料にできるのは食材と中間レシピだけ（第62項8）。使用停止のものは新しく選べない（第19項）
- 循環参照は保存前に止める（第62項3）
- 中間レシピの置き換えだけは、使用中レシピの材料を書き換えてよい例外（第39項 v15）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import BooleanField, Exists, ExpressionWrapper, Max, OuterRef, Q

from bakery.models import (
    AllergenItem,
    CostSnapshot,
    IdSequence,
    Ingredient,
    Product,
    Recipe,
    RecipeItem,
    RecipeKind,
    RecipeReplacement,
    SnapshotReason,
    Status,
    SystemSetting,
)
from bakery.services import audit, photos
from bakery.services.costing import (
    CircularReferenceError,
    CostCalculator,
    CostingError,
    MaterialKind,
    OverrideSource,
    RecipeCost,
    cost_rate_percent,
    replace_material,
    would_create_cycle,
)
from bakery.services.sources import DbSource

FINAL_YIELD = Decimal("1")


class RecipeRuleError(Exception):
    """レシピのルールに反する操作。message は画面にそのまま出せる日本語。"""


# --------------------------------------------------------------------------
# 使用中の判定
# --------------------------------------------------------------------------


def with_usage(queryset):
    """各レシピに in_use（商品に採用中、または他レシピの材料）を付ける。"""
    return queryset.annotate(
        used_by_product=Exists(Product.objects.filter(adopted_recipe=OuterRef("pk"))),
        used_as_material=Exists(RecipeItem.objects.filter(material_recipe=OuterRef("pk"))),
    ).annotate(in_use=ExpressionWrapper(Q(used_by_product=True) | Q(used_as_material=True),
                                        output_field=BooleanField()))


def ensure_editable(recipe):
    if recipe.is_in_use():
        raise RecipeRuleError("このレシピは使用中のため、材料・使用量は変更できません。「新しいバージョンを作る」から変更してください")


# --------------------------------------------------------------------------
# 作成・コピー
# --------------------------------------------------------------------------


def next_version(company, name, kind):
    """同じ名前・同じ区分のレシピの最大バージョン＋1。レシピ名にバージョンを埋め込まない（第37項）。"""
    current = Recipe.objects.filter(company=company, name=name, kind=kind).aggregate(v=Max("version"))["v"]
    return (current or 0) + 1


def default_yield(company, kind):
    if kind == RecipeKind.FINAL:
        return FINAL_YIELD  # 最終商品レシピには歩留まりを適用しない（第62項10）
    return SystemSetting.for_company(company).default_yield_rate


@transaction.atomic
def create_recipe(request, *, name, kind, recipe_type=""):
    company = request.company
    recipe = Recipe.objects.create(
        company=company,
        code=IdSequence.issue(company, "RECIPE"),
        name=name,
        version=next_version(company, name, kind),
        kind=kind,
        recipe_type=recipe_type if kind == RecipeKind.INTERMEDIATE else "",
        yield_rate=default_yield(company, kind),
        created_by=request.user,
        updated_by=request.user,
    )
    audit.record(request, "レシピ作成", recipe, after=audit.snapshot(recipe))
    return recipe


@transaction.atomic
def copy_recipe(request, source, *, name):
    """コピーして新しいレシピを作る。コピー元は変更しない。新しいRECIPE-IDを発行する（第35・37項）。"""
    company = request.company
    recipe = Recipe.objects.create(
        company=company,
        code=IdSequence.issue(company, "RECIPE"),
        name=name,
        version=next_version(company, name, source.kind),
        kind=source.kind,
        recipe_type=source.recipe_type,
        yield_rate=default_yield(company, source.kind),
        copied_from=source,
        preparation=source.preparation,
        procedure=source.procedure,
        memo=source.memo,
        created_by=request.user,
        updated_by=request.user,
    )
    RecipeItem.objects.bulk_create([
        RecipeItem(recipe=recipe, ingredient_id=item.ingredient_id, material_recipe_id=item.material_recipe_id,
                   quantity_g=item.quantity_g, step_label=item.step_label, sort_order=item.sort_order)
        for item in source.items.all()
    ])
    photos.copy_photos(source, recipe, request.user)
    audit.record(request, "レシピコピー", recipe,
                 before={"コピー元": {"id": source.pk, "label": str(source)}}, after=audit.snapshot(recipe))
    return recipe


# --------------------------------------------------------------------------
# 材料明細の操作
# --------------------------------------------------------------------------


def items_snapshot(recipe):
    return [
        {"id": i.pk, "材料": str(i.material), "g": str(i.quantity_g), "工程": i.step_label, "順": i.sort_order}
        for i in recipe.items.select_related("ingredient", "material_recipe")
    ]


def _check_material(recipe, *, ingredient=None, material_recipe=None):
    company = recipe.company
    if ingredient is not None:
        if ingredient.company_id != company.pk:
            raise RecipeRuleError("この食材は選べません")
        if ingredient.status != Status.ACTIVE:
            raise RecipeRuleError(f"「{ingredient.name}」は使用停止中のため選べません")
        return
    if material_recipe.company_id != company.pk:
        raise RecipeRuleError("このレシピは選べません")
    if material_recipe.kind != RecipeKind.INTERMEDIATE:
        raise RecipeRuleError("材料に選べるのは食材と中間レシピだけです（最終商品レシピは選べません）")
    if material_recipe.status != Status.ACTIVE:
        raise RecipeRuleError(f"「{material_recipe.name}」は使用停止中のため選べません")
    if material_recipe.pk == recipe.pk or would_create_cycle(DbSource(company), recipe.pk, material_recipe.pk):
        raise RecipeRuleError(f"「{material_recipe.name}」を材料にすると、レシピが自分自身を材料に使う形（循環参照）になるため選べません")
    if not material_recipe.items.exists():
        raise RecipeRuleError(f"「{material_recipe.name}」にはまだ材料がないため選べません")


def _record_items_change(request, recipe, action, before):
    recipe.updated_by = request.user
    recipe.save(update_fields=["updated_by", "updated_at"])
    audit.record(request, action, recipe, before={"明細": before}, after={"明細": items_snapshot(recipe)})


@transaction.atomic
def add_item(request, recipe, *, quantity_g, step_label="", ingredient=None, material_recipe=None):
    ensure_editable(recipe)
    _check_material(recipe, ingredient=ingredient, material_recipe=material_recipe)
    before = items_snapshot(recipe)
    last = recipe.items.aggregate(m=Max("sort_order"))["m"]
    item = RecipeItem.objects.create(
        recipe=recipe, ingredient=ingredient, material_recipe=material_recipe,
        quantity_g=quantity_g, step_label=step_label, sort_order=(last or 0) + 1,
    )
    _record_items_change(request, recipe, "レシピ材料追加", before)
    return item


@transaction.atomic
def update_item(request, item, *, quantity_g, step_label):
    """使用量・工程の変更。使用中のレシピでは工程だけ変えられる。"""
    recipe = item.recipe
    in_use = recipe.is_in_use()
    if in_use and quantity_g != item.quantity_g:
        ensure_editable(recipe)
    before = items_snapshot(recipe)
    item.quantity_g = quantity_g
    item.step_label = step_label
    item.save(update_fields=["quantity_g", "step_label"])
    _record_items_change(request, recipe, "レシピ工程変更" if in_use else "レシピ材料変更", before)


@transaction.atomic
def change_material(request, item, *, ingredient=None, material_recipe=None):
    recipe = item.recipe
    ensure_editable(recipe)
    _check_material(recipe, ingredient=ingredient, material_recipe=material_recipe)
    before = items_snapshot(recipe)
    item.ingredient = ingredient
    item.material_recipe = material_recipe
    item.save(update_fields=["ingredient", "material_recipe"])
    _record_items_change(request, recipe, "レシピ材料変更", before)


@transaction.atomic
def delete_item(request, item):
    recipe = item.recipe
    ensure_editable(recipe)
    before = items_snapshot(recipe)
    item.delete()
    _record_items_change(request, recipe, "レシピ材料削除", before)


@transaction.atomic
def reorder_items(request, recipe, ordered_ids):
    """表示順の変更。原価に影響しないので使用中でも行える（第62項16）。"""
    items = {i.pk: i for i in recipe.items.all()}
    if sorted(ordered_ids) != sorted(items):
        raise RecipeRuleError("画面が古くなっています。再読み込みしてから並べ替えてください")
    before = items_snapshot(recipe)
    for order, pk in enumerate(ordered_ids, start=1):
        items[pk].sort_order = order
    RecipeItem.objects.bulk_update(items.values(), ["sort_order"])
    _record_items_change(request, recipe, "レシピ表示順変更", before)


# --------------------------------------------------------------------------
# 材料検索（S23）
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    kind: MaterialKind
    obj: object
    disabled_reason: str = ""

    @property
    def is_ingredient(self):
        return self.kind == MaterialKind.INGREDIENT


def search_materials(recipe, q="", scope="all", limit=30):
    """食材と中間レシピを横断して探す（第25・26項）。選べないものは理由を付けて返す。"""
    company = recipe.company
    results: list[Candidate] = []
    if scope in ("all", "ingredient"):
        rows = Ingredient.objects.filter(company=company, status=Status.ACTIVE).select_related("supplier")
        if q:
            rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(legacy_code__icontains=q))
        results += [Candidate(MaterialKind.INGREDIENT, r) for r in rows.order_by("code")[:limit]]
    if scope in ("all", "recipe"):
        rows = Recipe.objects.filter(company=company, kind=RecipeKind.INTERMEDIATE, status=Status.ACTIVE)
        if q:
            rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q))
        rows = list(rows.annotate(has_items=Exists(RecipeItem.objects.filter(recipe=OuterRef("pk"))))
                    .order_by("name", "-version")[:limit])
        source = DbSource(company) if rows else None
        for r in rows:
            reason = ""
            if r.pk == recipe.pk or would_create_cycle(source, recipe.pk, r.pk):
                reason = "このレシピ自身を材料に使うことになるため選べません"
            elif not r.has_items:
                reason = "まだ材料がないため選べません"
            results.append(Candidate(MaterialKind.RECIPE, r, reason))
    return results


# --------------------------------------------------------------------------
# 画面用の原価・アレルゲン
# --------------------------------------------------------------------------


@dataclass
class ItemRow:
    item: RecipeItem
    kind: MaterialKind
    material: object
    unit_price: Decimal | None
    cost: Decimal | None

    @property
    def is_ingredient(self):
        return self.kind == MaterialKind.INGREDIENT

    @property
    def material_stopped(self):
        return self.material.status != Status.ACTIVE


@dataclass
class AllergenRow:
    item: AllergenItem
    sources: list[str]


@dataclass
class RecipeView:
    recipe: Recipe
    cost: RecipeCost | None
    error: str
    rows: list[ItemRow]
    product_cost: Decimal | None = None  # 最終商品レシピだけ：丸めを適用した商品1個の原価
    nutrition: object = None  # nutrition.RecipeNutrition（v26）。計算できなければ None
    nutrition_missing: list[str] = field(default_factory=list)  # 栄養値が未入力の材料への経路
    aggregated: list[dict] = field(default_factory=list)
    allergens: list[AllergenRow] = field(default_factory=list)
    unconfirmed_sources: list[str] = field(default_factory=list)

    @property
    def is_final(self):
        return self.recipe.kind == RecipeKind.FINAL

    @property
    def nutrition_columns(self):
        """画面の表の列：最終商品レシピは1個あたり、中間レシピは100gあたりと全量。"""
        if not self.nutrition:
            return []
        if self.is_final:
            return [self.nutrition.total]
        return [c for c in (self.nutrition.per_100g, self.nutrition.total) if c is not None]


def path_label(source, path):
    names = []
    for kind, ref_id in path:
        row = source.ingredient_rows.get(ref_id) if kind == MaterialKind.INGREDIENT else source.recipe_rows.get(ref_id)
        names.append(row.name if row else "？")
    return " → ".join(names)


def build_view(recipe, source=None):
    source = source or DbSource(recipe.company)
    error = ""
    try:
        cost = source.calculator().recipe_cost(recipe.pk)
    except CostingError as exc:
        cost, error = None, str(exc)

    line_costs = {lc.line.line_id: lc for lc in cost.lines} if cost else {}
    rows = []
    for item in recipe.items.select_related("ingredient", "material_recipe"):
        lc = line_costs.get(item.pk)
        kind = MaterialKind.INGREDIENT if item.ingredient_id else MaterialKind.RECIPE
        rows.append(ItemRow(item, kind, item.material, lc.unit_price if lc else None, lc.cost if lc else None))

    view = RecipeView(recipe, cost, error, rows)
    if cost and recipe.kind == RecipeKind.FINAL:
        view.product_cost = source.product_cost(cost.total_cost)
    if cost:
        view.nutrition = source.nutrition_calculator().recipe_nutrition(recipe.pk)
        view.nutrition_missing = sorted(path_label(source, p) for p in view.nutrition.missing_sources)
    if cost:
        for agg in cost.aggregated():
            material = (source.ingredient_rows if agg.kind == MaterialKind.INGREDIENT else source.recipe_rows)[agg.ref_id]
            view.aggregated.append({"kind": agg.kind, "material": material,
                                    "quantity_g": agg.quantity_g, "cost": agg.cost})
        items = AllergenItem.objects.in_bulk(list(cost.allergens))
        for allergen_id, paths in sorted(cost.allergens.items(), key=lambda kv: items[kv[0]].sort_order):
            view.allergens.append(AllergenRow(items[allergen_id], sorted(path_label(source, p) for p in paths)))
        view.unconfirmed_sources = sorted(path_label(source, p) for p in cost.unconfirmed_sources)
    return view


def recipe_costs(company, recipes):
    """一覧用：レシピID → (原価合計, 1g原価, 未確認あり) 。計算できないものは None。"""
    source = DbSource(company)
    calc = source.calculator()
    result = {}
    for recipe in recipes:
        try:
            rc = calc.recipe_cost(recipe.pk)
            total = source.product_cost(rc.total_cost) if recipe.kind == RecipeKind.FINAL else rc.total_cost
            result[recipe.pk] = (total, rc.cost_per_g, rc.has_unconfirmed_allergen)
        except CostingError:
            result[recipe.pk] = None
    return result


# --------------------------------------------------------------------------
# 中間レシピの置き換え（S25、管理者のみ）
# --------------------------------------------------------------------------


@dataclass
class ProductImpact:
    product: Product
    before_cost: Decimal
    after_cost: Decimal
    before_rate: Decimal | None
    after_rate: Decimal | None

    @property
    def diff(self):
        return self.after_cost - self.before_cost


@dataclass
class ReplacementImpact:
    old: Recipe
    new: Recipe
    parents: list[Recipe]  # 旧バージョンを直接材料にしているレシピ（ここの明細を差し替える）
    affected: list[Recipe]  # 多階層をたどって影響するすべてのレシピ
    products: list[ProductImpact]
    added_allergens: list[str]
    removed_allergens: list[str]
    unconfirmed_before: bool
    unconfirmed_after: bool


def replacement_impact(company, old, new):
    if old.pk == new.pk:
        raise RecipeRuleError("置き換え前と置き換え後に同じレシピが選ばれています")
    for r, label in ((old, "置き換え前"), (new, "置き換え後")):
        if r.company_id != company.pk or r.kind != RecipeKind.INTERMEDIATE:
            raise RecipeRuleError(f"{label}には中間レシピを選んでください")
    if new.status != Status.ACTIVE:
        raise RecipeRuleError(f"「{new}」は使用停止中のため、置き換え後に選べません")
    if not new.items.exists():
        raise RecipeRuleError(f"「{new}」にはまだ材料がありません")

    source = DbSource(company)
    parent_ids = set(RecipeItem.objects.filter(material_recipe=old).values_list("recipe_id", flat=True))
    if not parent_ids:
        raise RecipeRuleError(f"「{old}」はどのレシピの材料にも使われていないため、置き換えは不要です")

    after_source = OverrideSource(source, recipes={
        pid: replace_material(source.recipe(pid), old.pk, new.pk) for pid in parent_ids
    })
    before_calc, after_calc = source.calculator(), CostCalculator(after_source)
    affected_ids = source.recipes_using(MaterialKind.RECIPE, old.pk)
    try:
        for rid in affected_ids:
            after_calc.recipe_cost(rid)
    except CircularReferenceError:
        raise RecipeRuleError(f"「{new}」に置き換えると、レシピが自分自身を材料に使う形（循環参照）になります")
    except CostingError as exc:
        raise RecipeRuleError(str(exc))

    products = []
    for p in Product.objects.filter(company=company, adopted_recipe_id__in=affected_ids).order_by("code"):
        b = source.product_cost(before_calc.recipe_cost(p.adopted_recipe_id).total_cost)
        a = source.product_cost(after_calc.recipe_cost(p.adopted_recipe_id).total_cost)
        products.append(ProductImpact(p, b, a, cost_rate_percent(b, p.price_excluding_tax),
                                      cost_rate_percent(a, p.price_excluding_tax)))
    # アレルゲンの変化は、置き換える中間レシピそのものの違いで見る
    b_old = before_calc.recipe_cost(old.pk)
    a_new = after_calc.recipe_cost(new.pk)
    before_allergens, after_allergens = set(b_old.allergens), set(a_new.allergens)
    unconfirmed_before, unconfirmed_after = b_old.has_unconfirmed_allergen, a_new.has_unconfirmed_allergen
    names = AllergenItem.objects.in_bulk(list(before_allergens | after_allergens))

    return ReplacementImpact(
        old=old,
        new=new,
        parents=list(Recipe.objects.filter(pk__in=parent_ids).order_by("code")),
        affected=list(Recipe.objects.filter(pk__in=affected_ids).order_by("code")),
        products=products,
        added_allergens=[names[i].name for i in sorted(after_allergens - before_allergens, key=lambda i: names[i].sort_order)],
        removed_allergens=[names[i].name for i in sorted(before_allergens - after_allergens, key=lambda i: names[i].sort_order)],
        unconfirmed_before=unconfirmed_before,
        unconfirmed_after=unconfirmed_after,
    )


@transaction.atomic
def replace_intermediate(request, old, new):
    """確定。旧バージョンを材料にしている明細の参照先を新バージョンへ一括で差し替える。

    上位レシピのIDは変えない。使用量・工程・表示順も変えない（第39項 v15）。
    """
    company = request.company
    # 同時操作に備え、対象レシピを固定してから影響を計算し直す
    list(Recipe.objects.select_for_update().filter(pk__in=[old.pk, new.pk]))
    impact = replacement_impact(company, old, new)
    changed = RecipeItem.objects.filter(recipe__company=company, material_recipe=old).update(material_recipe=new)
    RecipeReplacement.objects.create(company=company, old_recipe=old, new_recipe=new, changed_by=request.user)
    log = audit.record(
        request, "中間レシピ置き換え", new,
        before={"置き換え前": str(old), "商品原価": {str(p.product): str(p.before_cost) for p in impact.products}},
        after={"置き換え後": str(new), "対象レシピ": [str(r) for r in impact.parents], "明細数": changed,
               "商品原価": {str(p.product): str(p.after_cost) for p in impact.products}},
    )
    CostSnapshot.objects.bulk_create([
        CostSnapshot(product=p.product, recipe_id=p.product.adopted_recipe_id, cost=p.after_cost,
                     price_excluding_tax=p.product.price_excluding_tax,
                     reason=SnapshotReason.REPLACEMENT, audit_log=log)
        for p in impact.products
    ])
    return impact
