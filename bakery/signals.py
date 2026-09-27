"""モデルの変更に合わせて行う後片付け。"""

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from bakery.models import ProductPhoto, RecipePhoto


@receiver(post_delete, sender=RecipePhoto)
@receiver(post_delete, sender=ProductPhoto)
def delete_photo_files(sender, instance, **kwargs):
    """写真の行を消したら（レシピ削除で一緒に消えた場合も）ファイルも消す。取り消されたときは消さない。"""
    files = [(f.storage, f.name) for f in (instance.image, instance.thumbnail) if f]
    transaction.on_commit(lambda: [storage.delete(name) for storage, name in files])
