"""食材画面（S10〜S14）とログイン・権限のテスト。"""

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
    Ingredient,
    IngredientCategory,
    Membership,
    Recipe,
    RecipeItem,
    RecipeKind,
    Role,
    Status,
    Supplier,
)


class IngredientViewTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.admin = User.objects.create_user("admin", password="pw-admin-123")
        cls.staff = User.objects.create_user("staff", password="pw-staff-123")
        call_command("setup_initial_data", company="テスト会社", store="本店", admin="admin", stdout=_Null())
        cls.company = Company.objects.get(name="テスト会社")
        Membership.objects.create(user=cls.staff, company=cls.company, role=Role.GENERAL)
        cls.flour = IngredientCategory.objects.get(company=cls.company, code="A")
        cls.dairy = IngredientCategory.objects.get(company=cls.company, code="L")
        cls.own = Supplier.objects.get(company=cls.company, name="自社")

    def login_admin(self):
        self.client.force_login(self.admin)

    def login_staff(self):
        self.client.force_login(self.staff)

    def step1_data(self, **overrides):
        data = {
            "name": "強力粉",
            "category": self.flour.pk,
            "supplier": self.own.pk,
            "purchase_weight_g": "25000",
            "purchase_price": "5000",
        }
        data.update(overrides)
        return data

    def register(self, **overrides):
        data = self.step1_data(**overrides)
        data["step"] = "register"
        return self.client.post(reverse("ingredient_new"), data)


class _Null:
    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass


class AccessTests(IngredientViewTestBase):
    def test_login_required(self):
        response = self.client.get(reverse("ingredient_list"))
        self.assertRedirects(response, reverse("login") + "?next=" + reverse("ingredient_list"))

    def test_user_without_membership_is_told_why(self):
        loner = get_user_model().objects.create_user("loner", password="x")
        self.client.force_login(loner)
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "所属する会社が登録されていません", status_code=403)

    def test_general_user_can_view_but_not_register(self):
        self.login_staff()
        self.assertEqual(self.client.get(reverse("ingredient_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("ingredient_new")).status_code, 403)
        self.assertEqual(self.register().status_code, 403)
        self.assertFalse(Ingredient.objects.exists())

    def test_other_company_data_is_not_visible(self):
        other = Company.objects.create(name="他社")
        cat = IngredientCategory.objects.create(company=other, code="A", name="粉")
        sup = Supplier.objects.create(company=other, code="SUP-001", name="他社仕入先")
        foreign = Ingredient.objects.create(company=other, code="FOOD-A001", name="他社の粉", category=cat,
                                            supplier=sup, purchase_weight_g=1000, purchase_price=100)
        self.login_admin()
        self.assertEqual(self.client.get(reverse("ingredient_detail", args=[foreign.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("ingredient_list") + "?status=all"), "他社の粉")


class RegisterTests(IngredientViewTestBase):
    def test_every_page_opens(self):
        self.login_admin()
        self.register()
        pk = Ingredient.objects.get().pk
        for name, args in [("home", []), ("ingredient_list", []), ("ingredient_new", []),
                           ("ingredient_detail", [pk]), ("ingredient_registered", [pk]),
                           ("ingredient_allergens", [pk]), ("ingredient_edit", [pk]), ("ingredient_delete", [pk])]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)

    def test_new_form_starts_without_errors(self):
        self.login_admin()
        response = self.client.get(reverse("ingredient_new"))
        self.assertNotContains(response, "入力してください</div>")
        self.assertContains(response, "カテゴリーを選ぶと表示されます")

    def test_confirm_page_shows_planned_id_and_unit_price(self):
        self.login_admin()
        response = self.client.post(reverse("ingredient_new"), {**self.step1_data(), "step": "confirm"})
        self.assertContains(response, "FOOD-A001")
        self.assertContains(response, "0.200 円/g")
        self.assertFalse(Ingredient.objects.exists())  # 確認画面ではまだ登録しない

    def test_register_issues_id_and_records_history(self):
        self.login_admin()
        response = self.register()
        ingredient = Ingredient.objects.get()
        self.assertRedirects(response, reverse("ingredient_registered", args=[ingredient.pk]))
        self.assertEqual(ingredient.code, "FOOD-A001")
        self.assertEqual(ingredient.unit_price(), Decimal("0.2"))
        self.assertEqual(ingredient.allergen_status, AllergenStatus.UNCONFIRMED)
        self.assertEqual(ingredient.created_by, self.admin)
        self.assertEqual(ingredient.price_history.count(), 1)
        self.assertTrue(AuditLog.objects.filter(action="食材登録", target_id=ingredient.pk).exists())

    def test_ids_are_numbered_per_category(self):
        self.login_admin()
        self.register(name="強力粉")
        self.register(name="薄力粉")
        self.register(name="バター", category=self.dairy.pk)
        self.assertEqual(
            list(Ingredient.objects.order_by("pk").values_list("code", flat=True)),
            ["FOOD-A001", "FOOD-A002", "FOOD-L001"],
        )

    def test_full_width_numbers_and_commas_are_accepted(self):
        self.login_admin()
        self.register(purchase_weight_g="２５，０００", purchase_price="５０００")
        self.assertEqual(Ingredient.objects.get().purchase_weight_g, Decimal("25000"))

    def test_specific_error_messages(self):
        self.login_admin()
        cases = [
            ({"name": ""}, "食材名を入力してください"),
            ({"category": ""}, "カテゴリーを選択してください"),
            ({"supplier": ""}, "仕入先を選択してください"),
            ({"purchase_weight_g": ""}, "購入重量を入力してください"),
            ({"purchase_weight_g": "0"}, "購入重量は1g以上で入力してください"),
            ({"purchase_weight_g": "abc"}, "購入重量は数字で入力してください"),
            ({"purchase_price": ""}, "購入価格を入力してください"),
            ({"purchase_price": "-1"}, "購入価格は0円以上で入力してください"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                response = self.register(**overrides)
                self.assertContains(response, message, status_code=422)
        self.assertFalse(Ingredient.objects.exists())

    def test_back_from_confirm_keeps_input(self):
        self.login_admin()
        response = self.client.post(reverse("ingredient_new"), {**self.step1_data(name="ライ麦粉"), "step": "back"})
        self.assertContains(response, 'value="ライ麦粉"')
        self.assertFalse(Ingredient.objects.exists())

    def test_stopped_supplier_cannot_be_chosen(self):
        self.own.status = Status.STOPPED
        self.own.save()
        self.login_admin()
        response = self.register()
        self.assertContains(response, "仕入先を選択し直してください", status_code=422)

    def test_duplicate_name_and_supplier_is_warned_on_confirm(self):
        self.login_admin()
        self.register()
        response = self.client.post(reverse("ingredient_new"), {**self.step1_data(), "step": "confirm"})
        self.assertContains(response, "同じ食材名・同じ仕入先の食材がすでにあります")


class LiveCheckTests(IngredientViewTestBase):
    def check(self, data, touched):
        return self.client.post(reverse("ingredient_new_check"), {**data, "_touched": ",".join(touched)})

    def test_errors_only_for_touched_fields(self):
        self.login_admin()
        response = self.check({"name": ""}, touched=["name"])
        self.assertContains(response, "食材名を入力してください")
        self.assertNotContains(response, "購入重量を入力してください")
        self.assertContains(response, "disabled")

    def test_preview_values_and_enabled_button(self):
        self.login_admin()
        response = self.check(self.step1_data(), touched=[])
        self.assertContains(response, "FOOD-A001")
        self.assertContains(response, "0.200 円/g")
        self.assertNotContains(response, "disabled")


class SupplierQuickAddTests(IngredientViewTestBase):
    def test_add_supplier_and_select_it(self):
        self.login_admin()
        response = self.client.post(reverse("supplier_quick_add"), {"supplier_name": "山田製粉"})
        supplier = Supplier.objects.get(name="山田製粉")
        self.assertEqual(supplier.code, "SUP-002")
        self.assertContains(response, f'<option value="{supplier.pk}" selected>')
        self.assertEqual(response["HX-Trigger"], "supplier-added")

    def test_duplicate_supplier_name(self):
        self.login_admin()
        response = self.client.post(reverse("supplier_quick_add"), {"supplier_name": "自社"})
        self.assertContains(response, "すでに登録されています")
        self.assertEqual(Supplier.objects.filter(name="自社").count(), 1)


class AfterRegisterTests(IngredientViewTestBase):
    def setUp(self):
        self.login_admin()
        self.register()
        self.ingredient = Ingredient.objects.get()

    def test_confirm_allergens(self):
        wheat = AllergenItem.objects.get(company=self.company, name="小麦")
        response = self.client.post(reverse("ingredient_allergens", args=[self.ingredient.pk]),
                                    {"allergens": [wheat.pk]})
        self.assertRedirects(response, reverse("ingredient_detail", args=[self.ingredient.pk]))
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.allergen_status, AllergenStatus.CONFIRMED)
        self.assertEqual(self.ingredient.allergen_confirmed_by, self.admin)
        self.assertEqual(list(self.ingredient.allergens.all()), [wheat])

    def test_confirm_with_no_allergens_means_none(self):
        self.client.post(reverse("ingredient_allergens", args=[self.ingredient.pk]), {})
        response = self.client.get(reverse("ingredient_detail", args=[self.ingredient.pk]))
        self.assertContains(response, "アレルゲンなし")

    def test_home_counts_unconfirmed(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "アレルゲン未確認の食材が 1 件あります")

    def test_edit_category_keeps_id(self):
        response = self.client.post(reverse("ingredient_edit", args=[self.ingredient.pk]), {
            "name": "強力粉（新）", "category": self.dairy.pk, "supplier": self.own.pk, "note": "",
        })
        self.assertRedirects(response, reverse("ingredient_detail", args=[self.ingredient.pk]))
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.code, "FOOD-A001")
        self.assertEqual(self.ingredient.category, self.dairy)
        log = AuditLog.objects.get(action="食材修正")
        self.assertEqual(log.before["name"], "強力粉")
        self.assertEqual(log.after["name"], "強力粉（新）")

    def test_stop_and_resume(self):
        url = reverse("ingredient_set_status", args=[self.ingredient.pk])
        self.client.post(url, {"status": "stopped"}, follow=True)
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.status, Status.STOPPED)
        self.assertNotContains(self.client.get(reverse("ingredient_list")), "強力粉")
        self.client.post(url, {"status": "active"})
        self.ingredient.refresh_from_db()
        self.assertEqual(self.ingredient.status, Status.ACTIVE)

    def test_delete_unused_and_id_is_not_reused(self):
        response = self.client.post(reverse("ingredient_delete", args=[self.ingredient.pk]))
        self.assertRedirects(response, reverse("ingredient_list"))
        self.assertFalse(Ingredient.objects.exists())
        self.register(name="別の粉")
        self.assertEqual(Ingredient.objects.get().code, "FOOD-A002")

    def test_cannot_delete_used_ingredient(self):
        recipe = Recipe.objects.create(company=self.company, code="RECIPE-001", name="生地",
                                       kind=RecipeKind.INTERMEDIATE, recipe_type="dough",
                                       yield_rate=Decimal("0.98"))
        RecipeItem.objects.create(recipe=recipe, ingredient=self.ingredient, quantity_g=1000)
        response = self.client.post(reverse("ingredient_delete", args=[self.ingredient.pk]))
        self.assertContains(response, "削除できません")
        self.assertTrue(Ingredient.objects.filter(pk=self.ingredient.pk).exists())
        detail = self.client.get(reverse("ingredient_detail", args=[self.ingredient.pk]))
        self.assertContains(detail, "RECIPE-001")

    def test_general_user_cannot_change(self):
        self.login_staff()
        pk = self.ingredient.pk
        for url in ["ingredient_edit", "ingredient_allergens", "ingredient_delete", "ingredient_set_status"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.post(reverse(url, args=[pk]), {"status": "stopped"}).status_code, 403)
        detail = self.client.get(reverse("ingredient_detail", args=[pk]))
        self.assertNotContains(detail, "修正する")


class ListTests(IngredientViewTestBase):
    def test_search_and_filters(self):
        self.login_admin()
        self.register(name="強力粉")
        self.register(name="バター", category=self.dairy.pk)
        list_url = reverse("ingredient_list")
        self.assertContains(self.client.get(list_url, {"q": "バタ"}), "FOOD-L001")
        self.assertNotContains(self.client.get(list_url, {"q": "バタ"}), "FOOD-A001")
        self.assertNotContains(self.client.get(list_url, {"category": self.dairy.pk}), "FOOD-A001")
        self.assertContains(self.client.get(list_url, {"q": "a001"}), "強力粉")
        partial = self.client.get(list_url, {"q": "粉"}, headers={"HX-Request": "true"})
        self.assertNotContains(partial, "<html")
        self.assertContains(partial, "強力粉")
