"""食材価格の変更と影響確認（spec.md 第43〜45項）。

変更 → 確認 → 影響表示 → 確定 → 即時反映。確定時に価格履歴・変更履歴・原価記録を残す（第62項2・6）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction

from bakery.models import CostSnapshot, Ingredient, IngredientPriceHistory, Product, Recipe, SnapshotReason
from bakery.services import audit
from bakery.services.costing import CostCalculator, CostingError, MaterialKind, OverrideSource, cost_rate_percent, unit_price
from bakery.services.recipes import ProductImpact, RecipeRuleError
from bakery.services.sources import DbSource


class StalePriceError(RecipeRuleError):
    """確認中に、別の人が同じ食材の価格を変更した。"""


@dataclass
class RecipeChange:
    recipe: Recipe
    before: Decimal  # 中間レシピは1g原価、最終商品レシピは原価合計
    after: Decimal


@dataclass
class PriceImpact:
    ingredient: Ingredient
    old_weight: Decimal
    old_price: Decimal
    new_weight: Decimal
    new_price: Decimal
    old_unit_price: Decimal
    new_unit_price: Decimal
    recipes: list[RecipeChange]
    products: list[ProductImpact]

    @property
    def price_diff(self):
        return self.new_price - self.old_price

    @property
    def unit_price_diff(self):
        return self.new_unit_price - self.old_unit_price


def price_impact(ingredient, new_weight, new_price):
    if new_weight == ingredient.purchase_weight_g and new_price == ingredient.purchase_price:
        raise RecipeRuleError("購入重量・購入価格が今と同じです。変更する値を入力してください")

    company = ingredient.company
    source = DbSource(company)
    new_data = dataclasses.replace(
        source.ingredient(ingredient.pk), unit_price=unit_price(new_price, new_weight, source.rounding)
    )
    before_calc = source.calculator()
    after_calc = CostCalculator(OverrideSource(source, ingredients={ingredient.pk: new_data}))

    affected_ids = source.recipes_using(MaterialKind.INGREDIENT, ingredient.pk)
    recipes = []
    costs = {}
    for recipe in Recipe.objects.filter(pk__in=affected_ids).order_by("kind", "code"):
        try:
            b, a = before_calc.recipe_cost(recipe.pk), after_calc.recipe_cost(recipe.pk)
        except CostingError:
            continue  # 材料のない中間レシピを含むなど、今も計算できないレシピは比較しない
        if recipe.is_intermediate:
            recipes.append(RecipeChange(recipe, b.cost_per_g, a.cost_per_g))
        else:
            costs[recipe.pk] = (source.product_cost(b.total_cost), source.product_cost(a.total_cost))
            recipes.append(RecipeChange(recipe, *costs[recipe.pk]))

    products = []
    for p in (Product.objects.filter(company=company, adopted_recipe_id__in=costs)
              .select_related("adopted_recipe").order_by("code")):
        b, a = costs[p.adopted_recipe_id]
        products.append(ProductImpact(p, b, a, cost_rate_percent(b, p.price_excluding_tax),
                                      cost_rate_percent(a, p.price_excluding_tax)))

    return PriceImpact(
        ingredient=ingredient,
        old_weight=ingredient.purchase_weight_g,
        old_price=ingredient.purchase_price,
        new_weight=new_weight,
        new_price=new_price,
        old_unit_price=source.ingredient(ingredient.pk).unit_price,
        new_unit_price=new_data.unit_price,
        recipes=recipes,
        products=products,
    )


@transaction.atomic
def change_price(request, ingredient, *, new_weight, new_price, seen_weight, seen_price):
    """確定。seen_* は確認画面を表示したときの価格。その後に別の人が変えていたら確定しない。"""
    ingredient = Ingredient.objects.select_for_update().get(pk=ingredient.pk, company=request.company)
    if ingredient.purchase_weight_g != seen_weight or ingredient.purchase_price != seen_price:
        raise StalePriceError("確認している間に、別の人がこの食材の価格を変更しました。最新の価格で影響をもう一度確認してください")
    impact = price_impact(ingredient, new_weight, new_price)

    fields = ["purchase_weight_g", "purchase_price"]
    before = audit.snapshot(ingredient, fields)
    ingredient.purchase_weight_g = new_weight
    ingredient.purchase_price = new_price
    ingredient.updated_by = request.user
    ingredient.save(update_fields=fields + ["updated_by", "updated_at"])
    ingredient.refresh_from_db(fields=fields)  # 履歴には保存後の値（小数第2位まで）を残す
    IngredientPriceHistory.objects.create(ingredient=ingredient, purchase_weight_g=new_weight,
                                          purchase_price=new_price, changed_by=request.user)
    log = audit.record(
        request, "食材価格変更", ingredient,
        before={**before, "商品原価": {str(p.product): str(p.before_cost) for p in impact.products}},
        after={**audit.snapshot(ingredient, fields),
               "商品原価": {str(p.product): str(p.after_cost) for p in impact.products}},
    )
    CostSnapshot.objects.bulk_create([
        CostSnapshot(product=p.product, recipe_id=p.product.adopted_recipe_id, cost=p.after_cost,
                     price_excluding_tax=p.product.price_excluding_tax,
                     reason=SnapshotReason.PRICE_CHANGE, audit_log=log)
        for p in impact.products
    ])
    return impact
