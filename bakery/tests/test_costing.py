"""原価計算エンジンのテスト。DBを使わないので PostgreSQL なしでも実行できる。"""

import random
from decimal import Decimal as D

from django.test import SimpleTestCase

from bakery.services.costing import (
    CircularReferenceError,
    CostCalculator,
    EmptyRecipeError,
    IngredientData,
    Line,
    MaterialKind,
    OverrideSource,
    RecipeData,
    UnitPriceRounding,
    CostRounding,
    cost_rate_percent,
    round_product_cost,
    price_including_tax,
    replace_material,
    unit_price,
    would_create_cycle,
)

ING = MaterialKind.INGREDIENT
REC = MaterialKind.RECIPE
YIELD = D("0.98")

# アレルゲンID
WHEAT, MILK, ALMOND = 1, 2, 3


class FakeSource:
    def __init__(self, ingredients=(), recipes=()):
        self.ingredients = {i.id: i for i in ingredients}
        self.recipes = {r.id: r for r in recipes}

    def ingredient(self, ingredient_id):
        return self.ingredients[ingredient_id]

    def recipe(self, recipe_id):
        return self.recipes[recipe_id]


def ing(id, name, price, weight, rounding=UnitPriceRounding.NONE, allergens=(), confirmed=True):
    return IngredientData(
        id, name, unit_price(price, weight, rounding), confirmed, frozenset(allergens)
    )


def line(kind, ref_id, grams):
    return Line(kind, ref_id, D(str(grams)))


class UnitPriceTests(SimpleTestCase):
    def test_spec_example(self):
        # spec.md 第11項：25000g・5000円 → 0.20円/g
        self.assertEqual(unit_price(5000, 25000), D("0.2"))

    def test_no_rounding_keeps_full_precision(self):
        self.assertEqual(unit_price(3730, 25000), D("0.1492"))

    def test_excel_roundup(self):
        # 既存Excelと同じ ROUNDUP(3730/25000, 2) = 0.15
        self.assertEqual(unit_price(3730, 25000, UnitPriceRounding.EXCEL_ROUNDUP_2), D("0.15"))
        self.assertEqual(unit_price(800, 500, UnitPriceRounding.EXCEL_ROUNDUP_2), D("1.60"))

    def test_weight_must_be_positive(self):
        for weight in (0, -1):
            with self.assertRaises(ValueError):
                unit_price(100, weight)


class TaxAndRateTests(SimpleTestCase):
    def test_tax_is_floored(self):
        # 既存Excelと同じ INT（切り捨て）：280×1.08=302.4 → 302、210×1.08=226.8 → 226
        self.assertEqual(price_including_tax(280, D("0.08")), 302)
        self.assertEqual(price_including_tax(210, D("0.08")), 226)
        self.assertEqual(price_including_tax(250, D("0.08")), 270)

    def test_cost_rate(self):
        self.assertEqual(cost_rate_percent(D("70"), 280), D("25"))

    def test_cost_rate_without_price(self):
        self.assertIsNone(cost_rate_percent(D("70"), 0))


def baguette_source(rounding=UnitPriceRounding.EXCEL_ROUNDUP_2):
    """一般的なバゲット生地の配合と、架空の仕入れ値（既存Excelと同じ計算方式を確かめるためのサンプル）。"""
    ingredients = [
        ing(1, "水", 0, 1000, rounding),
        ing(2, "準強力粉", 3730, 25000, rounding, [WHEAT]),  # 0.1492 → 切り上げ 0.15
        ing(3, "塩", 7950, 10000, rounding),  # 0.795 → 0.80
        ing(4, "インスタントドライイースト", 760, 500, rounding),  # 1.52
        ing(5, "モルトシロップ", 4150, 5000, rounding),  # 0.83
    ]
    baguette = RecipeData(
        100,
        "バゲット",
        YIELD,
        (
            line(ING, 1, 700),
            line(ING, 2, 1000),
            line(ING, 3, 20),
            line(ING, 4, 4),
            line(ING, 5, 3),
        ),
    )
    product = RecipeData(200, "バゲット（商品）", D("1"), (line(REC, 100, 250),))
    return FakeSource(ingredients, [baguette, product])


class RecipeCostTests(SimpleTestCase):
    def test_excel_style_baguette(self):
        # 既存Excelと同じ方式（1g単価を小数第2位へ切り上げ、出来高＝仕込み×98%）：
        # 150 + 16 + 6.08 + 2.49 = 174.57円、仕込み 1727g、出来高 1692.46g
        cost = CostCalculator(baguette_source()).recipe_cost(100)
        self.assertEqual(cost.total_cost, D("174.57"))
        self.assertEqual(cost.total_weight_g, D("1727"))
        self.assertEqual(cost.finished_weight_g, D("1692.46"))
        self.assertAlmostEqual(cost.cost_per_g, D("174.57") / D("1692.46"), places=15)

    def test_excel_style_baguette_product(self):
        # 250g → 原価 25.786…円、売価280円で原価率 9.209…%
        cost = CostCalculator(baguette_source()).recipe_cost(200)
        self.assertAlmostEqual(cost.total_cost, D("25.78642922137007669"), places=12)
        self.assertAlmostEqual(cost_rate_percent(cost.total_cost, 280), D("9.209439"), places=5)

    def test_product_cost_is_rounded_up_to_one_decimal(self):
        # v23 決定：1g単価は丸めず、商品原価を小数第1位へ切り上げ、原価率もその値で計算する
        cost = CostCalculator(baguette_source(UnitPriceRounding.NONE)).recipe_cost(200)
        self.assertAlmostEqual(cost.total_cost, D("25.6535"), places=4)
        product_cost = round_product_cost(cost.total_cost)
        self.assertEqual(product_cost, D("25.7"))
        self.assertEqual(f"{cost_rate_percent(product_cost, 280):.2f}", "9.18")
        self.assertEqual(round_product_cost(D("22.3")), D("22.3"))  # ちょうどなら上げない
        self.assertEqual(round_product_cost(D("22.3000001")), D("22.4"))
        self.assertEqual(round_product_cost(D("22.31"), CostRounding.NONE), D("22.31"))

    def test_final_recipe_has_no_yield_loss(self):
        cost = CostCalculator(baguette_source()).recipe_cost(200)
        self.assertEqual(cost.finished_weight_g, D("250"))

    def test_line_order_does_not_change_cost(self):
        source = baguette_source()
        before = CostCalculator(source).recipe_cost(100).total_cost
        recipe = source.recipes[100]
        shuffled = list(recipe.lines)
        random.Random(1).shuffle(shuffled)
        source.recipes[100] = RecipeData(recipe.id, recipe.name, recipe.yield_rate, tuple(shuffled))
        self.assertEqual(CostCalculator(source).recipe_cost(100).total_cost, before)

    def test_duplicate_material_lines_are_kept_and_summed(self):
        # 第27項：工程1 水50g、工程3 水50g → 明細は2行、合算は100g
        source = FakeSource(
            [ing(1, "水", 0, 1000), ing(2, "強力粉", 5000, 25000)],
            [
                RecipeData(
                    1,
                    "生地",
                    YIELD,
                    (line(ING, 1, 50), line(ING, 2, 1000), line(ING, 1, 50)),
                )
            ],
        )
        cost = CostCalculator(source).recipe_cost(1)
        self.assertEqual(len(cost.lines), 3)
        water = [m for m in cost.aggregated() if m.ref_id == 1][0]
        self.assertEqual(water.quantity_g, D("100"))
        self.assertEqual(cost.total_cost, D("200"))

    def test_nested_intermediate_recipes(self):
        # ダマンド → シナモンダマンド → シナモンロール（4階層目は商品）
        source = FakeSource(
            [
                ing(1, "アーモンドプードル", 2000, 1000, allergens=[ALMOND]),
                ing(2, "バター", 1000, 1000, allergens=[MILK]),
                ing(3, "シナモン", 3000, 1000),
                ing(4, "デニッシュ用小麦粉", 500, 1000, allergens=[WHEAT]),
            ],
            [
                RecipeData(10, "ダマンド", YIELD, (line(ING, 1, 500), line(ING, 2, 480))),
                RecipeData(11, "シナモンダマンド", YIELD, (line(REC, 10, 450), line(ING, 3, 50))),
                RecipeData(12, "デニッシュ", YIELD, (line(ING, 4, 980),)),
                RecipeData(20, "シナモンロール", D("1"), (line(REC, 12, 80), line(REC, 11, 40))),
            ],
        )
        calc = CostCalculator(source)
        damande = calc.recipe_cost(10)
        # (500×2 + 480×1) ÷ (980×0.98)
        self.assertEqual(damande.total_cost, D("1480"))
        self.assertEqual(damande.cost_per_g, D("1480") / D("960.4"))

        cinnamon = calc.recipe_cost(11)
        expected_cinnamon_cost = damande.cost_per_g * 450 + D("3") * 50
        self.assertEqual(cinnamon.total_cost, expected_cinnamon_cost)

        roll = calc.recipe_cost(20)
        danish_per_g = D("490") / (D("980") * YIELD)
        expected = danish_per_g * 80 + cinnamon.cost_per_g * 40
        self.assertEqual(roll.total_cost, expected)

        # アレルゲンの由来：アーモンド ← シナモンダマンド ← ダマンド ← アーモンドプードル
        self.assertEqual(
            roll.allergens[ALMOND],
            {((REC, 11), (REC, 10), (ING, 1))},
        )
        self.assertEqual(set(roll.allergens), {ALMOND, MILK, WHEAT})
        self.assertFalse(roll.has_unconfirmed_allergen)

    def test_unconfirmed_allergen_propagates(self):
        source = FakeSource(
            [ing(1, "小麦粉", 500, 1000, confirmed=True), ing(2, "謎の粉", 500, 1000, confirmed=False)],
            [
                RecipeData(1, "生地", YIELD, (line(ING, 1, 100), line(ING, 2, 10))),
                RecipeData(2, "商品", D("1"), (line(REC, 1, 50),)),
            ],
        )
        cost = CostCalculator(source).recipe_cost(2)
        self.assertTrue(cost.has_unconfirmed_allergen)
        self.assertEqual(cost.unconfirmed_sources, {((REC, 1), (ING, 2))})

    def test_empty_intermediate_recipe_used_as_material(self):
        source = FakeSource(
            [],
            [
                RecipeData(1, "空の生地", YIELD, ()),
                RecipeData(2, "商品", D("1"), (line(REC, 1, 50),)),
            ],
        )
        with self.assertRaises(EmptyRecipeError):
            CostCalculator(source).recipe_cost(2)


class CycleTests(SimpleTestCase):
    def source(self):
        return FakeSource(
            [ing(1, "粉", 100, 100)],
            [
                RecipeData(1, "A", YIELD, (line(REC, 2, 10),)),
                RecipeData(2, "B", YIELD, (line(REC, 3, 10),)),
                RecipeData(3, "C", YIELD, (line(ING, 1, 10),)),
            ],
        )

    def test_would_create_cycle(self):
        source = self.source()
        self.assertTrue(would_create_cycle(source, 3, 1))  # C に A を入れる → A→B→C→A
        self.assertTrue(would_create_cycle(source, 1, 1))  # 自分自身
        self.assertFalse(would_create_cycle(source, 1, 3))  # A に C を入れても循環しない

    def test_calculator_detects_cycle(self):
        source = self.source()
        source.recipes[3] = RecipeData(3, "C", YIELD, (line(REC, 1, 10),))
        with self.assertRaises(CircularReferenceError):
            CostCalculator(source).recipe_cost(1)


class PreviewTests(SimpleTestCase):
    def test_price_change_preview_does_not_touch_base(self):
        base = baguette_source(UnitPriceRounding.NONE)
        before = CostCalculator(base).recipe_cost(200).total_cost
        new_flour = ing(2, "準強力粉", 5000, 25000, allergens=[WHEAT])
        after = CostCalculator(OverrideSource(base, ingredients={2: new_flour})).recipe_cost(200)
        self.assertGreater(after.total_cost, before)
        self.assertEqual(CostCalculator(base).recipe_cost(200).total_cost, before)

    def test_replace_material_keeps_quantity_and_order(self):
        recipe = RecipeData(5, "上位", YIELD, (line(ING, 1, 10), line(REC, 7, 20), line(REC, 8, 30)))
        replaced = replace_material(recipe, 7, 9)
        self.assertEqual(
            [(l.kind, l.ref_id, l.quantity_g) for l in replaced.lines],
            [(ING, 1, D("10")), (REC, 9, D("20")), (REC, 8, D("30"))],
        )
