"""商品の原価・売価・原価率と、採用レシピの切替（spec.md 第38・39・41・42項）。

商品原価 ＝ 採用している最終商品レシピ（商品1個分）の材料原価合計。包材は将来追加する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef

from bakery.models import (
    AllergenItem,
    CostSnapshot,
    IdSequence,
    Product,
    Recipe,
    RecipeAdoption,
    RecipeItem,
    RecipeKind,
    SnapshotReason,
    Status,
)
from bakery.services import audit
from bakery.services.costing import CostingError, RecipeCost, cost_rate_percent, price_including_tax
from bakery.services.recipes import RecipeRuleError, build_view
from bakery.services.sources import DbSource


@dataclass
class ProductCost:
    product: Product
    price_including_tax: int
    cost: Decimal | None = None  # 採用レシピなし・計算不能なら None
    rate: Decimal | None = None
    has_unconfirmed_allergen: bool = False
    error: str = ""


def evaluate(company, products, source=None):
    """一覧・詳細用に、各商品の原価・税込売価・原価率をまとめて計算する。"""
    source = source or DbSource(company)
    calc = source.calculator()
    results = []
    for product in products:
        pc = ProductCost(product, price_including_tax(product.price_excluding_tax, source.tax_rate))
        if product.adopted_recipe_id is None:
            pc.error = "採用レシピが設定されていません"
        else:
            try:
                rc = calc.recipe_cost(product.adopted_recipe_id)
            except CostingError as exc:
                pc.error = str(exc)
            else:
                pc.cost = source.product_cost(rc.total_cost)
                pc.rate = cost_rate_percent(pc.cost, product.price_excluding_tax)
                pc.has_unconfirmed_allergen = rc.has_unconfirmed_allergen
        results.append(pc)
    return results


def adoptable_recipes(company):
    """採用できるレシピ：使用中の最終商品レシピで、材料があるもの（第39項）。"""
    return (
        Recipe.objects.filter(company=company, kind=RecipeKind.FINAL, status=Status.ACTIVE)
        .filter(Exists(RecipeItem.objects.filter(recipe=OuterRef("pk"))))
        .order_by("name", "-version")
    )


def check_adoptable(company, recipe):
    if recipe.company_id != company.pk or recipe.kind != RecipeKind.FINAL:
        raise RecipeRuleError("商品に採用できるのは最終商品レシピだけです")
    if recipe.status != Status.ACTIVE:
        raise RecipeRuleError(f"「{recipe}」は使用停止中のため採用できません")
    if not recipe.items.exists():
        raise RecipeRuleError(f"「{recipe}」にはまだ材料がありません")


def _cost_of(calc, recipe_id):
    try:
        return calc.recipe_cost(recipe_id)
    except CostingError as exc:
        raise RecipeRuleError(str(exc))


@transaction.atomic
def create_product(request, *, name, price_excluding_tax, recipe=None):
    company = request.company
    if recipe is not None:
        check_adoptable(company, recipe)
    product = Product.objects.create(
        company=company,
        code=IdSequence.issue(company, "ITEM"),
        name=name,
        price_excluding_tax=price_excluding_tax,
        adopted_recipe=recipe,
        created_by=request.user,
        updated_by=request.user,
    )
    log = audit.record(request, "商品登録", product, after=audit.snapshot(product))
    if recipe is not None:
        source = DbSource(company)
        cost = source.product_cost(_cost_of(source.calculator(), recipe.pk).total_cost)
        RecipeAdoption.objects.create(product=product, old_recipe=None, new_recipe=recipe,
                                      old_cost=None, new_cost=cost, changed_by=request.user)
        CostSnapshot.objects.create(product=product, recipe=recipe, cost=cost,
                                    price_excluding_tax=product.price_excluding_tax,
                                    reason=SnapshotReason.ADOPTION, audit_log=log)
    return product


# --------------------------------------------------------------------------
# S33 採用レシピの切替
# --------------------------------------------------------------------------


@dataclass
class AdoptionImpact:
    product: Product
    old_recipe: Recipe | None
    new_recipe: Recipe
    old_cost: Decimal | None
    new_cost: Decimal
    old_rate: Decimal | None
    new_rate: Decimal | None
    added_allergens: list[str] = field(default_factory=list)
    removed_allergens: list[str] = field(default_factory=list)
    new_has_unconfirmed: bool = False

    @property
    def diff(self):
        return None if self.old_cost is None else self.new_cost - self.old_cost


def adoption_impact(product, new_recipe):
    company = product.company
    if product.adopted_recipe_id == new_recipe.pk:
        raise RecipeRuleError("今の採用レシピと同じレシピが選ばれています")
    check_adoptable(company, new_recipe)
    source = DbSource(company)
    calc = source.calculator()
    new = _cost_of(calc, new_recipe.pk)
    old: RecipeCost | None = None
    if product.adopted_recipe_id:
        try:
            old = calc.recipe_cost(product.adopted_recipe_id)
        except CostingError:
            old = None
    before, after = set(old.allergens) if old else set(), set(new.allergens)
    names = AllergenItem.objects.in_bulk(list(before | after))
    old_cost = source.product_cost(old.total_cost) if old else None
    new_cost = source.product_cost(new.total_cost)

    def labels(ids):
        return [names[i].name for i in sorted(ids, key=lambda i: names[i].sort_order)]

    return AdoptionImpact(
        product=product,
        old_recipe=product.adopted_recipe,
        new_recipe=new_recipe,
        old_cost=old_cost,
        new_cost=new_cost,
        old_rate=cost_rate_percent(old_cost, product.price_excluding_tax) if old else None,
        new_rate=cost_rate_percent(new_cost, product.price_excluding_tax),
        added_allergens=labels(after - before) if old else [],
        removed_allergens=labels(before - after) if old else [],
        new_has_unconfirmed=new.has_unconfirmed_allergen,
    )


@transaction.atomic
def switch_adoption(request, product, new_recipe):
    """確定。確認後に即時反映し、切替履歴と原価記録を残す（第39項、第62項2）。"""
    product = Product.objects.select_for_update().get(pk=product.pk)
    impact = adoption_impact(product, new_recipe)
    before = audit.snapshot(product, ["adopted_recipe"])
    product.adopted_recipe = new_recipe
    product.updated_by = request.user
    product.save(update_fields=["adopted_recipe", "updated_by", "updated_at"])
    log = audit.record(
        request, "採用レシピ切替", product,
        before={**before, "原価": None if impact.old_cost is None else str(impact.old_cost)},
        after={**audit.snapshot(product, ["adopted_recipe"]), "原価": str(impact.new_cost)},
    )
    RecipeAdoption.objects.create(product=product, old_recipe=impact.old_recipe, new_recipe=new_recipe,
                                  old_cost=impact.old_cost, new_cost=impact.new_cost, changed_by=request.user)
    CostSnapshot.objects.create(product=product, recipe=new_recipe, cost=impact.new_cost,
                                price_excluding_tax=product.price_excluding_tax,
                                reason=SnapshotReason.ADOPTION, audit_log=log)
    return impact


# --------------------------------------------------------------------------
# S31 商品詳細
# --------------------------------------------------------------------------


def detail_view(product):
    """商品詳細用：原価（ProductCost）と採用レシピの内訳・アレルゲン（RecipeView）。"""
    source = DbSource(product.company)
    pc = evaluate(product.company, [product], source)[0]
    recipe_view = build_view(product.adopted_recipe, source) if product.adopted_recipe_id else None
    return pc, recipe_view
