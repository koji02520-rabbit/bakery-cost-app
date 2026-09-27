"""試用（デモ）データを作る。GitHub Codespaces などで、すぐに触れる状態にするためのもの。

使い方:
    python manage.py setup_demo

作るもの（仕入先・価格はすべて架空。栄養値は日本食品標準成分表から。取り込み前なら栄養は未入力）:
- 会社「デモベーカリー」、店舗「本店」、食材カテゴリー・アレルゲン項目・仕入先「自社」
- ログイン用ユーザー：demo_admin（管理者）／demo_staff（一般）。パスワードは DEMO_PASSWORD
- 食材10件、中間レシピ3件、最終商品レシピ3件、商品3件

本番環境では使わないこと（パスワードが公開されている）。すでにデモ会社がある場合は何もしない。
"""

from decimal import Decimal
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from bakery.models import (
    AllergenItem,
    AllergenStatus,
    Company,
    FoodComposition,
    IdSequence,
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    Membership,
    RecipeKind,
    Role,
    Store,
    Supplier,
)
from bakery.services import products as product_svc
from bakery.services import recipes as recipe_svc

COMPANY = "デモベーカリー"
DEMO_PASSWORD = "demo-bakery-2026"

# (名前, カテゴリー, 仕入先, 購入重量g, 購入価格円, アレルゲン, 成分表の食品番号 または 栄養値)
INGREDIENTS = [
    ("水", "Q", "自社", 1000, 0, [], (0, 0, 0, 0, 0)),
    ("強力粉", "A", "サンプル製粉", 25000, 5200, ["小麦"], "01020"),
    ("薄力粉", "A", "サンプル製粉", 25000, 4800, ["小麦"], "01015"),
    ("食塩", "E", "サンプル商店", 1000, 150, [], "17012"),
    ("ドライイースト", "I", "サンプル商店", 500, 900, [], "17083"),
    ("上白糖", "C", "サンプル商店", 1000, 260, [], "03003"),
    ("有塩バター", "L", "サンプル乳業", 450, 950, ["乳"], "14017"),
    ("牛乳", "L", "サンプル乳業", 1000, 230, ["乳"], "13003"),
    ("鶏卵", "H", "サンプル商店", 1000, 380, ["卵"], "12004"),
    ("卵黄", "H", "サンプル商店", 1000, 900, ["卵"], "12010"),
]

# (名前, 区分, 種類, [(材料名, g, 工程)])。材料名が中間レシピの名前なら中間レシピを使う
RECIPES = [
    ("バゲット生地", RecipeKind.INTERMEDIATE, "dough",
     [("強力粉", 1000, "1"), ("水", 680, "1"), ("食塩", 20, "1"), ("ドライイースト", 4, "1")]),
    ("バターロール生地", RecipeKind.INTERMEDIATE, "dough",
     [("強力粉", 1000, "1"), ("上白糖", 120, "1"), ("食塩", 18, "1"), ("ドライイースト", 20, "1"),
      ("鶏卵", 100, "1"), ("牛乳", 450, "1"), ("有塩バター", 150, "2")]),
    ("カスタードクリーム", RecipeKind.INTERMEDIATE, "cream",
     [("牛乳", 500, "1"), ("卵黄", 100, "1"), ("上白糖", 120, "1"), ("薄力粉", 40, "1"), ("有塩バター", 20, "2")]),
    ("バゲット", RecipeKind.FINAL, "", [("バゲット生地", 280, "分割")]),
    ("バターロール", RecipeKind.FINAL, "", [("バターロール生地", 45, "分割")]),
    ("クリームパン", RecipeKind.FINAL, "", [("バターロール生地", 50, "分割"), ("カスタードクリーム", 45, "包餡")]),
]

# (商品名, 税抜売価, 採用レシピ名)
PRODUCTS = [("バゲット", 320, "バゲット"), ("バターロール", 120, "バターロール"), ("クリームパン", 220, "クリームパン")]


class Command(BaseCommand):
    help = "試用（デモ）データを作る。本番では使わないこと"

    @transaction.atomic
    def handle(self, *args, **options):
        if Company.objects.filter(name=COMPANY).exists():
            self.stdout.write(f"「{COMPANY}」はすでにあります。何もしません")
            return

        User = get_user_model()
        admin, _ = User.objects.get_or_create(username="demo_admin")
        staff, _ = User.objects.get_or_create(username="demo_staff")
        for user in (admin, staff):
            user.set_password(DEMO_PASSWORD)
            user.save()
        call_command("setup_initial_data", company=COMPANY, store="本店", admin="demo_admin", stdout=self.stdout)
        company = Company.objects.get(name=COMPANY)
        Membership.objects.update_or_create(user=staff, defaults={
            "company": company, "store": Store.objects.get(company=company), "role": Role.GENERAL})
        request = SimpleNamespace(user=admin, company=company)

        ingredients = self._ingredients(company, admin)
        recipes = self._recipes(request, ingredients)
        for name, price, recipe_name in PRODUCTS:
            product_svc.create_product(request, name=name, price_excluding_tax=price, recipe=recipes[recipe_name])

        self.stdout.write(self.style.SUCCESS(
            f"試用データを作りました。ログイン：demo_admin（管理者）／demo_staff（一般）、パスワード {DEMO_PASSWORD}"))

    def _ingredients(self, company, user):
        allergens = {a.name: a for a in AllergenItem.objects.filter(company=company)}
        categories = {c.code: c for c in IngredientCategory.objects.filter(company=company)}
        suppliers = {s.name: s for s in Supplier.objects.filter(company=company)}
        result = {}
        for name, category, supplier_name, weight, price, allergen_names, nutrition in INGREDIENTS:
            if supplier_name not in suppliers:
                suppliers[supplier_name] = Supplier.objects.create(
                    company=company, code=IdSequence.issue(company, "SUP"), name=supplier_name)
            ingredient = Ingredient(
                company=company, code=IdSequence.issue(company, categories[category].id_prefix), name=name,
                category=categories[category], supplier=suppliers[supplier_name],
                purchase_weight_g=weight, purchase_price=price,
                allergen_status=AllergenStatus.CONFIRMED, allergen_confirmed_by=user,
                allergen_confirmed_at=timezone.now(), created_by=user, updated_by=user,
            )
            if isinstance(nutrition, str):
                food = FoodComposition.objects.filter(food_number=nutrition).first()
                if food:  # 成分表を取り込んでいなければ、栄養は未入力のまま
                    for field in Ingredient.NUTRITION_FIELDS:
                        setattr(ingredient, field, getattr(food, field))
                    ingredient.nutrition_food = food
                    ingredient.nutrition_note = "日本食品標準成分表（八訂）"
            else:
                for field, value in zip(Ingredient.NUTRITION_FIELDS, nutrition):
                    setattr(ingredient, field, Decimal(value))
                ingredient.nutrition_note = "水のため0"
            ingredient.save()
            ingredient.allergens.set([allergens[a] for a in allergen_names])
            IngredientPriceHistory.objects.create(ingredient=ingredient, purchase_weight_g=weight,
                                                  purchase_price=price, changed_by=user)
            result[name] = ingredient
        return result

    def _recipes(self, request, ingredients):
        recipes = {}
        for name, kind, recipe_type, lines in RECIPES:
            recipe = recipe_svc.create_recipe(request, name=name, kind=kind, recipe_type=recipe_type)
            for material, grams, step in lines:
                if material in recipes:
                    recipe_svc.add_item(request, recipe, quantity_g=Decimal(grams), step_label=step,
                                        material_recipe=recipes[material])
                else:
                    recipe_svc.add_item(request, recipe, quantity_g=Decimal(grams), step_label=step,
                                        ingredient=ingredients[material])
            recipes[name] = recipe
        return recipes
