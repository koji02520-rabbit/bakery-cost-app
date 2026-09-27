"""レシピ・商品の写真（v24・v25）。

保存前に画像を読み直して JPEG に変換する。これで
- スマートフォンの向き情報どおりに回転し、
- 撮影場所などの付帯情報（EXIF）を取り除き、
- 長い辺を 1600px（縮小画像は 480px）までに縮める。
画像として読めないファイルは受け付けない。
"""

from __future__ import annotations

import io

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Max
from PIL import Image, ImageOps, UnidentifiedImageError

from bakery.services import audit

FULL_SIZE = 1600
THUMB_SIZE = 480
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class PhotoError(Exception):
    """写真を受け付けられない。message は画面にそのまま出せる日本語。"""


def _to_jpeg(image, size):
    copy = image.copy()
    copy.thumbnail((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    copy.save(buffer, "JPEG", quality=85, optimize=True)  # exif を渡さないので付帯情報は残らない
    return ContentFile(buffer.getvalue())


def process_image(uploaded):
    """アップロードされたファイル → (写真, 縮小画像) の JPEG。"""
    if uploaded.size > MAX_UPLOAD_BYTES:
        raise PhotoError(f"「{uploaded.name}」は大きすぎます（20MBまで）")
    try:
        with Image.open(uploaded) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise PhotoError(f"「{uploaded.name}」は画像として読み込めません（JPEG・PNG などの写真を選んでください）")
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    return _to_jpeg(image, FULL_SIZE), _to_jpeg(image, THUMB_SIZE)


def _snapshot(owner):
    return [{"id": p.pk, "説明": p.caption, "順": p.sort_order} for p in owner.photos.all()]


def _record(request, owner, action, before):
    label = "商品写真" if owner._meta.model_name == "product" else "レシピ写真"
    audit.record(request, f"{label}{action}", owner, before={"写真": before}, after={"写真": _snapshot(owner)})


@transaction.atomic
def add_photos(request, owner, files):
    """owner はレシピまたは商品（related_name="photos" の写真を持つもの）。"""
    model = owner.photos.model
    files = [f for f in files if f]
    if not files:
        raise PhotoError("写真を選んでください")
    count = owner.photos.count()
    if count + len(files) > model.MAX_PHOTOS:
        raise PhotoError(f"写真は{model.MAX_PHOTOS}枚までです（今 {count} 枚）")
    processed = [process_image(f) for f in files]  # 1枚でも読めなければ、どれも保存しない
    before = _snapshot(owner)
    order = owner.photos.aggregate(m=Max("sort_order"))["m"] or 0
    added = []
    for full, thumb in processed:
        order += 1
        photo = model(**{model.OWNER_FIELD: owner}, sort_order=order, created_by=request.user)
        photo.image.save("photo.jpg", full, save=False)
        photo.thumbnail.save("thumb.jpg", thumb, save=False)
        photo.save()
        added.append(photo)
    _record(request, owner, "追加", before)
    return added


@transaction.atomic
def update_caption(request, photo, caption):
    before = _snapshot(photo.owner)
    photo.caption = caption.strip()[:100]
    photo.save(update_fields=["caption"])
    _record(request, photo.owner, "説明変更", before)


@transaction.atomic
def delete_photo(request, photo):
    owner = photo.owner
    before = _snapshot(owner)
    photo.delete()  # ファイルは post_delete で消す（bakery.signals）
    _record(request, owner, "削除", before)


@transaction.atomic
def move_photo(request, photo, direction):
    """direction: "first"（代表にする）/ "left" / "right"。"""
    owner = photo.owner
    photos = list(owner.photos.all())
    index = next(i for i, p in enumerate(photos) if p.pk == photo.pk)
    target = {"first": 0, "left": max(index - 1, 0), "right": min(index + 1, len(photos) - 1)}.get(direction, index)
    if target == index:
        return
    before = _snapshot(owner)
    photos.insert(target, photos.pop(index))
    for order, p in enumerate(photos, start=1):
        p.sort_order = order
    type(photo).objects.bulk_update(photos, ["sort_order"])
    _record(request, owner, "並べ替え", before)


def copy_photos(source, target, user):
    """レシピのコピー・新バージョン作成時に写真を引き継ぐ。ファイルも複製し、片方を消してももう片方は残る。"""
    model = target.photos.model
    for photo in source.photos.all():
        new = model(**{model.OWNER_FIELD: target}, caption=photo.caption, sort_order=photo.sort_order, created_by=user)
        for field in ("image", "thumbnail"):
            original = getattr(photo, field)
            try:
                with original.open("rb") as f:
                    getattr(new, field).save("photo.jpg", ContentFile(f.read()), save=False)
            except FileNotFoundError:
                break  # 元のファイルがない写真は引き継がない
        else:
            new.save()
