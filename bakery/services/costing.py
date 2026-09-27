"""原価計算エンジン。

画面・DBから独立した純粋な計算処理だけを置く（spec.md 第57・65項）。
DBからの読み込みは DataSource を実装したクラス（bakery.services.sources）が担当する。

単位の約束（spec.md 第24項）:
    食材単価        円/g
    使用量          g
    中間レシピ原価  円/g（材料原価合計 ÷ 有効重量）
    商品原価        円（最終商品レシピ1個分の材料原価合計）

金額・重量はすべて Decimal で扱い、float は使わない。丸めは表示時のみ（第62項1）。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from typing import Protocol

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class MaterialKind(str, Enum):
    INGREDIENT = "ingredient"
    RECIPE = "recipe"


class UnitPriceRounding(str, Enum):
    """1g単価の丸め方式（spec.md 第62項1。本決定はプロトタイプ検証後）。"""

    NONE = "none"  # 丸めない（既定）
    EXCEL_ROUNDUP_2 = "excel_roundup_2"  # 既存Excelと同じ：小数第2位へ切り上げ


class CostRounding(str, Enum):
    """商品原価（最終商品レシピの原価）の丸め方式（spec.md 第62項1、v23で決定）。

    丸めた値を正式な商品原価とし、原価率・原価記録・影響確認にも使う。
    中間レシピの1g原価は上位レシピの計算に使うため丸めない。
    """

    NONE = "none"
    CEIL_1 = "ceil_1"  # 小数第1位へ切り上げ（既定）


def round_product_cost(value: Decimal, rounding: CostRounding = CostRounding.CEIL_1) -> Decimal:
    if rounding == CostRounding.CEIL_1:
        return value.quantize(Decimal("0.1"), rounding=ROUND_CEILING)
    return value


class CostingError(Exception):
    """原価を計算できない状態。message は画面にそのまま出せる日本語にする。"""


class CircularReferenceError(CostingError):
    def __init__(self, path: tuple[int, ...]):
        self.path = path
        super().__init__("中間レシピが循環参照しています（自分自身を材料として使っています）")


class EmptyRecipeError(CostingError):
    def __init__(self, recipe_id: int, name: str):
        self.recipe_id = recipe_id
        super().__init__(f"中間レシピ「{name}」に材料がないため、1gあたりの原価を計算できません")


# --------------------------------------------------------------------------
# 基本の計算式
# --------------------------------------------------------------------------


def unit_price(
    purchase_price: Decimal | int,
    purchase_weight_g: Decimal | int,
    rounding: UnitPriceRounding = UnitPriceRounding.NONE,
) -> Decimal:
    """1gあたり価格 ＝ 購入価格 ÷ 購入重量（spec.md 第11項）。"""
    weight = Decimal(purchase_weight_g)
    if weight <= ZERO:
        raise ValueError("購入重量は1g以上で入力してください")
    value = Decimal(purchase_price) / weight
    if rounding == UnitPriceRounding.EXCEL_ROUNDUP_2:
        value = value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return value


def price_including_tax(price_excluding_tax: int, tax_rate: Decimal) -> int:
    """税込売価。端数は切り捨て（spec.md 第41・62項）。tax_rate は 0.08 のような小数。"""
    value = Decimal(price_excluding_tax) * (ONE + tax_rate)
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def cost_rate_percent(cost: Decimal, price_excluding_tax: int) -> Decimal | None:
    """原価率(%) ＝ 商品原価 ÷ 税抜売価 × 100。売価が0以下なら計算しない。"""
    if price_excluding_tax <= 0:
        return None
    return cost / Decimal(price_excluding_tax) * HUNDRED


# --------------------------------------------------------------------------
# レシピ原価
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """レシピ明細1行。工程・表示順は原価計算に使わないため持たない（第28・29項）。"""

    kind: MaterialKind
    ref_id: int
    quantity_g: Decimal
    line_id: int | None = None


@dataclass(frozen=True)
class IngredientData:
    id: int
    name: str
    unit_price: Decimal
    allergen_confirmed: bool = False
    allergen_ids: frozenset[int] = frozenset()
    nutrition: object = None  # 100gあたりの nutrition.Nutrients。未入力なら None（v26）


@dataclass(frozen=True)
class RecipeData:
    id: int
    name: str
    yield_rate: Decimal  # 中間レシピ 0.98、最終商品レシピ 1
    lines: tuple[Line, ...]


class DataSource(Protocol):
    def ingredient(self, ingredient_id: int) -> IngredientData: ...

    def recipe(self, recipe_id: int) -> RecipeData: ...


# 由来の経路：上位から順に (種類, ID) を並べたもの。
# 例：((RECIPE, ダマンド), (INGREDIENT, アーモンドプードル))
SourcePath = tuple[tuple[MaterialKind, int], ...]


@dataclass(frozen=True)
class LineCost:
    line: Line
    unit_price: Decimal
    cost: Decimal


@dataclass(frozen=True)
class AggregatedMaterial:
    kind: MaterialKind
    ref_id: int
    quantity_g: Decimal
    cost: Decimal


@dataclass
class RecipeCost:
    recipe_id: int
    lines: list[LineCost]
    total_cost: Decimal
    total_weight_g: Decimal  # 仕込み量
    yield_rate: Decimal
    # アレルゲンID → そのアレルゲンが来た経路の集合
    allergens: dict[int, set[SourcePath]] = field(default_factory=dict)
    # アレルゲン未確認の食材への経路
    unconfirmed_sources: set[SourcePath] = field(default_factory=set)

    @property
    def finished_weight_g(self) -> Decimal:
        """出来上がり量 ＝ 仕込み量 × 歩留まり（第62項17）。"""
        return self.total_weight_g * self.yield_rate

    @property
    def cost_per_g(self) -> Decimal | None:
        """1g原価 ＝ 材料原価合計 ÷ 有効重量（第33項）。"""
        finished = self.finished_weight_g
        if finished <= ZERO:
            return None
        return self.total_cost / finished

    @property
    def has_unconfirmed_allergen(self) -> bool:
        return bool(self.unconfirmed_sources)

    def aggregated(self) -> list[AggregatedMaterial]:
        """同じ材料の複数行を合算した一覧（第27・31項）。最初に出てきた順。"""
        totals: OrderedDict[tuple[MaterialKind, int], list[Decimal]] = OrderedDict()
        for lc in self.lines:
            key = (lc.line.kind, lc.line.ref_id)
            qty_cost = totals.setdefault(key, [ZERO, ZERO])
            qty_cost[0] += lc.line.quantity_g
            qty_cost[1] += lc.cost
        return [AggregatedMaterial(k[0], k[1], v[0], v[1]) for k, v in totals.items()]


class CostCalculator:
    """DataSource からレシピ原価を計算する。同じ中間レシピは1回だけ計算する。"""

    def __init__(self, source: DataSource):
        self.source = source
        self._memo: dict[int, RecipeCost] = {}

    def recipe_cost(self, recipe_id: int) -> RecipeCost:
        return self._recipe_cost(recipe_id, ())

    def _recipe_cost(self, recipe_id: int, stack: tuple[int, ...]) -> RecipeCost:
        if recipe_id in stack:
            raise CircularReferenceError(stack + (recipe_id,))
        if recipe_id in self._memo:
            return self._memo[recipe_id]

        recipe = self.source.recipe(recipe_id)
        line_costs: list[LineCost] = []
        allergens: dict[int, set[SourcePath]] = {}
        unconfirmed: set[SourcePath] = set()

        for line in recipe.lines:
            head = ((line.kind, line.ref_id),)
            if line.kind == MaterialKind.INGREDIENT:
                ing = self.source.ingredient(line.ref_id)
                price = ing.unit_price
                if not ing.allergen_confirmed:
                    unconfirmed.add(head)
                for allergen_id in ing.allergen_ids:
                    allergens.setdefault(allergen_id, set()).add(head)
            else:
                sub = self._recipe_cost(line.ref_id, stack + (recipe_id,))
                price = sub.cost_per_g
                if price is None:
                    raise EmptyRecipeError(line.ref_id, self.source.recipe(line.ref_id).name)
                unconfirmed.update(head + path for path in sub.unconfirmed_sources)
                for allergen_id, paths in sub.allergens.items():
                    allergens.setdefault(allergen_id, set()).update(head + p for p in paths)
            line_costs.append(LineCost(line, price, price * line.quantity_g))

        result = RecipeCost(
            recipe_id=recipe.id,
            lines=line_costs,
            total_cost=sum((lc.cost for lc in line_costs), ZERO),
            total_weight_g=sum((lc.line.quantity_g for lc in line_costs), ZERO),
            yield_rate=recipe.yield_rate,
            allergens=allergens,
            unconfirmed_sources=unconfirmed,
        )
        self._memo[recipe_id] = result
        return result


def would_create_cycle(source: DataSource, recipe_id: int, material_recipe_id: int) -> bool:
    """recipe_id に material_recipe_id を材料として追加すると循環するか（第62項3）。

    material_recipe_id から材料をたどって recipe_id に届くなら循環になる。
    """
    seen: set[int] = set()
    todo = [material_recipe_id]
    while todo:
        current = todo.pop()
        if current == recipe_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        todo.extend(
            line.ref_id
            for line in source.recipe(current).lines
            if line.kind == MaterialKind.RECIPE
        )
    return False


# --------------------------------------------------------------------------
# 変更前後の比較（価格変更・置き換えの影響確認に使う）
# --------------------------------------------------------------------------


class OverrideSource:
    """元の DataSource の一部だけを差し替えて見せる。確定前の「変更後」原価の試算用。"""

    def __init__(
        self,
        base: DataSource,
        ingredients: dict[int, IngredientData] | None = None,
        recipes: dict[int, RecipeData] | None = None,
    ):
        self.base = base
        self.ingredients = ingredients or {}
        self.recipes = recipes or {}

    def ingredient(self, ingredient_id: int) -> IngredientData:
        if ingredient_id in self.ingredients:
            return self.ingredients[ingredient_id]
        return self.base.ingredient(ingredient_id)

    def recipe(self, recipe_id: int) -> RecipeData:
        if recipe_id in self.recipes:
            return self.recipes[recipe_id]
        return self.base.recipe(recipe_id)


def replace_material(recipe: RecipeData, old_recipe_id: int, new_recipe_id: int) -> RecipeData:
    """材料の中間レシピ old を new に差し替えたレシピを返す（第39項 置き換え）。

    使用量・順番は変えない。
    """
    lines = tuple(
        Line(line.kind, new_recipe_id, line.quantity_g, line.line_id)
        if line.kind == MaterialKind.RECIPE and line.ref_id == old_recipe_id
        else line
        for line in recipe.lines
    )
    return RecipeData(recipe.id, recipe.name, recipe.yield_rate, lines)
