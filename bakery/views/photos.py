"""レシピ・商品の写真（v24・v25）。

追加・説明・並べ替え・削除は、ログインした同じ会社の人なら誰でも行える（写真は原価に影響しないため）。
写真ファイルは MEDIA_URL で直接公開せず、ログインした同じ会社の人にだけ photo_file から返す。

kind は "recipe" か "product"。操作のあとは、呼び出し元に合わせて
- 詳細画面の右上の写真欄（#photo-panel、_photo_panel.html）
- 写真の編集欄（#photo-section、_section.html）
のどちらかだけを描き直す。
"""

from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from bakery.models import Product, ProductPhoto, Recipe, RecipePhoto
from bakery.permissions import member_required
from bakery.services import photos as svc

KINDS = {
    "recipe": (Recipe, RecipePhoto),
    "product": (Product, ProductPhoto),
}


def _owner_model(kind):
    try:
        return KINDS[kind]
    except KeyError:
        raise Http404


def _get_owner(request, kind, pk):
    owner_model, _ = _owner_model(kind)
    return get_object_or_404(owner_model, pk=pk, company=request.company)


def _get_photo(request, kind, pk):
    _, photo_model = _owner_model(kind)
    owner_field = photo_model.OWNER_FIELD
    return get_object_or_404(photo_model.objects.select_related(owner_field), pk=pk,
                             **{f"{owner_field}__company": request.company})


def photo_context(kind, owner, error=""):
    """写真欄のテンプレートに渡す値。商品に写真がないときは採用レシピの代表写真を代わりに見せる。"""
    photos = list(owner.photos.all())
    fallback = None
    if kind == "product" and not photos and owner.adopted_recipe_id:
        fallback = owner.adopted_recipe.photos.first()
    return {"kind": kind, "owner": owner, "photos": photos, "fallback": fallback, "photo_error": error}


def _render(request, kind, owner, error=""):
    template = "bakery/photos/_panel.html" if request.POST.get("panel") else "bakery/photos/_section.html"
    return render(request, template, photo_context(kind, owner, error))


@member_required
def photo_file(request, kind, pk, size):
    photo = _get_photo(request, kind, pk)
    field = photo.thumbnail if size == "thumb" else photo.image
    try:
        response = FileResponse(field.open("rb"), content_type="image/jpeg")
    except FileNotFoundError:
        raise Http404("写真のファイルが見つかりません")
    response["Cache-Control"] = "private, max-age=86400"  # ファイル名は写真ごとに変わるので長めに持ってよい
    return response


@member_required
@require_POST
def photos_add(request, kind, pk):
    owner = _get_owner(request, kind, pk)
    try:
        svc.add_photos(request, owner, request.FILES.getlist("photos"))
    except svc.PhotoError as exc:
        return _render(request, kind, owner, str(exc))
    return _render(request, kind, owner)


@member_required
@require_POST
def photo_caption(request, kind, pk):
    photo = _get_photo(request, kind, pk)
    svc.update_caption(request, photo, request.POST.get("caption", ""))
    return _render(request, kind, photo.owner)


@member_required
@require_POST
def photo_move(request, kind, pk):
    photo = _get_photo(request, kind, pk)
    svc.move_photo(request, photo, request.POST.get("direction", ""))
    return _render(request, kind, photo.owner)


@member_required
@require_POST
def photo_delete(request, kind, pk):
    photo = _get_photo(request, kind, pk)
    owner = photo.owner
    svc.delete_photo(request, photo)
    return _render(request, kind, owner)


@member_required
def product_photos(request, pk):
    """商品の写真の編集（説明・並べ替え・削除）。"""
    product = get_object_or_404(Product.objects.select_related("adopted_recipe"), pk=pk, company=request.company)
    return render(request, "bakery/photos/product_photos.html", {"product": product, **photo_context("product", product)})
