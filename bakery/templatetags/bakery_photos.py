"""写真欄のテンプレート用。レシピと商品で同じテンプレートを使うため、URL名を kind から組み立てる。"""

from django import template
from django.urls import reverse

register = template.Library()


@register.simple_tag
def photo_url(kind, action, *args):
    """{% photo_url kind "file" photo.pk "thumb" %} → recipe_photo_file / product_photo_file"""
    name = f"{kind}_photos_add" if action == "add" else f"{kind}_photo_{action}"
    return reverse(name, args=args)
