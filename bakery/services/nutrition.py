"""栄養成分の計算（v26、spec.md 第21・67項）。画面・DBから独立した計算だけを置く。

単位の約束:
    食材          可食部100gあたり（熱量 kcal、ほか g）
    中間レシピ    材料の栄養の合計 ÷ 出来上がり量（仕込み量 × 歩留まり）。原価の1g単価と同じ考え方で、
                  減った重さは主に水分とみなし、栄養は残る
    最終商品レシピ 材料の栄養の合計 ＝ 商品1個あたり

栄養値が未入力の食材は0として足し合わせるが、未入力の材料への経路（missing_sources）を必ず残し、
画面では「栄養値未入力の材料あり」と出す。未入力を「0」として正しい値のように見せない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from bakery.services.costing import CircularReferenceError, DataSource, MaterialKind, SourcePath

ZERO = Decimal("0")
HUNDRED = Decimal("100")
SODIUM_TO_SALT = Decimal("2.54") / Decimal("1000")  # ナトリウム(mg) × 2.54 ÷ 1000 ＝ 食塩相当量(g)


@dataclass(frozen=True)
class Nutrients:
    energy_kcal: Decimal = ZERO
    protein_g: Decimal = ZERO
    fat_g: Decimal = ZERO
    carbohydrate_g: Decimal = ZERO
    salt_g: Decimal = ZERO

    FIELDS = ("energy_kcal", "protein_g", "fat_g", "carbohydrate_g", "salt_g")

    def __add__(self, other: Nutrients) -> Nutrients:
        return Nutrients(*(getattr(self, f) + getattr(other, f) for f in self.FIELDS))

    def scaled(self, factor: Decimal) -> Nutrients:
        return Nutrients(*(getattr(self, f) * factor for f in self.FIELDS))

    @classmethod
    def from_object(cls, obj) -> Nutrients | None:
        """同じ名前の属性を持つもの（食材・成分表）から作る。1つでも空欄なら None（未入力）。"""
        values = [getattr(obj, f) for f in cls.FIELDS]
        if any(v is None for v in values):
            return None
        return cls(*(Decimal(v) for v in values))


def salt_from_sodium_mg(sodium_mg: Decimal) -> Decimal:
    return (Decimal(sodium_mg) * SODIUM_TO_SALT).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class RecipeNutrition:
    recipe_id: int
    total: Nutrients  # レシピ全量の栄養（最終商品レシピなら商品1個あたり）
    total_weight_g: Decimal  # 仕込み量
    finished_weight_g: Decimal  # 出来上がり量
    missing_sources: set[SourcePath] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return not self.missing_sources

    @property
    def per_100g(self) -> Nutrients | None:
        """出来上がり100gあたり。"""
        if self.finished_weight_g <= ZERO:
            return None
        return self.total.scaled(HUNDRED / self.finished_weight_g)


class NutritionCalculator:
    """DataSource からレシピの栄養を計算する。IngredientData.nutrition（100gあたりの Nutrients か None）を使う。"""

    def __init__(self, source: DataSource):
        self.source = source
        self._memo: dict[int, RecipeNutrition] = {}

    def recipe_nutrition(self, recipe_id: int) -> RecipeNutrition:
        return self._calc(recipe_id, ())

    def _calc(self, recipe_id: int, stack: tuple[int, ...]) -> RecipeNutrition:
        if recipe_id in stack:
            raise CircularReferenceError(stack + (recipe_id,))
        if recipe_id in self._memo:
            return self._memo[recipe_id]

        recipe = self.source.recipe(recipe_id)
        total = Nutrients()
        weight = ZERO
        missing: set[SourcePath] = set()
        for line in recipe.lines:
            head = ((line.kind, line.ref_id),)
            weight += line.quantity_g
            if line.kind == MaterialKind.INGREDIENT:
                per_100g = getattr(self.source.ingredient(line.ref_id), "nutrition", None)
                if per_100g is None:
                    missing.add(head)
                    continue
                total = total + per_100g.scaled(line.quantity_g / HUNDRED)
            else:
                sub = self._calc(line.ref_id, stack + (recipe_id,))
                missing.update(head + path for path in sub.missing_sources)
                sub_per_100g = sub.per_100g
                if sub_per_100g is None:
                    missing.add(head)  # 材料のない中間レシピ
                    continue
                total = total + sub_per_100g.scaled(line.quantity_g / HUNDRED)

        result = RecipeNutrition(recipe_id, total, weight, weight * recipe.yield_rate, missing)
        self._memo[recipe_id] = result
        return result


# --------------------------------------------------------------------------
# 栄養成分表示（食品表示基準 別表第九）
# --------------------------------------------------------------------------

# (項目, 表示名, 単位, 表示する小数の桁, 「0」と表示できる100gあたりの量)
# たんぱく質・脂質・炭水化物の最小表示の位は1の位だが、より細かい位も表示できるため小数第1位まで出す
LABEL_ROWS = (
    ("energy_kcal", "熱量", "kcal", 0, Decimal("5")),
    ("protein_g", "たんぱく質", "g", 1, Decimal("0.5")),
    ("fat_g", "脂質", "g", 1, Decimal("0.5")),
    ("carbohydrate_g", "炭水化物", "g", 1, Decimal("0.5")),
    ("salt_g", "食塩相当量", "g", 1, Decimal("0.05")),
)


@dataclass(frozen=True)
class LabelRow:
    name: str
    value: str
    unit: str


def label_rows(per_piece: Nutrients, piece_weight_g: Decimal) -> list[LabelRow]:
    """1個あたりの栄養 → 栄養成分表示の各行。表示する位の1つ下を四捨五入する。

    100gあたりの量が「0と表示できる量」未満なら 0 と表示する（100gあたりは仕込み重量から求める）。
    """
    rows = []
    for field_name, name, unit, places, zero_below in LABEL_ROWS:
        value = getattr(per_piece, field_name)
        per_100g = value * HUNDRED / piece_weight_g if piece_weight_g > ZERO else value
        if per_100g < zero_below:
            value = ZERO
        quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        rows.append(LabelRow(name, f"{quantized:,.{places}f}", unit))
    return rows
