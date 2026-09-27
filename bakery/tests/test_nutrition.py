"""栄養成分（v26）のテスト。計算エンジン・表示の丸め・成分表の取り込み・画面。"""

from decimal import Decimal as D

from django.test import SimpleTestCase
from django.urls import reverse

from bakery.management.commands.import_food_composition import parse_value
from bakery.models import AuditLog, FoodComposition, Ingredient, Product
from bakery.services.costing import Line, MaterialKind, RecipeData
from bakery.services.nutrition import NutritionCalculator, Nutrients, label_rows, salt_from_sodium_mg
from bakery.tests.test_costing import FakeSource, ing
from bakery.tests.test_recipe_views import RecipeTestBase

ING, REC = MaterialKind.INGREDIENT, MaterialKind.RECIPE


def with_nutrition(data, nutrients):
    from dataclasses import replace

    return replace(data, nutrition=nutrients)


def baguette_nutrition_source():
    """一般的なバゲット生地の配合と、食材の100gあたり栄養（成分表に近い値）。"""
    n = {
        1: Nutrients(D(0), D(0), D(0), D(0), D(0)),  # 水
        2: Nutrients(D(337), D("11.8"), D("1.5"), D("71.7"), D(0)),  # 準強力粉
        3: Nutrients(D(0), D(0), D(0), D(0), D("99.5")),  # 塩
        4: Nutrients(D(307), D("37.1"), D("6.8"), D("43.1"), D("0.3")),  # イースト
        5: Nutrients(D(322), D("4.5"), D("0.4"), D(75), D("0.09")),  # モルト
    }
    ingredients = [with_nutrition(ing(i, f"食材{i}", 100, 1000), n[i]) for i in n]
    lines = tuple(Line(ING, i, D(q)) for i, q in [(1, 700), (2, 1000), (3, 20), (4, 4), (5, 3)])
    dough = RecipeData(100, "バゲット", D("0.98"), lines)
    product = RecipeData(200, "バゲット（商品）", D(1), (Line(REC, 100, D(250)),))
    return FakeSource(ingredients, [dough, product])


class NutritionEngineTests(SimpleTestCase):
    def test_total_and_uses_finished_weight(self):
        result = NutritionCalculator(baguette_nutrition_source()).recipe_nutrition(100)
        # 材料の熱量合計 3,370 + 12.28 + 9.66 = 3,391.94kcal
        # 既存Excelの方式（仕込み 1,727g で割る）なら 196.41kcal/100g
        self.assertEqual(result.total.energy_kcal, D("3391.94"))
        self.assertAlmostEqual(result.total.energy_kcal / D("17.27"), D("196.4065"), places=4)
        # v26：出来上がり量（1,692.46g）で割る → 200.41kcal/100g
        self.assertAlmostEqual(result.per_100g.energy_kcal, D("200.4148"), places=4)
        self.assertTrue(result.complete)

    def test_final_recipe_is_per_piece(self):
        result = NutritionCalculator(baguette_nutrition_source()).recipe_nutrition(200)
        # 250g × 200.41kcal/100g ＝ 501.04kcal（1個あたり）
        self.assertAlmostEqual(result.total.energy_kcal, D("501.0370"), places=4)
        self.assertEqual(result.total_weight_g, D(250))

    def test_missing_ingredient_is_reported_through_layers(self):
        source = baguette_nutrition_source()
        source.ingredients[3] = with_nutrition(source.ingredients[3], None)  # 塩を未入力に
        result = NutritionCalculator(source).recipe_nutrition(200)
        self.assertFalse(result.complete)
        self.assertEqual(result.missing_sources, {((REC, 100), (ING, 3))})

    def test_sodium_to_salt(self):
        self.assertEqual(salt_from_sodium_mg(D(600)), D("1.52"))

    def test_label_rounding_and_zero_rule(self):
        per_piece = Nutrients(D("487.48"), D("15.456"), D("0.3"), D("101.55"), D("1.349"))
        rows = {r.name: r.value for r in label_rows(per_piece, D(250))}
        self.assertEqual(rows, {
            "熱量": "487", "たんぱく質": "15.5",
            "脂質": "0.0",  # 100gあたり 0.12g（0.5g未満）なので 0 と表示できる
            "炭水化物": "101.6", "食塩相当量": "1.3",
        })
        # 100gあたりが基準以上なら、小さくても0にしない
        rows = {r.name: r.value for r in label_rows(Nutrients(D(3), D("0.3"), D(0), D(0), D("0.04")), D(10))}
        self.assertEqual((rows["熱量"], rows["たんぱく質"], rows["食塩相当量"]), ("3", "0.3", "0.0"))

    def test_parse_composition_values(self):
        self.assertEqual(parse_value(337), D("337"))
        self.assertEqual(parse_value("(11.3)"), D("11.3"))
        self.assertEqual(parse_value("Tr"), D("0"))
        self.assertEqual(parse_value("(Tr)"), D("0"))
        self.assertIsNone(parse_value("-"))
        self.assertIsNone(parse_value(""))
        self.assertIsNone(parse_value(None))


class NutritionViewTests(RecipeTestBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.food = FoodComposition.objects.create(
            food_number="01020", group="穀類", name="こむぎ ［小麦粉］ 強力粉 1等",
            energy_kcal=D(337), protein_g=D("11.8"), fat_g=D("1.5"), carbohydrate_g=D("71.7"), salt_g=D(0))

    def set_nutrition(self, ingredient, **values):
        self.client.force_login(self.admin)
        data = {"energy_kcal": "", "protein_g": "", "fat_g": "", "carbohydrate_g": "", "salt_g": "", **values}
        return self.client.post(reverse("ingredient_nutrition", args=[ingredient.pk]), data)

    def test_input_from_composition_table(self):
        self.client.force_login(self.admin)
        results = self.client.get(reverse("food_search"), {"q": "小麦粉 強力"})
        self.assertContains(results, "01020")
        self.assertContains(results, 'data-energy_kcal="337"')
        response = self.set_nutrition(self.flour, energy_kcal="337", protein_g="11.8", fat_g="1.5",
                                      carbohydrate_g="71.7", salt_g="0", nutrition_food=self.food.pk,
                                      nutrition_note="日本食品標準成分表（八訂）")
        self.assertRedirects(response, reverse("ingredient_detail", args=[self.flour.pk]))
        self.flour.refresh_from_db()
        self.assertTrue(self.flour.nutrition_complete)
        self.assertEqual(self.flour.nutrition_food, self.food)
        self.assertTrue(AuditLog.objects.filter(action="食材栄養成分変更", target_id=self.flour.pk).exists())
        detail = self.client.get(reverse("ingredient_detail", args=[self.flour.pk]))
        self.assertContains(detail, "337 kcal")
        self.assertContains(detail, "出典：日本食品標準成分表（八訂）")

    def test_partial_input_is_rejected_and_sodium_converts(self):
        response = self.set_nutrition(self.butter, energy_kcal="745", protein_g="0.6")
        self.assertContains(response, "脂質・炭水化物・食塩相当量も入力してください")
        self.set_nutrition(self.butter, energy_kcal="７４５", protein_g="0.6", fat_g="81", carbohydrate_g="0.2",
                           sodium_mg="590")
        self.butter.refresh_from_db()
        self.assertEqual(self.butter.salt_g, D("1.50"))
        # すべて空欄で保存すると未入力に戻る
        self.set_nutrition(self.butter)
        self.butter.refresh_from_db()
        self.assertFalse(self.butter.nutrition_complete)

    def test_general_user_cannot_edit_nutrition(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("ingredient_nutrition", args=[self.flour.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("food_search"), {"q": "小麦"}).status_code, 403)

    def test_recipe_and_product_nutrition(self):
        self.set_nutrition(self.flour, energy_kcal="337", protein_g="11.8", fat_g="1.5", carbohydrate_g="71.7", salt_g="0")
        self.set_nutrition(self.butter, energy_kcal="700", protein_g="0.6", fat_g="81", carbohydrate_g="0.2", salt_g="1.9")
        self.client.force_login(self.staff)
        dough = self.create("パン生地")
        self.add(dough, self.flour, 1000)
        self.add(dough, self.butter, 100)
        bread = self.create("バターロール", kind="final", recipe_type="")
        self.add(bread, dough, 60)
        # 生地：熱量 3370 + 700 = 4070kcal ÷ 出来上がり 1078g → 377.55kcal/100g
        detail = self.client.get(reverse("recipe_detail", args=[dough.pk]))
        self.assertContains(detail, "377.6 kcal")
        self.assertContains(detail, "4,070.0 kcal")
        # 1個（生地60g）：226.53kcal、たんぱく質 6.60g、脂質 5.34g、炭水化物 39.92g、食塩 0.106g
        product = Product.objects.create(company=self.company, code="ITEM-001", name="バターロール",
                                         price_excluding_tax=200, adopted_recipe=bread)
        page = self.client.get(reverse("product_detail", args=[product.pk]))
        for text in ["栄養成分表示", "1個当たり", "227kcal", "6.6g", "5.3g", "39.9g", "0.1g", "推定値"]:
            self.assertContains(page, text)

    def test_missing_nutrition_blocks_label(self):
        self.set_nutrition(self.flour, energy_kcal="337", protein_g="11.8", fat_g="1.5", carbohydrate_g="71.7", salt_g="0")
        self.client.force_login(self.staff)
        dough = self.create("パン生地")
        self.add(dough, self.flour, 1000)
        self.add(dough, self.salt, 20)  # 塩は未入力
        bread = self.create("塩パン", kind="final", recipe_type="")
        self.add(bread, dough, 60)
        product = Product.objects.create(company=self.company, code="ITEM-001", name="塩パン",
                                         price_excluding_tax=200, adopted_recipe=bread)
        page = self.client.get(reverse("product_detail", args=[product.pk]))
        self.assertContains(page, "栄養成分表示は作れません")
        self.assertContains(page, "パン生地 → 塩")
        self.assertNotContains(page, "推定値")
        listing = self.client.get(reverse("ingredient_list"), {"nutrition": "missing"})
        self.assertContains(listing, "塩")
        self.assertNotContains(listing, "強力粉")
