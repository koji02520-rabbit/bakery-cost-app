"""レシピの画面（S20〜S25）。

レシピの作成・編集・コピーは一般ユーザーもできる。使用停止・削除・中間レシピの置き換えは管理者のみ（spec.md 第4項）。
材料明細の操作は、押すたびに保存して編集部分（#editor-body）だけを描き直す。
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import ProtectedError, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import escape
from django.views.decorators.http import require_POST

from bakery.forms import RecipeCreateForm, RecipeInfoForm, RecipeItemForm
from bakery.models import Ingredient, Product, Recipe, RecipeItem, RecipeKind, RecipeType, Status
from bakery.permissions import admin_required, member_required
from bakery.services import audit
from bakery.services import recipes as svc
from bakery.services.costing import MaterialKind
from bakery.views.photos import photo_context

PAGE_SIZE = 50


def _is_htmx(request):
    return request.headers.get("HX-Request") == "true"


def _get_recipe(request, pk):
    return get_object_or_404(Recipe, pk=pk, company=request.company)


def _get_item(request, pk):
    return get_object_or_404(RecipeItem.objects.select_related("recipe"), pk=pk, recipe__company=request.company)


# --------------------------------------------------------------------------
# S20 レシピ一覧・検索
# --------------------------------------------------------------------------


@member_required
def recipe_list(request):
    company = request.company
    q = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    recipe_type = request.GET.get("type", "")
    state = request.GET.get("state", "")

    rows = svc.with_usage(Recipe.objects.filter(company=company))
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q))
    if kind in RecipeKind.values:
        rows = rows.filter(kind=kind)
    if recipe_type in RecipeType.values:
        rows = rows.filter(recipe_type=recipe_type)
    if state == "in_use":
        rows = rows.filter(status=Status.ACTIVE, in_use=True)
    elif state == "draft":
        rows = rows.filter(status=Status.ACTIVE, in_use=False)
    elif state == "stopped":
        rows = rows.filter(status=Status.STOPPED)
    elif state != "all":
        rows = rows.filter(status=Status.ACTIVE)

    rows = rows.prefetch_related("photos").order_by("kind", "name", "-version")
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    costs = svc.recipe_costs(company, page.object_list)
    for r in page.object_list:
        r.costs = costs.get(r.pk)

    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "page": page, "q": q, "kind": kind, "type": recipe_type, "state": state,
        "kinds": RecipeKind.choices, "types": RecipeType.choices, "query_without_page": params.urlencode(),
    }
    if _is_htmx(request):
        return render(request, "bakery/recipes/_list_results.html", context)
    return render(request, "bakery/recipes/list.html", context)


# --------------------------------------------------------------------------
# S21 レシピを作る
# --------------------------------------------------------------------------


@member_required
def recipe_new(request):
    company = request.company
    if request.method == "POST":
        form = RecipeCreateForm(request.POST, company=company)
        if form.is_valid():
            data = form.cleaned_data
            if data["mode"] == "copy":
                recipe = svc.copy_recipe(request, data["source"], name=data["name"])
                messages.success(request, f"{data['source'].code} をコピーして {recipe.code} を作りました")
            else:
                recipe = svc.create_recipe(request, name=data["name"], kind=data["kind"],
                                           recipe_type=data["recipe_type"])
                messages.success(request, f"{recipe.code} を作りました。材料を追加してください")
            return redirect("recipe_edit", pk=recipe.pk)
    else:
        initial = {}
        if request.GET.get("copy"):
            initial = {"mode": "copy", "source": request.GET["copy"]}
        form = RecipeCreateForm(company=company, initial=initial)
    return render(request, "bakery/recipes/new.html", {"form": form})


@member_required
@require_POST
def recipe_new_version(request, pk):
    """使用中のレシピを変えたいとき：同じ名前でコピーし、バージョンを1つ上げる（第30項 v14）。"""
    source = _get_recipe(request, pk)
    recipe = svc.copy_recipe(request, source, name=source.name)
    messages.success(request, f"{source.code}（v{source.version}）をコピーして、新しいバージョン "
                              f"{recipe.code}（v{recipe.version}）を作りました。商品に反映するには切り替えが必要です")
    return redirect("recipe_edit", pk=recipe.pk)


# --------------------------------------------------------------------------
# S22 レシピ編集（材料カード）
# --------------------------------------------------------------------------


def _editor_context(request, recipe, error=""):
    view = svc.build_view(recipe)
    return {
        "recipe": recipe,
        "v": view,
        "in_use": recipe.is_in_use(),
        "show_total": request.GET.get("view") == "total" or request.POST.get("view") == "total",
        "error": error,
    }


def _render_body(request, recipe, error=""):
    return render(request, "bakery/recipes/_editor_body.html", _editor_context(request, recipe, error))


def _picker_error(message):
    """材料検索パネル内にエラーを出す（パネルは閉じない）。"""
    response = HttpResponse(f'<p class="field-error" role="alert">{escape(message)}</p>')
    response["HX-Retarget"] = "#picker-error"
    response["HX-Reswap"] = "innerHTML"
    return response


@member_required
def recipe_edit(request, pk):
    recipe = _get_recipe(request, pk)
    context = _editor_context(request, recipe)
    if _is_htmx(request):
        return render(request, "bakery/recipes/_editor_body.html", context)
    return render(request, "bakery/recipes/edit.html", context)


@member_required
def recipe_info_edit(request, pk):
    """レシピ名・種類・下処理・手順・メモの修正。"""
    recipe = _get_recipe(request, pk)
    lock_type = recipe.is_in_use()
    fields = ["name", "recipe_type", "preparation", "procedure", "memo"]
    before = audit.snapshot(recipe, fields)
    if request.method == "POST":
        form = RecipeInfoForm(request.POST, instance=recipe, lock_type=lock_type)
        if form.is_valid():
            with transaction.atomic():
                recipe = form.save(commit=False)
                recipe.updated_by = request.user
                recipe.save()
                after = audit.snapshot(recipe, fields)
                if after != before:
                    audit.record(request, "レシピ情報修正", recipe, before=before, after=after)
            messages.success(request, "レシピの情報を保存しました")
            return redirect("recipe_edit" if request.POST.get("next") == "edit" else "recipe_detail", pk=recipe.pk)
    else:
        form = RecipeInfoForm(instance=recipe, lock_type=lock_type)
    return render(request, "bakery/recipes/info_edit.html", {
        "recipe": recipe, "form": form, "lock_type": lock_type, "next": request.GET.get("next", ""),
    })


# ---- S23 材料検索 --------------------------------------------------------


@member_required
def recipe_picker(request, pk):
    """材料検索パネル。row を付けると「材料を変える」モードになる。"""
    recipe = _get_recipe(request, pk)
    row = request.GET.get("row", "")
    item = _get_item(request, row) if row else None
    if item and item.recipe_id != recipe.pk:
        item = None
    q = request.GET.get("q", "").strip()
    scope = request.GET.get("scope", "all")
    context = {
        "recipe": recipe, "item": item, "q": q, "scope": scope,
        "candidates": svc.search_materials(recipe, q, scope),
        "last_step": recipe.items.order_by("-sort_order", "-id").values_list("step_label", flat=True).first() or "",
    }
    template = "bakery/recipes/_picker_results.html" if request.GET.get("results") else "bakery/recipes/_picker.html"
    return render(request, template, context)


def _material_from_post(request):
    kind = request.POST.get("kind")
    ref = request.POST.get("ref")
    if kind == MaterialKind.INGREDIENT.value:
        return {"ingredient": get_object_or_404(Ingredient, pk=ref, company=request.company)}
    if kind == MaterialKind.RECIPE.value:
        return {"material_recipe": get_object_or_404(Recipe, pk=ref, company=request.company)}
    raise svc.RecipeRuleError("材料を選び直してください")


@member_required
@require_POST
def recipe_item_add(request, pk):
    recipe = _get_recipe(request, pk)
    form = RecipeItemForm(request.POST)
    if not form.is_valid():
        return _picker_error(form.first_error())
    try:
        svc.add_item(request, recipe, quantity_g=form.cleaned_data["quantity_g"],
                     step_label=form.cleaned_data["step_label"], **_material_from_post(request))
    except svc.RecipeRuleError as exc:
        return _picker_error(str(exc))
    response = _render_body(request, recipe)
    response["HX-Trigger"] = "picker-close"
    return response


@member_required
@require_POST
def recipe_item_material(request, pk):
    """材料を変える（使用量・工程・表示順はそのまま）。"""
    item = _get_item(request, pk)
    try:
        svc.change_material(request, item, **_material_from_post(request))
    except svc.RecipeRuleError as exc:
        return _picker_error(str(exc))
    response = _render_body(request, item.recipe)
    response["HX-Trigger"] = "picker-close"
    return response


@member_required
@require_POST
def recipe_item_update(request, pk):
    item = _get_item(request, pk)
    form = RecipeItemForm(request.POST)
    if not form.is_valid():
        return _render_body(request, item.recipe, error=form.first_error())
    try:
        svc.update_item(request, item, quantity_g=form.cleaned_data["quantity_g"],
                        step_label=form.cleaned_data["step_label"])
    except svc.RecipeRuleError as exc:
        return _render_body(request, item.recipe, error=str(exc))
    return _render_body(request, item.recipe)


@member_required
@require_POST
def recipe_item_delete(request, pk):
    item = _get_item(request, pk)
    recipe = item.recipe
    try:
        svc.delete_item(request, item)
    except svc.RecipeRuleError as exc:
        return _render_body(request, recipe, error=str(exc))
    return _render_body(request, recipe)


@member_required
@require_POST
def recipe_reorder(request, pk):
    recipe = _get_recipe(request, pk)
    try:
        ids = [int(x) for x in request.POST.get("order", "").split(",") if x]
        svc.reorder_items(request, recipe, ids)
    except ValueError:
        return _render_body(request, recipe, error="並べ替えに失敗しました。再読み込みしてください")
    except svc.RecipeRuleError as exc:
        return _render_body(request, recipe, error=str(exc))
    return _render_body(request, recipe)


# --------------------------------------------------------------------------
# S24 レシピ詳細
# --------------------------------------------------------------------------


@member_required
def recipe_detail(request, pk):
    recipe = get_object_or_404(Recipe.objects.select_related("copied_from", "created_by", "updated_by"),
                               pk=pk, company=request.company)
    used_in = Recipe.objects.filter(items__material_recipe=recipe).distinct().order_by("code")
    products = Product.objects.filter(adopted_recipe=recipe).order_by("code")
    newer = Recipe.objects.filter(company=request.company, name=recipe.name, kind=recipe.kind,
                                  version__gt=recipe.version).order_by("-version").first()
    return render(request, "bakery/recipes/detail.html", {
        "recipe": recipe,
        "v": svc.build_view(recipe),
        "in_use": bool(used_in) or products.exists(),
        "used_in": used_in,
        "products": products,
        "newer": newer,
        **photo_context("recipe", recipe),
        "show_total": request.GET.get("view") == "total",
    })


# --------------------------------------------------------------------------
# 使用停止・削除（管理者）
# --------------------------------------------------------------------------


@admin_required
@require_POST
def recipe_set_status(request, pk):
    recipe = _get_recipe(request, pk)
    new_status = request.POST.get("status")
    if new_status in (Status.ACTIVE, Status.STOPPED) and new_status != recipe.status:
        before = audit.snapshot(recipe, ["status"])
        recipe.status = new_status
        recipe.updated_by = request.user
        recipe.save(update_fields=["status", "updated_by", "updated_at"])
        action = "レシピ使用停止" if new_status == Status.STOPPED else "レシピ使用再開"
        audit.record(request, action, recipe, before=before, after=audit.snapshot(recipe, ["status"]))
        messages.success(request, "使用停止にしました" if new_status == Status.STOPPED else "使用を再開しました")
    return redirect("recipe_detail", pk=recipe.pk)


@admin_required
def recipe_delete(request, pk):
    recipe = _get_recipe(request, pk)
    in_use = recipe.is_in_use()
    blocked = ""
    if request.method == "POST" and not in_use:
        label = str(recipe)
        try:
            with transaction.atomic():
                audit.record(request, "レシピ削除", recipe,
                             before={**audit.snapshot(recipe), "明細": svc.items_snapshot(recipe)})
                recipe.delete()
        except ProtectedError:
            blocked = "このレシピは切替・置き換え・原価の履歴に記録されているため削除できません。使用停止にしてください"
        else:
            messages.success(request, f"{label} を削除しました（ID は再利用されません）")
            return redirect("recipe_list")
    return render(request, "bakery/recipes/delete.html", {"recipe": recipe, "in_use": in_use, "blocked": blocked})


# --------------------------------------------------------------------------
# S25 中間レシピの置き換え（管理者）
# --------------------------------------------------------------------------


@admin_required
def recipe_replace(request):
    company = request.company
    intermediates = svc.with_usage(Recipe.objects.filter(company=company, kind=RecipeKind.INTERMEDIATE))
    olds = intermediates.filter(used_as_material=True).order_by("name", "version")
    news = intermediates.filter(status=Status.ACTIVE).order_by("name", "-version")

    params = request.POST if request.method == "POST" else request.GET
    old = new = impact = None
    error = ""
    if params.get("old"):
        old = get_object_or_404(Recipe, pk=params["old"], company=company)
    if params.get("new"):
        new = get_object_or_404(Recipe, pk=params["new"], company=company)

    if request.GET.get("done") and old and new:
        return render(request, "bakery/recipes/replace_done.html", {"old": old, "new": new})

    if old and new:
        try:
            if request.method == "POST":
                impact = svc.replace_intermediate(request, old, new)
                messages.success(request, f"{old} を {new} に置き換えました（{len(impact.parents)} 件のレシピ）")
                return redirect(f"{request.path}?done=1&old={old.pk}&new={new.pk}")
            impact = svc.replacement_impact(company, old, new)
        except svc.RecipeRuleError as exc:
            error = str(exc)
            impact = None

    return render(request, "bakery/recipes/replace.html", {
        "olds": olds, "news": news, "old": old, "new": new, "impact": impact, "error": error,
    })
