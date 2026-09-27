"""共通の画面：ホーム（S02）。"""

from django.db.models import Q
from django.shortcuts import render

from bakery.models import AllergenStatus, Ingredient, Product, Recipe, Status
from bakery.permissions import member_required


@member_required
def home(request):
    company = request.company
    unconfirmed_count = Ingredient.objects.filter(
        company=company, status=Status.ACTIVE, allergen_status=AllergenStatus.UNCONFIRMED
    ).count()

    # 横断検索（食材・レシピ・商品）
    q = request.GET.get("q", "").strip()
    ingredients, recipes, products = [], [], []
    if q:
        products = list(
            Product.objects.filter(company=company)
            .filter(Q(name__icontains=q) | Q(code__icontains=q))
            .order_by("status", "code")[:20]
        )
        recipes = list(
            Recipe.objects.filter(company=company)
            .filter(Q(name__icontains=q) | Q(code__icontains=q))
            .order_by("status", "name", "-version")[:20]
        )
        ingredients = list(
            Ingredient.objects.filter(company=company)
            .filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(legacy_code__icontains=q))
            .select_related("category", "supplier")[:20]
        )
    return render(request, "bakery/home.html", {
        "unconfirmed_count": unconfirmed_count,
        "q": q,
        "ingredients": ingredients,
        "recipes": recipes,
        "products": products,
    })

