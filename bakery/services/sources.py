"""DBの内容を原価計算エンジン（costing.py）の DataSource として見せる。

会社1社分の食材・レシピを最初にまとめて読み込む。数百件規模なら、1リクエストで
全件を読み込んでも十分に速く、多階層レシピのたびにDBを引くより単純で確実。
"""

from __future__ import annotations

from collections import defaultdict

from bakery.models import Ingredient, Recipe, RecipeItem, SystemSetting
from bakery.services.costing import (
    CostCalculator,
    CostRounding,
    IngredientData,
    Line,
    MaterialKind,
    RecipeData,
    UnitPriceRounding,
    round_product_cost,
)
from bakery.services.nutrition import NutritionCalculator, Nutrients


class DbSource:
    def __init__(self, company):
        self.company = company
        setting = SystemSetting.for_company(company)
        self.rounding = UnitPriceRounding(setting.unit_price_rounding)
        self.tax_rate = setting.tax_rate
        self.cost_rounding = CostRounding(setting.cost_rounding)

        allergen_map = defaultdict(set)
        through = Ingredient.allergens.through
        for ingredient_id, allergen_id in through.objects.filter(
            ingredient__company=company
        ).values_list("ingredient_id", "allergenitem_id"):
            allergen_map[ingredient_id].add(allergen_id)

        self._ingredients: dict[int, IngredientData] = {}
        self.ingredient_rows: dict[int, Ingredient] = {}
        for row in Ingredient.objects.filter(company=company):
            self.ingredient_rows[row.id] = row
            self._ingredients[row.id] = IngredientData(
                id=row.id,
                name=row.name,
                unit_price=row.unit_price(self.rounding),
                allergen_confirmed=row.allergen_confirmed,
                allergen_ids=frozenset(allergen_map[row.id]),
                nutrition=Nutrients.from_object(row),
            )

        lines = defaultdict(list)
        for item in RecipeItem.objects.filter(recipe__company=company).order_by("sort_order", "id"):
            if item.ingredient_id:
                lines[item.recipe_id].append(
                    Line(MaterialKind.INGREDIENT, item.ingredient_id, item.quantity_g, item.id)
                )
            else:
                lines[item.recipe_id].append(
                    Line(MaterialKind.RECIPE, item.material_recipe_id, item.quantity_g, item.id)
                )

        self._recipes: dict[int, RecipeData] = {}
        self.recipe_rows: dict[int, Recipe] = {}
        for row in Recipe.objects.filter(company=company):
            self.recipe_rows[row.id] = row
            self._recipes[row.id] = RecipeData(row.id, row.name, row.yield_rate, tuple(lines[row.id]))

    # DataSource
    def ingredient(self, ingredient_id: int) -> IngredientData:
        return self._ingredients[ingredient_id]

    def recipe(self, recipe_id: int) -> RecipeData:
        return self._recipes[recipe_id]

    def calculator(self) -> CostCalculator:
        return CostCalculator(self)

    def nutrition_calculator(self) -> NutritionCalculator:
        return NutritionCalculator(self)

    def product_cost(self, total_cost):
        """最終商品レシピの原価合計 → 正式な商品原価（設定の丸めを適用）。"""
        return round_product_cost(total_cost, self.cost_rounding)

    # 逆引き（影響確認用）
    def recipes_using(self, kind: MaterialKind, ref_id: int) -> set[int]:
        """指定の材料を直接・間接に使っているレシピIDの集合。"""
        direct = defaultdict(set)  # (kind, id) → それを直接使うレシピ
        for recipe in self._recipes.values():
            for line in recipe.lines:
                direct[(line.kind, line.ref_id)].add(recipe.id)
        found: set[int] = set()
        todo = list(direct[(kind, ref_id)])
        while todo:
            recipe_id = todo.pop()
            if recipe_id in found:
                continue
            found.add(recipe_id)
            todo.extend(direct[(MaterialKind.RECIPE, recipe_id)])
        return found
