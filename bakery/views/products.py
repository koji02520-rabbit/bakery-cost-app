"""商品の画面（S30〜S33）。

閲覧（原価・売価・原価率の確認）は全員。登録・修正・採用レシピの切替・使用停止は「商品管理」なので管理者のみ（spec.md 第4項）。
商品は切替履歴・原価記録を持つため削除せず、使用停止で対応する。
"""

from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from bakery.forms import ProductForm
from bakery.models import IdSequence, Product, Recipe, Status, SystemSetting
from bakery.permissions import admin_required, member_required
from bakery.services import audit
from bakery.services import products as svc
from bakery.services.nutrition import label_rows
from bakery.services.recipes import RecipeRuleError
from bakery.views.photos import photo_context

PAGE_SIZE = 50

SORTS = {
    "code": ("商品ID順", lambda pc: pc.product.code),
    "rate": ("原価率が高い順", lambda pc: -(pc.rate if pc.rate is not None else Decimal("-1"))),
    "cost": ("原価が高い順", lambda pc: -(pc.cost if pc.cost is not None else Decimal("-1"))),
}


# --------------------------------------------------------------------------
# S30 商品原価一覧
# --------------------------------------------------------------------------


@member_required
def product_list(request):
    company = request.company
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", Status.ACTIVE)
    sort = request.GET.get("sort", "code")
    if sort not in SORTS:
        sort = "code"

    rows = Product.objects.filter(company=company).select_related("adopted_recipe")
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if status in (Status.ACTIVE, Status.STOPPED):
        rows = rows.filter(status=status)

    # 原価率で並べ替えるため、絞り込んだ全件を計算してから並べる（数百件規模を想定：第49項）
    costs = sorted(svc.evaluate(company, rows), key=SORTS[sort][1])
    page = Paginator(costs, PAGE_SIZE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "page": page, "q": q, "status": status, "sort": sort,
        "sorts": [(k, v[0]) for k, v in SORTS.items()],
        "tax_rate": SystemSetting.for_company(company).tax_rate,
        "query_without_page": params.urlencode(),
    }
    if request.headers.get("HX-Request") == "true":
        return render(request, "bakery/products/_list_results.html", context)
    return render(request, "bakery/products/list.html", context)


# --------------------------------------------------------------------------
# S31 商品詳細
# --------------------------------------------------------------------------


@member_required
def product_detail(request, pk):
    product = get_object_or_404(Product.objects.select_related("adopted_recipe"), pk=pk, company=request.company)
    pc, recipe_view = svc.detail_view(product)
    label = None
    if recipe_view and recipe_view.nutrition and recipe_view.nutrition.complete:
        label = label_rows(recipe_view.nutrition.total, recipe_view.nutrition.total_weight_g)
    newer = None
    if product.adopted_recipe_id:
        r = product.adopted_recipe
        newer = (svc.adoptable_recipes(request.company)
                 .filter(name=r.name, version__gt=r.version).order_by("-version").first())
    return render(request, "bakery/products/detail.html", {
        "product": product,
        "pc": pc,
        "v": recipe_view,
        "newer": newer,
        **photo_context("product", product),
        "label": label,
        "tax_rate": SystemSetting.for_company(request.company).tax_rate,
        "adoptions": product.adoptions.select_related("old_recipe", "new_recipe", "changed_by")[:20],
        "snapshots": product.cost_snapshots.select_related("recipe")[:20],
    })


# --------------------------------------------------------------------------
# S32 商品登録・修正
# --------------------------------------------------------------------------


@admin_required
def product_new(request):
    company = request.company
    if request.method == "POST":
        form = ProductForm(request.POST, company=company)
        if form.is_valid():
            data = form.cleaned_data
            try:
                product = svc.create_product(request, name=data["name"],
                                             price_excluding_tax=data["price_excluding_tax"],
                                             recipe=data["adopted_recipe"])
            except RecipeRuleError as exc:
                form.add_error("adopted_recipe", str(exc))
            else:
                messages.success(request, f"商品「{product.name}」を {product.code} として登録しました")
                return redirect("product_detail", pk=product.pk)
    else:
        initial = {"adopted_recipe": request.GET.get("recipe")} if request.GET.get("recipe") else {}
        form = ProductForm(company=company, initial=initial)
    return render(request, "bakery/products/form.html", {
        "form": form, "preview_code": IdSequence.peek(company, "ITEM"),
    })


@admin_required
def product_edit(request, pk):
    """商品名・税抜売価の修正。採用レシピの変更は切替確認（S33）で行う。"""
    product = get_object_or_404(Product, pk=pk, company=request.company)
    fields = ["name", "price_excluding_tax"]
    if request.method == "POST":
        form = ProductForm(request.POST, company=request.company, with_recipe=False)
        if form.is_valid():
            with transaction.atomic():
                before = audit.snapshot(product, fields)
                product.name = form.cleaned_data["name"]
                product.price_excluding_tax = form.cleaned_data["price_excluding_tax"]
                product.updated_by = request.user
                product.save()
                after = audit.snapshot(product, fields)
                if after != before:
                    audit.record(request, "商品修正", product, before=before, after=after)
            messages.success(request, "商品の情報を保存しました")
            return redirect("product_detail", pk=product.pk)
    else:
        form = ProductForm(company=request.company, with_recipe=False,
                           initial={"name": product.name, "price_excluding_tax": product.price_excluding_tax})
    return render(request, "bakery/products/form.html", {"form": form, "product": product})


# --------------------------------------------------------------------------
# S33 採用レシピの切替確認
# --------------------------------------------------------------------------


@admin_required
def product_switch(request, pk):
    product = get_object_or_404(Product.objects.select_related("adopted_recipe"), pk=pk, company=request.company)
    candidates = svc.adoptable_recipes(request.company).exclude(pk=product.adopted_recipe_id)
    params = request.POST if request.method == "POST" else request.GET
    new_recipe = impact = None
    error = ""
    if params.get("recipe"):
        new_recipe = get_object_or_404(Recipe, pk=params["recipe"], company=request.company)
        try:
            if request.method == "POST":
                svc.switch_adoption(request, product, new_recipe)
                messages.success(request, f"採用レシピを {new_recipe.code} {new_recipe.name}（v{new_recipe.version}）に切り替えました")
                return redirect("product_detail", pk=product.pk)
            impact = svc.adoption_impact(product, new_recipe)
        except RecipeRuleError as exc:
            error = str(exc)
    return render(request, "bakery/products/switch.html", {
        "product": product, "candidates": candidates, "new_recipe": new_recipe, "impact": impact, "error": error,
    })


@admin_required
@require_POST
def product_set_status(request, pk):
    product = get_object_or_404(Product, pk=pk, company=request.company)
    new_status = request.POST.get("status")
    if new_status in (Status.ACTIVE, Status.STOPPED) and new_status != product.status:
        before = audit.snapshot(product, ["status"])
        product.status = new_status
        product.updated_by = request.user
        product.save(update_fields=["status", "updated_by", "updated_at"])
        action = "商品使用停止" if new_status == Status.STOPPED else "商品使用再開"
        audit.record(request, action, product, before=before, after=audit.snapshot(product, ["status"]))
        messages.success(request, "使用停止にしました" if new_status == Status.STOPPED else "使用を再開しました")
    return redirect("product_detail", pk=product.pk)
