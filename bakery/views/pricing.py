"""食材価格の変更（S40〜S42）。一般ユーザーも行える（spec.md 第4項）。

S40 食材を選ぶ → S41 新しい価格を入力 → S42 影響確認 → 確定。
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from bakery.forms import PriceChangeForm
from bakery.models import Ingredient, IngredientCategory, Status, SystemSetting
from bakery.permissions import member_required
from bakery.services import pricing
from bakery.services.costing import UnitPriceRounding, unit_price
from bakery.services.recipes import RecipeRuleError
from bakery.templatetags.bakery_format import grams_input

PAGE_SIZE = 50


# --------------------------------------------------------------------------
# S40 食材を選ぶ
# --------------------------------------------------------------------------


@member_required
def price_change_select(request):
    company = request.company
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    rows = Ingredient.objects.filter(company=company).select_related("category", "supplier")
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(legacy_code__icontains=q))
    if category:
        rows = rows.filter(category_id=category)
    if request.GET.get("stopped") != "1":
        rows = rows.filter(status=Status.ACTIVE)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "page": page, "q": q, "category": category, "show_stopped": request.GET.get("stopped") == "1",
        "categories": IngredientCategory.objects.filter(company=company),
        "query_without_page": params.urlencode(),
    }
    if request.headers.get("HX-Request") == "true":
        return render(request, "bakery/pricing/_select_results.html", context)
    return render(request, "bakery/pricing/select.html", context)


# --------------------------------------------------------------------------
# S41 新しい価格を入力 → S42 影響確認 → 確定
# --------------------------------------------------------------------------


def _decimal_or_none(value):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _rounding(company):
    return UnitPriceRounding(SystemSetting.for_company(company).unit_price_rounding)


def _render_input(request, ingredient, form, status=200):
    return render(request, "bakery/pricing/input.html", {
        "ingredient": ingredient, "form": form, "compare": _compare(ingredient, form),
    }, status=status)


def _compare(ingredient, form):
    """変更前後の1g単価（入力途中でも、読み取れた値だけで計算する）。"""
    rounding = _rounding(ingredient.company)
    after = None
    if form.is_bound:
        form.is_valid()
        weight = form.cleaned_data.get("purchase_weight_g")
        price = form.cleaned_data.get("purchase_price")
        if weight is not None and price is not None:
            after = unit_price(price, weight, rounding)
    return {"before": ingredient.unit_price(rounding), "after": after}


@member_required
def price_change_edit(request, pk):
    ingredient = get_object_or_404(Ingredient.objects.select_related("category", "supplier"),
                                   pk=pk, company=request.company)
    if request.method != "POST":
        form = PriceChangeForm(initial={
            "purchase_weight_g": grams_input(ingredient.purchase_weight_g),
            "purchase_price": grams_input(ingredient.purchase_price),
        })
        return _render_input(request, ingredient, form)

    form = PriceChangeForm(request.POST)
    step = request.POST.get("step")
    if step == "back" or not form.is_valid():
        return _render_input(request, ingredient, form, status=200 if step == "back" else 422)

    new_weight = form.cleaned_data["purchase_weight_g"]
    new_price = form.cleaned_data["purchase_price"]
    error = ""
    if step == "commit":
        try:
            impact = pricing.change_price(
                request, ingredient, new_weight=new_weight, new_price=new_price,
                seen_weight=_decimal_or_none(request.POST.get("seen_weight")),
                seen_price=_decimal_or_none(request.POST.get("seen_price")),
            )
        except pricing.StalePriceError as exc:
            error = str(exc)
            ingredient.refresh_from_db()
        except RecipeRuleError as exc:
            form.add_error(None, str(exc))
            return _render_input(request, ingredient, form, status=422)
        else:
            messages.success(request, f"{ingredient.name} の価格を変更しました（影響した商品 {len(impact.products)} 件）")
            return redirect("ingredient_detail", pk=ingredient.pk)

    try:
        impact = pricing.price_impact(ingredient, new_weight, new_price)
    except RecipeRuleError as exc:
        form.add_error(None, str(exc))
        return _render_input(request, ingredient, form, status=422)
    return render(request, "bakery/pricing/confirm.html", {
        "ingredient": ingredient, "form": form, "impact": impact, "error": error,
    })


@member_required
@require_POST
def price_change_check(request, pk):
    """入力途中の1g単価の比較とエラー（htmx）。"""
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    form = PriceChangeForm(request.POST)
    return render(request, "bakery/pricing/_compare.html", {
        "ingredient": ingredient, "form": form, "compare": _compare(ingredient, form), "oob": True,
    })
