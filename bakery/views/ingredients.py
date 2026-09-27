"""食材の画面（S10〜S14）と、食材登録中の仕入先追加。

登録・修正・アレルゲン確認・使用停止・削除は「食材マスタ管理」なので管理者のみ（spec.md 第4項）。
検索・閲覧は全員。
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from bakery.forms import AllergenForm, IngredientEditForm, IngredientStep1Form, NutritionForm, SupplierQuickForm
from bakery.models import (
    AllergenStatus,
    FoodComposition,
    IdSequence,
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    Product,
    Recipe,
    RecipeItem,
    Status,
    Supplier,
)
from bakery.permissions import admin_required, member_required
from bakery.services import audit
from bakery.services.costing import MaterialKind
from bakery.services.sources import DbSource
from bakery.templatetags.bakery_format import grams_input

PAGE_SIZE = 50


# --------------------------------------------------------------------------
# S10 食材一覧・検索
# --------------------------------------------------------------------------


@member_required
def ingredient_list(request):
    company = request.company
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    status = request.GET.get("status", Status.ACTIVE)
    allergen = request.GET.get("allergen", "")

    rows = Ingredient.objects.filter(company=company).select_related("category", "supplier")
    if q:
        rows = rows.filter(Q(name__icontains=q) | Q(code__icontains=q) | Q(legacy_code__icontains=q))
    if category:
        rows = rows.filter(category_id=category)
    if status in (Status.ACTIVE, Status.STOPPED):
        rows = rows.filter(status=status)
    if allergen == AllergenStatus.UNCONFIRMED:
        rows = rows.filter(allergen_status=AllergenStatus.UNCONFIRMED)
    nutrition = request.GET.get("nutrition", "")
    if nutrition == "missing":
        missing = Q()
        for name in Ingredient.NUTRITION_FIELDS:
            missing |= Q(**{f"{name}__isnull": True})
        rows = rows.filter(missing)

    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "q": q,
        "category": category,
        "status": status,
        "allergen": allergen,
        "nutrition": nutrition,
        "categories": IngredientCategory.objects.filter(company=company),
        "query_without_page": _query_without_page(request),
    }
    if request.headers.get("HX-Request") == "true":
        return render(request, "bakery/ingredients/_list_results.html", context)
    return render(request, "bakery/ingredients/list.html", context)


def _query_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


# --------------------------------------------------------------------------
# S11 食材登録 STEP1 → S12 確認 → 登録
# --------------------------------------------------------------------------


def _step1_feedback(form, touched):
    """入力欄ごとのエラー・自動表示（ID・1g単価）・「登録する」を押せるかどうか。

    まだ触っていない欄の「入力してください」は出さない（開いた直後に赤字だらけにしないため）。
    """
    if not form.is_bound:
        return {"errors": {}, "preview_code": None, "preview_unit_price": None, "can_submit": False}
    form.is_valid()
    errors = {}
    for name in IngredientStep1Form.FIELD_ORDER:
        if name in form.errors and (name in touched or "__all__" in touched):
            errors[name] = form.errors[name][0]
    data = form.cleaned_data
    category = data.get("category")
    weight = data.get("purchase_weight_g")
    price = data.get("purchase_price")
    return {
        "errors": errors,
        "preview_code": IdSequence.peek(form.company, category.id_prefix) if category else None,
        "preview_unit_price": price / weight if weight is not None and price is not None else None,
        "can_submit": not form.errors,
    }


def _render_step1(request, form, touched=(), status=200):
    context = {"form": form, "fb": _step1_feedback(form, set(touched)), "supplier_form": None}
    return render(request, "bakery/ingredients/new.html", context, status=status)


@admin_required
def ingredient_new(request):
    company = request.company
    if request.method != "POST":
        return _render_step1(request, IngredientStep1Form(company=company))

    form = IngredientStep1Form(request.POST, company=company)
    step = request.POST.get("step")

    if step == "back":
        # 確認画面から戻る：入力内容を残したまま STEP1 を表示する
        return _render_step1(request, form, touched=["__all__"])

    if not form.is_valid():
        return _render_step1(request, form, touched=["__all__"], status=422)

    if step == "register":
        ingredient = _register(request, form.cleaned_data)
        messages.success(request, f"食材「{ingredient.name}」を {ingredient.code} として登録しました")
        return redirect("ingredient_registered", pk=ingredient.pk)

    # 「登録する」→ 確認画面（S12）
    data = form.cleaned_data
    return render(request, "bakery/ingredients/confirm.html", {
        "form": form,
        "data": data,
        "preview_code": IdSequence.peek(company, data["category"].id_prefix),
        "unit_price": data["purchase_price"] / data["purchase_weight_g"],
        "duplicates": form.duplicates(),
    })


@transaction.atomic
def _register(request, data):
    company = request.company
    ingredient = Ingredient.objects.create(
        company=company,
        code=IdSequence.issue(company, data["category"].id_prefix),
        name=data["name"],
        category=data["category"],
        supplier=data["supplier"],
        purchase_weight_g=data["purchase_weight_g"],
        purchase_price=data["purchase_price"],
        created_by=request.user,
        updated_by=request.user,
    )
    ingredient.refresh_from_db()  # 履歴には保存後の値（小数第2位まで）を残す
    # 登録時の価格も価格履歴の1行目として残す（第62項6）
    IngredientPriceHistory.objects.create(
        ingredient=ingredient,
        purchase_weight_g=ingredient.purchase_weight_g,
        purchase_price=ingredient.purchase_price,
        changed_by=request.user,
    )
    audit.record(request, "食材登録", ingredient, after=audit.snapshot(ingredient))
    return ingredient


@admin_required
@require_POST
def ingredient_new_check(request):
    """STEP1 の入力途中の確認（htmx）。エラー・発行予定ID・1g単価・登録ボタンだけを差し替える。"""
    form = IngredientStep1Form(request.POST, company=request.company)
    touched = [t for t in request.POST.get("_touched", "").split(",") if t]
    return render(request, "bakery/ingredients/_step1_feedback.html", {
        "form": form, "fb": _step1_feedback(form, set(touched)),
    })


@admin_required
@require_POST
def supplier_quick_add(request):
    """食材登録の途中で、仕入先名だけを登録する（htmx）。仕入先の欄を差し替え、新しい仕入先を選んだ状態にする。"""
    company = request.company
    supplier_form = SupplierQuickForm(request.POST, company=company)
    step1 = IngredientStep1Form(company=company)
    selected = request.POST.get("supplier", "")
    if supplier_form.is_valid():
        supplier = Supplier.objects.create(
            company=company,
            code=IdSequence.issue(company, "SUP"),
            name=supplier_form.cleaned_data["supplier_name"],
            created_by=request.user,
            updated_by=request.user,
        )
        audit.record(request, "仕入先登録", supplier, after=audit.snapshot(supplier))
        selected = str(supplier.pk)
        supplier_form = None
        added = supplier
    else:
        added = None
    response = render(request, "bakery/ingredients/_supplier_block.html", {
        "form": step1, "selected_supplier": selected, "supplier_form": supplier_form, "added": added,
    })
    if added:
        response["HX-Trigger"] = "supplier-added"
    return response


@admin_required
def ingredient_registered(request, pk):
    """登録後に「アレルゲンを今入力する／あとで入力する」を選ぶ（S13 の注記、第17項）。"""
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    return render(request, "bakery/ingredients/registered.html", {"ingredient": ingredient})


# --------------------------------------------------------------------------
# S13 アレルゲン入力
# --------------------------------------------------------------------------


@admin_required
def ingredient_allergens(request, pk):
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    if request.method == "POST":
        form = AllergenForm(request.POST, company=request.company)
        if form.is_valid():
            fields = ["allergens", "allergen_status", "allergen_confirmed_by", "allergen_confirmed_at"]
            with transaction.atomic():
                before = audit.snapshot(ingredient, fields)
                ingredient.allergens.set(form.cleaned_data["allergens"])
                ingredient.allergen_status = AllergenStatus.CONFIRMED
                ingredient.allergen_confirmed_by = request.user
                ingredient.allergen_confirmed_at = timezone.now()
                ingredient.updated_by = request.user
                ingredient.save()
                audit.record(request, "アレルゲン確認", ingredient,
                             before=before, after=audit.snapshot(ingredient, fields))
            messages.success(request, "アレルゲンを確認済みにしました")
            return redirect("ingredient_detail", pk=ingredient.pk)
    else:
        form = AllergenForm(company=request.company,
                            initial={"allergens": list(ingredient.allergens.values_list("pk", flat=True))})
    mandatory, recommended = form.split_by_kind()
    return render(request, "bakery/ingredients/allergens.html", {
        "ingredient": ingredient, "form": form, "mandatory": mandatory, "recommended": recommended,
    })


# --------------------------------------------------------------------------
# S14 食材詳細
# --------------------------------------------------------------------------


@member_required
def ingredient_detail(request, pk):
    company = request.company
    ingredient = get_object_or_404(
        Ingredient.objects.select_related("category", "supplier", "allergen_confirmed_by"),
        pk=pk, company=company,
    )

    direct_ids = set(RecipeItem.objects.filter(ingredient=ingredient).values_list("recipe_id", flat=True))
    all_ids = DbSource(company).recipes_using(MaterialKind.INGREDIENT, ingredient.pk)
    recipes = Recipe.objects.filter(pk__in=all_ids).order_by("code")
    products = Product.objects.filter(adopted_recipe_id__in=all_ids).order_by("code")

    return render(request, "bakery/ingredients/detail.html", {
        "ingredient": ingredient,
        "allergens": ingredient.allergens.all(),
        "price_history": ingredient.price_history.select_related("changed_by")[:20],
        "direct_recipes": [r for r in recipes if r.pk in direct_ids],
        "indirect_recipes": [r for r in recipes if r.pk not in direct_ids],
        "products": products,
        "can_delete": not direct_ids,
    })


# --------------------------------------------------------------------------
# 修正・使用停止・削除（管理者）
# --------------------------------------------------------------------------


@admin_required
def ingredient_edit(request, pk):
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    fields = IngredientEditForm.Meta.fields
    before = audit.snapshot(ingredient, fields)
    if request.method == "POST":
        form = IngredientEditForm(request.POST, instance=ingredient, company=request.company)
        if form.is_valid():
            with transaction.atomic():
                ingredient = form.save(commit=False)
                ingredient.updated_by = request.user
                ingredient.save()
                after = audit.snapshot(ingredient, fields)
                if after != before:
                    audit.record(request, "食材修正", ingredient, before=before, after=after)
            messages.success(request, "食材の情報を保存しました")
            return redirect("ingredient_detail", pk=ingredient.pk)
    else:
        form = IngredientEditForm(instance=ingredient, company=request.company)
    return render(request, "bakery/ingredients/edit.html", {"ingredient": ingredient, "form": form})


@admin_required
@require_POST
def ingredient_set_status(request, pk):
    """使用停止・再開。停止しても過去のレシピ・原価は壊さない。新しいレシピでは選べなくなる（第19項）。"""
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    new_status = request.POST.get("status")
    if new_status in (Status.ACTIVE, Status.STOPPED) and new_status != ingredient.status:
        before = audit.snapshot(ingredient, ["status"])
        ingredient.status = new_status
        ingredient.updated_by = request.user
        ingredient.save(update_fields=["status", "updated_by", "updated_at"])
        action = "食材使用停止" if new_status == Status.STOPPED else "食材使用再開"
        audit.record(request, action, ingredient, before=before, after=audit.snapshot(ingredient, ["status"]))
        messages.success(request, "使用停止にしました" if new_status == Status.STOPPED else "使用を再開しました")
    return redirect("ingredient_detail", pk=ingredient.pk)


@admin_required
def ingredient_delete(request, pk):
    """削除はどのレシピにも使われていない場合だけ。使われていれば使用停止を案内する（第19項）。"""
    ingredient = get_object_or_404(Ingredient, pk=pk, company=request.company)
    in_use = RecipeItem.objects.filter(ingredient=ingredient).exists()
    if request.method == "POST" and not in_use:
        with transaction.atomic():
            audit.record(request, "食材削除", ingredient, before=audit.snapshot(ingredient))
            label = str(ingredient)
            ingredient.allergens.clear()
            ingredient.delete()
        messages.success(request, f"{label} を削除しました（ID は再利用されません）")
        return redirect("ingredient_list")
    return render(request, "bakery/ingredients/delete.html", {"ingredient": ingredient, "in_use": in_use})


# --------------------------------------------------------------------------
# 栄養成分（v26、管理者）
# --------------------------------------------------------------------------


@admin_required
def ingredient_nutrition(request, pk):
    """食材の栄養成分（100gあたり）の入力。成分表から選ぶか、手入力する。空欄で保存すると未入力に戻る。"""
    ingredient = get_object_or_404(Ingredient.objects.select_related("nutrition_food"), pk=pk, company=request.company)
    fields = Ingredient.NUTRITION_FIELDS + ["nutrition_food", "nutrition_note"]
    if request.method == "POST":
        form = NutritionForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                before = audit.snapshot(ingredient, fields)
                for name in fields:
                    setattr(ingredient, name, data.get(name) if name != "nutrition_note" else data.get(name, ""))
                ingredient.updated_by = request.user
                ingredient.save()
                ingredient.refresh_from_db()
                after = audit.snapshot(ingredient, fields)
                if after != before:
                    audit.record(request, "食材栄養成分変更", ingredient, before=before, after=after)
            messages.success(request, "栄養成分を保存しました" if ingredient.nutrition_complete
                             else "栄養成分を未入力に戻しました")
            return redirect("ingredient_detail", pk=ingredient.pk)
    else:
        form = NutritionForm(initial={
            **{name: grams_input(getattr(ingredient, name)) for name in Ingredient.NUTRITION_FIELDS},
            "nutrition_food": ingredient.nutrition_food_id,
            "nutrition_note": ingredient.nutrition_note,
        })
    return render(request, "bakery/ingredients/nutrition.html", {
        "ingredient": ingredient, "form": form, "q": ingredient.name.split()[0] if ingredient.name else "",
    })


@admin_required
def food_search(request):
    """日本食品標準成分表の検索（htmx）。空白で区切った言葉をすべて含む食品を探す。"""
    q = request.GET.get("q", "").strip()
    foods = []
    if q:
        rows = FoodComposition.objects.all()
        for word in q.split():
            rows = rows.filter(Q(name__icontains=word) | Q(food_number=word))
        foods = rows[:40]
    return render(request, "bakery/ingredients/_food_results.html", {"foods": foods, "q": q})
