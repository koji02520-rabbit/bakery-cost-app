"""レシピ画面（S20〜S25）のテスト。"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from bakery.models import (
    AllergenItem,
    AllergenStatus,
    AuditLog,
    Company,
    CostSnapshot,
    IdSequence,
    Ingredient,
    IngredientCategory,
    Membership,
    Product,
    Recipe,
    RecipeItem,
    RecipeKind,
    RecipeReplacement,
    Role,
    Status,
    Supplier,
)


class _Null:
    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass


class RecipeTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user("admin", password="x")
        cls.staff = User.objects.create_user("staff", password="x")
        call_command("setup_initial_data", company="テスト会社", store="本店", admin="admin", stdout=_Null())
        cls.company = Company.objects.get(name="テスト会社")
        Membership.objects.create(user=cls.staff, company=cls.company, role=Role.GENERAL)
        own = Supplier.objects.get(company=cls.company, name="自社")
        flour_cat = IngredientCategory.objects.get(company=cls.company, code="A")
        dairy_cat = IngredientCategory.objects.get(company=cls.company, code="L")
        wheat = AllergenItem.objects.get(company=cls.company, name="小麦")
        milk = AllergenItem.objects.get(company=cls.company, name="乳")

        def ingredient(name, cat, weight, price, allergens=(), confirmed=True):
            obj = Ingredient.objects.create(
                company=cls.company, code=IdSequence.issue(cls.company, cat.id_prefix), name=name, category=cat,
                supplier=own, purchase_weight_g=weight, purchase_price=price,
                allergen_status=AllergenStatus.CONFIRMED if confirmed else AllergenStatus.UNCONFIRMED,
            )
            obj.allergens.set(allergens)
            return obj

        cls.flour = ingredient("強力粉", flour_cat, 25000, 5000, [wheat])  # 0.2円/g
        cls.butter = ingredient("バター", dairy_cat, 450, 900, [milk])  # 2円/g
        cls.salt = ingredient("塩", flour_cat, 1000, 100, confirmed=False)  # 0.1円/g・未確認

    def setUp(self):
        self.client.force_login(self.staff)

    # ---- 操作の近道 ----
    def create(self, name, kind="intermediate", recipe_type="dough"):
        self.client.post(reverse("recipe_new"), {"mode": "new", "kind": kind, "recipe_type": recipe_type, "name": name})
        return Recipe.objects.filter(name=name).latest("pk")

    def add(self, recipe, material, qty, step=""):
        kind = "ingredient" if isinstance(material, Ingredient) else "recipe"
        return self.client.post(reverse("recipe_item_add", args=[recipe.pk]),
                                {"kind": kind, "ref": material.pk, "quantity_g": str(qty), "step_label": step})

    def make_dough_and_bread(self):
        """生地（中間）＝強力粉1000g＋バター100g、パン（最終）＝生地60g。商品に採用。"""
        dough = self.create("パン生地")
        self.add(dough, self.flour, 1000, "1")
        self.add(dough, self.butter, 100, "2")
        bread = self.create("バターロール", kind="final", recipe_type="")
        self.add(bread, dough, 60)
        product = Product.objects.create(company=self.company, code="ITEM-001", name="バターロール",
                                         price_excluding_tax=200, adopted_recipe=bread)
        return dough, bread, product


class CreateTests(RecipeTestBase):
    def test_create_intermediate_and_final(self):
        dough = self.create("パン生地")
        self.assertEqual((dough.code, dough.version, dough.yield_rate), ("RECIPE-001", 1, Decimal("0.98")))
        bread = self.create("バターロール", kind="final", recipe_type="")
        self.assertEqual((bread.code, bread.yield_rate, bread.recipe_type), ("RECIPE-002", Decimal("1"), ""))
        self.assertEqual(bread.created_by, self.staff)  # 一般ユーザーも作れる
        self.assertTrue(AuditLog.objects.filter(action="レシピ作成", target_id=dough.pk).exists())

    def test_create_errors(self):
        response = self.client.post(reverse("recipe_new"), {"mode": "new", "kind": "intermediate", "name": "生地"})
        self.assertContains(response, "中間レシピの種類を選んでください")
        response = self.client.post(reverse("recipe_new"), {"mode": "new", "name": ""})
        self.assertContains(response, "レシピ区分（中間レシピ／最終商品レシピ）を選んでください")
        self.assertContains(response, "レシピ名を入力してください")
        response = self.client.post(reverse("recipe_new"), {"mode": "copy", "name": "生地"})
        self.assertContains(response, "コピー元のレシピを選んでください")
        self.assertFalse(Recipe.objects.exists())

    def test_copy_with_new_name_starts_at_v1_and_keeps_source(self):
        dough = self.create("パン生地")
        self.add(dough, self.flour, 1000, "1")
        self.client.post(reverse("recipe_new"), {"mode": "copy", "source": dough.pk, "name": "全粒粉生地"})
        copy = Recipe.objects.get(name="全粒粉生地")
        self.assertEqual((copy.version, copy.copied_from, copy.kind), (1, dough, RecipeKind.INTERMEDIATE))
        self.assertEqual(copy.items.get().quantity_g, Decimal("1000"))
        self.assertEqual(dough.items.count(), 1)

    def test_new_version_of_in_use_recipe(self):
        dough, bread, product = self.make_dough_and_bread()
        response = self.client.post(reverse("recipe_new_version", args=[dough.pk]))
        v2 = Recipe.objects.get(name="パン生地", version=2)
        self.assertRedirects(response, reverse("recipe_edit", args=[v2.pk]))
        self.assertEqual(v2.copied_from, dough)
        self.assertNotEqual(v2.code, dough.code)
        self.assertEqual(list(v2.items.values_list("step_label", flat=True)), ["1", "2"])
        # 新しいバージョンを作っただけでは、上位レシピは旧バージョンのまま（第39項）
        self.assertEqual(bread.items.get().material_recipe, dough)


class EditorTests(RecipeTestBase):
    def test_costs_are_recalculated(self):
        dough = self.create("パン生地")
        response = self.add(dough, self.flour, 1000)
        # 強力粉 0.2円/g × 1000g = 200円、仕込み1000g → 出来上がり980g
        self.assertContains(response, "200.00 円")
        self.assertContains(response, "1,000g → 980g")
        self.assertContains(response, "0.204 円/g")  # 200 ÷ 980
        self.assertEqual(response["HX-Trigger"], "picker-close")

    def test_same_material_in_several_rows_is_summed(self):
        dough = self.create("パン生地")
        self.add(dough, self.flour, 500, "1")
        self.add(dough, self.flour, 300, "3")
        self.assertEqual(dough.items.count(), 2)  # 行はまとめない
        response = self.client.get(reverse("recipe_edit", args=[dough.pk]) + "?view=total",
                                   headers={"HX-Request": "true"})
        self.assertContains(response, "800 g")
        self.assertContains(response, "160.00 円")

    def test_final_recipe_cost_uses_intermediate_cost_per_gram(self):
        dough, bread, product = self.make_dough_and_bread()
        # 生地：200 + 200 = 400円 ÷ (1100g × 0.98) → 60g で 22.26…円 → 商品原価は切り上げて 22.3円
        response = self.client.get(reverse("recipe_detail", args=[bread.pk]))
        self.assertContains(response, "22.26 円")  # 材料行の原価は丸めない
        self.assertContains(response, "22.3 円")
        self.assertContains(response, "小麦")
        self.assertContains(response, "パン生地 → 強力粉")  # アレルゲンの由来

    def test_quantity_errors(self):
        dough = self.create("パン生地")
        for qty, message in [("", "使用量を入力してください"), ("0", "使用量は0gより大きい数で入力してください"),
                             ("abc", "使用量は数字で入力してください")]:
            with self.subTest(qty=qty):
                response = self.add(dough, self.flour, qty)
                self.assertContains(response, message)
                self.assertEqual(response["HX-Retarget"], "#picker-error")
        self.assertFalse(dough.items.exists())

    def test_material_rules(self):
        dough, bread, product = self.make_dough_and_bread()
        draft = self.create("下書き", kind="final", recipe_type="")
        empty = self.create("空の生地")
        cases = [
            (bread, "最終商品レシピは選べません"),
            (empty, "まだ材料がないため選べません"),
        ]
        for material, message in cases:
            with self.subTest(message=message):
                self.assertContains(self.add(draft, material, 10), message)
        self.salt.status = Status.STOPPED
        self.salt.save()
        self.assertContains(self.add(draft, self.salt, 10), "使用停止中のため選べません")

    def test_cycle_is_prevented(self):
        # ほかのレシピの材料になっているレシピは使用中で材料を追加できないため、
        # 追加で起きる循環は「自分自身を材料にする」場合だけ（多階層の循環は置き換えのテストで確認）
        a = self.create("A")
        self.add(a, self.flour, 100)
        self.assertContains(self.add(a, a, 10), "循環参照")
        picker = self.client.get(reverse("recipe_picker", args=[a.pk]), {"scope": "recipe"})
        self.assertContains(picker, "このレシピ自身を材料に使うことになるため選べません")

    def test_update_delete_reorder_and_change_material(self):
        dough = self.create("パン生地")
        self.add(dough, self.flour, 1000, "1")
        self.add(dough, self.butter, 100, "2")
        first, second = dough.items.order_by("sort_order")
        self.client.post(reverse("recipe_item_update", args=[first.pk]), {"quantity_g": "１２００", "step_label": "ミキシング"})
        first.refresh_from_db()
        self.assertEqual((first.quantity_g, first.step_label), (Decimal("1200"), "ミキシング"))
        self.client.post(reverse("recipe_reorder", args=[dough.pk]), {"order": f"{second.pk},{first.pk}"})
        self.assertEqual(list(dough.items.order_by("sort_order").values_list("pk", flat=True)), [second.pk, first.pk])
        self.client.post(reverse("recipe_item_material", args=[second.pk]), {"kind": "ingredient", "ref": self.salt.pk})
        second.refresh_from_db()
        self.assertEqual((second.ingredient, second.quantity_g), (self.salt, Decimal("100")))
        self.client.post(reverse("recipe_item_delete", args=[second.pk]))
        self.assertEqual(dough.items.count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="レシピ材料削除", target_id=dough.pk).exists())

    def test_in_use_recipe_is_locked_except_step_and_order(self):
        dough, bread, product = self.make_dough_and_bread()
        flour_row = dough.items.get(ingredient=self.flour)
        self.assertContains(self.add(dough, self.salt, 10), "使用中のため")
        self.assertContains(self.client.post(reverse("recipe_item_update", args=[flour_row.pk]),
                                             {"quantity_g": "999", "step_label": "1"}), "使用中のため")
        self.client.post(reverse("recipe_item_delete", args=[flour_row.pk]))
        self.assertEqual(dough.items.count(), 2)
        # 工程と並び順は変えられる
        self.client.post(reverse("recipe_item_update", args=[flour_row.pk]), {"quantity_g": "1000", "step_label": "捏ね"})
        flour_row.refresh_from_db()
        self.assertEqual(flour_row.step_label, "捏ね")
        ids = list(dough.items.order_by("-sort_order").values_list("pk", flat=True))
        self.client.post(reverse("recipe_reorder", args=[dough.pk]), {"order": ",".join(map(str, ids))})
        self.assertEqual(list(dough.items.order_by("sort_order").values_list("pk", flat=True)), ids)
        page = self.client.get(reverse("recipe_edit", args=[dough.pk]))
        self.assertContains(page, "このレシピは使用中です")
        self.assertNotContains(page, "＋ 材料を追加する")

    def test_info_edit_of_in_use_recipe(self):
        dough, bread, product = self.make_dough_and_bread()
        response = self.client.post(reverse("recipe_info_edit", args=[dough.pk]),
                                    {"name": "パン生地（改）", "preparation": "", "procedure": "こねる", "memo": ""})
        self.assertRedirects(response, reverse("recipe_detail", args=[dough.pk]))
        dough.refresh_from_db()
        self.assertEqual((dough.name, dough.procedure, dough.recipe_type), ("パン生地（改）", "こねる", "dough"))
        log = AuditLog.objects.get(action="レシピ情報修正")
        self.assertEqual(log.before["name"], "パン生地")

    def test_unconfirmed_allergen_warning(self):
        dough = self.create("パン生地")
        self.add(dough, self.salt, 10)
        self.assertContains(self.client.get(reverse("recipe_detail", args=[dough.pk])), "アレルゲン未確認の材料があります")


class PagesTests(RecipeTestBase):
    def test_every_page_opens(self):
        dough, bread, product = self.make_dough_and_bread()
        self.client.force_login(self.admin)
        for name, args in [("recipe_list", []), ("recipe_new", []), ("recipe_detail", [dough.pk]),
                           ("recipe_detail", [bread.pk]), ("recipe_edit", [dough.pk]), ("recipe_info_edit", [bread.pk]),
                           ("recipe_picker", [bread.pk]), ("recipe_delete", [dough.pk]), ("recipe_replace", []),
                           ("home", [])]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_list_filters(self):
        dough, bread, product = self.make_dough_and_bread()
        draft = self.create("試作生地")
        url = reverse("recipe_list")
        self.assertContains(self.client.get(url, {"state": "draft"}), "試作生地")
        self.assertNotContains(self.client.get(url, {"state": "draft"}), "バターロール")
        self.assertContains(self.client.get(url, {"state": "in_use"}), "パン生地")
        self.assertNotContains(self.client.get(url, {"kind": "final"}), "試作生地")
        self.assertContains(self.client.get(url, {"q": draft.code}), "試作生地")
        self.assertContains(self.client.get(url), "22.3 円/個")

    def test_home_search_finds_recipes(self):
        self.create("クロワッサン生地")
        self.assertContains(self.client.get(reverse("home"), {"q": "クロワ"}), "クロワッサン生地")


class AdminOnlyTests(RecipeTestBase):
    def test_general_user_cannot_stop_delete_or_replace(self):
        dough, bread, product = self.make_dough_and_bread()
        self.assertEqual(self.client.post(reverse("recipe_set_status", args=[dough.pk]), {"status": "stopped"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("recipe_delete", args=[dough.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("recipe_replace")).status_code, 403)

    def test_delete_draft_but_not_in_use(self):
        dough, bread, product = self.make_dough_and_bread()
        draft = self.create("試作")
        self.add(draft, self.flour, 10)
        self.client.force_login(self.admin)
        self.assertContains(self.client.post(reverse("recipe_delete", args=[dough.pk])), "使用中のため削除できません")
        self.assertRedirects(self.client.post(reverse("recipe_delete", args=[draft.pk])), reverse("recipe_list"))
        self.assertFalse(Recipe.objects.filter(pk=draft.pk).exists())
        self.assertEqual(self.create("次").code, "RECIPE-004")  # 削除したIDは再利用しない

    def test_stopped_recipe_cannot_be_chosen(self):
        dough, bread, product = self.make_dough_and_bread()
        self.client.force_login(self.admin)
        self.client.post(reverse("recipe_set_status", args=[dough.pk]), {"status": "stopped"})
        draft = self.create("試作", kind="final", recipe_type="")
        self.assertContains(self.add(draft, dough, 10), "使用停止中のため選べません")


class ReplaceTests(RecipeTestBase):
    def test_replace_intermediate(self):
        dough, bread, product = self.make_dough_and_bread()
        v2 = Recipe.objects.get(pk=self.client.post(reverse("recipe_new_version", args=[dough.pk])).url.split("/")[2])
        butter_row = v2.items.get(ingredient=self.butter)
        self.client.post(reverse("recipe_item_update", args=[butter_row.pk]), {"quantity_g": "200", "step_label": "2"})

        self.client.force_login(self.admin)
        url = reverse("recipe_replace")
        preview = self.client.get(url, {"old": dough.pk, "new": v2.pk})
        # 旧：400円 ÷ 1078g × 60g = 22.26…円、新：600円 ÷ 1176g × 60g = 30.61…円（商品原価は小数第1位へ切り上げ）
        self.assertContains(preview, "22.3 円")
        self.assertContains(preview, "30.7 円")
        self.assertContains(preview, "+8.4 円")
        self.assertContains(preview, "11.2% → 15.4%")
        self.assertEqual(bread.items.get().material_recipe, dough)  # 確認画面ではまだ変えない

        response = self.client.post(url, {"old": dough.pk, "new": v2.pk})
        self.assertRedirects(response, f"{url}?done=1&old={dough.pk}&new={v2.pk}")
        item = bread.items.get()
        self.assertEqual((item.material_recipe, item.quantity_g), (v2, Decimal("60")))
        bread.refresh_from_db()
        self.assertEqual(bread.code, "RECIPE-002")  # 上位レシピのIDは変わらない
        self.assertTrue(RecipeReplacement.objects.filter(old_recipe=dough, new_recipe=v2).exists())
        snap = CostSnapshot.objects.get(product=product)
        self.assertEqual(snap.cost, Decimal("30.7"))
        self.assertFalse(dough.is_in_use())
        done = self.client.get(response.url)
        self.assertContains(done, "旧バージョンを使用停止にする")

    def test_replace_rejects_cycle(self):
        a = self.create("A")
        self.add(a, self.flour, 100)
        b = self.create("B")
        self.add(b, a, 50)
        # B を A に置き換えたいが、B は A を材料に使っている → 置き換えると B が B を使う
        c = self.create("C", kind="final", recipe_type="")
        self.add(c, b, 10)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("recipe_replace"), {"old": a.pk, "new": b.pk})
        self.assertContains(response, "循環参照")
