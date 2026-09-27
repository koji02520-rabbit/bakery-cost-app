"""試用データ（setup_demo）のテスト。"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from bakery.management.commands.setup_demo import DEMO_PASSWORD
from bakery.models import Company, Ingredient, Product, Recipe


class SetupDemoTests(TestCase):
    def test_creates_usable_demo_and_is_idempotent(self):
        out = StringIO()
        call_command("setup_demo", stdout=out)
        company = Company.objects.get(name="デモベーカリー")
        self.assertEqual(Ingredient.objects.filter(company=company).count(), 10)
        self.assertEqual(Recipe.objects.filter(company=company).count(), 6)
        self.assertEqual(Product.objects.filter(company=company).count(), 3)

        # 管理者も一般ユーザーもログインでき、商品原価が計算できている
        self.assertTrue(self.client.login(username="demo_admin", password=DEMO_PASSWORD))
        page = self.client.get(reverse("product_list"))
        self.assertContains(page, "クリームパン")
        self.assertNotContains(page, "計算できません")
        self.client.logout()
        self.assertTrue(self.client.login(username="demo_staff", password=DEMO_PASSWORD))
        self.assertEqual(self.client.get(reverse("ingredient_new")).status_code, 403)  # 一般ユーザー

        call_command("setup_demo", stdout=out)  # 2回目は何もしない
        self.assertEqual(Product.objects.filter(company=company).count(), 3)
        self.assertIn("すでにあります", out.getvalue())
